#!/usr/bin/env python3
"""E4 HTTP load test on POST /v1/intents (real FastAPI path)."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "experiments" / "results"
XAIR = "http://127.0.0.1:8080"


def post_intent(i: int) -> tuple[dict, float]:
    import urllib.request

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    body = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": ts,
        "freshness_window_ms": 500,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        # Distinct target per intent: this endpoint posts directly to XAIR core
        # (bypassing the adapter's t_p publish/report step), so an EXECUTE here
        # acquires the coordinator's per-target lock with nothing to release it.
        # A reused target would therefore show DELAY (target_busy) on its
        # second use rather than isolating validation/adapter overhead.
        "payload": {"action_type": "TICK", "target_entity": f"e4_target_{i}", "parameters": {}},
    }
    t0 = time.perf_counter()
    req = urllib.request.Request(
        f"{XAIR}/v1/intents",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.loads(r.read().decode())
    e2e_ms = (time.perf_counter() - t0) * 1000.0
    return out, e2e_ms


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--intents", type=int, default=1000)
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "e4_load_http.csv")
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    # Prime context
    import urllib.request
    urllib.request.urlopen(
        urllib.request.Request(
            f"{XAIR}/v1/context/snapshot",
            data=json.dumps({"line": {"state": "RUN"}}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ),
        timeout=5,
    )

    internal_lats = []
    e2e_lats = []
    outcomes: dict[str, int] = {}
    t0 = time.perf_counter()
    for i in range(args.intents):
        out, e2e = post_intent(i)
        internal_lats.append(float(out.get("validation_latency_ms") or 0))
        e2e_lats.append(e2e)
        outcome = out.get("outcome") or "UNKNOWN"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    elapsed = time.perf_counter() - t0

    def p99(vals: list[float]) -> float:
        return sorted(vals)[int(len(vals) * 0.99) - 1] if vals else 0.0

    row = {
        "intents": args.intents,
        "elapsed_s": elapsed,
        "throughput_ips": args.intents / elapsed,
        "outcome_execute": outcomes.get("EXECUTE", 0),
        "outcome_delay": outcomes.get("DELAY", 0),
        "outcome_revoke": outcomes.get("REVOKE", 0),
        "outcome_degrade": outcomes.get("DEGRADE", 0),
        "vl_internal_p50_ms": sorted(internal_lats)[len(internal_lats) // 2],
        "vl_internal_p99_ms": p99(internal_lats),
        "vl_e2e_p50_ms": sorted(e2e_lats)[len(e2e_lats) // 2],
        "vl_e2e_p99_ms": p99(e2e_lats),
    }
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        w.writeheader()
        w.writerow(row)
    print(json.dumps(row, indent=2))
    if outcomes.get("EXECUTE", 0) != args.intents:
        print(f"WARNING: {args.intents - outcomes.get('EXECUTE', 0)}/{args.intents} intents did not EXECUTE: {outcomes}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
