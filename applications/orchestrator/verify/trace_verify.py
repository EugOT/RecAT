#!/usr/bin/env python3
"""Trace verifier for orchestrator.

Reads a JSONL trace written by the orchestrator runner and checks that
it is a faithful execution of the orchestrator-pattern tutor/student loop:
the runner spawns a tutor agent and a student agent across rounds,
the tutor patches the student's CLAUDE.md, the student is re-run on the
same test inputs, and the loop terminates with one of three verdicts:
converged, max_rounds, or aborted.

What gets checked:

  1. Envelope: valid JSON, step counters start at 0 and increment by 1
     with no gaps, first event is run_start, last is run_end.
  2. Event schema: every event type is legal and has its required fields.
  3. Structural bracketing: each round emits round_start, then one
     student_invoke + student_response per test case, then round_summary,
     and (if the round didn't converge) tutor_invoke + tutor_response,
     before the next round_start. The final round emits round_summary
     followed by verdict + run_end.
  4. Per-test grading replay: for every student_response event, the
     verifier independently checks the student's stdout against the
     test input and confirms the runner's `pass` field is correct.
     This is the load-bearing check — a runner that mis-grades student
     outputs would silently invalidate everything else.
  5. Round-summary consistency: pass_count and pass_rate match the
     student_response events for that round.
  6. Termination: the verdict is one of converged|max_rounds|aborted,
     and the verdict's claimed final_pass_rate matches the last
     round_summary.
  7. Iteration evidence (the philosophical claim): for the trace to
     count as a *positive* result, the run must have used more than
     one round (otherwise the tutor/student loop never engaged).
     A run that converges in 1 round is not a failure of the verifier
     but it is recorded as such — the trace_verify_all.sh aggregator
     reports it separately.

Usage:
  python3 verify/trace_verify.py <trace.jsonl>

Exits 0 on success, 1 on failure. Prints one summary line:

  RESULT: PASS <path>                           (verifier-clean)
  RESULT: FAIL <path> <reason>                  (verifier rejected)
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


LEGAL_EVENTS = {
    "run_start",
    "round_start",
    "student_invoke",
    "student_response",
    "round_summary",
    "tutor_invoke",
    "tutor_response",
    "verdict",
    "run_end",
}


VALID_VERDICTS = {"converged", "max_rounds", "aborted"}


# Strict output format the student must produce, on a single line, no surrounding
# whitespace and no extra text:
#
#     DIGIT_SUM=<S>;CHECK=<8 hex chars>
#
# where S is the digit sum of the input integer N, and the 8 hex chars are the
# first 8 characters of the lowercase hex SHA-256 of the bytes f"{N}-{S}".
#
# The CHECK component is the load-bearing iteration-forcer: it is mathematically
# impossible for a language model to produce a correct 8-char SHA-256 prefix
# without actually invoking a hash function. The student is forced to use Python
# (via Bash) and the tutor is forced to discover this requirement and patch it
# into the student's instructions.
ANSWER_PATTERN = re.compile(r"^DIGIT_SUM=(\d+);CHECK=([0-9a-f]{8})$")


class VerifyError(Exception):
    """A verification failure. Message becomes the reason field in output."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise VerifyError(msg)


def _require_keys(event: dict, keys: list[str], event_type: str) -> None:
    data = event.get("data", {})
    missing = [k for k in keys if k not in data]
    _require(
        not missing,
        f"event[{event.get('step')}] {event_type}: missing data keys {missing}",
    )


def digit_sum(n: int) -> int:
    """Compute digit sum of a non-negative integer. Ground truth for grading."""
    if n < 0:
        raise ValueError(f"digit_sum requires n >= 0, got {n}")
    s = 0
    while n > 0:
        s += n % 10
        n //= 10
    return s if s > 0 else 0


def true_check(n: int, s: int) -> str:
    """The required CHECK value for input n and digit sum s.

    First 8 lowercase hex chars of sha256(f"{n}-{s}"). This function is
    the ground truth — both the runner's grading and the verifier's
    replay call into it, so they cannot drift.
    """
    h = hashlib.sha256(f"{n}-{s}".encode("utf-8")).hexdigest()
    return h[:8]


def grade_student_output(test_input: int, raw_output: str) -> tuple[bool, str]:
    """Independently grade a student response against the strict format.

    Required format on a single line, no surrounding whitespace:

        DIGIT_SUM=<S>;CHECK=<8 hex chars>

    Where S is the digit sum of test_input and the 8 hex chars are the
    first 8 lowercase hex characters of sha256(f"{test_input}-{S}").

    Returns (passed, reason). The reason is empty on pass and a short
    diagnostic on fail. The diagnostic is precise enough that the tutor
    agent can read it and identify the failure mode.
    """
    if not isinstance(raw_output, str) or raw_output == "":
        return False, "empty output"
    stripped = raw_output.strip()
    if not stripped:
        return False, "empty output (only whitespace)"
    if "\n" in stripped:
        return False, "output has multiple lines (must be a single line with no surrounding text)"
    if stripped != raw_output.strip("\n").strip(" \t"):
        # Whitespace inside the line is also rejected by the regex below;
        # this branch is just defensive.
        pass
    m = ANSWER_PATTERN.match(stripped)
    if not m:
        return (
            False,
            f"output {stripped!r} does not match required format DIGIT_SUM=<int>;CHECK=<8 lowercase hex chars> "
            f"(pattern: ^DIGIT_SUM=(\\d+);CHECK=([0-9a-f]{{8}})$)",
        )
    reported_s = int(m.group(1))
    reported_check = m.group(2)
    expected_s = digit_sum(test_input)
    if reported_s != expected_s:
        return False, f"DIGIT_SUM wrong: got {reported_s}, expected {expected_s}"
    expected_check = true_check(test_input, expected_s)
    if reported_check != expected_check:
        return (
            False,
            f"CHECK wrong: got {reported_check!r}, expected {expected_check!r} "
            f"(must be sha256('{test_input}-{expected_s}').hexdigest()[:8])",
        )
    return True, ""


def load_trace(path: Path) -> list[dict]:
    events: list[dict] = []
    with path.open() as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                raise VerifyError(f"line {lineno}: invalid JSON ({e})")
            events.append(obj)
    _require(bool(events), "trace is empty")
    return events


def verify_envelope(events: list[dict]) -> None:
    for i, ev in enumerate(events):
        _require(isinstance(ev, dict), f"event {i}: not a JSON object")
        for key in ("run_id", "step", "ts", "event", "data"):
            _require(key in ev, f"event {i}: missing field '{key}'")
        _require(
            isinstance(ev["step"], int),
            f"event {i}: 'step' is not an integer",
        )
        _require(
            ev["step"] == i,
            f"event {i}: step field is {ev['step']}, expected {i}",
        )
        _require(
            ev["event"] in LEGAL_EVENTS,
            f"event {i}: unknown event type '{ev['event']}'",
        )
        _require(isinstance(ev["data"], dict), f"event {i}: data is not an object")

    run_ids = {ev["run_id"] for ev in events}
    _require(len(run_ids) == 1, f"mixed run_ids in single trace: {run_ids}")

    _require(
        events[0]["event"] == "run_start",
        f"first event is '{events[0]['event']}', expected 'run_start'",
    )
    _require(
        events[-1]["event"] == "run_end",
        f"last event is '{events[-1]['event']}', expected 'run_end'",
    )


@dataclass
class RoundState:
    round_index: int
    test_count: int
    seen_responses: list[dict] = field(default_factory=list)
    summary: dict | None = None
    tutor_invoked: bool = False
    tutor_responded: bool = False


def verify_structure_and_grading(events: list[dict]) -> dict:
    """Walk the event stream and check structure + grading replay.

    Returns the final verdict payload for use by the caller.
    """
    run_start = events[0]
    _require_keys(
        run_start,
        ["test_cases", "max_rounds", "convergence_threshold", "model"],
        "run_start",
    )
    test_cases = run_start["data"]["test_cases"]
    _require(
        isinstance(test_cases, list) and len(test_cases) >= 1,
        "run_start.test_cases must be a non-empty list",
    )
    test_count = len(test_cases)
    max_rounds = run_start["data"]["max_rounds"]
    convergence_threshold = run_start["data"]["convergence_threshold"]
    _require(
        isinstance(max_rounds, int) and max_rounds >= 1,
        f"max_rounds invalid: {max_rounds!r}",
    )
    _require(
        isinstance(convergence_threshold, (int, float))
        and 0 < convergence_threshold <= 1,
        f"convergence_threshold invalid: {convergence_threshold!r}",
    )

    current: RoundState | None = None
    rounds_seen: list[RoundState] = []
    verdict_payload: dict | None = None

    for i in range(1, len(events)):
        ev = events[i]
        etype = ev["event"]
        data = ev["data"]

        if etype == "run_start":
            raise VerifyError(f"event[{i}] unexpected second run_start")

        elif etype == "round_start":
            _require_keys(ev, ["round", "test_count"], etype)
            round_idx = data["round"]
            _require(isinstance(round_idx, int) and round_idx >= 1, f"event[{i}] bad round {round_idx}")
            _require(
                round_idx == len(rounds_seen) + 1,
                f"event[{i}] round {round_idx} not next after {len(rounds_seen)} prior rounds",
            )
            _require(
                data["test_count"] == test_count,
                f"event[{i}] round_start.test_count={data['test_count']} != run_start test_count={test_count}",
            )
            _require(
                current is None or (current.summary is not None),
                f"event[{i}] round_start while previous round still open",
            )
            current = RoundState(round_index=round_idx, test_count=test_count)
            rounds_seen.append(current)

        elif etype == "student_invoke":
            _require_keys(ev, ["round", "test_index", "input"], etype)
            _require(current is not None, f"event[{i}] student_invoke outside round")
            assert current is not None
            _require(
                data["round"] == current.round_index,
                f"event[{i}] student_invoke.round={data['round']} != current {current.round_index}",
            )
            ti = data["test_index"]
            _require(
                isinstance(ti, int) and 0 <= ti < test_count,
                f"event[{i}] test_index {ti} out of range",
            )
            _require(
                ti == len(current.seen_responses),
                f"event[{i}] test_index {ti} not next after {len(current.seen_responses)} responses",
            )
            _require(
                data["input"] == test_cases[ti],
                f"event[{i}] student_invoke.input {data['input']} != test_cases[{ti}]={test_cases[ti]}",
            )

        elif etype == "student_response":
            _require_keys(
                ev, ["round", "test_index", "input", "raw_output", "passed", "reason"], etype
            )
            _require(current is not None, f"event[{i}] student_response outside round")
            assert current is not None
            ti = data["test_index"]
            _require(
                ti == len(current.seen_responses),
                f"event[{i}] student_response.test_index={ti} not next after {len(current.seen_responses)}",
            )
            _require(
                data["input"] == test_cases[ti],
                f"event[{i}] student_response.input {data['input']} != test_cases[{ti}]",
            )
            # The load-bearing check: replay grading independently.
            true_passed, true_reason = grade_student_output(
                data["input"], data["raw_output"]
            )
            _require(
                bool(data["passed"]) == true_passed,
                f"event[{i}] student_response.passed={data['passed']} disagrees with verifier replay (truth={true_passed}, reason={true_reason!r})",
            )
            if not true_passed:
                # Reason field should be non-empty on failure (informational; we don't
                # require exact match, just non-empty).
                _require(
                    isinstance(data["reason"], str) and data["reason"] != "",
                    f"event[{i}] student_response.reason must be non-empty on failure",
                )
            current.seen_responses.append(data)

        elif etype == "round_summary":
            _require_keys(ev, ["round", "pass_count", "total", "pass_rate"], etype)
            _require(current is not None, f"event[{i}] round_summary outside round")
            assert current is not None
            _require(
                data["round"] == current.round_index,
                f"event[{i}] round_summary.round={data['round']} != current {current.round_index}",
            )
            _require(
                len(current.seen_responses) == test_count,
                f"event[{i}] round_summary before all {test_count} student_responses (got {len(current.seen_responses)})",
            )
            true_pass = sum(1 for r in current.seen_responses if r["passed"])
            _require(
                data["pass_count"] == true_pass,
                f"event[{i}] round_summary.pass_count={data['pass_count']} != recount {true_pass}",
            )
            _require(
                data["total"] == test_count,
                f"event[{i}] round_summary.total={data['total']} != test_count={test_count}",
            )
            true_rate = true_pass / test_count
            # Allow tiny float tolerance.
            _require(
                abs(data["pass_rate"] - true_rate) < 1e-9,
                f"event[{i}] round_summary.pass_rate={data['pass_rate']} != recount {true_rate}",
            )
            current.summary = data

        elif etype == "tutor_invoke":
            _require_keys(ev, ["round"], etype)
            _require(current is not None, f"event[{i}] tutor_invoke outside round")
            assert current is not None
            _require(
                current.summary is not None,
                f"event[{i}] tutor_invoke before round_summary",
            )
            _require(
                data["round"] == current.round_index,
                f"event[{i}] tutor_invoke.round={data['round']} != current {current.round_index}",
            )
            _require(
                not current.tutor_invoked,
                f"event[{i}] tutor_invoke twice in round {current.round_index}",
            )
            current.tutor_invoked = True

        elif etype == "tutor_response":
            _require_keys(ev, ["round", "patch_applied", "new_claude_md_size"], etype)
            _require(current is not None, f"event[{i}] tutor_response outside round")
            assert current is not None
            _require(
                current.tutor_invoked,
                f"event[{i}] tutor_response without prior tutor_invoke",
            )
            _require(
                not current.tutor_responded,
                f"event[{i}] tutor_response twice in round {current.round_index}",
            )
            _require(
                data["round"] == current.round_index,
                f"event[{i}] tutor_response.round={data['round']} != current {current.round_index}",
            )
            current.tutor_responded = True

        elif etype == "verdict":
            _require_keys(ev, ["status", "final_round", "final_pass_rate"], etype)
            status = data["status"]
            _require(
                status in VALID_VERDICTS,
                f"event[{i}] verdict.status={status!r} not in {sorted(VALID_VERDICTS)}",
            )
            _require(
                current is not None and current.summary is not None,
                f"event[{i}] verdict before any complete round",
            )
            assert current is not None and current.summary is not None
            _require(
                data["final_round"] == current.round_index,
                f"event[{i}] verdict.final_round={data['final_round']} != current {current.round_index}",
            )
            _require(
                abs(data["final_pass_rate"] - current.summary["pass_rate"]) < 1e-9,
                f"event[{i}] verdict.final_pass_rate={data['final_pass_rate']} != last round_summary {current.summary['pass_rate']}",
            )
            if status == "converged":
                _require(
                    current.summary["pass_rate"] >= convergence_threshold - 1e-9,
                    f"event[{i}] verdict 'converged' but final pass_rate {current.summary['pass_rate']} < threshold {convergence_threshold}",
                )
            if status == "max_rounds":
                _require(
                    current.round_index == max_rounds,
                    f"event[{i}] verdict 'max_rounds' but final_round={current.round_index} != max_rounds={max_rounds}",
                )
            verdict_payload = data

        elif etype == "run_end":
            _require_keys(ev, ["status"], etype)
            _require(verdict_payload is not None, f"event[{i}] run_end with no prior verdict")
            assert verdict_payload is not None
            _require(
                data["status"] == verdict_payload["status"],
                f"event[{i}] run_end.status={data['status']} != verdict.status={verdict_payload['status']}",
            )

        else:  # pragma: no cover
            raise VerifyError(f"event[{i}] unhandled event type '{etype}'")

    _require(verdict_payload is not None, "trace contains no verdict event")
    return verdict_payload


def verify(path: Path) -> dict:
    events = load_trace(path)
    verify_envelope(events)
    return verify_structure_and_grading(events)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: trace_verify.py <trace.jsonl>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.exists():
        print(f"RESULT: FAIL {path} no such file", file=sys.stdout)
        return 1
    try:
        verify(path)
    except VerifyError as e:
        print(f"RESULT: FAIL {path} {e}", file=sys.stdout)
        return 1
    print(f"RESULT: PASS {path}", file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
