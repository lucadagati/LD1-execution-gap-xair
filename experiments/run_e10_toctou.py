#!/usr/bin/env python3
"""E10: TOCTOU window measurement with controlled injection in [t_v, t_p]."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "http://127.0.0.1:9092"
XAIR = "http://127.0.0.1:8080"
RESULTS = ROOT / "experiments" / "results" / "e10_toctou.csv"

PUBLISH_DELAY_MS = 3.0


def _post(url: str, body: dict | None = None) -> dict:
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def post_intent(body: dict, **query) -> dict:
    qs = urlencode({k: str(v) for k, v in query.items()})
    return _post(f"{ADAPTER}/intent?{qs}", body)


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n) / denom
    return max(0, center - margin), min(1, center + margin)


def build_intent() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": 5000,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "line_1"},
    }


def inject_paused(delay_s: float) -> None:
    time.sleep(max(0.0, delay_s))
    _post(f"{XAIR}/v1/context/snapshot", {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})


def run_trial(inject_offset_ms: float, run_idx: int, do_inject: bool) -> dict:
    init = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}
    _post(f"{ADAPTER}/context", init)
    _post(f"{XAIR}/v1/context/snapshot", init)
    time.sleep(0.02)

    intent = build_intent()
    injector = None
    if do_inject:
        # Inject during widened publish window: validate_end + offset
        inject_at = (PUBLISH_DELAY_MS / 1000.0) * 0.5 + inject_offset_ms / 1000.0
        injector = threading.Thread(target=inject_paused, args=(inject_at,), daemon=True)
        injector.start()

    resp = post_intent(intent, mode="xair", publish_delay_ms=PUBLISH_DELAY_MS)
    if injector:
        injector.join(timeout=2.0)

    window = float(resp.get("toctou_window_ms") or 0)
    recheck_pub = float(resp.get("recheck_to_publish_ms") or 0)
    blocked = 1 if resp.get("reason") == "context_version_changed_at_publish" else 0
    authorized = 1 if resp.get("outcome") == "EXECUTE" else 0
    # Stale publication only when an injected drift still authorized EXECUTE.
    stale = 1 if do_inject and authorized else 0
    return {
        "run": run_idx,
        "inject": int(do_inject),
        "inject_offset_ms": inject_offset_ms,
        "publish_delay_ms": PUBLISH_DELAY_MS,
        "toctou_window_ms": window,
        "recheck_to_publish_ms": recheck_pub,
        "t_validate_end_ms": resp.get("t_validate_end_ms", 0),
        "t_recheck_start_ms": resp.get("t_recheck_start_ms", 0),
        "t_recheck_end_ms": resp.get("t_recheck_end_ms", 0),
        "t_publish_end_ms": resp.get("t_publish_end_ms", 0),
        "toctou_blocked": blocked,
        "authorized_publish": authorized,
        "stale_publish": stale,
        "outcome": resp.get("outcome"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    rows = []
    for i in range(args.runs):
        do_inject = rng.random() < 0.7
        offset = rng.uniform(0.2, PUBLISH_DELAY_MS * 0.95) if do_inject else 0.0
        rows.append(run_trial(offset, i, do_inject))

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    inj = [r for r in rows if r["inject"]]
    blocked = sum(r["toctou_blocked"] for r in inj)
    stale = sum(r["stale_publish"] for r in inj)
    n = len(inj)
    lo, hi = wilson_ci(blocked, n) if n else (0.0, 0.0)
    windows = sorted(float(r["toctou_window_ms"]) for r in rows)
    print(json.dumps({
        "runs": len(rows),
        "injected_runs": n,
        "toctou_blocked": blocked,
        "stale_publish": stale,
        "blocked_ci95": [lo, hi],
        "window_p50_ms": windows[len(windows) // 2] if windows else 0,
        "window_p99_ms": windows[int(len(windows) * 0.99) - 1] if windows else 0,
        "out": str(RESULTS),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
