#!/usr/bin/env python3
"""Orchestrator-pattern tutor/student loop for orchestrator.

The tutor is the orchestrator and the student is the worker. The tutor's
instructions are held constant across the trial; only the student's
instructions are mutated between rounds, based on the tutor's reading of
the student's failures. See ../README.md and the spec README's
Orchestrator section for the framing.

The runner shell script handles cache mode, argument parsing, and the
top-level for-trial loop. This script is invoked once per trial and
implements the actual loop:

  1. Read the initial student instructions from templates/student-initial.md
     and the tutor instructions from templates/tutor.md. These are PLAIN
     markdown files — there is no .claude/ directory anywhere inside
     applications/orchestrator/, so neither agent's instructions are
     discoverable as Claude Code configurations by any out-of-band claude
     invocation. The files are loaded into Python strings; the application
     source tree contains no on-disk .claude/ for either agent.
  2. For each round 1..max_rounds:
       a. For each test case, spawn ONE student `claude` invocation.
          Each invocation runs in its OWN fresh /tmp directory which
          the runner creates on the fly. The runner writes the current
          patched student instructions into /tmp/.../.claude/CLAUDE.md
          and a Bash-only settings.json into /tmp/.../.claude/settings.json,
          then runs `claude --print` with /tmp/.../ as cwd. The temp
          directory is deleted immediately after the invocation completes.
          The student has no filesystem access to anything in
          applications/orchestrator/.
       b. Grade each response via verify.trace_verify.grade_student_output
          (stdlib only, runs in this process — not exposed to the student).
       c. Emit events.
       d. If pass_rate >= threshold: emit verdict 'converged' and stop.
       e. Else if round == max_rounds: emit verdict 'max_rounds' and stop.
       f. Else: spawn the tutor `claude` in its OWN fresh /tmp directory
          following the same pattern. The tutor's instructions are written
          into /tmp/.../.claude/CLAUDE.md from the in-memory copy of
          templates/tutor.md. Pass the failure log + current student md as
          part of the prompt. Extract the PATCH_BEGIN/PATCH_END content
          from the tutor's stdout and update current_student_md in memory.
  3. Emit run_end and finish.
  4. Write the FINAL patched student md to a forensic file alongside the
     trace (write-only output, the student never sees it).

Isolation invariant: no subprocess in this trial ever has its cwd inside
applications/orchestrator/, and no .claude/ directory exists inside the
application source tree. Both the student and the tutor run in /tmp
directories that are created and destroyed per invocation.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def add_verify_path(root: Path) -> None:
    sys.path.insert(0, str(root / "verify"))


class TraceWriter:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.step = 0
        path.write_text("")  # truncate

    def emit(self, event: str, data: dict[str, Any]) -> None:
        ev = {
            "run_id": self.run_id,
            "step": self.step,
            "ts": time.time(),
            "event": event,
            "data": data,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(ev) + "\n")
        self.step += 1


# settings.json content for the student. Bash-only permissions.
# The student CAN call python3 via Bash to compute SHA-256, but cannot
# directly Read/Glob/Grep filesystem paths. Combined with the /tmp
# isolation, this means the student has no useful filesystem visibility:
# the only readable parents (/tmp, /) contain no experiment artifacts.
STUDENT_SETTINGS_JSON = '{"permissions": {"allow": ["Bash"]}}\n'

# settings.json content for the tutor. Allows Read so the tutor can
# inspect its own .claude/ if needed (it doesn't need to, since the
# instructions are loaded into context automatically, but allowing it
# is harmless because the tutor's /tmp directory contains only its own
# .claude/ and nothing else).
TUTOR_SETTINGS_JSON = '{"permissions": {"allow": ["Read", "Bash"]}}\n'


def extract_final_assistant_text(stream_json_stdout: str) -> str:
    """Walk a claude --output-format stream-json log and return the
    final assistant message's text content concatenated.

    The stream-json format produces one JSON object per line. Object
    types include `system`, `user`, `assistant`, and `result`. The
    assistant messages contain a `content` array of blocks; we want
    the LAST assistant message's text blocks. If there are no
    assistant messages, returns an empty string.
    """
    last_text = ""
    for line in stream_json_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            msg = ev.get("message", {})
            content = msg.get("content", [])
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            if text_parts:
                last_text = "".join(text_parts)
    return last_text


def invoke_claude_isolated(
    agent_claude_md: str,
    agent_settings_json: str,
    prompt: str,
    model: str,
    traces_dir: Path,
    trial_id: str,
    label: str,
    max_turns: int = 6,
    allowed_tools: str = "",
) -> str:
    """Invoke `claude --print` in a fresh /tmp directory.

    The conversation-log convention: stream-json stdout goes to
    `<traces_dir>/<trial_id>.<label>.conversation.jsonl` and stderr goes
    to `<traces_dir>/<trial_id>.<label>.agent-stderr.txt`. Each
    invocation produces its own pair of files; there are no hand-rolled
    separator lines and no shared conversation log.

    The /tmp directory contains only .claude/CLAUDE.md and
    .claude/settings.json with the supplied content. The directory is
    deleted when this function returns.

    Returns the final assistant text content extracted from the
    stream-json stdout (used by the runner for grading).
    """
    conv_file = traces_dir / f"{trial_id}.{label}.conversation.jsonl"
    stderr_file = traces_dir / f"{trial_id}.{label}.agent-stderr.txt"

    with tempfile.TemporaryDirectory(prefix=f"orchestrator-{label}-") as tmp:
        tmp_path = Path(tmp)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text(agent_claude_md)
        (claude_dir / "settings.json").write_text(agent_settings_json)

        cmd = [
            "claude",
            "--print",
            prompt,
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(max_turns),
            "--tools",
            allowed_tools,
            "--allowedTools",
            allowed_tools,
            "--strict-mcp-config",
            "--setting-sources",
            "project",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]
        try:
            with conv_file.open("w") as conv_out, stderr_file.open("w") as err_out:
                proc = subprocess.run(
                    cmd,
                    cwd=str(tmp_path),
                    stdout=conv_out,
                    stderr=err_out,
                    text=True,
                    check=False,
                )
        except FileNotFoundError:
            print("ERROR: 'claude' CLI not found in PATH.", file=sys.stderr)
            sys.exit(2)

        if proc.returncode != 0:
            print(
                f"WARNING: claude exited {proc.returncode} for {trial_id}.{label}; "
                f"see {stderr_file}",
                file=sys.stderr,
            )

        # Read back the conversation log to extract the final assistant text.
        stream_json_text = conv_file.read_text()
        return extract_final_assistant_text(stream_json_text)


def extract_patch(raw: str) -> str | None:
    """Extract the content between PATCH_BEGIN and PATCH_END markers.

    Returns None if either marker is missing or the order is wrong.
    """
    begin = raw.find("PATCH_BEGIN")
    end = raw.find("PATCH_END")
    if begin == -1 or end == -1 or end <= begin:
        return None
    content = raw[begin + len("PATCH_BEGIN") : end]
    if content.startswith("\n"):
        content = content[1:]
    if content.endswith("\n"):
        content = content[:-1]
    return content


def build_tutor_prompt(
    round_num: int,
    current_student_md: str,
    responses: list[dict],
) -> str:
    failed = [r for r in responses if not r["passed"]]
    passed = [r for r in responses if r["passed"]]
    parts = []
    parts.append(f"Round {round_num} of the tutor-student loop has just completed.")
    parts.append("")
    parts.append("CURRENT STUDENT CLAUDE.md:")
    parts.append("---")
    parts.append(current_student_md)
    parts.append("---")
    parts.append("")
    parts.append(f"FAILED TEST CASES ({len(failed)}):")
    for r in failed:
        parts.append(
            f"  input={r['input']}, raw_output={r['raw_output']!r}, reason={r['reason']!r}"
        )
    parts.append("")
    parts.append(f"PASSING TEST CASES ({len(passed)}):")
    for r in passed:
        parts.append(f"  input={r['input']}, raw_output={r['raw_output']!r}")
    parts.append("")
    parts.append(
        "Output a new full student CLAUDE.md between PATCH_BEGIN and PATCH_END "
        "markers as instructed in your tutor instructions. The text between the "
        "markers will be written verbatim into a /tmp directory and used as the "
        "student's instructions for the next round."
    )
    return "\n".join(parts)


def run_trial(
    root: Path,
    trial_id: str,
    trace_file: Path,
    traces_dir: Path,
    test_cases: list[int],
    max_rounds: int,
    threshold: float,
    model: str,
) -> str:
    """Run one trial. Returns the final verdict status."""
    add_verify_path(root)
    from trace_verify import grade_student_output  # type: ignore

    test_count = len(test_cases)

    # Initial student instructions: read from templates/student-initial.md
    # (a plain markdown file, NOT a .claude/ directory — the application
    # source tree contains no .claude/ directories at all, so neither
    # agent's instructions are discoverable by any out-of-band claude
    # invocation that walks parent directories looking for CLAUDE.md).
    # The string is held in this Python process for the rest of the
    # trial. Each student invocation writes its OWN copy into a fresh
    # /tmp/.../.claude/CLAUDE.md before launching claude.
    initial_student_md = (root / "templates" / "student-initial.md").read_text()
    current_student_md = initial_student_md

    # Tutor instructions: read from templates/tutor.md (same convention:
    # a plain file, not a .claude/ directory). Same isolation policy:
    # written to a /tmp/.../.claude/CLAUDE.md per invocation.
    tutor_md = (root / "templates" / "tutor.md").read_text()

    writer = TraceWriter(trace_file, trial_id)
    writer.emit(
        "run_start",
        {
            "test_cases": test_cases,
            "max_rounds": max_rounds,
            "convergence_threshold": threshold,
            "model": model,
        },
    )

    final_status = "aborted"

    for round_num in range(1, max_rounds + 1):
        print(f"  round {round_num}", file=sys.stderr)
        writer.emit("round_start", {"round": round_num, "test_count": test_count})

        round_responses: list[dict] = []
        pass_count = 0

        for test_index, n in enumerate(test_cases):
            writer.emit(
                "student_invoke",
                {"round": round_num, "test_index": test_index, "input": n},
            )
            prompt = (
                f"Compute the digit sum of {n} and the verification CHECK, "
                f"and respond per your instructions."
            )
            raw_output = invoke_claude_isolated(
                agent_claude_md=current_student_md,
                agent_settings_json=STUDENT_SETTINGS_JSON,
                prompt=prompt,
                model=model,
                traces_dir=traces_dir,
                trial_id=trial_id,
                label=f"student-r{round_num}-t{test_index}",
                allowed_tools="Bash",
            )
            passed, reason = grade_student_output(n, raw_output)
            writer.emit(
                "student_response",
                {
                    "round": round_num,
                    "test_index": test_index,
                    "input": n,
                    "raw_output": raw_output,
                    "passed": bool(passed),
                    "reason": reason,
                },
            )
            round_responses.append(
                {
                    "test_index": test_index,
                    "input": n,
                    "raw_output": raw_output,
                    "passed": bool(passed),
                    "reason": reason,
                }
            )
            if passed:
                pass_count += 1

        pass_rate = pass_count / test_count
        writer.emit(
            "round_summary",
            {
                "round": round_num,
                "pass_count": pass_count,
                "total": test_count,
                "pass_rate": pass_rate,
            },
        )
        print(
            f"    pass_count={pass_count}/{test_count} pass_rate={pass_rate:.4f}",
            file=sys.stderr,
        )
        # Convergence?
        if pass_rate >= threshold - 1e-9:
            writer.emit(
                "verdict",
                {
                    "status": "converged",
                    "final_round": round_num,
                    "final_pass_rate": pass_rate,
                },
            )
            writer.emit("run_end", {"status": "converged"})
            final_status = "converged"
            break

        # Max rounds?
        if round_num == max_rounds:
            writer.emit(
                "verdict",
                {
                    "status": "max_rounds",
                    "final_round": round_num,
                    "final_pass_rate": pass_rate,
                },
            )
            writer.emit("run_end", {"status": "max_rounds"})
            final_status = "max_rounds"
            break

        # Otherwise: invoke the tutor
        writer.emit("tutor_invoke", {"round": round_num})

        tutor_prompt = build_tutor_prompt(round_num, current_student_md, round_responses)
        tutor_raw = invoke_claude_isolated(
            agent_claude_md=tutor_md,
            agent_settings_json=TUTOR_SETTINGS_JSON,
            prompt=tutor_prompt,
            model=model,
            traces_dir=traces_dir,
            trial_id=trial_id,
            label=f"tutor-r{round_num}",
            max_turns=6,
            allowed_tools="Read,Bash",
        )

        new_md = extract_patch(tutor_raw)
        if new_md is None or new_md.strip() == "":
            print(
                "    tutor produced no extractable patch; aborting trial",
                file=sys.stderr,
            )
            writer.emit(
                "tutor_response",
                {
                    "round": round_num,
                    "patch_applied": False,
                    "new_claude_md_size": 0,
                },
            )
            writer.emit(
                "verdict",
                {
                    "status": "aborted",
                    "final_round": round_num,
                    "final_pass_rate": pass_rate,
                },
            )
            writer.emit("run_end", {"status": "aborted"})
            final_status = "aborted"
            break

        # Update the in-memory student program. The next round's student
        # invocations will write this content into their /tmp directories.
        current_student_md = new_md if new_md.endswith("\n") else new_md + "\n"
        new_size = len(current_student_md.encode("utf-8"))
        writer.emit(
            "tutor_response",
            {
                "round": round_num,
                "patch_applied": True,
                "new_claude_md_size": new_size,
            },
        )

    # Forensic dump: write the final patched student md alongside the trace
    # so a human can inspect what the tutor produced. This is a write-only
    # output; nothing reads it during the trial.
    forensic_path = trace_file.with_suffix(".final-student.md")
    forensic_path.write_text(current_student_md)

    return final_status


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True)
    p.add_argument("--trial-id", required=True)
    p.add_argument("--trace-file", required=True)
    p.add_argument("--traces-dir", required=True)
    p.add_argument("--inputs", required=True)
    p.add_argument("--model", required=True)
    args = p.parse_args(argv[1:])

    root = Path(args.root).resolve()
    inputs = json.loads(Path(args.inputs).read_text())
    test_cases = inputs["test_cases"]
    max_rounds = inputs["max_rounds"]
    threshold = inputs["convergence_threshold"]

    final = run_trial(
        root=root,
        trial_id=args.trial_id,
        trace_file=Path(args.trace_file),
        traces_dir=Path(args.traces_dir),
        test_cases=test_cases,
        max_rounds=max_rounds,
        threshold=threshold,
        model=args.model,
    )
    print(f"  [{args.trial_id}] {final.upper()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
