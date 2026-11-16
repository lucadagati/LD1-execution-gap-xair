#!/usr/bin/env python3
"""Key-results figure for the main text, from the frozen summary.

(a) valid intents revoked under 100 Hz synthetic churn, by gate (E16, five campaigns);
(b) the same for continuous intents on the replayed hydraulic trace at 10 ms (E16-trace);
(c) controller-owned context on the distributed testbed (E21, optimistic release): effects on a
    paused line and history violations after a pause and resume, by release path;
(d) stale effects of unconditional commands by pause offset: the window a check on a copy leaves open.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from common import ROOT  # noqa: E402

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"   # validated categorical slots 1-3 (all-pairs)
PURPLE = "#7b5ea7"                                      # slot 4, for the fourth testbed in (c)
# colour code shared with the paper's figures: components (XAIR orange, store and gateway blue, controller green,
# producers grey); red marks failures (revocations, stale effects), gold a warning
RED, GOLD, GREY = "#c62e2e", "#b07d12", "#6b6a66"
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def pooled(camps, key, cell, field="fpr"):
    k = n = 0
    for c in camps:
        e = c.get(key, {}).get(cell)
        if e:
            k += e[field]["k"]; n += e[field]["n"]
    return 100 * k / n if n else float("nan"), k, n


def hbars(ax, labels, values, color, title):
    y = range(len(labels))[::-1]
    ax.barh(list(y), values, color=color, height=0.55, edgecolor="white", linewidth=1.5)
    for yi, v in zip(y, values):
        ax.text(v + 2, yi, f"{v:.0f}%", va="center", ha="left", fontsize=8, color=INK)
    ax.set_yticks(list(y), labels, fontsize=8, color=INK)
    ax.set_xlim(0, 110)
    ax.set_xlabel("valid intents revoked [%]", fontsize=8, color=MUTED)
    ax.set_title(title, fontsize=9, color=INK, loc="left")


def style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", type=Path, default=ROOT / "data" / "execution-gap" / "paper_metrics_summary.json")
    ap.add_argument("--out", type=Path, default=ROOT / "journal" / "figures" / "key_results.pdf")
    a = ap.parse_args()
    d = json.loads(a.summary.read_text())
    dist = d["distributed"]
    camps = [dist] + list(dist.get("campaigns", {}).values())

    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.titlepad": 4})
    fig = plt.figure(figsize=(7.2, 4.15))
    gs = fig.add_gridspec(2, 2, height_ratios=[1.45, 1.0], hspace=0.6, wspace=0.6)
    axes = [fig.add_subplot(gs[0, 0]), fig.add_subplot(gs[0, 1]), fig.add_subplot(gs[1, :])]

    # (a) false revocations: one row per gate, one bar per workload (one hue, three tints)
    ax = axes[0]
    gates = [("global", "Global version"), ("readset", "Read-set version"), ("truth", "Predicate-truth version"),
             ("opa", "OPA, no version"), ("opa_readset", "OPA, read-set version")]
    loads = [("Synthetic, 100 Hz", lambda g: [f"{g}|unrelated|100", f"{g}|related_same|100"], "e16", RED, None),
             ("Trace, discrete", lambda g: [f"10|{g}|discrete"], "e16_trace", "#e9a3a3", None),
             ("Trace, continuous", lambda g: [f"10|{g}|continuous"], "e16_trace", "#ffffff", "////")]
    h = 0.32
    ys = [1.4 * i for i in range(len(gates))][::-1]
    for j, (lab, cells, key, col, hatch) in enumerate(loads):
        for yi, (g, _) in zip(ys, gates):
            k = n = 0
            for cell in cells(g):
                v = pooled(camps, key, cell)
                k += v[1]; n += v[2]
            r = 100 * k / n if n else 0
            y = yi + (1 - j) * h
            ax.barh(y, r, height=h * 0.92, color=col, edgecolor=RED, linewidth=0.8, hatch=hatch,
                    label=lab if yi == ys[0] else None)
            if r == 0:
                ax.plot([0], [y], marker="D", markersize=4, color=col, markeredgecolor=RED, markeredgewidth=0.8, clip_on=False)
            else:
                ax.text(r + 2, y, f"{r:.0f}%", va="center", fontsize=6.3, color=INK)
    ax.set_yticks(ys, [g[1] for g in gates], fontsize=7.5, color=INK)
    ax.set_xlim(0, 100)
    ax.set_ylim(min(ys) - 0.6, max(ys) + 0.6)
    ax.set_xlabel("valid intents revoked [%]", fontsize=8, color=MUTED)
    ax.set_title("(a) False revocations under frequent updates", fontsize=9, color=INK, loc="left")
    ax.legend(fontsize=6.8, frameon=False, loc="lower right", handlelength=1.4)

    # (b) controller-side release paths (entity colors fixed: slots 1-3)
    ax = axes[1]
    paths = [("xair|plc_plain", "unconditional", GREY, None), ("xair|plc_predicate", "local interlock", GOLD, "////"),
             ("xair|plc_conditional", "version-conditional", AQUA, "\\\\")]
    # stale effects of the three paths are zero for the interlock and the conditional command by construction
    # and, for the unconditional command, depend on where the pauses fall; (c) shows that dependence by offset
    metrics = [("e21_aba", "history_violations", "pause, resume:\nhistory violation")]
    h = 0.24
    ys = [0]
    for yi, (key, field, mlab) in zip(ys, metrics):
        for j, (g, plab, col, hatch) in enumerate(paths):
            v, k, n = pooled(camps, key, g, field)
            y = yi + (1 - j) * h
            ax.barh(y, v, height=h * 0.9, color=col, edgecolor="white", linewidth=1.5, hatch=hatch,
                    label=plab if yi == ys[0] else None)
            if v == 0:
                ax.plot([0], [y], marker="D", markersize=6, color=col, markeredgecolor=INK, markeredgewidth=0.6, clip_on=False)
            if v:
                ax.text(v + 1.5, y, f"{v:.1f}%", va="center", fontsize=7, color=INK)
    ax.set_yticks(ys, [m[2] for m in metrics], fontsize=7.5, color=INK)
    ax.set_xlim(0, 100)
    ax.set_xlabel("injected trials [%]", fontsize=8, color=MUTED)
    ax.set_title("(b) Pause, then resume", fontsize=9, color=INK, loc="left")
    ax.legend(fontsize=6.8, frameon=False, loc="lower right", handlelength=1.4)

    # (c) unconditional commands: stale-effect rate by pause offset, one colour, marker and dash per testbed
    ax = axes[2]
    series = [("Distributed (E4)", camps, "e21", "xair|plc_plain", "o", "-", RED),
              ("OpenPLC (E4-PLC)", [d.get("openplc", {})], "e24", "xair|openplc_plain", "D", ":", "#7a1a1a"),
              ("Local LAN (E4)", [d.get("lan", {})], "e21", "xair|plc_plain", "s", "--", "#e07a7a"),
              ("Wide area (E4)", [d.get("physical", {})], "e21", "xair|plc_plain", "^", "-.", "#3a0d0d")]
    for lab, srcs, key, g, mk, ls, colour in series:
        acc: dict = {}
        for c in srcs:
            for o, r in c.get(key, {}).get(g, {}).get("by_offset", {}).items():
                a_ = acc.setdefault(float(o), [0, 0]); a_[0] += r["k"]; a_[1] += r["n"]
        if not acc:
            continue
        xs = sorted(acc)
        rates = [acc[x][0] / acc[x][1] for x in xs]
        ax.plot(xs, [100 * r for r in rates], color=colour, linewidth=0, linestyle='none', marker=mk,
                markersize=4.5, markeredgecolor="white", markeredgewidth=0.6, label=lab)
    ax.set_xscale("symlog", linthresh=10, linscale=0.8)
    ax.set_xlim(0, 230)
    ax.set_xticks([0, 5, 10, 20, 50, 100, 200], ["0", "5", "10", "20", "50", "100", "200"])
    ax.minorticks_off()
    ax.set_ylim(0, 112)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_xlabel("pause written after submission [ms]", fontsize=8, color=MUTED)
    ax.set_ylabel("stale effects [%]", fontsize=8, color=MUTED)
    ax.set_title("(c) Unconditional commands: where a pause escapes a check on a replica", fontsize=9, color=INK, loc="left")
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.legend(fontsize=7, frameon=False, loc="center left", bbox_to_anchor=(1.01, 0.5), handlelength=2.6)
    for ax in axes:
        style(ax)
    fig.subplots_adjust(left=0.205, right=0.985, top=0.94, bottom=0.11)
    # panel (c) leaves room on the right for its legend, outside the data
    pos = axes[2].get_position()
    axes[2].set_position([pos.x0, pos.y0, pos.width * 0.76, pos.height])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out)
    print("wrote", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
