#!/usr/bin/env python3
"""E16-trace: valid intents under context churn replayed from real industrial telemetry.

A background writer replays the UCI hydraulic test-rig recording (see
``uci_hydraulic.py``) in real time as OPC UA-style notifications at a given
publishing interval, into the shared XAIR snapshot. Two kinds of valid
intents are submitted meanwhile, interleaved across cells and spread uniformly
over the whole replay with a random phase:

  discrete    reads only the line and gripper state (never written by the trace)
  continuous  additionally reads a 100 Hz pressure (``telemetry.PS1 < 250``,
              always true in the recording, whose maximum is about 192 bar)

Every revocation is a false positive. The gate uses one of three scopes:
``global`` (any update invalidates), ``readset`` (a value change on a path the
intent reads invalidates), or ``predicate`` (no version condition; the gate
only re-evaluates the predicates, which forgoes detection of changes undone
before the gate).
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import threading
import time
import uuid
from pathlib import Path

from common import RESULTS_DIR, XAIR, adapter, http_json, now_iso, percentile, released, wilson_ci, write_csv, xair_context
from uci_hydraulic import load_cycles, notifications

RUN = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}
SCOPES = ("global", "readset", "predicate")
INTERVALS_MS = (1000, 100, 10)
PRECONDITIONS = {
    "discrete": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
    "continuous": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}, {"expr": "telemetry.PS1 < 250"}],
}


class TraceWriter:
    """Posts recorded notifications to the XAIR snapshot at their recorded times (looping)."""

    def __init__(self, stream: list[tuple[float, dict]], period_s: float) -> None:
        self.stream, self.period_s = stream, period_s
        self._stop = threading.Event()
        self.writes = 0
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        t0 = time.monotonic()
        for loop in itertools.count():
            for t, patch in self.stream:
                due = t0 + loop * self.period_s + t
                delay = due - time.monotonic()
                if delay > 0 and self._stop.wait(delay):
                    return
                if self._stop.is_set():
                    return
                try:
                    http_json(f"{XAIR}/v1/context/snapshot", patch, timeout=5)
                    self.writes += 1
                except Exception:
                    pass

    def __enter__(self) -> "TraceWriter":
        self.t0 = time.monotonic()
        self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._t.join(timeout=10)
        self.elapsed = time.monotonic() - self.t0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100, help="Valid intents per (interval, scope, kind) cell")
    parser.add_argument("--intervals-ms", type=float, nargs="+", default=list(INTERVALS_MS))
    parser.add_argument("--first-cycle", type=int, default=0)
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--seed", type=int, default=16)
    parser.add_argument("--phase", choices=("random", "fixed"), default="random",
                        help="fixed: submit on the slot boundary (sensitivity check for phase locking)")
    parser.add_argument("--out", default=str(RESULTS_DIR / "e16_trace_churn.csv"))
    args = parser.parse_args()

    sensors, profile = load_cycles(args.first_cycle, args.cycles)
    span_s = args.cycles * 60
    rng = random.Random(args.seed)
    rows = []
    for interval in args.intervals_ms:
        stream = notifications(sensors, profile, interval)
        # One uninterrupted replay per interval; the trials of every (scope, kind)
        # cell are interleaved in random order and spread uniformly over the whole
        # recording, so all cells sample the same trace, cycle boundaries included.
        plan = [(scope, kind) for scope in SCOPES for kind in PRECONDITIONS for _ in range(args.runs)]
        rng.shuffle(plan)
        spacing = span_s / len(plan)
        adapter("context", RUN)
        with TraceWriter(stream, span_s) as writer:
            for n, (scope, kind) in enumerate(plan):
                # uniform random phase within each slot: a fixed spacing that is a multiple of
                # the publishing interval would lock every intent to the same notification phase
                phase = rng.random() if args.phase == "random" else 0.0
                delay = writer.t0 + (n + phase) * spacing - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                intent = {
                    "id": str(uuid.uuid4()), "source": "mes", "timestamp_decision": now_iso(),
                    "freshness_window_ms": 5000, "preconditions": PRECONDITIONS[kind],
                    "payload": {"action_type": "RESUME", "target_entity": f"e16t_{uuid.uuid4().hex[:8]}"},
                }
                t_trace = time.monotonic() - writer.t0
                resp = adapter("intent", intent, mode="xair", version_scope=scope)
                rows.append({
                    "publishing_interval_ms": interval, "version_scope": scope, "intent_kind": kind, "run": n,
                    "trace_time_s": round(t_trace, 3),
                    "gateway_released": int(released(resp)), "reason": resp.get("reason"),
                    "e2e_latency_ms": resp.get("e2e_latency_ms"),
                    "validation_to_gate_ms": resp.get("validation_to_gate_ms"),
                })
        rate = writer.writes / writer.elapsed if writer.elapsed else 0.0
        for r in rows:
            if r["publishing_interval_ms"] == interval:
                r["achieved_update_rate_hz"] = round(rate, 1)
        for scope, kind in itertools.product(SCOPES, PRECONDITIONS):
            cell = [r for r in rows if r["publishing_interval_ms"] == interval and r["version_scope"] == scope and r["intent_kind"] == kind]
            k = sum(1 - r["gateway_released"] for r in cell)
            print(json.dumps({"interval_ms": interval, "scope": scope, "kind": kind, "updates_hz": round(rate, 1),
                              "fpr": f"{k}/{len(cell)}", "ci95": [round(x, 3) for x in wilson_ci(k, len(cell))],
                              "e2e_p99_ms": round(percentile([r["e2e_latency_ms"] for r in cell], 0.99), 1)}), flush=True)
    xair_context(RUN)
    write_csv(Path(args.out), rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
