#!/usr/bin/env python3
"""E12: closed-loop scaling over producer concurrency and snapshot size.

``N`` producers each submit intents back-to-back from a persistent worker
pool (no per-batch barrier) until the repetition's trial budget is spent.
Every intent names a distinct target so throughput reflects validation and
gateway overhead, not resource-conflict delays. Each repetition is preceded
by a discarded warm-up.
"""

from __future__ import annotations

import argparse
import itertools
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import RESULTS_DIR, URLError, adapter, now_iso, percentile, released, write_csv, xair_context

_target_counter = itertools.count()
_counter_lock = threading.Lock()


def intent_body() -> dict:
    with _counter_lock:
        target = f"line_{next(_target_counter)}"
    return {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": now_iso(),
        "freshness_window_ms": 5000,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": target},
    }


def submit_one(mode: str) -> tuple[float, bool, int]:
    """Return (latency_ms, released, connection_retries); retries are counted, not hidden."""
    t0 = time.perf_counter()
    for attempt in range(5):
        try:
            resp = adapter("intent", intent_body(), timeout=60, mode=mode)
            return (time.perf_counter() - t0) * 1000.0, released(resp), attempt
        except (URLError, ConnectionResetError, TimeoutError, OSError):
            time.sleep(0.05 * (attempt + 1))
    return (time.perf_counter() - t0) * 1000.0, False, 5


def run_closed_loop(mode: str, producers: int, count: int) -> tuple[list[float], int, int, float]:
    budget = itertools.count()
    lat: list[float] = []
    rel = retries = 0
    lock = threading.Lock()

    def producer() -> None:
        nonlocal rel, retries
        while next(budget) < count:
            ms, ok, r = submit_one(mode)
            with lock:
                lat.append(ms)
                rel += int(ok)
                retries += r

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=producers) as ex:
        for f in [ex.submit(producer) for _ in range(producers)]:
            f.result()
    return lat, rel, retries, time.perf_counter() - t0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--producers", type=int, nargs="+", default=[1, 10, 50])
    parser.add_argument("--context-kb", type=int, nargs="+", default=[1, 64])
    parser.add_argument("--trials", type=int, default=100, help="Scored submissions per repetition")
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--modes", nargs="+", default=["local_authoritative", "xair"])
    parser.add_argument("--out", default=str(RESULTS_DIR / "e12_scaling.csv"))
    args = parser.parse_args()

    rows = []
    for mode in args.modes:
        for n in args.producers:
            for kb in args.context_kb:
                ctx = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}, "meta": {"pad": "x" * max(0, kb * 1024 - 128)}}
                adapter("context", ctx)
                xair_context(ctx)
                for rep in range(args.repetitions):
                    run_closed_loop(mode, n, args.warmup)
                    lat, rel, retries, elapsed = run_closed_loop(mode, n, args.trials)
                    rows.append({
                        "mode": mode, "producers": n, "context_kb": kb, "repetition": rep,
                        "trials": len(lat), "released": rel, "release_rate": rel / max(len(lat), 1),
                        "connection_retries": retries,
                        "throughput_ips": len(lat) / max(elapsed, 1e-6),
                        "e2e_p50_ms": percentile(lat, 0.50), "e2e_p99_ms": percentile(lat, 0.99),
                    })
                last = rows[-args.repetitions:]
                print(json.dumps({"mode": mode, "producers": n, "kb": kb,
                                  "throughput": [round(r["throughput_ips"]) for r in last],
                                  "p99": [round(r["e2e_p99_ms"], 1) for r in last]}))
    write_csv(Path(args.out), rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
