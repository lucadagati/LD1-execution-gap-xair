#!/usr/bin/env python3
"""Upload, compile, and start a Structured Text program on an OpenPLC v3 runtime (web API).

    python scripts/openplc_deploy.py --url http://127.0.0.1:18082 docker/openplc/cell_ctl.st
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("program")
    ap.add_argument("--url", default="http://127.0.0.1:18082")
    ap.add_argument("--user", default="openplc")
    ap.add_argument("--password", default="openplc")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.url, follow_redirects=True, timeout=60)
    c.post("/login", data={"username": a.user, "password": a.password})
    r = c.post("/upload-program", files={"file": ("cell_ctl.st", open(a.program, "rb"), "text/plain")})
    m = re.search(r"value='(\d+\.st)' id='prog_file'", r.text)
    if not m:
        print("upload failed", r.status_code, r.text[:300]); return 1
    st = m.group(1)
    c.post("/upload-program-action", data={"prog_name": "xair cell controller", "prog_descr": "conditional actuation",
                                           "prog_file": st, "epoch_time": str(int(time.time()))})
    c.get("/compile-program", params={"file": st})
    for _ in range(300):
        logs = c.get("/compilation-logs").text
        if "Compilation finished successfully" in logs:
            break
        if "Error" in logs and "error" in logs.lower() and "finished" in logs.lower():
            print(logs[-2000:]); return 1
        time.sleep(1)
    else:
        print("compilation timeout"); print(logs[-2000:]); return 1
    c.get("/start_plc")
    time.sleep(3)
    print("deployed", st, "status:", "Running" if "Running" in c.get("/dashboard").text else "unknown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
