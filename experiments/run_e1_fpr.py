#!/usr/bin/env python3
"""E1c: valid-intent control. Context satisfies every precondition at t_v and t_p.

A false positive is any valid intent that is *not released* at the gateway,
whatever the reason, so the rate bounds fail-closed over-blocking.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

from common import RESULTS_DIR, adapter, now_iso, released, wilson_ci, write_csv

RUN = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}, "robot": {"speed": 0.05}}


def run_trial(run_idx: int, mode: str) -> dict:
    adapter("context", RUN)
    intent = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": now_iso(),
        "freshness_window_ms": 1000,
        "deadline_ms": 1500,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "robot_3", "parameters": {}},
        "priority": 5,
    }
    time.sleep(0.02)
    resp = adapter("intent", intent, mode=mode)
    rel = released(resp)
    return {
        "run": run_idx,
        "baseline": mode,
        "valid_intent": 1,
        "outcome": resp.get("outcome"),
        "reason": resp.get("reason"),
        "gateway_released": int(rel),
        "wrongful_revoke": int(not rel),
        "e2e_latency_ms": resp.get("e2e_latency_ms"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--baselines", nargs="+", default=["local", "xair"])
    parser.add_argument("--out", default=str(RESULTS_DIR / "e1_fpr.csv"))
    args = parser.parse_args()

    rows = [run_trial(i, m) for m in args.baselines for i in range(args.runs)]
    out = write_csv(Path(args.out), rows)
    summary = {}
    for m in args.baselines:
        sub = [r for r in rows if r["baseline"] == m]
        k = sum(r["wrongful_revoke"] for r in sub)
        summary[m] = {"runs": len(sub), "FPR": k / len(sub), "FPR_ci95": wilson_ci(k, len(sub))}
    print(json.dumps({"summary": summary, "out": str(out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
