#!/usr/bin/env python3
"""E1b: stale RESUME under symmetric adapter baselines (HTTP gateway path).

Protocol per trial (paper Sec. IX, drift trial protocol):
  1. seed context RUN/OPEN through the gateway (adapter cache + XAIR snapshot);
  2. build the intent (t_d) while its preconditions hold;
  3. wait ``--pause-ms`` and apply a supervisory PAUSED/CLOSED update;
  4. submit the intent under the baseline and observe the gateway decision.

The intent is admissible at t_d and obsolete at t_v, so every trial belongs
to O_C. SER is the fraction released at the gateway boundary
(``gateway_released``), independent of whether ROS 2 is installed; the ROS
witness, when running, is recorded as a separate corroboration column.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from common import RESULTS_DIR, adapter, audit_count, now_iso, released, wait_xair_state, wilson_ci, write_csv

BASELINES = ("direct", "naive", "local", "xair")
RUN = {"line": {"state": "RUN"}, "robot": {"speed": 0.05}, "gripper": {"state": "OPEN"}}
PAUSED = {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}}


def build_resume_intent(seed: int, run_idx: int, freshness_ms: int, deadline_ms: int) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": now_iso(),
        "freshness_window_ms": freshness_ms,
        "deadline_ms": deadline_ms,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
        "payload": {
            "action_type": "RESUME",
            "target_entity": "robot_3",
            "parameters": {"reason": "cv_clearance", "seed": seed, "run": run_idx},
        },
        "priority": 5,
    }


def run_trial(run_idx: int, baseline: str, pause_ms: float, freshness_ms: int, deadline_ms: int, seed: int) -> dict:
    adapter("context", RUN)
    intent = build_resume_intent(seed, run_idx, freshness_ms, deadline_ms)
    t_decision = time.monotonic()
    time.sleep(pause_ms / 1000.0)
    adapter("context", PAUSED)
    paused_seen = wait_xair_state("PAUSED")

    audit_before = audit_count()
    resp = adapter("intent", intent, mode=baseline)
    age_at_submit_ms = (time.monotonic() - t_decision) * 1000.0
    time.sleep(0.05)
    audit_after = audit_count()

    outcome = resp.get("outcome") or "UNKNOWN"
    rel = released(resp)
    ros_observed = (audit_after - audit_before > 0) if audit_before is not None and audit_after is not None else None
    return {
        "scenario": "e1b",
        "run": run_idx,
        "baseline": baseline,
        "seed": seed,
        "outcome": outcome,
        "reason": resp.get("reason"),
        "paused_committed_before_submit": int(paused_seen),
        "gateway_released": int(rel),
        "stale_executed": int(rel),  # every trial is obsolete at t_v by construction
        "correct_revoke": int(outcome == "REVOKE" and not rel),
        "ros_published": int(bool(resp.get("ros_published"))),
        "ros_observed": "" if ros_observed is None else int(ros_observed),
        "validation_latency_ms": float(resp.get("validation_latency_ms") or 0),
        "e2e_latency_ms": float(resp.get("e2e_latency_ms") or 0),
        "intent_age_at_submit_ms": round(age_at_submit_ms, 3),
        "pause_ms": pause_ms,
        "freshness_ms": freshness_ms,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="E1b symmetric baseline HTTP experiments")
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--pause-ms", type=float, default=200)
    parser.add_argument("--freshness-ms", type=int, default=1000)
    parser.add_argument("--deadline-ms", type=int, default=1500)
    parser.add_argument("--baselines", nargs="+", default=list(BASELINES))
    parser.add_argument("--out-csv", default=str(RESULTS_DIR / "e1_baselines.csv"))
    args = parser.parse_args()

    rows = [
        run_trial(i, b, args.pause_ms, args.freshness_ms, args.deadline_ms, args.seed)
        for b in args.baselines
        for i in range(args.runs)
    ]
    out = write_csv(Path(args.out_csv), rows)

    summary = {}
    for b in args.baselines:
        sub = [r for r in rows if r["baseline"] == b]
        stale = sum(r["stale_executed"] for r in sub)
        summary[b] = {"runs": len(sub), "SER": stale / len(sub), "SER_ci95": wilson_ci(stale, len(sub)),
                      "max_intent_age_ms": max(r["intent_age_at_submit_ms"] for r in sub)}
    print(json.dumps({"csv": str(out), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
