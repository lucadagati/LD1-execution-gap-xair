#!/usr/bin/env python3
"""E18b: the actuation log under real process and store crashes.

A dedicated Redis container runs with an append-only file fsynced on every
write. A committer process commits ``n`` authorizations (in intent order) and
a consumer process applies them to an actuator whose idempotency record lives
in SQLite with synchronous commits. During the run the harness kills the
committer and the consumer with SIGKILL at random instants and restarts them,
and kills and restarts the Redis container itself. A restarted committer
starts again from the first intent, so every earlier commit is retried and
must be refused as a duplicate. At the end the log and the actuator must hold
every intent exactly once.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from common import ROOT, RESULTS_DIR, write_csv

PY = sys.executable
PORT = int(os.environ.get("E18B_REDIS_PORT", "6391"))
NAME = "xair-e18b-redis"

COMMITTER = r'''
import sys, time
from xair.core.context_store import RedisContextStore
from xair.core.versioning import read_set_version
url, n = sys.argv[1], int(sys.argv[2])
store = RedisContextStore(url)
i = 0
while i < n:
    try:
        v = read_set_version(store.snapshot_full()[2], ["line.state"])
        res = store.commit_authorization(f"d-{i}", ["line.state"], v, {"read_set": ["line.state"], "read_set_version": v},
                                         time.time() * 1000.0, None)
        if res["status"] in ("committed", "duplicate"):
            i += 1
            if res["status"] == "committed":
                time.sleep(0.005)  # paced, so that the harness can crash processes mid-run
        else:
            time.sleep(0.01)
    except Exception:
        time.sleep(0.05)
print("done", flush=True)
'''


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def start_redis(data_dir: str) -> None:
    docker("rm", "-f", NAME, check=False)
    docker("run", "-d", "--name", NAME, "-p", f"127.0.0.1:{PORT}:6379", "-v", f"{data_dir}:/data", "redis:7-alpine@sha256:520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7",
           "redis-server", "--appendonly", "yes", "--appendfsync", "always", "--save", "")
    wait_redis()


def wait_redis(timeout=20.0) -> None:
    import redis
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        try:
            redis.from_url(f"redis://127.0.0.1:{PORT}/0").ping()
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("redis did not come back")


def spawn(args, env):
    return subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


def run_once(rep: int, n: int, rng: random.Random) -> dict:
    tmp = tempfile.mkdtemp(prefix="e18b_")
    os.chmod(tmp, 0o777)
    start_redis(tmp)
    url = f"redis://127.0.0.1:{PORT}/0"
    import redis
    redis.from_url(url).set("xair:snapshot", json.dumps({"context": {"line": {"state": "RUN"}}, "version": 1,
                                                          "path_versions": {"line.state": 1}, "predicate_versions": {}}))
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    db = os.path.join(tmp, "actuator.sqlite")
    committer_cmd = [PY, "-c", COMMITTER, url, str(n)]
    consumer_cmd = [PY, "-m", "xair.adapters.actuation_consumer", "--redis", url, "--db", db]
    committer, consumer = spawn(committer_cmd, env), spawn(consumer_cmd, env)
    kills = {"committer": 0, "consumer": 0, "redis": 0}
    t_start = time.monotonic()
    redis_kill_at = sorted(rng.uniform(1.0, n * 0.005 * 0.8) for _ in range(2))
    t_end = t_start + 600
    while time.monotonic() < t_end:
        time.sleep(rng.uniform(0.2, 0.8))
        if committer.poll() is None and rng.random() < 0.25:
            committer.send_signal(signal.SIGKILL); committer.wait(); kills["committer"] += 1
            committer = spawn(committer_cmd, env)
        if rng.random() < 0.6:
            consumer.send_signal(signal.SIGKILL); consumer.wait(); kills["consumer"] += 1
            consumer = spawn(consumer_cmd, env)
        if redis_kill_at and time.monotonic() - t_start >= redis_kill_at[0]:
            docker("kill", "-s", "KILL", NAME); docker("start", NAME); wait_redis(); kills["redis"] += 1
            redis_kill_at.pop(0)
        if committer.poll() is not None:
            break
    committer.wait(timeout=120)
    # let the consumer drain the log, then stop it
    client = redis.from_url(url)
    for _ in range(600):
        if int(client.get("xair:consumer:actuator:offset") or 0) >= client.llen("xair:actuations"):
            break
        time.sleep(0.05)
    consumer.send_signal(signal.SIGKILL); consumer.wait()
    log = [json.loads(x) for x in client.lrange("xair:actuations", 0, -1)]
    ids = [r["intent_id"] for r in log]
    con = sqlite3.connect(db)
    done = [r[0] for r in con.execute("SELECT intent_id FROM done")]
    attempts = con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    docker("rm", "-f", NAME, check=False)
    expected = {f"d-{i}" for i in range(n)}
    return {"rep": rep, "n": n, **{f"kills_{k}": v for k, v in kills.items()},
            "log_records": len(ids), "distinct_logged": len(set(ids)), "missing_in_log": len(expected - set(ids)),
            "effects": len(done), "missing_effects": len(expected - set(done)),
            "extra_effects": len(set(done) - expected), "apply_attempts": attempts,
            "suppressed_replays": attempts - len(done)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--seed", type=int, default=182)
    ap.add_argument("--out", default=str(RESULTS_DIR / "e18b_durability.csv"))
    args = ap.parse_args()
    rng = random.Random(args.seed)
    rows = []
    for rep in range(args.reps):
        row = run_once(rep, args.n, rng)
        print(json.dumps(row), flush=True)
        rows.append(row)
    write_csv(Path(args.out), rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
