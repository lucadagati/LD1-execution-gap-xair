#!/usr/bin/env python3
"""E28: predicate-truth versions with many registered predicates under update churn.

A dedicated Redis holds ``n`` registered predicates (``p<i>.x < 100``, all true, as if registered by
other intents in flight). A background writer posts context updates through XAIR at a target rate;
each update changes the value of ``--touch`` of the paths (no predicate flips), as an OPC UA
subscription over many nodes would. Meanwhile intents with the truth version are submitted through
the gateway, optimistic and atomic: valid controls (false revocations) and pause-resume flips
inside the gate delay (an undone change; a stale release is a flip released although both writes
fell between validation and the check in store order). The writer's update latency and achieved
rate show the store lag; a flip that lands after the check because the store was slow is counted
as outside the window. One cell per (layout, n, rate); XAIR and gateway restart per cell.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
XPORT, GPORT, WPORT, RPORT = 18780, 19792, 19791, 6398
REDIS_URL = f"redis://127.0.0.1:{RPORT}/0"
os.environ["XAIR_URL"] = f"http://127.0.0.1:{XPORT}"
os.environ["ADAPTER_URL"] = f"http://127.0.0.1:{GPORT}"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from common import http_json, percentile, write_csv, wilson_ci  # noqa: E402
import run_e10_aba as aba  # noqa: E402


def start(cmd, env=None):
    return subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, **(env or {})}, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def stop(p):
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=5)
    except Exception:
        pass


class Writer:
    """Paced updates through XAIR; records each update's latency and the count achieved."""

    def __init__(self, n: int, touch: float, rate_hz: float, seed: int) -> None:
        self.n, self.touched, self.rate = n, max(1, round(touch * n)), rate_hz
        self.rng = random.Random(seed)
        self.lat: list[float] = []
        self.stop_ev = threading.Event()
        self.th = threading.Thread(target=self.run, daemon=True)
        self.t0 = self.t1 = 0.0

    def run(self) -> None:
        period, nxt = 1.0 / self.rate, time.monotonic()
        url = f"{os.environ['XAIR_URL']}/v1/context/snapshot"
        while not self.stop_ev.is_set():
            idx = self.rng.sample(range(self.n), self.touched)
            patch = {f"p{i}": {"x": self.rng.uniform(0, 99)} for i in idx}
            t = time.perf_counter()
            try:
                http_json(url, patch)
            except Exception:
                pass
            self.lat.append((time.perf_counter() - t) * 1000.0)
            nxt += period
            self.stop_ev.wait(max(0.0, nxt - time.monotonic()))

    def begin(self) -> None:
        self.t0 = time.monotonic()
        self.th.start()

    def end(self) -> dict:
        self.stop_ev.set()
        self.th.join(timeout=10)
        el = max(1e-9, time.monotonic() - self.t0)
        return {"updates": len(self.lat), "achieved_hz": len(self.lat) / el,
                "update_p50_ms": percentile(self.lat, 0.5) if self.lat else None,
                "update_p99_ms": percentile(self.lat, 0.99) if self.lat else None}


def cell(layout: str, n: int, rate: float, args) -> tuple[list[dict], dict]:
    import redis
    redis.from_url(REDIS_URL).flushdb()
    os.environ["XAIR_PREDICATE_LAYOUT"] = layout
    import xair.core.context_store as cs
    cs.PREDICATE_LAYOUT = layout
    store = cs.RedisContextStore(REDIS_URL)
    store.update({f"p{i}": {"x": 1.0} for i in range(n)} | {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
    exprs = [f"p{i}.x < 100" for i in range(n)]
    for k in range(0, len(exprs), 500):
        store.register_predicates(exprs[k:k + 500])
    xair = start([sys.executable, "-m", "uvicorn", "xair.adapters.http_server:app", "--host", "127.0.0.1", "--port", str(XPORT),
                  "--log-level", "warning"], {"REDIS_URL": REDIS_URL, "XAIR_PREDICATE_LAYOUT": layout, "PYTHONPATH": str(ROOT)})
    gw = None
    rows: list[dict] = []
    try:
        for _ in range(240):
            try:
                http_json(f"{os.environ['XAIR_URL']}/v1/metrics")
                break
            except Exception:
                time.sleep(0.25)
        gw = start([sys.executable, "scripts/actuator_gateway.py", str(WPORT), str(GPORT)],
                   {"XAIR_URL": os.environ["XAIR_URL"], "PYTHONPATH": str(ROOT)})
        for _ in range(240):
            try:
                import urllib.request
                urllib.request.urlopen(f"{os.environ['ADAPTER_URL']}/health", timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        writer = Writer(n, args.touch, rate, args.seed) if rate > 0 else None
        if writer:
            writer.begin()
        gates = (("xair", "truth"), ("xair_atomic", "truth"))
        plan = [(m, s, True) for m, s in gates for _ in range(args.runs)] + [(m, s, False) for m, s in gates for _ in range(args.controls)]
        random.Random(args.seed).shuffle(plan)
        for m, s, flip in plan:
            r = aba.trial(m, s, flip, args.delay_ms, args.pause_ms, args.resume_ms)
            r.update({"layout": layout, "registered": n, "rate_hz": rate})
            rows.append(r)
        wstats = writer.end() if writer else {"updates": 0, "achieved_hz": 0.0, "update_p50_ms": None, "update_p99_ms": None}
    finally:
        if gw:
            stop(gw)
        stop(xair)
        time.sleep(0.5)
    return rows, {"layout": layout, "registered": n, "rate_hz": rate, **wstats}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layouts", nargs="+", default=["document", "hash"])
    ap.add_argument("--n", type=int, nargs="+", default=[10, 1000, 10000])
    ap.add_argument("--rates-hz", type=float, nargs="+", default=[20, 100])
    ap.add_argument("--touch", type=float, default=0.1, help="fraction of the registered paths changed by each update")
    ap.add_argument("--runs", type=int, default=40, help="flip trials per gate")
    ap.add_argument("--controls", type=int, default=60)
    ap.add_argument("--delay-ms", type=float, default=50.0)
    ap.add_argument("--pause-ms", type=float, default=10.0)
    ap.add_argument("--resume-ms", type=float, default=25.0)
    ap.add_argument("--seed", type=int, default=28)
    ap.add_argument("--out", default=str(HERE / "results" / "e28_truth_scale.csv"))
    args = ap.parse_args()
    subprocess.run(["docker", "rm", "-f", "xair-e28-redis"], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--rm", "--name", "xair-e28-redis", "-p", f"127.0.0.1:{RPORT}:6379", "redis:7-alpine",
                    "redis-server", "--save", "", "--appendonly", "no"], check=True, capture_output=True)
    time.sleep(1.5)
    rows, stats = [], []
    try:
        for layout in args.layouts:
            for n in args.n:
                for rate in args.rates_hz:
                    r, s = cell(layout, n, rate, args)
                    rows += r
                    stats.append(s)
                    f = [x for x in r if x["flip"] and x["protocol"] == "valid"]
                    c = [x for x in r if not x["flip"]]
                    print(json.dumps({**s, "flips_in_window": len(f), "released_after_flip": sum(x["gateway_released"] for x in f),
                                      "flips_outside_window": sum(1 for x in r if x["flip"] and x["protocol"] != "valid"),
                                      "controls_revoked": sum(1 - x["gateway_released"] for x in c), "controls": len(c)}), flush=True)
    finally:
        subprocess.run(["docker", "rm", "-f", "xair-e28-redis"], capture_output=True)
    write_csv(Path(args.out), rows)
    Path(args.out).with_suffix(".writer.json").write_text(json.dumps(stats, indent=1))
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
