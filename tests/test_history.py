from __future__ import annotations

import json
import os
import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from offwork_capsule.history import HistoryIndexService
from offwork_capsule.storage import (
    CURRENT_PROJECT_SCHEMA_VERSION,
    initialize_project_storage,
)


def _jsonl(path: Path, records: list[dict[str, object]], *, final_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
    if final_newline:
        payload += "\n"
    path.write_text(payload, encoding="utf-8")


def _append_jsonl(path: Path, record: dict[str, object], *, newline: bool = True) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False))
        if newline:
            handle.write("\n")


def _codex_meta(project: Path, session_id: str = "codex-session") -> dict[str, object]:
    return {
        "timestamp": "2026-08-26T01:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "cwd": str(project),
            "parent_thread_id": None,
        },
        "ordinal": 1,
    }


def _codex_message(
    role: str,
    text: str,
    message_id: str,
    *,
    content_type: str | None = None,
) -> dict[str, object]:
    if role == "user" and content_type is None:
        return {
            "timestamp": "2026-08-26T01:01:00Z",
            "type": "event_msg",
            "payload": {
                "id": message_id,
                "type": "user_message",
                "message": text,
            },
            "ordinal": 2,
        }
    if content_type is None:
        content_type = "input_text" if role == "user" else "output_text"
    return {
        "timestamp": "2026-08-26T01:01:00Z",
        "type": "response_item",
        "payload": {
            "id": message_id,
            "type": "message",
            "role": role,
            "content": [{"type": content_type, "text": text}],
        },
        "ordinal": 2,
    }


def _codex_desktop_user(
    turn_id: str | None, text: str, message_id: str
) -> dict[str, object]:
    record = {
        "timestamp": "2026-08-26T01:01:00Z",
        "type": "response_item",
        "payload": {
            "id": message_id,
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
        "ordinal": 2,
    }
    if turn_id is not None:
        record["internal_chat_message_metadata_passthrough"] = {
            "turn_id": turn_id
        }
    return record


def _claude_message(
    project: Path,
    role: str,
    content: object,
    message_id: str,
    *,
    session_id: str = "claude-session",
    **extra: object,
) -> dict[str, object]:
    return {
        "type": role,
        "sessionId": session_id,
        "cwd": str(project),
        "timestamp": "2026-08-26T01:02:00Z",
        "uuid": message_id,
        "isSidechain": False,
        "message": {"role": role, "content": content},
        **extra,
    }


@pytest.fixture(autouse=True)
def _isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


def _contents(service: HistoryIndexService) -> list[str]:
    with service.storage.connect() as connection:
        return [
            str(row["content"])
            for row in connection.execute(
                "SELECT content FROM messages ORDER BY source_offset, message_id"
            )
        ]


def test_source_settings_are_explicit_and_only_enabled_sources_are_scanned(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_file = tmp_path / "codex" / "one.jsonl"
    claude_file = tmp_path / "claude" / "one.jsonl"
    _jsonl(codex_file, [_codex_meta(project), _codex_message("user", "codex visible", "c1")])
    _jsonl(claude_file, [_claude_message(project, "user", "claude visible", "a1")])
    service = HistoryIndexService(project)

    assert service.source_settings() == []
    enabled = service.enable_source("codex")
    assert enabled["enabled"] is True
    assert enabled["revision"] == 1
    assert service.enable_source("codex")["revision"] == 2

    result = service.index(
        source_roots={"codex": [codex_file], "claude": [claude_file]}
    )

    assert result["discovered"] == 1
    assert result["indexed"] == 1
    assert _contents(service) == ["codex visible"]
    disabled = service.disable_source("codex")
    assert disabled["enabled"] is False
    assert disabled["revision"] == 3
    assert service.index(source_roots={"codex": [codex_file]})["discovered"] == 0


def test_codex_parser_only_indexes_visible_user_and_assistant_text(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "codex.jsonl"
    records = [
        _codex_meta(project),
        _codex_message("user", "中文用户输入", "u1"),
        _codex_message("assistant", "assistant visible", "a1"),
        {
            "timestamp": "2026-08-26T01:01:30Z",
            "type": "response_item",
            "payload": {
                "id": "duplicated-user-record",
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "中文用户输入 environment_context AGENTS recommended_plugins",
                    }
                ],
            },
        },
        _codex_message("developer", "developer secret", "d1", content_type="input_text"),
        _codex_message("system", "system secret", "s1", content_type="input_text"),
        _codex_message("user", "tool result secret", "t1", content_type="tool_result"),
        {"timestamp": "2026-08-26T01:03:00Z", "type": "reasoning", "payload": {"text": "reasoning secret"}},
        {"timestamp": "2026-08-26T01:03:00Z", "type": "agent_message", "payload": {"text": "agent secret"}},
        {"timestamp": "2026-08-26T01:03:00Z", "type": "response_item", "payload": {"type": "function_call", "arguments": "tool call secret"}},
    ]
    _jsonl(source, records)
    service = HistoryIndexService(project)
    service.enable_source("codex")

    result = service.index(source_roots={"codex": [source]})

    assert result["messages"] == 2
    assert _contents(service) == ["中文用户输入", "assistant visible"]


def test_codex_desktop_groups_control_blocks_by_turn_and_event_wins(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "codex-desktop.jsonl"
    event_winner = _codex_message("user", "third real from event", "event-3")
    event_winner["internal_chat_message_metadata_passthrough"] = {
        "turn_id": "turn-3"
    }
    payload_nested = _codex_desktop_user(
        None, "second real request", "turn-2-real"
    )
    payload_nested["payload"]["internal_chat_message_metadata_passthrough"] = {
        "turn_id": "turn-2"
    }
    _jsonl(
        source,
        [
            _codex_meta(project, "desktop-session"),
            _codex_desktop_user("turn-1", "environment_context secret", "t1-env"),
            _codex_desktop_user("turn-1", "AGENTS secret", "t1-agents"),
            _codex_desktop_user("turn-1", "first real request", "turn-1-real"),
            _codex_message("assistant", "assistant visible", "assistant-1"),
            _codex_desktop_user("turn-2", "recommended_plugins secret", "t2-plugin"),
            payload_nested,
            _codex_desktop_user(None, "missing turn must stay fenced", "no-turn"),
            _codex_desktop_user("turn-3", "control block", "t3-control"),
            _codex_desktop_user("turn-3", "response fallback must lose", "t3-real"),
            event_winner,
        ],
    )
    service = HistoryIndexService(project)
    service.enable_source("codex")

    result = service.index(source_roots={"codex": [source]})

    assert result["messages"] == 4
    assert _contents(service) == [
        "first real request",
        "assistant visible",
        "second real request",
        "third real from event",
    ]


def test_codex_desktop_synthetic_only_turns_remain_outside_history(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "codex-desktop-controls.jsonl"
    controls = [
        "<environment_context>API_TOKEN=secret</environment_context>",
        "<recommended_plugins>plugin secret</recommended_plugins>",
        "# AGENTS.md instructions for /private/project\n\n<INSTRUCTIONS>secret</INSTRUCTIONS>",
        "<app-context>app secret</app-context>",
        "<permissions instructions>permission secret</permissions instructions>",
        "<skills_instructions>skill secret</skills_instructions>",
        "<collaboration_mode>mode secret</collaboration_mode>",
        "The following is the Codex agent history whose request actioned a new Codex session: secret",
        "The following is the Codex agent history added since your last message: secret",
        "# Files mentioned by the user:\n\n## secrets.txt\nattachment envelope",
    ]
    records = [_codex_meta(project, "desktop-controls")] + [
        _codex_desktop_user("control-%d" % index, content, "c%d" % index)
        for index, content in enumerate(controls)
    ] + [
        _codex_desktop_user(
            "real-turn",
            "Please explain the <environment_context> tag without exposing secrets.",
            "real",
        )
    ]
    _jsonl(source, records)
    service = HistoryIndexService(project)
    service.enable_source("codex")

    result = service.index(source_roots={"codex": [source]})

    assert result["messages"] == 1
    assert _contents(service) == [
        "Please explain the <environment_context> tag without exposing secrets."
    ]


def test_codex_child_session_is_skipped_as_a_whole(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "child.jsonl"
    metadata = _codex_meta(project, "child")
    metadata["payload"] = {
        "id": "child",
        "cwd": str(project),
        "parent_thread_id": "parent",
    }
    _jsonl(source, [metadata, _codex_message("user", "must not index", "u1")])
    service = HistoryIndexService(project)
    service.enable_source("codex")

    result = service.index(source_roots={"codex": [source]})

    assert result["skipped"] == 1
    assert _contents(service) == []


def test_claude_parser_fences_sidechains_tools_thinking_and_role_mismatch(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "claude.jsonl"
    sidechain = _claude_message(project, "assistant", "sidechain secret", "side")
    sidechain["isSidechain"] = True
    mismatch = _claude_message(project, "user", "mismatch secret", "mismatch")
    mismatch["message"] = {"role": "assistant", "content": "mismatch secret"}
    _jsonl(
        source,
        [
            _claude_message(project, "user", "user visible", "u1"),
            _claude_message(
                project,
                "assistant",
                [
                    {"type": "text", "text": "assistant visible"},
                    {"type": "thinking", "thinking": "thinking secret"},
                    {"type": "tool_use", "input": "tool input secret"},
                    {"type": "tool_result", "content": "tool output secret"},
                ],
                "a1",
            ),
            _claude_message(
                project,
                "assistant",
                "tool wrapper secret",
                "tool",
                toolUseResult={"content": "tool result secret"},
            ),
            sidechain,
            mismatch,
            {
                "type": "user",
                "sessionId": "claude-session",
                "timestamp": "2026-08-26T01:02:00Z",
                "uuid": "meta-command",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": "skill local-command coordinator meta secret",
                },
            },
            {"type": "system", "sessionId": "claude-session", "cwd": str(project), "message": {"role": "system", "content": "system secret"}},
        ],
    )
    service = HistoryIndexService(project)
    service.enable_source("claude")

    result = service.index(source_roots={"claude": [source]})

    assert result["messages"] == 2
    assert _contents(service) == ["user visible", "assistant visible"]


def test_sources_without_one_unambiguous_in_project_session_are_rejected(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    inside = project / "nested"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    valid = tmp_path / "valid.jsonl"
    outside_file = tmp_path / "outside.jsonl"
    missing_cwd = tmp_path / "missing.jsonl"
    mixed_session = tmp_path / "mixed.jsonl"
    _jsonl(valid, [_claude_message(inside, "user", "inside visible", "v1", session_id="inside")])
    _jsonl(outside_file, [_claude_message(outside, "user", "outside secret", "o1", session_id="outside")])
    record = _claude_message(project, "user", "missing secret", "m1", session_id="missing")
    record.pop("cwd")
    _jsonl(missing_cwd, [record])
    _jsonl(
        mixed_session,
        [
            _claude_message(project, "user", "first secret", "x1", session_id="one"),
            _claude_message(project, "assistant", "second secret", "x2", session_id="two"),
        ],
    )
    service = HistoryIndexService(project)
    service.enable_source("claude")

    result = service.index(
        source_roots={
            "claude": [valid, outside_file, missing_cwd, mixed_session]
        }
    )

    assert result["discovered"] == 4
    assert result["indexed"] == 1
    assert result["skipped"] == 3
    assert _contents(service) == ["inside visible"]


def test_any_incomplete_authoritative_session_metadata_rejects_the_source(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_source = tmp_path / "codex-incomplete.jsonl"
    incomplete_meta = _codex_meta(project)
    incomplete_meta["payload"] = {"id": "codex-session", "cwd": None}
    _jsonl(
        codex_source,
        [
            _codex_meta(project),
            incomplete_meta,
            _codex_message("user", "codex secret", "c1"),
        ],
    )
    claude_source = tmp_path / "claude-incomplete.jsonl"
    incomplete_claude = _claude_message(
        project, "assistant", "claude secret", "a2"
    )
    incomplete_claude.pop("cwd")
    _jsonl(
        claude_source,
        [
            _claude_message(project, "user", "claude secret", "a1"),
            incomplete_claude,
        ],
    )
    service = HistoryIndexService(project)
    service.enable_source("codex")
    service.enable_source("claude")

    result = service.index(
        source_roots={"codex": [codex_source], "claude": [claude_source]}
    )

    assert result["skipped"] == 2
    assert _contents(service) == []


def test_index_is_idempotent_appends_from_offset_and_holds_partial_lines(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "codex.jsonl"
    _jsonl(source, [_codex_meta(project), _codex_message("user", "first", "m1")])
    service = HistoryIndexService(project)
    service.enable_source("codex")

    first = service.index(source_roots={"codex": [source]})
    with service.storage.connect() as connection:
        original_id = str(connection.execute("SELECT message_id FROM messages").fetchone()[0])
    unchanged = service.index(source_roots={"codex": [source]})
    _append_jsonl(source, _codex_message("assistant", "second", "m2"))
    appended = service.index(source_roots={"codex": [source]})
    offset_before_partial = service._source_record(source, "codex")["read_offset"]
    _append_jsonl(source, _codex_message("user", "partial then complete", "m3"), newline=False)
    partial = service.index(source_roots={"codex": [source]})
    offset_during_partial = service._source_record(source, "codex")["read_offset"]
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    completed = service.index(source_roots={"codex": [source]})

    assert first["messages"] == 1
    assert unchanged["indexed"] == 0
    assert unchanged["skipped"] == 1
    assert appended["messages"] == 1
    assert partial["messages"] == 0
    assert offset_during_partial == offset_before_partial
    assert completed["messages"] == 1
    assert _contents(service) == ["first", "second", "partial then complete"]
    with service.storage.connect() as connection:
        assert str(connection.execute("SELECT message_id FROM messages WHERE content = 'first'").fetchone()[0]) == original_id


def test_replace_or_rewrite_rebuilds_source_without_stale_messages(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "claude.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    _jsonl(source, [_claude_message(project, "user", "old text", "old")])
    service = HistoryIndexService(project)
    service.enable_source("claude")
    service.index(source_roots={"claude": [source]})
    _jsonl(replacement, [_claude_message(project, "assistant", "new text", "new")])
    os.replace(replacement, source)

    result = service.index(source_roots={"claude": [source]})

    assert result["rebuilt"] == 1
    assert _contents(service) == ["new text"]


def test_inode_replacement_rebuilds_even_when_size_and_mtime_are_unchanged(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "claude.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    _jsonl(source, [_claude_message(project, "user", "old text", "old")])
    service = HistoryIndexService(project)
    service.enable_source("claude")
    service.index(source_roots={"claude": [source]})
    old_stat = source.stat()
    _jsonl(
        replacement,
        [_claude_message(project, "user", "new text", "new")],
    )
    assert replacement.stat().st_size == old_stat.st_size
    os.utime(
        replacement,
        ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns),
    )
    os.replace(replacement, source)

    result = service.index(source_roots={"claude": [source]})

    assert result["rebuilt"] == 1
    assert _contents(service) == ["new text"]


def test_early_committed_rewrite_plus_append_fails_full_prefix_guard_and_rebuilds(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "codex-large.jsonl"
    records = [
        _codex_meta(project),
        _codex_message("assistant", "original marker", "marker"),
    ] + [
        _codex_message("assistant", "filler-%04d-%s" % (index, "甲" * 40), "f%d" % index)
        for index in range(150)
    ]
    _jsonl(source, records)
    service = HistoryIndexService(project)
    service.enable_source("codex")
    service.index(source_roots={"codex": [source]})
    initial = service._source_record(source, "codex")
    assert int(initial["read_offset"]) > 4096
    rewritten = source.read_bytes().replace(
        b"original marker", b"tampered marker", 1
    )
    source.write_bytes(rewritten)
    _append_jsonl(
        source, _codex_message("assistant", "appended after rewrite", "appended")
    )

    result = service.index(source_roots={"codex": [source]})

    assert result["rebuilt"] == 1
    contents = _contents(service)
    assert "original marker" not in contents
    assert "tampered marker" in contents
    assert "appended after rewrite" in contents
    row = service._source_record(source, "codex")
    committed = source.read_bytes()[: int(row["read_offset"])]
    prefix_hash = hashlib.sha256(committed).hexdigest()
    assert row["checkpoint_hash"] == prefix_hash
    assert prefix_hash in str(row["source_fingerprint"])


def test_missing_source_deletes_derived_messages_and_reappearance_rebuilds(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "codex.jsonl"
    _jsonl(source, [_codex_meta(project), _codex_message("user", "before delete", "old")])
    service = HistoryIndexService(project)
    service.enable_source("codex")
    service.index(source_roots={"codex": [source]})
    source.unlink()

    missing = service.index(source_roots={"codex": [source]})

    assert missing["missing"] == 1
    assert _contents(service) == []
    with service.storage.connect() as connection:
        assert connection.execute("SELECT state FROM session_sources").fetchone()[0] == "missing"

    _jsonl(source, [_codex_meta(project), _codex_message("assistant", "after return", "new")])
    returned = service.index(source_roots={"codex": [source]})
    assert returned["rebuilt"] == 1
    assert _contents(service) == ["after return"]


def test_missing_session_source_can_reappear_at_a_new_path_without_conflict(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    old_path = tmp_path / "old" / "codex.jsonl"
    new_path = tmp_path / "new" / "codex.jsonl"
    _jsonl(
        old_path,
        [
            _codex_meta(project, "moved-session"),
            _codex_message("assistant", "old location", "old"),
        ],
    )
    service = HistoryIndexService(project)
    service.enable_source("codex")
    service.index(source_roots={"codex": [old_path]})
    original_source_id = str(
        service._source_record(old_path, "codex")["source_id"]
    )
    old_path.unlink()
    service.index(source_roots={"codex": [old_path]})
    _jsonl(
        new_path,
        [
            _codex_meta(project, "moved-session"),
            _codex_message("assistant", "new location", "new"),
        ],
    )

    result = service.index(source_roots={"codex": [new_path]})

    assert result["messages"] == 1
    assert result["rebuilt"] == 1
    assert not result["errors"]
    assert _contents(service) == ["new location"]
    with service.storage.connect() as connection:
        rows = connection.execute(
            "SELECT source_id,source_path,source_session_id,state FROM session_sources"
        ).fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == (
        original_source_id,
        str(new_path.resolve()),
        "moved-session",
        "indexed",
    )


def test_stale_missing_reconciliation_cannot_erase_moved_path_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    old_path = tmp_path / "old" / "codex.jsonl"
    new_path = tmp_path / "new" / "codex.jsonl"
    _jsonl(
        old_path,
        [
            _codex_meta(project, "moved-session"),
            _codex_message("assistant", "old location", "old"),
        ],
    )
    slow = HistoryIndexService(project)
    fast = HistoryIndexService(project)
    slow.enable_source("codex")
    slow.index(source_roots={"codex": [old_path]})
    new_path.parent.mkdir(parents=True)
    old_path.replace(new_path)
    _jsonl(
        new_path,
        [
            _codex_meta(project, "moved-session"),
            _codex_message("assistant", "new location", "new"),
        ],
    )

    reached = threading.Event()
    release = threading.Event()
    storage_type = type(slow.storage)
    real_connect = storage_type.connect

    class PausingConnection:
        def __init__(self, connection: object) -> None:
            self.connection = connection
            self.pause_after_exit = False

        def __enter__(self) -> "PausingConnection":
            self.connection.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *args: object) -> object:
            result = self.connection.__exit__(*args)  # type: ignore[attr-defined]
            if self.pause_after_exit:
                reached.set()
                if not release.wait(5):
                    raise RuntimeError("test timed out waiting to resume missing reconciliation")
            return result

        def execute(self, sql: str, *args: object) -> object:
            result = self.connection.execute(sql, *args)  # type: ignore[attr-defined]
            if sql.startswith("SELECT source_id,source_path,state"):
                self.pause_after_exit = True
            return result

        def __getattr__(self, name: str) -> object:
            return getattr(self.connection, name)

    def paused_connect(storage: object) -> object:
        connection = real_connect(storage)
        if threading.current_thread().name == "slow-missing-snapshot":
            return PausingConnection(connection)
        return connection

    monkeypatch.setattr(storage_type, "connect", paused_connect)
    output: dict[str, object] = {}

    def run_slow_reconciliation() -> None:
        try:
            output["result"] = slow.index(source_roots={"codex": []})
        except BaseException as error:  # pragma: no cover - surfaced in caller
            output["error"] = error

    worker = threading.Thread(
        name="slow-missing-snapshot", target=run_slow_reconciliation
    )
    worker.start()
    assert reached.wait(5)

    assert fast.index(source_roots={"codex": []})["missing"] == 1
    takeover = fast.index(source_roots={"codex": [new_path]})
    assert takeover["messages"] == 1
    assert takeover["rebuilt"] == 1
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert "error" not in output
    assert output["result"]["missing"] == 0  # type: ignore[index]
    assert _contents(fast) == ["new location"]
    with fast.storage.connect() as connection:
        row = connection.execute(
            "SELECT source_path,state FROM session_sources"
        ).fetchone()
    assert tuple(row) == (str(new_path.resolve()), "indexed")


def _pause_before_store(
    service: HistoryIndexService,
    reached: threading.Event,
    release: threading.Event,
) -> None:
    original = service._store_source

    def paused(*args: object, **kwargs: object) -> object:
        reached.set()
        if not release.wait(5):
            raise RuntimeError("test timed out waiting to resume store")
        return original(*args, **kwargs)

    service._store_source = paused  # type: ignore[method-assign]


def _run_index_in_thread(
    service: HistoryIndexService,
    source: Path,
    output: dict[str, object],
) -> None:
    try:
        output["result"] = service.index(source_roots={"codex": [source]})
    except BaseException as error:  # pragma: no cover - surfaced in caller
        output["error"] = error


def test_file_change_after_parse_is_rejected_before_stale_snapshot_commit(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "codex.jsonl"
    _jsonl(
        source,
        [_codex_meta(project), _codex_message("assistant", "snapshot-base", "base")],
    )
    service = HistoryIndexService(project)
    service.enable_source("codex")
    service.index(source_roots={"codex": [source]})
    _jsonl(
        source,
        [_codex_meta(project), _codex_message("assistant", "snapshot-a", "a")],
    )
    reached = threading.Event()
    release = threading.Event()
    _pause_before_store(service, reached, release)
    output: dict[str, object] = {}
    worker = threading.Thread(
        target=_run_index_in_thread, args=(service, source, output)
    )
    worker.start()
    assert reached.wait(5)
    _jsonl(
        source,
        [_codex_meta(project), _codex_message("assistant", "snapshot-b", "b")],
    )
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert "error" not in output
    result = output["result"]
    assert any(
        error["code"] == "SOURCE_CHANGED_DURING_INDEX"
        for error in result["errors"]
    )
    assert _contents(service) == ["snapshot-base"]


def test_slower_index_cannot_overwrite_a_newer_committed_source_snapshot(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "codex.jsonl"
    _jsonl(
        source,
        [_codex_meta(project), _codex_message("assistant", "snapshot-base", "base")],
    )
    slow = HistoryIndexService(project)
    fast = HistoryIndexService(project)
    slow.enable_source("codex")
    slow.index(source_roots={"codex": [source]})
    _jsonl(
        source,
        [_codex_meta(project), _codex_message("assistant", "snapshot-a", "a")],
    )
    reached = threading.Event()
    release = threading.Event()
    _pause_before_store(slow, reached, release)
    output: dict[str, object] = {}
    worker = threading.Thread(
        target=_run_index_in_thread, args=(slow, source, output)
    )
    worker.start()
    assert reached.wait(5)
    _jsonl(
        source,
        [_codex_meta(project), _codex_message("assistant", "snapshot-b", "b")],
    )
    fast_result = fast.index(source_roots={"codex": [source]})
    release.set()
    worker.join(5)

    assert fast_result["messages"] == 1
    assert not worker.is_alive()
    assert "error" not in output
    slow_result = output["result"]
    assert any(
        error["code"] == "SOURCE_INDEX_CONFLICT"
        for error in slow_result["errors"]
    )
    assert _contents(fast) == ["snapshot-b"]


def test_source_links_to_managed_session_and_task_when_native_id_matches(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "codex.jsonl"
    _jsonl(source, [_codex_meta(project, "native-1"), _codex_message("user", "linked", "u1")])
    service = HistoryIndexService(project)
    now = datetime.now(timezone.utc).isoformat()
    with service.storage.connect() as connection:
        connection.execute(
            "INSERT INTO tasks(task_id,title,goal,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("task-1", "task", "goal", "todo", now, now),
        )
        connection.execute(
            "INSERT INTO sessions(managed_session_id,task_id,provider,native_session_id,cwd,is_primary,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("managed-1", "task-1", "codex", "native-1", str(project.resolve()), 1, "active", now, now),
        )
    service.enable_source("codex")

    service.index(source_roots={"codex": [source]})

    with service.storage.connect() as connection:
        row = connection.execute(
            "SELECT managed_session_id,task_id,source_session_id FROM session_sources"
        ).fetchone()
    assert tuple(row) == ("managed-1", "task-1", "native-1")


def test_unchanged_source_is_linked_when_managed_session_is_attached_later(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "claude.jsonl"
    _jsonl(
        source,
        [
            _claude_message(
                project,
                "user",
                "linked after attach",
                "u1",
                session_id="native-later",
            )
        ],
    )
    service = HistoryIndexService(project)
    service.enable_source("claude")
    service.index(source_roots={"claude": [source]})
    now = datetime.now(timezone.utc).isoformat()
    with service.storage.connect() as connection:
        connection.execute(
            "INSERT INTO tasks(task_id,title,goal,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("task-later", "task", "goal", "todo", now, now),
        )
        connection.execute(
            "INSERT INTO sessions(managed_session_id,task_id,provider,native_session_id,cwd,is_primary,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "managed-later",
                "task-later",
                "claude",
                "native-later",
                str(project.resolve()),
                1,
                "active",
                now,
                now,
            ),
        )

    unchanged = service.index(source_roots={"claude": [source]})

    assert unchanged["skipped"] == 1
    with service.storage.connect() as connection:
        row = connection.execute(
            "SELECT managed_session_id,task_id FROM session_sources"
        ).fetchone()
    assert tuple(row) == ("managed-later", "task-later")


def test_malformed_json_is_counted_without_leaking_raw_content(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "codex.jsonl"
    secret = "MALFORMED-SHOULD-NOT-LEAK"
    source.write_text(
        json.dumps(_codex_meta(project))
        + "\n{not-json:"
        + secret
        + "}\n"
        + json.dumps(_codex_message("user", "safe visible", "u1"))
        + "\n",
        encoding="utf-8",
    )
    service = HistoryIndexService(project)
    service.enable_source("codex")

    result = service.index(source_roots={"codex": [source]})

    assert result["messages"] == 1
    assert result["errors"]
    assert secret not in json.dumps(result, ensure_ascii=False)
    assert _contents(service) == ["safe visible"]


def test_service_uses_current_storage_schema_and_reports_progress(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "claude.jsonl"
    _jsonl(source, [_claude_message(project, "user", "进度", "u1")])
    progress: list[dict[str, object]] = []
    service = HistoryIndexService(project)
    service.enable_source("claude")

    result = service.index(
        source_roots={"claude": [source]}, progress_callback=progress.append
    )

    with initialize_project_storage(project).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_PROJECT_SCHEMA_VERSION
        assert connection.execute("SELECT count(*) FROM messages_fts").fetchone()[0] == 1
    assert result["messages"] == 1
    assert progress
    assert progress[-1]["processed"] == progress[-1]["discovered"] == 1


def test_large_source_emits_bounded_intermediate_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "large.jsonl"
    records = [_codex_meta(project)] + [
        _codex_message("user", "message %d" % index, "m%d" % index)
        for index in range(2501)
    ]
    _jsonl(source, records)
    progress: list[dict[str, object]] = []
    json_lines_seen = 0
    real_loads = json.loads

    def counting_loads(*args: object, **kwargs: object) -> object:
        nonlocal json_lines_seen
        json_lines_seen += 1
        return real_loads(*args, **kwargs)

    def record_progress(item: dict[str, object]) -> None:
        progress.append({**item, "json_lines_seen": json_lines_seen})

    monkeypatch.setattr("offwork_capsule.history.json.loads", counting_loads)
    service = HistoryIndexService(project)
    service.enable_source("codex")

    service.index(
        source_roots={"codex": [source]}, progress_callback=record_progress
    )

    intermediate = [item for item in progress if item.get("phase") == "parsing"]
    assert [item["messages_seen"] for item in intermediate] == [1000, 2000]
    assert int(intermediate[0]["json_lines_seen"]) < 1500
    assert int(intermediate[1]["json_lines_seen"]) < 2500
