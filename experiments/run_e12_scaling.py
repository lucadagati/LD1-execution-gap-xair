#!/usr/bin/env python3
"""E12: scaling matrix — concurrent producers and context snapshot size.

Each cell is measured over several independent repetitions, each preceded by
a discarded warm-up phase, with every submitted intent addressed at a
distinct target entity so throughput/latency reflect validation and adapter
overhead rather than resource-conflict delays on a shared target.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "http://127.0.0.1:9092"
XAIR = "http://127.0.0.1:8080"
RESULTS = ROOT / "experiments" / "results" / "e12_scaling.csv"

_target_counter = itertools.count()


def _post(url: str, body: dict | None = None) -> dict:
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def intent_body() -> dict:
    target = f"line_{next(_target_counter)}"
    return {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": 5000,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": target},
    }


def submit_one(mode: str, retries: int = 5) -> tuple[float, bool]:
    """Submit one intent; retry transient connection resets under high concurrency.

    Returns (latency_ms, released) where `released` is False for a DELAY/REVOKE
    outcome (should not happen with distinct targets, but is scored honestly
    rather than assumed).
    """
    import urllib.error

    last_err: Exception | None = None
    t0 = time.perf_counter()
    for attempt in range(retries):
        try:
            resp = _post(f"{ADAPTER}/intent?{urlencode({'mode': mode})}", intent_body())
            latency_ms = (time.perf_counter() - t0) * 1000.0
            released = bool(resp.get("gateway_released", resp.get("outcome") == "EXECUTE"))
            return latency_ms, released
        except (urllib.error.URLError, ConnectionResetError, TimeoutError, OSError) as exc:
            last_err = exc
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError(f"submit_one failed after {retries} retries: {last_err}")


def percentile(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = max(int(len(s) * q) - 1, 0)
    return s[idx]


def median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def run_cell(mode: str, n: int, kb: int, trials: int, warmup: int) -> dict:
    workers = max(1, n)

    def submit_batch(count: int) -> tuple[list[float], int]:
        lat: list[float] = []
        released = 0
        remaining = count
        while remaining > 0:
            batch = min(workers, remaining)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(submit_one, mode) for _ in range(batch)]
                for fut in as_completed(futs):
                    ms, ok = fut.result()
                    lat.append(ms)
                    released += int(ok)
            remaining -= batch
        return lat, released

    if warmup > 0:
        submit_batch(warmup)  # discarded

    latencies, released = submit_batch(trials)
    return {"latencies": latencies, "released": released}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--producers", type=int, nargs="+", default=[1, 10, 50])
    parser.add_argument("--context-kb", type=int, nargs="+", default=[1, 64])
    parser.add_argument("--trials", type=int, default=100, help="Scored submissions per repetition")
    parser.add_argument("--warmup", type=int, default=20, help="Discarded submissions before each repetition")
    parser.add_argument("--repetitions", type=int, default=5, help="Independent repetitions per cell")
    parser.add_argument("--modes", nargs="+", default=["local_authoritative", "xair"])
    args = parser.parse_args()

    _post(f"{ADAPTER}/context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
    rows = []
    for mode in args.modes:
        for n in args.producers:
            for kb in args.context_kb:
                pad = "x" * max(0, kb * 1024 - 128)
                ctx = {"line": {"state": "RUN"}, "meta": {"pad": pad}}
                _post(f"{XAIR}/v1/context/snapshot", ctx)
                _post(f"{ADAPTER}/context", ctx)

                rep_throughput: list[float] = []
                rep_p50: list[float] = []
                rep_p99: list[float] = []
                for rep in range(args.repetitions):
                    t_start = time.perf_counter()
                    out = run_cell(mode, n, kb, args.trials, args.warmup)
                    elapsed = time.perf_counter() - t_start
                    lat = out["latencies"]
                    rep_throughput.append(len(lat) / max(elapsed, 1e-6))
                    rep_p50.append(percentile(lat, 0.5))
                    rep_p99.append(percentile(lat, 0.99))
                    rows.append({
                        "mode": mode,
                        "producers": n,
                        "max_workers": max(1, n),
                        "context_kb": kb,
                        "repetition": rep,
                        "trials": len(lat),
                        "released": out["released"],
                        "release_rate": out["released"] / max(len(lat), 1),
                        "throughput_ips": rep_throughput[-1],
                        "e2e_p50_ms": rep_p50[-1],
                        "e2e_p99_ms": rep_p99[-1],
                    })

                print(json.dumps({
                    "mode": mode, "producers": n, "context_kb": kb,
                    "median_throughput_ips": median(rep_throughput),
                    "median_p50_ms": median(rep_p50),
                    "median_p99_ms": median(rep_p99),
                }))

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
