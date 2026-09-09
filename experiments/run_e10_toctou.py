#!/usr/bin/env python3
"""E10: TOCTOU window measurement with controlled injection in [t_v, t_p].

Uses the adapter's native `inject_pause_after_validation_ms` / `publish_delay_ms`
query parameters so injection timing is measured server-side relative to the
adapter's own t_validate_end, rather than approximated by a client-side sleep.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "http://127.0.0.1:9092"
RESULTS = ROOT / "experiments" / "results" / "e10_toctou.csv"

DEFAULT_OFFSETS_MS = (0, 1, 3, 10, 30)


def _post(url: str, body: dict | None = None) -> dict:
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def post_intent(body: dict, **query) -> dict:
    qs = urlencode({k: str(v) for k, v in query.items() if v is not None})
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


def run_trial(offset_ms: float, publish_delay_ms: float, run_idx: int, do_inject: bool) -> dict:
    init = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}
    _post(f"{ADAPTER}/context", init)

    intent = build_intent()
    resp = post_intent(
        intent,
        mode="xair",
        publish_delay_ms=publish_delay_ms,
        inject_pause_after_validation_ms=offset_ms if do_inject else None,
    )

    t_validate_end = resp.get("t_validate_end_ms")
    t_injection_start = resp.get("t_injection_start_ms")
    t_injection_end = resp.get("t_injection_end_ms")
    t_recheck_end = resp.get("t_recheck_end_ms")

    timing_valid = None
    if do_inject:
        timing_valid = (
            t_validate_end is not None
            and t_injection_start is not None
            and t_injection_end is not None
            and t_recheck_end is not None
            and t_validate_end < t_injection_start <= t_injection_end < t_recheck_end
        )

    blocked = 1 if resp.get("reason") == "context_version_changed_at_publish" else 0
    authorized = 1 if resp.get("outcome") == "EXECUTE" else 0
    stale = 1 if do_inject and authorized else 0

    return {
        "run": run_idx,
        "inject": int(do_inject),
        "inject_offset_ms": offset_ms if do_inject else 0,
        "publish_delay_ms": publish_delay_ms,
        "timing_valid": int(timing_valid) if timing_valid is not None else "",
        "validation_to_gate_ms": resp.get("validation_to_gate_ms"),
        "validation_to_publish_ms": resp.get("validation_to_publish_ms"),
        "recheck_to_publish_ms": resp.get("recheck_to_publish_ms"),
        "t_validate_end_ms": t_validate_end,
        "t_injection_start_ms": t_injection_start,
        "t_injection_end_ms": t_injection_end,
        "t_recheck_end_ms": t_recheck_end,
        "toctou_blocked": blocked,
        "authorized_publish": authorized,
        "stale_publish": stale,
        "outcome": resp.get("outcome"),
        "reason": resp.get("reason"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-per-delay", type=int, default=40, help="Trials per offset cell (mix of injected + control)")
    parser.add_argument("--offsets-ms", type=float, nargs="+", default=list(DEFAULT_OFFSETS_MS))
    parser.add_argument("--publish-delay-ms", type=float, default=50.0)
    parser.add_argument("--inject-fraction", type=float, default=0.75, help="Fraction of each cell's trials that are injected (rest are non-injected controls)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    n_inject = round(args.runs_per_delay * args.inject_fraction)
    n_control = args.runs_per_delay - n_inject

    rows = []
    run_idx = 0
    for offset in args.offsets_ms:
        plan = [True] * n_inject + [False] * n_control
        rng.shuffle(plan)
        for do_inject in plan:
            rows.append(run_trial(offset, args.publish_delay_ms, run_idx, do_inject))
            run_idx += 1

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    inj = [r for r in rows if r["inject"]]
    ctrl = [r for r in rows if not r["inject"]]
    timing_valid_rows = [r for r in inj if r["timing_valid"] == 1]
    blocked = sum(r["toctou_blocked"] for r in inj)
    stale = sum(r["stale_publish"] for r in inj)
    ctrl_released = sum(r["authorized_publish"] for r in ctrl)
    n = len(inj)
    lo, hi = wilson_ci(stale, n) if n else (0.0, 0.0)

    def pct(vals, q):
        s = sorted(v for v in vals if v is not None)
        if not s:
            return 0.0
        idx = min(len(s) - 1, max(0, int(len(s) * q) - (1 if q >= 1 else 0)))
        return s[idx]

    gate_lat = [r["validation_to_gate_ms"] for r in rows if r["validation_to_gate_ms"] is not None]
    pub_lat = [r["validation_to_publish_ms"] for r in rows if r["validation_to_publish_ms"] is not None]

    print(json.dumps({
        "runs": len(rows),
        "injected_runs": n,
        "control_runs": len(ctrl),
        "timing_valid_injections": len(timing_valid_rows),
        "toctou_blocked": blocked,
        "stale_publish": stale,
        "stale_publish_ci95": [lo, hi],
        "control_released": ctrl_released,
        "validation_to_gate_p50_p95_p99_ms": [pct(gate_lat, 0.5), pct(gate_lat, 0.95), pct(gate_lat, 0.99)],
        "validation_to_release_p50_p95_p99_ms": [pct(pub_lat, 0.5), pct(pub_lat, 0.95), pct(pub_lat, 0.99)],
        "out": str(RESULTS),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
