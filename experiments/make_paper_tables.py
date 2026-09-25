#!/usr/bin/env python3
"""Generate LaTeX tables and number macros from the frozen summary.

Writes ``numbers.tex`` (macros used by the manuscript) and one ``tab_*.tex``
file per supplementary table into ``--out``, so no figure in the paper is
typed by hand.
"""

from __future__ import annotations

import argparse
import json
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
                 "tab_e12_pinned", "tab_e12_dist", "tab_e16_trace", "tab_e10_modes", "tab_e16_dist", "tab_e12_both", "tab_e4_reps"):
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

    (args.out / "numbers.tex").write_text("\n".join(macros) + "\n")
    print(f"Wrote {len(macros)} macros and tables to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
