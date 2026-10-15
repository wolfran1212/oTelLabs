#!/usr/bin/env python3
"""
collect_stats.py — CPU y memoria de los contenedores durante el benchmark (Fase 4)

Muestrea `docker stats` mientras corre k6 y guarda promedios en stats_<modo>.json,
que luego lee analyze_overhead.py. Correr EN PARALELO con k6, en otra terminal:

    python3 benchmark/collect_stats.py baseline      # mientras corre k6 baseline
    python3 benchmark/collect_stats.py otel          # mientras corre k6 con OTel

Por defecto espera 60s (warm-up de k6) y muestrea 300s (carga sostenida de 75 VUs).
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from statistics import mean

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

APP_CONTAINERS = ["service-a", "service-b"]
ALL_CONTAINERS = APP_CONTAINERS + ["otel-collector"]

UNITS = {"b": 1 / 1024**2, "kib": 1 / 1024, "kb": 1 / 1024, "mib": 1, "mb": 1,
         "gib": 1024, "gb": 1024}


def to_mib(value: str) -> float:
    m = re.match(r"([\d.]+)\s*([a-zA-Z]+)", value.strip())
    if not m:
        return 0.0
    return float(m.group(1)) * UNITS.get(m.group(2).lower(), 1)


def sample() -> dict:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *ALL_CONTAINERS],
        capture_output=True, text=True, check=True,
    ).stdout
    rows = {}
    for line in out.splitlines():
        d = json.loads(line)
        rows[d["Name"]] = {
            "cpu_pct": float(d["CPUPerc"].rstrip("%") or 0),
            "mem_mib": to_mib(d["MemUsage"].split("/")[0]),
        }
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["baseline", "otel"])
    parser.add_argument("--delay", type=int, default=60, help="segundos a esperar (warm-up)")
    parser.add_argument("--duration", type=int, default=300, help="segundos de muestreo")
    args = parser.parse_args()

    print(f"Esperando {args.delay}s de warm-up...")
    time.sleep(args.delay)

    samples = []
    end = time.time() + args.duration
    while time.time() < end:
        samples.append(sample())
        print(f"\r  muestras: {len(samples)}", end="", flush=True)
    print()

    per_container = {}
    for name in ALL_CONTAINERS:
        cpu = [s[name]["cpu_pct"] for s in samples if name in s]
        mem = [s[name]["mem_mib"] for s in samples if name in s]
        if cpu:
            per_container[name] = {
                "cpu_avg_pct": mean(cpu), "cpu_max_pct": max(cpu),
                "mem_avg_mib": mean(mem), "mem_max_mib": max(mem),
            }

    apps = [per_container[n] for n in APP_CONTAINERS if n in per_container]
    result = {
        "mode": args.mode,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "samples": len(samples),
        # Suma de service-a + service-b: el overhead del SDK vive en los procesos de la app
        "apps": {
            "cpu_avg_pct": sum(a["cpu_avg_pct"] for a in apps),
            "mem_avg_mib": sum(a["mem_avg_mib"] for a in apps),
        },
        "containers": per_container,
    }
    path = f"stats_{args.mode}.json"
    with open(path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nGuardado {path}")
    for name, c in per_container.items():
        print(f"  {name:<15} CPU avg {c['cpu_avg_pct']:6.1f}%   Mem avg {c['mem_avg_mib']:7.1f} MiB")


if __name__ == "__main__":
    main()
