"""Compare the fence-on repeat campaigns (distributed c6_fence, c7_fence; E3-stop seeds 28, 29) with the
published campaigns, metric by metric. Usage: fence_repeat_summary.py [DATA_DIR]
Writes docs/detailed-results/generated/tab_fence_repeat.csv (metric, campaign, value)."""
from __future__ import annotations
import csv, glob, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
D = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "execution-gap"
res: list[dict] = []


def rows(p):
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def add(metric, camp, value):
    res.append({"metric": metric, "campaign": camp, "value": value})


def truthy(v):
    return str(v).strip().lower() in ("1", "true")


camps = {"c1": D / "distributed"}
for c in sorted(glob.glob(str(D / "distributed" / "campaigns" / "c*"))):
    camps[Path(c).name] = Path(c)

for name, c in camps.items():
    for fn, mode in (("e10_toctou_boundary.csv", "optimistic"), ("e10_toctou_atomic.csv", "atomic")):
        if (c / fn).exists():
            inj = [x for x in rows(c / fn) if x["inject"] == "1"]
            before = [x for x in inj if x["version_order"] == "before_check"]
            add(f"E3 {mode}: released of those invalidated before the check", name,
                f"{sum(x['gateway_released'] == '1' for x in before)}/{len(before)}")
    if (c / "e10_aba.csv").exists():
        g = defaultdict(lambda: [0, 0, 0, 0])
        for x in rows(c / "e10_aba.csv"):
            i = 0 if x["flip"] == "1" else 2
            g[(x["mode"], x["scope"])][i] += x["gateway_released"] == "1"
            g[(x["mode"], x["scope"])][i + 1] += 1
        for k, v in sorted(g.items()):
            add(f"E3-ABA {k[0]}/{k[1]}: released after undone change; controls released", name, f"{v[0]}/{v[1]}; {v[2]}/{v[3]}")
    for fn, ev in (("e21_cell_controller.csv", "pause"), ("e21_cell_aba.csv", "pause-resume")):
        if (c / fn).exists():
            g = defaultdict(lambda: [0, 0, 0, 0, 0])
            for x in rows(c / fn):
                v = g[(x["mode"], x["actuator"])]
                if x["inject"] == "1":
                    v[0] += int(x["stale_effect"] or 0); v[1] += int(x["history_violation"] or 0); v[2] += 1
                else:
                    v[3] += int(x["executed"] or 0); v[4] += 1
            for k, v in sorted(g.items()):
                add(f"E4 {ev} {k[0]}/{k[1]}: stale; history; valid executed", name, f"{v[0]}/{v[2]}; {v[1]}/{v[2]}; {v[3]}/{v[4]}")
    if (c / "e16_context_churn.csv").exists():
        g = defaultdict(lambda: [0, 0])
        for x in rows(c / "e16_context_churn.csv"):
            k = (x["version_scope"], int(float(x["target_rate_hz"])))
            g[k][0] += x["gateway_released"] == "0"; g[k][1] += 1
        for k, v in sorted(g.items()):
            add(f"E5 {k[0]} @{k[1]} Hz: valid revoked", name, f"{v[0]}/{v[1]} ({100 * v[0] / v[1]:.0f}%)")
    if (c / "e16_trace_churn.csv").exists():
        g = defaultdict(lambda: [0, 0])
        for x in rows(c / "e16_trace_churn.csv"):
            k = (x["intent_kind"], x["version_scope"], x["publishing_interval_ms"])
            g[k][0] += x["gateway_released"] == "0"; g[k][1] += 1
        for k, v in sorted(g.items()):
            add(f"E5-trace {k[0]} {k[1]} @{k[2]} ms: valid revoked", name, f"{v[0]}/{v[1]} ({100 * v[0] / v[1]:.0f}%)")
    for fn, label, keyf in (("e1_baselines.csv", "E1", lambda x: (x["scenario"], x["baseline"])),
                            ("e9_consistency_sweep.csv", "E2", lambda x: (x["policy"], x["delay_ms"])),
                            ("e14_variants.csv", "E1-classes", lambda x: (x["scenario"], x["baseline"]))):
        if (c / fn).exists():
            g = defaultdict(lambda: [0, 0])
            for x in rows(c / fn):
                g[keyf(x)][0] += truthy(x["stale_executed"]); g[keyf(x)][1] += 1
            for k, v in sorted(g.items()):
                add(f"{label} {'/'.join(k)}: obsolete executed", name, f"{v[0]}/{v[1]}")
    if (c / "e17_policy.csv").exists():
        g = defaultdict(lambda: [0, 0])
        for x in rows(c / "e17_policy.csv"):
            g[(x["policy"], x["kind"])][0] += x["gateway_released"] == "1"; g[(x["policy"], x["kind"])][1] += 1
        for k, v in sorted(g.items()):
            add(f"E1-policy {k[0]}/{k[1]}: released", name, f"{v[0]}/{v[1]}")

for p in [D / "e27_stop_resume.csv"] + sorted(Path(x) for x in glob.glob(str(D / "e27_reps" / "*" / "e27_stop_resume.csv"))):
    if not p.exists():
        continue
    name = "e27 " + (p.parent.name if p.parent.name.startswith("seed") else "seed27")
    g = defaultdict(lambda: [0, 0, 0, 0])
    for x in rows(p):
        v = g[(x["stop_fence"], x["mode"])]
        if x["inject"] == "1":
            v[0] += int(x["stale_after_stop"] or 0); v[1] += 1
        else:
            v[2] += truthy(x["resume_released"]); v[3] += 1
    for k, v in sorted(g.items()):
        add(f"E3-stop fence={k[0]} {k[1]}: stale after stop; controls released", name, f"{v[0]}/{v[1]}; {v[2]}/{v[3]}")

out = ROOT / "docs" / "detailed-results" / "generated" / "tab_fence_repeat.csv"
with open(out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["metric", "campaign", "value"]); w.writeheader(); w.writerows(res)
by: dict = defaultdict(dict)
for r in res:
    by[r["metric"]][r["campaign"]] = r["value"]
for m, v in by.items():
    print(m, "|", " | ".join(f"{k}: {x}" for k, x in v.items()))

# compact LaTeX table for the detailed results: range over the published campaigns c1-c5 vs the fence-on campaign c6
KEY = [("E3 optimistic: released of those invalidated before the check", "E3, optimistic: released / invalidated before the check"),
       ("E3 atomic: released of those invalidated before the check", "E3, atomic: released / invalidated before the check"),
       ("E3-ABA xair/predicate: released after undone change; controls released", "E3-ABA, no version: released after undone change; controls"),
       ("E3-ABA xair/readset: released after undone change; controls released", "E3-ABA, read-set version"),
       ("E3-ABA xair/truth: released after undone change; controls released", "E3-ABA, truth version"),
       ("E4 pause xair/plc_plain: stale; history; valid executed", "E4 pause, unconditional (optimistic): stale; history; valid"),
       ("E4 pause xair/plc_predicate: stale; history; valid executed", "E4 pause, interlock (optimistic)"),
       ("E4 pause xair/plc_conditional: stale; history; valid executed", "E4 pause, version-conditional (optimistic)"),
       ("E4 pause-resume xair/plc_predicate: stale; history; valid executed", "E4 pause--resume, interlock (optimistic)"),
       ("E4 pause-resume xair/plc_conditional: stale; history; valid executed", "E4 pause--resume, version-conditional (optimistic)"),
       ("E5 global @20 Hz: valid revoked", "E5, global version, 20\\,Hz: valid revoked"),
       ("E5 global @100 Hz: valid revoked", "E5, global version, 100\\,Hz"),
       ("E5 readset @100 Hz: valid revoked", "E5, read-set version, 100\\,Hz"),
       ("E5 truth @100 Hz: valid revoked", "E5, truth version, 100\\,Hz"),
       ("E5-trace continuous readset @10 ms: valid revoked", "E5-trace, read-set version, 10\\,ms"),
       ("E5-trace continuous truth @10 ms: valid revoked", "E5-trace, truth version, 10\\,ms")]
def span(values: list[str]) -> str:
    """Range over campaigns, part by part: '0/200; 52--62/200' when the campaigns differ."""
    if len(set(values)) == 1:
        return values[0]
    parts = [v.split("; ") for v in values]
    out = []
    for col in zip(*parts):
        if len(set(col)) == 1:
            out.append(col[0])
            continue
        kn = [c.split(" ")[0].split("/") for c in col]
        ks = sorted(int(k) for k, _ in kn)
        ns = sorted({int(n) for _, n in kn})
        n = str(ns[0]) if len(ns) == 1 else f"{ns[0]}--{ns[-1]}"
        cell = f"{ks[0]}--{ks[-1]}/{n}" if ks[0] != ks[-1] else f"{ks[0]}/{n}"
        if "(" in col[0] and len(ns) == 1:
            cell += f" ({round(100 * ks[0] / ns[0])}--{round(100 * ks[-1] / ns[0])}%)"
        out.append(cell)
    return "; ".join(out)


with open(ROOT / "docs" / "detailed-results" / "generated" / "tab_fence_repeat.tex", "w") as f:
    for key, label in KEY:
        v = by.get(key, {})
        pub = [v[c] for c in ("c1", "c2", "c3", "c4", "c5") if c in v]
        f.write(f"{label} & {span(pub)} & {v.get('c6_fence', '--')} \\\\\n".replace("%", "\\%"))
