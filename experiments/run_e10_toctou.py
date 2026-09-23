#!/usr/bin/env python3
"""E10: invalidation injected inside the gateway's [t_v, t_p] window.

The gateway (``mode=xair``) sleeps ``publish_delay_ms`` after XAIR returns,
then rechecks version/predicates and releases. With
``inject_pause_after_validation_ms`` it also starts a thread that writes an
invalidating context update ``offset`` ms after validation. All instants
come from the gateway's monotonic clock.

Each injected trial is classified by where the write landed:

  before_recheck  injection completed before the recheck started: the gate
                  must block (a release here is a stale publication);
  concurrent      the write overlapped [recheck start, release]: its commit
                  instant is not observable, so a release is counted as a
                  *potential* stale publication (upper bound);
  after_release   the write started after the release completed: the intent
                  was admissible at t_p, so by Eq. (2) this is not stale
                  actuation but a post-release invalidation no pre-publication
                  gate can prevent.

The unprotected residual window is recheck-end -> release-end
(``recheck_to_publish_ms``), reported separately.
"""

from __future__ import annotations

import argparse
import json
import random
import uuid
from pathlib import Path

from common import RESULTS_DIR, adapter, now_iso, percentile, released, wilson_ci, write_csv

DEFAULT_OFFSETS_MS = (0, 1, 3, 10, 30)
RUN = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}


def classify(resp: dict) -> str:
    inj_start, inj_end = resp.get("t_injection_start_ms"), resp.get("t_injection_end_ms")
    rc_start, pub_end = resp.get("t_recheck_start_ms"), resp.get("t_publish_end_ms")
    if inj_start is None or inj_end is None or rc_start is None:
        return "not_observed"
    if inj_end < rc_start:
        return "before_recheck"
    if pub_end is not None and inj_start > pub_end:
        return "after_release"
    if pub_end is None and inj_start > (resp.get("t_recheck_end_ms") or rc_start):
        return "after_gate_decision"
    return "concurrent"


def run_trial(offset_ms: float, publish_delay_ms: float, run_idx: int, do_inject: bool) -> dict:
    adapter("context", RUN)
    intent = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": now_iso(),
        "freshness_window_ms": 5000,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "line_1"},
    }
    resp = adapter(
        "intent", intent, mode="xair", publish_delay_ms=publish_delay_ms,
        inject_pause_after_validation_ms=offset_ms if do_inject else None,
    )
    rel = released(resp)
    window = classify(resp) if do_inject else "control"
    stale = int(do_inject and rel and window == "before_recheck")
    potential = int(do_inject and rel and window in ("before_recheck", "concurrent"))
    return {
        "run": run_idx,
        "inject": int(do_inject),
        "inject_offset_ms": offset_ms if do_inject else "",
        "publish_delay_ms": publish_delay_ms,
        "injection_window": window,
        "gateway_released": int(rel),
        "gate_blocked": int(bool(resp.get("gate_blocked"))),
        "stale_publish": stale,
        "potential_stale_publish": potential,
        "post_release_invalidation": int(do_inject and rel and window == "after_release"),
        "outcome": resp.get("outcome"),
        "reason": resp.get("reason"),
        "validation_to_gate_ms": resp.get("validation_to_gate_ms"),
        "validation_to_publish_ms": resp.get("validation_to_publish_ms"),
        "recheck_to_publish_ms": resp.get("recheck_to_publish_ms"),
        "t_validate_end_ms": resp.get("t_validate_end_ms"),
        "t_injection_start_ms": resp.get("t_injection_start_ms"),
        "t_injection_end_ms": resp.get("t_injection_end_ms"),
        "t_recheck_start_ms": resp.get("t_recheck_start_ms"),
        "t_recheck_end_ms": resp.get("t_recheck_end_ms"),
        "t_publish_end_ms": resp.get("t_publish_end_ms"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-per-delay", type=int, default=40, help="Trials per offset cell (injected + controls)")
    parser.add_argument("--offsets-ms", type=float, nargs="+", default=list(DEFAULT_OFFSETS_MS))
    parser.add_argument("--publish-delay-ms", type=float, default=50.0)
    parser.add_argument("--inject-fraction", type=float, default=0.75)
    parser.add_argument("--seed", type=int, default=42, help="Shuffles injected/control order within a cell")
    parser.add_argument("--out", default=str(RESULTS_DIR / "e10_toctou.csv"))
    args = parser.parse_args()
    rng = random.Random(args.seed)

    n_inject = round(args.runs_per_delay * args.inject_fraction)
    rows, run_idx = [], 0
    for offset in args.offsets_ms:
        plan = [True] * n_inject + [False] * (args.runs_per_delay - n_inject)
        rng.shuffle(plan)
        for do_inject in plan:
            rows.append(run_trial(offset, args.publish_delay_ms, run_idx, do_inject))
            run_idx += 1
    out = write_csv(Path(args.out), rows)

    inj = [r for r in rows if r["inject"]]
    by_offset = {}
    for offset in args.offsets_ms:
        cell = [r for r in inj if r["inject_offset_ms"] == offset]
        k = sum(r["stale_publish"] for r in cell)
        by_offset[str(offset)] = {
            "injected": len(cell),
            "windows": {w: sum(1 for r in cell if r["injection_window"] == w) for w in sorted({r["injection_window"] for r in cell})},
            "stale": k, "stale_ci95": wilson_ci(k, len(cell)),
            "potential_stale": sum(r["potential_stale_publish"] for r in cell),
            "post_release": sum(r["post_release_invalidation"] for r in cell),
        }
    residual = [r["recheck_to_publish_ms"] for r in rows if r["recheck_to_publish_ms"] is not None]
    print(json.dumps({
        "runs": len(rows),
        "by_offset_ms": by_offset,
        "controls_released": sum(r["gateway_released"] for r in rows if not r["inject"]),
        "residual_window_p50_p99_max_ms": [percentile(residual, 0.5), percentile(residual, 0.99), max(residual, default=0)],
        "out": str(out),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
