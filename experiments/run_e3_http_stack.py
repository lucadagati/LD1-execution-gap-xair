#!/usr/bin/env python3
"""E3 multi-agent conflict via XAIR batch API — AI vs XR on robot_arm."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "experiments" / "results"
XAIR = "http://127.0.0.1:8080"


def post_batch(intents: list[dict]) -> dict:
    import urllib.request

    req = urllib.request.Request(
        f"{XAIR}/v1/intents/batch",
        data=json.dumps(intents).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def run_e3(run_idx: int) -> dict:
    import urllib.request

    urllib.request.urlopen(
        urllib.request.Request(
            f"{XAIR}/v1/context/snapshot",
            data=json.dumps({"line": {"state": "RUN"}}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ),
        timeout=5,
    )
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    ai = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": ts,
        "freshness_window_ms": 500,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "STOP", "target_entity": "robot_arm", "parameters": {}},
        "priority": 5,
    }
    xr = {
        "id": str(uuid.uuid4()),
        "source": "xr",
        "timestamp_decision": ts,
        "freshness_window_ms": 500,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "SET_POSE", "target_entity": "robot_arm", "parameters": {}},
        "priority": 5,
    }
    out = post_batch([ai, xr])
    results = {r["source"]: r for r in out.get("results", [])}
    xr_out = results.get("xr", {}).get("outcome")
    ai_out = results.get("ai", {}).get("outcome")
    xr_wins = xr_out in ("EXECUTE", "DEGRADE") and ai_out == "REVOKE"
    return {
        "run": run_idx,
        "winner": out.get("winner"),
        "ai_outcome": ai_out,
        "xr_outcome": xr_out,
        "xr_wins": int(xr_wins),
        "cv": out.get("cv", 0),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "e3_conflict_http.csv")
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    rows = [run_e3(i) for i in range(args.runs)]
    with args.out.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    cv_total = sum(int(r["cv"]) for r in rows)
    xr_rate = sum(int(r["xr_wins"]) for r in rows) / len(rows)
    print(json.dumps({"CV": cv_total, "xr_win_rate": xr_rate, "out": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
