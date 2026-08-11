#!/usr/bin/env python3
"""E11: stratified stochastic replay with mixed valid/invalid contexts.

Randomness affects whether context is drifted, pause delay, predicate count,
and producer source. Correctness is measured against the ground-truth label
(valid → EXECUTE/DEGRADE; drifted → REVOKE), not against a trivial always-stale
pattern.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "http://127.0.0.1:9092"
RESULTS = ROOT / "experiments" / "results" / "e11_stratified.csv"
SOURCES = ("ai", "xr", "mes")
PAUSE_MS = (50, 400)


def _post(url: str, body: dict) -> dict:
    import urllib.request

    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def post_intent(body: dict, mode: str) -> dict:
    return _post(f"{ADAPTER}/intent?{urlencode({'mode': mode})}", body)


def build_intent(rng: random.Random, n_pred: int, source: str, ctx_speed: int) -> dict:
    preds = [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}]
    for _ in range(max(0, n_pred - 2)):
        preds.append({"expr": f"robot.speed >= {max(0, ctx_speed - 1)}"})
    return {
        "id": str(uuid.uuid4()),
        "source": source,
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": 5000,
        "preconditions": preds[:n_pred],
        "payload": {"action_type": "RESUME", "target_entity": "robot_3", "ctx_speed": ctx_speed},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--baseline", default="xair")
    parser.add_argument("--drift-prob", type=float, default=0.5, help="Probability context is invalidated before intent")
    args = parser.parse_args()
    rng = random.Random(args.seed)
    rows = []
    for i in range(args.runs):
        pause_ms = rng.uniform(*PAUSE_MS)
        n_pred = rng.choice((2, 4, 8))
        source = rng.choice(SOURCES)
        ctx_speed = rng.randint(1, 5)
        drifted = rng.random() < args.drift_prob
        _post(
            f"{ADAPTER}/context",
            {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}, "robot": {"speed": ctx_speed}},
        )
        time.sleep(0.02)
        intent = build_intent(rng, n_pred, source, ctx_speed)
        if drifted:
            _post(
                f"{ADAPTER}/context",
                {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}, "robot": {"speed": ctx_speed}},
            )
            time.sleep(pause_ms / 1000.0)
            expected = "REVOKE"
        else:
            time.sleep(pause_ms / 1000.0)
            expected = "EXECUTE"
        resp = post_intent(intent, args.baseline)
        outcome = resp.get("outcome", "UNKNOWN")
        correct = 1 if outcome == expected or (expected == "EXECUTE" and outcome == "DEGRADE") else 0
        rows.append({
            "run": i,
            "baseline": args.baseline,
            "pause_ms": round(pause_ms, 2),
            "predicate_count": n_pred,
            "source": source,
            "drifted": int(drifted),
            "expected": expected,
            "outcome": outcome,
            "correct": correct,
            "stale_executed": 1 if drifted and outcome == "EXECUTE" else 0,
            "validation_latency_ms": resp.get("validation_latency_ms", 0),
        })
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    stale = sum(r["stale_executed"] for r in rows)
    correct = sum(r["correct"] for r in rows)
    drifted_n = sum(r["drifted"] for r in rows)
    print(json.dumps({
        "runs": len(rows),
        "drifted_runs": drifted_n,
        "correct_rate": correct / len(rows),
        "stale_rate": stale / max(drifted_n, 1),
        "out": str(RESULTS),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
