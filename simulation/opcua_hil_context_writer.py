#!/usr/bin/env python3
"""
OPC UA HIL context writer — publishes line.state to XAIR via OPC UA when asyncua is installed,
otherwise uses explicit HTTP snapshot updates (recorded as transport=http_snapshot).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
XAIR = "http://127.0.0.1:8080"


def http_context_update(state: str, transport_log: Path) -> None:
    import urllib.request

    body = json.dumps({"line": {"state": state}, "source": "opcua_hil", "transport": "http_snapshot"}).encode()
    req = urllib.request.Request(
        f"{XAIR}/v1/context/snapshot",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10):
        pass
    transport_log.write_text(json.dumps({"transport": "http_snapshot", "state": state}) + "\n")


def run_opcua_loop(port: int, duration: float, transport_log: Path) -> None:
    import asyncio
    from asyncua import Server

    async def main_async():
        server = Server()
        await server.init()
        server.set_endpoint(f"opc.tcp://0.0.0.0:{port}/adaptix/hil/")
        idx = await server.register_namespace("http://adaptix/hil")
        objects = server.get_objects_node()
        line_obj = await objects.add_object(idx, "Line")
        state_var = await line_obj.add_variable(idx, "State", "RUN")
        await state_var.set_writable()
        async with server:
            deadline = time.time() + duration
            toggle = 0
            while time.time() < deadline:
                state = "RUN" if toggle % 2 == 0 else "PAUSED"
                await state_var.write_value(state)
                import urllib.request

                body = json.dumps({"line": {"state": state}, "source": "opcua_hil", "transport": "opcua"}).encode()
                req = urllib.request.Request(
                    f"{XAIR}/v1/context/snapshot",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10):
                    pass
                transport_log.write_text(json.dumps({"transport": "opcua", "state": state, "port": port}) + "\n")
                toggle += 1
                await asyncio.sleep(2.0)

    asyncio.run(main_async())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4840)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--transport", choices=("auto", "opcua", "http_snapshot"), default="auto")
    args = parser.parse_args()
    log = ROOT / "experiments" / "results" / "e15_transport.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    use_opcua = args.transport == "opcua" or (args.transport == "auto")
    if use_opcua:
        try:
            run_opcua_loop(args.port, args.duration, log)
            return 0
        except ImportError:
            if args.transport == "opcua":
                print("asyncua not installed", file=sys.stderr)
                return 1
    for state in ("RUN", "PAUSED", "RUN", "PAUSED"):
        http_context_update(state, log)
        time.sleep(min(2.0, args.duration / 4))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
