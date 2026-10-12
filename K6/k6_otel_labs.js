/**
 * k6_otel_lab.js — Prueba de carga para el laboratorio OTel
 *
 * INSTALACIÓN k6 en Windows:
 *   winget install k6 --source winget
 *   O descargar desde: https://github.com/grafana/k6/releases
 *
 * EJECUCIÓN:
 *
 *   Prueba rápida (1 min, pocos usuarios):
 *   k6 run k6_otel_lab.js
 *
 *   Prueba completa con reporte:
 *   k6 run --out json=resultados.json k6_otel_lab.js
 *
 *   Solo tráfico normal (sin errores):
 *   k6 run --env MODO=normal k6_otel_lab.js
 *
 *   Inyectar errores (ver burn rate subir en Grafana):
 *   k6 run --env MODO=errores k6_otel_lab.js
 *
 *   Spike de carga (thundering herd):
 *   k6 run --env MODO=spike k6_otel_lab.js
 *
 *   URL personalizada:
 *   k6 run --env BASE_URL=http://127.0.0.1:8000 k6_otel_lab.js
 */

import http from "k6/http";
import { check, sleep, group } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

// ── Configuración ─────────────────────────────────────────────────
const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:8000";
const MODO     = __ENV.MODO     || "normal";

// ── Métricas custom ───────────────────────────────────────────────
const errores          = new Counter("errores_negocio");
const tasa_exito       = new Rate("tasa_exito");
const latencia_orden   = new Trend("latencia_orden_ms",   true);
const latencia_reserva = new Trend("latencia_reserva_ms", true);

// ── Datos de prueba ───────────────────────────────────────────────
const ORDENES_VALIDAS   = ["ord-001","ord-002","ord-003","ord-004","ord-005"];
const ORDENES_INVALIDAS = ["ord-999","ord-000","ord-xxx","ord-abc"];
const PRODUCTOS         = ["LAPTOP-X1","MOUSE-PRO","KEYBOARD-MK","MONITOR-4K","HEADSET-Z"];

// ── Escenarios de carga según MODO ────────────────────────────────
export const options = {

  scenarios: MODO === "spike" ? {
    // ── Spike — thundering herd ───────────────────────────────────
    spike: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: "15s", target: 1   },  // calentamiento
        { duration: "10s", target: 200 },  // spike súbito
        { duration: "30s", target: 200 },  // sostener el spike
        { duration: "10s", target: 1   },  // recuperación
        { duration: "15s", target: 1   },  // observar recuperación
      ],
      gracefulStop: "10s",
    },
  } : MODO === "errores" ? {
    // ── Modo errores — ver burn rate subir en Grafana ─────────────
    trafico_normal: {
      executor: "constant-vus",
      vus: 10,
      duration: "2m",
      tags: { tipo: "normal" },
    },
    trafico_errores: {
      executor: "constant-vus",
      vus: 5,
      duration: "2m",
      tags: { tipo: "error" },
      exec: "generarErrores",
    },
  } : {
    // ── Modo normal — tráfico realista ────────────────────────────
    warmup: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: "20s", target: 5  },
        { duration: "20s", target: 5  },
      ],
      gracefulRampDown: "5s",
      tags: { fase: "warmup" },
    },
    carga_sostenida: {
      executor: "constant-vus",
      vus: 20,
      duration: "1m30s",
      startTime: "40s",
      tags: { fase: "sostenida" },
    },
    pico_moderado: {
      executor: "ramping-vus",
      startVUs: 20,
      stages: [
        { duration: "15s", target: 50 },
        { duration: "20s", target: 50 },
        { duration: "15s", target: 20 },
      ],
      startTime: "2m20s",
      tags: { fase: "pico" },
    },
  },

  // ── SLOs del laboratorio como thresholds ─────────────────────────
  thresholds: {
    // SLO-1: Latencia p95 < 500ms
    "http_req_duration{endpoint:orden}":   ["p(95)<500"],
    // SLO-2: Error rate < 0.5%
    "http_req_failed":                     ["rate<0.005"],
    // SLO-3: Tasa de éxito > 99.5%
    "tasa_exito":                          ["rate>0.995"],
    // Latencia interna
    "latencia_orden_ms":                   ["p(99)<1000"],
  },

  summaryTrendStats: ["avg","min","med","max","p(90)","p(95)","p(99)","p(99.9)"],
};

// ── Setup ─────────────────────────────────────────────────────────
export function setup() {
  console.log(`
╔══════════════════════════════════════════════════════════╗
║           Laboratorio OTel — Prueba de carga k6          ║
║  URL:  ${BASE_URL.padEnd(48)}║
║  Modo: ${MODO.padEnd(48)}║
╚══════════════════════════════════════════════════════════╝
  `);

  // Verificar que el servicio está vivo
  const health = http.get(`${BASE_URL}/health`);
  if (health.status !== 200) {
    throw new Error(`service-a no responde en ${BASE_URL}/health — status: ${health.status}`);
  }
  console.log("✅ service-a saludable — iniciando prueba");
  return { start: Date.now() };
}

// ── VU Function principal — tráfico normal ────────────────────────
export default function(data) {
  const orden = ORDENES_VALIDAS[Math.floor(Math.random() * ORDENES_VALIDAS.length)];

  group("consultar_orden", () => {
    const inicio = Date.now();

    const res = http.get(
      `${BASE_URL}/order/${orden}`,
      {
        tags: { endpoint: "orden", orden_id: orden },
        timeout: "10s",
      }
    );

    latencia_orden.add(Date.now() - inicio);

    const ok = check(res, {
      "status 200":         r => r.status === 200,
      "tiene order":        r => {
        try { return JSON.parse(r.body).order !== undefined; }
        catch { return false; }
      },
      "tiene inventory":    r => {
        try { return JSON.parse(r.body).inventory !== undefined; }
        catch { return false; }
      },
      "tiene trace_id":     r => {
        try { return JSON.parse(r.body).trace_id !== undefined; }
        catch { return false; }
      },
      "latencia < 500ms":   () => (Date.now() - inicio) < 500,
    });

    tasa_exito.add(ok);
    if (!ok) errores.add(1);

    // Mostrar trace_id cada 10 requests para verificar correlación
    if (Math.random() < 0.1 && res.status === 200) {
      try {
        const body = JSON.parse(res.body);
        console.log(`trace_id: ${body.trace_id} — orden: ${orden} — ${Date.now() - inicio}ms`);
      } catch {}
    }
  });

  // Ocasionalmente hacer una reserva de inventario
  if (Math.random() < 0.2) {
    group("reservar_inventario", () => {
      const producto = PRODUCTOS[Math.floor(Math.random() * PRODUCTOS.length)];
      const inicio   = Date.now();

      const res = http.post(
        `http://localhost:8001/inventory/${producto}/reserve?quantity=1`,
        null,
        { tags: { endpoint: "reserva" }, timeout: "10s" }
      );

      latencia_reserva.add(Date.now() - inicio);

      check(res, {
        "reserva ok o sin stock": r => [200, 409].includes(r.status),
      });
    });
  }

  sleep(Math.random() * 0.8 + 0.2); // 200-1000ms entre requests
}

// ── VU Function errores — solo para MODO=errores ──────────────────
export function generarErrores() {
  const orden = ORDENES_INVALIDAS[Math.floor(Math.random() * ORDENES_INVALIDAS.length)];

  const res = http.get(
    `${BASE_URL}/order/${orden}`,
    { tags: { endpoint: "orden_invalida", tipo: "error" }, timeout: "5s" }
  );

  check(res, {
    "retorna 404": r => r.status === 404,
  });

  errores.add(1);
  sleep(0.3);
}

// ── Teardown — resumen final ───────────────────────────────────────
export function handleSummary(data) {
  const dur    = data.state.testRunDurationMs / 1000;
  const total  = data.metrics.http_reqs?.values?.count      || 0;
  const p95    = data.metrics.http_req_duration?.values?.["p(95)"] || 0;
  const p99    = data.metrics.http_req_duration?.values?.["p(99)"] || 0;
  const avg    = data.metrics.http_req_duration?.values?.avg || 0;
  const errPct = (data.metrics.http_req_failed?.values?.rate || 0) * 100;
  const rps    = data.metrics.http_reqs?.values?.rate || 0;
  const exito  = (data.metrics.tasa_exito?.values?.rate || 0) * 100;

  // Calcular burn rate
  const burnRate = errPct > 0 ? (errPct / 100 / 0.005).toFixed(2) : "0";
  const brColor  = parseFloat(burnRate) > 14.4 ? "🔴" :
                   parseFloat(burnRate) > 6     ? "🟡" : "🟢";

  console.log(`
╔══════════════════════════════════════════════════════════════╗
║                 RESULTADOS — OTel Lab k6                     ║
╠══════════════════════════════════════════════════════════════╣
║  Duración total:      ${String(dur.toFixed(0)+"s").padEnd(38)}║
║  Total requests:      ${String(total).padEnd(38)}║
║  Throughput (RPS):    ${String(rps.toFixed(2)).padEnd(38)}║
╠══════════════════════════════════════════════════════════════╣
║  LATENCIA                                                    ║
║  Promedio:            ${String(avg.toFixed(2)+" ms").padEnd(38)}║
║  p95:                 ${String(p95.toFixed(2)+" ms").padEnd(38)}║
║  p99:                 ${String(p99.toFixed(2)+" ms").padEnd(38)}║
╠══════════════════════════════════════════════════════════════╣
║  SLOs                                                        ║
║  Tasa de éxito:       ${String(exito.toFixed(3)+"%").padEnd(38)}║
║  Error rate:          ${String(errPct.toFixed(3)+"%").padEnd(38)}║
║  Burn Rate (1h SLO):  ${String(brColor+" "+burnRate+"x").padEnd(38)}║
╠══════════════════════════════════════════════════════════════╣
║  VEREDICTO SLOs                                              ║
║  p95 < 500ms:  ${p95 < 500 ? "✅ CUMPLE" : "❌ VIOLA "}  (${p95.toFixed(0)}ms)${" ".repeat(20)}║
║  Error < 0.5%: ${errPct < 0.5 ? "✅ CUMPLE" : "❌ VIOLA "}  (${errPct.toFixed(3)}%)${" ".repeat(20)}║
╚══════════════════════════════════════════════════════════════╝

Ver trazas en Jaeger:    http://localhost:16686
Ver métricas Grafana:    http://localhost:3000
Ver burn rate Prometheus: rate(http_requests_total{status=~"5.."}[1m])
  `);

  return {
    stdout: `\nResultados guardados. Ver Jaeger en http://localhost:16686\n`,
    "resultados_k6.json": JSON.stringify({
      modo:       MODO,
      timestamp:  new Date().toISOString(),
      duracion_s: dur,
      metricas: {
        total_requests: total,
        rps:            rps,
        latencia_avg:   avg,
        latencia_p95:   p95,
        latencia_p99:   p99,
        error_rate_pct: errPct,
        tasa_exito_pct: exito,
        burn_rate:      parseFloat(burnRate),
      },
      slos: {
        latencia_p95_ok: p95 < 500,
        error_rate_ok:   errPct < 0.5,
        burn_rate_ok:    parseFloat(burnRate) < 14.4,
      }
    }, null, 2),
  };
}