#!/usr/bin/env python3
"""E10-deadline: the temporal bound at the release boundary.

Valid intents with a deadline ``d`` (and a long freshness window) are held by
the gateway for an induced delay swept across ``d``, then released through the
optimistic recheck or the atomic authorization commit. For every trial we
record the age of the decision at the check (gateway clock, optimistic; store
clock, atomic) and at the middleware call t_m (CLOCK_REALTIME, shared by the
processes of one kernel), and count releases whose age exceeded ``d``:

  late_at_check       admitted although the age at the check exceeded d (must be 0);
  late_at_middleware  admitted in time, but the age at t_m exceeded d: the
                      check-to-middleware interval crossed the deadline.
"""

from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from common import RESULTS_DIR, adapter, released, write_csv

RUN = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}


def trial(mode: str, deadline_ms: int, delay_ms: float) -> dict:
    t_d = datetime.now(timezone.utc)
    intent = {
        "id": str(uuid.uuid4()), "source": "ai", "timestamp_decision": t_d.isoformat(),
        "freshness_window_ms": 5000, "deadline_ms": deadline_ms,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": f"e10d_{uuid.uuid4().hex[:8]}"},
    }
    resp = adapter("intent", intent, mode=mode, publish_delay_ms=delay_ms)
    t_d_ms = t_d.timestamp() * 1000.0
    rel = released(resp)
    wall = resp.get("t_middleware_wall_ms")
    age_m = wall - t_d_ms if rel and wall is not None else None
    gate_wall = resp.get("t_gate_wall_ms")
    # age at the check: store clock at the atomic commit, gateway clock at the optimistic gate
    age_c = resp.get("commit_age_at_commit_ms") if mode == "xair_atomic" else (
        gate_wall - t_d_ms if gate_wall is not None else None)
    return {
        "mode": mode, "deadline_ms": deadline_ms, "induced_delay_ms": delay_ms,
        "gateway_released": int(rel), "reason": resp.get("reason"),
        "age_at_check_ms": age_c, "age_at_middleware_ms": age_m,
        "late_at_check": int(rel and age_c is not None and age_c > deadline_ms),
        "late_at_middleware": int(rel and age_m is not None and age_m > deadline_ms),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deadline-ms", type=int, default=100)
    ap.add_argument("--delays-ms", type=float, nargs=3, default=[85.0, 105.0, 0.5], metavar=("FROM", "TO", "STEP"))
    ap.add_argument("--runs", type=int, default=10, help="Trials per (mode, delay)")
    ap.add_argument("--seed", type=int, default=10)
    ap.add_argument("--out", default=str(RESULTS_DIR / "e10_deadline.csv"))
    args = ap.parse_args()
    lo, hi, step = args.delays_ms
    delays = [round(lo + k * step, 3) for k in range(int(round((hi - lo) / step)) + 1)]
    plan = [(m, d) for m in ("xair", "xair_atomic") for d in delays for _ in range(args.runs)]
    random.Random(args.seed).shuffle(plan)
    adapter("context", RUN)
    rows = [trial(m, args.deadline_ms, d) for m, d in plan]
    write_csv(Path(args.out), rows)
    for m in ("xair", "xair_atomic"):
        cell = [r for r in rows if r["mode"] == m]
        print(json.dumps({"mode": m, "trials": len(cell), "released": sum(r["gateway_released"] for r in cell),
                          "late_at_check": sum(r["late_at_check"] for r in cell),
                          "late_at_middleware": sum(r["late_at_middleware"] for r in cell)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
