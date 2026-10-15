"""
service-a: FastAPI — Orquestador principal
Recibe requests HTTP externos, hace llamadas a service-b y accede a PostgreSQL.
Instrumentado con OpenTelemetry (OTel) SDK: trazas, métricas y logs correlacionados.
"""

import logging
import os
import time
import json
import psycopg2
import httpx
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from pythonjsonlogger import jsonlogger

# ── OTel SDK: imports ────────────────────────────────────────────────────────
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.propagate import inject

# ── Configuración desde variables de entorno ─────────────────────────────────
OTEL_ENDPOINT   = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
SERVICE_B_URL   = os.getenv("SERVICE_B_URL", "http://service-b:8001")
DB_DSN          = os.getenv("DATABASE_URL", "postgresql://app:secret@postgres:5432/appdb")
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", "9090"))
ENV             = os.getenv("ENVIRONMENT", "production")
APP_VERSION     = os.getenv("APP_VERSION", "1.0.0")
# OTEL_ENABLED=false → baseline del benchmark: no se registra ningún provider ni
# instrumentor. La API de OTel cae en sus implementaciones no-op, así el código de
# negocio queda idéntico y solo cambia el costo de la telemetría.
OTEL_ENABLED    = os.getenv("OTEL_ENABLED", "true").lower() != "false"

# ── 1. Resource: identidad del servicio en toda la telemetría ─────────────────
# El Resource viaja en cada señal (traza, métrica, log) para identificar el origen.
resource = Resource.create({
    SERVICE_NAME:    "service-a",
    SERVICE_VERSION: APP_VERSION,
    "deployment.environment": ENV,
    "cloud.provider": os.getenv("CLOUD_PROVIDER", "gcp"),
    # uvicorn corre con --workers 2: cada proceso necesita su propia identidad,
    # si no, sus contadores acumulados se pisan en el Collector/Prometheus
    "service.instance.id": f"{os.getenv('HOSTNAME', 'local')}-{os.getpid()}",
    "host.name":     os.getenv("HOSTNAME", "local"),
})

# ── 2. TracerProvider + OTLP exporter (gRPC → OTel Collector) ────────────────
# ── 3. MeterProvider + OTLP push cada 15s hacia el Collector ──────────────────
# ── 3b. LoggerProvider + OTLP: los logs viajan al Collector (→ Loki / Cloud Logging)
tracer_provider = meter_provider = logger_provider = None
if OTEL_ENABLED:
    tracer_provider = TracerProvider(resource=resource)
    # BatchSpanProcessor: agrupa spans antes de enviar → reduce overhead de red
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    otlp_metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True),
        export_interval_millis=15000,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[otlp_metric_reader])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTEL_ENDPOINT, insecure=True))
    )
    set_logger_provider(logger_provider)

tracer = trace.get_tracer("service-a", APP_VERSION)
meter = metrics.get_meter("service-a", APP_VERSION)

# ── 4. Instrumentos de métricas (SLIs) ───────────────────────────────────────
# Buckets en SEGUNDOS: los default del SDK (0, 5, 10, 25… pensados para ms)
# meten todo en el primer bucket y el p99 sale ~4.95s sin importar la latencia real
LATENCY_BUCKETS_S = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
http_requests_total = meter.create_counter(
    "http_requests_total",
    description="Total HTTP requests recibidos por service-a",
    unit="1",
)
http_request_duration = meter.create_histogram(
    "http_request_duration_seconds",
    description="Distribución de latencia de requests HTTP (p50/p95/p99)",
    unit="s",
    explicit_bucket_boundaries_advisory=LATENCY_BUCKETS_S,
)
db_query_duration = meter.create_histogram(
    "db_query_duration_seconds",
    description="Latencia de queries a PostgreSQL",
    unit="s",
    explicit_bucket_boundaries_advisory=LATENCY_BUCKETS_S,
)
service_b_calls_total = meter.create_counter(
    "service_b_calls_total",
    description="Llamadas HTTP a service-b",
    unit="1",
)
active_requests = meter.create_up_down_counter(
    "http_active_requests",
    description="Requests activos en vuelo (saturación)",
    unit="1",
)

# ── 5. Logging estructurado JSON con trace_id/span_id ────────────────────────
# El trace_id en el log es el PUENTE que correlaciona log ↔ traza en Grafana.
class OtelJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            # Formato hexadecimal de 32 dígitos para trace_id (estándar W3C)
            log_record["trace_id"] = format(ctx.trace_id, "032x")
            log_record["span_id"]  = format(ctx.span_id, "016x")
        log_record["service"]     = "service-a"
        log_record["version"]     = APP_VERSION
        log_record["environment"] = ENV

handler = logging.StreamHandler()
handler.setFormatter(OtelJsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
handlers = [handler]
if OTEL_ENABLED:
    # LoggingHandler adjunta trace_id/span_id del span activo a cada LogRecord OTLP
    handlers.append(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
logging.basicConfig(level=logging.INFO, handlers=handlers)
logger = logging.getLogger("service-a")

# ── 6. Auto-instrumentación de librerías ─────────────────────────────────────
# FastAPIInstrumentor: ver instrument_app() más abajo, junto a la creación de la app
# HTTPXClientInstrumentor propaga el W3C TraceContext hacia service-b
# Psycopg2Instrumentor crea spans para cada query SQL
if OTEL_ENABLED:
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
    Psycopg2Instrumentor().instrument(tracer_provider=tracer_provider)
    # CPU y memoria del proceso → panel "CPU Overhead" del dashboard
    SystemMetricsInstrumentor(config={
        "process.cpu.time": ["user", "system"],
        "process.memory.usage": None,
    }).instrument(meter_provider=meter_provider)

# ── Conexión DB ───────────────────────────────────────────────────────────────
def get_db_connection():
    return psycopg2.connect(DB_DSN)

# ── FastAPI App ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # No se necesita iniciar porque ya PrometheusmETRIC EN LINA 56 ya lo hizo en el serv
    # start_http_server(PROMETHEUS_PORT)
    logger.info("service-a started", extra={"otel_enabled": OTEL_ENABLED})
    yield
    # Shutdown: flushar spans, métricas y logs pendientes
    for provider in (tracer_provider, meter_provider, logger_provider):
        if provider:
            provider.shutdown()

app = FastAPI(
    title="Service A",
    description="Microservicio orquestador — OTel end-to-end lab",
    version=APP_VERSION,
    lifespan=lifespan,
)

# instrument_app sobre la instancia: crea el span SERVER de cada request y extrae
# el header traceparent (W3C). FastAPIInstrumentor().instrument() parchea la clase
# en el módulo fastapi, pero aquí FastAPI ya se importó arriba → no tenía efecto.
if OTEL_ENABLED:
    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider, excluded_urls="health")

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check — no genera trazas (excluido en el Collector)."""
    return {"status": "ok", "service": "service-a"}


@app.get("/order/{order_id}")
async def get_order(order_id: str, request: Request):
    """
    Flujo principal:
    1. Consulta la DB para obtener metadatos del pedido (custom span: fetch.order.db)
    2. Llama a service-b para enriquecer con datos de inventario (propagación W3C)
    3. Retorna la respuesta consolidada
    """
    start = time.time()
    labels = {"endpoint": "/order", "method": "GET"}
    # status_class se fija según el resultado; se cuenta UNA sola vez en el finally
    status_class = "5xx"
    active_requests.add(1, labels)

    try:
        # ── Custom span: lógica de negocio DB ────────────────────────────────
        with tracer.start_as_current_span(
            "fetch.order.db",
            kind=trace.SpanKind.CLIENT,
            attributes={
                "db.system":    "postgresql",
                "db.operation": "SELECT",
                "db.name":      "appdb",
                "order.id":     order_id,
            }
        ) as db_span:
            db_start = time.time()
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, product, quantity, status FROM orders WHERE id = %s",
                    (order_id,)
                )
                row = cur.fetchone()
                conn.close()

                db_duration = time.time() - db_start
                db_query_duration.record(db_duration, {"operation": "SELECT", "table": "orders"})

                if not row:
                    db_span.set_status(trace.StatusCode.ERROR, "Order not found")
                    raise HTTPException(status_code=404, detail=f"Order {order_id} not found")

                order_data = {
                    "id":       row[0],
                    "product":  row[1],
                    "quantity": row[2],
                    "status":   row[3],
                }
                db_span.set_attribute("order.status", order_data["status"])
                logger.info("Order fetched from DB", extra={"order_id": order_id, "status": order_data["status"]})

            except HTTPException:
                raise
            except Exception as e:
                db_span.record_exception(e)
                db_span.set_status(trace.StatusCode.ERROR, str(e))
                logger.error("DB query failed", extra={"error": str(e), "order_id": order_id})
                raise HTTPException(status_code=500, detail="Database error")

        # ── Llamada a service-b (propagación automática de trace context) ────
        with tracer.start_as_current_span(
            "call.service-b.inventory",
            kind=trace.SpanKind.CLIENT,
            attributes={
                "http.method": "GET",
                "peer.service": "service-b",
                "order.product": order_data["product"],
            }
        ) as sb_span:
            service_b_calls_total.add(1, {"status": "attempt"})
            try:
                # HTTPXClientInstrumentor inyecta automáticamente el header
                # traceparent: 00-{trace_id}-{span_id}-01 (W3C TraceContext)
                async with httpx.AsyncClient(timeout=5.0) as client:
                    headers = {}
                    inject(headers)  # propagación explícita como fallback
                    resp = await client.get(
                        f"{SERVICE_B_URL}/inventory/{order_data['product']}",
                        headers=headers,
                    )
                    resp.raise_for_status()
                    inventory = resp.json()

                service_b_calls_total.add(1, {"status": "success"})
                sb_span.set_attribute("http.status_code", resp.status_code)
                sb_span.set_attribute("inventory.available", inventory.get("available", 0))
                logger.info("Inventory fetched from service-b",
                            extra={"product": order_data["product"], "available": inventory.get("available")})
                status_class = "2xx"
            except httpx.HTTPStatusError as e:
                service_b_calls_total.add(1, {"status": "error"})
                sb_span.record_exception(e)
                sb_span.set_status(trace.StatusCode.ERROR, str(e))
                # Respuesta degradada: se contabiliza con la clase de status de service-b
                status_class = f"{e.response.status_code // 100}xx"
                logger.error("service-b call failed", extra={"error": str(e)})
                inventory = {"available": -1, "error": "service-b unavailable"}

        return {
            "order":     order_data,
            "inventory": inventory,
            "trace_id":  format(trace.get_current_span().get_span_context().trace_id, "032x"),
        }

    except HTTPException as e:
        status_class = f"{e.status_code // 100}xx"
        raise
    finally:
        http_requests_total.add(1, {**labels, "status": status_class})
        http_request_duration.record(time.time() - start, labels)
        active_requests.add(-1, labels)


@app.get("/metrics/health")
async def metrics_health():
    """Estado del pipeline de telemetría."""
    return {
        "otel_collector": OTEL_ENDPOINT,
        "prometheus_port": PROMETHEUS_PORT,
        "service_b_url": SERVICE_B_URL,
    }
