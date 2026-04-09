#!/usr/bin/env python3
"""Closed-form Monte Carlo amplification prediction.

For a per-trial Bernoulli with success probability `p > 1/2`, the
probability that the majority of `N` independent trials is correct is:

  P_amp(N, p) = sum over i ≥ ceil(N/2) of  C(N, i) · p^i · (1 − p)^(N−i)

This is exact, derivable from the algorithm without running anything,
and approaches 1 exponentially fast in N. The amplify experiment
predicts that the **isolated arm** (one fresh sub-agent per trial,
giving genuine independence) will track this curve, and that the
**non-isolated arm** (N attempts inside one context, where trials are
correlated by the model attending to its prior reasoning) will not.

This script is the parent's authoritative source for predicted curves.
The verifier and the Pass 2 README both depend on it.

Usage:
  python3 verify/predict.py                      # demo grid: p in {0.55..0.95}, N in {1..9}
  python3 verify/predict.py --p 0.7 --n 5        # one number
  python3 verify/predict.py --p 0.7              # curve for one p across N=1,3,5,7,9
  python3 verify/predict.py --gap-table          # for the README, with target window highlighted
"""

from __future__ import annotations

import argparse
import sys
from math import comb


def amplified_success(p: float, n: int) -> float:
    """Probability that the majority of n independent Bernoulli(p) trials is correct.

    For odd n there is no tie. For even n a tie is broken pessimistically:
    a tie is treated as a failure (we want strict majority). This is the
    convention the amplify experiment uses, because it is the verifier-
    auditable definition: a tie produces no winner and the verifier scores
    it FAIL rather than guessing.
    """
    if not (0.0 <= p <= 1.0):
        raise ValueError(f"p must be in [0, 1], got {p}")
    if n < 1:
        raise ValueError(f"n must be ≥ 1, got {n}")

    # Strict majority: i must be > n/2.
    threshold = n // 2 + 1  # smallest i such that 2i > n
    total = 0.0
    for i in range(threshold, n + 1):
        total += comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
    return total


def boost(p: float, n: int) -> float:
    """The lift over a single trial: P_amp(n, p) − p."""
    return amplified_success(p, n) - p


def demo_grid() -> None:
    p_values = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
    n_values = [1, 3, 5, 7, 9, 11]

    print("Predicted amplified success rate P_amp(N, p) for independent trials:")
    print()
    print("  " + "p \\ N".ljust(8) + "".join(f"{n:>10}" for n in n_values))
    print("  " + "-" * 8 + "".join(f"{'-' * 9:>10}" for _ in n_values))
    for p in p_values:
        row = f"  {p:<8.2f}"
        for n in n_values:
            row += f"{amplified_success(p, n):>10.4f}"
        print(row)
    print()
    print("Same grid expressed as boost-over-single-trial (P_amp − p):")
    print()
    print("  " + "p \\ N".ljust(8) + "".join(f"{n:>10}" for n in n_values))
    print("  " + "-" * 8 + "".join(f"{'-' * 9:>10}" for _ in n_values))
    for p in p_values:
        row = f"  {p:<8.2f}"
        for n in n_values:
            row += f"{boost(p, n):>+10.4f}"
        print(row)


def gap_table(target_low: float = 0.55, target_high: float = 0.85) -> None:
    """Pretty-print the prediction grid with the target window marked.

    This is the table the README's Pass 2 prediction subsection should show.
    The target window is the range of `p` we expect Pass 1 to land in for
    a useful calibration; this script makes the predicted curve visible
    for any `p` we end up measuring.
    """
    print(f"Predicted P_amp(N, p) inside the calibration target window p ∈ [{target_low}, {target_high}]:")
    print()
    print("  " + "p \\ N".ljust(8) + f"{'1':>10}{'3':>10}{'5':>10}{'7':>10}{'9':>10}{'lift @ N=5':>14}")
    print("  " + "-" * 8 + ("-" * 9 + " ") * 5 + "-" * 14)
    p = target_low
    while p <= target_high + 1e-9:
        row = f"  {p:<8.2f}"
        for n in (1, 3, 5, 7, 9):
            row += f"{amplified_success(p, n):>10.4f}"
        row += f"{boost(p, 5):>+14.4f}"
        print(row)
        p = round(p + 0.05, 2)
    print()
    print("Reading: at the bottom of the target window (p=0.55), N=5 amplification")
    print("buys you about a 4-percentage-point lift, which is small but visible. At")
    print("the top of the target window (p=0.85), N=5 buys ~9 points and the curve")
    print("is steep — exactly the regime where the isolated/non-isolated gap should")
    print("be most observable.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--p", type=float, help="per-trial success probability")
    parser.add_argument("--n", type=int, help="number of trials")
    parser.add_argument("--gap-table", action="store_true", help="print the README target-window table")
    args = parser.parse_args(argv[1:])

    if args.gap_table:
        gap_table()
        return 0

    if args.p is not None and args.n is not None:
        print(f"P_amp(N={args.n}, p={args.p}) = {amplified_success(args.p, args.n):.6f}")
        print(f"boost (P_amp − p)         = {boost(args.p, args.n):+.6f}")
        return 0

    if args.p is not None:
        print(f"Curve for p={args.p}:")
        for n in (1, 3, 5, 7, 9, 11):
            print(f"  N={n:2d}  P_amp={amplified_success(args.p, n):.6f}  boost={boost(args.p, n):+.6f}")
        return 0

    demo_grid()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
