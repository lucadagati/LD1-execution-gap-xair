#!/usr/bin/env python3
"""E21: release through a cell controller that owns the line state.

The controller (scripts/cell_controller.py, OPC UA) holds the line state and a
version of it and replicates every change to XAIR asynchronously, so XAIR's
copy lags the controller. An operator pause is written *at the controller* a
controlled offset after the intent is submitted. The intent reads the line
state and the controller's version. Six release paths are compared:

  xair + plc_plain                optimistic gate, unconditional command
  xair_atomic + plc_plain         atomic commit, unconditional command
  xair + plc_predicate            optimistic gate, controller-local interlock: the
                                  controller executes only if its line state is RUN
  xair_atomic + plc_predicate     atomic commit, controller-local interlock
  xair + plc_conditional          optimistic gate, command conditional on the version
  xair_atomic + plc_conditional   atomic commit, command conditional on the version

Ground truth is the controller's own state when the command takes effect: a
command executed while the line is paused is a stale actuation. With
``--aba-gap-ms`` the injected event is a pause followed, that long after, by a
resume. A command executed on RUN at a controller version newer than the one
in the context its last check observed (``witness_version``) took effect after
a change that was undone in between: a history violation, which the
controller-local interlock cannot see and the version condition refuses.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import random
import threading
import time
import uuid
from pathlib import Path

from common import RESULTS_DIR, adapter, now_iso, released, wait_xair_state, wilson_ci, write_csv, xair_context

CELL_URL = os.environ.get("CELL_URL", "opc.tcp://127.0.0.1:4840/cell/")
PATHS = (("xair", "plc_plain"), ("xair_atomic", "plc_plain"), ("xair", "plc_predicate"), ("xair_atomic", "plc_predicate"),
         ("xair", "plc_conditional"), ("xair_atomic", "plc_conditional"))


class OpenPLCCell:
    """The same interface over Modbus against the OpenPLC runtime (docker/openplc/cell_ctl.st)."""

    def __init__(self) -> None:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from plc_modbus import PLC
        self.plc = PLC(os.environ.get("PLC_ADDR"))
        self.client = self.plc.client

    def set_state(self, value: str) -> int:
        return self.plc.set_state(value)


class Cell:
    def __init__(self, url: str) -> None:
        self.url = url
        self.lock = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        from asyncua.sync import Client
        self.client = Client(self.url)
        self.client.connect()
        self.ns = self.client.get_namespace_index("urn:xair:cell")
        self.obj = self.client.nodes.objects.get_child([f"{self.ns}:Cell"])

    def set_state(self, value: str) -> int:
        with self.lock:
            for attempt in range(8):
                try:
                    return int(self.obj.call_method(f"{self.ns}:SetLineState", value))
                except Exception:
                    # the session of a long run over Wi-Fi can drop: open a new one and repeat the
                    # write (setting the same value again changes nothing)
                    if attempt == 7:
                        raise
                    try:
                        self.client.disconnect()
                    except Exception:
                        pass
                    time.sleep(2.0)
                    try:
                        self._connect()
                    except Exception:
                        pass


def wait_mirror(version: int, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = (xair_context().get("context") or {}).get("line", {})
        if line.get("state") == "RUN" and line.get("version") == version:
            return True
        time.sleep(0.005)
    return False


def trial(cell: Cell, mode: str, actuator: str, offset_ms: float | None, run: int, aba_gap_ms: float | None = None) -> dict:
    v = cell.set_state("RUN")
    mirrored = wait_mirror(v)
    intent = {
        "id": str(uuid.uuid4()), "source": "ai", "timestamp_decision": now_iso(), "freshness_window_ms": 5000,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "line.version >= 0"}],
        "payload": {"action_type": "RESUME", "target_entity": f"e21_{uuid.uuid4().hex[:8]}"},
    }
    pause_at: dict = {}

    def pause():
        time.sleep(offset_ms / 1000.0)
        pause_at["t"] = time.perf_counter()
        cell.set_state("PAUSED")
        if aba_gap_ms is not None:
            time.sleep(aba_gap_ms / 1000.0)
            cell.set_state("RUN")

    th = None
    t0 = time.perf_counter()
    if offset_ms is not None:
        th = threading.Thread(target=pause, daemon=True)
        th.start()
    resp = adapter("intent", intent, mode=mode, actuator=actuator)
    if th:
        th.join(timeout=2)
    res = resp.get("cell_result") or ""
    executed = res.startswith("executed")
    state_at_effect = res.split(":")[1] if executed else ""
    try:
        version_at_effect = int(res.split(":")[-1]) if res else None
    except ValueError:
        version_at_effect = None
    witness = resp.get("witness_version")
    history = int(executed and state_at_effect == "RUN" and witness is not None and version_at_effect is not None
                  and version_at_effect > int(witness))
    return {
        "mode": mode, "actuator": actuator, "inject": int(offset_ms is not None),
        "event": ("aba" if aba_gap_ms is not None else "pause") if offset_ms is not None else "",
        "aba_gap_ms": aba_gap_ms if aba_gap_ms is not None and offset_ms is not None else "",
        "mirrored": int(mirrored), "start_version": v, "witness_version": "" if witness is None else witness,
        "version_at_effect": "" if version_at_effect is None else version_at_effect,
        "history_violation": history,
        "inject_offset_ms": offset_ms if offset_ms is not None else "", "run": run,
        "gateway_released": int(released(resp)), "reason": resp.get("reason"), "cell_result": res,
        "executed": int(executed), "state_at_effect": state_at_effect,
        "stale_effect": int(executed and state_at_effect != "RUN"),
        "refused_by_controller": int(res.startswith("refused")),
        "e2e_latency_ms": resp.get("e2e_latency_ms"),
        "pause_after_submit_ms": (pause_at["t"] - t0) * 1000.0 if "t" in pause_at else "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20, help="Injected trials per (path, offset)")
    ap.add_argument("--controls", type=int, default=20, help="Valid trials per path")
    ap.add_argument("--offsets-ms", type=float, nargs="+", default=[0, 2, 4, 6, 8, 10, 12, 14, 16, 20, 30])
    ap.add_argument("--seed", type=int, default=21)
    ap.add_argument("--backend", choices=("opcua", "openplc"), default="opcua")
    ap.add_argument("--aba-gap-ms", type=float, default=None,
                    help="inject a pause and, this long after it, a resume (history violation) instead of a pause")
    ap.add_argument("--paths", nargs="+", default=None, help="subset of mode/actuator paths, e.g. xair/plc_plain")
    ap.add_argument("--out", default=str(RESULTS_DIR / "e21_cell_controller.csv"))
    args = ap.parse_args()
    cell = OpenPLCCell() if args.backend == "openplc" else Cell(CELL_URL)
    paths = [(m, a.replace("plc_", "openplc_")) for m, a in PATHS] if args.backend == "openplc" else list(PATHS)
    if args.paths:
        paths = [(m, a) for m, a in paths if f"{m}/{a}" in args.paths]
    plan = [(m, a, o) for m, a in paths for o in args.offsets_ms for _ in range(args.runs)]
    plan += [(m, a, None) for m, a in paths for _ in range(args.controls)]
    random.Random(args.seed).shuffle(plan)
    rows = [trial(cell, m, a, o, i, args.aba_gap_ms) for i, (m, a, o) in enumerate(plan)]
    cell.set_state("RUN")
    try:
        cell.client.disconnect()   # the sync OPC UA client runs a non-daemon thread
    except Exception:
        pass
    write_csv(Path(args.out), rows)
    for m, a in paths:
        inj = [r for r in rows if r["mode"] == m and r["actuator"] == a and r["inject"]]
        ctl = [r for r in rows if r["mode"] == m and r["actuator"] == a and not r["inject"]]
        k = sum(r["stale_effect"] for r in inj)
        h = sum(r["history_violation"] for r in inj)
        print(json.dumps({"mode": m, "actuator": a, "stale_effects": f"{k}/{len(inj)}", "ci95": wilson_ci(k, len(inj)),
                          "history_violations": f"{h}/{len(inj)}",
                          "refused": sum(r["refused_by_controller"] for r in inj),
                          "controls_executed": f"{sum(r['executed'] for r in ctl)}/{len(ctl)}"}))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    os._exit(code)  # never hang on a lingering client thread
