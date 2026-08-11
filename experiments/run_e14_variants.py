#!/usr/bin/env python3
"""E14: heterogeneous action scenarios beyond RESUME."""

from __future__ import annotations

import argparse
import csv
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "http://127.0.0.1:9092"
RESULTS = ROOT / "experiments" / "results" / "e14_variants.csv"

SCENARIOS = {
    "RESUME": {
        "init": {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}},
        "invalidate": {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}},
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
        "action_type": "RESUME",
    },
    "STOP": {
        "init": {"line": {"state": "RUN"}, "robot": {"moving": True}},
        "invalidate": {"line": {"state": "RUN"}, "robot": {"moving": False}},
        "preconditions": [{"expr": "robot.moving == True"}],
        "action_type": "STOP",
    },
    "GRASP": {
        "init": {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}},
        "invalidate": {"line": {"state": "RUN"}, "gripper": {"state": "CLOSED"}},
        "preconditions": [{"expr": "gripper.state == 'OPEN'"}],
        "action_type": "GRASP",
    },
    "SET_SPEED": {
        "init": {"line": {"state": "RUN"}, "robot": {"speed": 1.0}},
        "invalidate": {"line": {"state": "PAUSED"}, "robot": {"speed": 0.0}},
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "action_type": "SET_SPEED",
    },
}


def _post(url: str, body: dict) -> dict:
    import urllib.request

    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def run_scenario(name: str, cfg: dict, baseline: str, run_idx: int) -> dict:
    _post(f"{ADAPTER}/context", cfg["init"])
    intent = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": 500,
        "preconditions": cfg["preconditions"],
        "payload": {"action_type": cfg["action_type"], "target_entity": "robot_3", "run": run_idx},
    }
    time.sleep(0.4)
    _post(f"{ADAPTER}/context", cfg["invalidate"])
    resp = _post(f"{ADAPTER}/intent?mode={baseline}", intent)
    return {
        "scenario": name,
        "baseline": baseline,
        "run": run_idx,
        "stale_executed": 1 if resp.get("outcome") == "EXECUTE" else 0,
        "outcome": resp.get("outcome"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--baselines", nargs="+", default=["direct", "xair", "local"])
    args = parser.parse_args()
    rows = []
    for name, cfg in SCENARIOS.items():
        for baseline in args.baselines:
            for i in range(args.runs):
                rows.append(run_scenario(name, cfg, baseline, i))
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
