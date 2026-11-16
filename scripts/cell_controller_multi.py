#!/usr/bin/env python3
"""Emulated cell controller that owns several fast variables besides the line state (E26).

Same OPC UA interface and scan semantics as ``cell_controller.py``. In addition to
``line.state`` the controller owns ``--fast-vars`` variables (v0, v1, ...) that change
value at ``--fast-rate-hz`` each, as a field signal would. The version that the
conditional command compares has one of two scopes:

  global   one counter for all controller state: it advances on every change of the
           line state and of every fast variable;
  line     the version of the variable the intent reads (``line.state``): it advances
           only on a change of the line state, so fast variables never revoke it.

The bridge mirrors the version to XAIR's snapshot as ``line.version``, coalescing
bursts (a subscription publishes the latest value). ``GetScanStats`` returns the
mean, 99th-percentile and maximum duration of one scan in microseconds.

Usage: python scripts/cell_controller_multi.py --version-scope global|line --fast-vars 3
                                              --fast-rate-hz 50 [--port 4840] [--xair URL]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.request

from asyncua import Server, ua
from asyncua.common.methods import uamethod

from cell_controller import Cell


class MultiCell(Cell):
    def __init__(self, xair, scope: str, fast_vars: int) -> None:
        super().__init__(xair)
        self.scope = scope
        self.fast = {f"v{i}": 0.0 for i in range(fast_vars)}
        self.scan_us: list[float] = []

    def fast_change(self, name: str) -> None:
        self.fast[name] += 1.0
        if self.scope == "global":
            self.version += 1
            self.bridge_q.put_nowait((self.state, self.version))

    def scan(self) -> None:
        t0 = time.perf_counter()
        super().scan()
        self.scan_us.append((time.perf_counter() - t0) * 1e6)

    def scan_stats(self) -> str:
        xs = sorted(self.scan_us) or [0.0]
        return f"{sum(xs) / len(xs):.1f}:{xs[int(0.99 * (len(xs) - 1))]:.1f}:{xs[-1]:.1f}"

    async def bridge(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            state, version = await self.bridge_q.get()
            while not self.bridge_q.empty():          # coalesce: publish the latest value only
                state, version = self.bridge_q.get_nowait()
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
    ap.add_argument("--version-scope", choices=("global", "line"), default="line")
    ap.add_argument("--fast-vars", type=int, default=3)
    ap.add_argument("--fast-rate-hz", type=float, default=0.0)
    args = ap.parse_args()
    cell = MultiCell(args.xair, args.version_scope, args.fast_vars)

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

    @uamethod
    async def get_scan_stats(parent):
        return cell.scan_stats()

    S, I64, D = ua.VariantType.String, ua.VariantType.Int64, ua.VariantType.Double
    await obj.add_method(idx, "SetLineState", set_line_state, [S], [I64])
    await obj.add_method(idx, "Resume", resume, [S], [S])
    await obj.add_method(idx, "ConditionalResume", conditional_resume, [S, I64, D], [S])
    await obj.add_method(idx, "PredicateResume", predicate_resume, [S, D], [S])
    await obj.add_method(idx, "GetStatus", get_status, [], [S])
    await obj.add_method(idx, "GetScanStats", get_scan_stats, [], [S])
    cell.bridge_q.put_nowait((cell.state, cell.version))

    async def scanner():
        period = args.scan_ms / 1000.0
        nxt = time.monotonic()
        while True:
            nxt += period
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))
            cell.scan()

    async def writer(name: str, phase: float):
        period = 1.0 / args.fast_rate_hz
        nxt = time.monotonic() + phase
        while True:
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))
            cell.fast_change(name)
            nxt += period

    tasks = [scanner(), cell.bridge()]
    if args.fast_rate_hz > 0:
        for i, name in enumerate(cell.fast):
            tasks.append(writer(name, i * (1.0 / args.fast_rate_hz) / max(1, len(cell.fast))))
    async with server:
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
