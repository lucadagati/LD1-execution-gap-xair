#!/usr/bin/env python3
"""
E1b manufacturing via full HTTP stack — symmetric baselines (xair/direct/naive).
Same protocol as Unity DefectEventEmitter.EmitResumeIntent; outputs TestResults JSON + CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "scripts"))

ADAPTER = "http://127.0.0.1:9092"
XAIR = "http://127.0.0.1:8080"
OUT_DIR = ROOT.parent / "AdaptiX-Quest" / "TestResults"
RESULTS_DIR = ROOT / "experiments" / "results"
AUDIT_FILE = RESULTS_DIR / "ros_audit_state.json"
BASELINES = ("xair", "direct", "naive", "local")


def audit_count() -> int | None:
    if not AUDIT_FILE.exists():
        return None
    try:
        data = json.loads(AUDIT_FILE.read_text())
        return int(data.get("pose_count", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def get_xair_context() -> dict:
    import urllib.request

    with urllib.request.urlopen(f"{XAIR}/v1/context/snapshot", timeout=5) as r:
        return json.loads(r.read().decode()).get("context", {})


def post_adapter(path: str, body: dict, mode: str | None = None) -> dict:
    import urllib.request

    url = f"{ADAPTER}/{path}"
    if mode and path in ("intent", "command"):
        url = f"{url}?mode={mode}"
    t0 = time.perf_counter()
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.loads(r.read().decode())
    out["e2e_latency_ms"] = (time.perf_counter() - t0) * 1000.0
    return out


def build_resume_intent(seed: int, run_idx: int, freshness_ms: int) -> dict:
    rng = random.Random(seed + run_idx)
    ts = datetime.now(timezone.utc)
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": ts_str,
        "freshness_window_ms": freshness_ms,
        "deadline_ms": 800,
        "preconditions": [
            {"expr": "line.state == 'RUN'"},
            {"expr": "gripper.state == 'OPEN'"},
        ],
        "payload": {
            "action_type": "RESUME",
            "target_entity": "robot_3",
            "parameters": {"reason": "cv_clearance", "seed": seed, "run": run_idx},
        },
        "priority": 5,
    }


def run_e1_single(
    run_idx: int,
    baseline: str,
    pause_ms: float,
    freshness_ms: int,
    seed: int,
    pause_order: str = "before_intent",
) -> dict:
    """E1b: context invalid at t_e (line PAUSED) — stale RESUME if executed."""
    post_adapter(
        "context",
        {
            "line": {"state": "RUN"},
            "robot": {"speed": 0.05},
            "gripper": {"state": "OPEN"},
        },
    )
    intent = build_resume_intent(seed, run_idx, freshness_ms)

    if pause_order == "before_intent":
        time.sleep(pause_ms / 1000.0)
        paused = post_adapter("context", {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
        if paused.get("ok") is False and paused.get("error"):
            return _fail_row(run_idx, baseline, seed, pause_ms, freshness_ms, f"context_pause_failed:{paused.get('error')}")
        # Ensure XAIR sees PAUSED before intent (fixes SER=0.01 race on run 55)
        for _ in range(5):
            ctx = get_xair_context()
            if ctx.get("line", {}).get("state") == "PAUSED":
                break
            time.sleep(0.02)
            post_adapter("context", {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
        time.sleep(0.05)
        audit_before = audit_count()
        resp = post_adapter("intent", intent, mode=baseline)
        time.sleep(0.05)
        audit_after = audit_count()
    else:
        audit_before = audit_count()
        resp = post_adapter("intent", intent, mode=baseline)
        time.sleep(pause_ms / 1000.0)
        post_adapter("context", {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
        time.sleep(0.05)
        audit_after = audit_count()

    context_invalid_at_te = True
    outcome = resp.get("outcome", "UNKNOWN")
    ros_published = bool(resp.get("ros_published"))
    ros_observed = (
        audit_after - audit_before > 0
        if audit_before is not None and audit_after is not None
        else None
    )
    stale_executed = ros_published and context_invalid_at_te
    stale_observed = bool(ros_observed) and context_invalid_at_te and outcome != "UNKNOWN"
    obsolete = context_invalid_at_te and outcome != "UNKNOWN"
    correct_revoke = obsolete and outcome == "REVOKE" and not resp.get("ros_published")

    return {
        "scenario": "e1b",
        "run": run_idx,
        "baseline": baseline,
        "seed": seed,
        "pause_ms": pause_ms,
        "freshness_ms": freshness_ms,
        "pause_order": pause_order,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "response": resp,
        "context_invalid_at_te": context_invalid_at_te,
        "ros_published": ros_published,
        "ros_observed": ros_observed,
        "stale_executed": stale_executed,
        "stale_observed": stale_observed,
        "witness_agreement": (ros_observed is None) or (ros_observed == ros_published),
        "obsolete_intent": obsolete,
        "correct_revoke": correct_revoke,
        "outcome": outcome,
        "validation_latency_ms": float(resp.get("validation_latency_ms") or 0),
        "e2e_latency_ms": float(resp.get("e2e_latency_ms") or 0),
    }


def _fail_row(run_idx, baseline, seed, pause_ms, freshness_ms, reason):
    return {
        "scenario": "e1b",
        "run": run_idx,
        "baseline": baseline,
        "seed": seed,
        "pause_ms": pause_ms,
        "freshness_ms": freshness_ms,
        "pause_order": "before_intent",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "response": {"outcome": "UNKNOWN", "reason": reason},
        "context_invalid_at_te": True,
        "ros_published": False,
        "ros_observed": None,
        "stale_executed": False,
        "stale_observed": False,
        "witness_agreement": True,
        "obsolete_intent": False,
        "correct_revoke": False,
        "outcome": "UNKNOWN",
        "validation_latency_ms": 0.0,
        "e2e_latency_ms": 0.0,
    }


def main():
    parser = argparse.ArgumentParser(description="E1b symmetric baseline HTTP experiments")
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--pause-ms", type=float, default=400)
    parser.add_argument("--freshness-ms", type=int, default=500)
    parser.add_argument("--pause-order", choices=("before_intent", "after_intent"), default="before_intent")
    parser.add_argument("--baselines", nargs="+", default=list(BASELINES))
    parser.add_argument("--out-csv", type=Path, default=RESULTS_DIR / "e1_baselines.csv")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    args.out_csv.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for baseline in args.baselines:
        for i in range(args.runs):
            row = run_e1_single(
                i, baseline, args.pause_ms, args.freshness_ms, args.seed, args.pause_order
            )
            path = OUT_DIR / f"e1b_run_{i:03d}_{baseline}.json"
            path.write_text(json.dumps(row, indent=2))
            rows.append({k: row[k] for k in (
                "scenario", "run", "baseline", "seed", "outcome", "ros_published",
                "ros_observed", "stale_executed", "stale_observed", "witness_agreement",
                "obsolete_intent", "correct_revoke",
                "validation_latency_ms", "e2e_latency_ms", "pause_ms", "freshness_ms",
            )})

    fieldnames = list(rows[0].keys())
    with args.out_csv.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    summary = {}
    for b in args.baselines:
        sub = [r for r in rows if r["baseline"] == b]
        attempted = len(sub)
        unknown = sum(1 for r in sub if r["outcome"] == "UNKNOWN")
        stale = sum(1 for r in sub if r["stale_executed"])
        obsolete = sum(1 for r in sub if r["obsolete_intent"])
        correct_rev = sum(1 for r in sub if r["correct_revoke"])
        lats = [r["validation_latency_ms"] for r in sub if r["validation_latency_ms"] > 0]
        summary[b] = {
            "attempted": attempted,
            "unknown_rate": unknown / attempted if attempted else 0,
            "SER": stale / attempted if attempted else 0,
            "POA": correct_rev / max(obsolete - unknown, 1) if obsolete else 1.0,
            "vl_p99_ms": sorted(lats)[int(len(lats) * 0.99) - 1] if lats else 0,
        }

    summary_path = RESULTS_DIR / "e1_baselines_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps({"csv": str(args.out_csv), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
