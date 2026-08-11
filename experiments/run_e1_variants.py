#!/usr/bin/env python3
"""E1 variants: stale MOVE and stale GRASP (same framework as E1b)."""

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
sys.path.insert(0, str(ROOT.parent / "scripts"))

ADAPTER = "http://127.0.0.1:9092"
XAIR = "http://127.0.0.1:8080"
RESULTS = ROOT / "experiments" / "results" / "e1_variants.csv"

SCENARIOS = {
    "move": {
        "action_type": "MOVE",
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "robot.speed <= 0.1"}],
        "invalidate": {"line": {"state": "RUN"}, "robot": {"speed": 0.5}},
    },
    "grasp": {
        "action_type": "GRASP",
        "preconditions": [{"expr": "gripper.state == 'OPEN'"}, {"expr": "line.state == 'RUN'"}],
        "invalidate": {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}},
    },
}


def post(path: str, body: dict, mode: str | None = None) -> dict:
    import urllib.request

    url = f"{ADAPTER}/{path}"
    if mode and path == "intent":
        url = f"{url}?mode={mode}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def get_context() -> dict:
    import urllib.request

    with urllib.request.urlopen(f"{XAIR}/v1/context/snapshot", timeout=5) as r:
        return json.loads(r.read().decode()).get("context", {})


def run_variant(scenario: str, baseline: str, run_idx: int, pause_ms: float) -> dict:
    cfg = SCENARIOS[scenario]
    post("context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}, "robot": {"speed": 0.05}})
    intent = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": 500,
        "preconditions": cfg["preconditions"],
        "payload": {"action_type": cfg["action_type"], "target_entity": "robot_3"},
    }
    time.sleep(pause_ms / 1000.0)
    post("context", cfg["invalidate"])
    for _ in range(5):
        if get_context().get("line", {}).get("state") == cfg["invalidate"].get("line", {}).get("state", "RUN"):
            break
        time.sleep(0.02)
    resp = post("intent", intent, mode=baseline)
    stale = bool(resp.get("ros_published"))
    return {
        "scenario": scenario,
        "baseline": baseline,
        "run": run_idx,
        "outcome": resp.get("outcome"),
        "ros_published": resp.get("ros_published", False),
        "stale_executed": stale,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--baselines", nargs="+", default=["xair", "direct"])
    args = parser.parse_args()

    rows = []
    for scenario in SCENARIOS:
        for baseline in args.baselines:
            for i in range(args.runs):
                rows.append(run_variant(scenario, baseline, i, 400))

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
