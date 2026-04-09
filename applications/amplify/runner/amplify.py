#!/usr/bin/env python3
"""Pass 2: the amplification experiment.

Takes the calibrated cell from a Pass 1 calibration report and runs the
two-arm amplification experiment to test whether binomial amplification
math actually materializes when N independent sub-agents vote, and whether
it breaks when the same N attempts share a context.

The two arms:

  - **Isolated arm.** For each of M fresh problems at the calibrated cell,
    spawn max(N) independent `claude` sub-agents in parallel — each in
    its own fresh context, all on the same problem. Collect all answers.
    Then derive trial outcomes for every N value by taking the majority
    of the first N answers. This is the load-bearing optimization that
    cuts the cost ~3-5x: one batch of max(N) answers is enough to extract
    one trial outcome at every smaller N.

  - **Non-isolated arm.** For each (M, N), spawn ONE `claude` sub-agent
    and ask it to perform N independent attempts inside the same context,
    then majority-vote them itself. Each subsequent attempt sees all
    prior attempts in the conversation, so the trials are NOT independent.
    The trials cannot be derived from each other across N values; each
    (M, N) cell costs M invocations.

Closed-form prediction (from verify/predict.py):

  P_amp(N, p) = sum over i ≥ ⌈N/2⌉ of  C(N, i) · p^i · (1-p)^(N-i)

  For p ≈ 0.74:
                     N=1     N=3     N=5     N=7     N=9    N=11
    P_amp(N, 0.74)   0.74    0.83    0.89    0.93    0.95   0.97

The hypothesis: the isolated arm's empirical curve tracks this prediction;
the non-isolated arm's curve does NOT. The gap is the operational
measurement of how load-bearing scope discipline really is.

Usage:
  python3 runner/amplify.py                          # smoke: M=3, N=1,5,9
  python3 runner/amplify.py --m 10 --n 1,3,5,7       # bigger smoke
  python3 runner/amplify.py --m 30 --n 1,3,5,7,9,11  # full publishable run
  python3 runner/amplify.py --calibration <path>     # use a specific report
  python3 runner/amplify.py --model <model>          # different model

Output:
  - Live progress to stderr (also tee'd to traces/amplify-<ts>/progress.log)
  - Final report.json saved into traces/amplify-<ts>/
  - Per-probe conversation logs saved alongside
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Reuse calibrate.py for problem generation, Wilson CI, probe machinery
sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibrate  # noqa: E402


# ----------------------------------------------------------------------------
# Loading the calibrated cell from a Pass 1 report
# ----------------------------------------------------------------------------

def find_latest_calibration_report(traces_root: Path) -> Optional[Path]:
    """Find the most recent calibrate-*/report.json by mtime."""
    candidates = sorted(traces_root.glob("calibrate-*/report.json"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True)
    return candidates[0] if candidates else None


def load_calibration(report_path: Path) -> dict:
    """Load a calibration report and validate it has a calibrated cell."""
    with report_path.open() as f:
        report = json.load(f)
    if report.get("calibrated_cell") is None:
        raise ValueError(
            f"calibration report {report_path} has no calibrated cell "
            f"(verdict: {report.get('verdict')}). Re-run calibration first."
        )
    return report


# ----------------------------------------------------------------------------
# Closed-form binomial amplification prediction
# ----------------------------------------------------------------------------

def p_amp_predicted(p: float, n: int) -> float:
    """Closed-form: probability of strict majority correct over N independent
    Bernoulli(p) trials. For even N, ties count as failure (no winner)."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    threshold = n // 2 + 1  # smallest i such that 2i > n
    total = 0.0
    for i in range(threshold, n + 1):
        total += math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
    return total


# ----------------------------------------------------------------------------
# Vote computation
# ----------------------------------------------------------------------------

def majority_vote(answers: list[Optional[int]]) -> Optional[int]:
    """Return the integer that appears strictly more than half the time, or
    None if there is no strict majority. None values are treated as votes
    that count as not-the-majority."""
    valid = [a for a in answers if a is not None]
    if not valid:
        return None
    counts = Counter(valid)
    most_common, count = counts.most_common(1)[0]
    if count * 2 > len(answers):
        return most_common
    return None


def trial_outcome(answers: list[Optional[int]], n: int, truth: int) -> bool:
    """Take the majority of the first n answers, return True iff it equals truth."""
    if len(answers) < n:
        raise ValueError(f"need at least {n} answers, got {len(answers)}")
    vote = majority_vote(answers[:n])
    return vote is not None and vote == truth


# ----------------------------------------------------------------------------
# Isolated arm
# ----------------------------------------------------------------------------

@dataclass
class IsolatedTrial:
    problem_idx: int
    expression: str
    truth: int
    answers: list[Optional[int]]
    elapsed_s: list[float]


def run_isolated_arm(
    template_fn,
    cell_idx: int,
    m: int,
    max_n: int,
    model: str,
    run_dir: Path,
    parallel: int,
    rng: random.Random,
) -> list[IsolatedTrial]:
    """For each of M problems, spawn max_n fresh sub-agents and collect answers.

    Returns one IsolatedTrial per problem, holding all max_n answers. Trial
    outcomes at any specific N are computed downstream by taking the majority
    of the first N answers.
    """
    trials: list[IsolatedTrial] = []
    for prob_i in range(m):
        problem = template_fn(rng)
        print(f"[isolated] problem {prob_i+1}/{m}  expr={problem.expression[:80]}{'...' if len(problem.expression)>80 else ''}",
              file=sys.stderr)

        answers: list[Optional[int]] = [None] * max_n
        elapseds: list[float] = [0.0] * max_n

        # Fire max_n parallel probes on the same problem
        with ThreadPoolExecutor(max_workers=parallel) as ex:
            futures = {}
            for attempt_i in range(max_n):
                probe_id = f"isolated-p{prob_i+1:02d}-a{attempt_i+1:02d}"
                child_rng = random.Random(rng.randrange(1 << 30))
                # We override the template's RNG to use the same problem
                # for all attempts in this trial.
                fixed_template = lambda _rng, _p=problem: _p
                fut = ex.submit(
                    calibrate.probe, cell_idx, fixed_template, child_rng,
                    model, run_dir, probe_id,
                )
                futures[fut] = attempt_i
            for fut in as_completed(futures):
                attempt_i = futures[fut]
                result = fut.result()
                answers[attempt_i] = result.reported
                elapseds[attempt_i] = result.elapsed_s
                mark = "✓" if result.passed else "✗"
                print(f"  {mark} attempt {attempt_i+1}/{max_n}  reported={result.reported}  truth={problem.truth}  ({result.elapsed_s:.1f}s)",
                      file=sys.stderr)

        trials.append(IsolatedTrial(
            problem_idx=prob_i,
            expression=problem.expression,
            truth=problem.truth,
            answers=answers,
            elapsed_s=elapseds,
        ))
    return trials


# ----------------------------------------------------------------------------
# Non-isolated arm — multi-turn shared-context retry sessions
# ----------------------------------------------------------------------------
#
# The non-isolated arm tests the hypothesis: when N attempts at the same
# problem run in a SHARED conversation (so each attempt can see all prior
# attempts in its context), do they amplify like N independent attempts do?
# Or do they correlate (because the model anchors on its prior answers)?
#
# Implementation: each problem gets ONE persistent claude session that runs
# for max_n turns. Turn 1 sends the matched-prompt (identical to the
# isolated arm's prompt) via `claude --print --session-id <uuid>`. Turns 2
# through max_n send a short retry prompt via `claude --print --resume <uuid>`
# which appends a new user message to the existing conversation, producing
# a new assistant turn that can see all prior turns.
#
# Each turn is its own subprocess.run. The session state is persisted to
# disk by Claude Code between subprocess invocations. The whole multi-turn
# session runs inside one tempfile.TemporaryDirectory() that persists for
# all max_n turns of one problem and is auto-deleted when that problem's
# session completes.
#
# Nested sampling optimization: M problems × max_n turns each. For any
# N <= max_n, the trial outcome at N is computed by majority voting the
# first N answers from the same session. Same approach as the isolated arm.
#
# Matched-prompt invariant: turn 1 sends calibrate.PROMPT_TEMPLATE
# unchanged, so at N=1 the isolated and non-isolated arms are sampling the
# same distribution (single fresh attempt with identical prompt). The
# divergence between arms is purely structural (separate processes vs same
# session) and only manifests for N >= 2.

# Turn 1 prompt = the isolated arm's prompt verbatim. Pulled from calibrate
# at runtime so any change to PROMPT_TEMPLATE propagates automatically.

# Turns 2..max_n: short retry prompt that asks for another fresh attempt.
# The model sees all prior turns in context. Whether it produces a genuinely
# new computation or anchors on its prior answer is the experimental finding.
RETRY_TURN_PROMPT = """Now perform another completely independent attempt at the same expression.

Do NOT trust your prior reasoning or copy your prior answer. Recompute the entire calculation from scratch. You may use a different decomposition strategy if you like (e.g. if you used long multiplication before, try the distributive expansion this time).

The same rules apply: no python, no bc, no calculator of any kind, no web search. Compute by hand in your reasoning text.

Your final message must be EXACTLY one integer — the result of THIS attempt — with no commas, spaces, units, scientific notation, or commentary. Example: 1522760063"""


@dataclass
class NonIsolatedTrial:
    """One multi-turn session for one problem.

    Mirrors IsolatedTrial: holds max_n answers, allows derivation of trial
    outcomes at any N <= max_n via majority vote of first N answers.
    """
    problem_idx: int
    expression: str
    truth: int
    answers: list[Optional[int]]   # length max_n
    elapsed_s: list[float]         # length max_n
    session_id: str                # the conversation UUID for traceability


def run_non_isolated_arm(
    template_fn,
    cell_idx: int,
    m: int,
    max_n: int,
    model: str,
    run_dir: Path,
    parallel: int,
    rng: random.Random,
) -> list[NonIsolatedTrial]:
    """For each of M problems, run one max_n-turn session in shared context.

    Sessions for different problems are independent and can run in parallel.
    Each session uses its own tempdir and its own session UUID.
    """
    trials: list[NonIsolatedTrial] = []

    # Generate problems first (deterministic from rng)
    problems_with_seeds = []
    for prob_i in range(m):
        problem = template_fn(rng)
        session_id = str(uuid.UUID(int=rng.getrandbits(128)))
        problems_with_seeds.append((prob_i, problem, session_id))

    # Run sessions in parallel (each session is its own thread,
    # serialized internally across max_n turns)
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {}
        for prob_i, problem, session_id in problems_with_seeds:
            session_label = f"non-isolated-p{prob_i+1:02d}"
            fut = ex.submit(
                _non_isolated_session,
                problem, max_n, model, run_dir, session_label, session_id,
            )
            futs[fut] = (prob_i, problem)
        for fut in as_completed(futs):
            prob_i, problem = futs[fut]
            trial = fut.result()
            trial.problem_idx = prob_i
            trials.append(trial)
            n_passed = sum(1 for a in trial.answers if a == problem.truth)
            print(f"[non-isolated] problem {prob_i+1}/{m}  passes {n_passed}/{max_n}  "
                  f"truth={problem.truth}  expr={problem.expression[:60]}",
                  file=sys.stderr)
    # Sort by problem_idx for stable output
    trials.sort(key=lambda t: t.problem_idx)
    return trials


def _non_isolated_session(
    problem: calibrate.Problem,
    max_n: int,
    model: str,
    run_dir: Path,
    session_label: str,
    session_id: str,
) -> NonIsolatedTrial:
    """Run one max_n-turn session: turn 1 with --session-id, then --resume.

    The whole session runs inside one tempfile.TemporaryDirectory() that
    persists for all max_n turns and is auto-deleted on context exit. Each
    turn writes its own conversation log file inside run_dir.
    """
    answers: list[Optional[int]] = [None] * max_n
    elapseds: list[float] = [0.0] * max_n

    try:
        with tempfile.TemporaryDirectory(prefix=f"amplify-{session_label}-") as tmp:
            tmp_path = Path(tmp)
            claude_dir = tmp_path / ".claude"
            claude_dir.mkdir()
            (claude_dir / "CLAUDE.md").write_text(calibrate.MINIMAL_CLAUDE_MD)
            (claude_dir / "settings.json").write_text(calibrate.MINIMAL_SETTINGS_JSON)

            for turn_i in range(max_n):
                conv_path = run_dir / f"{session_label}-t{turn_i+1:02d}.conv.jsonl"
                err_path = run_dir / f"{session_label}-t{turn_i+1:02d}.stderr.txt"

                if turn_i == 0:
                    # Turn 1: matched prompt + --session-id
                    prompt = calibrate.PROMPT_TEMPLATE.format(expression=problem.expression)
                    cmd = [
                        "claude", "--print", prompt,
                        "--session-id", session_id,
                        "--model", model,
                        "--output-format", "stream-json", "--verbose",
                        "--max-turns", "30",
                        "--permission-mode", "bypassPermissions",
                    ]
                else:
                    # Turns 2..max_n: retry prompt + --resume
                    prompt = RETRY_TURN_PROMPT
                    cmd = [
                        "claude", "--print", prompt,
                        "--resume", session_id,
                        "--model", model,
                        "--output-format", "stream-json", "--verbose",
                        "--max-turns", "30",
                        "--permission-mode", "bypassPermissions",
                    ]

                t0 = time.time()
                try:
                    with conv_path.open("w") as cf, err_path.open("w") as ef:
                        subprocess.run(
                            cmd,
                            cwd=str(tmp_path),
                            stdin=subprocess.DEVNULL,
                            stdout=cf,
                            stderr=ef,
                            check=False,
                        )
                except FileNotFoundError:
                    pass
                elapseds[turn_i] = time.time() - t0
                answers[turn_i] = calibrate.extract_final_int(conv_path)
    except FileNotFoundError:
        pass

    return NonIsolatedTrial(
        problem_idx=-1,
        expression=problem.expression,
        truth=problem.truth,
        answers=answers,
        elapsed_s=elapseds,
        session_id=session_id,
    )


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def run_amplify(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    repo_root = Path(__file__).resolve().parent.parent
    traces_root = repo_root / "traces"
    traces_root.mkdir(exist_ok=True)

    # Find or use calibration report
    if args.calibration:
        cal_path = Path(args.calibration)
    else:
        cal_path = find_latest_calibration_report(traces_root)
        if cal_path is None:
            print("ERROR: no calibrate-*/report.json found in traces/. "
                  "Run `python3 runner/calibrate.py` first or pass --calibration.",
                  file=sys.stderr)
            return 2
    cal_report = load_calibration(cal_path)
    cell_idx = cal_report["calibrated_cell"]

    # Pull p̂ for the predicted curve
    cell_data = next(c for c in cal_report["cells"] if c["idx"] == cell_idx)
    p_hat = cell_data["k"] / cell_data["n"]

    # Get the matching template from calibrate.py's staircase
    templates = calibrate.make_difficulty_staircase()
    template = templates[cell_idx]

    n_values = sorted(set(int(x) for x in args.n.split(",")))
    if any(n < 1 for n in n_values):
        print("ERROR: N values must be >= 1", file=sys.stderr)
        return 2
    max_n = max(n_values)

    # Set up trace dir
    ts = time.strftime("%Y%m%d-%H%M%S")
    run_dir = traces_root / f"amplify-{ts}"
    run_dir.mkdir()

    # Tee stderr to progress log
    progress_log_path = run_dir / "progress.log"
    progress_log_file = progress_log_path.open("w", buffering=1)

    class _Tee:
        def __init__(self, *streams):
            self._streams = streams
        def write(self, s):
            for st in self._streams:
                st.write(s); st.flush()
            return len(s)
        def flush(self):
            for st in self._streams:
                st.flush()

    _original_stderr = sys.stderr
    sys.stderr = _Tee(_original_stderr, progress_log_file)

    print(f"=== amplify Pass 2: amplification experiment ===", file=sys.stderr)
    print(f"  calibration:  {cal_path}", file=sys.stderr)
    print(f"  cell idx:     {cell_idx}", file=sys.stderr)
    print(f"  template:     {template(random.Random(0)).label}", file=sys.stderr)
    print(f"  p̂ (from cal): {p_hat:.3f}", file=sys.stderr)
    print(f"  model:        {args.model}", file=sys.stderr)
    print(f"  M (trials):   {args.m}", file=sys.stderr)
    print(f"  N values:     {n_values}", file=sys.stderr)
    print(f"  max_n:        {max_n}", file=sys.stderr)
    print(f"  parallel:     {args.parallel}", file=sys.stderr)
    print(f"  traces:       {run_dir}", file=sys.stderr)
    iso_invs = args.m * max_n
    nis_invs = 0 if args.skip_non_isolated else args.m * len(n_values)
    if args.skip_non_isolated:
        print(f"  invocations:  isolated={iso_invs}, non-isolated=SKIPPED, total={iso_invs}", file=sys.stderr)
    else:
        print(f"  invocations:  isolated={iso_invs}, non-isolated={nis_invs}, total={iso_invs + nis_invs}", file=sys.stderr)
    print("", file=sys.stderr)

    t_start = time.time()

    # Isolated arm
    print("--- ISOLATED ARM ---", file=sys.stderr)
    isolated_trials = run_isolated_arm(
        template, cell_idx, args.m, max_n, args.model,
        run_dir, args.parallel, rng,
    )

    print("", file=sys.stderr)

    # Non-isolated arm — multi-turn shared-context sessions
    if args.skip_non_isolated:
        print("--- NON-ISOLATED ARM (SKIPPED) ---", file=sys.stderr)
        non_isolated_trials = []
    else:
        print("--- NON-ISOLATED ARM (multi-turn shared context) ---", file=sys.stderr)
        non_isolated_trials = run_non_isolated_arm(
            template, cell_idx, args.m, max_n, args.model,
            run_dir, args.parallel, rng,
        )

    elapsed_total = time.time() - t_start

    # Compute curves
    isolated_curve = {}
    for n in n_values:
        outcomes = [trial_outcome(t.answers, n, t.truth) for t in isolated_trials]
        k = sum(1 for o in outcomes if o)
        lo, ph, hi = calibrate.wilson_ci(k, len(outcomes))
        isolated_curve[n] = {
            "n_trials": len(outcomes),
            "k": k,
            "p_hat": ph,
            "ci_low": lo,
            "ci_high": hi,
            "predicted": p_amp_predicted(p_hat, n),
        }

    non_isolated_curve = {}
    if not args.skip_non_isolated:
        for n in n_values:
            outcomes = [trial_outcome(t.answers, n, t.truth) for t in non_isolated_trials]
            k = sum(1 for o in outcomes if o)
            lo, ph, hi = calibrate.wilson_ci(k, len(outcomes))
            non_isolated_curve[n] = {
                "n_trials": len(outcomes),
                "k": k,
                "p_hat": ph,
                "ci_low": lo,
                "ci_high": hi,
                "predicted": p_amp_predicted(p_hat, n),
            }

    # Print summary
    print("", file=sys.stderr)
    print("=== amplification curves ===", file=sys.stderr)
    if args.skip_non_isolated:
        print(f"{'N':>3}  {'predicted':>10}  {'isolated p̂':>12}  {'iso CI':>16}",
              file=sys.stderr)
        print(f"{'-':>3}  {'---------':>10}  {'-----------':>12}  {'------':>16}",
              file=sys.stderr)
        for n in n_values:
            ic = isolated_curve[n]
            iso_ci = f"[{ic['ci_low']:.3f},{ic['ci_high']:.3f}]"
            print(f"{n:>3}  {ic['predicted']:>10.3f}  {ic['p_hat']:>12.3f}  {iso_ci:>16}",
                  file=sys.stderr)
    else:
        print(f"{'N':>3}  {'predicted':>10}  {'isolated p̂':>12}  {'iso CI':>16}  {'non-iso p̂':>12}  {'non-iso CI':>16}",
              file=sys.stderr)
        print(f"{'-':>3}  {'---------':>10}  {'-----------':>12}  {'------':>16}  {'----------':>12}  {'----------':>16}",
              file=sys.stderr)
        for n in n_values:
            ic = isolated_curve[n]
            nc = non_isolated_curve[n]
            iso_ci = f"[{ic['ci_low']:.3f},{ic['ci_high']:.3f}]"
            nis_ci = f"[{nc['ci_low']:.3f},{nc['ci_high']:.3f}]"
            print(f"{n:>3}  {ic['predicted']:>10.3f}  {ic['p_hat']:>12.3f}  {iso_ci:>16}  {nc['p_hat']:>12.3f}  {nis_ci:>16}",
                  file=sys.stderr)
    print("", file=sys.stderr)
    print(f"  total wall clock: {elapsed_total:.0f}s", file=sys.stderr)
    print(f"  total invocations: {iso_invs + nis_invs}", file=sys.stderr)

    # Build report
    report = {
        "calibration_report": str(cal_path),
        "model": args.model,
        "cell_idx": cell_idx,
        "p_hat_calibration": p_hat,
        "M": args.m,
        "n_values": n_values,
        "wall_clock_s": elapsed_total,
        "isolated_arm": {
            "trials": [
                {
                    "problem_idx": t.problem_idx,
                    "expression": t.expression,
                    "truth": t.truth,
                    "answers": t.answers,
                    "elapsed_s": t.elapsed_s,
                }
                for t in isolated_trials
            ],
            "curve": {str(n): isolated_curve[n] for n in n_values},
        },
        "non_isolated_arm": None if args.skip_non_isolated else {
            "trials": [
                {
                    "problem_idx": t.problem_idx,
                    "expression": t.expression,
                    "truth": t.truth,
                    "answers": t.answers,
                    "elapsed_s": t.elapsed_s,
                    "session_id": t.session_id,
                }
                for t in non_isolated_trials
            ],
            "curve": {str(n): non_isolated_curve[n] for n in n_values},
        },
    }
    if args.skip_non_isolated:
        report["non_isolated_arm_skip_reason"] = (
            "Skipped via --skip-non-isolated."
        )

    report_path = run_dir / "report.json"
    with report_path.open("w") as f:
        json.dump(report, f, indent=2)
    print(f"  report:       {report_path}", file=sys.stderr)
    print(json.dumps(report, indent=2))

    sys.stderr = _original_stderr
    progress_log_file.close()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibration", default=None,
                        help="Path to calibrate-*/report.json. If omitted, uses the most recent.")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--m", type=int, default=3,
                        help="Trials per (arm, N) cell. Default 3 for proof-of-concept smoke.")
    parser.add_argument("--n", default="1,5,9",
                        help="Comma-separated N values. Default '1,5,9' for proof-of-concept.")
    parser.add_argument("--parallel", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--skip-non-isolated", action="store_true",
                        help="Skip the non-isolated arm. The current non-isolated arm "
                             "is broken (uses --print which is single-turn). Set this "
                             "to run only the isolated arm cleanly.")
    args = parser.parse_args(argv[1:])
    return run_amplify(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
