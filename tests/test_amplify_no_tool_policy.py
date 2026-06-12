from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_DIR = ROOT / "applications" / "amplify" / "runner"
sys.path.insert(0, str(RUNNER_DIR))

import calibrate  # noqa: E402


def test_no_tool_command_disables_tools_and_mcp() -> None:
    cmd = calibrate.build_no_tool_claude_cmd("prompt", "claude-haiku-4-5-20251001")

    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert "--setting-sources" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert "--disable-slash-commands" in cmd
    assert "--no-session-persistence" in cmd
    assert "--permission-mode" not in cmd
    assert "bypassPermissions" not in cmd


def test_no_tool_command_preserves_session_when_requested() -> None:
    cmd = calibrate.build_no_tool_claude_cmd(
        "prompt",
        "claude-haiku-4-5-20251001",
        session_id="00000000-0000-0000-0000-000000000001",
        persist_session=True,
    )

    assert "--session-id" in cmd
    assert "--no-session-persistence" not in cmd


def test_trace_policy_violation_rejects_forbidden_tools(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "tools": ["Bash", "WebFetch"],
                "mcp_servers": [{"name": "example", "status": "connected"}],
            }
        )
        + "\n"
    )

    assert calibrate.trace_policy_violation(trace)


def test_trace_policy_violation_accepts_empty_tool_surface(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "tools": [],
                "mcp_servers": [],
            }
        )
        + "\n"
    )

    assert not calibrate.trace_policy_violation(trace)


def test_trace_policy_violation_fails_closed_without_init(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({"type": "assistant", "message": {"content": []}}) + "\n")

    assert calibrate.trace_policy_violation(trace)


def test_probe_outcome_taxonomy_separates_harness_failures() -> None:
    assert (
        calibrate.classify_probe_outcome(
            returncode=None,
            reported=None,
            truth=10,
            policy_violation=False,
        )
        == "cli_not_found"
    )
    assert (
        calibrate.classify_probe_outcome(
            returncode=1,
            reported=None,
            truth=10,
            policy_violation=False,
        )
        == "cli_nonzero"
    )
    assert (
        calibrate.classify_probe_outcome(
            returncode=0,
            reported=10,
            truth=10,
            policy_violation=True,
        )
        == "tool_policy_violation"
    )
    assert (
        calibrate.classify_probe_outcome(
            returncode=0,
            reported=10,
            truth=10,
            policy_violation=False,
        )
        == "model_correct"
    )
