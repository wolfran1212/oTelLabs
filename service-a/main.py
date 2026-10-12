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
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.propagate import inject

# ── Configuración desde variables de entorno ─────────────────────────────────
OTEL_ENDPOINT   = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
SERVICE_B_URL   = os.getenv("SERVICE_B_URL", "http://service-b:8001")
DB_DSN          = os.getenv("DATABASE_URL", "postgresql://app:secret@postgres:5432/appdb")
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", "9090"))
ENV             = os.getenv("ENVIRONMENT", "production")
APP_VERSION     = os.getenv("APP_VERSION", "1.0.0")

# ── 1. Resource: identidad del servicio en toda la telemetría ─────────────────
# El Resource viaja en cada señal (traza, métrica, log) para identificar el origen.
resource = Resource.create({
    SERVICE_NAME:    "service-a",
    SERVICE_VERSION: APP_VERSION,
    "deployment.environment": ENV,
    "cloud.provider": os.getenv("CLOUD_PROVIDER", "gcp"),
    "host.name":     os.getenv("HOSTNAME", "local"),
})

# ── 2. TracerProvider + OTLP exporter (gRPC → OTel Collector) ────────────────
tracer_provider = TracerProvider(resource=resource)
otlp_span_exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)
# BatchSpanProcessor: agrupa spans antes de enviar → reduce overhead de red
tracer_provider.add_span_processor(BatchSpanProcessor(otlp_span_exporter))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("service-a", APP_VERSION)

# ── 3. MeterProvider + Prometheus reader (scraping en :9090/metrics) ──────────
# PrometheusMetricReader expone las métricas OTel en formato Prometheus
otlp_metric_exporter = OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True)
otlp_metric_reader = PeriodicExportingMetricReader(otlp_metric_exporter, export_interval_millis=15000)
meter_provider = MeterProvider(
    resource=resource,
    metric_readers=[ otlp_metric_reader]
)
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("service-a", APP_VERSION)

# ── 4. Instrumentos de métricas (SLIs) ───────────────────────────────────────
http_requests_total = meter.create_counter(
    "http_requests_total",
    description="Total HTTP requests recibidos por service-a",
    unit="1",
)
http_request_duration = meter.create_histogram(
    "http_request_duration_seconds",
    description="Distribución de latencia de requests HTTP (p50/p95/p99)",
    unit="s",
)
db_query_duration = meter.create_histogram(
    "db_query_duration_seconds",
    description="Latencia de queries a PostgreSQL",
    unit="s",
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
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("service-a")

# ── 6. Auto-instrumentación de librerías ─────────────────────────────────────
# FastAPIInstrumentor crea spans automáticos para cada endpoint HTTP
# HTTPXClientInstrumentor propaga el W3C TraceContext hacia service-b
# Psycopg2Instrumentor crea spans para cada query SQL
FastAPIInstrumentor().instrument(tracer_provider=tracer_provider)
HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
Psycopg2Instrumentor().instrument(tracer_provider=tracer_provider)

# ── Conexión DB ───────────────────────────────────────────────────────────────
def get_db_connection():
    return psycopg2.connect(DB_DSN)

# ── FastAPI App ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # No se necesita iniciar porque ya PrometheusmETRIC EN LINA 56 ya lo hizo en el serv
    # start_http_server(PROMETHEUS_PORT)
    logger.info("Prometheus metrics server started", extra={"port": PROMETHEUS_PORT})
    yield
    # Shutdown: flushar todos los spans pendientes
    tracer_provider.shutdown()
    meter_provider.shutdown()

app = FastAPI(
    title="Service A",
    description="Microservicio orquestador — OTel end-to-end lab",
    version=APP_VERSION,
    lifespan=lifespan,
)

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
    active_requests.add(1, labels)
    http_requests_total.add(1, labels)

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
                status_labels = {**labels, "status": "2xx"} 
            except httpx.HTTPStatusError as e:
                service_b_calls_total.add(1, {"status": "error"})
                sb_span.record_exception(e)
                sb_span.set_status(trace.StatusCode.ERROR, str(e))
                status_labels = {**labels, "status": f"{e.status_code // 100}xx"}
                http_requests_total.add(1, status_labels)
                logger.error("service-b call failed", extra={"error": str(e)})
                inventory = {"available": -1, "error": "service-b unavailable"}

        total_duration = time.time() - start
        http_requests_total.add(1, status_labels)
        http_request_duration.record(total_duration, labels)

        return {
            "order":     order_data,
            "inventory": inventory,
            "trace_id":  format(trace.get_current_span().get_span_context().trace_id, "032x"),
        }

    finally:
        active_requests.add(-1, labels)


@app.get("/metrics/health")
async def metrics_health():
    """Estado del pipeline de telemetría."""
    return {
        "otel_collector": OTEL_ENDPOINT,
        "prometheus_port": PROMETHEUS_PORT,
        "service_b_url": SERVICE_B_URL,
    }
