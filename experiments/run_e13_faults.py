#!/usr/bin/env python3
"""E13: quantitative fault injection suite (extends E7)."""

from __future__ import annotations

import argparse
import csv
import json
import random
import string
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "http://127.0.0.1:9092"
RESULTS = ROOT / "experiments" / "results" / "e13_faults.csv"


def req(url: str, body: dict | None = None, method: str = "POST") -> tuple[int, dict]:
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except Exception as e:
        code = getattr(e, "code", 0) or 0
        return code, {"ok": False, "error": str(e)}


def post_raw(url: str, payload: bytes) -> tuple[int, dict]:
    import urllib.request

    r = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except Exception as e:
        return getattr(e, "code", 0) or 0, {"ok": False, "error": str(e)}


def intent_body(**kw) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": 500,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "line_1"},
    }
    base.update(kw)
    return base


def fuzz_payload(rng: random.Random) -> bytes:
    kind = rng.randint(0, 4)
    if kind == 0:
        return b"{not-json"
    if kind == 1:
        return json.dumps({"id": "x"}).encode()
    if kind == 2:
        return json.dumps(intent_body()).encode() + b"\x00extra"
    if kind == 3:
        return json.dumps({"timestamp_decision": "bad-ts", "freshness_window_ms": 500}).encode()
    return json.dumps(intent_body(preconditions=[{"expr": "line.state == '" + rng.choice(string.ascii_letters) + "'"}])).encode()


def fail_closed(status: int, out: dict) -> bool:
    """Structural faults must never authorize EXECUTE."""
    if status >= 400:
        return True
    return out.get("outcome") != "EXECUTE" and out.get("ros_published") is not True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-per-fault", type=int, default=30)
    parser.add_argument("--fuzz-count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    rows = []

    for i in range(args.fuzz_count):
        status, out = post_raw(f"{ADAPTER}/intent?mode=xair", fuzz_payload(rng))
        rows.append({
            "fault": "malformed_fuzz",
            "run": i,
            "pass": fail_closed(status, out),
            "http_status": status,
            "outcome": out.get("outcome"),
            "error": out.get("error"),
        })

    for i in range(args.runs_per_fault):
        iid = str(uuid.uuid4())
        intent = intent_body(id=iid)
        req(f"{ADAPTER}/context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
        _, r1 = req(f"{ADAPTER}/intent?mode=xair", intent)
        _, r2 = req(f"{ADAPTER}/intent?mode=xair", intent)
        rows.append({
            "fault": "duplicate_id",
            "run": i,
            "pass": r1.get("outcome") == r2.get("outcome") == "EXECUTE",
            "outcome": r2.get("outcome"),
        })

        past = (datetime.now(timezone.utc) - timedelta(milliseconds=600)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        _, r_past = req(f"{ADAPTER}/intent?mode=xair", intent_body(timestamp_decision=past))
        rows.append({
            "fault": "clock_skew_past",
            "run": i,
            "pass": r_past.get("outcome") == "REVOKE",
            "outcome": r_past.get("outcome"),
        })

        # Bounded future skew: within tolerance must accept (EXECUTE); criterion is not vacuous.
        future = (datetime.now(timezone.utc) + timedelta(milliseconds=500)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        _, r_fut = req(f"{ADAPTER}/intent?mode=xair", intent_body(timestamp_decision=future))
        rows.append({
            "fault": "clock_skew_future",
            "run": i,
            "pass": r_fut.get("outcome") == "EXECUTE",
            "outcome": r_fut.get("outcome"),
        })

        # Far-future skew beyond TemporalValidator.max_future_skew_ms (default 1000 ms) must revoke.
        far = (datetime.now(timezone.utc) + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        _, r_far = req(f"{ADAPTER}/intent?mode=xair", intent_body(timestamp_decision=far))
        rows.append({
            "fault": "clock_skew_far_future",
            "run": i,
            "pass": r_far.get("outcome") == "REVOKE",
            "outcome": r_far.get("outcome"),
        })

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r.keys()}))
        w.writeheader()
        w.writerows(rows)

    by_fault: dict[str, list] = {}
    for r in rows:
        by_fault.setdefault(r["fault"], []).append(r)
    summary = {f: {"passed": sum(x["pass"] for x in xs), "total": len(xs)} for f, xs in by_fault.items()}
    passed = sum(r["pass"] for r in rows)
    print(json.dumps({"passed": passed, "total": len(rows), "by_fault": summary, "out": str(RESULTS)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
