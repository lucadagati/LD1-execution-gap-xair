#!/usr/bin/env python3
"""E14: the E1b drift pattern applied to four action classes.

Each scenario seeds a context under which the intent is admissible at t_d,
then applies a scenario-specific invalidation before submission.
STOP is included only as a further predicate shape ("stop if still
moving"); safety-reducing commands belong to the reflex layer and should not
be gated by preconditions that can revoke them (paper Sec. X).
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

from common import RESULTS_DIR, adapter, now_iso, released, wilson_ci, write_csv

SCENARIOS = {
    "RESUME": {
        "init": {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}},
        "invalidate": {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}},
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
    },
    "STOP": {
        "init": {"line": {"state": "RUN"}, "robot": {"moving": True}},
        "invalidate": {"robot": {"moving": False}},
        "preconditions": [{"expr": "robot.moving == true"}],
    },
    "GRASP": {
        "init": {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}},
        "invalidate": {"gripper": {"state": "CLOSED"}},
        "preconditions": [{"expr": "gripper.state == 'OPEN'"}],
    },
    "SET_SPEED": {
        "init": {"line": {"state": "RUN"}, "robot": {"speed": 1.0}},
        "invalidate": {"line": {"state": "PAUSED"}, "robot": {"speed": 0.0}},
        "preconditions": [{"expr": "line.state == 'RUN'"}],
    },
}


def run_scenario(name: str, cfg: dict, baseline: str, run_idx: int, pause_ms: float) -> dict:
    adapter("context", cfg["init"])
    intent = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": now_iso(),
        "freshness_window_ms": 1000,
        "preconditions": cfg["preconditions"],
        "payload": {"action_type": name, "target_entity": "robot_3", "parameters": {"run": run_idx}},
    }
    time.sleep(pause_ms / 1000.0)
    adapter("context", cfg["invalidate"])
    resp = adapter("intent", intent, mode=baseline)
    rel = released(resp)
    return {"scenario": name, "baseline": baseline, "run": run_idx, "outcome": resp.get("outcome"),
            "reason": resp.get("reason"), "gateway_released": int(rel), "stale_executed": int(rel)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--pause-ms", type=float, default=200)
    parser.add_argument("--baselines", nargs="+", default=["direct", "local", "xair"])
    parser.add_argument("--out", default=str(RESULTS_DIR / "e14_variants.csv"))
    args = parser.parse_args()
    rows = [run_scenario(n, c, b, i, args.pause_ms) for n, c in SCENARIOS.items() for b in args.baselines for i in range(args.runs)]
    out = write_csv(Path(args.out), rows)
    summary = {}
    for r in rows:
        summary.setdefault(f"{r['scenario']}/{r['baseline']}", []).append(r["stale_executed"])
    print(json.dumps({k: {"stale": sum(v), "n": len(v), "ci95": wilson_ci(sum(v), len(v))} for k, v in summary.items()}, indent=2))
    print(f"Wrote {len(rows)} rows to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
