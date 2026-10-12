/**
 * benchmark/k6_benchmark.js — Fase 4: Análisis de overhead OTel
 *
 * Ejecutar sin instrumentación (baseline):
 *   k6 run --env INSTRUMENTED=false --out csv=results_baseline.csv k6_benchmark.js
 *
 * Ejecutar con instrumentación OTel:
 *   k6 run --env INSTRUMENTED=true --out csv=results_otel.csv k6_benchmark.js
 *
 * Comparar resultados:
 *   python3 analyze_overhead.py
 *
 * Instalar k6: https://grafana.com/docs/k6/latest/set-up/install-k6/
 */

import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Histogram, Rate, Trend } from "k6/metrics";
import { textSummary } from "https://jslib.k6.io/k6-summary/0.0.2/index.js";

// ── Configuración ────────────────────────────────────────────────────────────
const BASE_URL    = __ENV.BASE_URL    || "http://localhost:8000";
const INSTRUMENTED = __ENV.INSTRUMENTED !== "false";  // default: true

const ORDER_IDS = ["ord-001", "ord-002", "ord-003", "ord-004", "ord-005"];

// ── Métricas personalizadas k6 ────────────────────────────────────────────────
const p99Latency      = new Trend("p99_latency_ms",  true);
const errorCount      = new Counter("request_errors");
const successRate     = new Rate("success_rate");
const dbLatency       = new Trend("db_latency_ms",   true);

// ── Escenarios de carga ───────────────────────────────────────────────────────
export const options = {
  scenarios: {
    // Escenario 1: Ramp-up gradual (warm-up)
    warmup: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: "30s", target: 10 },
        { duration: "30s", target: 10 },
      ],
      gracefulRampDown: "10s",
      tags: { scenario: "warmup" },
    },

    // Escenario 2: Carga sostenida (medición principal)
    sustained_load: {
      executor: "constant-vus",
      vus: 50,
      duration: "3m",
      startTime: "1m",   // Después del warmup
      gracefulStop: "10s",
      tags: { scenario: "sustained" },
    },

    // Escenario 3: Pico de carga (stress test)
    spike: {
      executor: "ramping-vus",
      startVUs: 50,
      stages: [
        { duration: "15s", target: 200 },
        { duration: "30s", target: 200 },
        { duration: "15s", target: 50  },
      ],
      startTime: "5m",
      tags: { scenario: "spike" },
    },
  },

  thresholds: {
    // SLOs del laboratorio
    http_req_duration: [
      "p(95)<500",   // p95 < 500ms (SLO de latencia)
      "p(99)<1000",  // p99 < 1s
    ],
    http_req_failed: ["rate<0.005"],  // Error rate < 0.5% (SLO de disponibilidad)
    success_rate:    ["rate>0.995"],
  },

  // Opciones de reporte
  summaryTrendStats: ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)", "p(99.9)"],
};

// ── Setup ────────────────────────────────────────────────────────────────────
export function setup() {
  console.log(`
════════════════════════════════════════════════════════
  Benchmark OTel Lab — Fase 4: Análisis de Overhead
  Base URL:     ${BASE_URL}
  Instrumentado: ${INSTRUMENTED}
  Modo:         ${INSTRUMENTED ? "CON OTel SDK" : "SIN instrumentación (baseline)"}
════════════════════════════════════════════════════════`);

  // Verificar health de los servicios antes de iniciar
  const health = http.get(`${BASE_URL}/health`);
  if (health.status !== 200) {
    throw new Error(`Service-a no responde: ${health.status}`);
  }
  return { instrumented: INSTRUMENTED, start_time: Date.now() };
}

// ── VU Function — escenario principal ────────────────────────────────────────
export default function (data) {
  const orderId = ORDER_IDS[Math.floor(Math.random() * ORDER_IDS.length)];
  const url = `${BASE_URL}/order/${orderId}`;

  const params = {
    headers: {
      "Content-Type": "application/json",
      "User-Agent":   `k6-benchmark/${data.instrumented ? "otel" : "baseline"}`,
      // Si está instrumentado, OTel SDK agrega automáticamente traceparent
    },
    tags: {
      instrumented: String(data.instrumented),
      order_id:     orderId,
    },
    timeout: "10s",
  };

  const startTime = Date.now();
  const response  = http.get(url, params);
  const duration  = Date.now() - startTime;

  // ── Verificaciones ────────────────────────────────────────────────────────
  const success = check(response, {
    "status 200":           (r) => r.status === 200,
    "body no vacío":        (r) => r.body && r.body.length > 0,
    "tiene trace_id":       (r) => {
      try {
        return data.instrumented ? JSON.parse(r.body).trace_id !== undefined : true;
      } catch { return false; }
    },
    "latencia < 500ms":     () => duration < 500,
    "body tiene order":     (r) => {
      try { return JSON.parse(r.body).order !== undefined; }
      catch { return false; }
    },
    "body tiene inventory": (r) => {
      try { return JSON.parse(r.body).inventory !== undefined; }
      catch { return false; }
    },
  });

  // ── Registrar métricas ────────────────────────────────────────────────────
  p99Latency.add(duration);
  successRate.add(success);

  if (!success || response.status >= 400) {
    errorCount.add(1);
  }

  // Extraer latencia de DB del header si el servicio la expone
  const dbMs = response.headers["X-DB-Duration-Ms"];
  if (dbMs) {
    dbLatency.add(parseFloat(dbMs));
  }

  // Pausa realista entre requests (simula usuario real)
  sleep(Math.random() * 0.5 + 0.1);  // 100-600ms
}

// ── Teardown: resumen final ───────────────────────────────────────────────────
export function handleSummary(data) {
  const mode = data.state.testRunDurationMs > 0 ? "otel" : "baseline";

  // Tabla de resultados para comparativa
  const p95 = data.metrics.http_req_duration?.values?.["p(95)"] || 0;
  const p99 = data.metrics.http_req_duration?.values?.["p(99)"] || 0;
  const avg = data.metrics.http_req_duration?.values?.["avg"]   || 0;
  const errRate = (data.metrics.http_req_failed?.values?.rate || 0) * 100;
  const rps = data.metrics.http_reqs?.values?.rate || 0;

  console.log(`
╔══════════════════════════════════════════════════════════╗
║         RESULTADOS — Fase 4: Overhead OTel               ║
║  Modo: ${INSTRUMENTED ? "CON OTel SDK    " : "SIN instrumentación"}                        ║
╠══════════════════════════════════════════════════════════╣
║  Métrica                    Valor                        ║
╠══════════════════════════════════════════════════════════╣
║  Latencia promedio:         ${avg.toFixed(2).padStart(8)} ms               ║
║  Latencia p95:              ${p95.toFixed(2).padStart(8)} ms               ║
║  Latencia p99:              ${p99.toFixed(2).padStart(8)} ms               ║
║  Error rate:                ${errRate.toFixed(3).padStart(8)} %               ║
║  Throughput (RPS):          ${rps.toFixed(2).padStart(8)} req/s            ║
╚══════════════════════════════════════════════════════════╝

Guarda estos resultados y compáralos con el modo contrario.
Overhead esperado OTel: p99 +3-8ms, CPU +2-5%, Mem +15-30MB.
`);

  return {
    stdout: textSummary(data, { indent: " ", enableColors: true }),
    [`results_${INSTRUMENTED ? "otel" : "baseline"}.json`]: JSON.stringify({
      mode: INSTRUMENTED ? "with_otel" : "baseline",
      timestamp: new Date().toISOString(),
      metrics: {
        latency_avg_ms: avg,
        latency_p95_ms: p95,
        latency_p99_ms: p99,
        error_rate_pct: errRate,
        throughput_rps: rps,
      }
    }, null, 2),
  };
}
