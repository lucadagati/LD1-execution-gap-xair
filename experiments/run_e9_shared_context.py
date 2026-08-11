#!/usr/bin/env python3
"""E9: shared cross-cell context — local adapter cache vs centralized XAIR snapshot."""

from __future__ import annotations

import csv
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "http://127.0.0.1:9092"
XAIR = "http://127.0.0.1:8080"
AUDIT_FILE = ROOT / "experiments" / "results" / "ros_audit_state.json"
RESULTS = ROOT / "experiments" / "results" / "e9_shared_context.csv"


def _post(url: str, body: dict, retries: int = 5) -> dict:
    import urllib.error
    import urllib.request

    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode()
                if not raw.strip():
                    raise ValueError("empty response body")
                return json.loads(raw)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_err = exc
            time.sleep(0.08 * (attempt + 1))
    return {"outcome": "UNKNOWN", "ros_published": False, "error": str(last_err)}


def audit_count() -> int | None:
    if not AUDIT_FILE.exists():
        return None
    for _ in range(3):
        try:
            return int(json.loads(AUDIT_FILE.read_text()).get("pose_count", 0))
        except (json.JSONDecodeError, ValueError):
            time.sleep(0.02)
    return None


def post_adapter(path: str, body: dict, mode: str | None = None) -> dict:
    url = f"{ADAPTER}/{path}"
    if mode:
        url = f"{url}?mode={mode}"
    return _post(url, body)


def post_xair_context(body: dict) -> dict:
    return _post(f"{XAIR}/v1/context/snapshot", body)


def build_intent(run_idx: int) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": 500,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "robot_3", "run": run_idx},
    }


def get_xair_context() -> dict:
    import urllib.request

    with urllib.request.urlopen(f"{XAIR}/v1/context/snapshot", timeout=5) as r:
        return json.loads(r.read().decode()).get("context", {})


def run_once(run_idx: int) -> dict:
    # Cell A adapter bootstraps local cache as RUN.
    post_adapter("context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
    intent = build_intent(run_idx)
    time.sleep(0.05)
    # Remote cell B updates centralized store only (no adapter POST).
    post_xair_context({"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
    for _ in range(15):
        if get_xair_context().get("line", {}).get("state") == "PAUSED":
            break
        time.sleep(0.02)
        post_xair_context({"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
    time.sleep(0.08)

    a0 = audit_count()
    local = post_adapter("intent", intent, mode="local")
    time.sleep(0.15)
    a1 = audit_count()
    xair = post_adapter("intent", {**intent, "id": str(uuid.uuid4())}, mode="xair")
    time.sleep(0.15)
    a2 = audit_count()

    local_observed = (a1 - a0 > 0) if a0 is not None and a1 is not None else None
    xair_observed = (a2 - a1 > 0) if a1 is not None and a2 is not None else None

    xair_out = xair.get("outcome") or "UNKNOWN"
    return {
        "run": run_idx,
        "local_outcome": local.get("outcome"),
        "local_ros": bool(local.get("ros_published")),
        "local_ros_observed": local_observed,
        "xair_outcome": xair_out,
        "xair_ros": bool(xair.get("ros_published")),
        "xair_ros_observed": xair_observed,
        "local_stale": bool(local.get("ros_published")),
        "xair_blocked": not xair.get("ros_published"),
        "xair_revoke": xair_out == "REVOKE",
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    args = parser.parse_args()

    rows = [run_once(i) for i in range(args.runs)]
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    n = len(rows)
    obs_rows = [r for r in rows if r["local_ros_observed"] is not None]
    summary = {
        "runs": n,
        "local_stale_rate": sum(1 for r in rows if r["local_stale"]) / n,
        "xair_blocked_rate": sum(1 for r in rows if r["xair_blocked"]) / n,
        "local_stale_observed_rate": (
            sum(1 for r in obs_rows if r["local_ros_observed"]) / len(obs_rows) if obs_rows else None
        ),
        "xair_publish_observed_rate": (
            sum(1 for r in obs_rows if r["xair_ros_observed"]) / len(obs_rows) if obs_rows else None
        ),
    }
    print(json.dumps({"csv": str(RESULTS), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
