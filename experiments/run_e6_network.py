#!/usr/bin/env python3
"""E6: kernel network impairment (tc netem) on the loopback path.

Requires CAP_NET_ADMIN. netem on ``lo`` delays *every* loopback packet on
the host, so run it only on a dedicated machine.

Each configuration interleaves drifted and valid-control trials, and every
row records the revocation reason. Both are needed to interpret the result:
under large delays an intent can also be revoked because its freshness
window elapsed in transit, which is fail-closed but is not evidence that the
context change was observed, and without valid controls a policy that
revokes everything would look perfect.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
import uuid
from pathlib import Path

from common import RESULTS_DIR, adapter, now_iso, released, write_csv

NETEM_CONFIGS = [
    {"delay_ms": 0, "jitter_ms": 0, "loss_pct": 0},
    {"delay_ms": 10, "jitter_ms": 0, "loss_pct": 0},
    {"delay_ms": 50, "jitter_ms": 10, "loss_pct": 0},
    {"delay_ms": 100, "jitter_ms": 10, "loss_pct": 0},
    {"delay_ms": 250, "jitter_ms": 50, "loss_pct": 0},
    {"delay_ms": 50, "jitter_ms": 0, "loss_pct": 0.1},
    {"delay_ms": 50, "jitter_ms": 0, "loss_pct": 1.0},
]
RUN = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}
PAUSED = {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}}


def tc(*args: str) -> bool:
    cmd = ["tc", "qdisc", *args] if os.geteuid() == 0 else ["sudo", "tc", "qdisc", *args]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def tc_apply(delay_ms: float, jitter_ms: float, loss_pct: float) -> bool:
    tc("del", "dev", "lo", "root")
    if delay_ms <= 0 and loss_pct <= 0:
        return True
    netem = ["delay", f"{delay_ms}ms"] + ([f"{jitter_ms}ms"] if jitter_ms > 0 else [])
    netem += ["loss", f"{loss_pct}%"] if loss_pct > 0 else []
    return tc("add", "dev", "lo", "root", "netem", *netem)


def run_trial(cfg: dict, run_idx: int, drifted: bool, freshness_ms: int) -> dict:
    adapter("context", RUN, timeout=60)
    intent = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": now_iso(),
        "freshness_window_ms": freshness_ms,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "line_1"},
    }
    if drifted:
        adapter("context", PAUSED, timeout=60)
    resp = adapter("intent", intent, timeout=60, mode="xair")
    return {
        **cfg,
        "run": run_idx,
        "drifted": int(drifted),
        "outcome": resp.get("outcome"),
        "reason": resp.get("reason"),
        "gateway_released": int(released(resp)),
        "e2e_latency_ms": resp.get("e2e_latency_ms", 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30, help="Drifted trials per configuration")
    parser.add_argument("--controls", type=int, default=10, help="Valid-intent controls per configuration")
    parser.add_argument("--freshness-ms", type=int, default=500)
    parser.add_argument("--use-netem", action="store_true", help="Apply tc netem (needs CAP_NET_ADMIN)")
    parser.add_argument("--out", default=str(RESULTS_DIR / "e6_network.csv"))
    args = parser.parse_args()

    rows = []
    try:
        for cfg in NETEM_CONFIGS:
            applied = tc_apply(cfg["delay_ms"], cfg["jitter_ms"], cfg["loss_pct"]) if args.use_netem else False
            plan = [True] * args.runs + [False] * args.controls
            for i, drifted in enumerate(plan):
                rows.append({**run_trial(cfg, i, drifted, args.freshness_ms), "netem_applied": int(applied)})
                time.sleep(0.01)
    finally:
        if args.use_netem:
            tc("del", "dev", "lo", "root")

    out = write_csv(Path(args.out), rows)
    print(f"Wrote {len(rows)} rows to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
