#!/usr/bin/env python3
"""Self-test for orchestrator's trace_verify.py.

A verifier is not done until it has caught a planted regression. This
script builds a faithful trace of a 3-round tutor-student loop that
converges in round 3, confirms trace_verify.py accepts it, then
plants regressions and confirms each one is rejected with a specific
reason.

Run:  pixi run selftest-orchestrator
Exit: 0 on success, nonzero if any check fails.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).parent
VERIFY = HERE / "trace_verify.py"


def true_check(n: int, s: int) -> str:
    return hashlib.sha256(f"{n}-{s}".encode("utf-8")).hexdigest()[:8]


def faithful_trace() -> list[dict]:
    """Build a deterministic 3-round trace that converges on round 3.

    Test cases: [12, 99, 1234]. True digit sums: [3, 18, 10].

    Round 1: student outputs prose. 0/3 pass.
    Round 2: student outputs JSON with wrong field name 'sum' instead of 'digit_sum'.
             0/3 pass.
    Round 3: student outputs correct JSON. 3/3 pass. Converged.
    """
    test_cases = [12, 99, 1234]
    true_sums = [3, 18, 10]
    test_count = len(test_cases)
    convergence_threshold = 1.0
    max_rounds = 5

    events: list[dict] = []
    step = 0
    ts = 1_700_000_000.0

    def emit(event: str, data: dict) -> None:
        nonlocal step, ts
        events.append(
            {"run_id": "selftest-run", "step": step, "ts": ts, "event": event, "data": data}
        )
        step += 1
        ts += 0.001

    emit(
        "run_start",
        {
            "test_cases": test_cases,
            "max_rounds": max_rounds,
            "convergence_threshold": convergence_threshold,
            "model": "claude-haiku-4-5-20251001",
        },
    )

    # ── Round 1: prose responses, 0/3 pass ─────────────────────────────
    emit("round_start", {"round": 1, "test_count": test_count})
    for ti, n in enumerate(test_cases):
        emit("student_invoke", {"round": 1, "test_index": ti, "input": n})
        prose = f"The digit sum of {n} is {true_sums[ti]}."
        emit(
            "student_response",
            {
                "round": 1,
                "test_index": ti,
                "input": n,
                "raw_output": prose,
                "passed": False,
                "reason": "does not match required format",
            },
        )
    emit("round_summary", {"round": 1, "pass_count": 0, "total": test_count, "pass_rate": 0.0})
    emit("tutor_invoke", {"round": 1})
    emit("tutor_response", {"round": 1, "patch_applied": True, "new_claude_md_size": 250})

    # ── Round 2: right format, fabricated CHECK ────────────────────────
    emit("round_start", {"round": 2, "test_count": test_count})
    for ti, n in enumerate(test_cases):
        emit("student_invoke", {"round": 2, "test_index": ti, "input": n})
        # Right format but wrong (made-up) CHECK — the student hasn't been
        # told to use Python's hashlib yet.
        bad_format = f"DIGIT_SUM={true_sums[ti]};CHECK=deadbeef"
        emit(
            "student_response",
            {
                "round": 2,
                "test_index": ti,
                "input": n,
                "raw_output": bad_format,
                "passed": False,
                "reason": "CHECK wrong",
            },
        )
    emit("round_summary", {"round": 2, "pass_count": 0, "total": test_count, "pass_rate": 0.0})
    emit("tutor_invoke", {"round": 2})
    emit("tutor_response", {"round": 2, "patch_applied": True, "new_claude_md_size": 410})

    # ── Round 3: correct format AND correct CHECK, all pass ────────────
    emit("round_start", {"round": 3, "test_count": test_count})
    for ti, n in enumerate(test_cases):
        emit("student_invoke", {"round": 3, "test_index": ti, "input": n})
        s = true_sums[ti]
        check = true_check(n, s)
        good = f"DIGIT_SUM={s};CHECK={check}"
        emit(
            "student_response",
            {
                "round": 3,
                "test_index": ti,
                "input": n,
                "raw_output": good,
                "passed": True,
                "reason": "",
            },
        )
    emit("round_summary", {"round": 3, "pass_count": 3, "total": test_count, "pass_rate": 1.0})

    emit(
        "verdict",
        {"status": "converged", "final_round": 3, "final_pass_rate": 1.0},
    )
    emit("run_end", {"status": "converged"})

    return events


def write_trace(events: list[dict]) -> Path:
    fd, path = tempfile.mkstemp(suffix=".jsonl", prefix="tutor-selftest-")
    with open(fd, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return Path(path)


def run_verify(events: list[dict]) -> tuple[int, str]:
    path = write_trace(events)
    try:
        proc = subprocess.run(
            [sys.executable, str(VERIFY), str(path)],
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout.strip()
    finally:
        path.unlink(missing_ok=True)


def expect_pass(name: str, events: list[dict]) -> bool:
    rc, out = run_verify(events)
    if rc == 0 and out.startswith("RESULT: PASS"):
        print(f"  ✓ {name} accepted")
        return True
    print(f"  ✗ {name} expected PASS, got rc={rc} out={out}")
    return False


def expect_fail(name: str, events: list[dict], reason_substring: str) -> bool:
    rc, out = run_verify(events)
    if rc != 0 and reason_substring in out:
        print(f"  ✓ {name} rejected ({reason_substring!r} found)")
        return True
    print(f"  ✗ {name} expected FAIL containing {reason_substring!r}, got rc={rc} out={out}")
    return False


def main() -> int:
    base = faithful_trace()

    print("baseline acceptance:")
    ok = True
    ok &= expect_pass("3-round converged trace", base)

    print("planted regressions:")

    # 1. Faked grading: claim a prose response passed.
    bad = copy.deepcopy(base)
    for ev in bad:
        if ev["event"] == "student_response" and ev["data"]["round"] == 1:
            ev["data"]["passed"] = True
            ev["data"]["reason"] = ""
            break
    ok &= expect_fail("faked passing grade on prose", bad, "disagrees with verifier replay")

    # 2. Step counter gap.
    bad = copy.deepcopy(base)
    bad[3]["step"] = 99
    ok &= expect_fail("step counter gap", bad, "step field is 99")

    # 3. Round_summary pass_count miscounted.
    bad = copy.deepcopy(base)
    for ev in bad:
        if ev["event"] == "round_summary" and ev["data"]["round"] == 3:
            ev["data"]["pass_count"] = 2  # truth is 3
            ev["data"]["pass_rate"] = 2 / 3
            break
    ok &= expect_fail("miscounted round_summary", bad, "round_summary.pass_count")

    # 4. Verdict 'converged' but pass rate is below threshold.
    bad = copy.deepcopy(base)
    # Make round 3 fail one test, then claim 'converged'
    for ev in bad:
        if (
            ev["event"] == "student_response"
            and ev["data"]["round"] == 3
            and ev["data"]["test_index"] == 0
        ):
            # Replace the good JSON with something that won't grade as passing.
            ev["data"]["raw_output"] = "wrong"
            ev["data"]["passed"] = False
            ev["data"]["reason"] = "not valid JSON"
            break
    # Update round_summary to be honest about the actual count (2/3 = 0.6667).
    for ev in bad:
        if ev["event"] == "round_summary" and ev["data"]["round"] == 3:
            ev["data"]["pass_count"] = 2
            ev["data"]["pass_rate"] = 2 / 3
            break
    # And the verdict's final_pass_rate to match the new round_summary, but
    # *keep* the lying 'converged' status with threshold 1.0 — the verifier
    # should reject this because rate < threshold.
    for ev in bad:
        if ev["event"] == "verdict":
            ev["data"]["final_pass_rate"] = 2 / 3
            break
    for ev in bad:
        if ev["event"] == "run_end":
            ev["data"]["status"] = "converged"
            break
    ok &= expect_fail("false 'converged' below threshold", bad, "< threshold")

    # 5. Unknown event type.
    bad = copy.deepcopy(base)
    bad[3]["event"] = "totally_made_up"
    ok &= expect_fail("unknown event type", bad, "unknown event type")

    # 6. Test_index out of range.
    bad = copy.deepcopy(base)
    # Corrupt the first round's first student invocation.
    for ev in bad:
        if ev["event"] == "student_invoke" and ev["data"]["round"] == 1:
            ev["data"]["test_index"] = 5  # garbage
            break
    ok &= expect_fail("bad test_index", bad, "out of range")

    # 7. Trace truncated (no run_end).
    bad = copy.deepcopy(base)[:-1]
    ok &= expect_fail("missing run_end", bad, "expected 'run_end'")

    if ok:
        print("\nAll selftest checks passed.")
        return 0
    print("\nSelftest FAILED.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
