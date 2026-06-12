from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
AMPLIFY_RUNNER_DIR = ROOT / "applications" / "amplify" / "runner"
ORCHESTRATOR_RUNNER_DIR = ROOT / "applications" / "orchestrator" / "runner"
sys.path.insert(0, str(AMPLIFY_RUNNER_DIR))
sys.path.insert(0, str(ORCHESTRATOR_RUNNER_DIR))

import amplify  # noqa: E402
import loop  # noqa: E402


def test_amplify_planned_invocations_count_non_isolated_turns() -> None:
    assert amplify.planned_invocation_counts(m=3, max_n=9, skip_non_isolated=False) == (27, 27)
    assert amplify.planned_invocation_counts(m=3, max_n=9, skip_non_isolated=True) == (27, 0)


def test_orchestrator_invocation_fails_on_nonzero_claude(monkeypatch, tmp_path: Path) -> None:
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["claude"], returncode=17)

    monkeypatch.setattr(loop.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="claude exited 17"):
        loop.invoke_claude_isolated(
            agent_claude_md="# Agent\n",
            agent_settings_json="{}",
            prompt="prompt",
            model="claude-haiku-4-5-20251001",
            traces_dir=tmp_path,
            trial_id="trial-001",
            label="student-r1-t0",
            allowed_tools="Bash",
        )
