#!/usr/bin/env python3
"""E4: sequential load on POST /v1/intents (XAIR core only, no gateway recheck).

Each intent names a distinct target: this endpoint stops at AUTHORIZED (no
t_p report follows), so a reused target would be held and later intents
would be DELAYed, measuring coordination rather than validation cost.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from common import RESULTS_DIR, XAIR, http_json, now_iso, percentile, write_csv, xair_context


RUN_TAG = uuid.uuid4().hex[:8]  # targets are unique per run: this path never releases a target lock


def post_intent(i: int) -> tuple[dict, float]:
    body = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": now_iso(),
        "freshness_window_ms": 500,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "TICK", "target_entity": f"e4_{RUN_TAG}_{i}", "parameters": {}},
    }
    t0 = time.perf_counter()
    out = http_json(f"{XAIR}/v1/intents", body, timeout=10)
    return out, (time.perf_counter() - t0) * 1000.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--intents", type=int, default=10000)
    parser.add_argument("--out", default=str(RESULTS_DIR / "e4_load_http.csv"))
    args = parser.parse_args()

    xair_context({"line": {"state": "RUN"}})
    internal, e2e = [], []
    outcomes: dict[str, int] = {}
    t0 = time.perf_counter()
    for i in range(args.intents):
        out, ms = post_intent(i)
        internal.append(float(out.get("validation_latency_ms") or 0))
        e2e.append(ms)
        outcome = out.get("outcome") or "UNKNOWN"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    elapsed = time.perf_counter() - t0

    row = {
        "intents": args.intents,
        "elapsed_s": elapsed,
        "throughput_ips": args.intents / elapsed,
        "outcome_execute": outcomes.get("EXECUTE", 0),
        "outcome_delay": outcomes.get("DELAY", 0),
        "outcome_revoke": outcomes.get("REVOKE", 0),
        "outcome_degrade": outcomes.get("DEGRADE", 0),
        "vl_internal_p50_ms": percentile(internal, 0.50),
        "vl_internal_p99_ms": percentile(internal, 0.99),
        "vl_e2e_p50_ms": percentile(e2e, 0.50),
        "vl_e2e_p99_ms": percentile(e2e, 0.99),
    }
    write_csv(Path(args.out), [row])
    print(json.dumps(row, indent=2))
    if outcomes.get("EXECUTE", 0) != args.intents:
        print(f"WARNING: not every intent executed: {outcomes}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
