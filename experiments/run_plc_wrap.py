#!/usr/bin/env python3
"""Drive the OpenPLC cell controller through a wrap of its 16-bit version counter (see scripts/run_plc_wrap.sh)."""
from __future__ import annotations

import argparse
import json
import time

from plc_modbus import PLC


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--changes", type=int, default=32768)
    ap.add_argument("--out", default="plc_wrap.json")
    a = ap.parse_args()
    plc = PLC()
    plc.set_state("RUN")
    witness = plc.version()
    counter0, epoch0 = witness % 32768, witness // 32768
    t0 = time.time()
    state, bad_steps, last = "RUN", 0, witness
    for _ in range(a.changes):
        state = "PAUSED" if state == "RUN" else "RUN"
        v = plc.set_state(state)
        bad_steps += int(v != last + 1)
        last = v
    if state != "RUN":
        last = plc.set_state("RUN")
    counter1, epoch1 = last % 32768, last // 32768
    # at the point where the counter has wrapped back to its old value, a stale witness must be refused
    old = plc.command(witness, "version")
    new = plc.command(plc.version(), "version")
    res = {"changes": a.changes, "seconds": round(time.time() - t0, 1), "non_consecutive_steps": bad_steps,
           "witness": {"counter": counter0, "epoch": epoch0}, "after": {"counter": counter1, "epoch": epoch1},
           "old_witness_result": old, "fresh_witness_result": new,
           "pass": bad_steps == 0 and counter1 == counter0 and epoch1 == epoch0 + 1 and old.startswith("refused") and new.startswith("executed")}
    # also the exact moment of the wrap: counter equal to the witness' counter, epoch different
    json.dump(res, open(a.out, "w"), indent=1)
    print(json.dumps(res))
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
