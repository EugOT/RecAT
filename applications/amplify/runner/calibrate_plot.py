#!/usr/bin/env python3
"""Generate statistical analysis charts from a calibrate.py JSON report.

Reads the report file produced by `runner/calibrate.py` (or reads probe data
directly from a `traces/calibrate-<ts>/` directory if no JSON report is
available) and writes a multi-panel PNG visualizing:

  1. **Difficulty curve** — empirical p̂ at each visited cell, with Wilson
     CI error bars and the target window shaded. The shape of this curve
     is the calibration result.

  2. **Trial-by-trial timeline at the calibrated cell** — running p̂ as
     samples accumulate, with the running 95% CI shown as a shaded band.
     Lets you see how fast the estimate stabilized and where any
     unusual streaks happened.

  3. **Pass/fail ribbon** — visual sequence of every trial at the
     calibrated cell, color-coded. Useful for spotting clusters of
     failures (which would suggest non-Bernoulli behavior) vs. uniformly
     scattered failures (the Bernoulli we want).

  4. **Probe duration histogram** — distribution of how long each
     probe took. Outliers indicate slow problems or model retries.

  5. **Failure error magnitudes** — for failed probes, the absolute
     difference between truth and reported answer, on a log scale.
     Reveals whether failures are off-by-small (long-division slips)
     or wild misses (model gave up).

Usage:
  python3 runner/calibrate_plot.py <report.json>
  python3 runner/calibrate_plot.py traces/calibrate-20260408-124726/
  python3 runner/calibrate_plot.py --help

Output:
  Saves a PNG next to the input (or to --out if specified).

Dependencies:
  matplotlib (the only non-stdlib dep). If missing, prints an install
  hint and exits 1.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Optional

try:
    import matplotlib
    matplotlib.use("Agg")  # headless backend; no display required
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
except ImportError:
    print("ERROR: matplotlib not installed.", file=sys.stderr)
    print("       Install with:  pip install matplotlib", file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------------------
# Wilson CI (duplicated from calibrate.py to avoid circular import)
# ----------------------------------------------------------------------------

def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return (0.0, 0.5, 1.0)
    p_hat = k / n
    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    spread = (z / denom) * math.sqrt(p_hat * (1.0 - p_hat) / n + z * z / (4 * n * n))
    lo = max(0.0, center - spread)
    hi = min(1.0, center + spread)
    return (lo, p_hat, hi)


# ----------------------------------------------------------------------------
# Loading data
# ----------------------------------------------------------------------------

def load_from_json(path: Path) -> dict:
    """Load a calibrate.py JSON report."""
    with path.open() as f:
        return json.load(f)


def load_from_trace_dir(trace_dir: Path) -> dict:
    """Reconstruct a calibrate.py-like report from a traces/calibrate-* directory.

    Used when the JSON report file got lost or the calibration is still
    running. Walks every probe-*.conv.jsonl file, extracts the per-probe
    result, and groups by cell index.
    """
    cells_data: dict[int, list[dict]] = {}

    progress_log = trace_dir / "progress.log"
    if not progress_log.exists():
        # Try /tmp as a fallback for in-progress runs
        for candidate in (Path("/tmp/calibrate-progress.log"),):
            if candidate.exists():
                progress_log = candidate
                break

    # Parse the progress log if available — gives us truth/reported/passed/elapsed
    # without re-walking conversation files. Format:
    #   ✓ cell 7 muldiv-8x8//6  truth=NUMBER  reported=NUMBER  (TIMEs)  → cell n=N k=K p̂=P CI=[L,H]
    if progress_log.exists():
        line_re = re.compile(
            r"^\s*([✓✗])\s+cell\s+(\d+)\s+([\w\-x()/+]+)\s+"
            r"truth=(-?\d+)\s+reported=(-?\d+|None)\s+\(([\d.]+)s\)"
        )
        with progress_log.open() as f:
            for line in f:
                m = line_re.match(line)
                if not m:
                    continue
                mark, idx, label, truth, reported, elapsed = m.groups()
                idx = int(idx)
                cells_data.setdefault(idx, []).append({
                    "label": label,
                    "truth": int(truth),
                    "reported": None if reported == "None" else int(reported),
                    "passed": (mark == "✓"),
                    "elapsed_s": float(elapsed),
                })
    else:
        print(f"WARNING: no progress log found at {progress_log}", file=sys.stderr)

    # Build a report-like dict
    cells_list = []
    for idx in sorted(cells_data.keys()):
        history = cells_data[idx]
        n = len(history)
        k = sum(1 for h in history if h["passed"])
        cells_list.append({
            "idx": idx,
            "n": n,
            "k": k,
            "ci": list(wilson_ci(k, n)),
            "status": "from-trace-dir",
            "history": history,
        })

    return {
        "verdict": "reconstructed",
        "model": "(unknown)",
        "target_low": 0.60,
        "target_high": 0.90,
        "total_probes": sum(c["n"] for c in cells_list),
        "cells": cells_list,
        "calibrated_cell": None,
        "traces_dir": str(trace_dir),
    }


# ----------------------------------------------------------------------------
# Plotting
# ----------------------------------------------------------------------------

def plot_calibration(report: dict, out_path: Path) -> None:
    """Render a 5-panel calibration analysis figure."""
    cells = report["cells"]
    target_low = report["target_low"]
    target_high = report["target_high"]
    target_mid = (target_low + target_high) / 2.0

    if not cells:
        print("ERROR: report has no cell data.", file=sys.stderr)
        sys.exit(1)

    # Identify the cell of interest: prefer report["calibrated_cell"], else
    # the visited cell with the most samples (assumed to be the dwell target).
    cal_idx = report.get("calibrated_cell")
    if cal_idx is not None:
        cal_cell = next((c for c in cells if c["idx"] == cal_idx), None)
    else:
        cal_cell = max(cells, key=lambda c: c["n"])

    fig = plt.figure(figsize=(14, 11))
    gs = fig.add_gridspec(3, 2, hspace=0.45, wspace=0.30,
                          height_ratios=[1.2, 1.0, 0.8])

    # ------------------------------------------------------------------
    # Panel 1 (top, full width): Difficulty curve across all visited cells
    # ------------------------------------------------------------------
    ax1 = fig.add_subplot(gs[0, :])
    visited = sorted(cells, key=lambda c: c["idx"])
    xs = [c["idx"] for c in visited]
    ps = [c["k"] / c["n"] if c["n"] > 0 else 0.5 for c in visited]
    ci_lows = [wilson_ci(c["k"], c["n"])[0] for c in visited]
    ci_highs = [wilson_ci(c["k"], c["n"])[2] for c in visited]
    ns = [c["n"] for c in visited]

    err_low = [p - lo for p, lo in zip(ps, ci_lows)]
    err_high = [hi - p for p, hi in zip(ps, ci_highs)]

    # Target window shading
    ax1.axhspan(target_low, target_high, alpha=0.15, color="green",
                label=f"target window [{target_low:.2f}, {target_high:.2f}]")
    ax1.axhline(target_mid, color="green", linestyle="--", alpha=0.6, linewidth=1,
                label=f"target midpoint {target_mid:.2f}")

    # Per-cell error bars
    ax1.errorbar(xs, ps, yerr=[err_low, err_high], fmt="o", capsize=5,
                 markersize=8, color="#2C5F8D", ecolor="#2C5F8D",
                 elinewidth=1.5, label="p̂ ± 95% Wilson CI")

    # Annotate sample size
    for x, p, n in zip(xs, ps, ns):
        ax1.annotate(f"n={n}", (x, p), textcoords="offset points",
                     xytext=(0, 12), ha="center", fontsize=8, color="#444")

    # Highlight the calibrated cell
    if cal_cell:
        cal_p = cal_cell["k"] / cal_cell["n"]
        ax1.plot(cal_cell["idx"], cal_p, "*", markersize=22,
                 color="orange", zorder=5,
                 markeredgecolor="black", markeredgewidth=1.5,
                 label=f"calibrated cell {cal_cell['idx']}")

    ax1.set_xlabel("Difficulty cell index")
    ax1.set_ylabel("Empirical pass rate p̂")
    ax1.set_title(f"Difficulty curve — {report.get('model', '(unknown model)')}",
                  fontsize=13, fontweight="bold")
    ax1.set_ylim(-0.05, 1.05)
    ax1.set_xticks(xs)
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc="lower left", fontsize=9, framealpha=0.9)

    # ------------------------------------------------------------------
    # Panel 2 (middle-left): Running p̂ and CI at the calibrated cell
    # ------------------------------------------------------------------
    ax2 = fig.add_subplot(gs[1, 0])
    if cal_cell and cal_cell.get("history"):
        history = cal_cell["history"]
        running_n = []
        running_p = []
        running_ci_lo = []
        running_ci_hi = []
        passes = 0
        for i, h in enumerate(history, start=1):
            if h["passed"]:
                passes += 1
            lo, p, hi = wilson_ci(passes, i)
            running_n.append(i)
            running_p.append(p)
            running_ci_lo.append(lo)
            running_ci_hi.append(hi)

        ax2.axhspan(target_low, target_high, alpha=0.15, color="green")
        ax2.axhline(target_mid, color="green", linestyle="--", alpha=0.6, linewidth=1)
        ax2.fill_between(running_n, running_ci_lo, running_ci_hi,
                         alpha=0.25, color="#2C5F8D", label="95% CI")
        ax2.plot(running_n, running_p, "-", color="#2C5F8D", linewidth=2, label="running p̂")
        ax2.set_xlabel("Trials at calibrated cell")
        ax2.set_ylabel("Running p̂")
        ax2.set_title(f"Cell {cal_cell['idx']} convergence", fontsize=11, fontweight="bold")
        ax2.set_ylim(-0.05, 1.05)
        ax2.grid(True, alpha=0.3)
        ax2.legend(loc="lower right", fontsize=9)
    else:
        ax2.text(0.5, 0.5, "No history for calibrated cell",
                 ha="center", va="center", transform=ax2.transAxes)
        ax2.axis("off")

    # ------------------------------------------------------------------
    # Panel 3 (middle-right): Pass/fail ribbon at calibrated cell
    # ------------------------------------------------------------------
    ax3 = fig.add_subplot(gs[1, 1])
    if cal_cell and cal_cell.get("history"):
        history = cal_cell["history"]
        for i, h in enumerate(history):
            color = "#3CB371" if h["passed"] else "#DC143C"
            ax3.add_patch(mpatches.Rectangle((i, 0), 1, 1, color=color))
        ax3.set_xlim(0, len(history))
        ax3.set_ylim(0, 1)
        ax3.set_xlabel(f"Trial number (cell {cal_cell['idx']})")
        ax3.set_yticks([])
        ax3.set_title("Pass/fail timeline", fontsize=11, fontweight="bold")
        n_pass = sum(1 for h in history if h["passed"])
        ax3.text(0.5, -0.30,
                 f"{n_pass} pass / {len(history) - n_pass} fail = {n_pass/len(history):.3f}",
                 ha="center", va="top", transform=ax3.transAxes, fontsize=10)
        # Legend
        pass_patch = mpatches.Patch(color="#3CB371", label="pass")
        fail_patch = mpatches.Patch(color="#DC143C", label="fail")
        ax3.legend(handles=[pass_patch, fail_patch], loc="upper right", fontsize=9)
    else:
        ax3.text(0.5, 0.5, "No history",
                 ha="center", va="center", transform=ax3.transAxes)
        ax3.axis("off")

    # ------------------------------------------------------------------
    # Panel 4 (bottom-left): Probe duration histogram
    # ------------------------------------------------------------------
    ax4 = fig.add_subplot(gs[2, 0])
    all_durations = []
    for c in cells:
        for h in c.get("history", []):
            if h.get("elapsed_s"):
                all_durations.append(h["elapsed_s"])
    if all_durations:
        ax4.hist(all_durations, bins=30, color="#6A5ACD", edgecolor="black", alpha=0.8)
        ax4.axvline(sum(all_durations) / len(all_durations),
                    color="orange", linestyle="--",
                    label=f"mean = {sum(all_durations)/len(all_durations):.1f}s")
        ax4.set_xlabel("Probe duration (seconds)")
        ax4.set_ylabel("Frequency")
        ax4.set_title("Per-probe wall clock", fontsize=11, fontweight="bold")
        ax4.legend(fontsize=9)
        ax4.grid(True, alpha=0.3, axis="y")
    else:
        ax4.text(0.5, 0.5, "No duration data", ha="center", va="center",
                 transform=ax4.transAxes)
        ax4.axis("off")

    # ------------------------------------------------------------------
    # Panel 5 (bottom-right): Failure error magnitudes
    # ------------------------------------------------------------------
    ax5 = fig.add_subplot(gs[2, 1])
    failures = []
    for c in cells:
        for h in c.get("history", []):
            if not h.get("passed") and h.get("reported") is not None:
                err = abs(h["truth"] - h["reported"])
                if err > 0:
                    failures.append(err)
    if failures:
        # Log scale because errors range from 1 to 10^12
        log_failures = [math.log10(e) for e in failures]
        ax5.hist(log_failures, bins=20, color="#DC143C", edgecolor="black", alpha=0.7)
        ax5.set_xlabel("log₁₀(|truth − reported|)")
        ax5.set_ylabel("Frequency")
        ax5.set_title(f"Failure magnitudes (n={len(failures)})",
                      fontsize=11, fontweight="bold")
        ax5.grid(True, alpha=0.3, axis="y")
        # Annotate the smallest and largest errors
        smallest = min(failures)
        largest = max(failures)
        ax5.text(0.02, 0.98,
                 f"smallest: {smallest}\nlargest: {largest:,}",
                 transform=ax5.transAxes, verticalalignment="top",
                 fontsize=8, family="monospace",
                 bbox=dict(boxstyle="round", facecolor="white", alpha=0.85))
    else:
        ax5.text(0.5, 0.5, "No failures recorded yet", ha="center", va="center",
                 transform=ax5.transAxes)
        ax5.axis("off")

    # ------------------------------------------------------------------
    # Figure title with summary
    # ------------------------------------------------------------------
    summary_lines = [
        f"amplify calibration analysis",
    ]
    if cal_cell:
        lo, ph, hi = wilson_ci(cal_cell["k"], cal_cell["n"])
        summary_lines.append(
            f"calibrated cell {cal_cell['idx']}: "
            f"n={cal_cell['n']}, k={cal_cell['k']}, p̂={ph:.3f}, "
            f"95% CI=[{lo:.3f}, {hi:.3f}]"
        )
    summary_lines.append(
        f"total probes: {report.get('total_probes', sum(c['n'] for c in cells))}, "
        f"verdict: {report.get('verdict', 'n/a')}"
    )
    fig.suptitle("\n".join(summary_lines), fontsize=12, fontweight="bold", y=0.995)

    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    print(f"Wrote {out_path}", file=sys.stderr)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", type=Path,
                        help="Either a calibrate.py JSON report file OR a "
                             "traces/calibrate-<ts>/ directory")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output PNG path (default: alongside input)")
    args = parser.parse_args(argv[1:])

    if not args.input.exists():
        print(f"ERROR: {args.input} does not exist", file=sys.stderr)
        return 1

    if args.input.is_dir():
        report = load_from_trace_dir(args.input)
        default_out = args.input / "analysis.png"
    else:
        report = load_from_json(args.input)
        default_out = args.input.with_suffix(".png")

    out_path = args.out or default_out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    plot_calibration(report, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
