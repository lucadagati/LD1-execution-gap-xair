#!/usr/bin/env python3
"""E4 load test — validation latency at 100 intent/s."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xair.core.models import ActionDescriptor, ActionIntent
from xair.core.runtime import XAIRRuntime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--intents", type=int, default=1000)
    parser.add_argument("--out", type=Path, default=ROOT / "experiments" / "results" / "e4_load.csv")
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    rt = XAIRRuntime(context={"robot": {"speed": 0.0}, "line": {"state": "RUN"}})
    now = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    for i in range(args.intents):
        intent = ActionIntent(
            id=f"load-{i}",
            source="ai",
            timestamp_decision=now,
            freshness_window_ms=500,
            preconditions=["robot.speed < 0.1"],
            payload=ActionDescriptor("TICK", f"robot_{i % 10}"),
        )
        rt.process_intent(intent, now=now)
    elapsed = time.perf_counter() - t0
    m = rt.get_metrics()
    row = {
        "intents": args.intents,
        "elapsed_s": elapsed,
        "throughput_ips": args.intents / elapsed,
        "p50_ms": m["validation_latency_p50_ms"],
        "p99_ms": m["validation_latency_p99_ms"],
    }
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        w.writeheader()
        w.writerow(row)
    print(json.dumps(row, indent=2))


if __name__ == "__main__":
    main()
