#!/usr/bin/env python3
"""Visualization for an amplify Pass 2 report.

Reads runner/amplify.py's report.json and renders:

  1. **Amplification curves** — empirical isolated and non-isolated success
     rates plotted against the closed-form binomial-amplification prediction
     P_amp(N, p̂_calibration). The headline result.

  2. **Per-attempt reasoning token counts** — distributions for both arms,
     side by side, to expose the per-attempt effort confound. If the two
     arms allocate similar reasoning per attempt, the gap between curves
     (if any) is attributable to scope; if they're very different, the gap
     is confounded with effort.

  3. **Per-trial pass/fail mosaic** — for each (arm, N) cell, a strip
     showing which trials passed and which failed. Reveals whether
     failures are uniformly distributed (Bernoulli) or clustered.

  4. **Per-problem heatmap** — for the isolated arm, a grid of
     (problem × attempt) outcomes. Lets you see if a particular problem
     is unusually hard for ALL its parallel attempts (high problem variance)
     vs. random failures (clean Bernoulli).

  5. **Failure error magnitudes** — log scale, both arms.

Usage:
  pixi run python applications/amplify/runner/amplify_plot.py traces/amplify-<ts>/report.json

Dependencies: matplotlib from the Pixi environment.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
except ImportError:
    print("ERROR: matplotlib required. Install with:", file=sys.stderr)
    print("  pixi install", file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------------------
# Wilson CI
# ----------------------------------------------------------------------------

def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return (0.0, 0.5, 1.0)
    p_hat = k / n
    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    spread = (z / denom) * math.sqrt(p_hat * (1.0 - p_hat) / n + z * z / (4 * n * n))
    return (max(0.0, center - spread), p_hat, min(1.0, center + spread))


# ----------------------------------------------------------------------------
# Conversation log parsing for per-attempt reasoning tokens
# ----------------------------------------------------------------------------

def count_assistant_text_chars(conv_path: Path) -> int:
    """Total characters of assistant-text content in a conversation log.

    We use character count as a proxy for reasoning effort. Token counts
    would be more accurate but require a tokenizer; characters are within
    a small constant factor and don't need extra dependencies.
    """
    total = 0
    try:
        with conv_path.open() as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "assistant":
                    for c in obj.get("message", {}).get("content", []):
                        if c.get("type") == "text":
                            total += len(c.get("text", ""))
                        elif c.get("type") == "thinking":
                            total += len(c.get("thinking", ""))
    except FileNotFoundError:
        return 0
    return total


def collect_per_attempt_chars(report: dict, traces_dir: Path) -> tuple[list[int], list[int]]:
    """Extract per-attempt reasoning character counts for both arms.

    Returns (isolated_chars, non_isolated_chars_per_attempt).

    For non-isolated, the chars are total chars per session divided by N
    (best-effort estimate of per-attempt reasoning).
    """
    isolated_chars: list[int] = []
    for trial in report["isolated_arm"]["trials"]:
        prob_i = trial["problem_idx"]
        for attempt_i in range(len(trial["answers"])):
            conv_name = f"isolated-p{prob_i+1:02d}-a{attempt_i+1:02d}.conv.jsonl"
            chars = count_assistant_text_chars(traces_dir / conv_name)
            if chars > 0:
                isolated_chars.append(chars)

    non_iso_chars: list[int] = []  # per-turn chars (multi-turn shared context)
    if report.get("non_isolated_arm") is not None:
        for trial in report["non_isolated_arm"]["trials"]:
            prob_i = trial["problem_idx"]
            # New structure: multi-turn session with max_n turns, one file per turn
            if "answers" in trial:
                for turn_i in range(len(trial["answers"])):
                    conv_name = f"non-isolated-p{prob_i+1:02d}-t{turn_i+1:02d}.conv.jsonl"
                    chars = count_assistant_text_chars(traces_dir / conv_name)
                    if chars > 0:
                        non_iso_chars.append(chars)
            else:
                # Legacy structure (pre-multi-turn): one file per (N, problem) pair
                n = trial.get("n", 1)
                conv_name = f"non-isolated-N{n:02d}-p{prob_i+1:02d}.conv.jsonl"
                total_chars = count_assistant_text_chars(traces_dir / conv_name)
                if total_chars > 0 and n > 0:
                    non_iso_chars.append(total_chars // n)

    return isolated_chars, non_iso_chars


# ----------------------------------------------------------------------------
# Plot
# ----------------------------------------------------------------------------

def plot_amplification(report: dict, out_path: Path, traces_dir: Path) -> None:
    n_values = report["n_values"]
    iso_curve = report["isolated_arm"]["curve"]
    has_nis = report.get("non_isolated_arm") is not None
    nis_curve = report["non_isolated_arm"]["curve"] if has_nis else {}
    p_hat_cal = report["p_hat_calibration"]

    fig = plt.figure(figsize=(15, 12))
    gs = fig.add_gridspec(3, 2, hspace=0.45, wspace=0.30,
                          height_ratios=[1.4, 1.0, 0.9])

    # ------------------------------------------------------------------
    # Panel 1 (top, full width): Amplification curves
    # ------------------------------------------------------------------
    ax1 = fig.add_subplot(gs[0, :])

    # Closed-form prediction (smooth, with all integer N from 1 to max)
    n_smooth = list(range(1, max(n_values) + 1))
    pred_smooth = []
    for n in n_smooth:
        threshold = n // 2 + 1
        total = 0.0
        for i in range(threshold, n + 1):
            total += math.comb(n, i) * (p_hat_cal ** i) * ((1 - p_hat_cal) ** (n - i))
        pred_smooth.append(total)
    ax1.plot(n_smooth, pred_smooth, "k--", linewidth=2, alpha=0.6,
             label=f"closed-form prediction P_amp(N, p̂={p_hat_cal:.3f})")

    # Isolated arm — empirical points with CIs
    iso_xs = n_values
    iso_ys = [iso_curve[str(n)]["p_hat"] for n in n_values]
    iso_los = [iso_curve[str(n)]["ci_low"] for n in n_values]
    iso_his = [iso_curve[str(n)]["ci_high"] for n in n_values]
    iso_err_lo = [y - lo for y, lo in zip(iso_ys, iso_los)]
    iso_err_hi = [hi - y for y, hi in zip(iso_ys, iso_his)]
    ax1.errorbar(iso_xs, iso_ys, yerr=[iso_err_lo, iso_err_hi],
                 fmt="o-", color="#2C5F8D", markersize=10,
                 capsize=6, linewidth=2,
                 label="isolated arm (empirical, ±95% CI)")

    # Non-isolated arm (skipped if deleted)
    if has_nis:
        nis_xs = n_values
        nis_ys = [nis_curve[str(n)]["p_hat"] for n in n_values]
        nis_los = [nis_curve[str(n)]["ci_low"] for n in n_values]
        nis_his = [nis_curve[str(n)]["ci_high"] for n in n_values]
        nis_err_lo = [y - lo for y, lo in zip(nis_ys, nis_los)]
        nis_err_hi = [hi - y for y, hi in zip(nis_ys, nis_his)]
        ax1.errorbar(nis_xs, nis_ys, yerr=[nis_err_lo, nis_err_hi],
                     fmt="s-", color="#DC143C", markersize=10,
                     capsize=6, linewidth=2,
                     label="non-isolated arm (empirical, ±95% CI)")

    # Calibration baseline
    ax1.axhline(p_hat_cal, color="green", linestyle=":", alpha=0.6,
                label=f"single-trial p̂ = {p_hat_cal:.3f} (calibration)")

    ax1.set_xlabel("N (majority-vote size)", fontsize=12)
    ax1.set_ylabel("Empirical pass rate", fontsize=12)
    title = ("amplification curves: isolated vs non-isolated vs prediction"
             if has_nis else
             "amplification curves: isolated arm vs prediction")
    ax1.set_title(title, fontsize=14, fontweight="bold")
    ax1.set_xticks(n_values)
    ax1.set_ylim(-0.05, 1.05)
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc="lower right", fontsize=10, framealpha=0.9)

    # ------------------------------------------------------------------
    # Panel 2 (middle-left): per-attempt reasoning chars (isolated only)
    # ------------------------------------------------------------------
    ax2 = fig.add_subplot(gs[1, 0])
    iso_chars, nis_chars = collect_per_attempt_chars(report, traces_dir)
    if iso_chars:
        bins = 30
        ax2.hist(iso_chars, bins=bins, alpha=0.7, color="#2C5F8D",
                 label=f"isolated  (n={len(iso_chars)}, mean={sum(iso_chars)/len(iso_chars):.0f})")
        if has_nis and nis_chars:
            ax2.hist(nis_chars, bins=bins, alpha=0.55, color="#DC143C",
                     label=f"non-isolated  (n={len(nis_chars)}, mean={sum(nis_chars)/len(nis_chars):.0f})")
            ratio = (sum(iso_chars)/len(iso_chars)) / max(1, sum(nis_chars)/len(nis_chars))
            ax2.text(0.98, 0.98, f"ratio iso/non-iso = {ratio:.2f}×",
                     transform=ax2.transAxes, ha="right", va="top",
                     fontsize=10,
                     bbox=dict(boxstyle="round", facecolor="white", alpha=0.85))
        ax2.set_xlabel("Reasoning characters per attempt")
        ax2.set_ylabel("Frequency")
        title = "Per-attempt reasoning length (isolated arm)"
        ax2.set_title(title, fontsize=11, fontweight="bold")
        ax2.legend(fontsize=9)
        ax2.grid(True, alpha=0.3)
    else:
        ax2.text(0.5, 0.5, "No conversation logs found", ha="center", va="center",
                 transform=ax2.transAxes)
        ax2.axis("off")

    # ------------------------------------------------------------------
    # Panel 3 (middle-right): Per-cell pass/fail counts
    # ------------------------------------------------------------------
    ax3 = fig.add_subplot(gs[1, 1])
    iso_passes = [iso_curve[str(n)]["k"] for n in n_values]
    iso_total = [iso_curve[str(n)]["n_trials"] for n in n_values]
    x_pos = list(range(len(n_values)))

    if has_nis:
        width = 0.35
        nis_passes = [nis_curve[str(n)]["k"] for n in n_values]
        nis_total = [nis_curve[str(n)]["n_trials"] for n in n_values]
        ax3.bar([x - width/2 for x in x_pos], iso_passes, width,
                color="#2C5F8D", alpha=0.7, label="isolated passes")
        ax3.bar([x - width/2 for x in x_pos],
                [t - p for t, p in zip(iso_total, iso_passes)], width,
                bottom=iso_passes, color="#2C5F8D", alpha=0.25, label="isolated fails")
        ax3.bar([x + width/2 for x in x_pos], nis_passes, width,
                color="#DC143C", alpha=0.7, label="non-iso passes")
        ax3.bar([x + width/2 for x in x_pos],
                [t - p for t, p in zip(nis_total, nis_passes)], width,
                bottom=nis_passes, color="#DC143C", alpha=0.25, label="non-iso fails")
    else:
        width = 0.6
        ax3.bar(x_pos, iso_passes, width,
                color="#2C5F8D", alpha=0.7, label="isolated passes")
        ax3.bar(x_pos,
                [t - p for t, p in zip(iso_total, iso_passes)], width,
                bottom=iso_passes, color="#2C5F8D", alpha=0.25, label="isolated fails")
    ax3.set_xticks(x_pos)
    ax3.set_xticklabels([str(n) for n in n_values])
    ax3.set_xlabel("N")
    ax3.set_ylabel("Trials")
    ax3.set_title("Pass/fail counts per (arm, N)", fontsize=11, fontweight="bold")
    ax3.legend(fontsize=8, loc="upper left")
    ax3.grid(True, alpha=0.3, axis="y")

    # ------------------------------------------------------------------
    # Panel 4 (bottom-left): Per-problem heatmap (isolated arm)
    # ------------------------------------------------------------------
    ax4 = fig.add_subplot(gs[2, 0])
    iso_trials = report["isolated_arm"]["trials"]
    if iso_trials:
        max_n = max(len(t["answers"]) for t in iso_trials)
        grid = []
        for trial in iso_trials:
            row = []
            for ans in trial["answers"]:
                if ans is None:
                    row.append(-1)
                elif ans == trial["truth"]:
                    row.append(1)
                else:
                    row.append(0)
            row += [-1] * (max_n - len(row))
            grid.append(row)
        cmap = mcolors.ListedColormap(["#404040", "#DC143C", "#3CB371"])
        bounds = [-1.5, -0.5, 0.5, 1.5]
        norm = mcolors.BoundaryNorm(bounds, cmap.N)
        ax4.imshow(grid, cmap=cmap, norm=norm, aspect="auto", interpolation="nearest")
        ax4.set_xticks(range(max_n))
        ax4.set_xticklabels([f"a{i+1}" for i in range(max_n)], fontsize=8)
        ax4.set_yticks(range(len(iso_trials)))
        ax4.set_yticklabels([f"prob{i+1}" for i in range(len(iso_trials))], fontsize=8)
        ax4.set_title("Isolated arm: per-problem × per-attempt",
                      fontsize=11, fontweight="bold")
    else:
        ax4.axis("off")

    # ------------------------------------------------------------------
    # Panel 5 (bottom-right): Failure error magnitudes (log scale)
    # ------------------------------------------------------------------
    ax5 = fig.add_subplot(gs[2, 1])
    iso_fails: list[int] = []
    for trial in iso_trials:
        for ans in trial["answers"]:
            if ans is not None and ans != trial["truth"]:
                err = abs(ans - trial["truth"])
                if err > 0:
                    iso_fails.append(err)
    nis_fails: list[int] = []
    if has_nis:
        for t in report["non_isolated_arm"]["trials"]:
            # New (multi-turn) structure: iterate all answers in the session
            if "answers" in t:
                for ans in t["answers"]:
                    if ans is not None and ans != t["truth"]:
                        err = abs(ans - t["truth"])
                        if err > 0:
                            nis_fails.append(err)
            else:
                # Legacy structure: single reported answer
                if not t.get("passed") and t.get("reported") is not None:
                    err = abs(t["reported"] - t["truth"])
                    if err > 0:
                        nis_fails.append(err)
    if iso_fails or nis_fails:
        if iso_fails:
            ax5.hist([math.log10(e) for e in iso_fails], bins=15,
                     alpha=0.55, color="#2C5F8D",
                     label=f"isolated (n={len(iso_fails)})")
        if nis_fails:
            ax5.hist([math.log10(e) for e in nis_fails], bins=15,
                     alpha=0.55, color="#DC143C",
                     label=f"non-isolated (n={len(nis_fails)})")
        ax5.set_xlabel("log₁₀(|truth − reported|)")
        ax5.set_ylabel("Frequency")
        ax5.set_title("Failure error magnitudes", fontsize=11, fontweight="bold")
        ax5.legend(fontsize=9)
        ax5.grid(True, alpha=0.3, axis="y")
    else:
        ax5.text(0.5, 0.5, "No failures recorded", ha="center", va="center",
                 transform=ax5.transAxes)
        ax5.axis("off")

    # ------------------------------------------------------------------
    # Title summary
    # ------------------------------------------------------------------
    summary = (
        f"amplify Pass 2 — model: {report['model']}, "
        f"cell {report['cell_idx']}, p̂(cal)={p_hat_cal:.3f}, "
        f"M={report['M']}, N={n_values}"
    )
    fig.suptitle(summary, fontsize=12, fontweight="bold", y=0.995)

    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    print(f"Wrote {out_path}", file=sys.stderr)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", type=Path,
                        help="Path to amplify report.json (or its parent directory)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv[1:])

    if not args.input.exists():
        print(f"ERROR: {args.input} does not exist", file=sys.stderr)
        return 1

    if args.input.is_dir():
        report_path = args.input / "report.json"
        out_path = args.out or (args.input / "amplification.png")
        traces_dir = args.input
    else:
        report_path = args.input
        out_path = args.out or report_path.with_name("amplification.png")
        traces_dir = report_path.parent

    if not report_path.exists():
        print(f"ERROR: {report_path} does not exist", file=sys.stderr)
        return 1

    with report_path.open() as f:
        report = json.load(f)

    plot_amplification(report, out_path, traces_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
