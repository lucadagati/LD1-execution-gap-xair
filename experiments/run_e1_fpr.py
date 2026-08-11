#!/usr/bin/env python3
"""E1c FPR study: valid context → XAIR must EXECUTE (wrongful revoke rate)."""

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
RESULTS_DIR = ROOT / "experiments" / "results"
ADAPTER = "http://127.0.0.1:9092"


def post(path: str, body: dict, mode: str = "xair") -> dict:
    import urllib.request

    url = f"{ADAPTER}/{path}?mode={mode}" if path == "intent" else f"{ADAPTER}/{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def run_fpr(run_idx: int) -> dict:
    post("context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}, "robot": {"speed": 0.05}})
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    intent = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": ts,
        "freshness_window_ms": 500,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "robot_3", "parameters": {}},
        "priority": 5,
    }
    time.sleep(0.02)
    resp = post("intent", intent, mode="xair")
    valid = True
    wrongful_revoke = valid and resp.get("outcome") == "REVOKE"
    return {
        "run": run_idx,
        "valid_intent": valid,
        "outcome": resp.get("outcome"),
        "wrongful_revoke": wrongful_revoke,
        "ros_published": bool(resp.get("ros_published")),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "e1_fpr.csv")
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    rows = [run_fpr(i) for i in range(args.runs)]
    with args.out.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    valid = len(rows)
    fpr = sum(1 for r in rows if r["wrongful_revoke"]) / valid
    print(json.dumps({"FPR": fpr, "runs": valid, "out": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
