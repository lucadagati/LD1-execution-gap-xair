#!/usr/bin/env python3
"""Access to the OpenPLC cell controller (docker/openplc/cell_ctl.st) over Modbus TCP.

Holding registers: 0 line_state (1 RUN, 0 PAUSED), 1 version counter (0..32767; register 10 is its epoch, so
the client-side version is epoch*32768+counter), 2 set_req,
3 cmd_seq, 4 cmd_mode (0 unconditional, 1 conditional, 2 predicate: RUN only), 5 cmd_expected, 6 cmd_done,
7 cmd_result (1 executed on RUN, 2 executed on PAUSED, 3 refused: version, 4 refused: not RUN), 8 effects,
9 conveyor (1 RUNNING, 0 STOPPED): RESUME restarts the conveyor, a pause stops it,
10 version epoch, 11 expected epoch of a conditional command.
A command is one transaction on the client side: a lock covers issuing it and
waiting for its result, and sequence numbers are allocated by this client.

As a script it runs the bridge that replicates the PLC's line state to XAIR by
polling, as a SCADA or OPC UA gateway in front of a PLC would:
    python scripts/plc_modbus.py bridge --plc 127.0.0.1:5020 --xair http://127.0.0.1:18080 --poll-ms 5
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.request

from pymodbus.client import ModbusTcpClient


class PLC:
    def __init__(self, addr: str | None = None) -> None:
        host, port = (addr or os.environ.get("PLC_ADDR", "127.0.0.1:5020")).rsplit(":", 1)
        self.client = ModbusTcpClient(host, port=int(port), timeout=2)
        self.client.connect()
        self.lock = threading.Lock()       # one Modbus request at a time
        self.cmd_lock = threading.Lock()   # one command transaction at a time
        self.seq: int | None = None

    def regs(self, start: int, count: int) -> list[int]:
        with self.lock:
            rr = self.client.read_holding_registers(start, count=count)
        if rr.isError():
            raise RuntimeError(f"modbus read failed: {rr}")
        return [v - 65536 if v > 32767 else v for v in rr.registers]

    def version(self) -> int:
        """The PLC version as one integer, epoch*32768 + counter."""
        c, e = self.regs(1, 1)[0], self.regs(10, 1)[0]
        return e * 32768 + c

    def state(self) -> tuple[str, int]:
        r = self.regs(0, 11)
        return ("RUN" if r[0] == 1 else "PAUSED"), r[10] * 32768 + r[1]

    def set_state(self, value: str, timeout_s: float = 2.0) -> int:
        """Operator/HMI write; returns the version after the PLC applied it."""
        target = 1 if value == "RUN" else 0
        with self.lock:
            self.client.write_register(2, target & 0xFFFF)
        t = time.monotonic()
        while time.monotonic() - t < timeout_s:
            r = self.regs(0, 11)
            if r[0] == target and r[2] == -1:
                return r[10] * 32768 + r[1]
            time.sleep(0.002)
        raise TimeoutError("PLC did not apply the state change")

    def command(self, expected: int | None, kind: str | bool, timeout_s: float = 2.0) -> str:
        """Issue one command and wait for its result. ``kind``: "plain", "version"
        (needs ``expected`` = the PLC version), or "predicate" (line must be RUN)."""
        if isinstance(kind, bool):
            kind = "version" if kind else "plain"
        mode = {"plain": 0, "version": 1, "predicate": 2}[kind]
        with self.cmd_lock:
            if self.seq is None:
                self.seq = self.regs(6, 1)[0]
            self.seq = self.seq + 1 if self.seq < 32000 else 1
            seq = self.seq
            exp, exp_epoch = (-1, -1) if expected is None else (int(expected) % 32768, int(expected) // 32768)
            with self.lock:
                self.client.write_register(11, exp_epoch & 0xFFFF)   # before cmd_seq, which triggers the scan
                self.client.write_registers(3, [seq & 0xFFFF, mode, exp & 0xFFFF])
            t = time.monotonic()
            while time.monotonic() - t < timeout_s:
                r = self.regs(0, 11)
                v, d, res = r[10] * 32768 + r[1], r[6], r[7]
                if d == seq:
                    if res == 1:
                        return f"executed:RUN:{v}"
                    if res == 2:
                        return f"executed:PAUSED:{v}"
                    if res == 3:
                        return f"refused:version_changed:{v}"
                    if res == 4:
                        return f"refused:predicate_false:{v}"
                    return f"error:unknown_result_{res}:{v}"
                time.sleep(0.001)
            raise TimeoutError("PLC did not execute the command")


def bridge(plc: PLC, xair: str, poll_ms: float) -> None:
    last = None
    while True:
        try:
            st = plc.state()
            if st != last:
                body = json.dumps({"line": {"state": st[0], "version": st[1]}}).encode()
                req = urllib.request.Request(f"{xair.rstrip('/')}/v1/context/snapshot", data=body,
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
                last = st
        except Exception:
            time.sleep(0.1)
        time.sleep(poll_ms / 1000.0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["bridge", "status"])
    ap.add_argument("--plc", default=None)
    ap.add_argument("--xair", default=os.environ.get("XAIR_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--poll-ms", type=float, default=5.0)
    a = ap.parse_args()
    p = PLC(a.plc)
    if a.cmd == "status":
        print(p.regs(0, 12))
    else:
        bridge(p, a.xair, a.poll_ms)
