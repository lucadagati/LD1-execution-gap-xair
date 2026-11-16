#!/usr/bin/env python3
"""E16: valid intents under concurrent context churn, global vs read-set gate.

Every intent is valid at t_v, t_g and t_m (its predicates read only
``line.state`` and ``gripper.state``, which stay RUN/OPEN). A background
writer updates the shared snapshot at a target rate with one of two
patterns, neither of which invalidates the intent:

  unrelated     telemetry.temp_c changes value on every write
  related_same  line.state is rewritten with its current value (RUN)

Any revocation is therefore a false positive. The gate compares either the
global snapshot version (``version_scope=global``: any accepted update
invalidates) or the version of the intent's read-set
(``version_scope=readset``: only a value change on a path the intent reads
invalidates).
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

RUN = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}
SCOPES = ("global", "readset")
PATTERNS = ("unrelated", "related_same")
RATES_HZ = (0, 20, 100, 500)


class ChurnWriter:
    """Paced writers posting to the XAIR snapshot until stopped; counts achieved writes."""

    def __init__(self, pattern: str, rate_hz: float, threads: int = 4, seed: int = 0, poisson: bool = False) -> None:
        self.pattern, self.rate_hz, self.threads, self.poisson = pattern, rate_hz, threads, poisson
        self._stop = threading.Event()
        self._rng = random.Random(seed)
        self.writes = 0
        self._lock = threading.Lock()
        self._pool: list[threading.Thread] = []

    def _patch(self) -> dict:
        if self.pattern == "unrelated":
            return {"telemetry": {"temp_c": round(20 + 10 * self._rng.random(), 3)}}
        return {"line": {"state": "RUN"}}

    def _run(self, period: float, phase: float) -> None:
        rng = random.Random(self._rng.random())
        nxt = time.monotonic() + phase
        while not self._stop.is_set():
            delay = nxt - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
                if self._stop.is_set():
                    break
            try:
                http_json(f"{XAIR}/v1/context/snapshot", self._patch(), timeout=5)
                with self._lock:
                    self.writes += 1
            except Exception:
                pass
            # a periodic writer keeps a fixed phase against the closed loop of trials; exponential gaps
            # make the updates independent of the submissions, as the model of Eq. (fpr) assumes
            nxt += rng.expovariate(1.0 / period) if self.poisson else period

    def __enter__(self) -> "ChurnWriter":
        self.t0 = time.monotonic()
        if self.rate_hz > 0:
            period = self.threads / self.rate_hz
            for i in range(self.threads):
                t = threading.Thread(target=self._run, args=(period, i * period / self.threads), daemon=True)
                t.start()
                self._pool.append(t)
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        for t in self._pool:
            t.join(timeout=5)
        self.elapsed = time.monotonic() - self.t0


def run_cell(scope: str, pattern: str, rate: float, n: int, seed: int, poisson: bool = False, jitter: bool = False) -> tuple[list[dict], dict]:
    adapter("context", RUN)
    rows = []
    with ChurnWriter(pattern, rate, seed=seed, poisson=poisson) as churn:
        time.sleep(0.2 if rate else 0.0)  # let the writers reach their pace
        t0 = time.monotonic()
        gap_rng = random.Random(seed * 1009 + int(rate))
        for i in range(n):
            if jitter and rate:
                # random pause before each trial, uniform over one update period, so that the trial
                # does not keep a fixed phase against a periodic writer (closed-loop aliasing)
                time.sleep(gap_rng.random() / rate)
            intent = {
                "id": str(uuid.uuid4()), "source": "ai", "timestamp_decision": now_iso(),
                "freshness_window_ms": 5000,
                "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
                "payload": {"action_type": "RESUME", "target_entity": f"e16_{uuid.uuid4().hex[:8]}"},
            }
            # "opa": external policy-engine gate (no versions); otherwise an XAIR gate scope
            resp = adapter("intent", intent, mode="opa" if scope.startswith("opa") else "xair",
                           version_scope=("readset" if scope == "opa_readset" else None) if scope.startswith("opa") else scope)
            rows.append({
                "version_scope": scope, "pattern": pattern, "target_rate_hz": rate, "run": i,
                "gateway_released": int(released(resp)), "outcome": resp.get("outcome"),
                "reason": resp.get("reason"), "e2e_latency_ms": resp.get("e2e_latency_ms"),
                "validation_to_gate_ms": resp.get("validation_to_gate_ms"),
                # store reads (shared monotonic clock, ms relative to the request): the version the gate
                # compares was read at the first interval and at the second, whose distance is the
                # delta_g of the model (Eq. fpr)
                "val_read_lo_ms": resp.get("validation_store_io_lo_ms"), "val_read_hi_ms": resp.get("validation_store_io_hi_ms"),
                "gate_read_lo_ms": resp.get("store_io_lo_ms"), "gate_read_hi_ms": resp.get("store_io_hi_ms"),
            })
        elapsed = time.monotonic() - t0
    cell = {"achieved_rate_hz": churn.writes / churn.elapsed if churn.elapsed else 0.0,
            "goodput_ips": sum(r["gateway_released"] for r in rows) / elapsed}
    for r in rows:
        r.update(cell)
    return rows, cell


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100, help="Valid intents per (scope, pattern, rate) cell")
    parser.add_argument("--rates-hz", type=float, nargs="+", default=list(RATES_HZ))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--writer", choices=("periodic", "poisson"), default="periodic",
                        help="update process: strictly periodic (default, as in all earlier campaigns) or Poisson")
    parser.add_argument("--jitter", action="store_true",
                        help="random pause (uniform over one update period) before each trial: breaks phase locking with a periodic writer")
    parser.add_argument("--scopes", nargs="+", default=list(SCOPES), help="gate scopes (global readset truth predicate opa opa_readset)")
    parser.add_argument("--out", default=str(RESULTS_DIR / "e16_context_churn.csv"))
    args = parser.parse_args()

    rows, summary = [], {}
    for rate, pattern, scope in itertools.product(args.rates_hz, PATTERNS, args.scopes):
        cell_rows, cell = run_cell(scope, pattern, rate, args.runs, args.seed, poisson=args.writer == "poisson", jitter=args.jitter)
        rows += cell_rows
        k = sum(1 - r["gateway_released"] for r in cell_rows)
        summary[f"{scope}|{pattern}|{rate:g}"] = {
            "fpr": k / len(cell_rows), "fpr_ci95": wilson_ci(k, len(cell_rows)),
            "achieved_rate_hz": round(cell["achieved_rate_hz"], 1), "goodput_ips": round(cell["goodput_ips"], 1),
            "e2e_p99_ms": round(percentile([r["e2e_latency_ms"] for r in cell_rows], 0.99), 2),
        }
        print(json.dumps({f"{scope}|{pattern}|{rate:g}": summary[f"{scope}|{pattern}|{rate:g}"]}))
    xair_context(RUN)
    write_csv(Path(args.out), rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
