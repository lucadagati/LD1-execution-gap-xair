#!/usr/bin/env python3
"""E9: distributed context consistency sweep — propagation delay x cache policy."""

from __future__ import annotations

import argparse
import csv
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "http://127.0.0.1:9092"
XAIR = "http://127.0.0.1:8080"
AUDIT_FILE = ROOT / "experiments" / "results" / "ros_audit_state.json"
RESULTS = ROOT / "experiments" / "results" / "e9_consistency_sweep.csv"

POLICIES = ("local_stale", "local_push", "local_authoritative", "xair")
DELAYS_MS = (0, 10, 50, 100, 250, 500)
PUSH_LATENCY_MS = 50
FRESHNESS_MS = 5000


def _post(url: str, body: dict | None = None) -> dict:
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def post_adapter(path: str, body: dict, **query) -> dict:
    qs = urlencode(query)
    url = f"{ADAPTER}/{path}?{qs}" if qs else f"{ADAPTER}/{path}"
    return _post(url, body)


def post_xair_context(body: dict) -> dict:
    return _post(f"{XAIR}/v1/context/snapshot", body)


def audit_count() -> int | None:
    if not AUDIT_FILE.exists():
        return None
    try:
        return int(json.loads(AUDIT_FILE.read_text()).get("pose_count", 0))
    except Exception:
        return None


def build_intent(run_idx: int) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": FRESHNESS_MS,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "robot_3", "run": run_idx},
    }


def run_trial(policy: str, delay_ms: int, run_idx: int) -> dict:
    """Remote writer updates XAIR only; adapter-local cache seeded via POST /context."""
    init = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}
    post_adapter("context", init)
    time.sleep(0.03)
    post_xair_context(init)
    time.sleep(0.03)

    remote = {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}}
    post_xair_context(remote)
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)

    before = audit_count()
    intent = build_intent(run_idx)
    # Sensitivity/emulation study: emulate push arrival after a fixed latency L=50 ms
    # rather than measuring an asynchronous push channel. Notification is set iff
    # wall-clock wait since remote write has reached L (deterministic threshold).
    push_notified = delay_ms >= PUSH_LATENCY_MS
    if policy == "local_push":
        resp = post_adapter("intent", intent, mode="local_push", push_notified=str(push_notified).lower())
    else:
        resp = post_adapter("intent", intent, mode=policy)
    after = audit_count()

    stale = 1 if resp.get("outcome") == "EXECUTE" else 0
    witnessed = stale
    return {
        "policy": policy,
        "delay_ms": delay_ms,
        "push_notified": int(push_notified),
        "run": run_idx,
        "outcome": resp.get("outcome"),
        "reason": resp.get("reason"),
        "stale_executed": stale,
        "stale_observed": witnessed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rows = []
    for delay_ms in DELAYS_MS:
        for policy in POLICIES:
            for i in range(args.runs):
                rows.append(run_trial(policy, delay_ms, i))

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    summary = {}
    for policy in POLICIES:
        sub = [r for r in rows if r["policy"] == policy]
        summary[policy] = {
            "runs": len(sub),
            "stale_rate": sum(r["stale_executed"] for r in sub) / len(sub),
            "revoke_rate": sum(1 for r in sub if r["outcome"] == "REVOKE") / len(sub),
        }
    print(json.dumps({"csv": str(RESULTS), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
