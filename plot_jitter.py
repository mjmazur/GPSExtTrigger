#!/usr/bin/env python3
"""
plot_jitter.py – Visualise trigger_gen.py jitter CSV and optional stats JSON.

Usage:
    python plot_jitter.py jitter_log.csv                    # CSV only
    python plot_jitter.py jitter_log.csv --stats stats.json # CSV + summary box
    python plot_jitter.py jitter_log.csv -o jitter.png      # save to file
"""

import argparse
import csv
import json
import sys
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import AutoMinorLocator


# ── helpers ────────────────────────────────────────────────────────────────
def load_csv(path):
    """Return dict of numpy arrays keyed by column name."""
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    if not rows:
        print("Error: CSV file is empty.", file=sys.stderr)
        sys.exit(1)

    data = {}
    for key in rows[0]:
        vals = []
        for r in rows:
            v = r[key]
            if v == "":
                vals.append(np.nan)
            else:
                try:
                    vals.append(float(v))
                except ValueError:
                    vals.append(np.nan)
        data[key] = np.array(vals)
    return data


def load_stats(path):
    """Load the optional stats JSON written by trigger_gen.py --stats-json."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def stats_text(stats):
    """Build a multi-line annotation string from the stats JSON."""
    run = stats.get("run", {})
    jit = stats.get("jitter_us", {})
    lines = [
        f"Rate: {run.get('rate_hz', '?')} Hz   "
        f"Cycles: {run.get('total_cycles', '?')}   "
        f"Elapsed: {run.get('elapsed_s', '?'):.1f} s",
    ]
    for label, key in [("ON err", "on_error"), ("OFF err", "off_error"), ("Period err", "period_error")]:
        s = jit.get(key)
        if s:
            lines.append(
                f"{label}: μ={s['mean']:.2f} σ={s['stddev']:.2f} "
                f"min={s['min']:.2f} max={s['max']:.2f} µs"
            )
    return "\n".join(lines)


# ── plotting ───────────────────────────────────────────────────────────────
def plot(data, stats=None, out_path=None):
    us = 1_000_000  # seconds → microseconds

    cycle = data["cycle"]
    on_err  = data["on_error_s"]  * us
    off_err = data["off_error_s"] * us
    per_err = data["period_error_s"] * us  # has NaN for first cycle

    fig, axes = plt.subplots(3, 2, figsize=(14, 9),
                             gridspec_kw={"width_ratios": [3, 1]})
    fig.suptitle("Trigger Jitter Analysis", fontsize=14, fontweight="bold")

    series = [
        (on_err,  "ON-edge error",     "#2196F3"),
        (off_err, "OFF-edge error",    "#FF9800"),
        (per_err, "Period error",      "#4CAF50"),
    ]

    for row, (vals, label, colour) in enumerate(series):
        ax_ts, ax_hist = axes[row]
        valid = ~np.isnan(vals)

        # ── time series ──
        ax_ts.plot(cycle[valid], vals[valid], linewidth=0.4, color=colour, alpha=0.8)

        mean_val = np.nanmean(vals)
        std_val  = np.nanstd(vals)
        min_val  = np.nanmin(vals)
        max_val  = np.nanmax(vals)
        n_val    = int(np.sum(valid))
        ax_ts.axhline(mean_val, color=colour, linestyle="--", linewidth=0.8, alpha=0.6)
        ax_ts.axhspan(mean_val - std_val, mean_val + std_val,
                       color=colour, alpha=0.07)

        # Per-subplot statistics annotation
        stat_str = (f"μ={mean_val:.2f}  σ={std_val:.2f}\n"
                    f"min={min_val:.2f}  max={max_val:.2f}\n"
                    f"n={n_val}")
        ax_ts.text(0.98, 0.96, stat_str, transform=ax_ts.transAxes,
                   fontsize=7.5, fontfamily="monospace",
                   verticalalignment="top", horizontalalignment="right",
                   bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                             edgecolor=colour, alpha=0.85))

        ax_ts.set_ylabel(f"{label} (µs)")
        ax_ts.xaxis.set_minor_locator(AutoMinorLocator())
        ax_ts.yaxis.set_minor_locator(AutoMinorLocator())
        ax_ts.grid(True, which="major", linewidth=0.5, alpha=0.3)
        ax_ts.grid(True, which="minor", linewidth=0.25, alpha=0.15)

        # ── histogram ──
        v = vals[valid]
        if len(v) > 0:
            bins = min(200, max(20, len(v) // 50))
            ax_hist.hist(v, bins=bins, orientation="horizontal",
                         color=colour, alpha=0.7, edgecolor="white", linewidth=0.3)
            ax_hist.axhline(mean_val, color=colour, linestyle="--", linewidth=0.8, alpha=0.6)
        ax_hist.set_xlabel("Count")
        ax_hist.yaxis.set_minor_locator(AutoMinorLocator())
        ax_hist.grid(True, which="major", linewidth=0.5, alpha=0.3)
        # share y range with time-series
        ax_hist.sharey(ax_ts)
        ax_hist.tick_params(labelleft=False)

    axes[-1][0].set_xlabel("Cycle")

    # ── stats annotation ──
    if stats:
        fig.text(0.5, -0.02, stats_text(stats),
                 fontsize=8, fontfamily="monospace",
                 ha="center", va="top",
                 bbox=dict(boxstyle="round,pad=0.5", facecolor="#f5f5f5",
                           edgecolor="#cccccc"))

    fig.tight_layout(rect=[0, 0.04 if stats else 0, 1, 0.96])

    if out_path:
        fig.savefig(out_path, dpi=200, bbox_inches="tight")
        print(f"Saved plot to {out_path}")
    else:
        plt.show()


# ── main ───────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Plot jitter data produced by trigger_gen.py --jitter-csv")
    parser.add_argument("csv", help="Path to jitter CSV file")
    parser.add_argument("--stats", "-s", help="Path to stats JSON file (from --stats-json)")
    parser.add_argument("-o", "--output", help="Save plot to file instead of showing interactively")
    args = parser.parse_args()

    data = load_csv(args.csv)

    # Validate expected columns
    required = {"cycle", "on_error_s", "off_error_s", "period_error_s"}
    missing = required - set(data.keys())
    if missing:
        print(f"Error: CSV is missing columns: {missing}", file=sys.stderr)
        sys.exit(1)

    stats = None
    if args.stats:
        stats = load_stats(args.stats)

    plot(data, stats=stats, out_path=args.output)


if __name__ == "__main__":
    main()
