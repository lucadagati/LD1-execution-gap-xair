"""E4 / E4-ABA outcomes per release path on the distributed testbed (five fence-off campaigns, injected trials):
how many intents the gateway released, the controller refused and the controller executed, with stale
effects and history violations. Writes docs/detailed-results/generated/tab_e4_outcomes.csv and .tex."""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "execution-gap" / "distributed"
OUT = ROOT / "docs" / "detailed-results" / "generated"
MODES = {"xair": "optimistic", "xair_atomic": "atomic"}
PATHS = {"plc_plain": "unconditional", "plc_predicate": "local interlock", "plc_conditional": "version-conditional"}
FIELDS = ("gateway_released", "refused_by_controller", "executed", "stale_effect", "history_violation")


def main() -> int:
    out_rows = []
    for kind, event in (("controller", "pause"), ("aba", "pause-resume")):
        filename = f"e21_cell_{kind}.csv"
        files = [DATA / filename]  # Campaign 1 is stored at the testbed root.
        files.extend(DATA / "campaigns" / f"c{i}" / filename for i in range(2, 6))
        agg: dict = defaultdict(lambda: [0] * (len(FIELDS) + 1))
        for f in files:
            with open(f, newline="") as fh:
                for r in csv.DictReader(fh):
                    if r["inject"] != "1":
                        continue
                    a = agg[(r["mode"], r["actuator"])]
                    a[0] += 1
                    for i, k in enumerate(FIELDS, 1):
                        a[i] += int(r[k] or 0)
        for (mode, act), v in sorted(agg.items(), key=lambda kv: (list(MODES).index(kv[0][0]), list(PATHS).index(kv[0][1]))):
            out_rows.append({"event": event, "mode": MODES[mode], "path": PATHS[act], "injected": v[0],
                             **{k: v[i] for i, k in enumerate(FIELDS, 1)}, "campaigns": len(files)})
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "tab_e4_outcomes.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0]))
        w.writeheader()
        w.writerows(out_rows)
    with open(OUT / "tab_e4_outcomes.tex", "w") as fh:
        for r in out_rows:
            fh.write(f"{r['event']} & {r['mode']}, {r['path']} & {r['injected']} & {r['gateway_released']} & "
                     f"{r['refused_by_controller']} & {r['executed']} & {r['stale_effect']} & {r['history_violation']} \\\\\n")
    print("wrote", OUT / "tab_e4_outcomes.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
