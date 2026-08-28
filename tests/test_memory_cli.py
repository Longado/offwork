from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from offwork_capsule.sessions import SessionService
from offwork_capsule.state import StateService


ROOT = Path(__file__).parents[1]
ENVELOPE_KEYS = {
    "schema_version",
    "command",
    "ok",
    "data",
    "meta",
    "warnings",
    "error",
}


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    env["HOME"] = str(tmp_path / "home")
    return subprocess.run(
        [sys.executable, "-m", "offwork_capsule.cli", *arguments],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=tmp_path,
    )


def _json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    payload = json.loads(result.stdout)
    assert set(payload) == ENVELOPE_KEYS
    assert payload["schema_version"] == "offwork.cli/v1"
    return payload


def _codex_history(
    project: Path,
    path: Path,
    *,
    count: int = 1,
    session_id: str = "native-codex-1",
    text: str = "中文恢复证据",
) -> None:
    records: list[dict[str, Any]] = [
        {
            "timestamp": "2026-08-26T01:00:00Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(project)},
        }
    ]
    for index in range(count):
        records.append(
            {
                "timestamp": "2026-08-26T01:01:00Z",
                "type": "response_item",
                "payload": {
                    "id": "assistant-%d" % index,
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "%s %d" % (text, index),
                        }
                    ],
                },
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _add_task(tmp_path: Path, project: Path) -> dict[str, Any]:
    result = _run(
        tmp_path,
        "task",
        "add",
        "cli memory task",
        "--goal",
        "verify memory commands",
        "--project",
        str(project),
        "--json",
    )
    assert result.returncode == 0
    return _json(result)["data"]


def test_source_enable_disable_human_and_json_contract(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    enabled = _run(
        tmp_path,
        "source",
        "enable",
        "codex",
        "--project",
        str(project),
        "--json",
    )
    payload = _json(enabled)
    assert enabled.returncode == 0
    assert enabled.stderr == ""
    assert payload["command"] == "source.enable"
    assert payload["data"]["source_kind"] == "codex"
    assert payload["data"]["enabled"] is True
    assert payload["data"]["revision"] == 1

    enabled_human = _run(
        tmp_path,
        "source",
        "enable",
        "codex",
        "--project",
        str(project),
    )
    assert enabled_human.returncode == 0
    assert "codex enabled (revision 2)" in enabled_human.stdout

    disabled = _run(
        tmp_path,
        "source",
        "disable",
        "codex",
        "--project",
        str(project),
    )
    assert disabled.returncode == 0
    assert disabled.stderr == ""
    assert "codex" in disabled.stdout
    assert "disabled" in disabled.stdout
    assert "revision 3" in disabled.stdout

    disabled_json = _run(
        tmp_path,
        "source",
        "disable",
        "codex",
        "--project",
        str(project),
        "--json",
    )
    assert _json(disabled_json)["command"] == "source.disable"


def test_invalid_source_is_parseable_error_without_database(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    invalid = _run(
        tmp_path,
        "source",
        "enable",
        "other",
        "--project",
        str(project),
        "--json",
    )

    payload = _json(invalid)
    assert invalid.returncode == 2
    assert invalid.stderr == ""
    assert payload["command"] == "source.enable"
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert not (project / ".offwork").exists()


def test_index_uses_only_enabled_default_sources_and_json_stdout_is_pure(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    history = tmp_path / "home" / ".codex" / "sessions" / "one.jsonl"
    _codex_history(project, history, count=1000)
    enable = _run(
        tmp_path,
        "source",
        "enable",
        "codex",
        "--project",
        str(project),
        "--json",
    )
    assert enable.returncode == 0

    indexed = _run(
        tmp_path,
        "index",
        "--project",
        str(project),
        "--json",
    )

    payload = _json(indexed)
    assert indexed.returncode == 0
    assert payload["command"] == "index"
    assert payload["data"]["discovered"] == 1
    assert payload["data"]["indexed"] == 1
    assert payload["data"]["messages"] == 1000
    assert payload["data"]["errors"] == []
    assert "1000" in indexed.stderr
    assert "中文恢复证据" not in indexed.stderr

    human = _run(tmp_path, "index", "--project", str(project))
    assert human.returncode == 0
    assert "Indexed:" in human.stdout
    assert "Messages:" in human.stdout


def test_index_progress_is_global_across_many_small_sources(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    history_root = tmp_path / "home" / ".codex" / "sessions"
    for source_index in range(5):
        _codex_history(
            project,
            history_root / ("%d.jsonl" % source_index),
            count=250,
            session_id="native-codex-%d" % source_index,
        )
    assert _run(
        tmp_path,
        "source",
        "enable",
        "codex",
        "--project",
        str(project),
        "--json",
    ).returncode == 0

    indexed = _run(
        tmp_path,
        "index",
        "--project",
        str(project),
        "--json",
    )

    payload = _json(indexed)
    assert indexed.returncode == 0
    assert payload["data"]["messages"] == 1250
    assert "1000" in indexed.stderr
    assert "中文恢复证据" not in indexed.stderr


def test_search_human_escapes_terminal_controls_and_field_forgery(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    hostile_text = (
        "needle\nSource: forged\r\nTask: forged"
        "\x1b]8;;https://evil.invalid\x07click\x1b]8;;\x07"
        "\x1b[31mred\x1b[0m\x85next"
    )
    hostile_session = "native\nTask: forged\x1b[2J"
    history = tmp_path / "home" / ".codex" / "sessions" / "hostile.jsonl"
    _codex_history(
        project,
        history,
        session_id=hostile_session,
        text=hostile_text,
    )
    assert _run(
        tmp_path,
        "source",
        "enable",
        "codex",
        "--project",
        str(project),
    ).returncode == 0
    assert _run(tmp_path, "index", "--project", str(project)).returncode == 0

    searched = _run(
        tmp_path,
        "search",
        "needle",
        "--project",
        str(project),
    )

    assert searched.returncode == 0
    assert searched.stderr == ""
    assert "\x1b" not in searched.stdout
    assert "\x07" not in searched.stdout
    assert "\x85" not in searched.stdout
    lines = searched.stdout.splitlines()
    assert len([line for line in lines if line.startswith("Source:")]) == 1
    assert len([line for line in lines if line.startswith("Task:")]) == 1
    assert "\\nSource: forged\\r\\nTask: forged" in searched.stdout
    assert "\\x1b]8;;https://evil.invalid\\x07" in searched.stdout
    assert "Native session: native\\nTask: forged\\x1b[2J" in searched.stdout

    searched_json = _run(
        tmp_path,
        "search",
        "needle",
        "--project",
        str(project),
        "--json",
    )
    raw_result = _json(searched_json)["data"]["results"][0]
    assert "\x1b]8;;https://evil.invalid\x07" in raw_result["snippet"]
    assert raw_result["source_session_id"] == hostile_session


def test_search_human_shows_warning_and_complete_evidence_and_json_empty(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    history = tmp_path / "home" / ".codex" / "sessions" / "one.jsonl"
    _codex_history(project, history)
    assert _run(
        tmp_path,
        "source",
        "enable",
        "codex",
        "--project",
        str(project),
    ).returncode == 0
    assert _run(tmp_path, "index", "--project", str(project)).returncode == 0

    human = _run(
        tmp_path,
        "search",
        "中文恢复",
        "--source",
        "codex",
        "--project",
        str(project),
    )
    assert human.returncode == 0
    assert human.stderr == ""
    assert human.stdout.splitlines()[0] == "历史内容不证明当前状态"
    for value in (
        "Source: codex",
        "Time: 2026-08-26T01:01:00Z",
        "Role: assistant",
        "Managed session: None",
        "Native session: native-codex-1",
        "Task: None",
        "Snippet: 中文恢复证据 0",
        "Evidence:",
        str(history),
        "offset=",
        "fingerprint=",
    ):
        assert value in human.stdout

    empty = _run(
        tmp_path,
        "search",
        "不存在的内容",
        "--project",
        str(project),
        "--json",
    )
    payload = _json(empty)
    assert empty.returncode == 0
    assert empty.stderr == ""
    assert payload["command"] == "search"
    assert payload["data"] == {
        "query": "不存在的内容",
        "results": [],
        "warning": "历史内容不证明当前状态",
    }


def test_search_invalid_task_is_fixed_error_envelope(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = _run(
        tmp_path,
        "search",
        "中文",
        "--task",
        "tsk_missing",
        "--project",
        str(project),
        "--json",
    )

    payload = _json(result)
    assert result.returncode == 3
    assert result.stderr == ""
    assert payload["command"] == "search"
    assert payload["error"]["code"] == "TASK_NOT_FOUND"


def test_memory_add_list_forget_human_and_json(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = _add_task(tmp_path, project)
    session = SessionService(project).attach(task["task_id"], "codex")

    added = _run(
        tmp_path,
        "memory",
        "add",
        "只保留明确保存的事实",
        "--task",
        task["task_id"],
        "--session",
        session["managed_session_id"],
        "--project",
        str(project),
        "--json",
    )
    payload = _json(added)
    assert added.returncode == 0
    assert added.stderr == ""
    assert payload["command"] == "memory.add"
    memory = payload["data"]
    assert memory["provenance_kind"] == "user_explicit"
    assert memory["task_id"] == task["task_id"]

    added_human = _run(
        tmp_path,
        "memory",
        "add",
        "项目级明确记忆",
        "--project",
        str(project),
    )
    assert added_human.returncode == 0
    assert "[active]" in added_human.stdout
    assert "项目级明确记忆" in added_human.stdout

    listed = _run(
        tmp_path,
        "memory",
        "list",
        "--task",
        task["task_id"],
        "--project",
        str(project),
    )
    assert listed.returncode == 0
    assert listed.stderr == ""
    assert memory["memory_id"] in listed.stdout
    assert "只保留明确保存的事实" in listed.stdout

    forgotten = _run(
        tmp_path,
        "memory",
        "forget",
        memory["memory_id"],
        "--revision",
        str(memory["revision"]),
        "--project",
        str(project),
    )
    assert forgotten.returncode == 0
    assert forgotten.stderr == ""
    assert "forgotten" in forgotten.stdout

    forgotten_json = _run(
        tmp_path,
        "memory",
        "forget",
        memory["memory_id"],
        "--project",
        str(project),
        "--json",
    )
    assert _json(forgotten_json)["command"] == "memory.forget"

    active = _run(
        tmp_path,
        "memory",
        "list",
        "--project",
        str(project),
        "--json",
    )
    active_memories = _json(active)["data"]["memories"]
    assert [item["content"] for item in active_memories] == ["项目级明确记忆"]
    all_memories = _run(
        tmp_path,
        "memory",
        "list",
        "--forgotten",
        "--project",
        str(project),
        "--json",
    )
    archived = _json(all_memories)["data"]["memories"]
    forgotten_memory = next(
        item for item in archived if item["memory_id"] == memory["memory_id"]
    )
    assert forgotten_memory["archived_at"] is not None


def test_memory_reference_and_revision_errors_are_parseable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    missing_task = _run(
        tmp_path,
        "memory",
        "add",
        "fact",
        "--task",
        "tsk_missing",
        "--project",
        str(project),
        "--json",
    )
    assert missing_task.returncode == 3
    assert _json(missing_task)["error"]["code"] == "TASK_NOT_FOUND"

    missing_session = _run(
        tmp_path,
        "memory",
        "add",
        "fact",
        "--session",
        "msn_missing",
        "--project",
        str(project),
        "--json",
    )
    assert missing_session.returncode in (3, 4)
    assert _json(missing_session)["error"]["code"] in {
        "SESSION_NOT_FOUND",
        "MEMORY_SCOPE_MISMATCH",
    }

    added = _run(
        tmp_path,
        "memory",
        "add",
        "fact",
        "--project",
        str(project),
        "--json",
    )
    memory = _json(added)["data"]
    stale = _run(
        tmp_path,
        "memory",
        "forget",
        memory["memory_id"],
        "--revision",
        str(memory["revision"] + 1),
        "--project",
        str(project),
        "--json",
    )
    assert stale.returncode == 4
    assert _json(stale)["error"]["code"] == "STALE_REVISION"


def test_memory_source_search_index_help_do_not_initialize_databases(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    commands = [
        ("source", "enable", "--help"),
        ("source", "disable", "--help"),
        ("index", "--help"),
        ("search", "--help"),
        ("memory", "add", "--help"),
        ("memory", "list", "--help"),
        ("memory", "forget", "--help"),
    ]

    for command in commands:
        result = _run(tmp_path, *command)
        assert result.returncode == 0, command

    assert not (project / ".offwork").exists()
    assert not (tmp_path / ".offwork").exists()
    assert not (tmp_path / "xdg-data").exists()


def test_nested_command_name_only_uses_known_subcommands(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    for parent in ("source", "memory", "session"):
        missing = _run(tmp_path, parent, "--json")
        payload = _json(missing)
        assert missing.returncode == 2
        assert payload["command"] == parent

    unknown = _run(tmp_path, "source", "bogus", "--json")
    assert unknown.returncode == 2
    assert _json(unknown)["command"] == "source"
