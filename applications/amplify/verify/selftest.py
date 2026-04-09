#!/usr/bin/env python3
"""Self-test for amplify's trace_verify.py.

A verifier is not done until it has caught a planted regression. This
script:

  1. Synthesizes a valid amplify trace by computing the answer correctly
     in Python.
  2. Confirms trace_verify.py accepts it.
  3. Mutates the trace in several ways, each corresponding to a specific
     failure mode the verifier is supposed to catch, and confirms that
     trace_verify.py rejects the mutated trace with a specific reason.

Run:  python3 verify/selftest.py
Exit: 0 on success, nonzero if any check fails.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).parent
VERIFY = HERE / "trace_verify.py"


def faithful_trace(run_id: str, a: int, b: int) -> list[dict]:
    """Generate a faithful amplify trace for a × b."""
    answer = a * b
    ts = 1_700_000_000.0
    return [
        {
            "run_id": run_id,
            "step": 0,
            "ts": ts,
            "event": "run_start",
            "data": {"a": a, "b": b, "encoding": "claude-agent"},
        },
        {
            "run_id": run_id,
            "step": 1,
            "ts": ts + 0.001,
            "event": "final_answer",
            "data": {"a": a, "b": b, "answer": answer},
        },
        {
            "run_id": run_id,
            "step": 2,
            "ts": ts + 0.002,
            "event": "run_end",
            "data": {"answer": answer},
        },
    ]


def write_trace(events: list[dict]) -> Path:
    fd, path = tempfile.mkstemp(suffix=".jsonl", prefix="amplify-selftest-")
    with open(fd, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return Path(path)


def run_verify(events: list[dict]) -> tuple[int, str]:
    path = write_trace(events)
    try:
        proc = subprocess.run(
            ["python3", str(VERIFY), str(path)],
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
        print(f"  ✓ {name} rejected ({reason_substring!r} found in output)")
        return True
    print(f"  ✗ {name} expected FAIL containing {reason_substring!r}, got rc={rc} out={out}")
    return False


def main() -> int:
    base_easy = faithful_trace("test-easy", 47, 83)        # answer 3901
    base_hard = faithful_trace("test-hard", 2929, 39393)   # answer 115382097

    print("baseline acceptance:")
    ok = True
    ok &= expect_pass("trivial 47 × 83 trace", base_easy)
    ok &= expect_pass("hard 2929 × 39393 trace", base_hard)

    print("planted regressions:")

    # 1. Wrong answer (off by 1)
    bad = copy.deepcopy(base_easy)
    bad[1]["data"]["answer"] = bad[1]["data"]["answer"] + 1
    bad[2]["data"]["answer"] = bad[2]["data"]["answer"] + 1
    ok &= expect_fail("wrong answer (off by 1)", bad, "answer mismatch")

    # 2. Wrong answer matching a known wild miss (the user's example)
    bad = copy.deepcopy(base_hard)
    bad[1]["data"]["answer"] = 115413897  # observed in the wild; off by 31800
    bad[2]["data"]["answer"] = 115413897
    ok &= expect_fail("known-wild wrong answer", bad, "answer mismatch")

    # 3. Missing run_start
    bad = copy.deepcopy(base_easy)[1:]
    # Renumber
    for i, ev in enumerate(bad):
        ev["step"] = i
    ok &= expect_fail("missing run_start", bad, "expected 'run_start'")

    # 4. Missing run_end
    bad = copy.deepcopy(base_easy)[:-1]
    ok &= expect_fail("missing run_end", bad, "expected 'run_end'")

    # 5. Step counter gap
    bad = copy.deepcopy(base_easy)
    bad[1]["step"] = 99
    ok &= expect_fail("step counter gap", bad, "step field is 99")

    # 6. Unknown event type
    bad = copy.deepcopy(base_easy)
    bad[1]["event"] = "totally_made_up"
    ok &= expect_fail("unknown event type", bad, "unknown event type")

    # 7. final_answer.answer not an integer (string instead)
    bad = copy.deepcopy(base_easy)
    bad[1]["data"]["answer"] = "3901"
    bad[2]["data"]["answer"] = "3901"
    ok &= expect_fail("answer is a string", bad, "not an int")

    # 8. final_answer.a disagrees with run_start.a
    bad = copy.deepcopy(base_easy)
    bad[1]["data"]["a"] = bad[1]["data"]["a"] + 1
    ok &= expect_fail("final_answer.a mismatch", bad, "disagrees with run_start.a")

    # 9. run_end.answer disagrees with final_answer.answer
    bad = copy.deepcopy(base_easy)
    bad[2]["data"]["answer"] = bad[2]["data"]["answer"] + 1
    ok &= expect_fail("run_end.answer mismatch", bad, "disagrees with final_answer.answer")

    # 10. Mixed run_ids
    bad = copy.deepcopy(base_easy)
    bad[1]["run_id"] = "different-run-id"
    ok &= expect_fail("mixed run_ids", bad, "mixed run_ids")

    # 11. More than 3 events (the strict length check)
    bad = copy.deepcopy(base_easy)
    bad.append(
        {
            "run_id": "test-easy",
            "step": 3,
            "ts": 1_700_000_000.003,
            "event": "run_end",
            "data": {"answer": 3901},
        }
    )
    ok &= expect_fail("more than 3 events", bad, "expected exactly 3 events")

    # 12. Wrong middle event (something in the legal set but in the wrong position)
    bad = [
        copy.deepcopy(base_easy[0]),
        copy.deepcopy(base_easy[2]),  # run_end where final_answer should be
        copy.deepcopy(base_easy[2]),
    ]
    bad[1]["step"] = 1
    bad[2]["step"] = 2
    ok &= expect_fail("wrong middle event", bad, "expected 'final_answer'")

    if ok:
        print("\nAll selftest checks passed.")
        return 0
    print("\nSelftest FAILED.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
