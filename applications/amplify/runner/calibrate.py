#!/usr/bin/env python3
"""Adaptive calibration search for amplify.

Goal: find a problem-difficulty cell where the chosen model's per-attempt
success rate `p` lands inside [target_low, target_high] (default 0.60-0.90)
with a tight enough confidence interval to call it calibrated.

Approach:

  1. A staircase of difficulty templates, each of which is a *generator*
     of fresh random problems at that complexity. Different trials at the
     same difficulty cell get different numbers, so the cell measures the
     model's general capability at that complexity, not its memory of one
     specific problem.

  2. For each visited cell, maintain (n_trials, k_passes) and compute a
     Wilson 95% confidence interval for p. (Wilson is closed-form, well-
     behaved at the extremes, and needs no scipy.)

  3. Acquisition function: bracket the target window with binary search
     across difficulty cells, then dwell on the candidate cell until its
     CI is narrow enough that the cell is decisively in or out of the
     target window.

  4. Parallel probes: each batch fires `--parallel` claude invocations
     simultaneously. Every invocation is a fresh sub-agent with no shared
     context, which is the same independence guarantee the runner relies
     on (and the same one Pass 2's isolated arm will need).

  5. Honor system on calculator use: the prompt tells the agent it cannot
     use Python or any tool to evaluate the arithmetic. The verifier
     cannot catch a violation directly (a Python-computed answer is
     correct, so it would pass), but the conversation logs are saved for
     post-hoc audit.

Output:
  - Live progress to stderr.
  - Per-batch results to stderr.
  - Final report (JSON) to stdout, plus a human-readable summary to stderr.
  - Conversation logs and stderr files written to traces/calibrate-<ts>/.

Usage:
  pixi run calibrate
  pixi run calibrate --model claude-haiku-4-5-20251001 --budget 60
  pixi run calibrate --target-low 0.55 --target-high 0.85
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# ----------------------------------------------------------------------------
# Wilson confidence interval (no scipy required).
# ----------------------------------------------------------------------------


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson score interval for k passes out of n trials.

    Returns (lower, point_estimate, upper) for a (1 - 2 * Phi(-z))-coverage
    interval. Default z=1.96 → 95% CI.

    For n=0 returns the trivial (0.0, 0.5, 1.0) — "we know nothing".
    """
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
# Problem templates: indexed difficulty staircase of arithmetic compounds.
# ----------------------------------------------------------------------------


@dataclass
class Problem:
    label: str  # short tag identifying the difficulty cell
    expression: str  # human-readable expression, e.g. "(12345 * 67890) // 234"
    truth: int  # ground-truth integer answer (computed by parent in Python)


def _rand_d(rng: random.Random, d: int) -> int:
    """Random d-digit positive integer (no leading zero)."""
    return rng.randint(10 ** (d - 1), 10**d - 1)


def _mul_template(d1: int, d2: int) -> Callable[[random.Random], Problem]:
    def gen(rng: random.Random) -> Problem:
        a, b = _rand_d(rng, d1), _rand_d(rng, d2)
        return Problem(
            label=f"mul-{d1}x{d2}",
            expression=f"{a} * {b}",
            truth=a * b,
        )

    return gen


def _muldiv_template(d1: int, d2: int, d3: int) -> Callable[[random.Random], Problem]:
    def gen(rng: random.Random) -> Problem:
        a, b, c = _rand_d(rng, d1), _rand_d(rng, d2), _rand_d(rng, d3)
        return Problem(
            label=f"muldiv-{d1}x{d2}//{d3}",
            expression=f"floor( ({a} * {b}) / {c} )",
            truth=(a * b) // c,
        )

    return gen


def _twomul_div_template(d1: int, d2: int, d3: int) -> Callable[[random.Random], Problem]:
    def gen(rng: random.Random) -> Problem:
        a, b = _rand_d(rng, d1), _rand_d(rng, d1)
        c, d = _rand_d(rng, d2), _rand_d(rng, d2)
        e = _rand_d(rng, d3)
        return Problem(
            label=f"twomul-({d1}x{d1}+{d2}x{d2})//{d3}",
            expression=f"floor( ({a} * {b} + {c} * {d}) / {e} )",
            truth=(a * b + c * d) // e,
        )

    return gen


def _chained_template(d1: int, d2: int, d3: int) -> Callable[[random.Random], Problem]:
    def gen(rng: random.Random) -> Problem:
        a, b = _rand_d(rng, d1), _rand_d(rng, d1)
        c = _rand_d(rng, d2)
        d, e = _rand_d(rng, d3), _rand_d(rng, d3)
        return Problem(
            label=f"chained-(({d1}x{d1})//{d2}+{d3})*{d3}",
            expression=f"( floor( ({a} * {b}) / {c} ) + {d} ) * {e}",
            truth=((a * b) // c + d) * e,
        )

    return gen


def make_difficulty_staircase() -> list[Callable[[random.Random], Problem]]:
    """Ordered list of problem generators, easiest first."""
    return [
        _mul_template(3, 3),  # 0: trivial
        _mul_template(4, 4),  # 1: easy
        _mul_template(5, 5),  # 2
        _mul_template(5, 7),  # 3
        _muldiv_template(5, 7, 4),  # 4: roughly the (15234534*2341234)//23423 regime
        _muldiv_template(6, 7, 5),  # 5
        _muldiv_template(7, 7, 5),  # 6
        _muldiv_template(8, 8, 6),  # 7
        _twomul_div_template(6, 6, 5),  # 8
        _twomul_div_template(7, 7, 5),  # 9
        _chained_template(6, 5, 4),  # 10: very hard
        _chained_template(7, 5, 4),  # 11
    ]


# ----------------------------------------------------------------------------
# Probe: spawn a fresh `claude` sub-agent and capture its answer.
# ----------------------------------------------------------------------------

PROMPT_TEMPLATE = """You will compute an arithmetic expression by hand. Read these rules carefully — they are the contract.

RULES:

1. You MAY write scratch work in your reasoning text. Long multiplication, decomposition into easier parts, long division, cross-checks, casting out nines — all encouraged. Show whatever work you need.

2. You MAY NOT use python, python3, bc, expr, calc, awk, dc, or any other tool to evaluate the multiplication, any sub-product of it, the division, any sub-quotient, or any arithmetic expression involving the input numbers. The verifier will compute the answer in Python independently and compare against yours; that is the parent's job, not yours. If you delegate the arithmetic to a tool you corrupt the experiment.

3. You MAY NOT search the web for the answer or any reformulation of it.

4. You MAY NOT ask another sub-agent or model to do the arithmetic.

5. Your final message must be EXACTLY one integer with no commas, spaces, units, scientific notation, or commentary. Example: 1522760063

The expression to compute is:

  {expression}

Begin."""


@dataclass
class ProbeResult:
    cell_idx: int
    label: str
    expression: str
    truth: int
    reported: Optional[int]
    passed: bool
    elapsed_s: float
    conv_path: str
    err_path: str
    returncode: Optional[int] = None
    outcome: str = "unknown"
    policy_violation: bool = False

    @property
    def counts_toward_estimate(self) -> bool:
        """Whether this result is a model sample, not a harness failure."""
        return self.outcome in MEASURED_OUTCOMES


def extract_final_int(conv_path: Path) -> Optional[int]:
    """Extract the last large integer from the assistant's final text message."""
    last_text = ""
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
                            last_text = c["text"]
    except FileNotFoundError:
        return None
    if not last_text:
        return None
    # The protocol says final message is exactly one integer. In practice
    # the model may add commentary; grab the last "long" integer in the text.
    candidates = re.findall(r"-?\b\d{2,}\b", last_text.strip())
    if not candidates:
        return None
    try:
        return int(candidates[-1])
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Per-probe /tmp isolation primitive
# ----------------------------------------------------------------------------
#
# Every probe runs in its own freshly-created tempfile.TemporaryDirectory()
# with a minimal .claude/CLAUDE.md and .claude/settings.json. This pattern is
# modeled on applications/orchestrator/runner/loop.py:invoke_claude_isolated().
#
# Why: the original (non-isolated) code path inherited the parent process's
# cwd, which meant `claude --print` auto-discovered whatever .claude/ files
# happened to be in the cwd hierarchy. This caused two real problems
# documented in docs/isolation-analysis-bug/:
#
#   1. Implicit dependency on cwd. If you ran calibrate.py from somewhere
#      outside applications/amplify/, the local .claude/ wasn't discovered,
#      the no-calculator rule wasn't enforced, and the model used Python
#      via Bash to compute the multiplication — silently breaking the
#      experimental contract.
#
#   2. Shared filesystem state across probes. All probes in a run shared the
#      same auto-discovered .claude/, which meant any race condition or
#      cache effect on Anthropic's side could affect probes inconsistently.
#
# The fix: each probe writes its own minimal .claude/ into a fresh /tmp dir
# and runs claude --print with cwd=that-tempdir. The tempdir is auto-deleted
# on context exit. There is no shared filesystem state between probes.
# The no-calculator contract is enforced by CLI-level tool restriction,
# not by permissions.allow. In current Claude Code, permissions.allow
# auto-allows matching tools; it is not a restrictive allowlist.

MINIMAL_CLAUDE_MD = """# amplify probe — isolated invocation

You are computing one arithmetic expression by hand and emitting one integer.

Rules:
- Compute by hand in your reasoning text. Long multiplication, decomposition,
  long division, casting out nines — all encouraged.
- Do not use any tool to compute the arithmetic. Bash is denied; do not try.
- Do not search the web.
- Final message: exactly one integer, no commentary, no commas, no units.
"""

# Project-local settings are intentionally minimal. The load-bearing
# no-tool constraint is the CLI's `--tools ""` plus strict MCP config below.
MINIMAL_SETTINGS_JSON = json.dumps(
    {
        "autoMemoryEnabled": False,
        "hooks": {},
    },
    indent=2,
)

NO_TOOL_CLAUDE_ARGS = [
    "--tools",
    "",
    "--strict-mcp-config",
    "--setting-sources",
    "project",
    "--disable-slash-commands",
]

FORBIDDEN_NO_TOOL_NAMES = {
    "Bash",
    "WebFetch",
    "WebSearch",
    "Task",
    "Read",
    "Edit",
    "Write",
}

MEASURED_OUTCOMES = {"model_correct", "model_wrong", "malformed"}


def build_no_tool_claude_cmd(
    prompt: str,
    model: str,
    effort: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
    resume_session_id: Optional[str] = None,
    persist_session: bool = False,
) -> list[str]:
    """Build a Claude Code command for a no-tool arithmetic attempt."""
    cmd = [
        "claude",
        "--print",
        prompt,
    ]
    if session_id:
        cmd += ["--session-id", session_id]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd += [
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        "30",
        *NO_TOOL_CLAUDE_ARGS,
    ]
    if not persist_session:
        cmd.append("--no-session-persistence")
    if effort:
        cmd += ["--effort", effort]
    return cmd


def trace_policy_violation(conv_path: Path) -> bool:
    """Return True if a no-tool trace lacks or violates tool metadata."""
    try:
        with conv_path.open() as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "system" or obj.get("subtype") != "init":
                    continue
                if "tools" not in obj or "mcp_servers" not in obj:
                    return True
                tools_raw = obj["tools"]
                mcp_servers_raw = obj["mcp_servers"]
                if not isinstance(tools_raw, list) or not isinstance(mcp_servers_raw, list):
                    return True
                tools = set(tools_raw)
                has_forbidden_builtin = bool(tools & FORBIDDEN_NO_TOOL_NAMES)
                has_mcp_tool = any(str(tool).startswith("mcp__") for tool in tools)
                has_mcp_server = bool(mcp_servers_raw)
                return has_forbidden_builtin or has_mcp_tool or has_mcp_server
    except FileNotFoundError:
        return True
    return True


def classify_probe_outcome(
    *,
    returncode: Optional[int],
    reported: Optional[int],
    truth: int,
    policy_violation: bool,
) -> str:
    """Classify probe outcome without mixing harness failures into p-hat."""
    if returncode is None:
        return "cli_not_found"
    if returncode != 0:
        return "cli_nonzero"
    if policy_violation:
        return "tool_policy_violation"
    if reported is None:
        return "malformed"
    if reported == truth:
        return "model_correct"
    return "model_wrong"


def probe(
    cell_idx: int,
    template: Callable[[random.Random], Problem],
    rng: random.Random,
    model: str,
    traces_dir: Path,
    probe_id: str,
    effort: Optional[str] = None,
) -> ProbeResult:
    """Spawn one fresh claude sub-agent in its own /tmp dir and check its answer.

    Each probe runs in a fresh tempfile.TemporaryDirectory() with a minimal
    .claude/CLAUDE.md and settings.json. The tempdir is deleted on context
    exit. No shared filesystem state with any other probe.
    """
    problem = template(rng)
    conv_path = traces_dir / f"{probe_id}.conv.jsonl"
    err_path = traces_dir / f"{probe_id}.stderr.txt"
    prompt = PROMPT_TEMPLATE.format(expression=problem.expression)

    cmd = build_no_tool_claude_cmd(prompt, model, effort)

    t0 = time.time()
    returncode: Optional[int] = None
    try:
        with tempfile.TemporaryDirectory(prefix=f"amplify-probe-{probe_id}-") as tmp:
            tmp_path = Path(tmp)
            claude_dir = tmp_path / ".claude"
            claude_dir.mkdir()
            (claude_dir / "CLAUDE.md").write_text(MINIMAL_CLAUDE_MD)
            (claude_dir / "settings.json").write_text(MINIMAL_SETTINGS_JSON)

            with conv_path.open("w") as conv_f, err_path.open("w") as err_f:
                proc = subprocess.run(
                    cmd,
                    cwd=str(tmp_path),
                    stdin=subprocess.DEVNULL,
                    stdout=conv_f,
                    stderr=err_f,
                    check=False,
                )
                returncode = proc.returncode
    except FileNotFoundError:
        elapsed = time.time() - t0
        return ProbeResult(
            cell_idx=cell_idx,
            label=problem.label,
            expression=problem.expression,
            truth=problem.truth,
            reported=None,
            passed=False,
            elapsed_s=elapsed,
            conv_path=str(conv_path),
            err_path=str(err_path),
            returncode=None,
            outcome="cli_not_found",
        )
    elapsed = time.time() - t0

    reported = extract_final_int(conv_path)
    policy_violation = trace_policy_violation(conv_path)
    outcome = classify_probe_outcome(
        returncode=returncode,
        reported=reported,
        truth=problem.truth,
        policy_violation=policy_violation,
    )
    passed = outcome == "model_correct"
    return ProbeResult(
        cell_idx=cell_idx,
        label=problem.label,
        expression=problem.expression,
        truth=problem.truth,
        reported=reported,
        passed=passed,
        elapsed_s=elapsed,
        conv_path=str(conv_path),
        err_path=str(err_path),
        returncode=returncode,
        outcome=outcome,
        policy_violation=policy_violation,
    )


# ----------------------------------------------------------------------------
# Search state and acquisition.
# ----------------------------------------------------------------------------


@dataclass
class CellState:
    idx: int
    n: int = 0
    k: int = 0
    history: list[ProbeResult] = field(default_factory=list)

    def update(self, result: ProbeResult) -> None:
        self.history.append(result)
        if not result.counts_toward_estimate:
            return
        self.n += 1
        if result.passed:
            self.k += 1

    def ci(self) -> tuple[float, float, float]:
        return wilson_ci(self.k, self.n)

    def status(self, target_low: float, target_high: float) -> str:
        if self.n == 0:
            return "unvisited"
        lo, _, hi = self.ci()
        if lo > target_high:
            return "decided-easy"
        if hi < target_low:
            return "decided-hard"
        if lo >= target_low and hi <= target_high:
            return "decided-in-window"
        return "candidate"


@dataclass
class Search:
    """Adaptive multi-cell calibration search.

    Three phases (interleaved, not strictly sequential):

      1. **Coarse sweep** — probe 3-5 cells across the staircase with small
         batches to learn where p ≈ target_mid lives. Bisection over the
         difficulty axis until we have one cell with p̂ near target_mid.

      2. **Centering** — among visited cells, pick the one whose p̂ is
         closest to target_mid (the middle of [target_low, target_high]).
         "Closest to center" beats "first to be in-window" because a cell
         with p̂ at the window edge will bounce around the boundary as
         more samples arrive and the CI never settles.

      3. **Dwelling with escape hatch** — accumulate trials at the
         centermost candidate. If after `dwell_patience` trials at one
         cell the CI hasn't narrowed enough (because the cell sits at
         the window edge), give up on it and pivot to whichever
         neighboring unvisited cell looks more promising. This is what
         makes the search **model-agnostic**: a model that's stronger
         than expected has its right cell at higher difficulty, a
         weaker model at lower difficulty, and the script finds either
         without operator intervention.

    The "centermost candidate" rule is the load-bearing change vs. the
    naive "first in-window" rule. It costs nothing (still O(probes)) but
    avoids the failure mode where the script locks onto a cell whose true
    p is at the window boundary and burns its budget on a CI that flickers
    in and out of the window forever.
    """

    cells: list[CellState]
    target_low: float
    target_high: float
    max_ci_width: float = 0.20  # CI must be ≤ this wide to call calibrated
    dwell_patience: int = 40  # max probes at one cell before pivoting
    initial_batch_min: int = 5  # min trials before considering a cell "characterized"
    committed_best_idx: Optional[int] = None  # set after triangulation finishes; locks dwelling

    def visited(self) -> list[CellState]:
        return [c for c in self.cells if c.n > 0]

    def target_mid(self) -> float:
        return (self.target_low + self.target_high) / 2.0

    def find_calibrated(self) -> Optional[CellState]:
        """Return the visited cell with the most confidence in being inside
        the target window.

        Calibration condition (looser than 'CI fully inside window' — that
        condition is unsatisfiable for cells whose true p is near the window
        edge, even with infinite samples):

          1. Point estimate p̂ is inside [target_low, target_high].
          2. Wilson 95% CI width is ≤ max_ci_width — we're confident
             in the estimate to within ±max_ci_width/2.

        Among cells satisfying both, pick the one with the smallest CI
        width (most precise estimate). Tie-broken toward cells closer to
        the target window center.
        """
        target_mid = self.target_mid()
        ok = []
        for c in self.visited():
            if c.n == 0:
                continue
            p_hat = c.k / c.n
            if not (self.target_low <= p_hat <= self.target_high):
                continue
            lo, _, hi = c.ci()
            if (hi - lo) > self.max_ci_width:
                continue
            ok.append(c)
        if not ok:
            return None
        return min(ok, key=lambda c: (c.ci()[2] - c.ci()[0], abs((c.k / c.n) - target_mid)))

    def _characterized(self) -> list[CellState]:
        """Cells with at least initial_batch_min trials. Their p̂ is meaningful enough to compare."""
        return [c for c in self.visited() if c.n >= self.initial_batch_min]

    def pick_next(self) -> Optional[int]:
        """Choose the next cell index to probe.

        The acquisition rule, in order:

          1. **Cold start.** Nothing visited → probe the staircase midpoint.

          2. **Finish characterization.** Some visited cells have < initial_batch_min
             trials → keep sampling the least-characterized one until at least one
             cell has the minimum sample size.

          3. **In-window exists.** At least one characterized cell has p̂ in
             [target_low, target_high]. Pick the one closest to target_mid.
             Then:

               a. **Triangulate.** If either immediate neighbor of the centermost
                  in-window cell hasn't been characterized yet, probe it first.
                  This is the load-bearing exploration step — it stops the
                  search from locking onto the first in-window cell without
                  checking whether a neighbor is actually closer to the middle
                  of the window.

               b. **Re-center.** Recompute the centermost cell across the
                  now-characterized neighborhood. If a neighbor turned out
                  to be a better candidate, pivot to it.

               c. **Escape hatch.** If the chosen cell has dwell_patience+
                  trials and its CI is still too wide, the cell is probably
                  at the window edge. Pivot to the side of the staircase
                  that should move p̂ toward the window center.

               d. **Dwell.** Otherwise, return the chosen cell.

          4. **Bisect.** No in-window candidate. Find the easy/hard
             boundary, probe between or beyond it.

          5. **Give up.** Staircase doesn't bracket the target.
        """
        visited = self.visited()
        if not visited:
            return len(self.cells) // 2

        # **Committed state**: once we've finished triangulating and chosen a
        # cell to dwell on, stop exploring. Just return that cell, with the
        # escape hatch as the only way to bail out.
        if self.committed_best_idx is not None:
            committed = self.cells[self.committed_best_idx]
            ci = committed.ci()
            p_hat = committed.k / committed.n if committed.n > 0 else 0.5
            # Pivot if: we've dwelled patience-many trials AND either
            #   (a) p̂ has drifted out of the window entirely, OR
            #   (b) CI is still wider than threshold AND p̂ is too close
            #       to the window edge to ever fit (within edge_safety).
            edge_safety = self.max_ci_width / 4
            p_at_edge = (
                p_hat < self.target_low + edge_safety or p_hat > self.target_high - edge_safety
            )
            if committed.n >= self.dwell_patience and (
                not (self.target_low <= p_hat <= self.target_high)
                or ((ci[2] - ci[0]) > self.max_ci_width and p_at_edge)
            ):
                pivot = self._pick_pivot_neighbor(committed)
                if pivot is not None:
                    self.committed_best_idx = None  # de-commit; let exploration take over
                    return pivot
            return committed.idx

        # 2. Finish characterization phase.
        partial = [c for c in visited if c.n < self.initial_batch_min]
        characterized = self._characterized()
        if partial and not characterized:
            return min(partial, key=lambda c: c.n).idx

        # 3. In-window candidate exists.
        in_window = [c for c in characterized if self.target_low <= (c.k / c.n) <= self.target_high]

        if in_window:
            target_mid = self.target_mid()
            # Sort: closeness to target_mid, then larger n (more precise estimate
            # wins ties), then smaller index (deterministic).
            in_window.sort(key=lambda c: (abs((c.k / c.n) - target_mid), -c.n, c.idx))
            best = in_window[0]

            # 3a. Triangulate: characterize both immediate neighbors first.
            for delta in (-1, +1):
                adj = best.idx + delta
                if 0 <= adj < len(self.cells):
                    if self.cells[adj].n < self.initial_batch_min:
                        return adj

            # 3b. Re-center across the now-characterized neighborhood.
            neighborhood = [c for c in characterized if abs(c.idx - best.idx) <= 1]
            in_window_neighborhood = [
                c for c in neighborhood if self.target_low <= (c.k / c.n) <= self.target_high
            ]
            if in_window_neighborhood:
                in_window_neighborhood.sort(
                    key=lambda c: (abs((c.k / c.n) - target_mid), -c.n, c.idx)
                )
                best = in_window_neighborhood[0]

            # 3c. **Commit**: every immediate neighbor of best is characterized,
            # so triangulation is done. Lock in the dwell target.
            self.committed_best_idx = best.idx
            return best.idx

        # 4. Bisection — no in-window candidate yet.
        easy = [c for c in characterized if (c.k / c.n) > self.target_high]
        hard = [c for c in characterized if (c.k / c.n) < self.target_low]

        max_easy = max((c.idx for c in easy), default=None)
        min_hard = min((c.idx for c in hard), default=None)

        if max_easy is not None and min_hard is not None:
            if min_hard - max_easy >= 2:
                return (max_easy + min_hard) // 2
            # Adjacent decided cells. Sample more at whichever has higher uncertainty.
            be = next(c for c in characterized if c.idx == max_easy)
            bh = next(c for c in characterized if c.idx == min_hard)
            return be.idx if (be.ci()[2] - be.ci()[0]) > (bh.ci()[2] - bh.ci()[0]) else bh.idx

        if max_easy is not None and min_hard is None:
            # Need to go harder.
            unsampled_higher = [
                c for c in self.cells if c.idx > max_easy and c.n < self.initial_batch_min
            ]
            if unsampled_higher:
                return unsampled_higher[len(unsampled_higher) // 2].idx
            return max(characterized, key=lambda c: c.idx).idx

        if min_hard is not None and max_easy is None:
            # Need to go easier.
            unsampled_lower = [
                c for c in self.cells if c.idx < min_hard and c.n < self.initial_batch_min
            ]
            if unsampled_lower:
                return unsampled_lower[len(unsampled_lower) // 2].idx
            return min(characterized, key=lambda c: c.idx).idx

        # 5. Fallback — shouldn't reach here under normal operation.
        return max(characterized, key=lambda c: c.ci()[2] - c.ci()[0]).idx

    def _pick_pivot_neighbor(self, current: CellState) -> Optional[int]:
        """When dwelling on `current` isn't producing a tight CI, decide
        which adjacent cell to try instead. Pick the side whose direction
        moves p̂ toward the window middle."""
        target_mid = self.target_mid()
        current_mean = current.k / current.n if current.n > 0 else 0.5
        prefer_harder = current_mean > target_mid  # too easy — go harder

        # Look at immediate neighbors first, then expand.
        for dist in (1, 2, 3):
            for direction in (+1, -1) if prefer_harder else (-1, +1):
                idx = current.idx + dist * direction
                if 0 <= idx < len(self.cells):
                    if self.cells[idx].n < self.dwell_patience:
                        return idx
        return None


# ----------------------------------------------------------------------------
# Main loop.
# ----------------------------------------------------------------------------


def run_calibration(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)

    templates = make_difficulty_staircase()
    cells = [CellState(idx=i) for i in range(len(templates))]
    search = Search(
        cells=cells,
        target_low=args.target_low,
        target_high=args.target_high,
        max_ci_width=args.max_ci_width,
    )

    repo_root = Path(__file__).resolve().parent.parent
    traces_root = repo_root / "traces"
    traces_root.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    run_dir = traces_root / f"calibrate-{ts}"
    run_dir.mkdir()

    # Tee stderr writes to a progress.log file inside the run directory.
    # This means all human-readable output is preserved in the run dir
    # automatically — no manual copying or shell redirect required.
    progress_log_path = run_dir / "progress.log"
    progress_log_file = progress_log_path.open("w", buffering=1)  # line-buffered

    class _Tee:
        def __init__(self, *streams):
            self._streams = streams

        def write(self, s):
            for stream in self._streams:
                stream.write(s)
                stream.flush()
            return len(s)

        def flush(self):
            for stream in self._streams:
                stream.flush()

    # Replace sys.stderr with a tee that writes to both the original stderr
    # and the progress log. The original stderr handle is captured first so
    # we don't recurse.
    _original_stderr = sys.stderr
    sys.stderr = _Tee(_original_stderr, progress_log_file)

    print("=== amplify calibration search ===", file=sys.stderr)
    print(f"  model:        {args.model}", file=sys.stderr)
    print(f"  effort:       {args.effort or '(CLI default)'}", file=sys.stderr)
    print(f"  target:       p ∈ [{args.target_low}, {args.target_high}]", file=sys.stderr)
    print(f"  max CI width: {args.max_ci_width}", file=sys.stderr)
    print(f"  budget:       {args.budget} probes", file=sys.stderr)
    print(f"  parallel:     {args.parallel}", file=sys.stderr)
    print(f"  staircase:    {len(templates)} cells", file=sys.stderr)
    print(f"  traces:       {run_dir}", file=sys.stderr)
    print("", file=sys.stderr)

    total_probes = 0
    probe_seq = 0
    t_start = time.time()

    while total_probes < args.budget:
        # Check stop condition first.
        calibrated = search.find_calibrated()
        if (
            calibrated is not None
            and (calibrated.ci()[2] - calibrated.ci()[0]) <= args.max_ci_width
        ):
            break

        next_idx = search.pick_next()
        if next_idx is None:
            print(
                "[search] staircase doesn't bracket the target window — giving up.", file=sys.stderr
            )
            break

        # Decide batch size for this iteration.
        batch_size = min(args.parallel, args.budget - total_probes)

        # Build the batch — same cell, fresh problems each.
        cell = cells[next_idx]
        cell_status = cell.status(search.target_low, search.target_high)
        print(
            f"[search] probing cell {next_idx} ({templates[next_idx](random.Random(0)).label}, "
            f"current n={cell.n}, k={cell.k}, status={cell_status}) "
            f"with batch of {batch_size}",
            file=sys.stderr,
        )

        with ThreadPoolExecutor(max_workers=batch_size) as ex:
            futures = []
            for _ in range(batch_size):
                probe_seq += 1
                probe_id = f"probe-{probe_seq:04d}-cell{next_idx}"
                # Each probe gets its own RNG instance to ensure parallel-safe
                # problem generation.
                child_rng = random.Random(rng.randrange(1 << 30))
                futures.append(
                    ex.submit(
                        probe,
                        next_idx,
                        templates[next_idx],
                        child_rng,
                        args.model,
                        run_dir,
                        probe_id,
                        args.effort,
                    )
                )
            for fut in as_completed(futures):
                result = fut.result()
                cell.update(result)
                total_probes += 1
                if not result.counts_toward_estimate:
                    print(
                        f"  ! cell {next_idx} {result.label} excluded from p-hat "
                        f"outcome={result.outcome} returncode={result.returncode} "
                        f"policy_violation={result.policy_violation} see={result.err_path}",
                        file=sys.stderr,
                    )
                    continue
                lo, ph, hi = cell.ci()
                marker = "✓" if result.passed else "✗"
                print(
                    f"  {marker} cell {next_idx} {result.label}  "
                    f"truth={result.truth}  reported={result.reported}  "
                    f"({result.elapsed_s:.1f}s)  → cell n={cell.n} k={cell.k} "
                    f"p̂={ph:.3f} CI=[{lo:.3f},{hi:.3f}]",
                    file=sys.stderr,
                )

    elapsed_total = time.time() - t_start

    # Final report.
    calibrated = search.find_calibrated()
    visited = search.visited()

    print("", file=sys.stderr)
    print("=== calibration summary ===", file=sys.stderr)
    print(f"  total probes:   {total_probes}", file=sys.stderr)
    print(f"  wall clock:     {elapsed_total:.1f}s", file=sys.stderr)
    print(f"  cells visited:  {len(visited)}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        f"  {'cell':<6} {'label':<22} {'n':>4} {'k':>4} {'p̂':>8} {'CI low':>8} {'CI high':>8}  status",
        file=sys.stderr,
    )
    print(
        f"  {'----':<6} {'-----':<22} {'-':>4} {'-':>4} {'--':>8} {'------':>8} {'-------':>8}  ------",
        file=sys.stderr,
    )
    for c in visited:
        lo, ph, hi = c.ci()
        # Pull a label sample from the template.
        label = templates[c.idx](random.Random(0)).label
        st = c.status(search.target_low, search.target_high)
        print(
            f"  {c.idx:<6} {label:<22} {c.n:>4} {c.k:>4} {ph:>8.3f} {lo:>8.3f} {hi:>8.3f}  {st}",
            file=sys.stderr,
        )
    print("", file=sys.stderr)

    if calibrated is not None:
        lo, ph, hi = calibrated.ci()
        label = templates[calibrated.idx](random.Random(0)).label
        print(f"  CALIBRATED: cell {calibrated.idx} ({label})", file=sys.stderr)
        print(f"  p̂ = {ph:.3f}, 95% CI = [{lo:.3f}, {hi:.3f}], n = {calibrated.n}", file=sys.stderr)
        verdict = "calibrated"
    else:
        print("  NO CALIBRATION FOUND in the staircase under this budget.", file=sys.stderr)
        verdict = "not-calibrated"

    # JSON report on stdout for programmatic consumption.
    report = {
        "verdict": verdict,
        "model": args.model,
        "effort": args.effort,
        "target_low": args.target_low,
        "target_high": args.target_high,
        "total_probes": total_probes,
        "wall_clock_s": elapsed_total,
        "traces_dir": str(run_dir),
        "calibrated_cell": calibrated.idx if calibrated else None,
        "cells": [
            {
                "idx": c.idx,
                "n": c.n,
                "k": c.k,
                "ci": list(c.ci()),
                "status": c.status(search.target_low, search.target_high),
                "history": [
                    {
                        "label": h.label,
                        "expression": h.expression,
                        "truth": h.truth,
                        "reported": h.reported,
                        "passed": h.passed,
                        "elapsed_s": h.elapsed_s,
                        "returncode": h.returncode,
                        "outcome": h.outcome,
                        "policy_violation": h.policy_violation,
                    }
                    for h in c.history
                ],
            }
            for c in visited
        ],
    }
    # Save the JSON report into the run directory automatically. Also print
    # it to stdout for piping/programmatic use.
    report_path = run_dir / "report.json"
    with report_path.open("w") as f:
        json.dump(report, f, indent=2)
    print(f"  report:       {report_path}", file=sys.stderr)
    print(json.dumps(report, indent=2))

    # Restore stderr and close the progress log.
    sys.stderr = _original_stderr
    progress_log_file.close()

    return 0 if verdict == "calibrated" else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--model", default="claude-haiku-4-5-20251001", help="Claude model to calibrate"
    )
    parser.add_argument(
        "--target-low", type=float, default=0.60, help="Lower edge of target p window"
    )
    parser.add_argument(
        "--target-high", type=float, default=0.90, help="Upper edge of target p window"
    )
    parser.add_argument(
        "--max-ci-width",
        type=float,
        default=0.25,
        help="CI width required to declare a cell calibrated",
    )
    parser.add_argument("--budget", type=int, default=120, help="Maximum total probes (cost cap)")
    parser.add_argument("--parallel", type=int, default=5, help="Parallel probes per batch")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    parser.add_argument(
        "--effort",
        default=None,
        choices=[None, "low", "medium", "high", "max"],
        help="claude --effort level (low/medium/high/max). Default: omit flag (CLI default).",
    )
    args = parser.parse_args(argv[1:])

    return run_calibration(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
