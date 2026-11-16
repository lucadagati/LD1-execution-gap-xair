#!/usr/bin/env python3
"""E11: mixed valid/drifted trials with randomized pause, predicate count, and source.

Correctness is scored against the per-trial ground-truth label
(valid -> released, drifted -> not released). Because conjunctive evaluation
stops at the first false predicate, a drifted trial evaluates only its first
predicate whatever its length: the per-length cost is therefore reported on
valid trials only, where every predicate is evaluated.
"""

from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from pathlib import Path

from common import RESULTS_DIR, adapter, now_iso, released, write_csv

SOURCES = ("ai", "xr", "mes")
PAUSE_MS = (50, 400)
PREDICATE_COUNTS = (2, 4, 8, 16, 32)


def build_intent(n_pred: int, source: str, ctx_speed: int) -> dict:
    preds = [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}]
    preds += [{"expr": f"robot.speed >= {max(0, ctx_speed - 1)}"} for _ in range(max(0, n_pred - 2))]
    return {
        "id": str(uuid.uuid4()),
        "source": source,
        "timestamp_decision": now_iso(),
        "freshness_window_ms": 5000,
        "preconditions": preds[:n_pred],
        "payload": {"action_type": "RESUME", "target_entity": "robot_3", "parameters": {"ctx_speed": ctx_speed}},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--baseline", default="xair")
    parser.add_argument("--drift-prob", type=float, default=0.5)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    out_path = Path(args.out) if args.out else RESULTS_DIR / f"e11_stratified_seed{args.seed}.csv"
    rng = random.Random(args.seed)

    rows = []
    for i in range(args.runs):
        pause_ms = rng.uniform(*PAUSE_MS)
        n_pred = rng.choice(PREDICATE_COUNTS)
        source = rng.choice(SOURCES)
        ctx_speed = rng.randint(1, 5)
        drifted = rng.random() < args.drift_prob
        adapter("context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}, "robot": {"speed": ctx_speed}})
        time.sleep(0.02)
        intent = build_intent(n_pred, source, ctx_speed)
        if drifted:
            adapter("context", {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
        time.sleep(pause_ms / 1000.0)
        resp = adapter("intent", intent, mode=args.baseline)
        rel = released(resp)
        rows.append({
            "run": i,
            "seed": args.seed,
            "baseline": args.baseline,
            "pause_ms": round(pause_ms, 2),
            "predicate_count": n_pred,
            "predicates_evaluated": 1 if drifted else n_pred,
            "source": source,
            "drifted": int(drifted),
            "expected_release": int(not drifted),
            "gateway_released": int(rel),
            "outcome": resp.get("outcome"),
            "reason": resp.get("reason"),
            "correct": int(rel == (not drifted)),
            "stale_executed": int(drifted and rel),
            "wrongful_revoke": int((not drifted) and not rel),
            "validation_latency_ms": resp.get("validation_latency_ms", 0),
        })
    write_csv(out_path, rows)
    drifted_n = sum(r["drifted"] for r in rows)
    print(json.dumps({
        "runs": len(rows),
        "drifted_runs": drifted_n,
        "correct_rate": sum(r["correct"] for r in rows) / len(rows),
        "stale": sum(r["stale_executed"] for r in rows),
        "wrongful_revoke": sum(r["wrongful_revoke"] for r in rows),
        "out": str(out_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
