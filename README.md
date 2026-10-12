# Pipeline de Observabilidad End-to-End con OpenTelemetry

**Laboratorio 2 — Maestría en Arquitectura de Software**
**Curso: Observabilidad en Ambientes Productivos**

> Implementación completa de un pipeline de observabilidad que captura métricas (Prometheus), logs estructurados y trazas distribuidas (Jaeger/Tempo) desde dos microservicios en GCP GKE y AWS ECS, habilitando correlación cross-signal a través del `trace_id`.

---

## Arquitectura general

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLIENTE / k6 benchmark                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ HTTP GET /order/{id}
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  service-a  (FastAPI :8000)                                          │
│  ┌──────────────────┐  ┌─────────────────┐  ┌───────────────────┐  │
│  │ OTel SDK         │  │ Auto-instr.     │  │ Custom Spans      │  │
│  │ TracerProvider   │  │ FastAPI + httpx │  │ fetch.order.db    │  │
│  │ MeterProvider    │  │ psycopg2        │  │ call.service-b    │  │
│  └────────┬─────────┘  └────────┬────────┘  └────────┬──────────┘  │
│           │ OTLP gRPC           │                     │            │
│           └─────────────────────┴─────────────────────┘            │
│                                 │                                   │
│  /metrics :9090 ←── Prometheus reader                               │
│  logs → stdout JSON (con trace_id)                                  │
└───────────────────┬─────────────────────┬───────────────────────────┘
                    │ W3C traceparent      │ HTTP GET /inventory/{product}
                    │ header propagado     ▼
                    │         ┌───────────────────────────────────────┐
                    │         │  service-b  (FastAPI :8001)           │
                    │         │  OTel SDK — mismo trace_id            │
                    │         │  Custom spans: inventory.db.fetch     │
                    │         │  /metrics :9091                       │
                    │         └────────────────┬──────────────────────┘
                    │                          │ OTLP gRPC
                    ▼                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    OTel Collector  :4317/:4318                       │
│  Receivers:  otlp (gRPC + HTTP) · prometheus · hostmetrics         │
│  Processors: memory_limiter → resource → resourcedetection →       │
│              filter/health → batch                                  │
│  Exporters:                                                         │
│    ├── Jaeger      :14250  (trazas)                                 │
│    ├── Tempo       :4317   (trazas alternativas)                    │
│    ├── Prometheus  :8889   (métricas con scraping)                  │
│    ├── Cloud Logging / CloudWatch (logs JSON)                       │
│    └── logging     (debug stdout)                                   │
└───────────────┬─────────────────┬───────────────────────────────────┘
                │                 │
                ▼                 ▼
    ┌───────────────┐   ┌──────────────────────┐
    │ Jaeger :16686 │   │ Prometheus :9090/9091 │
    │ Trace UI      │   │ + Grafana :3000        │
    └───────────────┘   │   6 paneles SLI       │
                        └──────────────────────┘
```

---

## Estructura del repositorio

```
otel-lab/
├── service-a/
│   ├── main.py              # FastAPI + OTel SDK completo
│   ├── requirements.txt     # Dependencias Python
│   └── Dockerfile
│
├── service-b/
│   ├── main.py              # FastAPI + OTel SDK + cache en memoria
│   ├── requirements.txt
│   └── Dockerfile
│
├── otel-collector/
│   └── collector-config.yaml  # Pipeline completo: receivers/processors/exporters
│
├── prometheus/
│   └── prometheus.yaml      # Configuración de scraping
│
├── tempo/
│   └── tempo.yaml           # Backend de trazas Grafana stack
│
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/     # Prometheus + Jaeger + Tempo auto-configurados
│   │   └── dashboards/
│   └── dashboards/
│       └── otel-lab-dashboard.json  # 8 paneles (6 SLIs + burn rate + propagación)
│
├── k8s/
│   ├── gcp/
│   │   └── deployment.yaml  # GKE: DaemonSet Collector + Deployments + HPA
│   └── aws/
│       └── ecs-task-definition.json  # ECS Fargate task definition
│
├── benchmark/
│   ├── k6_benchmark.js      # Escenarios: warmup + carga sostenida + spike
│   └── analyze_overhead.py  # Análisis comparativo baseline vs. OTel
│
├── scripts/
│   └── init-db.sql          # Esquema PostgreSQL + datos de prueba
│
├── docker-compose.yaml      # Stack completo local
└── README.md
```

---

## Fase 1 — Instrumentación con OTel SDK

### Decisiones de diseño de la instrumentación

#### 1. Resource: identidad del servicio

El `Resource` es el objeto más importante de la configuración OTel: viaja en **cada señal** (traza, métrica, log) y permite a los backends saber de dónde vienen los datos.

```python
# service-a/main.py — líneas 44-52
resource = Resource.create({
    SERVICE_NAME:    "service-a",          # Nombre del servicio en Jaeger/Grafana
    SERVICE_VERSION: APP_VERSION,          # Para correlacionar incidentes con releases
    "deployment.environment": ENV,         # production / staging / development
    "cloud.provider": os.getenv("CLOUD_PROVIDER", "gcp"),
    "host.name":     os.getenv("HOSTNAME", "local"),
})
```

#### 2. TracerProvider: configuración de trazas

```python
# service-a/main.py — líneas 55-60
tracer_provider = TracerProvider(resource=resource)
otlp_span_exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)

# BatchSpanProcessor: agrupa spans en memoria antes de enviarlos al Collector
# → Reduce el overhead de red: en lugar de 1 request HTTP por span,
#   envía lotes de hasta 512 spans cada 5 segundos
tracer_provider.add_span_processor(BatchSpanProcessor(otlp_span_exporter))
```

**Por qué BatchSpanProcessor y no SimpleSpanProcessor:**

| Procesador | Cuándo usar | Overhead |
|---|---|---|
| `SimpleSpanProcessor` | Solo desarrollo/debug | Alto: 1 request/span |
| `BatchSpanProcessor` | Producción | Bajo: agrupa spans |

#### 3. MeterProvider: métricas con doble exportación

```python
# service-a/main.py — líneas 63-74
# Reader 1: expone /metrics en formato Prometheus (scraping local)
prometheus_reader = PrometheusMetricReader()

# Reader 2: envía métricas al Collector cada 15 segundos (OTLP push)
otlp_metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True),
    export_interval_millis=15000
)

meter_provider = MeterProvider(
    resource=resource,
    metric_readers=[prometheus_reader, otlp_metric_reader]  # Los dos simultáneamente
)
```

La doble exportación permite:
- Scraping directo por Prometheus (`http://service-a:9090/metrics`)
- Push al Collector para Cloud Monitoring (GCP) o CloudWatch (AWS)

#### 4. Los 4 SLIs instrumentados

```python
# service-a/main.py — líneas 77-95

# SLI-1: Disponibilidad
http_requests_total = meter.create_counter(
    "http_requests_total",
    description="Total HTTP requests recibidos",
)
# → Prometheus query: rate(http_requests_total{status=~"2.."}[5m]) / rate(http_requests_total[5m])

# SLI-2: Latencia (histograma para percentiles p50/p95/p99)
http_request_duration = meter.create_histogram(
    "http_request_duration_seconds",
    description="Distribución de latencia HTTP",
)
# → Prometheus query: histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))

# SLI-3: Error Rate (derivado de http_requests_total con status=5xx)

# SLI-4: Saturación
active_requests = meter.create_up_down_counter(
    "http_active_requests",
    description="Requests activos en vuelo",
)
```

#### 5. Logging estructurado: el trace_id como pivote de correlación

```python
# service-a/main.py — líneas 98-116
class OtelJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        span = trace.get_current_span()
        ctx  = span.get_span_context()
        if ctx and ctx.is_valid:
            # trace_id en formato hexadecimal de 32 dígitos (estándar W3C)
            log_record["trace_id"] = format(ctx.trace_id, "032x")
            log_record["span_id"]  = format(ctx.span_id, "016x")
        log_record["service"] = "service-a"
```

Cada línea de log emitida dentro de un span automáticamente incluye el `trace_id`:

```json
{
  "timestamp": "2024-01-15T10:30:45.123Z",
  "level": "INFO",
  "message": "Order fetched from DB",
  "trace_id": "1a2b3c4d5e6f7890abcdef1234567890",
  "span_id":  "abcdef1234567890",
  "service":  "service-a",
  "order_id": "ord-001",
  "status":   "delivered"
}
```

En Grafana Explorer: busca logs por `trace_id` → click en el valor → navega directamente al flame graph en Jaeger. Ese es el **diagnóstico en 4 minutos** vs. el war room de 4 horas.

#### 6. Auto-instrumentación vs. Custom Spans

```python
# service-a/main.py — líneas 119-122
FastAPIInstrumentor().instrument(tracer_provider=tracer_provider)
# → Crea automáticamente un span por cada endpoint HTTP
# → Atributos: http.method, http.route, http.status_code, http.url

HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
# → Inyecta automáticamente el header W3C traceparent en cada llamada a service-b
# → El header propagado: "traceparent: 00-{trace_id}-{span_id}-01"

Psycopg2Instrumentor().instrument(tracer_provider=tracer_provider)
# → Crea spans para cada query SQL con: db.statement, db.operation, db.name
```

**Custom spans para lógica de negocio** (no cubiertos por auto-instr.):

```python
# service-a/main.py — líneas 167-185
with tracer.start_as_current_span(
    "fetch.order.db",                      # Nombre del span en Jaeger
    kind=trace.SpanKind.CLIENT,
    attributes={
        "db.system":    "postgresql",
        "db.operation": "SELECT",
        "order.id":     order_id,          # Atributo de negocio custom
    }
) as db_span:
    # El código dentro del `with` está cubierto por el span
    # Si lanza una excepción, el span se marca como ERROR automáticamente
    result = query_database(order_id)
    db_span.set_attribute("order.status", result["status"])
```

#### 7. Propagación del contexto entre servicios (W3C TraceContext)

```
service-a                              service-b
    │                                      │
    │── span: GET /order/ord-001 ─────────┐│
    │   │                                 ││
    │   │── span: fetch.order.db          ││
    │   │                                 ││
    │   │── span: call.service-b ─────────┼┼──→ HTTP GET /inventory/LAPTOP-X1
    │   │         traceparent:            ││    Header: traceparent: 00-abc123-xyz789-01
    │   │         00-abc123-xyz789-01     ││         ↑
    │   │                                 ││    Este header es el MISMO trace_id
    │   │                                 ││    service-b continúa el mismo trace
    │   │                                 │└── span: GET /inventory/LAPTOP-X1 (hijo)
    │   │                                 │    span: inventory.db.fetch (nieto)
```

En el flame graph de Jaeger, todos estos spans aparecen bajo el **mismo trace_id**, formando un árbol que muestra el recorrido completo del request.

---

## Fase 2 — OTel Collector: el corazón del pipeline

### Anatomía del pipeline en collector-config.yaml

```yaml
# otel-collector/collector-config.yaml

service:
  pipelines:
    traces:
      receivers:  [otlp]
      processors: [memory_limiter, resource, resourcedetection, filter/health, batch]
      exporters:  [jaeger, otlp/tempo, logging]

    metrics:
      receivers:  [otlp, prometheus, hostmetrics]
      processors: [memory_limiter, resource, resourcedetection, attributes/metrics, batch]
      exporters:  [prometheus, logging]

    logs:
      receivers:  [otlp]
      processors: [memory_limiter, resource, batch]
      exporters:  [googlecloud, awscloudwatchlogs, logging]
```

### Por qué el orden de los processors importa

```
memory_limiter  →  resource  →  filter  →  batch
     ↑                ↑            ↑          ↑
 Primero para    Enriquece     Descarta    Último para
 evitar OOM      antes de      antes de    agrupar y
 del Collector   filtrar       enviar      enviar eficiente
```

**`memory_limiter` primero**: si hay un spike de telemetría, el Collector empieza a rechazar datos antes de usar toda la RAM del nodo y caerse. Un Collector caído es invisible — peor que datos perdidos.

**`batch` último**: agrupa todos los spans procesados antes de enviarlos al backend. Configuración:

```yaml
processors:
  batch:
    timeout: 5s            # Envía aunque el batch no esté lleno (máximo wait)
    send_batch_size: 1024  # Envía cuando tenga 1024 spans
    send_batch_max_size: 2048
```

### filter/health: excluir endpoints de health check

```yaml
processors:
  filter/health:
    error_mode: ignore
    traces:
      span:
        - 'attributes["http.target"] == "/health"'
```

Sin este filtro, Prometheus hace un scraping a `/health` cada 15 segundos. Con 10 réplicas, eso son 600 spans/minuto que no aportan valor diagnóstico y consumen error budget del Collector.

---

## Fase 3 — Backends y Visualización

### Verificar propagación de contexto

```bash
# 1. Hacer un request a service-a
curl http://localhost:8000/order/ord-001 | python3 -m json.tool

# La respuesta incluye el trace_id:
# {
#   "order": {...},
#   "inventory": {...},
#   "trace_id": "1a2b3c4d5e6f7890abcdef1234567890"
# }

# 2. Buscar la traza en Jaeger
# Abrir: http://localhost:16686
# Service: service-a → Find Traces
# Verificar que el trace tiene spans de AMBOS servicios bajo el mismo trace_id
```

**Lo que debes ver en el flame graph de Jaeger:**

```
trace_id: 1a2b3c4d5e6f7890abcdef1234567890   [total: ~180ms]
│
├── service-a: GET /order/ord-001            [180ms]
│   ├── service-a: fetch.order.db            [45ms] ← Custom span
│   │   └── service-a: SELECT orders...      [40ms] ← Auto-instr. psycopg2
│   └── service-a: call.service-b.inventory  [130ms] ← Custom span
│       └── service-b: GET /inventory/...    [125ms] ← Propagación W3C ✅
│           └── service-b: inventory.db.fetch [120ms] ← Custom span
│               └── service-b: SELECT inv... [115ms] ← Auto-instr.
```

Si el span de `service-b` aparece como hijo del de `service-a` → la propagación W3C TraceContext funciona correctamente.

### Verificar correlación log ↔ traza en Grafana

```bash
# En Grafana Explorer (http://localhost:3000)
# Seleccionar datasource: Jaeger
# Buscar trace_id: 1a2b3c4d5e6f7890abcdef1234567890
# Click en "Logs for this span" → navega a los logs con ese trace_id

# O en Cloud Logging (GCP):
gcloud logging read 'jsonPayload.trace_id="1a2b3c4d5e6f7890abcdef1234567890"' \
  --project=TU_PROYECTO --limit=50 --format=json
```

### Dashboard Grafana — los 6 paneles principales

| Panel | Métrica Prometheus | SLO Objetivo |
|---|---|---|
| SLI-1: Disponibilidad | `rate(http_requests_total{status=~"2.."}[5m]) / rate(http_requests_total[5m])` | ≥ 99.5% |
| SLI-2: Latencia p95 | `histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))` | < 500ms |
| SLI-3: Error Rate | `rate(http_requests_total{status=~"5.."}[5m]) / rate(...)` | ≤ 0.5% |
| SLI-4: Throughput | `rate(http_requests_total[5m])` | Referencial |
| CPU Overhead | `rate(process_cpu_seconds_total[5m]) * 100` | < 5% overhead |
| Collector Health | `rate(otelcol_exporter_sent_spans_total[5m])` | 0 spans rechazados |

Adicionalmente, el panel de **Burn Rate** muestra:
```promql
(rate(http_requests_total{status=~"5.."}[1h]) / rate(http_requests_total[1h])) / 0.005
```
Un burn rate > 14.4 en ventana de 1h debe disparar una alerta PAGE (Clase 2).

---

## Fase 4 — Análisis de Overhead

### Método de medición

El overhead de OTel se mide comparando dos ejecuciones idénticas del mismo código:
- **Baseline**: sin `FastAPIInstrumentor`, sin `BatchSpanProcessor`, sin `/metrics`
- **Con OTel**: configuración completa del laboratorio

```bash
# Paso 1: Levantar el stack local
docker compose up -d
docker compose ps  # Verificar que todos los servicios están healthy

# Paso 2: Benchmark BASELINE (sin instrumentación)
# Primero, detener el OTel Collector para simular el baseline:
docker compose stop otel-collector

k6 run \
  --env INSTRUMENTED=false \
  --env BASE_URL=http://localhost:8000 \
  --out csv=results_baseline.csv \
  benchmark/k6_benchmark.js

# Paso 3: Levantar el Collector y ejecutar CON OTel
docker compose start otel-collector
sleep 10  # Esperar a que el Collector esté healthy

k6 run \
  --env INSTRUMENTED=true \
  --env BASE_URL=http://localhost:8000 \
  --out csv=results_otel.csv \
  benchmark/k6_benchmark.js

# Paso 4: Analizar diferencias
python3 benchmark/analyze_overhead.py
```

### Tabla comparativa de resultados (valores de referencia)

| Métrica | Sin OTel (baseline) | Con OTel SDK | Overhead |
|---|---|---|---|
| Latencia promedio (ms) | ~45 ms | ~48 ms | +6.7% |
| Latencia p95 (ms) | ~120 ms | ~128 ms | +6.7% |
| Latencia p99 (ms) | ~280 ms | ~295 ms | +5.4% |
| CPU promedio (%) | 8% | 10% | +25% (+2pp) |
| Memoria RSS (MB) | 85 MB | 112 MB | +32% (+27MB) |
| Error rate (%) | 0.01% | 0.01% | 0% |
| Throughput (RPS) | 245 | 238 | -2.9% |

> **Nota**: Los valores anteriores son de referencia. Los valores reales dependen del hardware del nodo y la configuración del Collector. Documentar los obtenidos en el laboratorio en esta tabla.

**Overhead del OTel Collector** (proceso separado):
- CPU adicional: ~100-300m cores (configurable con `memory_limiter`)
- Memoria: ~150-300 MB dependiendo del volumen de telemetría y tamaño del batch

**Veredicto de referencia industria**: un overhead de p99 < 10ms es considerado aceptable para la mayoría de servicios según la documentación de OpenTelemetry y estudios de Datadog (2024). El valor adicional de la observabilidad (reducción de MTTD de horas a minutos) supera ampliamente este costo.

---

## Inicio rápido — Entorno local

```bash
# 1. Clonar y navegar
git clone https://github.com/mafeopa96/otel-lab
cd otel-lab

# 2. Levantar el stack completo
docker compose up -d

# 3. Esperar a que todos los servicios estén healthy (~60 segundos)
docker compose ps

# 4. Verificar endpoints
curl http://localhost:8000/health              # service-a
curl http://localhost:8001/health              # service-b
curl http://localhost:13133/                   # OTel Collector health
curl http://localhost:16686/                   # Jaeger UI (browser)
curl http://localhost:3000/                    # Grafana (admin/admin)

# 5. Generar tráfico de prueba
curl "http://localhost:8000/order/ord-001"
curl "http://localhost:8000/order/ord-002"
curl "http://localhost:8000/order/ord-003"

# 6. Ver la traza en Jaeger
# Abrir http://localhost:16686 → Service: service-a → Find Traces

# 7. Ver el dashboard en Grafana
# Abrir http://localhost:3000 → Dashboards → OTel Lab

# 8. Ver logs con trace_id correlacionados
docker compose logs service-a | grep trace_id | head -5
```

---

## Despliegue en GCP GKE

```bash
# Pre-requisitos: gcloud CLI, kubectl, Docker

# 1. Crear cluster GKE
gcloud container clusters create otel-lab \
  --region=us-central1 \
  --num-nodes=3 \
  --machine-type=e2-standard-4 \
  --enable-autoscaling \
  --min-nodes=2 \
  --max-nodes=10

# 2. Configurar kubectl
gcloud container clusters get-credentials otel-lab --region=us-central1

# 3. Crear namespace
kubectl create namespace otel-lab
kubectl config set-context --current --namespace=otel-lab

# 4. Construir y publicar imágenes
export GCP_PROJECT=$(gcloud config get-value project)

docker build -t gcr.io/$GCP_PROJECT/service-a:1.0.0 service-a/
docker build -t gcr.io/$GCP_PROJECT/service-b:1.0.0 service-b/

docker push gcr.io/$GCP_PROJECT/service-a:1.0.0
docker push gcr.io/$GCP_PROJECT/service-b:1.0.0

# 5. Crear secrets
kubectl create secret generic gcp-credentials \
  --from-literal=project_id=$GCP_PROJECT

# 6. Crear ConfigMap del Collector
kubectl create configmap collector-config \
  --from-file=collector-config.yaml=otel-collector/collector-config.yaml

# 7. Aplicar manifiestos
sed -i "s/\${GCP_PROJECT}/$GCP_PROJECT/g" k8s/gcp/deployment.yaml
kubectl apply -f k8s/gcp/deployment.yaml

# 8. Verificar despliegue
kubectl get pods -n otel-lab
kubectl get services -n otel-lab

# 9. Obtener IP externa de service-a
kubectl get svc service-a-svc -n otel-lab -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

---

## Despliegue en AWS ECS Fargate

```bash
# Pre-requisitos: AWS CLI, Docker

# 1. Autenticar con ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS \
  --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com

# 2. Crear repositorios ECR
aws ecr create-repository --repository-name otel-lab/service-a --region us-east-1
aws ecr create-repository --repository-name otel-lab/service-b --region us-east-1

# 3. Construir y publicar
docker build -t $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/otel-lab/service-a:1.0.0 service-a/
docker build -t $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/otel-lab/service-b:1.0.0 service-b/

docker push $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/otel-lab/service-a:1.0.0
docker push $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/otel-lab/service-b:1.0.0

# 4. Crear parámetros SSM
aws ssm put-parameter \
  --name "/otel-lab/db-url" \
  --value "postgresql://app:secret@RDS_ENDPOINT:5432/appdb" \
  --type SecureString

# 5. Registrar task definition
sed -i "s/ACCOUNT_ID/$AWS_ACCOUNT_ID/g" k8s/aws/ecs-task-definition.json
aws ecs register-task-definition \
  --cli-input-json file://k8s/aws/ecs-task-definition.json

# 6. Crear servicio ECS
aws ecs create-service \
  --cluster otel-lab \
  --service-name otel-lab-svc \
  --task-definition otel-lab \
  --desired-count 2 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[SUBNET_ID],securityGroups=[SG_ID],assignPublicIp=ENABLED}"
```

---

## Troubleshooting frecuente

### Los spans no aparecen en Jaeger

```bash
# 1. Verificar que el Collector está recibiendo datos
curl http://localhost:55679/debug/tracez  # zPages del Collector

# 2. Verificar que el Collector puede llegar a Jaeger
docker compose logs otel-collector | grep -i "jaeger\|error\|failed"

# 3. Verificar que service-a envía al Collector
docker compose logs service-a | grep -i "otlp\|collector"

# 4. Verificar la conectividad de red
docker compose exec service-a curl http://otel-collector:13133/
```

### El trace_id no aparece en los logs

```bash
# Verificar que la instrumentación FastAPI está activa antes de crear el span
# El orden de inicialización importa:
# 1. TracerProvider se configura
# 2. FastAPIInstrumentor().instrument(tracer_provider=tracer_provider)
# 3. App FastAPI se crea
# Si el orden es incorrecto, el contexto del span no está disponible en los logs

docker compose logs service-a | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        obj = json.loads(line.strip())
        if 'trace_id' in obj:
            print('✅ trace_id encontrado:', obj['trace_id'])
            break
    except: pass
"
```

### Prometheus no scrapea las métricas

```bash
# Verificar endpoint de métricas de service-a
curl http://localhost:9090/metrics | grep http_requests_total

# Verificar configuración de Prometheus
curl http://localhost:9091/api/v1/targets | python3 -m json.tool | grep -A5 "service-a"
```

---

## Referencias

- Chen, K., Patnala, V., Carraway, D. & Deo, P. (2019). *Engineering Reliable Mobile Applications*. Google / O'Reilly. sre.google
- OpenTelemetry Project. (2024). *Python API & SDK*. https://opentelemetry.io/docs/languages/python/
- OpenTelemetry Project. (2024). *Collector Configuration*. https://opentelemetry.io/docs/collector/
- Thurgood, S. et al. (2018). *Alerting on SLOs*, SRE Workbook Cap. 5. sre.google/workbook/alerting-on-slos/
- Grafana Labs. (2024). *Trace Correlations in Grafana*. https://grafana.com/docs/grafana/latest/explore/trace-integration/
- Google Cloud. (2024). *Cloud Trace + OpenTelemetry*. https://cloud.google.com/trace/docs/setup/python-ot
- AWS. (2024). *AWS Distro for OpenTelemetry*. https://aws-otel.github.io/
- Ochoa Paipilla, M. F. (2026). *sre_training_stacktools*. GitHub: mafeopa96/sre_training_stacktools
