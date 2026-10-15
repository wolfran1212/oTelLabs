# REPORTE TÉCNICO: PIPELINE DE OBSERVABILIDAD END-TO-END CON OPENTELEMETRY
## Implementación Multi-Cloud (GCP GKE y AWS ECS) y Análisis Comparativo de Overhead

**Maestría en Arquitectura de Software**  
**Curso:** Observabilidad en Ambientes Productivos — Laboratorio 2  
**Autor:** Maestría en Arquitectura de Software  
**Fecha:** Octubre 2026  
**Repositorio:** [github.com/wolfran1212/oTelLabs](https://github.com/wolfran1212/oTelLabs)  

---

<!-- PAGE BREAK: PÁGINA 1 -->

## 1. Introducción y Arquitectura del Sistema

### 1.1 Resumen Ejecutivo
El presente documento detalla el diseño, instrumentación, despliegue y análisis empírico de un pipeline de observabilidad moderno basado en el estándar abierto **OpenTelemetry (OTel)**. El sistema integra dos microservicios (`service-a` y `service-b`) con dependencia HTTP y persistencia en PostgreSQL, desplegados en entornos de contenedores multi-cloud: **Google Cloud Platform (GCP GKE)** y **Amazon Web Services (AWS ECS Fargate)**.

El objetivo primordial consiste en habilitar la **correlación cross-signal** unificando los tres pilares de la observabilidad (trazas distribuidas, métricas de rendimiento y logs estructurados) a través del identificador unívoco de contexto W3C (`trace_id`), cuantificando adicionalmente el overhead computacional introducido por la instrumentación en escenarios de estrés y carga realista.

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           CLIENTE / k6 BENCHMARK                                │
└────────────────────────────────────────┬────────────────────────────────────────┘
                                         │ HTTP GET /order/{order_id}
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  service-a (FastAPI :8000)                                                      │
│  - TracerProvider + MeterProvider + LoggerProvider (OTel SDK)                   │
│  - Auto-instrumentación: FastAPI, HTTPX Client, Psycopg2, System Metrics        │
│  - Custom Spans: fetch.order.db, call.service-b.inventory                       │
│  - Logging JSON: emisión continua con trace_id y span_id                        │
└──────────────────┬─────────────────────────────────────┬────────────────────────┘
                   │ W3C traceparent                     │ SELECT orders
                   │ 00-{trace_id}-{span_id}-01          ▼
                   ▼                             ┌───────────────┐
┌──────────────────────────────────────┐         │ PostgreSQL    │
│  service-b (FastAPI :8001)           │         │ :5432 (appdb) │
│  - Tracer/Meter/LoggerProvider       │         └───────▲───────┘
│  - Custom Span: inventory.db.fetch   │                 │ SELECT inv
│  - Context extraction W3C            ├─────────────────┘
└──────────────────┬───────────────────┘
                   │ OTLP gRPC (:4317)
                   ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  OTel Collector (:4317 / :4318 / :8888 / :8889 / :13133)                        │
│  Processors: memory_limiter → resource → resourcedetection → filter → batch     │
└──────────────┬───────────────────┬───────────────────────┬──────────────────────┘
               │ Trazas            │ Métricas              │ Logs OTLP
               ▼                   ▼                       ▼
       ┌───────────────┐   ┌───────────────┐       ┌───────────────────────┐
       │ Jaeger /      │   │ Prometheus    │       │ Loki (local) /        │
       │ Tempo         │   │ & Grafana     │       │ Cloud Logging (GKE)   │
       └───────────────┘   └───────────────┘       └───────────────────────┘
```

### 1.2 Especificación de Componentes
1. **Microservicio A (`service-a`):** Servicio orquestador expuesto públicamente. Recibe solicitudes de órdenes, consulta metadatos en PostgreSQL y realiza llamadas HTTP salientes a `service-b`.
2. **Microservicio B (`service-b`):** Servicio interno de inventario. Procesa consultas de stock de productos contra la base de datos PostgreSQL y persiste cache local.
3. **Base de Datos:** PostgreSQL 16 con esquemas relacionales `orders` e `inventory`.
4. **Agente de Telemetría:** OpenTelemetry Collector Contrib (v0.103.0).

---

<!-- PAGE BREAK: PÁGINA 2 -->

## 2. Fase 1 — Decisiones de Diseño de la Instrumentación (OTel SDK)

### 2.1 Identidad del Servicio (`Resource`)
El objeto `Resource` encapsula los metadatos inmutables del entorno y es adjuntado automáticamente a cada señal emitida (traza, métrica o log), garantizando trazabilidad en arquitecturas distribuidas:

```python
resource = Resource.create({
    SERVICE_NAME: "service-a",
    SERVICE_VERSION: "1.0.0",
    "deployment.environment": os.getenv("ENVIRONMENT", "production"),
    "cloud.provider": os.getenv("CLOUD_PROVIDER", "gcp"),
    "host.name": os.getenv("HOSTNAME", "local"),
    "service.instance.id": f"{os.getenv('HOSTNAME', 'local')}-{os.getpid()}",
})
```

El atributo `service.instance.id` es indispensable porque cada contenedor ejecuta `uvicorn --workers 2`: sin una identidad por proceso, ambos workers exportan series con las mismas etiquetas y sus contadores acumulados se sobrescriben en el Collector, falseando los SLIs.

Toda la inicialización del SDK está condicionada por la variable `OTEL_ENABLED` (por defecto `true`). Con `OTEL_ENABLED=false` no se registra ningún provider ni instrumentor y la API de OTel opera en modo *no-op*; el código de negocio permanece idéntico. Este interruptor es el que permite medir el overhead en la Fase 4.

### 2.2 TracerProvider y Justificación de Procesadores
Se configuró `TracerProvider` con exportador OTLP sobre protocolo binario gRPC (`endpoint: otel-collector:4317`).
* **Decisión Crítica: `BatchSpanProcessor` vs `SimpleSpanProcessor`:**
  * `SimpleSpanProcessor` efectúa una llamada síncrona de red por cada span finalizado, bloqueando el hilo de ejecución e introduciendo una penalización de latencia inaceptable para producción.
  * `BatchSpanProcessor` amortigua los spans en memoria y los despacha en lotes (`max_queue_size=2048`, `max_export_batch_size=512`, `scheduled_delay_millis=5000`), desacoplando el tiempo de respuesta del cliente del costo de transmisión de telemetría.

### 2.3 Auto-instrumentación y Custom Spans de Negocio
Se implementó un esquema híbrido de instrumentación:
1. **Auto-instrumentación:**
   * `FastAPIInstrumentor.instrument_app(app)`: Inicia el span raíz (SERVER) del request HTTP, extrae el encabezado `traceparent` entrante y captura atributos estandarizados (`http.method`, `http.status_code`, `http.route`). Se instrumenta la **instancia** de la aplicación y no la clase: `FastAPIInstrumentor().instrument()` reemplaza la clase dentro del módulo `fastapi`, pero como `main.py` importa `FastAPI` antes de esa llamada, la aplicación se construía con la clase original y quedaba sin instrumentar (trazas fragmentadas, sin span raíz y con `service-b` desconectado).
   * `HTTPXClientInstrumentor`: Inyecta automáticamente los encabezados de propagación W3C (`traceparent`) en las llamadas hacia `service-b`.
   * `Psycopg2Instrumentor`: Registra spans automáticos para cada query SQL a PostgreSQL, registrando la instrucción `db.statement` y duración de ejecución.
   * `SystemMetricsInstrumentor`: Emite `process.cpu.time` y `process.memory.usage` de cada proceso, que alimentan el panel de CPU del dashboard.
2. **Custom Spans de Lógica Crítica:**
   Para demarcar operaciones de negocio y facilitar el análisis de cuellos de botella se incorporaron bloques de contexto manuales:
   * En `service-a`: Span `fetch.order.db` con tags de negocio (`order.id`, `order.status`) y span `call.service-b.inventory`.
   * En `service-b`: Span `inventory.db.fetch` con atributos de inventario (`product_id`, `warehouse`).

### 2.4 Medición de los Cuatro SLIs (MeterProvider)
Se instrumentó un `MeterProvider` con exportación periódica OTLP cada 15 segundos:
* **SLI-1 Disponibilidad:** Contador `http_requests_total` con label `status` (`2xx`, `4xx`, `5xx`). Se incrementa **una sola vez por request**, en el bloque `finally`, con la clase de status real de la respuesta.
* **SLI-2 Latencia:** Histograma `http_request_duration_seconds` con distribución de percentiles (p50, p95, p99). Se declaran buckets explícitos en segundos (`5 ms … 10 s`) mediante `explicit_bucket_boundaries_advisory`: los buckets por defecto del SDK (`0, 5, 10, 25, …`) están pensados para milisegundos, y con valores en segundos toda la distribución caía en el primer bucket, produciendo un p99 artificial de ~4.95 s.
* **SLI-3 Error Rate:** Ratio derivado de peticiones 5xx sobre total de peticiones.
* **SLI-4 Saturación:** UpDownCounter `http_active_requests` que monitorea las solicitudes concurrentes en vuelo.

### 2.5 Logging Estructurado con trace_id
Se desarrolló la clase `OtelJsonFormatter` derivada de `pythonjsonlogger.jsonlogger.JsonFormatter`. Cuando un log es emitido dentro del ciclo de vida de un request, extrae el contexto activo mediante `trace.get_current_span().get_span_context()` e incrusta los atributos:
Además del formato JSON a `stdout`, cada servicio registra un `LoggerProvider` con `OTLPLogExporter` y añade un `LoggingHandler` al logger raíz. El handler adjunta de forma nativa `trace_id` y `span_id` al `LogRecord` OTLP, de modo que los logs llegan al Collector por el mismo canal que trazas y métricas:

```python
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTEL_ENDPOINT, insecure=True))
)
set_logger_provider(logger_provider)
handlers.append(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
```

Ejemplo de log JSON en `stdout`:

```json
{
  "timestamp": "2026-10-12T01:27:30.714Z",
  "level": "INFO",
  "service": "service-a",
  "message": "Order fetched from DB",
  "order_id": "ord-001",
  "trace_id": "beef8a60adc233138742a51314af1248",
  "span_id": "a569d797ac756a58"
}
```

---

<!-- PAGE BREAK: PÁGINA 3 -->

## 3. Fase 2 y 3 — Pipeline del OTel Collector y Correlación Cross-Signal

### 3.1 Arquitectura del Pipeline en `collector-config.yaml`
El OpenTelemetry Collector desacopla la aplicación de los backends de telemetría. La configuración encadena procesadores en un orden estrictamente seleccionado:

```
Ingesta: OTLP gRPC/HTTP → memory_limiter → resource → filter/health → batch → Exporters
```

1. **`memory_limiter` (Primer eslabón):** Verifica el uso de memoria cada 0.5s con límite en 400 MiB y margen de pico de 100 MiB. Previene la caída catastrófica por OOM (Out Of Memory) del agente bajo ráfagas intensas descartando carga excedente de forma controlada.
2. **`resource` y `resourcedetection`:** Inyecta metadatos del entorno (`deployment.environment`, `cloud.region`, `gcp`, `ecs`).
3. **`filter/health`:** Descarta trazas cuyo endpoint sea `/health`, evitando polución en los backends y ahorro de error budget.
4. **`batch` (Último eslabón):** Agrupa hasta 1024 spans o espera 5 segundos antes de transmitir hacia los exporters, optimizando el ancho de banda y conexiones TCP.

El exporter `prometheus` se configura con `resource_to_telemetry_conversion: true`, que convierte los atributos del `Resource` (`service.name`, `deployment.environment`, `cloud.provider`) en etiquetas de Prometheus. Sin esta opción la etiqueta `service_name` no existe y la variable `$service` del dashboard queda vacía.

Para el entorno local (`docker compose`) se combina un segundo archivo, `collector-config.local.yaml`, que retira `googlecloud` del pipeline de logs: ese exporter exige credenciales de GCP y, sin ellas, impide que el Collector inicie. En GKE se usa solo la configuración base, con credenciales provistas por Workload Identity.

### 3.2 Backends de Almacenamiento y Visualización
* **Trazas:** Exportadas a **Jaeger** (`:16686`) y **Tempo** (`:3200`).
* **Métricas:** Expuestas en endpoint scraping (`:8889`) hacia **Prometheus** y visualizadas en **Grafana** (`:3000`). Las métricas internas del Collector (`:8888`) las recolecta Prometheus directamente.
* **Logs:** Exportados por OTLP a **Loki 3** (`otlphttp/loki`), que almacena `trace_id` y `span_id` como *structured metadata*, y a **GCP Cloud Logging** en el despliegue GKE. Se conserva además la salida JSON en `stdout`.

### 3.3 Dashboard Grafana (6 Paneles SRE)
El dashboard `otel-lab-main` provee observabilidad en tiempo real a través de:
1. **SLI-1 Disponibilidad (%):** Gauge con SLO objetivo $\ge 99.5\%$.
2. **SLI-2 Latencias (s):** Series temporales con curvas p50, p95 y p99.
3. **SLI-3 Error Rate (%):** Tasa de errores 5xx con umbral crítico en $0.5\%$.
4. **SLI-4 Throughput (RPS):** Tasa de transacciones procesadas por segundo.
5. **CPU Overhead (%):** Consumo de CPU de cada servicio (`process.cpu.time`) frente al del propio Collector.
6. **Collector Health:** Monitoreo de spans encolados, transmitidos y descartados por `memory_limiter`.
7. **Panel Especial — Burn Rate:** Monitorea la velocidad de agotamiento del Error Budget en ventanas móviles de 1 hora ($BR > 14.4$ activa alerta PAGE).

Las consultas de Error Rate y Burn Rate devuelven `0` cuando no existen respuestas 5xx (en lugar de "No data"), para que la ausencia de errores se distinga de la ausencia de datos.

### 3.4 Demostración de Correlación Cross-Signal
La propagación de contexto garantiza que un único `trace_id` conecte toda la cadena de observabilidad:
1. **Traza en Jaeger:** La transacción `beef8a60adc233138742a51314af1248` despliega un árbol completo con spans de `service-a`, cliente HTTPX, `service-b` y consultas SQL de ambos servicios.
2. **Logs en Grafana Explore (Loki):** La consulta `{service_name="service-a"} | trace_id="<trace_id>"` devuelve los logs de ese request; con `service_name=~"service-.*"` aparecen los de ambos microservicios. En GKE, el filtro equivalente en Cloud Logging es `jsonPayload.trace_id`.
3. **Pivoteo bidireccional:** El datasource Loki define un *derived field* sobre `trace_id` que convierte cada valor en un enlace a la traza en Jaeger (log → traza). A la inversa, el datasource Jaeger configura `tracesToLogsV2` y el botón "Logs for this span" abre en Loki los logs del mismo `trace_id` (traza → log). Ambas vistas se muestran lado a lado en el modo *Split* de Explore, reduciendo el Tiempo Medio de Diagnóstico (MTTD) de horas a minutos.

---

<!-- PAGE BREAK: PÁGINA 4 -->

## 4. Fase 4 — Análisis Empírico de Overhead de OpenTelemetry

### 4.1 Metodología Experimental del Benchmark
Para determinar el costo computacional de la instrumentación se ejecutaron pruebas de carga con **k6 (v2.2.0)** sobre la misma infraestructura y el mismo código, variando únicamente la variable de entorno `OTEL_ENABLED` de los servicios:
* **Escenario Baseline (Sin OTel, `OTEL_ENABLED=false`):** No se registran providers ni instrumentors; la API de OTel opera en modo *no-op*. Se conserva el logging JSON a `stdout`, presente en ambos escenarios.
* **Escenario Con OTel SDK (`OTEL_ENABLED=true`):** Instrumentación completa (trazas, métricas y logs) exportando por OTLP al Collector.

> En una primera versión el baseline se obtenía deteniendo el Collector. Ese método no es válido: el SDK sigue creando spans y reintentando su exportación, por lo que no mide el costo de la instrumentación. Los resultados de esa versión se descartaron.

**Perfil de carga** (recomendación del laboratorio: 50–100 usuarios concurrentes durante 5 minutos):
* **Warm-up:** Ramp-up de 1 a 10 VUs durante 60 segundos.
* **Carga sostenida:** **75 VUs constantes durante 5 minutos**, con una pausa aleatoria de 100–600 ms entre requests para simular usuarios reales.
* El escenario de pico (200 VUs) queda como prueba de estrés opcional (`--env SPIKE=true`) y **no** forma parte de la comparación, porque satura la base de datos y su efecto domina el p99.

**Medición de recursos:** Durante los 5 minutos de carga sostenida, `benchmark/collect_stats.py` muestrea `docker stats` de `service-a`, `service-b` y `otel-collector`, y guarda promedios en `stats_baseline.json` / `stats_otel.json`. `benchmark/analyze_overhead.py` consolida estos archivos con `results_baseline.json` / `results_otel.json` (generados por k6).

**Entorno de ejecución:** stack completo en Docker Compose (Docker 27.4, máquina virtual con 8 vCPU y 7.6 GiB RAM) sobre un host Apple M1 Pro. k6 se ejecuta en el mismo host, por lo que compite por CPU con los servicios; esto afecta a ambos escenarios por igual.

### 4.2 Resultados Obtenidos y Tabla Comparativa Consolidada

Valores de `results_*.json` (k6) y `stats_*.json` (`collect_stats.py`), consolidados con `benchmark/analyze_overhead.py`; salidas completas en `benchmark/output/`. Ambas corridas sin errores (25,086 solicitudes baseline, 23,031 con OTel).

| Métrica | Sin OTel (Baseline) | Con OTel SDK | Delta | Overhead (%) |
| :--- | :---: | :---: | :---: | :---: |
| **Latencia promedio** | 564.1 ms | 647.4 ms | +83.3 ms | **+14.8%** |
| **Latencia p95** | 804.8 ms | 899.5 ms | +94.7 ms | **+11.8%** |
| **Latencia p99** | 834.4 ms | 1,035.8 ms | +201.4 ms | **+24.1%** |
| **Tasa de error** | 0.00 % | 0.00 % | +0.00 % | — |
| **Throughput** | 69.5 req/s | 63.8 req/s | -5.7 req/s | **-8.2%** |
| **CPU apps (a + b)** | 159.0 % | 168.1 % | +9.0 pp | **+5.7%** |
| **CPU por request** | 22.9 ms | 26.3 ms | +3.5 ms | **+15.1%** |
| **Memoria apps (a + b)** | 307.9 MiB | 318.0 MiB | +10.1 MiB | **+3.3%** |
| **Memoria OTel Collector** | 59.8 MiB | 107.8 MiB | +48.0 MiB | **+80.3%** |

*CPU por request* = CPU promedio de `service-a` + `service-b` / throughput. Aísla el costo del SDK de la saturación.

### 4.3 Análisis Crítico de los Resultados
1. **Ambos escenarios operan cerca del techo de CPU de `service-a`** (2 workers, techo 200%): ~156% sin OTel y ~161% con OTel. Todo costo extra se traduce en menos throughput (-8.2%) y más cola en los percentiles altos (+201 ms en p99).
2. **Costo intrínseco del SDK: +3.5 ms de CPU por request (+15%).** Cada `/order` genera 11 spans, 3–4 logs OTLP y varias mediciones; en Python la creación de spans y el `LoggingHandler` corren en el hilo del request, solo la exportación es asíncrona.
3. **El cuello de botella principal es la aplicación:** crear un `httpx.AsyncClient` por request cuesta 14.9 ms de CPU (contexto SSL), ~2/3 del costo del baseline; `psycopg2.connect()` por request suma 2.4 ms. Reutilizar cliente y pool aumentaría la capacidad.
4. **Memoria:** +10 MiB en los servicios; el Collector pasó de 60 a 108 MiB, lejos del límite de 400 MiB.
5. **SLO:** `p(95) < 500 ms` se incumple en ambos escenarios: la limitación de capacidad existe con o sin OTel.
6. **Veredicto:** el overhead no es despreciable en un servicio limitado por CPU (~13% del CPU por request). Dimensionar con la instrumentación activa; considerar sampling (`ParentBased(TraceIdRatioBased)` o tail sampling) y revisar el volumen de logs OTLP.
7. **Validez:** una primera corrida con OTel se descartó porque el host se suspendió ~80 s durante la carga (`benchmark/output/k6_otel_descartada_suspension.log`); la reportada se ejecutó con `caffeinate`. Una corrida independiente adicional registró 64.2 req/s y p99 1,039.5 ms, prácticamente idénticos. k6 corre en el mismo host: los valores absolutos no son extrapolables a un clúster dedicado.

### 4.4 Correcciones Posteriores a la Primera Toma de Evidencias
Al revisar la primera versión del laboratorio se identificaron defectos que invalidaban parte de las evidencias. Todos se corrigieron y las evidencias de las secciones 3.3, 3.4 y 4 se volvieron a tomar:

| Defecto detectado | Impacto | Corrección |
| :--- | :--- | :--- |
| `FastAPIInstrumentor().instrument()` invocado después de importar `FastAPI` | Trazas fragmentadas: sin span raíz, `service-b` en trazas separadas | `FastAPIInstrumentor.instrument_app(app)` sobre la instancia |
| Baseline obtenido deteniendo solo el Collector | El SDK seguía activo; el overhead medido no era real (p99 "con OTel" menor que el baseline) | Variable `OTEL_ENABLED` en ambos servicios |
| CPU y memoria sin fuente de datos | Cifras del reporte no verificables | `collect_stats.py` → `stats_*.json` |
| `http_requests_total` se incrementaba 2–3 veces por request y no contaba los 4xx/5xx propios | SLI-1 de disponibilidad distorsionado | Un único incremento por request en `finally` |
| `httpx.HTTPStatusError.status_code` (atributo inexistente) | Un fallo de `service-b` se convertía en un 500 no controlado | `e.response.status_code` |
| Histogramas en segundos con buckets por defecto (ms) | p99 fijo en ~4.95 s, sin relación con la latencia real | Buckets explícitos en segundos |
| Logs solo en `stdout`, sin backend de logs en Grafana | Correlación log ↔ traza no realizable en Grafana Explore | `LoggerProvider` OTLP + Loki + derived field `trace_id` |
| Sin etiqueta `service_name` en Prometheus | Variable `$service` del dashboard vacía | `resource_to_telemetry_conversion: true` |
| Dos workers de uvicorn con la misma identidad | Contadores sobrescritos entre procesos | `service.instance.id` por proceso |
| Panel de CPU consultaba `process_cpu_seconds_total` | Graficaba el CPU de Jaeger, no el de los servicios | `process.cpu.time` vía `SystemMetricsInstrumentor` |
| Healthcheck con `curl`, ausente en `python:slim` | Servicios reportados como `unhealthy` | Healthcheck con `urllib` de Python |

La lección central es que la observabilidad también debe validarse: un dashboard con datos verosímiles no garantiza que las métricas midan lo que dicen medir. Tres de estos defectos (buckets, contador duplicado e instrumentación inactiva) producían valores plausibles pero incorrectos.

---

<!-- PAGE BREAK: PÁGINA 5 -->

## 5. Despliegue Multi-Cloud (IaC), Conclusiones y Referencias

### 5.1 Estrategia de Infraestructura como Código (IaC)

| Dimensión | Google Cloud Platform (GCP GKE) | Amazon Web Services (AWS ECS Fargate) |
| :--- | :--- | :--- |
| **Patrón de Despliegue** | **DaemonSet** (un OTel Collector por nodo) | **Sidecar** (OTel Collector por cada Task) |
| **Autenticación Cloud** | Workload Identity (`roles/cloudtrace.agent`, etc.) | IAM Roles for Tasks (`AWSXRayDaemonWriteAccess`) |
| **Mecanismo de Despliegue** | Terraform (`terraform/gcp/`) + Helm Charts | Terraform (`terraform/aws/`) + Task Definition |
| **Ventaja Arquitectónica** | Menor uso de memoria al compartir Collector | Aislamiento completo de recursos por servicio |
| **Estado** | Desplegado y verificado (clúster, imágenes en GCR, pods) | Definido en IaC; despliegue no verificado |

**Limitación declarada — AWS:** Para AWS se entrega la infraestructura base en Terraform (VPC, clúster ECS, roles IAM para X-Ray y CloudWatch) y la *task definition* de Fargate con el Collector como sidecar. No se ejecutó el despliegue, por lo que no se presentan capturas de X-Ray ni de CloudWatch. Para completarlo falta: (1) agregar `aws_ecs_task_definition` y `aws_ecs_service` al módulo Terraform, y (2) una configuración del Collector específica para AWS con los exporters `awsxray` y `awscloudwatchlogs`, ya que la configuración actual exporta a Jaeger y Cloud Logging.

### 5.2 Conclusiones Arquitectónicas
1. **Estandarización y Portabilidad:** OpenTelemetry elimina el bloqueo con proveedores (*vendor lock-in*), permitiendo enviar simultáneamente señales a Jaeger, Tempo, Prometheus, Loki y Cloud Logging sin modificar el código de aplicación: el cambio de backend se resuelve en la configuración del Collector, como demuestra el override local que retira Cloud Logging.
2. **Valor del Contexto Unificado:** La adopción del estándar W3C TraceContext convierte el `trace_id` en el eje articulador del diagnóstico en producción, reduciendo radicalmente los tiempos de resolución de incidentes.
3. **Validar la telemetría:** Los defectos de la sección 4.4 muestran que una métrica mal instrumentada es más peligrosa que una ausente, porque genera confianza injustificada. Antes de definir SLOs sobre una métrica hay que contrastarla con una fuente independiente, por ejemplo los resultados de k6 frente a los percentiles de Prometheus.
4. **Resiliencia Operativa:** El orden de los procesadores en el Collector (`memory_limiter` antes de cualquier procesamiento pesado) protege la infraestructura ante tormentas de telemetría (*telemetry storms*).

### 5.3 Referencias Bibliográficas
* OpenTelemetry Authors. (2024). *OpenTelemetry Specification v1.25.0*. Cloud Native Computing Foundation (CNCF).
* Beyer, B., Jones, C., Petoff, J., & Murphy, N. R. (2018). *The Site Reliability Workbook: Practical Ways to Implement SRE*. O'Reilly Media.
* Google Cloud. (2024). *Observability architecture: Cloud Trace and Cloud Logging integration*. Google Cloud Architecture Center.
* Amazon Web Services. (2024). *AWS Distro for OpenTelemetry (ADOT) on Amazon ECS*. AWS Documentation.
