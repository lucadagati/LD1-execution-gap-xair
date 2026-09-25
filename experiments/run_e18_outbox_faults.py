#!/usr/bin/env python3
"""E18: fault model of the committed actuation log and its consumer.

Runs against the context store directly (Redis when XAIR_E18_REDIS_URL or
REDIS_URL is set, else in memory). Every scenario commits ``n`` authorizations
through the atomic commit and checks, against the log, what the idempotent
actuator finally did:

  gateway_crash_after_commit  the gateway dies after t_c, before any middleware
                              call; the consumer still delivers every command;
  duplicate_commit            the gateway retries each commit (lost reply); the
                              second attempt is refused, one record per intent;
  consumer_crash_after_apply  the consumer crashes after an effect but before
                              its offset moves, then restarts; the replayed
                              record is suppressed by the actuator;
  consumer_restarts           the consumer restarts between batches while
                              commits continue; nothing is lost;
  concurrent_commits          eight threads commit on one target; effects follow
                              the commit order and commit versions never decrease;
  post_commit_invalidation    the read-set changes after the commit and before
                              the consumer applies; each record is flagged, and
                              withheld when the consumer rechecks at apply.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from pathlib import Path

from common import RESULTS_DIR, write_csv

from xair.adapters.actuation_consumer import ActuationConsumer, ConsumerCrash, IdempotentActuator
from xair.core.context_store import ACTUATION_LOG_KEY, COMMITTED_KEY, SNAPSHOT_KEY, RedisContextStore
from xair.core.versioning import read_set_version

PATHS = ["line.state"]


def fresh_store(url: str) -> RedisContextStore:
    if url:
        import redis
        client = redis.from_url(url)
        client.delete(SNAPSHOT_KEY, ACTUATION_LOG_KEY, COMMITTED_KEY, *client.keys("xair:consumer:*"))
    store = RedisContextStore(url)
    store.update({"line": {"state": "RUN"}})
    return store


def commit(store: RedisContextStore, intent_id: str) -> dict:
    v = read_set_version(store.snapshot_full()[2], PATHS)
    return store.commit_authorization(intent_id, PATHS, v, {"read_set": PATHS, "read_set_version": v,
                                                            "target_entity": "line_1"}, time.time() * 1000.0, None)


def scenario(name: str, url: str, n: int, rng: random.Random) -> dict:
    store, act = fresh_store(url), IdempotentActuator()
    ids = [f"{name}-{i}" for i in range(n)]
    extra: dict = {}
    if name == "gateway_crash_after_commit":
        for i in ids:
            commit(store, i)          # ... and the gateway never calls middleware
        ActuationConsumer(store, act).poll()
    elif name == "duplicate_commit":
        dup = [commit(store, i)["status"] for i in ids for _ in range(2)]
        extra["refused_duplicates"] = dup.count("duplicate")
        ActuationConsumer(store, act).poll()
    elif name == "consumer_crash_after_apply":
        for i in ids:
            commit(store, i)
        crash_at = rng.randrange(n)

        def hook(pos, _rec, state={"done": False}):
            if pos == crash_at and not state["done"]:
                state["done"] = True
                raise ConsumerCrash()
        try:
            ActuationConsumer(store, act, after_apply_hook=hook).poll()
        except ConsumerCrash:
            pass
        ActuationConsumer(store, act).poll()
        extra["crash_position"] = crash_at
    elif name == "consumer_restarts":
        for k, i in enumerate(ids):
            commit(store, i)
            if rng.random() < 0.2:
                ActuationConsumer(store, act).poll()   # a fresh consumer process each time
        ActuationConsumer(store, act).poll()
    elif name == "concurrent_commits":
        chunks = [ids[k::8] for k in range(8)]
        threads = [threading.Thread(target=lambda c=c: [commit(store, i) for i in c]) for c in chunks]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ActuationConsumer(store, act).poll()
        log = store.actuation_log()
        extra["effects_in_commit_order"] = int(act.effects == [r["intent_id"] for r in log])
        extra["commit_versions_monotone"] = int(all(a["commit_version"] <= b["commit_version"] for a, b in zip(log, log[1:])))
    elif name == "post_commit_invalidation":
        for i in ids:
            commit(store, i)
        store.update({"line": {"state": "PAUSED"}})
        c = ActuationConsumer(store, act, recheck_at_apply=True)
        c.poll()
        extra["flagged"] = len(c.invalidated_in_flight)
        extra["withheld"] = len(c.withheld)
    log_ids = [r["intent_id"] for r in store.actuation_log()]
    return {
        "scenario": name, "backend": "redis" if url else "memory", "n": n,
        "log_records": len(log_ids), "distinct_logged": len(set(log_ids)),
        "effects": len(act.effects), "distinct_effects": len(set(act.effects)),
        "duplicate_effects": len(act.effects) - len(set(act.effects)),
        "lost": len(set(ids) - set(act.effects)) if name != "post_commit_invalidation" else "",
        "suppressed_replays": act.suppressed_duplicates, **extra,
    }


SCENARIOS = ("gateway_crash_after_commit", "duplicate_commit", "consumer_crash_after_apply",
             "consumer_restarts", "concurrent_commits", "post_commit_invalidation")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--seed", type=int, default=18)
    ap.add_argument("--out", default=str(RESULTS_DIR / "e18_outbox_faults.csv"))
    args = ap.parse_args()
    url = os.environ.get("XAIR_E18_REDIS_URL", os.environ.get("REDIS_URL", ""))
    rng = random.Random(args.seed)
    rows = []
    for rep in range(args.reps):
        for name in SCENARIOS:
            rows.append({"rep": rep, **scenario(name, url, args.n, rng)})
    write_csv(Path(args.out), rows)
    for name in SCENARIOS:
        cell = [r for r in rows if r["scenario"] == name]
        print(json.dumps({"scenario": name, "reps": len(cell),
                          "effects": sum(r["effects"] for r in cell), "duplicate_effects": sum(r["duplicate_effects"] for r in cell),
                          "lost": sum(r["lost"] or 0 for r in cell)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
