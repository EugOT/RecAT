#!/usr/bin/env python3
"""Trace verifier for amplify.

Reads a JSONL trace written by the amplify .claude/ program and checks
that it is a faithful execution of "compute a × b by hand and report the
answer." The verifier is the proof that .claude/ is a program: a trace
that passes every check is one Bernoulli draw of evidence about the
model's per-attempt success rate `p`.

What gets checked:

  1. Envelope: valid JSON, step counts start at 0 and increment by 1
     with no gaps, first event is run_start, last is run_end, single
     run_id throughout.
  2. Event schema: every event type is legal and has its required
     fields. The amplify schema has exactly three events per run:
     run_start, final_answer, run_end — in that order, no others.
  3. Internal consistency: the (a, b) in final_answer match the (a, b)
     in run_start, and the answer in run_end matches the answer in
     final_answer.
  4. Headline check (the Bernoulli draw): final_answer.answer must equal
     a × b, where a × b is computed independently by Python in this
     verifier. PASS means the model got the multiplication right; FAIL
     means it got it wrong. This is the entire experimental measurement.

Usage:
  pixi run python verify/trace_verify.py <trace.jsonl>

Exits 0 on success, 1 on failure. Prints one summary line:

  RESULT: PASS <path>                            (on success)
  RESULT: FAIL <path> <reason>                   (on failure)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


LEGAL_EVENTS = {"run_start", "final_answer", "run_end"}


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

    # The amplify program has a fixed three-event shape.
    _require(
        len(events) == 3,
        f"expected exactly 3 events (run_start, final_answer, run_end), got {len(events)}",
    )
    _require(
        events[1]["event"] == "final_answer",
        f"middle event is '{events[1]['event']}', expected 'final_answer'",
    )


def verify_structure_and_arithmetic(events: list[dict]) -> tuple[int, int, int]:
    """Check schema, internal consistency, and the headline arithmetic check.

    Returns (a, b, reported_answer) on success.
    """
    run_start, final_answer, run_end = events

    # run_start
    _require_keys(run_start, ["a", "b", "encoding"], "run_start")
    a = run_start["data"]["a"]
    b = run_start["data"]["b"]
    _require(
        isinstance(a, int) and a > 0,
        f"run_start.data.a is not a positive int: {a!r}",
    )
    _require(
        isinstance(b, int) and b > 0,
        f"run_start.data.b is not a positive int: {b!r}",
    )

    # final_answer
    _require_keys(final_answer, ["a", "b", "answer"], "final_answer")
    fa_a = final_answer["data"]["a"]
    fa_b = final_answer["data"]["b"]
    reported = final_answer["data"]["answer"]
    _require(
        fa_a == a,
        f"final_answer.a={fa_a} disagrees with run_start.a={a}",
    )
    _require(
        fa_b == b,
        f"final_answer.b={fa_b} disagrees with run_start.b={b}",
    )
    _require(
        isinstance(reported, int),
        f"final_answer.answer is not an int: {reported!r}",
    )

    # run_end
    _require_keys(run_end, ["answer"], "run_end")
    re_answer = run_end["data"]["answer"]
    _require(
        re_answer == reported,
        f"run_end.answer={re_answer} disagrees with final_answer.answer={reported}",
    )

    # The headline check: did the model get the multiplication right?
    # This is the Bernoulli draw — PASS means yes, FAIL means no.
    truth = a * b
    _require(
        reported == truth,
        f"answer mismatch: model reported {reported}, truth is {a}*{b}={truth}",
    )

    return a, b, reported


def verify(path: Path) -> None:
    events = load_trace(path)
    verify_envelope(events)
    verify_structure_and_arithmetic(events)


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
