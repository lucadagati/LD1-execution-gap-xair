#!/usr/bin/env python3
"""E27: a stop issued while an earlier RESUME on the same target is in flight.

The RESUME is validated first and its gate (optimistic) or commit (atomic) is delayed by
``--gate-delay-ms``; a STOP of the same target (class stop, never rechecked) is submitted a
controlled offset after the RESUME. Ground truth: the RESUME is stale if it is released after the
STOP was released (``t_middleware_wall_ms`` of the RESUME is later than the STOP's). Valid controls
(a RESUME alone) must still be released. With the stop fence off (XAIR_STOP_FENCE=0, the behaviour
before the fence) the recheck sees an unchanged line state and the RESUME follows the stop; with it
on, the stop advanced an epoch that the RESUME's recheck or commit observes.
Each (fence, mode) cell starts a fresh XAIR and gateway against one Redis.
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
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
XPORT, GPORT, WPORT = 18680, 19692, 19691
os.environ["XAIR_URL"] = f"http://127.0.0.1:{XPORT}"
os.environ["ADAPTER_URL"] = f"http://127.0.0.1:{GPORT}"
sys.path.insert(0, str(HERE))

from common import adapter, http_json, now_iso, write_csv  # noqa: E402

POLICY = {"RESUME": {"class": "start"}, "STOP": {"class": "stop"}}


def start(cmd, env=None):
    return subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, **(env or {})}, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def stop_proc(p):
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=5)
    except Exception:
        pass


def intent(action: str, target: str, clock_offset_ms: float = 0.0) -> dict:
    return {"id": str(uuid.uuid4()), "source": "ai", "timestamp_decision": now_iso(clock_offset_ms), "freshness_window_ms": 5000,
            "preconditions": [{"expr": "line.state == 'RUN'"}],
            "payload": {"action_type": action, "target_entity": target}}


PRODUCER_OFFSET_MS = 0.0   # producer clock minus XAIR clock (positive: the producer runs ahead)


def trial(mode: str, delay_ms: float, offset_ms: float | None, run: int) -> dict:
    target = f"e27_{uuid.uuid4().hex[:8]}"
    out: dict = {}

    def resume():
        out["resume"] = adapter("intent", intent("RESUME", target, PRODUCER_OFFSET_MS), mode=mode,
                                publish_delay_ms=delay_ms)

    th = threading.Thread(target=resume)
    th.start()
    if offset_ms is not None:
        time.sleep(offset_ms / 1000.0)
        out["stop"] = adapter("intent", intent("STOP", target), mode=mode)
    th.join(timeout=10)
    r, s = out.get("resume", {}), out.get("stop", {})
    released = int(bool(r.get("gateway_released")))
    t_r, t_s = r.get("t_middleware_wall_ms"), s.get("t_middleware_wall_ms")
    stale = int(offset_ms is not None and released and t_s is not None and t_r is not None and t_r > t_s)
    return {"mode": mode, "inject": int(offset_ms is not None), "stop_offset_ms": offset_ms if offset_ms is not None else "",
            "run": run, "resume_released": released, "resume_reason": r.get("reason"),
            "stop_released": int(bool(s.get("gateway_released"))) if s else "",
            "t_resume_ms": t_r if t_r is not None else "", "t_stop_ms": t_s if t_s is not None else "",
            "stale_after_stop": stale}


def trial_after(mode: str, delta_ms: float, run: int) -> dict:
    """A RESUME decided ``delta_ms`` after a STOP of its target: valid, it must be released."""
    target = f"e27_{uuid.uuid4().hex[:8]}"
    stop = adapter("intent", intent("STOP", target), mode=mode)
    time.sleep(delta_ms / 1000.0)
    r = adapter("intent", intent("RESUME", target, PRODUCER_OFFSET_MS), mode=mode)
    return {"mode": mode, "inject": 2, "stop_offset_ms": delta_ms, "run": run,
            "resume_released": int(bool(r.get("gateway_released"))), "resume_reason": r.get("reason"),
            "stop_released": int(bool(stop.get("gateway_released"))), "t_resume_ms": r.get("t_middleware_wall_ms") or "",
            "t_stop_ms": stop.get("t_middleware_wall_ms") or "", "stale_after_stop": 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=30, help="injected trials per (fence, mode, offset)")
    ap.add_argument("--controls", type=int, default=100)
    ap.add_argument("--gate-delay-ms", type=float, default=40.0)
    ap.add_argument("--offsets-ms", type=float, nargs="+", default=[0, 10, 20, 30, 36, 38, 40, 42, 44, 50, 60])
    ap.add_argument("--seed", type=int, default=27)
    ap.add_argument("--producer-offset-ms", type=float, default=0.0,
                    help="the producer's clock minus XAIR's: shifts every RESUME timestamp (E3-stop with a skewed producer)")
    ap.add_argument("--eps-ms", type=float, default=0.0, help="declared clock uncertainty of XAIR and the gateway")
    ap.add_argument("--fences", nargs="+", default=["0", "1"])
    ap.add_argument("--after-offsets-ms", type=float, nargs="*", default=[],
                    help="also submit a valid RESUME decided this long after a STOP (cost of the epsilon margin)")
    ap.add_argument("--after-runs", type=int, default=30)
    ap.add_argument("--out", default=str(HERE / "results" / "e27_stop_resume.csv"))
    args = ap.parse_args()
    global PRODUCER_OFFSET_MS
    PRODUCER_OFFSET_MS = args.producer_offset_ms
    os.environ["XAIR_CLOCK_UNCERTAINTY_MS"] = str(args.eps_ms)
    py = sys.executable
    rows = []
    for fence in args.fences:
        xair = start([py, "-m", "uvicorn", "xair.adapters.http_server:app", "--host", "127.0.0.1", "--port", str(XPORT),
                      "--log-level", "warning"],
                     {"REDIS_URL": os.environ.get("E27_REDIS_URL", "redis://127.0.0.1:6396/0"), "XAIR_STOP_FENCE": fence,
                      "PYTHONPATH": str(ROOT)})
        gw = None
        try:
            for _ in range(120):
                try:
                    http_json(f"{os.environ['XAIR_URL']}/v1/metrics")
                    break
                except Exception:
                    time.sleep(0.25)
            gw = start([py, "scripts/actuator_gateway.py", str(WPORT), str(GPORT)],
                       {"XAIR_URL": os.environ["XAIR_URL"], "PYTHONPATH": str(ROOT)})
            for _ in range(120):
                try:
                    import urllib.request
                    urllib.request.urlopen(f"{os.environ['ADAPTER_URL']}/health", timeout=1).read()
                    break
                except Exception:
                    time.sleep(0.25)
            http_json(f"{os.environ['XAIR_URL']}/v1/policy", POLICY, method="PUT")
            for mode in ("xair", "xair_atomic"):
                plan = [(o, i) for o in args.offsets_ms for i in range(args.runs)] + [(None, i) for i in range(args.controls)]
                plan += [(("after", d), i) for d in args.after_offsets_ms for i in range(args.after_runs)]
                random.Random(args.seed).shuffle(plan)
                for k, (off, _) in enumerate(plan):
                    http_json(f"{os.environ['XAIR_URL']}/v1/context/snapshot", {"line": {"state": "RUN"}})
                    row = (trial_after(mode, off[1], k) if isinstance(off, tuple)
                           else trial(mode, args.gate_delay_ms, off, k))
                    row["stop_fence"] = int(fence)
                    row["producer_offset_ms"], row["eps_ms"] = args.producer_offset_ms, args.eps_ms
                    rows.append(row)
                inj = [r for r in rows if r["stop_fence"] == int(fence) and r["mode"] == mode and r["inject"] == 1]
                ctl = [r for r in rows if r["stop_fence"] == int(fence) and r["mode"] == mode and not r["inject"]]
                print(json.dumps({"fence": int(fence), "mode": mode, "stale_after_stop": f"{sum(r['stale_after_stop'] for r in inj)}/{len(inj)}",
                                  "controls_released": f"{sum(r['resume_released'] for r in ctl)}/{len(ctl)}"}), flush=True)
        finally:
            if gw:
                stop_proc(gw)
            stop_proc(xair)
            time.sleep(0.5)
    write_csv(Path(args.out), rows)
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
