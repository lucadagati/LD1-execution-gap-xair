#!/usr/bin/env python3
"""E23: cost of predicate-truth versions as the number of registered predicates grows.

Against a dedicated Redis instance, ``n`` predicates ``p<i>.x < 100`` are
registered (one per path p<i>), and a writer updates the context in a loop. Each
update changes the value of a fraction ``f`` of the n paths (keeping every
predicate true, so no version advances) plus one unrelated field. Measured per
(n, f): update latency p50/p99 and throughput, snapshot read latency, the latency
of registering an already-known predicate together with the snapshot read (the
validation path), snapshot size, and Redis memory. A baseline with no registered
predicates gives the read-set cost. A final phase measures retirement: with a
short retention, the registry shrinks back to the predicates still in use.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import time
from pathlib import Path

from common import RESULTS_DIR, percentile, write_csv

PORT = int(os.environ.get("E23_REDIS_PORT", "6394"))
NAME = "xair-e23-redis"


def redis_up():
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", NAME, "-p", f"127.0.0.1:{PORT}:6379", "redis:7-alpine@sha256:520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7",
                    "redis-server", "--save", "", "--appendonly", "no"], check=True, capture_output=True)
    import redis
    for _ in range(100):
        try:
            redis.from_url(f"redis://127.0.0.1:{PORT}/0").ping()
            return
        except Exception:
            time.sleep(0.1)


def cell(n: int, f: float, updates: int, rng: random.Random, layout: str, register: bool = True) -> dict:
    import redis
    import xair.core.context_store as cs
    url = f"redis://127.0.0.1:{PORT}/0"
    client = redis.from_url(url)
    client.flushdb()
    cs.PREDICATE_LAYOUT = layout
    store = cs.RedisContextStore(url)
    store.update({f"p{i}": {"x": 1.0} for i in range(max(n, 1))} | {"line": {"state": "RUN"}})
    exprs = [f"p{i}.x < 100" for i in range(n)] if register else []
    for k in range(0, len(exprs), 500):              # registration in batches
        store.register_predicates(exprs[k:k + 500])
    touched = max(0, round(f * n))
    updates = min(updates, max(100, 30000 // max(1, touched)))  # at least 100 observations per cell
    lat = []
    t_all = time.perf_counter()
    for u in range(updates):
        idx = rng.sample(range(max(n, 1)), touched) if touched else []
        patch = {f"p{i}": {"x": rng.uniform(0, 99)} for i in idx}
        patch["telemetry"] = {"t": u}
        t0 = time.perf_counter()
        store.update(patch)
        lat.append((time.perf_counter() - t0) * 1000)
    elapsed = time.perf_counter() - t_all
    reads, regs = [], []
    for _ in range(200):
        t0 = time.perf_counter(); store.snapshot_full(); reads.append((time.perf_counter() - t0) * 1000)
        e = [exprs[rng.randrange(len(exprs))]] if exprs else []
        t0 = time.perf_counter(); store.register_and_snapshot(e); regs.append((time.perf_counter() - t0) * 1000)
    info = client.info("memory")
    pred_bytes = (client.memory_usage(cs.PRED_KEY) or 0) if layout == "hash" else 0
    return {"layout": layout if register else "none", "context_paths": n, "registered": len(exprs),
            "fraction_touched": f, "paths_changed_per_update": touched, "updates": updates,
            "update_p50_ms": percentile(lat, 0.5), "update_p99_ms": percentile(lat, 0.99),
            "update_max_ms": max(lat), "observations": len(lat),
            "updates_per_s": updates / elapsed,
            "read_p50_ms": percentile(reads, 0.5), "register_read_p50_ms": percentile(regs, 0.5),
            "snapshot_bytes": client.strlen(cs.SNAPSHOT_KEY), "predicate_hash_bytes": pred_bytes,
            "redis_used_memory_bytes": info["used_memory"]}


def retirement(n: int, layout: str) -> dict:
    import xair.core.context_store as cs
    cs.PREDICATE_LAYOUT = layout
    url = f"redis://127.0.0.1:{PORT}/0"
    import redis
    redis.from_url(url).flushdb()
    store = cs.RedisContextStore(url)
    store.update({"line": {"state": "RUN"}})
    old = cs.PREDICATE_RETENTION_S
    try:
        cs.PREDICATE_RETENTION_S = 2.0
        store.register_predicates([f"p{i}.x < 100" for i in range(n)])
        before = len(store.predicate_versions)
        time.sleep(1.2)
        store.register_predicates(["line.state == 'RUN'"])      # the predicate still in use
        time.sleep(1.2)
        store.update({"telemetry": {"t": 1}})
        if layout == "hash":
            store.gc_predicates()
            after = redis.from_url(url).hlen(cs.PRED_KEY)
        else:
            after = len(store.predicate_versions)
        return {"layout": layout, "registered_before": before if layout == "document" else n + 0, "registered_after": after}
    finally:
        cs.PREDICATE_RETENTION_S = old


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[0, 10, 100, 1000, 10000])
    ap.add_argument("--fractions", type=float, nargs="+", default=[0.0, 0.01, 0.1, 1.0])
    ap.add_argument("--updates", type=int, default=300)
    ap.add_argument("--layouts", nargs="+", default=["document", "hash"])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--out", default=str(RESULTS_DIR / "e23_truth_scaling.csv"))
    args = ap.parse_args()
    rng = random.Random(args.seed)
    redis_up()
    rows = []
    try:
        for r in range(args.reps):
          for n in args.sizes:
            plan = [("document", 0.0, False)] + [(lay, f, True) for lay in args.layouts for f in args.fractions] if n else [("document", 0.0, False)]
            for lay, f, reg in plan:
                row = {"rep": r, **cell(n, f, args.updates, rng, lay, reg)}
                print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()}), flush=True)
                rows.append(row)
        ret = [retirement(1000, lay) for lay in args.layouts]
        print(json.dumps({"retirement": ret}))
        write_csv(Path(args.out), rows)
        write_csv(Path(args.out).with_name("e23_retirement.csv"), ret)
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
