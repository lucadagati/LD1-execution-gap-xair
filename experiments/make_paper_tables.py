#!/usr/bin/env python3
"""Generate LaTeX tables and number macros from the frozen summary.

Writes ``numbers.tex`` (number macros) and one ``tab_*.tex`` file per table
into ``--out`` (the manuscript's or ``docs/detailed-results/generated``), so
no figure in the paper or in the detailed-results document is typed by hand.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from common import ROOT


def kn(x: dict) -> str:
    return f"{x['k']}/{x['n']}"


def pc(k: int, n: int) -> str:
    """Percentage, integer unless rounding would hide that it is not exactly 0 or 100."""
    v = 100 * k / n
    if v != round(v) and round(v) in (0, 100):
        return f"{v:.1f}"
    return f"{v:.0f}"


def sig(v: float) -> str:
    """Percentage with two significant figures below 10 and none above."""
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}" if v >= 1 else f"{v:.2g}"


def pct(k: int, n: int) -> str:
    return f"{round(100 * k / n)}\\%"


MODE_NAMES = {"direct": "Direct", "naive": "Freshness-only", "local": "Local (coherent)", "xair": "XAIR", "opa": "Policy engine"}
SCOPE_NAMES = {"global": "Global", "readset": "Read-set", "truth": "Predicate-truth", "predicate": "Predicate-only", "opa": "Policy engine", "opa_readset": "Policy engine, read-set"}
PATTERN_NAMES = {"unrelated": "unrelated field", "related_same": "same-value rewrite"}


def e8_numbers(d: dict) -> tuple[int, int, int, list[str]]:
    """Witness agreement and motion detection over campaigns whose ROS witness stayed alive."""
    agree = total = motion = released = 0
    used = []
    for camp, modes in sorted(d.get("e8_gazebo", {}).items()):
        # A campaign is usable for the witness analysis only if the subscriber
        # observed every released intent of at least one released mode
        # (a stalled subscriber reads zero from then on).
        ok = all(m["ros_observed"] == m["released"]["k"] for m in modes.values())
        if not ok:
            continue
        used.append(camp)
        for m in modes.values():
            agree += m["ros_witness_agrees"]["k"]
            total += m["ros_witness_agrees"]["n"]
            if m["released"]["k"]:
                motion += m["sim_motion"]["k"]
                released += m["released"]["k"]
    return agree, total, (motion, released), used


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, default=ROOT / "data" / "execution-gap" / "paper_metrics_summary.json")
    parser.add_argument("--out", type=Path, default=ROOT / "journal" / "generated")
    args = parser.parse_args()
    d = json.loads(args.summary.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    for name in ("tab_e6", "tab_e8", "tab_e11_cost", "tab_e13", "tab_e14", "tab_e16",
                 "tab_e12_pinned", "tab_e12_dist", "tab_e16_trace", "tab_e10_modes", "tab_e16_dist", "tab_e12_both", "tab_e4_reps", "tab_e10_summary", "tab_dist_extra", "tab_e1_all", "tab_e10_aba", "tab_e21", "tab_e10_deadline", "tab_e16_pooled", "tab_e20_offline", "tab_e20", "tab_e23", "tab_e24", "tab_e25",
                 "tab_e21_offsets", "tab_e20_sens", "tab_e20_rotation"):
        (args.out / f"{name}.tex").write_text("")  # suites without data yield empty tables
    macros: list[str] = []

    agree, total, (motion, rel), used = e8_numbers(d)
    macros += [f"\\newcommand{{\\EightWitness}}{{{agree}/{total}}}",
               f"\\newcommand{{\\EightMotion}}{{{motion}/{rel} ({pct(motion, rel) if rel else '--'})}}",
               f"\\newcommand{{\\EightCampaigns}}{{{len(used)}}}"]

    e6 = d.get("e6", {})
    short, long_ = e6.get("e6_network", {}), e6.get("e6_network_fresh10s", {})
    if short and long_:
        stale = sum(c["stale_on_drifted"]["k"] for v in e6.values() for c in v.values())
        temporal_cfgs = [k for k, c in short.items() if c["revoke_reasons_drifted"].get("temporal")]
        wrong_short = sum(c["wrongful_on_valid"]["k"] for c in short.values())
        n_short = sum(c["wrongful_on_valid"]["n"] for c in short.values())
        ctx_long = sum(c["revoke_reasons_drifted"].get("context", 0) for c in long_.values())
        n_long = sum(c["stale_on_drifted"]["n"] for c in long_.values())
        rel_long = sum(c["wrongful_on_valid"]["n"] - c["wrongful_on_valid"]["k"] for c in long_.values())
        nv_long = sum(c["wrongful_on_valid"]["n"] for c in long_.values())
        macros.append(
            "\\newcommand{\\SixResult}{"
            + (f"No drifted intent is released ({stale} stale releases in total). " if stale == 0 else f"{stale} drifted intents are released. ")
            + f"With a 500\\,ms freshness window, the {len(temporal_cfgs)} configurations with at least 50\\,ms of per-packet delay revoke every drifted intent for elapsed time rather than context, and valid controls are revoked too ({wrong_short}/{n_short} over all configurations); "
            + f"with a 10\\,s window {ctx_long}/{n_long} drifted intents are revoked on context and {rel_long}/{nv_long} controls are released. "
            + "Impairment thus never makes the gate fail open, but a freshness window shorter than the transport delay turns fail-closed into denial of service.}"
        )
        rows = []
        for key in short:
            a, b = short[key], long_.get(key, {})
            label = key.replace("d", "", 1).replace("_j", "/").replace("_l", "/")
            rs = lambda c: "; ".join(f"{r} {n}" for r, n in sorted(c["revoke_reasons_drifted"].items())) or "--"
            rows.append(f"{label} & {a['e2e_ms']['p50']:.0f} & {kn(a['stale_on_drifted'])} & {rs(a)} & {a['wrongful_on_valid']['n'] - a['wrongful_on_valid']['k']}/{a['wrongful_on_valid']['n']}"
                        + (f" & {rs(b)} & {b['wrongful_on_valid']['n'] - b['wrongful_on_valid']['k']}/{b['wrongful_on_valid']['n']}" if b else " & -- & --") + " \\\\")
        (args.out / "tab_e6.tex").write_text("\n".join(rows) + "\n")

    e14 = d.get("e14", {})
    if e14:
        scen = sorted({k.split("|")[0] for k in e14})
        (args.out / "tab_e14.tex").write_text("\n".join(
            f"\\texttt{{{s.replace('_', '\\_')}}} & " + " & ".join(kn(e14[f"{s}|{b}"]) for b in ("direct", "local", "xair")) + " \\\\"
            for s in scen) + "\n")

    e13 = d.get("e13", {})
    if e13:
        (args.out / "tab_e13.tex").write_text("\n".join(
            f"{f.replace('_', ' ')} & {kn(v)} \\\\" for f, v in e13["by_fault"].items()) + "\n")

    e11 = d.get("e11", {})
    if e11:
        (args.out / "tab_e11_cost.tex").write_text("\n".join(
            f"{k} & {v['n']} & {v['p50']:.3f} & {v['p95']:.3f} & {v['max']:.3f} \\\\"
            for k, v in sorted(e11["validation_cost_by_predicate_count_valid_only"].items(), key=lambda kv: int(kv[0]))) + "\n")

    for name, e16 in (("tab_e16", d.get("e16", {})), ("tab_e16_dist", d.get("distributed", {}).get("e16", {}))):
        if not e16:
            continue
        rows = []
        for k, v in e16.items():
            scope, pattern, rate = k.split("|")
            rows.append((float(rate), scope, pattern, v))
        rows.sort()
        (args.out / f"{name}.tex").write_text("\n".join(
            f"{SCOPE_NAMES.get(scope, scope)} & {PATTERN_NAMES.get(pattern, pattern)} & {rate:g} & {v['achieved_rate_hz']:.0f} & {kn(v['fpr'])} & {v['goodput_ips']:.1f} & {v['e2e_ms']['p99']:.1f} \\\\"
            for rate, scope, pattern, v in rows) + "\n")

    e8 = d.get("e8_gazebo", {})
    if e8:
        (args.out / "tab_e8.tex").write_text("\n".join(
            f"{c} & {MODE_NAMES.get(b, b)} & {kn(m['released'])} & {m['ros_observed']} & {kn(m['ros_witness_agrees'])} & {kn(m['sim_motion'])} \\\\"
            for c, modes in sorted(e8.items()) for b, m in sorted(modes.items())) + "\n")
        macros.append(f"\\newcommand{{\\EightCampaignsUsed}}{{{', '.join(used)}}}")

    fmt = lambda c: f"{c['median_throughput_ips']:.0f} / {c['median_p50_ms']:.1f} / {c['median_p99_ms']:.1f}"
    for name, e12 in (("tab_e12_pinned", d.get("pinned", {}).get("e12", {})),
                      ("tab_e12_dist", d.get("distributed", {}).get("e12", {}))):
        if e12:
            (args.out / f"{name}.tex").write_text("\n".join(
                f"{p} & {kb} & {fmt(e12[f'local_authoritative|{p}|{kb}'])} & {fmt(e12[f'xair|{p}|{kb}'])} \\\\"
                for p in (1, 10, 50) for kb in (1, 64) if f"xair|{p}|{kb}" in e12) + "\n")

    e12p, e12d = d.get("pinned", {}).get("e12", {}), d.get("distributed", {}).get("e12", {})
    if e12p and e12d:
        (args.out / "tab_e12_both.tex").write_text("\n".join(
            f"{p} & {kb} & " + " & ".join(fmt(e[f'{m}|{p}|{kb}']) for e in (e12p, e12d) for m in ("local_authoritative", "xair")) + " \\\\"
            for p in (1, 10, 50) for kb in (1, 64)) + "\n")
    reps = d.get("pinned", {}).get("e4_reps", [])
    if reps:
        (args.out / "tab_e4_reps.tex").write_text("; ".join(
            f"{r['throughput_ips']:.0f} intents/s, internal p50 {r['vl_internal_p50_ms']:.3f}\\,ms, end-to-end p50/p99 {r['vl_e2e_p50_ms']:.2f}/{r['vl_e2e_p99_ms']:.2f}\\,ms"
            for r in reps) + ".")

    dist = d.get("distributed", {})
    trace = dist.get("e16_trace", {})
    if trace:
        rows = []
        for iv in sorted({float(k.split("|")[0]) for k in trace}, reverse=True):
            rate_hz = max(v["achieved_update_rate_hz"] for k, v in trace.items() if float(k.split("|")[0]) == iv)
            vals = [kn(trace[f"{iv:g}|{sc}|{kind}"]["fpr"]) for sc in ("global", "readset", "predicate") for kind in ("discrete", "continuous")]
            rows.append(f"{iv:g} & {rate_hz:.0f} & " + " & ".join(vals) + " \\\\")
        (args.out / "tab_e16_trace.tex").write_text("\n".join(rows) + "\n")

    opt, atom = dist.get("e10_boundary", {}), dist.get("e10_atomic", {})
    if opt and atom:
        rows = []
        for off in opt["by_offset_ms"]:
            o, a = opt["by_offset_ms"][off], atom["by_offset_ms"].get(off)
            if not a:
                continue
            w = lambda c, k: c["windows"].get(k, 0)
            rows.append(f"{off} & {w(o, 'before_recheck')}/{w(o, 'concurrent')}/{w(o, 'after_release')} & {o['released']}/{o['injected']} & {o['potential_stale']}"
                        f" & {a['released']}/{a['injected']} & {a['stale']['k']} \\\\")
        (args.out / "tab_e10_modes.tex").write_text("\n".join(rows) + "\n")

    # ---- five-campaign tables (distributed testbed) ----
    camps = [dist] + list(dist.get("campaigns", {}).values()) if dist else []
    def pooled(key, fn):
        return sum(fn(c.get(key, {})) for c in camps if c.get(key))
    def ncamp(key):
        return sum(1 for c in camps if c.get(key))
    if camps and all(c.get("e10_atomic") for c in camps):
        vo = lambda k: (lambda e: e.get("version_ordering", {}).get(k, 0))
        pos = lambda k: (lambda e: e.get("position_vs_middleware", {}).get(k, 0))
        def cells(key):
            before = pooled(key, vo("blocked_before_check")) + pooled(key, vo("released_before_check"))
            blocked = pooled(key, vo("blocked_before_check"))
            rel = pooled(key, vo("released_after_check"))
            return (f"{blocked}/{before}", f"{rel}",
                    f"{pooled(key, pos('stale_at_middleware'))} / {pooled(key, pos('ambiguous'))} / {pooled(key, pos('after_middleware'))}")
        def rng(key, path):
            vals = []
            for c in camps:
                e = c.get(key, {})
                for k in path:
                    e = e.get(k, {}) if isinstance(e, dict) else {}
                if isinstance(e, (int, float)):
                    vals.append(e)
            return f"{min(vals):.2f}--{max(vals):.2f}" if vals else "--"
        rows = []
        for label, ko, ka in (("induced", "e10_boundary", "e10_atomic"), ("natural", "e10_natural", "e10_natural_atomic")):
            o, a_ = cells(ko), cells(ka)
            rows.append(f"\\multirow{{3}}{{*}}{{{label}}} & blocked / ordered before check & {o[0]} & {a_[0]} \\\\")
            rows.append(f" & released (write ordered after check) & {o[1]} & {a_[1]} \\\\")
            rows.append(f" & write before / overlapping / after $t_m$ & {o[2]} & {a_[2]} \\\\")
        rows.append("\\midrule")
        rows.append(f"\\multicolumn{{2}}{{@{{}}l}}{{check-to-$t_m$, lower bound p50 [ms]}} & {rng('e10_natural', ['residual_bounds_ms', 'lo', 'p50'])} & {rng('e10_natural_atomic', ['residual_bounds_ms', 'lo', 'p50'])} \\\\")
        rows.append(f"\\multicolumn{{2}}{{@{{}}l}}{{check-to-$t_m$, upper bound p50 [ms]}} & {rng('e10_natural', ['residual_bounds_ms', 'hi', 'p50'])} & {rng('e10_natural_atomic', ['residual_bounds_ms', 'hi', 'p50'])} \\\\")
        rows.append(f"\\multicolumn{{2}}{{@{{}}l}}{{validation to release p50 [ms]}} & {rng('e10_natural', ['validation_to_release_ms_controls', 'p50'])} & {rng('e10_natural_atomic', ['validation_to_release_ms_controls', 'p50'])} \\\\")
        (args.out / "tab_e10_summary.tex").write_text("\n".join(rows) + "\n")
        macros.append(f"\\newcommand{{\\TenCampaigns}}{{{len(camps)}}}")
    if camps and all(c.get("e16_trace") for c in camps):
        rows = []
        keys = camps[0]["e16_trace"].keys()
        ivs = sorted({float(k.split("|")[0]) for k in keys}, reverse=True)
        gates = (("global", "Global version"), ("readset", "Read-set version"), ("truth", "Predicate-truth version"),
                 ("predicate", "Predicate-only"), ("opa", "OPA, predicates only"), ("opa_readset", "OPA, read-set version"))
        for sc, lab in gates:
            vals = []
            for iv in ivs:
                for kind in ("discrete", "continuous"):
                    ks = [c["e16_trace"][f"{iv:g}|{sc}|{kind}"]["fpr"] for c in camps if f"{iv:g}|{sc}|{kind}" in c["e16_trace"]]
                    k, n = sum(x["k"] for x in ks), sum(x["n"] for x in ks)
                    vals.append(pc(k, n) if n else "--")
            if any(v != "--" for v in vals):
                rows.append(f"{lab} & " + " & ".join(vals) + " \\\\")
        (args.out / "tab_e16_trace.tex").write_text("\n".join(rows) + "\n")
        n_trace = sum(c["e16_trace"][next(iter(keys))]["fpr"]["n"] for c in camps)
        macros.append(f"\\newcommand{{\\TraceN}}{{{n_trace}}}")
        for iv in ivs:
            rate_hz = statistics.fmean(c["e16_trace"][f"{iv:g}|global|discrete"]["achieved_update_rate_hz"] for c in camps)
            macros.append(f"\\newcommand{{\\TraceRate{['A', 'B', 'C'][ivs.index(iv)]}}}{{{rate_hz:.0f}}}")

    phys = d.get("physical", {})
    lan = d.get("lan", {})
    pooled = lambda key, sub, field: (sum(c.get(key, {}).get(sub, {}).get(field, {}).get("k", 0) for c in camps),
                                      sum(c.get(key, {}).get(sub, {}).get(field, {}).get("n", 0) for c in camps))
    frac = lambda kn_: f"{kn_[0]}/{kn_[1]}" if kn_[1] else "--"
    # E1 across testbeds
    if d.get("e1"):
        names = {"direct": "Direct", "naive": "Freshness-only", "local": "Local (coherent)", "xair": "XAIR", "opa": "Policy engine (OPA)"}
        rows = []
        for m in ("direct", "naive", "local", "xair", "opa"):
            cells = []
            for src in (d, dist, phys, lan):
                e = src.get("e1", {}).get(m)
                cells.append(f"{kn(e['SER'])} / {e['e2e_latency_ms']['p50']:.1f}" if e else "--")
            rows.append(f"{names[m]} & " + " & ".join(cells) + " \\\\")
        (args.out / "tab_e1_all.tex").write_text("\n".join(rows) + "\n")
    # E10-ABA
    if dist.get("e10_aba"):
        labels = {"xair|readset": "XAIR, read-set version", "xair|truth": "XAIR, predicate-truth version",
                  "xair|predicate": "XAIR, predicate-only", "xair_atomic|readset": "XAIR atomic, read-set version",
                  "xair_atomic|truth": "XAIR atomic, predicate-truth version", "opa|opa": "Policy engine (OPA), predicates only",
                  "opa|opa_readset": "Policy engine (OPA), read-set version"}
        rows = []
        for g, lab in labels.items():
            pk = phys.get("e10_aba", {}).get(g, {}).get("released_after_undone_change")
            lk = lan.get("e10_aba", {}).get(g, {}).get("released_after_undone_change")
            rows.append(f"{lab} & {frac(pooled('e10_aba', g, 'released_after_undone_change'))} & "
                        f"{frac(pooled('e10_aba', g, 'controls_released'))} & {kn(pk) if pk else '--'} & {kn(lk) if lk else '--'} \\\\")
        (args.out / "tab_e10_aba.tex").write_text("\n".join(rows) + "\n")
    # E21: pause trials (stale effects) and pause-resume trials (history violations), by release path
    PATHS21 = (("xair|plc_plain", "optimistic, unconditional"), ("xair_atomic|plc_plain", "atomic, unconditional"),
               ("xair|plc_predicate", "optimistic, local interlock"), ("xair_atomic|plc_predicate", "atomic, local interlock"),
               ("xair|plc_conditional", "optimistic, version-conditional"), ("xair_atomic|plc_conditional", "atomic, version-conditional"))
    if dist.get("e21"):
        rows = []
        for g, lab in PATHS21:
            ref = sum(c.get("e21", {}).get(g, {}).get("refused_by_controller", 0) for c in camps)
            pe, le = phys.get("e21", {}).get(g, {}), lan.get("e21", {}).get(g, {})
            cells = [frac(pooled('e21', g, 'stale_effects')),
                     frac(pooled('e21_aba', g, 'stale_effects')), frac(pooled('e21_aba', g, 'history_violations')),
                     kn(pe['stale_effects']) if pe else "--", kn(le['stale_effects']) if le else "--"]
            le_aba = lan.get("e21_aba", {}).get(g, {})
            cells.append(kn(le_aba["history_violations"]) if le_aba else "--")
            rows.append(f"{lab} & " + " & ".join(cells) + " \\\\")
        (args.out / "tab_e21.tex").write_text("\n".join(rows) + "\n")
        # stale effects of unconditional commands by pause offset (the window a check on a copy leaves open)
        def by_off(srcs, key, g):
            acc: dict = {}
            for c in srcs:
                for o, r in c.get(key, {}).get(g, {}).get("by_offset", {}).items():
                    a = acc.setdefault(float(o), [0, 0]); a[0] += r["k"]; a[1] += r["n"]
            return acc
        rows = []
        for lab, srcs, key, g in (("Distributed (E21)", camps, "e21", "xair|plc_plain"),
                                  ("Wide area (E22)", [phys], "e21", "xair|plc_plain"),
                                  ("Wi-Fi LAN (E22)", [lan], "e21", "xair|plc_plain"),
                                  ("OpenPLC (E24)", [d.get("openplc", {})], "e24", "xair|openplc_plain")):
            acc = by_off(srcs, key, g)
            if acc:
                rows.append(f"{lab} & " + "; ".join(f"{o:g}: {100 * k / n:.0f}" for o, (k, n) in sorted(acc.items())) + " \\\\")
        (args.out / "tab_e21_offsets.tex").write_text("\n".join(rows) + "\n")
    # E10-deadline
    if dist.get("e10_deadline"):
        labels = {"optimistic": "optimistic", "atomic": "atomic", "atomic_guard": "atomic + release guard",
                  "atomic_margin3": "atomic + 3\\,ms commit margin"}
        rows = []
        for m, lab in labels.items():
            cs = [c["e10_deadline"][m] for c in camps if m in c.get("e10_deadline", {})]
            if not cs:
                continue
            ctm = [x["check_to_middleware_ms"]["p50"] for x in cs]
            rows.append(f"{lab} & {sum(x['trials'] for x in cs)} & {sum(x['released'] for x in cs)} & "
                        f"{sum(x['late_at_check'] for x in cs)} & {sum(x['late_at_middleware'] for x in cs)} & "
                        + (f"{min(ctm):.2f}" if f"{min(ctm):.2f}" == f"{max(ctm):.2f}" else f"{min(ctm):.2f}--{max(ctm):.2f}") + " \\\\")
        (args.out / "tab_e10_deadline.tex").write_text("\n".join(rows) + "\n")
    # E16 pooled
    if camps and camps[0].get("e16"):
        rows = []
        for sc, lab in (("global", "Global version"), ("readset", "Read-set version"), ("truth", "Predicate-truth version"), ("opa", "Policy engine (OPA)"),
                        ("opa_readset", "OPA, read-set version")):
            vals = []
            for rate in ("20", "100", "500"):
                k = n = 0
                for c in camps:
                    for pat in ("unrelated", "related_same"):
                        e = c["e16"].get(f"{sc}|{pat}|{rate}")
                        if e:
                            k += e["fpr"]["k"]; n += e["fpr"]["n"]
                vals.append(pc(k, n) if n else "--")
            rows.append(f"{lab} & " + " & ".join(vals) + " \\\\")
        (args.out / "tab_e16_pooled.tex").write_text("\n".join(rows) + "\n")
    # E20
    if dist.get("e20"):
        off = dist["e20_offline"]
        ks = ("1", "10", "60", "300", "1800", "3600")
        (args.out / "tab_e20_offline.tex").write_text(
            " & ".join(f"{100 * off[k]['obsolete_fraction']:.3g}" for k in ks) + " \\\\\n"
            + "Interior idle intervals only [\\%] & " + " & ".join(f"{100 * off[k].get('interior_fraction', float('nan')):.3g}" for k in ks) + " \\\\\n")
        labels = {"direct|": "Direct", "xair|global": "XAIR, global", "xair|readset": "XAIR, read-set",
                  "xair|truth": "XAIR, predicate-truth", "xair_atomic|truth": "XAIR atomic, predicate-truth"}
        rows = []
        for g, lab in labels.items():
            cells = []
            for L in ("30", "240"):
                e = dist["e20"].get(f"{L}|{g}")
                cells.append(f"{kn(e['stale_released'])} & {kn(e['false_revocations'])}" if e else "-- & --")
            rows.append(f"{lab} & " + " & ".join(cells) + " \\\\")
        # the v1.6 rotation design (one gate per decision), kept for the detailed results
        (args.out / "tab_e20_rotation.tex").write_text("\n".join(rows) + "\n")
        pdist = dist.get("e20_paired")
        if pdist:
            rows = []
            for g, lab in labels.items():
                cells = []
                for L in ("30", "240"):
                    e = pdist.get(L, {}).get("gates", {}).get(g)
                    cells.append(f"{kn(e['stale_released'])} & {kn(e['false_revocations'])}" if e else "-- & --")
                rows.append(f"{lab} & " + " & ".join(cells) + " \\\\")
            for L, name in (("30", "Thirty"), ("240", "TwoForty")):
                c = pdist.get(L, {})
                for key, mac in (("decisions", "Dec"), ("obsolete", "Obs"), ("valid", "Val"), ("label_discordant", "Disc")):
                    macros.append(f"\\newcommand{{\\TwentyP{mac}{name}}}{{{c.get(key, '--')}}}")
        (args.out / "tab_e20.tex").write_text("\n".join(rows) + "\n")
    sens = dist.get("e20_sensitivity", {})
    if sens:
        lat = ("60", "300", "1800", "3600")
        rows_def = (("uniform", "any", "Decisions uniform over idle time"),
                    ("uniform_le8h", "any", "\\quad idle intervals $\\le$\\,8\\,h only"),
                    ("idle_start", "any", "One decision when a machine becomes idle"),
                    ("uniform", "variant", "Uniform; next step is a different part"),
                    ("uniform_le8h", "variant", "\\quad idle intervals $\\le$\\,8\\,h only"),
                    ("idle_start", "variant", "At idle; next step is a different part"),
                    ("uniform", "breakdown", "Uniform; next step is a breakdown"))
        rows = []
        for model, proxy, lab in rows_def:
            vals = [sens.get(f"{L}|{model}|{proxy}", {}).get("fraction") for L in lat]
            rows.append(f"{lab} & " + " & ".join(sig(100 * v) if v is not None else "--" for v in vals) + " \\\\")
        (args.out / "tab_e20_sens.tex").write_text("\n".join(rows) + "\n")
        u = sens.get("3600|uniform|any", {})
        if u:
            macros.append(f"\\newcommand{{\\TwentyPerResource}}{{{100 * u['per_resource_min']:.2g}--{100 * u['per_resource_max']:.2g}}}")
    if dist and dist.get("e18"):
        e17 = dist.get("e17", {})
        pol = lambda k: kn(e17[k]) if k in e17 else "--"
        e18 = dist["e18"]
        names = {"gateway_crash_after_commit": "gateway crash after commit", "duplicate_commit": "retried commit",
                 "consumer_crash_after_apply": "consumer crash after effect", "consumer_restarts": "consumer restarts",
                 "concurrent_commits": "8 concurrent committers", "post_commit_invalidation": "read-set change after commit"}
        f18 = "\n".join(f"{names.get(k, k)} & {v['committed']} & {v['effects']} & {v['duplicate_effects']} & {v['lost']} \\\\" for k, v in e18.items())
        (args.out / "tab_dist_extra.tex").write_text(
            "E17 (released / trials): without policy, drift " + pol("0|drift") + ", valid " + pol("0|valid")
            + "; with policy, drift " + pol("1|drift") + ", valid " + pol("1|valid") + ".\n\n"
            "E18 (10 repetitions of 100 commits per scenario; emulated crashes).\n"
            "\\begin{center}\\small\\begin{tabular}{@{}lrrrr@{}}\\toprule\n"
            "\\textbf{Scenario} & \\textbf{Committed} & \\textbf{Effects} & \\textbf{Duplicates} & \\textbf{Lost} \\\\\n\\midrule\n"
            + f18 + "\n\\bottomrule\\end{tabular}\\end{center}\n")

    e23 = d.get("scaling", {}).get("e23")
    if e23:
        med = lambda xs: statistics.median(xs) if xs else float("nan")
        cell23 = lambda lay, n, f: [x for x in e23 if x["layout"] == lay and int(x["context_paths"]) == n and abs(x["fraction_touched"] - f) < 1e-9]
        rows = []
        for n in sorted({int(r["context_paths"]) for r in e23 if r["context_paths"] > 0}):
            base = cell23("none", n, 0.0)
            for lay in ("document", "hash"):
                for f in (0.0, 0.1, 1.0):
                    rs = cell23(lay, n, f)
                    if not rs:
                        continue
                    obs = int(sum(x.get("observations", x["updates"]) for x in rs))
                    mx = max(x.get("update_max_ms", x["update_p99_ms"]) for x in rs)
                    rng23 = lambda xs: f"{min(xs):.1f}--{max(xs):.1f}" if f"{min(xs):.1f}" != f"{max(xs):.1f}" else f"{xs[0]:.1f}"
                    rows.append(f"{n} & {lay} & {100 * f:g} & {rng23([x['update_p50_ms'] for x in rs])} & {mx:.1f} & {obs} & "
                                f"{med([x['updates_per_s'] for x in rs]):.0f} & {med([x['register_read_p50_ms'] for x in rs]):.2f} & "
                                f"{med([(x['snapshot_bytes'] + x['predicate_hash_bytes']) / 1024 for x in rs]):.0f} & "
                                f"{rng23([x['update_p50_ms'] for x in base]) if base else '--'} \\\\")
        (args.out / "tab_e23.tex").write_text("\n".join(rows) + "\n")
    e24 = d.get("openplc", {}).get("e24")
    if e24:
        labels = {"xair|openplc_plain": "optimistic, unconditional", "xair_atomic|openplc_plain": "atomic, unconditional",
                  "xair|openplc_predicate": "optimistic, local interlock", "xair_atomic|openplc_predicate": "atomic, local interlock",
                  "xair|openplc_conditional": "optimistic, version-conditional", "xair_atomic|openplc_conditional": "atomic, version-conditional"}
        (args.out / "tab_e24.tex").write_text("\n".join(
            f"{lab} & {kn(e24[g]['stale_effects'])} & {e24[g]['refused_by_controller']} & {e24[g]['blocked_by_xair']} & {kn(e24[g]['controls_executed'])} & {e24[g]['e2e_ms_controls']['p50']:.0f} \\\\"
            for g, lab in labels.items() if g in e24) + "\n")

    # E25: the runtime on embedded boards (everything on one board, loopback, one core per process)
    emb = d.get("embedded", {})
    if emb:
        names = {"rpi4": "Raspberry Pi 4, first campaign", "vf2": "VisionFive 2, first campaign"}
        rows = []
        for b in ("rpi4", "vf2"):
            e = emb.get(b)
            if not e:
                continue
            reps = e.get("e4_reps", [])
            thr = [r["throughput_ips"] for r in reps]
            e2e = statistics.median(r["vl_e2e_p50_ms"] for r in reps)
            internal = statistics.median(r["vl_internal_p50_ms"] for r in reps)
            cost = e["e11"]["validation_cost_by_predicate_count_valid_only"]
            vr = lambda k: e[k]["validation_to_release_ms_controls"]["p50"]
            gl = lambda rate: sum(e["e16"][f"global|{p}|{rate}"]["fpr"]["k"] for p in ("unrelated", "related_same"))
            gn = lambda rate: sum(e["e16"][f"global|{p}|{rate}"]["fpr"]["n"] for p in ("unrelated", "related_same"))
            ver = sum(e["e16"][f"{sc}|{p}|{r}"]["fpr"]["k"] for sc in ("readset", "truth") for p in ("unrelated", "related_same") for r in ("20", "100"))
            dg = statistics.median(e["e16"][f"global|{p}|100"]["validation_to_gate_ms"]["p50"] for p in ("unrelated", "related_same"))
            rss = e.get("environment", {}).get("rss_mb", {}).get("xair")
            rows.append(f"{names.get(b, b)} & {min(thr):.0f}--{max(thr):.0f} & {e2e:.1f} & "
                        f"{100 * gl('20') / gn('20'):.0f} / {100 * gl('100') / gn('100'):.0f} & {dg:.1f} & {rss if rss is not None else '--'} \\\\")
        (args.out / "tab_e25.tex").write_text("\n".join(rows) + "\n")
        for b, tag in (("rpi4", "Pi"), ("vf2", "Vf")):
            e = emb.get(b)
            if e:
                thr = [r["throughput_ips"] for r in e["e4_reps"]]
                macros.append(f"\\newcommand{{\\Emb{tag}Ips}}{{{min(thr):.0f}--{max(thr):.0f}}}")
    (args.out / "numbers.tex").write_text("\n".join(macros) + "\n")
    print(f"Wrote {len(macros)} macros and tables to {args.out}")
    # Every generated row ends with \genrule: an \hline in the paper's grid tables,
    # empty in the detailed-results document (\providecommand{\genrule}{}).
    for f in sorted(args.out.glob("tab_*.tex")):
        lines = f.read_text().split("\n")
        f.write_text("\n".join(ln + " \\genrule" if ln.rstrip().endswith("\\\\") else ln for ln in lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
