#!/usr/bin/env python3
"""Genera docs/Reporte_tecnico_oTelLab.pdf (formato APA) desde los resultados reales del lab.

Lee results_*.json, stats_*.json y benchmark/output/k6_*.log, e imprime a PDF con Chrome headless:
    python3 docs/build_report.py
"""
import html, json, re, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_HTML = Path(tempfile.gettempdir()) / "reporte_otellab.html"
OUT_PDF = REPO / "docs" / "Reporte_tecnico_oTelLab.pdf"
SHOTS = REPO / "docs" / "screenshots"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

load = lambda p: json.loads((REPO / p).read_text())
b, o = load("results_baseline.json")["metrics"], load("results_otel.json")["metrics"]
sb, so = load("stats_baseline.json"), load("stats_otel.json")

def k6_requests(log):
    txt = re.sub(r"\x1b\[[0-9;]*m", "", (REPO / "benchmark/output" / log).read_text())
    return int(re.search(r"http_reqs\.+: (\d+)", txt).group(1))
nb_req, no_req = k6_requests("k6_baseline.log"), k6_requests("k6_otel.log")

def es(x, d=1):  # formato numérico español: 1.234,5
    s = f"{x:,.{d}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")
def sg(x, d=1): return ("+" if x >= 0 else "−") + es(abs(x), d)
def pct(bv, ov): return (ov - bv) / bv * 100 if bv else 0.0

cpu_b, cpu_o = sb["apps"]["cpu_avg_pct"], so["apps"]["cpu_avg_pct"]
mem_b, mem_o = sb["apps"]["mem_avg_mib"], so["apps"]["mem_avg_mib"]
cpr_b, cpr_o = cpu_b * 10 / b["throughput_rps"], cpu_o * 10 / o["throughput_rps"]
col_b, col_o = sb["containers"]["otel-collector"], so["containers"]["otel-collector"]
sa_b, sa_o = sb["containers"]["service-a"]["cpu_avg_pct"], so["containers"]["service-a"]["cpu_avg_pct"]
V = dict(b=b, o=o, cpr_b=cpr_b, cpr_o=cpr_o, cpu_b=cpu_b, cpu_o=cpu_o, mem_b=mem_b, mem_o=mem_o,
         col_b=col_b, col_o=col_o, sa_b=sa_b, sa_o=sa_o, nb_req=nb_req, no_req=no_req)

def img(name, num, caption):
    return f'''<figure><img src="file://{SHOTS / name}"><figcaption><b>Figura {num}.</b> {caption}</figcaption></figure>'''

def code(s):
    return f"<pre>{html.escape(s.strip())}</pre>"

def table_overhead():
    rows = [
        ("Latencia promedio", b["latency_avg_ms"], o["latency_avg_ms"], "ms", 1),
        ("Latencia percentil 95 (p95)", b["latency_p95_ms"], o["latency_p95_ms"], "ms", 1),
        ("Latencia percentil 99 (p99)", b["latency_p99_ms"], o["latency_p99_ms"], "ms", 1),
        ("Tasa de errores", b["error_rate_pct"], o["error_rate_pct"], "%", 2),
        ("Throughput", b["throughput_rps"], o["throughput_rps"], "req/s", 1),
        ("Uso medio de CPU (service-a + service-b)", cpu_b, cpu_o, "%", 1),
        ("<b>CPU por solicitud</b>", cpr_b, cpr_o, "ms", 1),
        ("Memoria (service-a + service-b)", mem_b, mem_o, "MiB", 1),
        ("Memoria del OTel Collector", col_b["mem_avg_mib"], col_o["mem_avg_mib"], "MiB", 1),
    ]
    tr = []
    for label, bv, ov, u, d in rows:
        du = "pp" if u == "%" and "CPU" in label else u
        tr.append(f"<tr><td>{label}</td><td>{es(bv,d)} {u}</td><td>{es(ov,d)} {u}</td>"
                  f"<td>{sg(ov-bv,d)} {du}</td><td>{sg(pct(bv,ov)) + ' %' if bv else '—'}</td></tr>")
    return ("<table><thead><tr><th>Métrica</th><th>Sin OTel<br>(baseline)</th><th>Con OTel<br>SDK</th>"
            "<th>Diferencia<br>absoluta</th><th>Overhead<br>(%)</th></tr></thead><tbody>"
            + "".join(tr) + "</tbody></table>")

def k6_excerpt():
    def box(log, title):
        txt = re.sub(r"\x1b\[[0-9;]*m", "", (REPO / "benchmark/output" / log).read_text())
        keep = [l.rstrip() for l in txt.splitlines()
                if re.match(r"\s+(✓|✗)?\s*(http_req_duration|http_req_failed|http_reqs|iterations)\.", l)]
        sus = [l for l in txt.splitlines() if l.startswith("sustained_load ✓")]
        return f"# {title}\n" + "\n".join(keep) + ("\n" + sus[-1] if sus else "")
    an = subprocess.run([sys.executable, "benchmark/analyze_overhead.py"], cwd=REPO,
                        capture_output=True, text=True).stdout
    an = an[an.index("Tabla para el reporte"):].rstrip()
    return (box("k6_baseline.log", "k6 run --env INSTRUMENTED=false  (OTEL_ENABLED=false)") + "\n\n"
            + box("k6_otel.log", "k6 run --env INSTRUMENTED=true   (OTEL_ENABLED=true)") + "\n\n"
            + "# python3 benchmark/analyze_overhead.py\n" + an)

def analysis():
    lat_d = o["latency_p99_ms"] - b["latency_p99_ms"]
    return f'''
<p>El análisis de los datos experimentales arroja los siguientes hallazgos:</p>
<ol>
<li><b>Ambos escenarios operan cerca del techo de CPU de service-a.</b> El servicio ejecuta dos workers de uvicorn (techo teórico de 200 % de CPU) y promedió {es(sa_b)} % sin instrumentación y {es(sa_o)} % con OTel. En ese régimen, cualquier costo de CPU adicional se traduce en menos solicitudes atendidas y en más tiempo de espera en cola, por lo que el overhead se manifiesta principalmente en el throughput ({sg(pct(b["throughput_rps"], o["throughput_rps"]))} %) y en los percentiles altos de latencia ({sg(lat_d, 0)} ms en p99, {sg(pct(b["latency_p99_ms"], o["latency_p99_ms"]))} %).</li>
<li><b>El costo intrínseco del SDK se expresa mejor como CPU por solicitud.</b> Dividiendo el CPU consumido por el throughput, cada solicitud pasa de {es(cpr_b)} ms a {es(cpr_o)} ms de CPU ({sg(cpr_o-cpr_b)} ms, {sg(pct(cpr_b, cpr_o), 0)} %). Cada llamada a <code>/order</code> genera 11 spans, tres a cuatro registros de log OTLP y varias mediciones de métricas entre los dos servicios. En Python, la creación de spans, la serialización de atributos y el <code>LoggingHandler</code> se ejecutan en el hilo de la solicitud; solo la exportación es asíncrona gracias a <code>BatchSpanProcessor</code> y <code>BatchLogRecordProcessor</code>.</li>
<li><b>El principal cuello de botella es la aplicación, no OTel.</b> Al perfilar service-a se midió que crear un <code>httpx.AsyncClient</code> nuevo en cada solicitud —por la inicialización del contexto SSL— cuesta 14,9 ms de CPU, aproximadamente dos tercios del costo total por solicitud en la línea base; abrir una conexión <code>psycopg2</code> por solicitud suma otros 2,4 ms. Reutilizar un único cliente HTTP y un pool de conexiones elevaría la capacidad del servicio y reduciría el peso relativo de la instrumentación.</li>
<li><b>Memoria.</b> El SDK agrega {es(mem_o-mem_b,0)} MiB entre ambos servicios ({sg(pct(mem_b, mem_o))} %), atribuibles a las colas de spans y logs pendientes de exportación y al estado de los agregadores de métricas. El Collector pasó de {es(col_b["mem_avg_mib"],0)} a {es(col_o["mem_avg_mib"],0)} MiB al procesar telemetría real, muy por debajo del límite de 400 MiB del <code>memory_limiter</code>.</li>
<li><b>SLO de latencia.</b> El umbral de k6 <code>p(95) &lt; 500 ms</code> se incumplió en ambos escenarios, lo que confirma que la limitación de capacidad existe con o sin instrumentación.</li>
<li><b>Veredicto.</b> Con 75 usuarios concurrentes sobre un servicio limitado por CPU, el overhead de OTel no es despreciable: la instrumentación consume alrededor del {es((cpr_o-cpr_b)/cpr_o*100,0)} % del CPU de cada solicitud. Para producción se recomienda (a) corregir el cuello de botella de la aplicación antes de dimensionar; (b) dimensionar la capacidad con la instrumentación activa, porque la línea base sobreestima la capacidad real; (c) aplicar muestreo en el SDK (<code>ParentBased(TraceIdRatioBased)</code>) o <i>tail sampling</i> en el Collector cuando el costo por solicitud sea crítico, y (d) revisar el volumen de logs OTLP por solicitud.</li>
</ol>
<p><b>Validez de la medición.</b> Una primera corrida con OTel se descartó porque el equipo anfitrión se suspendió durante aproximadamente 80 segundos en mitad de la carga sostenida, lo que distorsionaba el throughput y los percentiles (su registro se conserva como <code>benchmark/output/k6_otel_descartada_suspension.log</code>). La corrida reportada se ejecutó con la suspensión del sistema inhibida (<code>caffeinate</code>). Una corrida adicional independiente con OTel registró 64,2 req/s y un p99 de 1.039,5 ms, prácticamente idénticos a los reportados, lo que respalda la reproducibilidad de la medición. Dado que k6 se ejecuta en el mismo equipo que los contenedores, los valores absolutos no son extrapolables a un clúster dedicado; una medición concluyente requeriría repetir cada escenario al menos tres veces y reportar la mediana.</p>
'''

CORRECTIONS = [
    ("<code>FastAPIInstrumentor().instrument()</code> invocado después de importar <code>FastAPI</code>", "Trazas fragmentadas: sin span raíz y service-b en trazas separadas", "<code>FastAPIInstrumentor.instrument_app(app)</code> sobre la instancia"),
    ("Línea base obtenida deteniendo solo el Collector", "El SDK seguía activo; el overhead reportado no era real", "Variable <code>OTEL_ENABLED</code> en ambos servicios"),
    ("CPU y memoria sin fuente de datos", "Cifras no verificables", "<code>collect_stats.py</code> → <code>stats_*.json</code>"),
    ("<code>http_requests_total</code> incrementado 2–3 veces por solicitud", "SLI-1 de disponibilidad distorsionado", "Un único incremento por solicitud en <code>finally</code>"),
    ("<code>HTTPStatusError.status_code</code> (atributo inexistente)", "Un fallo de service-b terminaba en un 500 no controlado", "<code>e.response.status_code</code>"),
    ("Histogramas en segundos con cubetas por defecto (pensadas para ms)", "p99 fijo en ~4,95 s, sin relación con la latencia real", "Cubetas explícitas en segundos"),
    ("Logs solo en <code>stdout</code>, sin backend de logs en Grafana", "Correlación log ↔ traza imposible en Grafana Explore", "<code>LoggerProvider</code> OTLP + Loki + campo derivado <code>trace_id</code>"),
    ("Sin etiqueta <code>service_name</code> en Prometheus", "Variable <code>$service</code> del dashboard vacía", "<code>resource_to_telemetry_conversion: true</code>"),
    ("Dos workers de uvicorn con la misma identidad", "Contadores sobrescritos entre procesos", "<code>service.instance.id</code> por proceso"),
    ("Panel de CPU sobre <code>process_cpu_seconds_total</code>", "Graficaba el CPU de Jaeger, no el de los servicios", "<code>process.cpu.time</code> vía <code>SystemMetricsInstrumentor</code>"),
    ("Healthcheck con <code>curl</code>, ausente en la imagen <code>python:slim</code>", "Servicios reportados como <i>unhealthy</i>", "Healthcheck con <code>urllib</code> de Python"),
]

def corrections_table():
    return ("<table class='small wide3'><thead><tr><th>Defecto detectado</th><th>Impacto</th><th>Corrección</th></tr></thead><tbody>"
            + "".join(f"<tr><td>{a}</td><td>{b_}</td><td>{c}</td></tr>" for a, b_, c in CORRECTIONS)
            + "</tbody></table>")

CSS = """
@page { size: Letter; margin: 1in; @top-right { content: counter(page); font: 12pt 'Times New Roman'; } }
html { font-family: 'Times New Roman', Times, serif; font-size: 12pt; color: #000; }
body { margin: 0; line-height: 2; }
p { margin: 0; text-indent: 0.5in; text-align: left; }
p.noindent, .cover p { text-indent: 0; }
h1 { font-size: 12pt; text-align: center; font-weight: bold; margin: 0; line-height: 2; break-after: avoid; }
h2 { font-size: 12pt; text-align: left; font-weight: bold; margin: 0; line-height: 2; break-after: avoid; }
h3 { font-size: 12pt; font-weight: bold; font-style: italic; margin: 0; line-height: 2; break-after: avoid; }
ol, ul { margin: 0 0 0 0.5in; padding-left: 0.3in; }
li { margin: 0; }
code { font-family: 'Courier New', monospace; font-size: 10.5pt; }
pre { font-family: 'Courier New', monospace; font-size: 8.6pt; line-height: 1.25; background: #f4f4f4;
      border: 1px solid #ccc; padding: 8px 10px; margin: 6px 0 12px; white-space: pre-wrap; break-inside: avoid; }
.cover { height: 9in; display: flex; flex-direction: column; justify-content: center; text-align: center; break-after: page; }
.cover p { text-align: center; }
.cover .title { font-weight: bold; margin-bottom: 1em; }
.tbl { break-inside: avoid; }
.thead { break-after: avoid; }
table.small { break-inside: auto; }
table code { font-size: 8.6pt; overflow-wrap: anywhere; }
table.wide3 { table-layout: fixed; }
table.small tr { break-inside: avoid; }
table.wide3 td:nth-child(1) { width: 36%; } table.wide3 td:nth-child(2) { width: 28%; }
.runhead { text-align: center; font-weight: bold; }
figure { margin: 10px 0 14px; break-inside: avoid; text-align: center; }
figure img { max-width: 100%; max-height: 4.3in; border: 1px solid #999; }
figcaption, .tnote { font-size: 10pt; line-height: 1.3; text-align: left; margin-top: 4px; }
.tlabel { font-weight: bold; text-indent: 0; margin-top: 8px; }
.ttitle { font-style: italic; text-indent: 0; line-height: 1.5; }
table { border-collapse: collapse; width: 100%; font-size: 10.5pt; line-height: 1.3; margin: 4px 0 2px; break-inside: avoid; }
th { border-top: 1.5px solid #000; border-bottom: 1px solid #000; padding: 4px 6px; text-align: center; font-weight: bold; }
td { padding: 3px 6px; border-bottom: 1px solid #ddd; vertical-align: top; }
td:not(:first-child) { text-align: center; }
tbody tr:last-child td { border-bottom: 1.5px solid #000; }
table.small { font-size: 9.5pt; }
table.small td { text-align: left !important; }
.refs p { text-indent: -0.5in; padding-left: 0.5in; }
.pb { break-before: page; }
"""

def build():
    fig = iter([1, 2, 3, 4, 5, 7, 8])  # la Figura 6 (salida k6) se numera a mano
    F = lambda *a: img(a[0], next(fig), a[1])
    body = f"""
<div class="cover">
<p class="title">Implementación de pipeline de observabilidad end-to-end con OpenTelemetry y evaluación empírica de overhead en arquitecturas multi-cloud</p>
<p>Informe técnico de instrumentación SDK, Collector, correlación cross-signal y benchmark de rendimiento</p>
<p>&nbsp;</p>
<p>Carlos Alberto Zambrano Braydi<br>0000395349</p>
<p>Henry Julián Salazar Salcedo<br>0000396117</p>
<p>Wolfran Alirio Pinzón Murillo<br>0000393439</p>
<p>&nbsp;</p>
<p>Maestría en Arquitectura de Software, Universidad de La Sabana<br>
Observabilidad en ambientes productivos — Unidad 2 — Grupo 11<br>
Mg. María Fernanda Ochoa Paipilla<br>Octubre de 2026</p>
</div>

<p class="runhead noindent">Implementación de pipeline de observabilidad end-to-end con OpenTelemetry</p>

<h1>Introducción</h1>
<p>En los sistemas distribuidos basados en microservicios, el diagnóstico de incidentes se degrada con rapidez cuando las señales de telemetría residen en silos desconectados. Un fallo originado en una consulta lenta de base de datos suele propagarse aguas arriba manifestándose como un error 504 Gateway Timeout o una degradación en la experiencia del usuario, generando largas sesiones de depuración cuando el equipo carece de un hilo conductor que relacione los registros de log con el recorrido exacto de la petición (Sachto y Walcer, 2022).</p>
<p>El presente informe documenta la solución al Laboratorio 2 de la asignatura Observabilidad en Ambientes Productivos. El encargo consistió en diseñar e implementar un pipeline de observabilidad integral basado en el estándar abierto OpenTelemetry (OTel), que capture trazas distribuidas, métricas de rendimiento y logs estructurados desde dos microservicios dependientes (service-a y service-b) con persistencia en PostgreSQL, con despliegue objetivo en Google Cloud Platform (GKE) y Amazon Web Services (ECS Fargate). Asimismo, se exigió habilitar la correlación cross-signal mediante el identificador <code>trace_id</code> del estándar W3C TraceContext y medir de forma empírica la penalización computacional (overhead) inducida por la instrumentación frente a una línea base no instrumentada.</p>
<p>Esta versión del informe incorpora las correcciones realizadas tras una revisión técnica de la primera entrega: varios defectos de instrumentación producían métricas plausibles pero incorrectas, y el método de la línea base no desactivaba realmente el SDK. Las evidencias y los resultados de las secciones de visualización y de overhead se volvieron a tomar; el detalle de las correcciones se presenta al final de la Fase 4.</p>

<h2>Arquitectura objetivo y flujo transaccional</h2>
<p>La topología de la aplicación responde al patrón de orquestación de servicios:</p>
<ol>
<li><b>service-a (orquestador HTTP):</b> expone el endpoint público <code>GET /order/{{order_id}}</code> sobre FastAPI. Al recibir la solicitud inicia el contexto de traza distribuida, consulta la tabla <code>orders</code> en PostgreSQL mediante Psycopg2 y realiza una llamada HTTP asíncrona hacia service-b con HTTPX.</li>
<li><b>service-b (servicio de inventario):</b> expone el endpoint interno <code>GET /inventory/{{product_id}}</code>. Extrae el encabezado <code>traceparent</code> propagado por el orquestador, continúa el mismo <code>trace_id</code> y consulta la tabla <code>inventory</code>.</li>
<li><b>OpenTelemetry Collector Contrib (v0.103.0):</b> agente intermedio que recibe señales por OTLP (4317 gRPC y 4318 HTTP), ejecuta el pipeline de procesamiento y exporta hacia Jaeger y Tempo (trazas), Prometheus (métricas) y Loki o Google Cloud Logging (logs).</li>
</ol>

<h1>Fase 1 — Instrumentación con OTel SDK en Python</h1>
<h2>Identidad del servicio mediante el Resource</h2>
<p>El objeto <code>Resource</code> define el origen de la telemetría y viaja de forma inmutable en cada traza, métrica y línea de log. En ambos microservicios se configuró con el nombre del servicio, la versión, el entorno, el proveedor de nube y un identificador de instancia por proceso:</p>
{code('''resource = Resource.create({
    SERVICE_NAME:    "service-a",
    SERVICE_VERSION: APP_VERSION,
    "deployment.environment": ENV,
    "cloud.provider": os.getenv("CLOUD_PROVIDER", "gcp"),
    "host.name":      os.getenv("HOSTNAME", "local"),
    "service.instance.id": f"{os.getenv('HOSTNAME', 'local')}-{os.getpid()}",
})''')}
<p>El atributo <code>service.instance.id</code> es indispensable porque cada contenedor ejecuta <code>uvicorn --workers 2</code>: sin una identidad por proceso, ambos workers exportan series con las mismas etiquetas y sus contadores acumulados se sobrescriben en el Collector, falseando los SLIs.</p>
<p>Toda la inicialización del SDK está condicionada por la variable <code>OTEL_ENABLED</code> (por defecto <code>true</code>). Con <code>OTEL_ENABLED=false</code> no se registra ningún provider ni instrumentor y la API de OpenTelemetry opera en modo <i>no-op</i>, de modo que el código de negocio permanece idéntico. Este interruptor es el que permite medir el overhead real en la Fase 4.</p>

<h2>TracerProvider y selección de procesadores de spans</h2>
<p>Una decisión de diseño crítica es la elección del procesador de spans. <code>SimpleSpanProcessor</code> realiza una exportación bloqueante por cada span finalizado; en un endpoint que genera spans para la ruta HTTP, las consultas SQL y la llamada saliente, añadiría decenas de milisegundos por solicitud. Por ello se implementó <code>BatchSpanProcessor</code> con un <code>OTLPSpanExporter</code> sobre gRPC: retiene los spans en un búfer en memoria y los despacha en lotes de hasta 512 elementos cada 5 segundos, aislando el hilo de atención de solicitudes del tráfico de telemetría.</p>

<h2>Auto-instrumentación y spans personalizados</h2>
<p>Se combinaron dos niveles de instrumentación:</p>
<ol>
<li><b>Auto-instrumentación:</b> <code>FastAPIInstrumentor.instrument_app(app)</code> genera el span raíz (SERVER) de cada solicitud y extrae el encabezado <code>traceparent</code>; <code>HTTPXClientInstrumentor</code> inyecta el encabezado W3C en las llamadas a service-b; <code>Psycopg2Instrumentor</code> registra cada sentencia SQL con su <code>db.statement</code>, y <code>SystemMetricsInstrumentor</code> emite el CPU y la memoria de cada proceso. Se instrumenta la <i>instancia</i> de la aplicación y no la clase: <code>FastAPIInstrumentor().instrument()</code> reemplaza la clase dentro del módulo <code>fastapi</code>, pero como <code>main.py</code> importa <code>FastAPI</code> antes de esa llamada, la aplicación se construía con la clase original y quedaba sin instrumentar.</li>
<li><b>Spans de lógica de negocio:</b> la auto-instrumentación captura la infraestructura pero omite el contexto de dominio, por lo que se añadieron <code>fetch.order.db</code> (atributos <code>order.id</code> y <code>order.status</code>) y <code>call.service-b.inventory</code> en el orquestador, e <code>inventory.db.fetch</code> en el servicio de inventario.</li>
</ol>

<h2>MeterProvider y los cuatro indicadores de nivel de servicio (SLIs)</h2>
<p>Siguiendo los Golden Signals de Google SRE (Ewaschuk, 2016), se configuró un <code>MeterProvider</code> con un <code>PeriodicExportingMetricReader</code> que envía las mediciones al Collector cada 15 segundos:</p>
<ol>
<li><b>Disponibilidad (SLI-1):</b> contador <code>http_requests_total</code> con la etiqueta <code>status</code> (<code>2xx</code>, <code>4xx</code>, <code>5xx</code>). Se incrementa una sola vez por solicitud, en el bloque <code>finally</code>, con la clase de estado real de la respuesta.</li>
<li><b>Latencia (SLI-2):</b> histograma <code>http_request_duration_seconds</code> para estimar p50, p95 y p99. Se declararon cubetas explícitas en segundos (de 5 ms a 10 s) mediante <code>explicit_bucket_boundaries_advisory</code>: las cubetas por defecto del SDK (0, 5, 10, 25…) están pensadas para milisegundos, y con valores en segundos toda la distribución caía en la primera cubeta, produciendo un p99 artificial de ~4,95 s.</li>
<li><b>Tasa de error (SLI-3):</b> proporción de respuestas 5xx sobre el total.</li>
<li><b>Saturación (SLI-4):</b> <code>UpDownCounter</code> <code>http_active_requests</code>, que rastrea las solicitudes en curso.</li>
</ol>

<h2>Logging estructurado y el trace_id como pivote de correlación</h2>
<p>Los logs se emiten por dos vías. La primera es la clase <code>OtelJsonFormatter</code>, basada en <code>pythonjsonlogger</code>, que consulta el span activo y escribe en la salida estándar un JSON con el <code>trace_id</code> (32 dígitos hexadecimales) y el <code>span_id</code> (16 dígitos). La segunda es un <code>LoggerProvider</code> con <code>OTLPLogExporter</code>: el <code>LoggingHandler</code> adjunta de forma nativa el contexto del span a cada registro y lo envía al Collector por el mismo canal que las trazas y las métricas.</p>
{code('''logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTEL_ENDPOINT, insecure=True)))
set_logger_provider(logger_provider)
handlers.append(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))''')}
{F("04-logs-trace_ids.jpg", "Salida de registros estructurados en formato JSON con inclusión de <code>trace_id</code> y <code>span_id</code> en service-a.")}

<h1>Fase 2 — Despliegue y configuración del OTel Collector</h1>
<p>El OpenTelemetry Collector actúa como concentrador y enrutador agnóstico de telemetría. La configuración <code>collector-config.yaml</code> implementa tres pipelines independientes para trazas, métricas y logs.</p>
<h2>Orden e importancia de los procesadores</h2>
<ol>
<li><b>memory_limiter (primer procesador):</b> límite de 400 MiB, margen de pico de 100 MiB y verificación cada 0,5 s. Ante una tormenta de telemetría descarta datos de forma controlada antes de que el OOM Killer elimine el proceso: un agente caído suspende toda la visibilidad de la plataforma.</li>
<li><b>resource y resourcedetection:</b> insertan atributos de entorno (<code>deployment.environment</code>, <code>cloud.region</code>) y detectan metadatos de GKE o ECS.</li>
<li><b>filter/health:</b> excluye las trazas de <code>/health</code>; los sondeos de liveness y readiness generan cientos de trazas por minuto sin valor diagnóstico.</li>
<li><b>batch (último procesador):</b> agrupa hasta 1024 registros o espera 5 segundos antes de exportar, reduciendo el costo de conexiones de red.</li>
</ol>
<h2>Exporters configurados</h2>
<ul>
<li><b>Trazas:</b> Jaeger y Tempo vía OTLP gRPC.</li>
<li><b>Métricas:</b> endpoint de scraping en el puerto 8889 para Prometheus, con <code>resource_to_telemetry_conversion: true</code> para convertir <code>service.name</code>, <code>deployment.environment</code> y <code>cloud.provider</code> en etiquetas. Las métricas internas del Collector (puerto 8888) las recolecta Prometheus directamente.</li>
<li><b>Logs:</b> Loki 3 mediante <code>otlphttp/loki</code>, que almacena <code>trace_id</code> y <code>span_id</code> como metadatos estructurados, y Google Cloud Logging mediante <code>googlecloud</code> en el despliegue GKE.</li>
</ul>
<p>Para el entorno local se combina un segundo archivo, <code>collector-config.local.yaml</code>, que retira <code>googlecloud</code> del pipeline de logs: ese exporter exige credenciales de GCP y, sin ellas, impide que el Collector inicie. Al combinar configuraciones, el Collector reemplaza las listas completas, y un exporter definido pero sin pipeline no se inicia. Así, cambiar de backend es una decisión de configuración y no de código.</p>

<h1>Fase 3 — Backends, visualización y correlación cross-signal</h1>
<h2>Propagación de contexto W3C en Jaeger UI</h2>
<p>Al consultar el endpoint <code>/order/{{order_id}}</code>, Jaeger muestra que el contexto viaja de extremo a extremo bajo un único identificador. Como se aprecia en la Figura 2, cada traza agrupa 11 spans —siete de service-a y cuatro de service-b—: el span raíz HTTP, la consulta a <code>orders</code>, la llamada HTTPX, la recepción en service-b y la consulta a <code>inventory</code>. La presencia de spans de ambos servicios en una misma traza demuestra que la inyección y extracción del encabezado <code>traceparent</code> funciona.</p>
{F("01-jaeger-distributed-trace.png", "Búsqueda en Jaeger UI: cada traza de <code>GET /order/{order_id}</code> agrupa 11 spans de service-a (7) y service-b (4) bajo un único <code>trace_id</code>.")}
<h2>Métricas y scraping en Prometheus</h2>
<p>Prometheus raspa periódicamente el puerto 8889 del Collector, que expone las métricas exportadas por los SDK con el prefijo <code>otelcol_</code>, y el puerto 8888 con las métricas internas del propio Collector.</p>
{F("02-Prometheus.jpg", "Estado de los targets de scraping y evaluación de métricas OTel en Prometheus UI.")}
<h2>Dashboard de Grafana y paneles SLI</h2>
<p>El dashboard <code>otel-lab-main</code> contiene los paneles solicitados: disponibilidad (SLI-1, meta ≥ 99,5 %), latencias p50/p95/p99 (SLI-2), tasa de errores 5xx (SLI-3), throughput (SLI-4), CPU de cada servicio frente al Collector y salud del Collector (spans enviados, fallidos, en cola y descartados). Se añadieron además un panel de Burn Rate sobre una ventana de una hora y una tabla de llamadas entre servicios. Las consultas de tasa de error y Burn Rate devuelven 0 cuando no hay respuestas 5xx, para distinguir la ausencia de errores de la ausencia de datos. La Figura 4 se tomó durante una corrida de carga con 75 usuarios concurrentes.</p>
{F("03-grafana-slis-dashboard.png", "Dashboard en Grafana durante una prueba de carga: cuatro SLIs, CPU por servicio, salud del Collector, Burn Rate y propagación entre servicios.")}
<h2>Correlación cross-signal en Grafana Explore</h2>
<p>Los logs OTLP llegan a Loki con el <code>trace_id</code> y el <code>span_id</code> como metadatos estructurados, junto con los atributos del <code>Resource</code>. La consulta <code>{{service_name="service-a"}} | trace_id="&lt;id&gt;"</code> devuelve los logs de una solicitud concreta. El datasource de Loki define un campo derivado sobre <code>trace_id</code> que convierte cada valor en un enlace a la traza en Jaeger (log → traza); a la inversa, el datasource de Jaeger configura <code>tracesToLogsV2</code> para abrir desde un span los logs con el mismo identificador (traza → log). El operador puede así pasar de un pico en el panel de latencia a la traza y, de ahí, a la línea de log que describe la causa.</p>
{F("05-cross-signal-correlation.png", "Registro de service-a en Grafana Explore (Loki): el log incluye <code>trace_id</code>, <code>span_id</code> y los atributos del <code>Resource</code>; el campo <code>trace_id</code> enlaza con la traza en Jaeger.")}

<h1>Fase 4 — Análisis empírico de overhead con k6</h1>
<h2>Diseño experimental y perfil de carga</h2>
<p>Se ejecutaron pruebas de carga con k6 (v2.2.0) sobre la misma infraestructura y el mismo código, variando únicamente la variable <code>OTEL_ENABLED</code> de los servicios:</p>
<ol>
<li><b>Línea base (sin OTel, <code>OTEL_ENABLED=false</code>):</b> no se registran providers ni instrumentors. Se conserva el logging JSON a <code>stdout</code>, presente en ambos escenarios.</li>
<li><b>Con OTel SDK (<code>OTEL_ENABLED=true</code>):</b> trazas, métricas y logs exportados por OTLP al Collector.</li>
</ol>
<p>En la primera entrega, la línea base se obtenía deteniendo el Collector. Ese método no es válido: el SDK sigue creando spans y reintentando su exportación, por lo que no mide el costo de la instrumentación, y explicaba que la corrida instrumentada pareciera más rápida que la línea base. Esos resultados se descartaron.</p>
<p>El perfil de carga sigue la recomendación del laboratorio: un calentamiento de 1 a 10 usuarios virtuales durante 60 segundos, seguido de <b>75 usuarios virtuales constantes durante 5 minutos</b>, con una pausa aleatoria de 100 a 600 ms entre solicitudes. El escenario de pico de 200 usuarios queda como prueba de estrés opcional y no forma parte de la comparación, porque satura la base de datos y su efecto domina el p99. Durante los cinco minutos de carga sostenida, <code>collect_stats.py</code> muestrea <code>docker stats</code> de service-a, service-b y el Collector; <code>analyze_overhead.py</code> consolida esos datos con los resultados de k6. El entorno fue Docker Compose (Docker 27.4, máquina virtual de 8 vCPU y 7,6 GiB) sobre un equipo Apple M1 Pro, con k6 ejecutándose en el mismo equipo.</p>

<div class="tbl"><p class="tlabel">Tabla 1</p>
<p class="ttitle">Comparación de métricas de rendimiento y recursos entre la línea base y la aplicación con OTel SDK</p>
{table_overhead()}
<p class="tnote"><i>Nota.</i> Valores de <code>results_baseline.json</code> y <code>results_otel.json</code> (k6) y de <code>stats_baseline.json</code> y <code>stats_otel.json</code> (<code>collect_stats.py</code>). Se procesaron {es(nb_req,0)} solicitudes en la línea base y {es(no_req,0)} con OTel, sin errores. <i>CPU por solicitud</i> = CPU medio de ambos servicios dividido por el throughput; aísla el costo del SDK de la saturación. pp = puntos porcentuales.</p></div>

<h2>Interpretación de los resultados</h2>
{analysis()}

<div class="tbl"><p class="noindent" style="margin-top:8px"><b>Figura 6.</b> <span style="font-size:10pt">Extracto de la salida de consola de k6 para ambos escenarios y tabla generada por <code>analyze_overhead.py</code>. Salidas completas en <code>benchmark/output/</code>.</span></p>
<pre>{html.escape(k6_excerpt())}</pre></div>

<h2>Correcciones posteriores a la primera entrega</h2>
<p>La revisión técnica de la primera versión identificó los siguientes defectos, todos corregidos antes de volver a tomar las evidencias:</p>
<div class="thead"><p class="tlabel">Tabla 2</p>
<p class="ttitle">Defectos de instrumentación detectados, impacto y corrección aplicada</p></div>
{corrections_table()}
<p>Tres de estos defectos —las cubetas del histograma, el contador duplicado y la instrumentación inactiva de FastAPI— producían valores verosímiles pero incorrectos. La lección es que la propia telemetría debe validarse contra una fuente independiente antes de definir SLOs sobre ella.</p>

<h1>Despliegue multi-cloud e Infraestructura como Código (IaC)</h1>
<h2>Estrategia en Google Cloud Platform (GKE)</h2>
<p>Los manifiestos de Terraform (<code>terraform/gcp/</code>) aprovisionan una red VPC, un clúster GKE con Workload Identity y un pool de nodos. El OTel Collector se diseñó con el patrón DaemonSet (un pod por nodo): un único proceso atiende la telemetría de todos los pods del nodo, reduciendo el consumo de memoria frente al modelo sidecar. La autenticación con Cloud Trace y Cloud Logging se resuelve vinculando el ServiceAccount de Kubernetes con una cuenta de servicio de GCP con los roles <code>roles/cloudtrace.agent</code> y <code>roles/logging.logWriter</code>.</p>
<p>Las Figuras 7 y 8 muestran el clúster <code>otel-lab</code> con tres nodos operativos y los pods de service-a, service-b y Jaeger en ejecución. Sin embargo, en esas capturas el DaemonSet del Collector no tenía pods listos (0/3) y no aparece el pod de PostgreSQL, por lo que la telemetría de extremo a extremo se validó en el entorno local (Fases 1 a 4) y no en GKE. Además, el despliegue en GKE es anterior a las correcciones de instrumentación y debe repetirse con las imágenes actualizadas.</p>
{F("12-gke-pods-services.jpg", "Pods y servicios en el namespace <code>otel-lab</code> de GKE: service-a, service-b y Jaeger en ejecución; no figuran pods del Collector ni de PostgreSQL.")}
{F("11-cluster-GCP.jpg", "Clúster GKE <code>otel-lab</code> en la consola de Google Cloud Platform con su grupo de nodos de tres instancias.")}
<h2>Estrategia en Amazon Web Services (ECS Fargate)</h2>
<p>En AWS, Terraform (<code>terraform/aws/</code>) crea una VPC multi-AZ, un clúster ECS, un Log Group de CloudWatch y los roles IAM con las políticas <code>AWSXRayDaemonWriteAccess</code> y <code>CloudWatchLogsFullAccess</code>. Al ser Fargate un modelo serverless sin acceso a los nodos, el patrón DaemonSet no es aplicable: la <i>task definition</i> ejecuta el Collector como sidecar, comunicado con los servicios por <code>localhost:4317</code>.</p>
<p><b>Limitación declarada.</b> El despliegue en AWS no se ejecutó, por lo que no se presentan evidencias de X-Ray ni de CloudWatch. Para completarlo falta agregar <code>aws_ecs_task_definition</code> y <code>aws_ecs_service</code> al módulo de Terraform, y una configuración del Collector específica para AWS con los exporters <code>awsxray</code> y <code>awscloudwatchlogs</code>, ya que la configuración actual exporta a Jaeger y Cloud Logging.</p>

<div class="tbl"><p class="tlabel">Tabla 3</p>
<p class="ttitle">Comparativa arquitectónica de despliegue entre GCP GKE y AWS ECS Fargate</p>
<table class="small"><thead><tr><th>Criterio</th><th>Google Cloud Platform (GKE)</th><th>Amazon Web Services (ECS Fargate)</th></tr></thead><tbody>
<tr><td>Patrón del Collector</td><td>DaemonSet (1 pod por nodo)</td><td>Sidecar (1 contenedor por tarea)</td></tr>
<tr><td>Mecanismo de autorización</td><td>Workload Identity (K8s SA → GCP SA)</td><td>IAM Roles for Tasks (Task Role)</td></tr>
<tr><td>Automatización IaC</td><td>Terraform + Helm Charts</td><td>Terraform + Task Definition JSON</td></tr>
<tr><td>Backend nativo de trazas</td><td>Google Cloud Trace</td><td>AWS X-Ray</td></tr>
<tr><td>Backend nativo de logs</td><td>Google Cloud Logging</td><td>Amazon CloudWatch Logs</td></tr>
<tr><td>Impacto en memoria y costos</td><td>Menor huella (agente compartido)</td><td>Aislamiento por tarea (mayor costo)</td></tr>
<tr><td>Estado en este laboratorio</td><td>Clúster y servicios desplegados; Collector sin pods listos</td><td>Definido en IaC; sin desplegar</td></tr>
</tbody></table>
<p class="tnote"><i>Nota.</i> Comparación de trade-offs operacionales entre el despliegue en nodos Kubernetes compartidos y el despliegue serverless en contenedores Fargate.</p></div>

<h1 class="pb">Conclusiones</h1>
<ol>
<li><b>Interoperabilidad sin bloqueo con proveedores.</b> OpenTelemetry desacopla la lógica de los microservicios de las plataformas de visualización: el mismo código alimenta Jaeger, Tempo, Prometheus, Loki y Cloud Logging, y el cambio de backend se resuelve en la configuración del Collector, como demuestra el archivo de override local que retira Cloud Logging sin tocar el código.</li>
<li><b>Correlación cross-signal.</b> La propagación W3C <code>traceparent</code> y el registro del <code>trace_id</code> en logs OTLP permiten pasar del dashboard a la traza y de la traza al log exacto en Grafana Explore, eliminando la correlación manual de marcas temporales.</li>
<li><b>El overhead depende del margen de capacidad.</b> La instrumentación añadió {es(cpr_o-cpr_b)} ms de CPU por solicitud ({sg(pct(cpr_b, cpr_o),0)} %). En un servicio cerca de su techo de CPU, ese costo se traduce en {sg(pct(b["throughput_rps"], o["throughput_rps"]))} % de throughput y {sg(pct(b["latency_p99_ms"], o["latency_p99_ms"]))} % de p99. La conclusión de la primera entrega —un overhead de +0,54 %— provenía de una línea base que no desactivaba el SDK. La capacidad debe dimensionarse con la instrumentación activa.</li>
<li><b>La telemetría también debe validarse.</b> Defectos como cubetas de histograma inadecuadas, contadores duplicados o una instrumentación que no se activa producen métricas verosímiles pero falsas, más peligrosas que la ausencia de métricas porque generan confianza injustificada.</li>
<li><b>Resiliencia operativa del Collector.</b> Situar <code>memory_limiter</code> como primer procesador y filtrar los health checks antes del empaquetado en lotes protege la disponibilidad del agente de telemetría ante picos de carga.</li>
</ol>

<h1 class="pb">Referencias</h1>
<div class="refs">
<p>Amazon Web Services. (2024). <i>AWS Distro for OpenTelemetry on Amazon ECS</i>. https://docs.aws.amazon.com/ecs/latest/developerguide/opentelemetry.html</p>
<p>Beyer, B., Jones, C., Petoff, J. y Murphy, N. R. (Eds.). (2018). <i>The site reliability workbook: Practical ways to implement SRE</i>. O'Reilly Media.</p>
<p>Cloud Native Computing Foundation. (2024). <i>OpenTelemetry specification</i>. https://opentelemetry.io/docs/specs/otel/</p>
<p>Ewaschuk, R. (2016). Monitoring distributed systems. En B. Beyer, C. Jones, J. Petoff y N. R. Murphy (Eds.), <i>Site reliability engineering: How Google runs production systems</i> (pp. 57–67). O'Reilly Media.</p>
<p>Google Cloud. (2024). <i>Observability architecture: Designing telemetry pipelines with OpenTelemetry Collector</i>. https://cloud.google.com/architecture/observability-opentelemetry</p>
<p>Grafana Labs. (2024). <i>Trace integration in Explore</i>. https://grafana.com/docs/grafana/latest/explore/trace-integration/</p>
<p>Grafana Labs. (2024). <i>Ingesting logs to Loki using OpenTelemetry Collector</i>. https://grafana.com/docs/loki/latest/send-data/otel/</p>
<p>Grafana Labs. (2024). <i>k6 documentation</i>. https://grafana.com/docs/k6/latest/</p>
<p>Ochoa Paipilla, M. F. (2026, 8 de octubre). <i>Chaos engineering, métricas SRE y baseline dinámico de alertas</i> [Sesión de clase]. Maestría en Arquitectura de Software, Universidad de La Sabana.</p>
<p>OpenTelemetry Authors. (2024). <i>OpenTelemetry Python documentation</i>. https://opentelemetry-python.readthedocs.io/</p>
<p>Sachto, A. y Walcer, A. (2022). <i>Anatomy of an incident</i>. Google Site Reliability Engineering. https://sre.google/resources/practices-and-processes/anatomy-of-an-incident/</p>
<p>World Wide Web Consortium. (2021). <i>Trace context</i> (W3C Recommendation). https://www.w3.org/TR/trace-context/</p>
</div>
"""
    OUT_HTML.write_text(f"<!doctype html><html lang='es'><head><meta charset='utf-8'><title>Reporte técnico oTelLab</title><style>{CSS}</style></head><body>{body}</body></html>")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    "--allow-file-access-from-files", f"--print-to-pdf={OUT_PDF}", f"file://{OUT_HTML}"],
                   check=True, capture_output=True)
    print("PDF:", OUT_PDF)

if __name__ == "__main__":
    build()
