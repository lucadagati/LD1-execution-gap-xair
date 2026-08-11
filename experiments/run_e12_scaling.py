#!/usr/bin/env python3
"""E12: scaling matrix — concurrent producers and context snapshot size."""

from __future__ import annotations

import argparse
import csv
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


def _post(url: str, body: dict | None = None) -> dict:
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def intent_body() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": 5000,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "line_1"},
    }


def submit_one(mode: str, retries: int = 5) -> float:
    """Submit one intent; retry transient connection resets under high concurrency."""
    import urllib.error

    last_err: Exception | None = None
    t0 = time.perf_counter()
    for attempt in range(retries):
        try:
            _post(f"{ADAPTER}/intent?{urlencode({'mode': mode})}", intent_body())
            return (time.perf_counter() - t0) * 1000.0
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--producers", type=int, nargs="+", default=[1, 10, 50])
    parser.add_argument("--context-kb", type=int, nargs="+", default=[1, 64])
    parser.add_argument("--trials", type=int, default=200, help="Submissions per configuration")
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
                latencies: list[float] = []
                t_start = time.perf_counter()
                workers = max(1, n)
                # Submit exactly args.trials intents with up to `n` concurrent workers.
                remaining = args.trials
                while remaining > 0:
                    batch = min(workers, remaining)
                    with ThreadPoolExecutor(max_workers=workers) as ex:
                        futs = [ex.submit(submit_one, mode) for _ in range(batch)]
                        for fut in as_completed(futs):
                            latencies.append(fut.result())
                    remaining -= batch
                elapsed = time.perf_counter() - t_start
                rows.append({
                    "mode": mode,
                    "producers": n,
                    "max_workers": workers,
                    "context_kb": kb,
                    "trials": len(latencies),
                    "throughput_ips": len(latencies) / max(elapsed, 1e-6),
                    "e2e_p50_ms": percentile(latencies, 0.5),
                    "e2e_p99_ms": percentile(latencies, 0.99),
                })
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
