#!/usr/bin/env python3
"""Generate LaTeX tables and number macros from the frozen summary.

Writes ``numbers.tex`` (macros used by the manuscript) and one ``tab_*.tex``
file per supplementary table into ``--out``, so no figure in the paper is
typed by hand.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from common import ROOT


def kn(x: dict) -> str:
    return f"{x['k']}/{x['n']}"


def pct(k: int, n: int) -> str:
    return f"{round(100 * k / n)}\\%"


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
                 "tab_e12_pinned", "tab_e12_dist", "tab_e16_trace", "tab_e10_modes", "tab_e16_dist", "tab_e12_both", "tab_e4_reps", "tab_e10_summary", "tab_dist_extra"):
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
            f"\\texttt{{{f.replace('_', '\\_')}}} & {kn(v)} \\\\" for f, v in e13["by_fault"].items()) + "\n")

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
            f"{scope} & {pattern.replace('_', ' ')} & {rate:g} & {v['achieved_rate_hz']:.0f} & {kn(v['fpr'])} & {v['goodput_ips']:.1f} & {v['e2e_ms']['p99']:.1f} \\\\"
            for rate, scope, pattern, v in rows) + "\n")

    e8 = d.get("e8_gazebo", {})
    if e8:
        (args.out / "tab_e8.tex").write_text("\n".join(
            f"{c} & {b} & {kn(m['released'])} & {m['ros_observed']} & {kn(m['ros_witness_agrees'])} & {kn(m['sim_motion'])} \\\\"
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
        dl = lambda m, k: sum(c.get("e10_deadline", {}).get(m, {}).get(k, 0) for c in camps)
        rows.append(f"\\multicolumn{{2}}{{@{{}}l}}{{deadline: late at check / at $t_m$}} & {dl('xair', 'late_at_check')} / {dl('xair', 'late_at_middleware')} & {dl('xair_atomic', 'late_at_check')} / {dl('xair_atomic', 'late_at_middleware')} \\\\")
        (args.out / "tab_e10_summary.tex").write_text("\n".join(rows) + "\n")
        macros.append(f"\\newcommand{{\\TenCampaigns}}{{{len(camps)}}}")
    if camps and all(c.get("e16_trace") for c in camps):
        rows = []
        keys = camps[0]["e16_trace"].keys()
        for iv in sorted({float(k.split("|")[0]) for k in keys}, reverse=True):
            vals = []
            for sc in ("global", "readset", "predicate"):
                for kind in ("discrete", "continuous"):
                    ks = [c["e16_trace"][f"{iv:g}|{sc}|{kind}"]["fpr"] for c in camps]
                    k, n = sum(x["k"] for x in ks), sum(x["n"] for x in ks)
                    vals.append(f"{100 * k / n:.0f}" if k else "0")
            rate_hz = statistics.fmean(c["e16_trace"][f"{iv:g}|global|discrete"]["achieved_update_rate_hz"] for c in camps)
            rows.append(f"{iv:g} & {rate_hz:.0f} & " + " & ".join(vals) + " \\\\")
        (args.out / "tab_e16_trace.tex").write_text("\n".join(rows) + "\n")
        n_trace = sum(c["e16_trace"][next(iter(keys))]["fpr"]["n"] for c in camps)
        macros.append(f"\\newcommand{{\\TraceN}}{{{n_trace}}}")

    if dist and dist.get("e18"):
        lines = []
        dlines = []
        for m, label in (("xair", "optimistic"), ("xair_atomic", "atomic")):
            t = sum(c.get("e10_deadline", {}).get(m, {}).get("trials", 0) for c in camps)
            r = sum(c.get("e10_deadline", {}).get(m, {}).get("released", 0) for c in camps)
            lc = sum(c.get("e10_deadline", {}).get(m, {}).get("late_at_check", 0) for c in camps)
            lm = sum(c.get("e10_deadline", {}).get(m, {}).get("late_at_middleware", 0) for c in camps)
            ctm = [c["e10_deadline"][m]["check_to_middleware_ms"]["p50"] for c in camps if c.get("e10_deadline")]
            dlines.append(f"{label} & {t} & {r} & {lc} & {lm} & {min(ctm):.2f}--{max(ctm):.2f} \\\\")
        e17 = dist.get("e17", {})
        pol = lambda k: kn(e17[k]) if k in e17 else "--"
        e18 = dist["e18"]
        names = {"gateway_crash_after_commit": "gateway crash after commit", "duplicate_commit": "retried commit",
                 "consumer_crash_after_apply": "consumer crash after effect", "consumer_restarts": "consumer restarts",
                 "concurrent_commits": "8 concurrent committers", "post_commit_invalidation": "read-set change after commit"}
        f18 = "\n".join(f"{names.get(k, k)} & {v['committed']} & {v['effects']} & {v['duplicate_effects']} & {v['lost']} \\\\" for k, v in e18.items())
        (args.out / "tab_dist_extra.tex").write_text(
            "E10-deadline (deadline 100\\,ms, five campaigns): late releases by the age at the check and at $t_m$.\n"
            "\\begin{center}\\small\\begin{tabular}{@{}lrrrrc@{}}\\toprule\n"
            "\\textbf{Mode} & \\textbf{Trials} & \\textbf{Released} & \\textbf{Late at check} & \\textbf{Late at $t_m$} & \\textbf{Check to $t_m$ p50 [ms]} \\\\\n\\midrule\n"
            + "\n".join(dlines) + "\n\\bottomrule\\end{tabular}\\end{center}\n"
            "E17 (released / trials): without policy, drift " + pol("0|drift") + ", valid " + pol("0|valid")
            + "; with policy, drift " + pol("1|drift") + ", valid " + pol("1|valid") + ".\n\n"
            "E18 (10 repetitions of 100 commits per scenario; the last scenario uses a consumer that rechecks at apply time and withholds changed commands).\n"
            "\\begin{center}\\small\\begin{tabular}{@{}lrrrr@{}}\\toprule\n"
            "\\textbf{Scenario} & \\textbf{Committed} & \\textbf{Effects} & \\textbf{Duplicates} & \\textbf{Lost} \\\\\n\\midrule\n"
            + f18 + "\n\\bottomrule\\end{tabular}\\end{center}\n")
    (args.out / "numbers.tex").write_text("\n".join(macros) + "\n")
    print(f"Wrote {len(macros)} macros and tables to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
