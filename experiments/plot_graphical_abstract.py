#!/usr/bin/env python3
"""Graphical abstract (Elsevier: at least 1328 x 531 px, w x h), from the frozen summary.

Left: where the last check runs, from decision to effect. Right: width of the interval a check on a replica leaves uncovered.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

from common import ROOT  # noqa: E402
from plot_key_results import AQUA, BLUE, GRID, INK, MUTED, ORANGE  # noqa: E402

RED = "#c62e2e"


def effects(src: dict, key: str, cells: tuple[str, ...]) -> tuple[int, int]:
    k = n = 0
    for c in src:
        for cell in cells:
            e = c.get(key, {}).get(cell)
            if e:
                k += e["stale_effects"]["k"]; n += e["stale_effects"]["n"]
    return k, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", type=Path, default=ROOT / "data" / "execution-gap" / "paper_metrics_summary.json")
    ap.add_argument("--out", type=Path, default=ROOT / "journal" / "figures" / "graphical_abstract.png")
    a = ap.parse_args()
    d = json.loads(a.summary.read_text())
    dist = d["distributed"]
    camps = [dist] + list(dist.get("campaigns", {}).values())
    sources = [("Emulated OPC UA\ncontroller", "e21", camps, "plc"),
               ("Physical node,\nwide area", "e21", [d.get("physical", {})], "plc"),
               ("Physical node,\nWi-Fi LAN", "e21", [d.get("lan", {})], "plc"),
               ("OpenPLC\nruntime", "e24", [d.get("openplc", {})], "openplc")]
    sources = [s for s in sources if any(c.get(s[1]) for c in s[2])]

    fig = plt.figure(figsize=(13.28, 5.31), dpi=300)
    fig.patch.set_facecolor("white")
    # --- left: timeline
    ax = fig.add_axes([0.02, 0.06, 0.50, 0.80])
    ax.set_xlim(0, 10); ax.set_ylim(0, 6); ax.axis("off")
    fig.text(0.02, 0.945, "Execution gap: a check on a replica is not enough", fontsize=17, weight="bold", color=INK)
    ax.text(0.1, 5.2, "An action that was admissible when decided takes effect after the plant has changed.",
            fontsize=11, color=MUTED)
    ax.annotate("", xy=(9.8, 3.4), xytext=(0.3, 3.4), arrowprops=dict(arrowstyle="-|>", color=INK, lw=1.6))
    for x, lab, sub in ((0.6, "$t_d$", "decision"), (2.4, "$t_v$", "validation"), (4.6, "$t_g$ / $t_c$", "last check"),
                        (6.8, "$t_m$", "release"), (8.9, "$t_a$", "effect")):
        ax.plot([x], [3.4], "o", color=INK, ms=7)
        ax.text(x, 3.85, lab, ha="center", fontsize=13, color=INK)
        ax.text(x, 2.85, sub, ha="center", fontsize=10.5, color=MUTED)
    ax.plot([4.6, 8.9], [2.35, 2.35], color=RED, lw=6, solid_capstyle="butt")
    ax.text(6.75, 1.9, "A change after a check on a replica escapes it", ha="center", fontsize=10.5, color=RED)
    ax.add_patch(FancyBboxPatch((7.6, 0.25), 2.3, 1.15, boxstyle="round,pad=0.05", fc="#e8f7f1", ec="#1baf7a", lw=1.5))
    ax.text(8.75, 0.83, "The controller checks\nits own version\nin the same scan", ha="center", va="center", fontsize=10, color="#0d6b4a")
    ax.add_patch(FancyBboxPatch((0.3, 0.25), 6.9, 1.15, boxstyle="round,pad=0.05", fc="#fdeee8", ec=ORANGE, lw=1.5))
    ax.text(3.75, 0.83, "AIS: the producer states validity and preconditions\n"
            "XAIR: rechecks versions, or commits atomically in the store", ha="center", va="center", fontsize=10, color=INK)
    # --- right: width of the interval a check on a replica leaves uncovered (ranges stated in the paper)
    bx = fig.add_axes([0.70, 0.18, 0.28, 0.50])
    rows = [("Gate interval\n(store check to effect)", 1, 4, BLUE),
            ("Replica lag\n(controller to store)", 10, 130, ORANGE)]
    for yi, (lab, lo, hi, col) in zip((1, 0), rows):
        bx.barh(yi, hi - lo, left=lo, height=0.42, color=col)
    bx.text(4.6, 1, "1-4 ms", va="center", fontsize=11, color=INK)
    bx.annotate("", xy=(190, 0), xytext=(100, 0), arrowprops=dict(arrowstyle="-|>", color=ORANGE, lw=2.5))
    bx.text(230, 0, "10 to over\n100 ms", va="center", fontsize=11, color=INK)
    bx.set_xscale("log"); bx.set_xlim(1, 1500); bx.set_ylim(-0.6, 1.6)
    bx.set_yticks([1, 0], [r[0] for r in rows], fontsize=10.5, color=INK)
    bx.set_xlabel("Interval in which a change escapes the check [ms]", fontsize=10.5, color=MUTED)
    for sp in ("top", "right"):
        bx.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        bx.spines[sp].set_color(MUTED)
    bx.tick_params(colors=MUTED, labelsize=10)
    bx.xaxis.grid(True, color=GRID); bx.set_axisbelow(True)
    fig.text(0.58, 0.78, "Intervals that a replica check leaves uncovered.\nFor controller-owned state, the controller scan closes them.", fontsize=10.5, color=MUTED, va="top")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out, dpi=300)
    print("wrote", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
