#!/usr/bin/env python3
"""Aggregator for amplify trace verification.

Runs trace_verify.py over every event-trace in traces/, groups results by
rung, and prints a per-rung pass-rate table. The per-rung pass rate is
the empirical measurement of `p` for that rung — that is the entire
output of Pass 1 (calibration).

Usage:
  pixi run aggregate-amplify [<traces_dir> [<verifier_path>]]

Defaults:
  traces_dir     = <repo>/traces
  verifier_path  = <repo>/verify/trace_verify.py

Trace filename format (set by runner/run.sh):
  rung<N>-<label>-p<problem_index>-t<trial_zero_padded>.jsonl
  e.g. rung3-4x4-p0-t001.jsonl

Conversation logs (*.conversation.jsonl) and stderr files (*.txt) are
skipped — only event traces are aggregated.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


# Greedy label group: anything between the rung index and the `-p<N>-t<N>$`
# tail. Tolerates labels containing dashes (e.g. `4x5-hard`). The trailing
# `-p\d+-t\d+$` anchor forces the greedy match to leave them in place.
TRACE_NAME_RE = re.compile(r"^rung(\d+)-(.+)-p(\d+)-t(\d+)$")


def main(argv: list[str]) -> int:
    if len(argv) > 3:
        print("usage: aggregate.py [<traces_dir> [<verifier_path>]]", file=sys.stderr)
        return 2

    here = Path(__file__).parent
    traces_dir = Path(argv[1]) if len(argv) >= 2 else here.parent / "traces"
    verifier = Path(argv[2]) if len(argv) >= 3 else here / "trace_verify.py"

    if not traces_dir.is_dir():
        print(f"No traces/ directory at {traces_dir}. Run ./runner/run.sh first.", file=sys.stderr)
        return 1

    # Filter out conversation files and other non-event files.
    traces = sorted(
        p for p in traces_dir.glob("*.jsonl") if not p.name.endswith(".conversation.jsonl")
    )

    if not traces:
        print(f"No traces found in {traces_dir}. Run ./runner/run.sh first.", file=sys.stderr)
        return 1

    pass_count = 0
    fail_count = 0
    failures: list[str] = []
    rung_pass: dict[int, int] = defaultdict(int)
    rung_fail: dict[int, int] = defaultdict(int)
    rung_label: dict[int, str] = {}

    for t in traces:
        base = t.stem  # filename without .jsonl
        m = TRACE_NAME_RE.match(base)
        if not m:
            # Trace not following the rung naming convention; count it
            # as ungrouped but still verify.
            rung_index = -1
            label = "?"
        else:
            rung_index = int(m.group(1))
            label = m.group(2)
            rung_label[rung_index] = label

        proc = subprocess.run(
            [sys.executable, str(verifier), str(t)],
            capture_output=True,
            text=True,
        )
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        verdict_word = out.split(" ", 2)[1] if out.startswith("RESULT: ") else "UNKNOWN"

        if verdict_word == "PASS":
            pass_count += 1
            rung_pass[rung_index] += 1
            print(f"✓  {base}")
        else:
            fail_count += 1
            rung_fail[rung_index] += 1
            # Surface stderr too — if the verifier crashed (rather than printing
            # a structured RESULT: FAIL line) the only diagnostic is on stderr.
            detail = out if out else f"unknown: rc={proc.returncode}"
            if err:
                detail = f"{detail}\n     stderr: {err}"
            failures.append(detail)
            print(f"✗  {base}")
            if err and verdict_word == "UNKNOWN":
                print(f"     stderr: {err}")

    total = pass_count + fail_count

    # Per-rung table.
    print()
    print("Per-rung pass rate (= empirical p for that rung):")
    print()
    print(f"  {'rung':<6} {'label':<8} {'pass':>8} {'total':>8} {'p':>10}")
    print(f"  {'----':<6} {'-----':<8} {'----':>8} {'-----':>8} {'-':>10}")

    all_rungs = sorted(set(rung_pass) | set(rung_fail))
    for r in all_rungs:
        rp = rung_pass.get(r, 0)
        rf = rung_fail.get(r, 0)
        rt = rp + rf
        p_str = f"{rp / rt:.3f}" if rt > 0 else "n/a"
        label = rung_label.get(r, "?") if r >= 0 else "(ungrp)"
        print(f"  {r:<6} {label:<8} {rp:>8} {rt:>8} {p_str:>10}")

    print()
    print(f"Summary: {pass_count}/{total} passed, {fail_count} failed")
    print()
    print("Calibration interpretation:")
    print("  - rungs with p ≈ 1.000 are too easy; nothing for amplification to amplify.")
    print("  - rungs with p ≈ 0.000 are too hard; amplification can't help below p=0.5.")
    print("  - the smallest rung whose p falls in the [0.55, 0.85] target window is the")
    print("    Pass 2 input set. Use that rung's problems for the two-arm experiment.")

    if fail_count > 0 and pass_count == 0:
        print()
        print("Failures:")
        for f in failures:
            print(f"  {f}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
