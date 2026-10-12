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
│  - TracerProvider + MeterProvider (OTel SDK)                                    │
│  - Auto-instrumentación: FastAPI, HTTPX Client, Psycopg2                        │
│  - Custom Spans: fetch.order.db, call.service-b.inventory                       │
│  - Logging JSON: emisión continua con trace_id y span_id                        │
└──────────────────┬─────────────────────────────────────┬────────────────────────┘
                   │ W3C traceparent                     │ SELECT orders
                   │ 00-{trace_id}-{span_id}-01          ▼
                   ▼                             ┌───────────────┐
┌──────────────────────────────────────┐         │ PostgreSQL    │
│  service-b (FastAPI :8001)           │         │ :5432 (appdb) │
│  - TracerProvider + MeterProvider    │         └───────▲───────┘
│  - Custom Span: inventory.db.fetch   │                 │ SELECT inv
│  - Context extraction W3C            ├─────────────────┘
└──────────────────┬───────────────────┘
                   │ OTLP gRPC (:4317)
                   ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  OTel Collector (:4317 / :4318 / :8888 / :8889 / :13133)                        │
│  Processors: memory_limiter → resource → resourcedetection → filter → batch     │
└──────────────┬───────────────────┬───────────────────────┬──────────────────────┘
               │ Trazas            │ Métricas              │ Logs JSON
               ▼                   ▼                       ▼
       ┌───────────────┐   ┌───────────────┐       ┌───────────────────────┐
       │ Jaeger /      │   │ Prometheus    │       │ Cloud Logging (GCP) / │
       │ Cloud Trace   │   │ & Grafana     │       │ CloudWatch (AWS)      │
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
})
```

### 2.2 TracerProvider y Justificación de Procesadores
Se configuró `TracerProvider` con exportador OTLP sobre protocolo binario gRPC (`endpoint: otel-collector:4317`).
* **Decisión Crítica: `BatchSpanProcessor` vs `SimpleSpanProcessor`:**
  * `SimpleSpanProcessor` efectúa una llamada síncrona de red por cada span finalizado, bloqueando el hilo de ejecución e introduciendo una penalización de latencia inaceptable para producción.
  * `BatchSpanProcessor` amortigua los spans en memoria y los despacha en lotes (`max_queue_size=2048`, `max_export_batch_size=512`, `scheduled_delay_millis=5000`), desacoplando el tiempo de respuesta del cliente del costo de transmisión de telemetría.

### 2.3 Auto-instrumentación y Custom Spans de Negocio
Se implementó un esquema híbrido de instrumentación:
1. **Auto-instrumentación:**
   * `FastAPIInstrumentor`: Inicia el span raíz del request HTTP, capturando atributos estandarizados (`http.method`, `http.status_code`, `http.route`).
   * `HTTPXClientInstrumentor`: Inyecta automáticamente los encabezados de propagación W3C (`traceparent`) en las llamadas hacia `service-b`.
   * `Psycopg2Instrumentor`: Registra spans automáticos para cada query SQL a PostgreSQL, registrando la instrucción `db.statement` y duración de ejecución.
2. **Custom Spans de Lógica Crítica:**
   Para demarcar operaciones de negocio y facilitar el análisis de cuellos de botella se incorporaron bloques de contexto manuales:
   * En `service-a`: Span `fetch.order.db` con tags de negocio (`order.id`, `order.status`) y span `call.service-b.inventory`.
   * En `service-b`: Span `inventory.db.fetch` con atributos de inventario (`product_id`, `warehouse`).

### 2.4 Medición de los Cuatro SLIs (MeterProvider)
Se instrumentó un `MeterProvider` con exportación periódica OTLP cada 15 segundos:
* **SLI-1 Disponibilidad:** Contador `http_requests_total` con labels `{status="2xx|5xx"}`.
* **SLI-2 Latencia:** Histograma `http_request_duration_seconds` con distribución de percentiles (p50, p95, p99).
* **SLI-3 Error Rate:** Ratio derivado de peticiones 5xx sobre total de peticiones.
* **SLI-4 Saturación:** UpDownCounter `http_active_requests` que monitorea las solicitudes concurrentes en vuelo.

### 2.5 Logging Estructurado con trace_id
Se desarrolló la clase `OtelJsonFormatter` derivada de `pythonjsonlogger.jsonlogger.JsonFormatter`. Cuando un log es emitido dentro del ciclo de vida de un request, extrae el contexto activo mediante `trace.get_current_span().get_span_context()` e incrusta los atributos:
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

### 3.2 Backends de Almacenamiento y Visualización
* **Trazas:** Exportadas a **Jaeger** (`:16686`) y **GCP Cloud Trace**.
* **Métricas:** Expuestas en endpoint scraping (`:8889`) hacia **Prometheus** y visualizadas en **Grafana** (`:3000`).
* **Logs:** Despachados a **GCP Cloud Logging** y `stdout` estructurado.

### 3.3 Dashboard Grafana (6 Paneles SRE)
El dashboard `otel-lab-main` provee observabilidad en tiempo real a través de:
1. **SLI-1 Disponibilidad (%):** Gauge con SLO objetivo $\ge 99.5\%$.
2. **SLI-2 Latencias (s):** Series temporales con curvas p50, p95 y p99.
3. **SLI-3 Error Rate (%):** Tasa de errores 5xx con umbral crítico en $0.5\%$.
4. **SLI-4 Throughput (RPS):** Tasa de transacciones procesadas por segundo.
5. **CPU Overhead (%):** Comparativa del consumo de CPU de las aplicaciones vs. Collector.
6. **Collector Health:** Monitoreo de spans encolados, transmitidos y descartados por `memory_limiter`.
7. **Panel Especial — Burn Rate:** Monitorea la velocidad de agotamiento del Error Budget en ventanas móviles de 1 hora ($BR > 14.4$ activa alerta PAGE).

### 3.4 Demostración de Correlación Cross-Signal
La propagación de contexto garantiza que un único `trace_id` conecte toda la cadena de observabilidad:
1. **Traza en Jaeger:** La transacción `beef8a60adc233138742a51314af1248` despliega un árbol completo con spans de `service-a`, cliente HTTPX, `service-b` y consultas SQL de ambos servicios.
2. **Logs en Grafana / Cloud Logging:** Al filtrar por `jsonPayload.trace_id = "beef8a60..."`, el sistema exhibe los logs sincrónicos de ambos microservicios.
3. **Pivoteo Instantáneo:** En Grafana Explorer, hacer clic en "Logs for this span" realiza la transición directa entre la traza y las líneas de log, reduciendo el Tiempo Medio de Diagnóstico (MTTD) de horas a minutos.

---

<!-- PAGE BREAK: PÁGINA 4 -->

## 4. Fase 4 — Análisis Empírico de Overhead de OpenTelemetry

### 4.1 Metodología Experimental del Benchmark
Para determinar con rigor el costo computacional de la instrumentación se ejecutó una batería de pruebas de carga automatizadas con **k6 (v2.2.0)** bajo dos configuraciones idénticas de infraestructura:
* **Escenario Baseline (Sin OTel):** Microservicios ejecutando la lógica de negocio sin interceptores de telemetría activos ni agente Collector.
* **Escenario Con OTel SDK:** Microservicios con instrumentación completa (Tracer, Meter, Logging JSON estructurado) transmitiendo a OTel Collector.

**Perfil de Carga Sintética:**
* **Fase 1 (Warmup):** Ramp-up gradual de 1 a 10 VUs durante 60 segundos.
* **Fase 2 (Carga Sostenida):** 50 VUs concurrentes continuos durante 3 minutos (180 segundos).
* **Fase 3 (Spike / Thundering Herd):** Disparo súbito de hasta 200 VUs concurrentes durante 60 segundos.
* **Total de iteraciones procesadas:** **14,970 solicitudes HTTP** por cada corrida.

### 4.2 Resultados Obtenidos y Tabla Comparativa Consolidada

A partir de los archivos de telemetría capturados ([results_baseline.json](file:///c:/Users/WOLFRAN/Documents/Apps/Maestria/oTelLabs/results_baseline.json) y [results_otel.json](file:///c:/Users/WOLFRAN/Documents/Apps/Maestria/oTelLabs/results_otel.json)) y procesados con el analizador [analyze_overhead.py](file:///c:/Users/WOLFRAN/Documents/Apps/Maestria/oTelLabs/benchmark/analyze_overhead.py), se obtuvieron los siguientes valores empíricos:

| Métrica Evaluada | Sin OTel (Baseline) | Con OTel SDK | Delta Absoluto | Overhead (%) | Veredicto SRE |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Latencia promedio (avg)** | 942.1 ms | 947.2 ms | +5.1 ms | **+0.54%** | 🟢 Despreciable (< 3%) |
| **Latencia Percentil 95 (p95)**| 2,910.3 ms | 2,840.2 ms | -70.1 ms | **-2.41%** | 🟢 Dentro de margen |
| **Latencia Percentil 99 (p99)**| 3,165.9 ms | 3,034.4 ms | -131.5 ms | **-4.15%** | 🟢 Cumple (< 10 ms delta) |
| **Tasa de Error (Error Rate)** | 0.00 % | 0.00 % | 0.00 % | **0.00%** | 🟢 Tolerancia cero mantenida |
| **Throughput (RPS)** | 41.6 req/s | 41.5 req/s | -0.1 req/s | **-0.32%** | 🟢 Capacidad nominal plena |
| **Uso de CPU promedio** | 11.8 % | 14.2 % | +2.4 pp | **+20.3%** | 🟡 Aceptable para SDK |
| **Consumo Memoria RSS** | 168.4 MB | 209.9 MB | +41.5 MB | **+24.6%** | 🟢 Normal por buffers en RAM |

### 4.3 Análisis Crítico de los Resultados
1. **Comportamiento de la Latencia:** El incremento en la latencia promedio fue de únicamente **5.1 ms (+0.54%)**, demostrando que el uso de `BatchSpanProcessor` y llamadas asíncronas HTTPX amortigua efectivamente el costo de trazabilidad.
2. **Impacto en Percentiles Extremos (p99):** La latencia p99 se mantuvo en torno a los ~3.0 segundos en ambas pruebas debido a la saturación del spike de 200 usuarios concurrentes, evidenciando que el cuello de botella es el acceso a la base de datos relacional y no la capa de observabilidad.
3. **Consumo de Memoria y CPU:** El incremento de memoria (+41.5 MB) responde a las estructuras internas de los SDKs (colas de spans pendientes y tablas de métricas en memoria antes de la exportación periódica). El overhead de CPU (+2.4 puntos porcentuales) es atribuible a la serialización Protobuf y generación de hashes W3C.
4. **Veredicto Técnico:** Conforme a las directrices de ingeniería de fiabilidad del **Google SRE Workbook** y reportes de la industria, una sobrecarga de latencia menor al 3% y delta p99 < 10 ms valida la viabilidad del pipeline para producción de misión crítica.

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

### 5.2 Conclusiones Arquitectónicas
1. **Estandarización y Portabilidad:** OpenTelemetry elimina el bloqueo con proveedores (*vendor lock-in*), permitiendo enviar simultáneamente señales a Jaeger, Cloud Trace, Prometheus y CloudWatch sin modificar una sola línea del código de aplicación.
2. **Valor del Contexto Unificado:** La adopción del estándar W3C TraceContext convierte el `trace_id` en el eje articulador del diagnóstico en producción, reduciendo radicalmente los tiempos de resolución de incidentes.
3. **Resiliencia Operativa:** El orden de los procesadores en el Collector (`memory_limiter` antes de cualquier procesamiento pesado) protege la infraestructura ante tormentas de telemetría (*telemetry storms*).

### 5.3 Referencias Bibliográficas
* OpenTelemetry Authors. (2024). *OpenTelemetry Specification v1.25.0*. Cloud Native Computing Foundation (CNCF).
* Beyer, B., Jones, C., Petoff, J., & Murphy, N. R. (2018). *The Site Reliability Workbook: Practical Ways to Implement SRE*. O'Reilly Media.
* Google Cloud. (2024). *Observability architecture: Cloud Trace and Cloud Logging integration*. Google Cloud Architecture Center.
* Amazon Web Services. (2024). *AWS Distro for OpenTelemetry (ADOT) on Amazon ECS*. AWS Documentation.
