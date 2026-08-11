#!/usr/bin/env python3
"""
OPC UA HIL bridge — exposes line.state on an OPC UA server and forwards every update to XAIR snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
XAIR = "http://127.0.0.1:8080"
LOG = ROOT / "experiments" / "results" / "e15_transport.log"


def _post_snapshot(state: str, transport: str = "opcua") -> None:
    import urllib.request

    body = json.dumps({
        "line": {"state": state},
        "gripper": {"state": "OPEN" if state == "RUN" else "CLOSED"},
        "source": "opcua_hil",
        "transport": transport,
    }).encode()
    req = urllib.request.Request(
        f"{XAIR}/v1/context/snapshot",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10):
        pass
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({"transport": transport, "state": state, "ts": time.time()}) + "\n")


async def run_daemon(port: int) -> None:
    from asyncua import Server, ua

    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://0.0.0.0:{port}/adaptix/hil/")
    idx = await server.register_namespace("http://adaptix/hil")
    objects = server.get_objects_node()
    line_obj = await objects.add_object(idx, "Line")
    state_var = await line_obj.add_variable(idx, "State", "RUN")
    await state_var.set_writable()

    async def set_line_state(parent, state):
        from asyncua import ua

        val = state.Value if hasattr(state, "Value") else str(state)
        val = str(val)
        await state_var.write_value(val)
        _post_snapshot(val, "opcua")
        return [ua.Variant(val, ua.VariantType.String)]

    in_args = [ua.VariantType.String]
    out_args = [ua.VariantType.String]
    await line_obj.add_method(idx, "SetLineState", set_line_state, in_args, out_args)

    async with server:
        _post_snapshot("RUN", "opcua")
        while True:
            await asyncio.sleep(3600)


async def write_state(port: int, state: str) -> None:
    from asyncua import Client

    url = f"opc.tcp://127.0.0.1:{port}/adaptix/hil/"
    async with Client(url=url) as client:
        idx = await client.get_namespace_index("http://adaptix/hil")
        line = client.get_node(f"ns={idx};i=1")
        method = await line.get_child(f"{idx}:SetLineState")
        await line.call_method(method, state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4840)
    parser.add_argument("--daemon", action="store_true", help="Run OPC UA server and forward writes to XAIR")
    parser.add_argument("--write", type=str, help="Set line.state via OPC UA method (RUN|PAUSED)")
    args = parser.parse_args()

    if args.daemon:
        asyncio.run(run_daemon(args.port))
        return 0
    if args.write:
        asyncio.run(write_state(args.port, args.write))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
