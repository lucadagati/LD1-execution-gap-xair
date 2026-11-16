#!/usr/bin/env python3
"""Emulated cell controller (PLC-style) on OPC UA, owner of the line state.

Semantics of the running example (paper Sec. 3). ``line.state`` is the line's
run mode, set by the operator or HMI: RUN, or PAUSED (e.g. to clear a jam).
``RESUME`` restarts the conveyor of the station, which is STOPPED while the
station waits (for the next part, a confirmation, or after a pause); it is
admissible only while the line is in RUN mode, hence its precondition
``line.state == 'RUN'``. Its effect is ``conveyor = RUNNING``; pausing the
line stops the conveyor. A RESUME executed on a PAUSED line is therefore a
stale effect: the conveyor moves on a line the operator has paused.

The controller holds the line state and a version that advances on every
change of it (the conveyor state does not advance it: no predicate reads it).
A version value is never reused, including across restarts (assumption A4 of
the paper): with ``--state-file`` the last version is persisted atomically on
every change and a restart resumes after it; otherwise the version starts at
the wall-clock time in microseconds, so a restart starts above every value the
previous run could have reached unless the clock steps back. Builds before
v1.7 started at 0 on every restart.
Commands are queued and executed at the next scan tick, so the check and the
effect of a command happen in one scan, without interleaving any state change:

  SetLineState(value) -> version      operator / HMI write (applied at once)
  Resume(intent_id) -> result         unconditional command
  ConditionalResume(intent_id, expected_version, deadline_epoch_ms) -> result
                                      executed only if the version is still
                                      ``expected_version`` and the deadline has
                                      not passed at the scan that executes it
  PredicateResume(intent_id, deadline_epoch_ms) -> result
                                      executed only if the line state is RUN
                                      (and the deadline has not passed) at the
                                      scan that executes it: a controller-local
                                      interlock on the state, without a version

``result`` is "executed:<line state at the effect>:<version>" or
"refused:<reason>:<version>"; ``GetStatus`` returns
"<line state>:<version>:<effects>:<conveyor state>". A bridge replicates every
line-state change to the XAIR snapshot asynchronously, as a subscription
would, so XAIR's copy lags the controller by the bridge latency; the conveyor
state stays local to the controller.

Usage: python scripts/cell_controller.py [--port 4840] [--scan-ms 10] [--xair http://xair:8080]
                                        [--state-file cell_version.json] [--version-seed N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import urllib.request

from asyncua import Server, ua
from asyncua.common.methods import uamethod

logging.getLogger("asyncua").setLevel(logging.ERROR)


class Cell:
    def __init__(self, xair: str | None, state_file: str | None = None, version_seed: int | None = None,
                 sampling_ms: float = 0.0) -> None:
        self.state_file = state_file
        self.sampling_ms = sampling_ms
        if version_seed is not None:
            start = int(version_seed)
        elif state_file and os.path.exists(state_file):
            with open(state_file, encoding="utf-8") as f:
                start = int(json.load(f)["version"]) + 1   # never reuse the last persisted value
        else:
            start = time.time_ns() // 1000                  # boot epoch in microseconds
        self.state, self.version = "RUN", start
        self._persist()
        self.conveyor = "STOPPED"
        self.pending: list[tuple[str, int | None, float | None, asyncio.Future, bool]] = []
        self.bridge_q: asyncio.Queue = asyncio.Queue()
        self.xair = xair.rstrip("/") if xair else None
        self.effects = 0

    def set_state(self, value: str) -> int:
        """Operator/HMI write of the line's run mode; a pause also stops the conveyor."""
        if value != self.state:
            self.state = value
            self.version += 1
            self._persist()
            self.bridge_q.put_nowait((self.state, self.version))
        if value != "RUN":
            self.conveyor = "STOPPED"
        return self.version

    def _persist(self) -> None:
        """Write the current version atomically (tmp file, fsync, rename) before it is used."""
        if not self.state_file:
            return
        tmp = f"{self.state_file}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": self.version}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_file)

    def scan(self) -> None:
        """One scan: execute queued commands against the state as it is now."""
        now = time.time() * 1000.0
        batch, self.pending = self.pending, []
        for _intent_id, expected, deadline, fut, needs_run in batch:
            if expected is not None and expected != self.version:
                fut.set_result(f"refused:version_changed:{self.version}")
            elif needs_run and self.state != "RUN":
                fut.set_result(f"refused:predicate_false:{self.version}")
            elif deadline is not None and now > deadline:
                fut.set_result(f"refused:deadline:{self.version}")
            else:
                self.effects += 1
                self.conveyor = "RUNNING"   # the effect of RESUME
                fut.set_result(f"executed:{self.state}:{self.version}")

    async def bridge(self) -> None:
        loop = asyncio.get_running_loop()
        published = (self.state, self.version)
        while True:
            if self.sampling_ms > 0:
                # An OPC UA subscription with a sampling interval and a queue of one: the server
                # looks at the value once per interval and publishes it only if it differs from the
                # last published one, so a pause that is undone within one interval is never sent.
                await asyncio.sleep(self.sampling_ms / 1000.0)
                while not self.bridge_q.empty():
                    self.bridge_q.get_nowait()
                state, version = self.state, self.version
                if (state, version) == published:
                    continue
                published = (state, version)
            else:
                state, version = await self.bridge_q.get()
            if not self.xair:
                continue
            body = json.dumps({"line": {"state": state, "version": version}}).encode()
            req = urllib.request.Request(f"{self.xair}/v1/context/snapshot", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5).read())
            except Exception:
                pass


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4840)
    ap.add_argument("--scan-ms", type=float, default=10.0)
    ap.add_argument("--xair", default=None)
    ap.add_argument("--state-file", default=None, help="persist the version here; a restart resumes after it")
    ap.add_argument("--version-seed", type=int, default=None, help="initial version (default: boot epoch in us)")
    ap.add_argument("--sampling-ms", type=float, default=float(os.environ.get("CELL_SAMPLING_MS", "0")),
                    help="replicate by sampling at this interval (0: every change, as a queue that never overflows)")
    args = ap.parse_args()
    cell = Cell(args.xair, state_file=args.state_file, version_seed=args.version_seed, sampling_ms=args.sampling_ms)

    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://0.0.0.0:{args.port}/cell/")
    idx = await server.register_namespace("urn:xair:cell")
    obj = await server.nodes.objects.add_object(idx, "Cell")

    @uamethod
    async def set_line_state(parent, value: str):
        return cell.set_state(value)

    async def enqueue(intent_id, expected, deadline, needs_run=False):
        fut = asyncio.get_running_loop().create_future()
        cell.pending.append((intent_id, expected, deadline, fut, needs_run))
        return await fut

    @uamethod
    async def resume(parent, intent_id: str):
        return await enqueue(intent_id, None, None)

    @uamethod
    async def conditional_resume(parent, intent_id: str, expected_version: int, deadline_epoch_ms: float):
        return await enqueue(intent_id, int(expected_version),
                             float(deadline_epoch_ms) if deadline_epoch_ms > 0 else None)

    @uamethod
    async def predicate_resume(parent, intent_id: str, deadline_epoch_ms: float):
        return await enqueue(intent_id, None, float(deadline_epoch_ms) if deadline_epoch_ms > 0 else None, True)

    @uamethod
    async def get_status(parent):
        return f"{cell.state}:{cell.version}:{cell.effects}:{cell.conveyor}"

    S, I64, D = ua.VariantType.String, ua.VariantType.Int64, ua.VariantType.Double
    await obj.add_method(idx, "SetLineState", set_line_state, [S], [I64])
    await obj.add_method(idx, "Resume", resume, [S], [S])
    await obj.add_method(idx, "ConditionalResume", conditional_resume, [S, I64, D], [S])
    await obj.add_method(idx, "PredicateResume", predicate_resume, [S, D], [S])
    await obj.add_method(idx, "GetStatus", get_status, [], [S])
    cell.bridge_q.put_nowait((cell.state, cell.version))

    async def scanner():
        period = args.scan_ms / 1000.0
        nxt = time.monotonic()
        while True:
            nxt += period
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))
            cell.scan()

    async with server:
        await asyncio.gather(scanner(), cell.bridge())


if __name__ == "__main__":
    asyncio.run(main())
