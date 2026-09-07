"""Derives loadtest/results/summary.json from the committed raw CSVs -
the README's numbers are quoted from this file's output, not hand-typed,
so a reader can regenerate the claim from the same raw data instead of
trusting prose transcription.

Usage: python3 loadtest/summarize.py
"""
from __future__ import annotations

import csv
import json
import os

_DIR = os.path.join(os.path.dirname(__file__), "results")


def _aggregated_row(path: str) -> dict[str, str]:
    with open(path) as f:
        for row in csv.DictReader(f):
            if row["Name"] == "Aggregated":
                return row
    raise ValueError(f"no Aggregated row in {path}")


def _run_summary(csv_name: str) -> dict[str, float | int]:
    row = _aggregated_row(os.path.join(_DIR, csv_name))
    return {
        "requests": int(row["Request Count"]),
        "failures": int(row["Failure Count"]),
        "median_ms": float(row["50%"]),
        "p95_ms": float(row["95%"]),
        "p99_ms": float(row["99%"]),
        "requests_per_s": round(float(row["Requests/s"]), 2),
    }


def main() -> None:
    baseline = _run_summary("baseline_stats.csv")
    cache = _run_summary("cache_stats.csv")
    concurrency = _run_summary("gateway_concurrency_stats.csv")

    p95_reduction = 1 - cache["p95_ms"] / baseline["p95_ms"]
    median_reduction = 1 - cache["median_ms"] / baseline["median_ms"]
    throughput_multiple = cache["requests"] / baseline["requests"]

    summary = {
        "cache_ab_test": {
            "description": (
                "same 3-concurrent-user, 10-minute traffic mix run twice "
                "against loadtest-model (qwen2.5:0.5b, num_predict=60) - "
                "once with x-cache: no-store (baseline), once with the "
                "cache live"
            ),
            "baseline": baseline,
            "cache_on": cache,
            "p95_reduction_pct": round(p95_reduction * 100, 1),
            "median_reduction_pct": round(median_reduction * 100, 1),
            "throughput_multiple": round(throughput_multiple, 2),
        },
        "gateway_concurrency_test": {
            "description": (
                "50 concurrent users, 2 minutes, bypass_cache=true, against "
                "loadtest/stub_backend.py (an instant Ollama-contract stub) "
                "- isolates the gateway's own pipeline from real model "
                "inference latency"
            ),
            "result": concurrency,
        },
    }
    out_path = os.path.join(_DIR, "summary.json")
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
