#!/usr/bin/env python3
"""Generate IEEE paper figures (vector PDF) from experiment CSVs."""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "experiments" / "results"
DEFAULT_OUT = ROOT / "experiments" / "plots"
_paper_figures = ROOT.parent / "ResearchTrack" / "execution-gap-paper" / "figures"
if _paper_figures.parent.exists():
    DEFAULT_OUT = _paper_figures

BASELINE_ORDER = ("direct", "naive", "local", "xair")
DISPLAY = {
    "direct": "Direct",
    "naive": "Freshness-only",
    "local": "Local guard",
    "local_stale": "Local stale",
    "xair": "XAIR",
}
COLORS = {
    "direct": "#c0392b",
    "naive": "#e67e22",
    "local": "#2980b9",
    "local_stale": "#8e44ad",
    "xair": "#27ae60",
}


def _as_float(val) -> float:
    if val in (True, "True", "true", "1", 1):
        return 1.0
    if val in (False, "False", "false", "0", 0, "", None):
        return 0.0
    return float(val)


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n) / denom
    return max(0, center - margin), min(1, center + margin)


def apply_ieee_style() -> None:
    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.titlesize": 11,
            "axes.labelsize": 10,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.fontsize": 9,
            "figure.dpi": 150,
            "savefig.dpi": 300,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.25,
            "grid.linestyle": "--",
        }
    )


def save(fig, out: Path, stem: str, *, tight: bool = True) -> None:
    bbox = "tight" if tight else None
    fig.savefig(out / f"{stem}.pdf", bbox_inches=bbox, pad_inches=0.05)
    fig.savefig(out / f"{stem}.png", dpi=300, bbox_inches=bbox, pad_inches=0.05)
    plt.close(fig)


def annotate_count(ax, x: float, k: int, n: int, y_offset: float = 0.04) -> None:
    y = k / n if n else 0
    ax.text(
        x,
        y + y_offset,
        f"{k}/{n}",
        ha="center",
        va="bottom",
        fontsize=9,
        fontweight="bold",
        clip_on=True,
    )


def plot_stale_count_bars(
    ax,
    keys: tuple[str, ...],
    stale_counts: dict[str, int],
    n_by_key: dict[str, int],
    *,
    title: str,
    ylabel: str = "Stale ROS publications",
) -> None:
    xs = list(range(len(keys)))
    n_max = max(n_by_key.values()) if n_by_key else 1
    heights = [stale_counts.get(k, 0) for k in keys]
    colors = [COLORS.get(k, "#666") for k in keys]
    ax.bar(xs, heights, color=colors, edgecolor="black", linewidth=0.8, width=0.62, zorder=2)
    for i, k in enumerate(keys):
        n = n_by_key[k]
        annotate_count(ax, i, stale_counts.get(k, 0), n, y_offset=max(n_max * 0.03, 0.5))
    ax.set_xticks(xs, [DISPLAY.get(k, k) for k in keys])
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.set_ylim(0, n_max * 1.2)
    ax.set_yticks(range(0, n_max + 1, max(1, n_max // 5)))


def load_e1(path: Path):
    ser: dict[str, list[float]] = defaultdict(list)
    lats: list[float] = []
    with path.open() as f:
        for row in csv.DictReader(f):
            b = row["baseline"]
            ser[b].append(_as_float(row.get("stale_executed")))
            if b == "xair":
                lat = float(row.get("validation_latency_ms") or 0)
                if lat > 0:
                    lats.append(lat)
    return ser, lats


def load_e8(path: Path):
    stale: dict[str, int] = defaultdict(int)
    n: dict[str, int] = defaultdict(int)
    with path.open() as f:
        for row in csv.DictReader(f):
            if str(row.get("unknown", "")).lower() in ("true", "1"):
                continue
            b = row["baseline"]
            n[b] += 1
            if _as_float(row.get("stale_executed")) > 0:
                stale[b] += 1
    return stale, n


def load_e9(path: Path):
    local_stale = xair_stale = 0
    n = 0
    with path.open() as f:
        for row in csv.DictReader(f):
            n += 1
            if _as_float(row.get("local_stale")) > 0:
                local_stale += 1
            if _as_float(row.get("xair_ros")) > 0:
                xair_stale += 1
    return local_stale, xair_stale, n


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    apply_ieee_style()

    e1_path = RESULTS / "e1_baselines.csv"
    if e1_path.exists():
        ser, lats = load_e1(e1_path)
        keys = [b for b in BASELINE_ORDER if b in ser]
        stale_counts = {b: int(sum(ser[b])) for b in keys}
        n_by = {b: len(ser[b]) for b in keys}
        fig, ax = plt.subplots(figsize=(6.2, 3.8))
        plot_stale_count_bars(
            ax,
            tuple(keys),
            stale_counts,
            n_by,
            title="E1b: stale RESUME executions (HTTP layer)",
            ylabel="Stale executions (count)",
        )
        ax.axvspan(-0.5, 1.5, color="#fdecea", alpha=0.7, zorder=0)
        ax.axvspan(1.5, len(keys) - 0.5, color="#eafaf1", alpha=0.7, zorder=0)
        fig.tight_layout()
        save(fig, args.out, "e1_ser_by_baseline")

        if lats:
            fig, ax = plt.subplots(figsize=(6, 3.2))
            p50 = sorted(lats)[max(int(len(lats) * 0.5) - 1, 0)]
            p99 = sorted(lats)[max(int(len(lats) * 0.99) - 1, 0)]
            ax.hist(lats, bins=min(12, max(4, len(lats) // 3)), color="#27ae60", edgecolor="black", alpha=0.85)
            ax.axvline(p50, color="#2c3e50", linestyle=":", linewidth=1.5, label=f"p50 = {p50:.3f} ms")
            ax.axvline(p99, color="#c0392b", linestyle="--", linewidth=1.5, label=f"p99 = {p99:.3f} ms")
            ax.set_xlabel("XAIR validation latency (ms)")
            ax.set_ylabel("Trial count")
            ax.set_title(f"E1b: XAIR validation latency ({len(lats)} trials)")
            ax.legend(loc="upper right", frameon=True)
            fig.tight_layout()
            save(fig, args.out, "e1_validation_latency")

    e4_path = RESULTS / "e4_load_http.csv"
    if e4_path.exists():
        with e4_path.open() as f:
            row = next(csv.DictReader(f))
        internal_p50 = float(row["vl_internal_p50_ms"])
        internal_p99 = float(row["vl_internal_p99_ms"])
        e2e_p50 = float(row["vl_e2e_p50_ms"])
        e2e_p99 = float(row["vl_e2e_p99_ms"])
        tp = float(row.get("throughput_released_ips") or row.get("throughput_ips") or 0)
        n_int = int(row.get("completed_trials") or row.get("intents") or row.get("intents_requested") or 0)
        fig, ax = plt.subplots(figsize=(5.8, 3.4))
        labels = ["internal p50", "internal p99", "e2e p50", "e2e p99"]
        vals = [internal_p50, internal_p99, e2e_p50, e2e_p99]
        colors = ["#3498db", "#9b59b6", "#1abc9c", "#e67e22"]
        bars = ax.bar(labels, vals, color=colors, edgecolor="black", width=0.6)
        ymax = max(vals) * 1.18
        ax.set_ylim(0, ymax)
        for bar, val in zip(bars, vals):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + ymax * 0.02,
                f"{val:.2f}",
                ha="center",
                va="bottom",
                fontsize=8,
            )
        ax.set_ylabel("Latency (ms)")
        ax.set_title(f"E4: internal vs end-to-end latency ({n_int:,} intents)")
        ax.text(
            0.02,
            0.98,
            f"Throughput: {tp:.0f} intent/s",
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=9,
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="#ccc", alpha=0.95),
        )
        ax.grid(axis="y", linestyle=":", alpha=0.35)
        ax.set_axisbelow(True)
        fig.tight_layout()
        save(fig, args.out, "e4_load_latency")

    e8_path = RESULTS / "e8_gazebo_cell.csv"
    e9s_path = RESULTS / "e9_consistency_sweep.csv"
    e8_stale, e8_n = load_e8(e8_path) if e8_path.exists() else ({}, {})

    # Right panel: E9 sweep aggregate for local_stale vs xair (not legacy shared_context).
    e9_stale_counts: dict[str, int] = {}
    e9_n_by: dict[str, int] = {}
    if e9s_path.exists():
        by_pol: dict[str, list[int]] = defaultdict(list)
        with e9s_path.open() as f:
            for row in csv.DictReader(f):
                by_pol[row["policy"]].append(int(row["stale_executed"]))
        for pol in ("local_stale", "xair"):
            vals = by_pol.get(pol, [])
            e9_stale_counts[pol] = sum(vals)
            e9_n_by[pol] = len(vals)

    if e8_stale:
        keys = [b for b in BASELINE_ORDER if b in e8_n]
        fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.0))

        plot_stale_count_bars(
            axes[0],
            tuple(keys),
            e8_stale,
            e8_n,
            title="(a) E8, coherent cache",
            ylabel="Stale authorizations (count)",
        )
        axes[0].axvspan(-0.5, 1.5, color="#fdecea", alpha=0.65, zorder=0)
        axes[0].axvspan(1.5, len(keys) - 0.5, color="#eafaf1", alpha=0.65, zorder=0)

        e9_keys = ("local_stale", "xair")
        plot_stale_count_bars(
            axes[1],
            e9_keys,
            e9_stale_counts,
            e9_n_by,
            title="(b) E9 sweep, stale vs xair",
            ylabel="Stale authorizations (count)",
        )

        n_ref = max(max(e8_n.values()), max(e9_n_by.values() or [1]))
        for ax in axes:
            ax.set_ylim(0, n_ref * 1.25)
            ax.axhline(n_ref, color="#95a5a6", linestyle=":", linewidth=1, zorder=1)

        fig.subplots_adjust(left=0.08, right=0.98, top=0.88, bottom=0.22, wspace=0.35)
        save(fig, args.out, "e8_e9_outcomes", tight=False)

    if e9s_path.exists():
        from collections import defaultdict as _dd  # noqa: F401 — already imported

        by: dict[tuple[str, int], list[int]] = defaultdict(list)
        with e9s_path.open() as f:
            for row in csv.DictReader(f):
                by[(row["policy"], int(row["delay_ms"]))].append(int(row["stale_executed"]))
        policies = sorted({k[0] for k in by})
        delays = sorted({k[1] for k in by})
        grid = []
        for p in policies:
            grid.append([sum(by.get((p, d), [0])) / max(len(by.get((p, d), [])), 1) for d in delays])
        fig, ax = plt.subplots(figsize=(6.5, 3.2))
        im = ax.imshow(grid, aspect="auto", cmap="YlOrRd", vmin=0, vmax=1)
        ax.set_xticks(range(len(delays)), [str(d) for d in delays])
        ax.set_yticks(range(len(policies)), policies)
        ax.set_xlabel("Emulated push/propagation delay (ms)")
        ax.set_ylabel("Cache policy")
        ax.set_title("E9: stale-authorization rate (push = sensitivity threshold)")
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        fig.tight_layout()
        save(fig, args.out, "e9_consistency_heatmap")

    e10_path = RESULTS / "e10_toctou.csv"
    if e10_path.exists():
        gate_lat, release_lat = [], []
        with e10_path.open() as f:
            for row in csv.DictReader(f):
                if row.get("validation_to_gate_ms"):
                    gate_lat.append(float(row["validation_to_gate_ms"]))
                if row.get("validation_to_publish_ms"):
                    release_lat.append(float(row["validation_to_publish_ms"]))
        gate_lat.sort()
        release_lat.sort()
        if gate_lat:
            fig, ax = plt.subplots(figsize=(5.5, 3.2))
            ax.plot(gate_lat, [(i + 1) / len(gate_lat) for i in range(len(gate_lat))],
                    color="#2980b9", linewidth=2, label=f"Validation-to-gate ($n{{=}}{len(gate_lat)}$)")
            if release_lat:
                ax.plot(release_lat, [(i + 1) / len(release_lat) for i in range(len(release_lat))],
                        color="#c0392b", linewidth=2, linestyle="--",
                        label=f"Validation-to-release ($n{{=}}{len(release_lat)}$)")
            ax.set_xlabel("Latency (ms)")
            ax.set_ylabel("CDF")
            ax.set_title("E10: validation-to-gate and validation-to-release latency")
            ax.legend(fontsize=7)
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            save(fig, args.out, "e10_toctou_cdf")

    print(f"Wrote figures to {args.out}")


if __name__ == "__main__":
    main()
