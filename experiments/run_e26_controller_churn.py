#!/usr/bin/env python3
"""E26: cost of the version-conditional command under churn of controller-owned state.

The controller (scripts/cell_controller_multi.py) owns the line state and ``--fast-vars``
fast variables that change at a given rate each. The intent reads only the line state,
which stays RUN, so every refusal of a valid command is a false positive. The version
that the conditional command compares has scope ``global`` (any controller change
advances it) or ``line`` (only a change of the line state does). Each (scope, rate) cell
starts a fresh controller and gateway against a shared XAIR. A second block injects a
pause and a resume 3 ms apart (A-B-A) and counts the commands that executed after a
change that was undone (history violations), for both scopes under churn.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
XPORT, GPORT, WPORT, CPORT = 18580, 19592, 19591, 14840
os.environ.setdefault("XAIR_URL", f"http://127.0.0.1:{XPORT}")
os.environ.setdefault("ADAPTER_URL", f"http://127.0.0.1:{GPORT}")
os.environ["CELL_URL"] = f"opc.tcp://127.0.0.1:{CPORT}/cell/"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))

from common import adapter, now_iso, released, wilson_ci, write_csv, xair_context  # noqa: E402
import run_e21_cell as e21  # noqa: E402


def start(cmd: list[str], env: dict | None = None) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, **(env or {})}, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def stop(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=5)
    except Exception:
        pass


def wait_line_run(timeout_s: float = 3.0) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        line = (xair_context().get("context") or {}).get("line", {})
        if line.get("state") == "RUN" and line.get("version") is not None:
            return True
        time.sleep(0.005)
    return False


def valid_trial(scope: str, rate: float, run: int, mode: str) -> dict:
    mirrored = wait_line_run()
    intent = {
        "id": str(uuid.uuid4()), "source": "ai", "timestamp_decision": now_iso(), "freshness_window_ms": 5000,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "line.version >= 0"}],
        "payload": {"action_type": "RESUME", "target_entity": f"e26_{uuid.uuid4().hex[:8]}"},
    }
    resp = adapter("intent", intent, mode=mode, actuator="plc_conditional")
    res = resp.get("cell_result") or ""
    return {"block": "valid", "scope": scope, "rate_hz_per_var": rate, "run": run, "mode": mode, "mirrored": int(mirrored),
            "gateway_released": int(released(resp)), "reason": resp.get("reason"), "cell_result": res,
            "executed": int(res.startswith("executed")), "refused_by_controller": int(res.startswith("refused")),
            "e2e_latency_ms": resp.get("e2e_latency_ms"), "validation_to_gate_ms": resp.get("validation_to_gate_ms"),
            "history_violation": "", "stale_effect": ""}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=300, help="valid commands per (scope, rate, mode)")
    ap.add_argument("--aba-runs", type=int, default=100, help="A-B-A injections per (scope, rate, mode)")
    ap.add_argument("--rates-hz", type=float, nargs="+", default=[0, 20, 50, 100])
    ap.add_argument("--scopes", nargs="+", default=["global", "line"])
    ap.add_argument("--modes", nargs="+", default=["xair", "xair_atomic"])
    ap.add_argument("--fast-vars", type=int, default=3)
    ap.add_argument("--seed", type=int, default=26)
    ap.add_argument("--out", default=str(HERE / "results" / "e26_controller_churn.csv"))
    args = ap.parse_args()
    py = sys.executable
    rows: list[dict] = []
    stats: list[dict] = []
    for scope in args.scopes:
        for rate in args.rates_hz:
            ctl = start([py, "scripts/cell_controller_multi.py", "--port", str(CPORT), "--xair", os.environ["XAIR_URL"],
                         "--version-scope", scope, "--fast-vars", str(args.fast_vars), "--fast-rate-hz", str(rate)],
                        {"PYTHONPATH": f"{ROOT}/scripts"})
            gw = start([py, "scripts/actuator_gateway.py", str(WPORT), str(GPORT)],
                       {"XAIR_URL": os.environ["XAIR_URL"], "PYTHONPATH": str(ROOT)})
            try:
                for _ in range(120):
                    try:
                        cell = e21.Cell(os.environ["CELL_URL"])
                        break
                    except Exception:
                        time.sleep(0.25)
                else:
                    raise RuntimeError("controller did not start")
                for _ in range(120):
                    try:
                        import urllib.request
                        urllib.request.urlopen(f"{os.environ['ADAPTER_URL']}/health", timeout=1).read()
                        break
                    except Exception:
                        time.sleep(0.25)
                plan = [("valid", m, i) for m in args.modes for i in range(args.runs)]
                if scope == "line" or True:
                    plan += [("aba", m, i) for m in args.modes for i in range(args.aba_runs)]
                random.Random(args.seed).shuffle(plan)
                cell.set_state("RUN")
                for k, (blk, m, i) in enumerate(plan):
                    if blk == "valid":
                        rows.append(valid_trial(scope, rate, k, m))
                    else:
                        off = random.Random(args.seed + k).choice([0, 2, 4, 6, 8, 10, 12])
                        r = e21.trial(cell, m, "plc_conditional", off, k, aba_gap_ms=3.0)
                        rows.append({"block": "aba", "scope": scope, "rate_hz_per_var": rate, "run": k, "mode": m,
                                     "mirrored": r["mirrored"], "gateway_released": r["gateway_released"],
                                     "reason": r["reason"], "cell_result": r["cell_result"], "executed": r["executed"],
                                     "refused_by_controller": r["refused_by_controller"], "e2e_latency_ms": r["e2e_latency_ms"],
                                     "validation_to_gate_ms": "", "history_violation": r["history_violation"],
                                     "stale_effect": r["stale_effect"]})
                s = cell.obj.call_method(f"{cell.ns}:GetScanStats")
                stats.append({"scope": scope, "rate_hz_per_var": rate, "scan_mean_p99_max_us": s})
                v = [r for r in rows if r["scope"] == scope and r["rate_hz_per_var"] == rate and r["block"] == "valid"]
                rel = [r for r in v if r["gateway_released"]]
                fr_c = sum(r["refused_by_controller"] for r in rel)
                print(json.dumps({"scope": scope, "rate": rate, "valid": len(v), "gateway_released": len(rel),
                                  "controller_refused": fr_c, "scan_us": s}), flush=True)
                try:
                    cell.client.disconnect()
                except Exception:
                    pass
            finally:
                stop(gw)
                stop(ctl)
                time.sleep(0.5)
    write_csv(Path(args.out), rows)
    Path(args.out).with_suffix(".scan.json").write_text(json.dumps(stats, indent=1))
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
