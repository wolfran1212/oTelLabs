#!/usr/bin/env python3
"""
analyze_overhead.py — Análisis comparativo de overhead OTel (Fase 4)

Uso:
    # 1. Ejecutar benchmarks:
    #    k6 run --env INSTRUMENTED=false k6_benchmark.js  → genera results_baseline.json
    #    k6 run --env INSTRUMENTED=true  k6_benchmark.js  → genera results_otel.json
    # 2. Analizar:
    python3 analyze_overhead.py
"""

import json
import sys
from pathlib import Path

def load_results(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"⚠️  {path} no encontrado. Ejecutar primero el benchmark k6.")
        return {}
    with open(p) as f:
        return json.load(f)

def pct_diff(baseline: float, otel: float) -> str:
    if baseline == 0:
        return "N/A"
    diff = ((otel - baseline) / baseline) * 100
    sign = "+" if diff > 0 else ""
    emoji = "🔴" if diff > 10 else "🟡" if diff > 3 else "🟢"
    return f"{emoji} {sign}{diff:.2f}%"

def main():
    baseline = load_results("results_baseline.json")
    otel     = load_results("results_otel.json")

    if not baseline or not otel:
        print("Faltan archivos de resultados. Ver instrucciones arriba.")
        sys.exit(1)

    b = baseline.get("metrics", {})
    o = otel.get("metrics", {})

    print("\n" + "═" * 72)
    print("  ANÁLISIS DE OVERHEAD — OpenTelemetry SDK (Fase 4 del Lab)")
    print("═" * 72)
    print(f"\n  Baseline: {baseline.get('timestamp', 'N/A')}")
    print(f"  OTel:     {otel.get('timestamp', 'N/A')}\n")

    headers = f"{'Métrica':<28} {'Baseline':>12} {'Con OTel':>12} {'Overhead':>14}"
    print(headers)
    print("─" * 72)

    metrics = [
        ("Latencia promedio (ms)",  "latency_avg_ms",  1,   "ms"),
        ("Latencia p95 (ms)",       "latency_p95_ms",  1,   "ms"),
        ("Latencia p99 (ms)",       "latency_p99_ms",  1,   "ms"),
        ("Error rate (%)",          "error_rate_pct",  0.1, "%"),
        ("Throughput (req/s)",      "throughput_rps",  1,   "rps"),
    ]

    for label, key, precision, unit in metrics:
        b_val = b.get(key, 0)
        o_val = o.get(key, 0)
        diff  = pct_diff(b_val, o_val)
        print(f"  {label:<26} {b_val:>10.{precision}f} {unit}  {o_val:>10.{precision}f} {unit}  {diff:>14}")

    print("─" * 72)

    # Latencia adicional absoluta
    p99_overhead = o.get("latency_p99_ms", 0) - b.get("latency_p99_ms", 0)
    print(f"\n  Latencia adicional p99:  {p99_overhead:+.2f} ms")
    print(f"  Overhead aceptable:      < 10ms en p99 (estándar industria)")

    # Veredicto
    print("\n" + "─" * 72)
    if p99_overhead < 10 and o.get("error_rate_pct", 0) < 0.5:
        print("  ✅ OVERHEAD ACEPTABLE — OTel es viable para producción")
        print("     El overhead de instrumentación está dentro de los límites")
        print("     establecidos por el SRE Workbook (< 3-5% en p99).")
    elif p99_overhead < 30:
        print("  🟡 OVERHEAD MODERADO — Revisar configuración del Collector")
        print("     Considerar: aumentar batch_size, reducir export_interval,")
        print("     habilitar tail_sampling para reducir volumen.")
    else:
        print("  🔴 OVERHEAD EXCESIVO — Requiere optimización")
        print("     Revisar: ¿Collector en el mismo nodo? ¿Red congestionada?")
        print("     ¿Exporters configurados correctamente?")

    print("\n  Tabla para el reporte del laboratorio:")
    print("  ┌──────────────────────────┬────────────┬────────────┬──────────┐")
    print("  │ Métrica                  │ Sin OTel   │ Con OTel   │ Overhead │")
    print("  ├──────────────────────────┼────────────┼────────────┼──────────┤")
    for label, key, precision, unit in metrics:
        b_val = b.get(key, 0)
        o_val = o.get(key, 0)
        diff  = ((o_val - b_val) / b_val * 100) if b_val else 0
        sign  = "+" if diff > 0 else ""
        print(f"  │ {label:<24} │ {b_val:>8.{precision}f} {unit} │ {o_val:>8.{precision}f} {unit} │ {sign}{diff:>5.1f}%  │")
    print("  └──────────────────────────┴────────────┴────────────┴──────────┘")
    print()

if __name__ == "__main__":
    main()
