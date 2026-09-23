#!/usr/bin/env python3
"""E9: remote writer vs adapter cache policy, swept over propagation delay.

Per trial the adapter cache and the XAIR snapshot are seeded RUN; a remote
writer (another cell, an MES) then updates *only* the XAIR snapshot to
PAUSED; after ``delay_ms`` the intent is submitted under one policy.

The delay is causally relevant only for ``local_push``, whose refresh is
emulated by a fixed threshold (notified iff delay >= L). ``local_stale``
never refreshes and ``local_authoritative``/``xair`` always read the shared
snapshot, so their outcome is delay-independent by design; the sweep
documents that, it does not measure a propagation channel.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

from common import RESULTS_DIR, adapter, now_iso, released, wait_xair_state, wilson_ci, write_csv, xair_context

POLICIES = ("local_stale", "local_push", "local_authoritative", "xair")
DELAYS_MS = (0, 10, 50, 100, 250, 500)
PUSH_LATENCY_MS = 50
FRESHNESS_MS = 5000
RUN = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}
REMOTE_PAUSE = {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}}


def run_trial(policy: str, delay_ms: int, run_idx: int) -> dict:
    adapter("context", RUN)  # seeds adapter cache and XAIR snapshot
    intent = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": now_iso(),
        "freshness_window_ms": FRESHNESS_MS,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "robot_3", "run": run_idx},
    }
    xair_context(REMOTE_PAUSE)  # remote writer: XAIR snapshot only
    wait_xair_state("PAUSED")
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)
    push_notified = delay_ms >= PUSH_LATENCY_MS
    query = {"mode": policy}
    if policy == "local_push":
        query["push_notified"] = str(push_notified).lower()
    resp = adapter("intent", intent, **query)
    rel = released(resp)
    return {
        "policy": policy,
        "delay_ms": delay_ms,
        "push_notified": int(push_notified) if policy == "local_push" else "",
        "run": run_idx,
        "outcome": resp.get("outcome"),
        "reason": resp.get("reason"),
        "gateway_released": int(rel),
        "stale_executed": int(rel),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10, help="Trials per (delay, policy) cell")
    parser.add_argument("--out", default=str(RESULTS_DIR / "e9_consistency_sweep.csv"))
    args = parser.parse_args()

    rows = [run_trial(p, d, i) for d in DELAYS_MS for p in POLICIES for i in range(args.runs)]
    out = write_csv(Path(args.out), rows)
    summary = {}
    for p in POLICIES:
        sub = [r for r in rows if r["policy"] == p]
        k = sum(r["stale_executed"] for r in sub)
        summary[p] = {"runs": len(sub), "stale_rate": k / len(sub), "ci95": wilson_ci(k, len(sub))}
    print(json.dumps({"csv": str(out), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
