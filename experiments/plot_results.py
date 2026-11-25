#!/usr/bin/env python3
"""Paper figures (vector PDF) from a results directory.

Colors follow the policy, never its rank, and every bar carries a direct
k/n label so identity and value never rely on color alone.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from common import RESULTS_DIR, ROOT, percentile  # noqa: E402

DEFAULT_OUT = ROOT / "journal" / "figures" if (ROOT / "journal").is_dir() else RESULTS_DIR / "figures"

DISPLAY = {
    "direct": "Direct", "naive": "Freshness\nonly", "local": "Local\n(coherent)", "xair": "XAIR",
    "local_stale": "Local stale", "local_push": "Local push", "local_authoritative": "Local auth.",
}
COLORS = {  # validated categorical slots (adjacent pairs pass CVD and normal-vision floors)
    "direct": "#eb6834", "naive": "#4a3aa7", "local": "#1baf7a", "xair": "#2a78d6",
    "local_stale": "#e87ba4", "local_push": "#eda100", "local_authoritative": "#008300",
}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d9d8d4"


def style() -> None:
    plt.rcParams.update({
        "font.size": 9, "axes.titlesize": 9.5, "axes.labelsize": 9, "xtick.labelsize": 8.5,
        "ytick.labelsize": 8.5, "legend.fontsize": 8, "savefig.dpi": 300,
        "axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": MUTED,
        "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
        "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.axisbelow": True,
    })


def save(fig, out: Path, stem: str) -> None:
    fig.savefig(out / f"{stem}.pdf", bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def count_bars(ax, keys: list[str], k: dict[str, int], n: dict[str, int], ylabel: str) -> None:
    xs = range(len(keys))
    ax.bar(xs, [k[key] for key in keys], width=0.6, color=[COLORS[key] for key in keys],
           edgecolor="white", linewidth=2)
    n_max = max(n.values())
    for i, key in enumerate(keys):
        ax.text(i, k[key] + n_max * 0.03, f"{k[key]}/{n[key]}", ha="center", va="bottom", fontsize=8.5, color=INK)
    ax.set_xticks(list(xs), [DISPLAY[key] for key in keys])
    ax.set_ylim(0, n_max * 1.18)
    ax.set_ylabel(ylabel)


def fig_e1(R: Path, out: Path) -> None:
    rows = load(R / "e1_baselines.csv")
    if not rows:
        return
    keys = [b for b in ("direct", "naive", "local", "xair") if any(r["baseline"] == b for r in rows)]
    k = {b: sum(int(r["stale_executed"]) for r in rows if r["baseline"] == b) for b in keys}
    n = {b: sum(1 for r in rows if r["baseline"] == b) for b in keys}
    fig, ax = plt.subplots(figsize=(3.5, 2.3))
    count_bars(ax, keys, k, n, "Stale releases")
    save(fig, out, "e1_ser_by_baseline")

    lat = [float(r["validation_latency_ms"]) for r in rows if r["baseline"] == "xair" and float(r["validation_latency_ms"]) > 0]
    if lat:
        fig, ax = plt.subplots(figsize=(3.5, 2.2))
        ax.hist(lat, bins=20, color=COLORS["xair"], edgecolor="white", linewidth=1)
        for q, ls in ((0.50, ":"), (0.99, "--")):
            v = percentile(lat, q)
            ax.axvline(v, color=INK, linestyle=ls, linewidth=1.2, label=f"p{int(q * 100)} = {v:.3f} ms")
        ax.set_xlabel("XAIR validation latency at $t_v$ (ms)")
        ax.set_ylabel("Trials")
        ax.legend(frameon=False)
        save(fig, out, "e1_validation_latency")


def fig_e4(R: Path, out: Path) -> None:
    rows = load(R / "e4_load_http.csv")
    if not rows:
        return
    row = rows[0]
    labels = ["internal\np50", "internal\np99", "end-to-end\np50", "end-to-end\np99"]
    vals = [float(row[k]) for k in ("vl_internal_p50_ms", "vl_internal_p99_ms", "vl_e2e_p50_ms", "vl_e2e_p99_ms")]
    fig, ax = plt.subplots(figsize=(3.5, 2.2))
    ax.bar(range(4), vals, width=0.6, color=[COLORS["xair"]] * 2 + [MUTED] * 2, edgecolor="white", linewidth=2)
    for i, v in enumerate(vals):
        ax.text(i, v, f"{v:.3f}" if v < 1 else f"{v:.2f}", ha="center", va="bottom", fontsize=8)
    ax.set_yscale("log")
    ax.set_xticks(range(4), labels)
    ax.set_ylabel("Latency (ms, log)")
    save(fig, out, "e4_load_latency")


def fig_e9(R: Path, out: Path) -> None:
    rows = load(R / "e9_consistency_sweep.csv")
    if not rows:
        return
    policies = ("local_stale", "local_push", "local_authoritative", "xair")
    delays = sorted({int(r["delay_ms"]) for r in rows})
    fig, ax = plt.subplots(figsize=(3.5, 2.4))
    width = 0.8 / len(policies)
    for j, p in enumerate(policies):
        ys = []
        for d in delays:
            cell = [r for r in rows if r["policy"] == p and int(r["delay_ms"]) == d]
            ys.append(sum(int(r["stale_executed"]) for r in cell) / max(len(cell), 1))
        xs = [i + (j - 1.5) * width for i in range(len(delays))]
        ax.bar(xs, [max(y, 0.012) for y in ys], width=width, color=COLORS[p], edgecolor="white",
               linewidth=1, label=DISPLAY[p])
    ax.set_xticks(range(len(delays)), [str(d) for d in delays])
    ax.set_xlabel("Delay between remote write and submission (ms)")
    ax.set_ylabel("Stale-release rate")
    ax.set_ylim(0, 1.05)
    ax.set_yticks([0, 0.5, 1.0])
    ax.legend(ncol=4, frameon=False, loc="lower center", bbox_to_anchor=(0.5, 1.0), fontsize=7,
              handlelength=1.0, columnspacing=0.8)
    save(fig, out, "e9_consistency_sweep")


def fig_e10(R: Path, out: Path) -> None:
    rows = load(R / "e10_toctou.csv") + load(R / "e10_toctou_boundary.csv")
    inj = [r for r in rows if r["inject"] == "1" and r["t_injection_end_ms"] and r["t_recheck_start_ms"]]
    if not inj:
        return
    fig, ax = plt.subplots(figsize=(3.5, 2.3))
    groups = (("0", "Blocked at $t_g$", COLORS["xair"], "o"), ("1", "Released", COLORS["direct"], "^"))
    for flag, label, color, marker in groups:
        sub = [r for r in inj if r["gateway_released"] == flag]
        xs = [float(r["t_injection_end_ms"]) - float(r["t_recheck_start_ms"]) for r in sub]
        ys = [float(r["inject_offset_ms"]) for r in sub]
        ax.scatter(xs, ys, s=16, color=color, marker=marker, edgecolors="white", linewidths=0.5, label=f"{label} ({len(sub)})", zorder=3)
    ax.axvline(0, color=INK, linewidth=1, linestyle="--")
    ax.text(0.5, ax.get_ylim()[1] * 0.97, "$t_g$", fontsize=7, color=MUTED, va="top")
    ax.set_xlabel("Injection end $-$ $t_g$ (ms)")
    ax.set_ylabel("Injection offset (ms)")
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.legend(frameon=False, loc="lower right")
    save(fig, out, "e10_injection_timing")


def fig_e16(R: Path, out: Path) -> None:
    rows = load(R / "e16_context_churn.csv")
    if not rows:
        return
    series = (("global", "unrelated", "Global, unrelated field", "#eb6834", "o", "-"),
              ("global", "related_same", "Global, same-value rewrite", "#4a3aa7", "s", "-"),
              ("readset", "unrelated", "Read-set, unrelated field", "#2a78d6", "^", "--"),
              ("readset", "related_same", "Read-set, same-value rewrite", "#1baf7a", "v", ":"))
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.4))
    for scope, pattern, label, color, marker, ls in series:
        cells = defaultdict(list)
        for r in rows:
            if r["version_scope"] == scope and r["pattern"] == pattern:
                cells[float(r["target_rate_hz"])].append(r)
        xs = sorted(cells)
        ach = [float(cells[x][0]["achieved_rate_hz"]) for x in xs]
        fpr = [sum(1 - int(r["gateway_released"]) for r in cells[x]) / len(cells[x]) for x in xs]
        good = [float(cells[x][0]["goodput_ips"]) for x in xs]
        axes[0].plot(ach, fpr, color=color, marker=marker, linestyle=ls, linewidth=2, markersize=5, label=label)
        axes[1].plot(ach, good, color=color, marker=marker, linestyle=ls, linewidth=2, markersize=5)
    axes[0].set_ylabel("Valid intents revoked")
    axes[0].set_ylim(-0.03, 1.05)
    axes[1].set_ylabel("Goodput (intent/s)")
    for ax in axes:
        ax.set_xlabel("Achieved context-update rate (Hz)")
        ax.grid(axis="x", color=GRID, linewidth=0.6)
    fig.legend(ncol=2, frameon=False, loc="lower center", bbox_to_anchor=(0.5, 1.0), fontsize=7.5)
    fig.tight_layout()
    save(fig, out, "e16_context_churn")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=RESULTS_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    style()
    for fn in (fig_e1, fig_e4, fig_e9, fig_e10, fig_e16):
        fn(args.results, args.out)
    print(f"Wrote figures to {args.out}")


if __name__ == "__main__":
    main()
