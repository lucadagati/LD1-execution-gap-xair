#!/usr/bin/env python3
"""Tables and numbers of the review round (2026-10-29/30) for the manuscript.

Reads the new campaigns under experiments/results (or the frozen copy under data/execution-gap/review)
and writes journal/generated/tab_rev_*.tex (rows only, like the other generated tables) and
journal/generated/numbers_review.tex (macros).  Nothing is typed by hand.

  python experiments/analyze_review.py [--results DIR] [--out DIR]
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "journal" / "generated"
NUM: dict[str, str] = {}


def rows(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def wilson_hi(k: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return float("nan")
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c + h) / d


def frac(k: int, n: int) -> str:
    return f"{k}/{n}"


def write_rows(name: str, lines: list[str]) -> None:
    text = "\n".join(l + " \\genrule" for l in lines) + "\n"
    (GEN / name).write_text(text, encoding="utf-8")
    docs = ROOT / "docs" / "detailed-results" / "generated"
    if GEN == ROOT / "journal" / "generated" and docs.exists():
        (docs / name).write_text(text, encoding="utf-8")


_W = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]


def words(x) -> str:
    """Letters only, for a TeX control sequence: 55 -> FiveFive, -55 -> MinusFiveFive."""
    t = str(int(abs(x)))
    return ("Minus" if x < 0 else "Plus" if isinstance(x, float) and x > 0 else "") + "".join(_W[int(c)] for c in t)


def macro(name: str, value) -> None:
    NUM[name] = str(value)


# ---------------------------------------------------------------- E4-ABA: pause duration and sampling
def aba_table(res: Path) -> None:
    cfgs = []  # (label, file)
    base = res / "distributed" / "review"
    for g, fn in ((3, "gaps/e21_cell_aba.csv"), (10, "gaps/e21_cell_aba_gap10.csv"), (25, "gaps/e21_cell_aba_gap25.csv"),
                  (50, "gaps/e21_cell_aba_gap50.csv"), (100, "gaps/e21_cell_aba_gap100.csv")):
        cfgs.append((f"distributed, all changes published & {g}", base / fn))
    for S in (50, 200):
        for g, fn in ((3, "e21_cell_aba.csv"), (20, "e21_cell_aba_gap20.csv")):
            cfgs.append((f"distributed, sampled every {S}\\,ms & {g}", base / f"sampling{S}" / fn))
    out = []
    for label, f in cfgs:
        if not f.exists():
            continue
        r = [x for x in rows(f) if x["inject"] == "1"]
        cell = []
        for act in ("plc_plain", "plc_predicate", "plc_conditional"):
            xs = [x for x in r if x["actuator"] == act]
            k = sum(int(x["history_violation"]) for x in xs)
            cell.append(frac(k, len(xs)))
        out.append(f"{label} & " + " & ".join(cell) + " \\\\")
        src = "E" if "all changes" in label else "S" + str(int(label.split("every ")[1].split("\\")[0]))
        g = int(label.split("&")[1])
        for act, nm in zip(("plc_plain", "plc_predicate", "plc_conditional"), ("Plain", "Interlock", "Version")):
            xs = [x for x in r if x["actuator"] == act]
            tag = "".join(c if c.isalpha() else _W[int(c)] for c in src)
            macro(f"RevAba{tag}G{words(g)}{nm}K", sum(int(x["history_violation"]) for x in xs))
            macro(f"RevAba{tag}G{words(g)}{nm}N", len(xs))
    # the physical nodes: the default pause of the original campaigns and the repeats, and the added durations
    for sub, name, dflt in (("physical", "wide-area node", 20), ("lan", "LAN node", 10)):
        for g, pat in sorted(((5, "e21_cell_aba_gap5.csv"), (dflt, "e21_cell_aba.csv"), (50, "e21_cell_aba_gap50.csv"))):
            fs = sorted((res / sub / "campaigns").glob(f"c*/{pat}"))
            if pat == "e21_cell_aba.csv":
                fs = [ROOT / "data" / "execution-gap" / sub / pat] + fs
            if not fs:
                continue
            r = [x for f in fs for x in rows(f) if x["inject"] == "1"]
            cell = []
            for act in ("plc_plain", "plc_predicate", "plc_conditional"):
                xs = [x for x in r if x["actuator"] == act]
                cell.append(frac(sum(int(x["history_violation"]) for x in xs), len(xs)))
            out.append(f"{name} & {g} & " + " & ".join(cell) + " \\\\")
    write_rows("tab_rev_aba.tex", out)


def sampling_table(res: Path) -> None:
    offs = (0, 20, 50, 100, 150, 200, 300)
    out = []
    for S in (0, 50, 200):
        f = res / "distributed" / "review" / f"sampling{S}" / "e21_cell_controller.csv"
        if not f.exists():
            continue
        by = defaultdict(lambda: [0, 0])
        for x in rows(f):
            if x["inject"] == "1" and x["actuator"] == "plc_plain":
                o = float(x["inject_offset_ms"])
                by[o][0] += 1
                by[o][1] += int(x["stale_effect"])
        cells = [frac(by[o][1], by[o][0]) if o in by else "--" for o in offs]
        last = max((o for o, v in by.items() if v[1] > 0), default=None)
        macro(f"RevSampS{words(S)}LastStale", int(last) if last is not None else "none")
        label = "every change" if S == 0 else f"{S}\\,ms"
        out.append(f"{label} & " + " & ".join(cells) + " \\\\")
    write_rows("tab_rev_sampling.tex", out)


# ---------------------------------------------------------------- E3-stop with a skewed producer
def stop_table(res: Path) -> None:
    out = []
    for f in sorted(glob.glob(str(res / "review" / "e27_eps*_prod*.csv"))):
        name = Path(f).stem
        eps = float(name.split("eps")[1].split("_")[0])
        prod = float(name.split("prod")[1])
        r = rows(f)
        inj = [x for x in r if x["inject"] == "1"]
        stale = sum(int(x["stale_after_stop"]) for x in inj)
        ctl = [x for x in r if x["inject"] == "0"]
        cr = sum(int(x["resume_released"]) for x in ctl)
        after = defaultdict(lambda: [0, 0])
        for x in r:
            if x["inject"] == "2":
                a = after[float(x["stop_offset_ms"])]
                a[0] += 1
                a[1] += int(x["resume_released"])
        cells = [f"{int(after[d][1])}/{int(after[d][0])}" if d in after else "--" for d in (0, 5, 20, 60, 120)]
        out.append(f"{eps:g} & {prod:+g} & {frac(stale, len(inj))} & {frac(cr, len(ctl))} & " + " & ".join(cells) + " \\\\")
        tag = f"Eps{words(int(eps))}Prod{words(prod if prod < 0 else float(prod))}"
        macro(f"RevStop{tag}StaleK", stale)
        macro(f"RevStop{tag}StaleN", len(inj))
        ref = sum(1 for x in ctl if x["mode"] == "xair_atomic" and x["resume_reason"] == "decision_ahead_of_store_clock_at_commit")
        macro(f"RevStop{tag}AheadRefusedK", ref)
        macro(f"RevStop{tag}AheadRefusedN", sum(1 for x in ctl if x["mode"] == "xair_atomic"))
    write_rows("tab_rev_stop.tex", out)


# ---------------------------------------------------------------- replication on P, L and the boards
def campaign_dirs(sub: str, res: Path) -> list[tuple[str, Path]]:
    """c0 is the original campaign (data/execution-gap/<sub>); c1, c2, ... are the repeats."""
    out = [("c0", ROOT / "data" / "execution-gap" / sub)]
    seen = set()
    for base in (ROOT / "data" / "execution-gap" / sub / "campaigns", res / sub / "campaigns"):
        for d in sorted(base.glob("c*")) if base.exists() else []:
            if d.name not in seen and any(d.glob("*.csv")):
                seen.add(d.name)
                out.append((d.name, d))
    return out


def f_(x, d=1):
    return f"{x:.{d}f}"


def rng(vals: list[float], d: int = 0) -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return "--"
    lo, hi = min(vals), max(vals)
    return f(lo, d) if f(lo, d) == f(hi, d) else f"{f(lo, d)}--{f(hi, d)}"


def f(x, d=0):
    return f"{x:.{d}f}"


def e3_stats(d: Path):
    k = m = 0
    for name in ("e10_natural_window.csv", "e10_natural_window_atomic.csv"):
        if (d / name).exists():
            for r in rows(d / name):
                if r["inject"] == "1" and r["version_order"] == "before_check":
                    m += 1
                    k += int(r["gateway_released"] == "0")
    return k, m


def e4_curve(d: Path):
    """(first, last offset at which at least half of the unconditional commands acted on a paused line, largest offset)."""
    fpath = d / "e21_cell_controller.csv"
    if not fpath.exists():
        return None
    by = defaultdict(lambda: [0, 0])
    for r in rows(fpath):
        if r["inject"] == "1" and r["actuator"] == "plc_plain":
            o = float(r["inject_offset_ms"])
            by[o][0] += 1
            by[o][1] += int(r["stale_effect"])
    hi = sorted(o for o, (n, k) in by.items() if k * 2 >= n)
    anys = sorted(o for o, (n, k) in by.items() if k > 0)
    return (hi[0], hi[-1], max(anys), max(by)) if hi else (None, None, None, max(by))


def e5_rates(d: Path):
    fpath = d / "e16_context_churn.csv"
    if not fpath.exists():
        return None
    by = defaultdict(lambda: [0, 0])
    for r in rows(fpath):
        if float(r["target_rate_hz"]) == 20.0:
            k = (r["version_scope"], r["pattern"])
            by[k][0] += 1
            by[k][1] += int(r["gateway_released"] == "0")
    g = by.get(("global", "unrelated"))
    rs = by.get(("readset", "unrelated"))
    return (100 * g[1] / g[0] if g else None, 100 * rs[1] / rs[0] if rs else None)


def replication_table(res: Path) -> None:
    tb = [("P", "physical"), ("L", "lan"), ("B, Pi", "embedded/rpi4"), ("B, VF2", "embedded/vf2")]
    out = []
    for label, sub in tb:
        camps = campaign_dirs(sub, res)
        e3 = [e3_stats(d) for _, d in camps if (d / "e10_natural_window.csv").exists()]
        e4 = [c for c in (e4_curve(d) for _, d in camps) if c]
        e5 = [c for c in (e5_rates(d) for _, d in camps) if c and c[0] is not None]
        n_e3, n_e4, n_e5 = len(e3), len(e4), len(e5)
        e3s = "all" if all(k == m for k, m in e3) and e3 else "no"
        blocked = f"{sum(k for k, _ in e3)}/{sum(m for _, m in e3)}" if e3 else "--"
        e4s = (f"{rng([c[0] for c in e4])} & {rng([c[1] for c in e4])} & {rng([c[2] for c in e4])}" if e4 and e4[0][0] is not None else "-- & -- & --")
        for tag, idx in (("Start", 0), ("Last", 1), ("Any", 2)):
            vals = [c[idx] for c in e4 if c[idx] is not None]
            if vals:
                macro(f"RevRepl{sub.split('/')[0].capitalize()}EFour{tag}Min", int(min(vals)))
                macro(f"RevRepl{sub.split('/')[0].capitalize()}EFour{tag}Max", int(max(vals)))
        e5g = rng([c[0] for c in e5]) if e5 else "--"
        e5r = rng([c[1] for c in e5]) if e5 else "--"
        out.append(f"{label} & {n_e3} & {blocked} & {n_e4 or '--'} & {e4s} & {n_e5 or '--'} & {e5g} & {e5r} \\\\")
    write_rows("tab_rev_repl.tex", out)


def plc_wrap(res: Path) -> None:
    for base in (res / "openplc", ROOT / "data" / "execution-gap" / "openplc"):
        f = base / "plc_wrap.json"
        if f.exists():
            d = json.load(open(f))
            macro("RevPlcWrapChanges", d["changes"])
            macro("RevPlcWrapSeconds", int(round(d["seconds"])))
            macro("RevPlcWrapEpochAfter", d["after"]["epoch"])
            macro("RevPlcWrapPass", "passed" if d["pass"] else "FAILED")
            e = ROOT / "data" / "execution-gap" / "openplc" / "e24_openplc_epoch.csv"
            if e.exists():
                r = [x for x in rows(e) if x["inject"] == "1" and x["actuator"] != "openplc_plain"]
                macro("RevPlcEpochStale", sum(int(x["stale_effect"]) for x in r))
                macro("RevPlcEpochTrials", len(r))
                ctl = [x for x in rows(e) if x["inject"] == "0"]
                macro("RevPlcEpochCtlOk", sum(int(x["executed"]) for x in ctl))
                macro("RevPlcEpochCtlN", len(ctl))
            return


# ---------------------------------------------------------------- Eq. (fpr) on the boards: instrumented interval and writer process
def eq3_table(res: Path) -> None:
    out = []
    worst = {"periodic": 0.0, "poisson": 0.0}
    gaps: list[float] = []
    seed_rng = defaultdict(list)
    for board, bl in (("rpi4", "Pi"), ("vf2", "VF2")):
        for kind in ("periodic", "poisson"):
            pat = "e16_eq3_seed*.csv" if kind == "periodic" else "e16_eq3_poisson_seed*.csv"
            files = sorted(glob.glob(str(res / "embedded_eq3_instr" / board / pat)))
            if not files:
                continue
            allrows = []
            for fn in files:
                rr = rows(fn)
                allrows += rr
                for rate in (20.0,):
                    xs = [x for x in rr if float(x["target_rate_hz"]) == rate]
                    seed_rng[(board, kind)].append(100 * sum(x["gateway_released"] == "0" for x in xs) / len(xs))
            for rate in (20.0, 50.0, 100.0):
                xs = [x for x in allrows if float(x["target_rate_hz"]) == rate]
                n = len(xs)
                meas = 100 * sum(x["gateway_released"] == "0" for x in xs) / n
                def model(d_ms):
                    d = d_ms / 1000.0
                    return 100 * ((1 - math.exp(-rate * d)) if kind == "poisson" else min(1.0, rate * d))
                vg = [float(x["validation_to_gate_ms"]) for x in xs if x["validation_to_gate_ms"]]
                dt = [((float(x["gate_read_lo_ms"]) + float(x["gate_read_hi_ms"])) - (float(x["val_read_lo_ms"]) + float(x["val_read_hi_ms"]))) / 2
                      for x in xs if x.get("gate_read_lo_ms") and x.get("val_read_lo_ms")]
                m_vg = sum(model(d) for d in vg) / len(vg)
                m_dt = sum(model(d) for d in dt) / len(dt)
                med_vg = sorted(vg)[len(vg) // 2]
                med_dt = sorted(dt)[len(dt) // 2]
                worst[kind] = max(worst[kind], abs(meas - m_dt))
                gaps.append(meas - m_dt)
                out.append(f"{bl} & {kind} & {int(rate)} & {n} & {meas:.1f} & {med_vg:.1f} & {m_vg:.1f} & {med_dt:.1f} & {m_dt:.1f} \\\\")
                macro(f"RevEqThreeDev{'Vf' if bl == 'VF2' else bl}{kind.capitalize()}{words(int(rate))}", f"{meas - m_dt:+.1f}")
    write_rows("tab_rev_eq3.tex", out)
    if worst["poisson"]:
        macro("RevEqThreeWorstPoisson", f"{worst['poisson']:.1f}")
        macro("RevEqThreeWorstPeriodic", f"{worst['periodic']:.1f}")
        macro("RevEqThreeUnder", f"{max(gaps):.1f}")
        macro("RevEqThreeOver", f"{max(-g for g in gaps):.1f}")
    for (board, kind), v in seed_rng.items():
        if kind == "periodic":
            b = "Pi" if board == "rpi4" else "VF"
            macro(f"RevEqThreePeriodicTwentyMin{b}", f"{min(v):.0f}")
            macro(f"RevEqThreePeriodicTwentyMax{b}", f"{max(v):.0f}")


def e25_published(res: Path) -> None:
    """Table 8(b): full embedded campaign on the published build (fence on), rows for tab_rev_e25b.tex."""
    import statistics
    out = []
    for board, label in (("rpi4", "Raspberry Pi 4"), ("vf2", "VisionFive 2")):
        base = res / "embedded" / board
        reps = [rows(f)[0] for f in sorted(glob.glob(str(base / "e4_load_http_rep*.csv")))]
        thr = [float(r["throughput_ips"]) for r in reps]
        e2e = statistics.median(float(r["vl_e2e_p50_ms"]) for r in reps)
        d = rows(base / "e16_context_churn.csv")
        g = [x for x in d if x["version_scope"] == "global"]
        def fpr(rate):
            xs = [x for x in g if float(x["target_rate_hz"]) == rate]
            return 100 * sum(x["gateway_released"] == "0" for x in xs) / len(xs)
        dg = statistics.median(statistics.median(float(x["validation_to_gate_ms"]) for x in g
                               if float(x["target_rate_hz"]) == 100 and x["pattern"] == p) for p in ("unrelated", "related_same"))
        ver = [x for x in d if x["version_scope"] in ("readset", "truth") and float(x["target_rate_hz"]) in (20.0, 100.0)]
        assert all(x["gateway_released"] == "1" for x in ver), "read-set/truth revoked a valid intent"
        rss = [json.loads(l)["rss_mb"]["xair"] for l in open(base / "environment.txt") if l.strip() and "rss_mb" in l][0]
        thr_s = f"{min(thr):.0f}" if round(min(thr)) == round(max(thr)) else f"{min(thr):.0f}--{max(thr):.0f}"
        out.append(f"{label} & {thr_s} & {e2e:.1f} & {fpr(20.0):.0f} / {fpr(100.0):.0f} & {dg:.1f} & {rss} \\\\")
    write_rows("tab_rev_e25b.tex", out)


def phase_lock(res: Path) -> None:
    """Periodic writer against the closed loop of sequential trials: with and without a random pause."""
    devs, per_devs = [], []
    for board, bl in (("rpi4", "Pi"), ("vf2", "VF")):
        # raw campaigns under experiments/results; frozen copies under data/execution-gap (pub -> nojit)
        for tag, d0, frozen in (("Jit", "embedded_eq3_jit", "embedded_eq3_jit"), ("Fix", "embedded_eq3_pub", "embedded_eq3_nojit")):
            files = sorted(glob.glob(str(res / d0 / board / "e16_eq3_seed*.csv"))) or \
                sorted(glob.glob(str(ROOT / "data" / "execution-gap" / frozen / board / "e16_eq3_seed*.csv")))
            if not files:
                continue
            allrows = [x for f in files for x in rows(f)]
            for rate in (20.0, 50.0, 100.0):
                xs = [x for x in allrows if float(x["target_rate_hz"]) == rate]
                meas = 100 * sum(x["gateway_released"] == "0" for x in xs) / len(xs)
                vg = [float(x["validation_to_gate_ms"]) / 1000 for x in xs if x["validation_to_gate_ms"]]
                per = 100 * sum(min(1.0, rate * d) for d in vg) / len(vg)
                poi = 100 * sum(1 - math.exp(-rate * d) for d in vg) / len(vg)
                best = min((per - meas, "per"), (poi - meas, "poi"), key=lambda t: abs(t[0]))[0]
                if tag == "Jit":
                    devs.append(abs(best))
                    per_devs.append(abs(per - meas))
                if rate == 50.0:
                    macro(f"RevPhase{tag}{bl}Meas", f"{meas:.0f}")
                    macro(f"RevPhase{tag}{bl}Pred", f"{per:.0f}")
    if devs:
        macro("RevPhaseJitMaxDev", f"{max(devs):.1f}")
        macro("RevPhaseJitPerMaxDev", f"{max(per_devs):.1f}")


def fence_table(res: Path) -> None:
    out, ons = [], []
    for board, label in (("rpi4", "Raspberry Pi~4"), ("vf2", "VisionFive~2")):
        vals = {}
        for fence in (0, 1):
            fs = sorted(glob.glob(str(res / "embedded" / board / "campaigns" / f"fence{fence}" / "e4_load_http_rep*.csv")))
            vals[fence] = [float(rows(f)[0]["throughput_ips"]) for f in fs]
        old = [float(rows(f)[0]["throughput_ips"]) for f in sorted(glob.glob(str(ROOT / "data" / "execution-gap" / "embedded" / board / "e4_load_http_rep*.csv")))]
        if not vals[0] or not vals[1]:
            continue
        mean = lambda v: sum(v) / len(v)
        out.append(f"{label} & {rng(old)} & {rng(vals[0])} & {rng(vals[1])} & {100 * (1 - mean(vals[1]) / mean(vals[0])):.0f} \\\\")
        ons += vals[1]
        key = "Pi" if board == "rpi4" else "Vf"
        macro(f"RevFence{key}Off", rng(vals[0]))
        macro(f"RevFence{key}On", rng(vals[1]))
        macro(f"RevFence{key}Old", rng(old))
        macro(f"RevFence{key}Drop", f"{100 * (1 - mean(vals[1]) / mean(vals[0])):.0f}")
    if ons:
        macro("RevFenceOnRange", rng(ons))
    write_rows("tab_rev_fence.tex", out)


def repl_host(res: Path) -> None:
    out = []
    # PLC (E4-PLC): unconditional commands on a paused line; interlock and version-conditional command
    files = [ROOT / "data" / "execution-gap" / "openplc" / "e24_openplc.csv"] + sorted((res / "openplc" / "campaigns").glob("c*/e24_openplc.csv"))
    plain, other_k, other_n = [], 0, 0
    for f in files:
        r = [x for x in rows(f) if x["inject"] == "1"]
        p_ = [x for x in r if x["actuator"] == "openplc_plain"]
        plain.append(100 * sum(int(x["stale_effect"]) for x in p_) / len(p_))
        o = [x for x in r if x["actuator"] != "openplc_plain"]
        other_k += sum(int(x["stale_effect"]) for x in o)
        other_n += len(o)
    out.append(f"PLC & {len(files)} & {rng(plain)} & {other_k}/{other_n} & -- & -- \\\\")
    macro("RevReplPlcPlain", rng(plain))
    macro("RevReplPlcOtherK", other_k)
    macro("RevReplPlcOtherN", other_n)
    macro("RevReplPlcCampaigns", len(files))
    # E4-updates: share of valid commands not executed under a global controller version, by rate
    files = [ROOT / "data" / "execution-gap" / "e26_controller_churn.csv"] + sorted((res / "e26_campaigns").glob("c*.csv"))
    by = defaultdict(list)
    linek = linen = 0
    for f in files:
        c = defaultdict(lambda: [0, 0])
        for x in rows(f):
            if x["block"] != "valid":
                continue
            k = (x["scope"], int(float(x["rate_hz_per_var"])))
            c[k][0] += 1
            c[k][1] += int(x["executed"] in ("0", 0))
        for k, v in c.items():
            if k[0] == "global" and k[1] > 0:
                by[k[1]].append(100 * v[1] / v[0])
            if k[0] == "line":
                linek += v[1]
                linen += v[0]
    cells = " & ".join(rng(by[r]) for r in (20, 50, 100))
    out.append(f"E4-updates & {len(files)} & {cells} & {linek}/{linen} \\\\")
    for r in (20, 50, 100):
        macro(f"RevReplUpd{words(r)}", rng(by[r]))
    macro("RevReplUpdLineK", linek)
    macro("RevReplUpdLineN", linen)
    write_rows("tab_rev_repl2.tex", out)


def deadline_table(res: Path) -> None:
    out = []
    names = {"optimistic": "optimistic", "atomic": "atomic"}
    for sub, label in (("physical", "wide-area"), ("lan", "LAN")):
        fs = sorted((res / sub / "campaigns").glob("c*/e10_deadline_eps*_prod*_d*.csv"))
        groups = defaultdict(list)
        for f in fs:
            stem = f.stem
            eps = float(stem.split("eps")[1].split("_")[0])
            prod = float(stem.split("prod")[1].split("_")[0])
            d = float(stem.split("_d")[-1])
            groups[(d, prod, eps)].append(f)
        for (d, prod, eps), files in sorted(groups.items()):
            r = [x for f in files for x in rows(f)]
            for mode, nm in names.items():
                xs = [x for x in r if x["mode"] == mode]
                rel = [x for x in xs if x["gateway_released"] == "1"]
                late_c = sum(int(x["late_at_check"]) for x in rel)
                late_m = sum(int(x["late_at_middleware"]) for x in rel)
                worst = max((float(x["age_at_check_ms"]) - d for x in rel if x["age_at_check_ms"]), default=0.0)
                out.append(f"{label} & {int(d)} & {eps:g} & {prod:+g} & {nm} & {len(rel)}/{len(xs)} & {late_c} & {late_m} & {max(worst, 0):.0f} \\\\")
                tag = f"{label.replace('-', '').capitalize()}D{words(int(d))}E{words(int(eps))}P{words(prod if prod < 0 else float(prod))}{''.join(w.capitalize() for w in mode.split('_'))}"
                macro(f"RevDl{tag}Late", late_c)
                macro(f"RevDl{tag}Rel", len(rel))
                macro(f"RevDl{tag}N", len(xs))
                macro(f"RevDl{tag}Max", f"{max(worst, 0):.0f}")
    write_rows("tab_rev_deadline.tex", out)


def delay_degrade(res: Path) -> None:
    f = res / "review" / "e29_delay_degrade.csv"
    if not f.exists():
        return
    import statistics
    r = rows(f)
    for case, nm in (("delay", "Delay"), ("degrade", "Degrade")):
        xs = [x for x in r if x["case"] == case]
        macro(f"RevE{nm}Ok", sum(int(x["correct"]) for x in xs))
        macro(f"RevE{nm}N", len(xs))
        macro(f"RevE{nm}Ms", f"{statistics.median(float(x['first_ms']) for x in xs):.2f}")


def truth_index(res: Path) -> None:
    f = res / "review" / "e28_truth_scale_indexed.writer.json"
    if not f.exists():
        return
    d = json.load(open(f))
    for x in d:
        if x["layout"] == "hash" and x["rate_hz"] == 100:
            n = {1000: "Thousand", 10000: "TenThousand"}.get(x["registered"])
            if n:
                macro(f"RevIdx{n}Ms", f"{x['update_p50_ms']:.0f}")
                macro(f"RevIdx{n}Hz", f"{x['achieved_hz']:.1f}")


def legacy_table(res: Path) -> None:
    """Order of the first campaign (version read before the registration), rebuilt as an ablation (E25b-legacy)."""
    gaps = {("Pi", "h"): [], ("Pi", "i"): [], ("Vf", "h"): [], ("Vf", "i"): []}
    add = []
    for board, bl in (("rpi4", "Pi"), ("vf2", "Vf")):
        for kind in ("periodic", "poisson"):
            pat = "e16_eq3_seed*.csv" if kind == "periodic" else "e16_eq3_poisson_seed*.csv"
            allrows = []
            for fn in sorted(glob.glob(str(res / "embedded_eq3_legacy" / board / pat))):
                allrows += rows(fn)
            if not allrows:
                return
            for rate in (20.0, 50.0):
                xs = [x for x in allrows if float(x["target_rate_hz"]) == rate]
                meas = 100 * sum(x["gateway_released"] == "0" for x in xs) / len(xs)

                def model(d_ms):
                    d = d_ms / 1000.0
                    return 100 * ((1 - math.exp(-rate * d)) if kind == "poisson" else min(1.0, rate * d))
                vg = [float(x["validation_to_gate_ms"]) for x in xs if x["validation_to_gate_ms"]]
                dt = [((float(x["gate_read_lo_ms"]) + float(x["gate_read_hi_ms"])) - (float(x["val_read_lo_ms"]) + float(x["val_read_hi_ms"]))) / 2
                      for x in xs if x.get("gate_read_lo_ms") and x.get("val_read_lo_ms")]
                gaps[(bl, "h")].append(meas - sum(model(d) for d in vg) / len(vg))
                gaps[(bl, "i")].append(meas - sum(model(d) for d in dt) / len(dt))
                add.append(sorted(dt)[len(dt) // 2] - sorted(vg)[len(vg) // 2])
    for (bl, k), v in gaps.items():
        tag = "Instr" if k == "i" else "Harness"
        macro(f"RevLeg{bl}{tag}Low", f"{min(v):+.1f}")
        macro(f"RevLeg{bl}{tag}High", f"{max(v):+.1f}")
    first = res.parent / "embedded" if (res.parent / "embedded").exists() else res / "embedded"
    for board, bl in (("rpi4", "Pi"), ("vf2", "Vf")):
        for tag, base in (("Legacy", res / "embedded_eq3_legacy"), ("First", first)):
            xs = []
            for fn in sorted(glob.glob(str(base / board / "e16_eq3_seed*.csv"))):
                xs += [x for x in rows(fn) if float(x["target_rate_hz"]) == 20.0]
            if xs:
                macro(f"Rev{tag}{bl}MeasTwenty", f"{100 * sum(x['gateway_released'] == '0' for x in xs) / len(xs):.0f}")
    macro("RevLegAddMin", f"{min(add):.1f}")
    macro("RevLegAddMax", f"{max(add):.1f}")


def main() -> int:
    global GEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT / "experiments" / "results"))
    ap.add_argument("--out", default=str(GEN), help="directory for the generated rows and macros")
    a = ap.parse_args()
    res = Path(a.results)
    GEN = Path(a.out)
    GEN.mkdir(parents=True, exist_ok=True)
    aba_table(res)
    sampling_table(res)
    stop_table(res)
    replication_table(res)
    plc_wrap(res)
    e25_published(res)
    eq3_table(res)
    phase_lock(res)
    legacy_table(res)
    fence_table(res)
    repl_host(res)
    delay_degrade(res)
    truth_index(res)
    deadline_table(res)
    (GEN / "numbers_review.tex").write_text("".join(f"\\newcommand{{\\{k}}}{{{v}}}\n" for k, v in sorted(NUM.items())))
    docs = ROOT / "docs" / "detailed-results" / "generated"
    if GEN == ROOT / "journal" / "generated" and docs.exists():   # reference copy for the public tree
        (docs / "numbers_review.tex").write_text((GEN / "numbers_review.tex").read_text())
    print("review tables written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
