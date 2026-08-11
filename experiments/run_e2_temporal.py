#!/usr/bin/env python3
"""E2: temporal validity — freshness/deadline sweeps with controlled delay."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "scripts"))

ADAPTER = "http://127.0.0.1:9092"
RESULTS = ROOT / "experiments" / "results" / "e2_temporal.csv"


def post(path: str, body: dict, mode: str = "xair") -> dict:
    import urllib.request

    url = f"{ADAPTER}/{path}?mode={mode}" if path == "intent" else f"{ADAPTER}/{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()

    rows = []
    for freshness in (200, 500, 1000):
        for delay_ms in (0, 100, 300, 600, 900):
            for i in range(args.runs):
                post("context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
                ts = datetime.now(timezone.utc) - timedelta(milliseconds=delay_ms)
                intent = {
                    "id": str(uuid.uuid4()),
                    "source": "ai",
                    "timestamp_decision": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    "freshness_window_ms": freshness,
                    "deadline_ms": freshness + 200,
                    "preconditions": [{"expr": "line.state == 'RUN'"}],
                    "payload": {"action_type": "RESUME", "target_entity": "line_1"},
                }
                time.sleep(0.01)
                resp = post("intent", intent)
                rows.append({
                    "freshness_ms": freshness,
                    "delay_ms": delay_ms,
                    "run": i,
                    "outcome": resp.get("outcome"),
                    "ros_published": resp.get("ros_published", False),
                })

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
