#!/usr/bin/env python3
"""E10-ABA: a change undone before the gate.

The gateway waits an induced delay before its check. Meanwhile the harness
pauses the line and resumes it again, so at the check the predicate holds but
it was false in between: the intent was not continuously admissible, and a
gate that only re-evaluates predicates cannot tell. Compared gates: read-set
version, predicate-truth version, predicates only (XAIR), and the external
policy engine (OPA). Valid controls (no flip) measure false revocations.
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
import uuid
from pathlib import Path

from common import RESULTS_DIR, adapter, now_iso, released, wilson_ci, write_csv, xair_context

RUN = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}
GATES = (("xair", "readset"), ("xair", "truth"), ("xair", "predicate"), ("opa", None), ("opa", "readset"),
         ("xair_atomic", "readset"), ("xair_atomic", "truth"))


def gate_key(mode: str, scope: str | None) -> str:
    """Scope column of a gate: the XAIR scope, ``opa``, or ``opa_<scope>`` for a versioned OPA gate."""
    if mode == "opa":
        return f"opa_{scope}" if scope else "opa"
    return scope


def classify(v_val, vp, vr, v_chk) -> str:
    """Store-order class of a flip trial: ``valid`` iff both writes fall in (t_v, check].

    A pause at or before the validation snapshot (the intent was then validated
    against the paused or resumed state) or a resume after the check places the flip
    outside the window, even when the intent was revoked at validation and never
    reached a check."""
    if vp is not None and v_val is not None and vp <= v_val:
        return "flip_outside_window"
    if vr is not None and v_chk is not None and vr > v_chk:
        return "flip_outside_window"
    if None in (v_val, vp, vr, v_chk):
        return "unknown"
    return "valid" if v_val < vp < vr <= v_chk else "flip_outside_window"


def trial(mode: str, scope: str | None, flip: bool, delay_ms: float, pause_ms: float, resume_ms: float) -> dict:
    adapter("context", RUN)
    intent = {"id": str(uuid.uuid4()), "source": "ai", "timestamp_decision": now_iso(), "freshness_window_ms": 5000,
              "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
              "payload": {"action_type": "RESUME", "target_entity": f"aba_{uuid.uuid4().hex[:8]}"}}
    versions: dict = {}

    def flipper():
        time.sleep(pause_ms / 1000.0)
        versions["pause"] = xair_context({"line": {"state": "PAUSED"}}).get("context_version")
        time.sleep(max(0.0, resume_ms - pause_ms) / 1000.0)
        versions["resume"] = xair_context({"line": {"state": "RUN"}}).get("context_version")

    th = threading.Thread(target=flipper, daemon=True) if flip else None
    if th:
        th.start()
    resp = adapter("intent", intent, mode=mode, version_scope=scope, publish_delay_ms=delay_ms)
    if th:
        th.join(timeout=2)
    v_val = resp.get("validation_context_version")
    v_chk = resp.get("commit_version") if mode == "xair_atomic" else resp.get("gate_read_version")
    vp, vr = versions.get("pause"), versions.get("resume")
    # A flip trial tests a history violation only if both writes fall between the
    # validation snapshot and the last check, in store order.
    protocol = "control" if not flip else classify(v_val, vp, vr, v_chk)
    return {"mode": mode, "scope": gate_key(mode, scope), "flip": int(flip), "gateway_released": int(released(resp)),
            "reason": resp.get("reason"), "e2e_latency_ms": resp.get("e2e_latency_ms"),
            "validation_version": v_val, "pause_version": vp, "resume_version": vr, "check_version": v_chk,
            "protocol": protocol}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=50, help="Flip trials per gate")
    ap.add_argument("--until-valid", action="store_true",
                    help="repeat flip trials until each gate has --runs trials whose flip fell inside the window")
    ap.add_argument("--max-attempts", type=int, default=4, help="with --until-valid: at most this many times --runs per gate")
    ap.add_argument("--controls", type=int, default=25, help="Valid trials per gate")
    ap.add_argument("--delay-ms", type=float, default=50.0)
    ap.add_argument("--pause-ms", type=float, default=10.0, help="Pause this long after submission")
    ap.add_argument("--resume-ms", type=float, default=25.0, help="Resume this long after submission (before the gate)")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--gates", nargs="+", default=[f"{m}/{gate_key(m, s)}" for m, s in GATES],
                    help="subset of mode/scope gates, e.g. xair/readset xair/truth")
    ap.add_argument("--out", default=str(RESULTS_DIR / "e10_aba.csv"))
    args = ap.parse_args()
    gates = [(m, s) for m, s in GATES if f"{m}/{gate_key(m, s)}" in args.gates]
    plan = [(m, s, True) for m, s in gates for _ in range(args.runs)] + [(m, s, False) for m, s in gates for _ in range(args.controls)]
    random.Random(args.seed).shuffle(plan)
    rows = [trial(m, s, f, args.delay_ms, args.pause_ms, args.resume_ms) for m, s, f in plan]
    if args.until_valid:
        for m, s in gates:
            key = gate_key(m, s)
            for _ in range(args.runs * (args.max_attempts - 1)):
                ok = sum(1 for r in rows if r["mode"] == m and r["scope"] == key and r["protocol"] == "valid")
                if ok >= args.runs:
                    break
                rows.append(trial(m, s, True, args.delay_ms, args.pause_ms, args.resume_ms))
    xair_context(RUN)
    write_csv(Path(args.out), rows)
    for m, s in gates:
        key = gate_key(m, s)
        f = [r for r in rows if r["mode"] == m and r["scope"] == key and r["flip"] and r["protocol"] != "flip_outside_window"]
        c = [r for r in rows if r["mode"] == m and r["scope"] == key and not r["flip"]]
        k = sum(r["gateway_released"] for r in f)
        print(json.dumps({"gate": f"{m}/{key}", "released_after_undone_change": f"{k}/{len(f)}", "ci95": wilson_ci(k, len(f)),
                          "controls_released": f"{sum(r['gateway_released'] for r in c)}/{len(c)}",
                          "flip_outside_window": sum(1 for r in rows if r["mode"] == m and r["scope"] == key and r["protocol"] == "flip_outside_window")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
