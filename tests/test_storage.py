from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import fcntl
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from offwork_capsule import capsule as capsule_module
from offwork_capsule.capsule import (
    CapsuleValidationError,
    archive_capsule,
    build_capsule,
    load_latest_capsule,
    validate_for_restore,
)
from offwork_capsule.project import capture_project_state


NOW = datetime(2026, 8, 26, 2, 3, 4, tzinfo=timezone.utc)


def _storage_module():
    try:
        return importlib.import_module("offwork_capsule.storage")
    except ModuleNotFoundError:
        pytest.fail("offwork_capsule.storage is not implemented")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _context() -> dict:
    return {
        "goal": "继续当前任务",
        "next_step": "运行下一项检查",
        "open_loops": [],
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)}


def _column_info(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {
        row[1]: row for row in connection.execute("PRAGMA table_info(%s)" % table)
    }


def _create_legacy_v2_database(database: Path, task_status: str) -> None:
    with sqlite3.connect(str(database)) as connection:
        connection.executescript(
            f"""
            PRAGMA foreign_keys = ON;
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                goal TEXT NOT NULL DEFAULT '',
                acceptance_commands_json TEXT NOT NULL DEFAULT '[]',
                auto_complete INTEGER NOT NULL DEFAULT 0,
                open_loops_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                archived_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE task_dependencies (
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                depends_on_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (task_id, depends_on_task_id)
            );
            CREATE TABLE sessions (
                managed_session_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                native_session_id TEXT,
                tmux_socket TEXT,
                tmux_name TEXT,
                cwd TEXT NOT NULL,
                is_primary INTEGER NOT NULL DEFAULT 0,
                parent_session_id TEXT REFERENCES sessions(managed_session_id),
                state TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE capsules (
                capsule_id TEXT PRIMARY KEY,
                task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
                managed_session_id TEXT REFERENCES sessions(managed_session_id) ON DELETE SET NULL,
                parent_capsule_id TEXT REFERENCES capsules(capsule_id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                archive_path TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                archived_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE memories (
                memory_id TEXT PRIMARY KEY,
                task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
                managed_session_id TEXT REFERENCES sessions(managed_session_id) ON DELETE SET NULL,
                capsule_id TEXT REFERENCES capsules(capsule_id) ON DELETE SET NULL,
                content TEXT NOT NULL,
                provenance_kind TEXT NOT NULL,
                provenance_ref TEXT,
                content_hash TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                archived_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO tasks VALUES (
                'task-a', 'Task A', '', 'Goal A', '[]', 0, '[]',
                '{task_status}', 2, NULL, 'before', 'before'
            );
            INSERT INTO tasks VALUES (
                'task-b', 'Task B', '', 'Goal B', '[]', 0, '[]',
                'todo', 1, NULL, 'before', 'before'
            );
            INSERT INTO task_dependencies VALUES ('task-b', 'task-a', 'before');
            INSERT INTO sessions VALUES (
                'session-a', 'task-a', 'codex', 'native-a', NULL, NULL,
                '/tmp', 1, NULL, 'active', 1, 'before', 'before'
            );
            INSERT INTO capsules VALUES (
                'capsule-a', 'task-a', 'session-a', NULL, 'ready', 'hash-a',
                '/tmp/capsule-a', 1, NULL, 'before', 'before'
            );
            INSERT INTO memories VALUES (
                'memory-a', 'task-a', 'session-a', 'capsule-a', 'remember',
                'message', 'source-a', 'hash-m', 1, NULL, 'before', 'before'
            );
            PRAGMA user_version = 2;
            """
        )


def test_capture_project_state_is_bounded_to_explicit_nested_project(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    project = repo / "nested" / "project"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "demo@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Demo"], cwd=repo, check=True)
    (repo / "outside.txt").write_text("before\n", encoding="utf-8")
    (project / "inside.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)

    (repo / "outside.txt").write_text("outside change\n", encoding="utf-8")
    (repo / "outside-new.txt").write_text("outside new\n", encoding="utf-8")
    (project / "inside.txt").write_text("inside change\n", encoding="utf-8")
    (project / "inside-new.txt").write_text("inside new\n", encoding="utf-8")

    state = capture_project_state(project)

    assert state["is_git_repo"] is True
    assert set(state["dirty_files"]) == {"inside.txt", "inside-new.txt"}
    assert "outside" not in state["status_porcelain"]
    assert "outside" not in state["diff_stat"]
    assert "nested/project" not in "\n".join(state["dirty_files"])


def test_project_capture_keeps_dirty_symlink_path_inside_boundary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    project = repo / "project"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "demo@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Demo"], cwd=repo, check=True)
    link = project / "external-link"
    link.symlink_to(tmp_path / "outside-a")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    link.unlink()
    link.symlink_to(tmp_path / "outside-b")

    state = capture_project_state(project)

    assert state["dirty_files"] == ["external-link"]


def test_project_storage_initializes_idempotently_with_private_permissions(
    tmp_path: Path,
) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    project.mkdir()

    first = storage_module.initialize_project_storage(project)
    second = storage_module.initialize_project_storage(project)

    assert first.project_id == second.project_id
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", first.project_id)
    assert json.loads(first.project_file.read_text(encoding="utf-8"))["project_id"] == first.project_id
    assert _mode(project / ".offwork") == 0o700
    assert _mode(first.project_file) == 0o600
    assert _mode(first.database_path) == 0o600
    assert _mode(first.lock_path) == 0o600

    with first.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] > 0


@pytest.mark.parametrize(
    "fixed_path",
    [".offwork", "project.json", "state.sqlite3", "state.lock", "latest.json"],
)
def test_project_storage_rejects_symlinked_fixed_paths(
    tmp_path: Path, fixed_path: str
) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    if fixed_path == ".offwork":
        outside.mkdir()
        (project / ".offwork").symlink_to(outside, target_is_directory=True)
    else:
        offwork = project / ".offwork"
        offwork.mkdir()
        outside.write_text("outside", encoding="utf-8")
        (offwork / fixed_path).symlink_to(outside)

    with pytest.raises(storage_module.StorageInitializationError, match="symlink"):
        storage_module.initialize_project_storage(project)


def test_project_initialization_holds_process_lock(tmp_path: Path) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    project.mkdir()
    storage = storage_module.initialize_project_storage(project)
    descriptor = os.open(str(storage.lock_path), os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(storage_module.initialize_project_storage, project)
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.2)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            assert future.result(timeout=2).project_id == storage.project_id
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_concurrent_first_project_initialization_is_consistent(tmp_path: Path) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    project.mkdir()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                storage_module.initialize_project_storage,
                [project] * 8,
            )
        )

    assert len({result.project_id for result in results}) == 1
    with results[0].connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_project_storage_creates_v02_core_schema_and_indexes(tmp_path: Path) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    project.mkdir()
    storage = storage_module.initialize_project_storage(project)

    with storage.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assert {
            "tasks",
            "task_dependencies",
            "sessions",
            "capsules",
            "memories",
            "source_settings",
            "session_sources",
            "messages",
            "messages_fts",
        } <= tables

        assert {
            "task_id",
            "goal",
            "acceptance_commands_json",
            "auto_complete",
            "open_loops_json",
            "status",
            "revision",
            "archived_at",
        } <= _table_columns(connection, "tasks")
        assert {
            "managed_session_id",
            "task_id",
            "provider",
            "native_session_id",
            "tmux_socket",
            "tmux_name",
            "cwd",
            "is_primary",
            "parent_session_id",
            "state",
            "revision",
            "created_at",
            "updated_at",
        } <= _table_columns(connection, "sessions")
        assert {
            "capsule_id",
            "parent_capsule_id",
            "status",
            "content_hash",
        } <= _table_columns(connection, "capsules")
        assert {
            "memory_id",
            "provenance_kind",
            "provenance_ref",
            "content_hash",
        } <= _table_columns(connection, "memories")
        assert {
            "source_id",
            "managed_session_id",
            "task_id",
            "source_session_id",
            "source_path",
            "format_version",
            "source_fingerprint",
            "mtime_ns",
            "size_bytes",
            "read_offset",
            "checkpoint_hash",
            "state",
        } <= _table_columns(connection, "session_sources")
        source_columns = _column_info(connection, "session_sources")
        assert source_columns["managed_session_id"][3] == 0
        assert source_columns["task_id"][3] == 0
        message_columns = _column_info(connection, "messages")
        assert {"source_id", "source_message_id", "source_offset"} <= set(
            message_columns
        )
        assert message_columns["source_id"][3] == 1
        assert message_columns["source_message_id"][3] == 1

        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "sessions_provider_native_unique" in indexes
        assert "sessions_one_primary_per_task" in indexes

        fts_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'messages_fts'"
        ).fetchone()[0]
        assert "trigram" in fts_sql.lower()


def test_project_schema_rejects_task_status_outside_v02_states(tmp_path: Path) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    project.mkdir()
    storage = storage_module.initialize_project_storage(project)

    with storage.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks(task_id, title, status, created_at, updated_at) "
                "VALUES ('task-invalid', 'Invalid', 'active', 'now', 'now')"
            )


def test_v02_defaults_sources_can_index_before_attachment_and_delete_cascades(
    tmp_path: Path,
) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    project.mkdir()
    storage = storage_module.initialize_project_storage(project)

    with storage.connect() as connection:
        connection.execute(
            "INSERT INTO tasks(task_id, title, status, created_at, updated_at) "
            "VALUES ('task-a', 'Task A', 'in_progress', 'now', 'now')"
        )
        task = connection.execute(
            "SELECT goal, acceptance_commands_json, auto_complete, open_loops_json "
            "FROM tasks WHERE task_id = 'task-a'"
        ).fetchone()
        assert tuple(task) == ("", "[]", 0, "[]")

        connection.execute(
            "INSERT INTO source_settings(source_kind, created_at, updated_at) "
            "VALUES ('codex', 'now', 'now')"
        )
        assert connection.execute(
            "SELECT enabled FROM source_settings WHERE source_kind = 'codex'"
        ).fetchone()[0] == 0

        connection.execute(
            "INSERT INTO session_sources("
            "source_id, source_kind, source_path, format_version, source_fingerprint, "
            "mtime_ns, size_bytes, read_offset, state, created_at, updated_at"
            ") VALUES ('source-a', 'codex', '/tmp/session.jsonl', 1, 'fp-a', "
            "10, 20, 0, 'indexed', 'now', 'now')"
        )
        source = connection.execute(
            "SELECT managed_session_id, task_id FROM session_sources "
            "WHERE source_id = 'source-a'"
        ).fetchone()
        assert tuple(source) == (None, None)

        connection.execute(
            "INSERT INTO messages("
            "message_id, source_id, source_message_id, role, content, "
            "source_fingerprint, source_offset, created_at"
            ") VALUES ('message-a', 'source-a', 'offset:0', 'user', "
            "'restore workspace safely', 'fp-a', 0, 'now')"
        )
        assert connection.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'workspace'"
        ).fetchone()[0] == 1

        connection.execute("DELETE FROM session_sources WHERE source_id = 'source-a'")
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'workspace'"
        ).fetchone()[0] == 0


def test_project_schema_rejects_future_version_without_overwriting(
    tmp_path: Path,
) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    offwork = project / ".offwork"
    offwork.mkdir(parents=True)
    database = offwork / "state.sqlite3"
    with sqlite3.connect(str(database)) as connection:
        connection.execute("CREATE TABLE future_data(value TEXT)")
        connection.execute("INSERT INTO future_data VALUES ('keep')")
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(storage_module.StorageVersionError, match="999"):
        storage_module.initialize_project_storage(project)

    with sqlite3.connect(str(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999
        assert connection.execute("SELECT value FROM future_data").fetchone()[0] == "keep"
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'tasks'"
        ).fetchone()[0] == 0


def test_project_schema_migrates_legacy_v2_active_to_v3_with_references(
    tmp_path: Path,
) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    offwork = project / ".offwork"
    offwork.mkdir(parents=True)
    database = offwork / "state.sqlite3"
    _create_legacy_v2_database(database, "active")

    storage = storage_module.initialize_project_storage(project)

    with storage.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT status FROM tasks WHERE task_id = 'task-a'"
        ).fetchone()[0] == "in_progress"
        assert connection.execute(
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = 'task-b'"
        ).fetchone()[0] == "task-a"
        assert connection.execute(
            "SELECT task_id FROM sessions WHERE managed_session_id = 'session-a'"
        ).fetchone()[0] == "task-a"
        assert tuple(
            connection.execute(
                "SELECT task_id, managed_session_id FROM capsules "
                "WHERE capsule_id = 'capsule-a'"
            ).fetchone()
        ) == ("task-a", "session-a")
        assert tuple(
            connection.execute(
                "SELECT task_id, managed_session_id, capsule_id FROM memories "
                "WHERE memory_id = 'memory-a'"
            ).fetchone()
        ) == ("task-a", "session-a", "capsule-a")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE tasks SET status = 'active' WHERE task_id = 'task-a'"
            )


def test_project_schema_v2_unknown_status_rolls_back_entire_database(
    tmp_path: Path,
) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    offwork = project / ".offwork"
    offwork.mkdir(parents=True)
    database = offwork / "state.sqlite3"
    _create_legacy_v2_database(database, "unknown")

    with pytest.raises(
        storage_module.StorageInitializationError, match="unknown task status"
    ):
        storage_module.initialize_project_storage(project)

    with sqlite3.connect(str(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert tuple(
            connection.execute(
                "SELECT status, revision FROM tasks WHERE task_id = 'task-a'"
            ).fetchone()
        ) == ("unknown", 2)
        assert connection.execute(
            "SELECT count(*) FROM task_dependencies"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM capsules").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'tasks_v3'"
        ).fetchone()[0] == 0


def test_project_schema_migrates_v1_data_to_v3(tmp_path: Path) -> None:
    storage_module = _storage_module()
    legacy_candidate = "legacy:" + hashlib.sha256(b"session-a").hexdigest()
    project = tmp_path / "project"
    offwork = project / ".offwork"
    offwork.mkdir(parents=True)
    database = offwork / "state.sqlite3"
    with sqlite3.connect(str(database)) as connection:
        connection.executescript(
            f"""
            PRAGMA foreign_keys = ON;
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, archived_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                managed_session_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                provider TEXT NOT NULL, native_session_id TEXT,
                tmux_socket TEXT, tmux_name TEXT, cwd TEXT NOT NULL,
                is_primary INTEGER NOT NULL DEFAULT 0, parent_session_id TEXT,
                state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE source_settings (
                source_kind TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                settings_json TEXT NOT NULL DEFAULT '{{}}',
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE session_sources (
                session_source_id TEXT PRIMARY KEY,
                managed_session_id TEXT NOT NULL REFERENCES sessions(managed_session_id),
                source_kind TEXT NOT NULL, source_locator TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                last_offset INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1, archived_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                message_id TEXT PRIMARY KEY,
                managed_session_id TEXT NOT NULL REFERENCES sessions(managed_session_id),
                session_source_id TEXT REFERENCES session_sources(session_source_id),
                source_message_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL, source_offset INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, archived_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE capsules (
                capsule_id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(task_id),
                managed_session_id TEXT REFERENCES sessions(managed_session_id),
                parent_capsule_id TEXT REFERENCES capsules(capsule_id),
                status TEXT NOT NULL, content_hash TEXT NOT NULL,
                archive_path TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                archived_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE memories (
                memory_id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(task_id),
                managed_session_id TEXT REFERENCES sessions(managed_session_id),
                capsule_id TEXT REFERENCES capsules(capsule_id), content TEXT NOT NULL,
                provenance_kind TEXT NOT NULL, provenance_ref TEXT,
                content_hash TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                archived_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO tasks VALUES (
                'task-a', 'Task A', '', 'active', 1, NULL, 'now', 'now'
            );
                INSERT INTO sessions VALUES (
                    'session-a', 'task-a', 'codex', 'native-a', NULL, NULL,
                    '{project}', 1, NULL, 'active', 1, 'now', 'now'
                );
                INSERT INTO sessions VALUES (
                    'session-b', 'task-a', 'codex', 'legacy:session-a', NULL, NULL,
                    '{project}', 0, NULL, 'active', 1, 'now', 'now'
                );
            INSERT INTO source_settings VALUES ('codex', 1, '{{}}', 1, 'now', 'now');
            INSERT INTO session_sources VALUES (
                'source-a', 'session-a', 'codex', '/tmp/session.jsonl',
                'fp-a', 42, 1, NULL, 'now', 'now'
            );
            INSERT INTO session_sources VALUES (
                '{legacy_candidate}', 'session-b', 'legacy',
                '/tmp/legacy-collision.jsonl', 'fp-collision', 0, 1,
                NULL, 'now', 'now'
            );
            INSERT INTO messages VALUES (
                'message-a', 'session-a', 'source-a', NULL, 'user',
                'legacy workspace message', 'fp-a', 42, 1, NULL, 'now'
            );
            INSERT INTO messages VALUES (
                'message-orphan', 'session-a', NULL, NULL, 'assistant',
                'discard orphan marker', 'fp-orphan', 99, 1, NULL, 'now'
            );
            PRAGMA user_version = 1;
            """
        )

    bad_project = tmp_path / "bad-project"
    bad_offwork = bad_project / ".offwork"
    bad_offwork.mkdir(parents=True)
    bad_database = bad_offwork / "state.sqlite3"
    bad_database.write_bytes(database.read_bytes())
    with sqlite3.connect(str(bad_database)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "UPDATE messages SET session_source_id = 'missing-source' "
            "WHERE message_id = 'message-a'"
        )

    with pytest.raises(storage_module.StorageInitializationError, match="dangling"):
        storage_module.initialize_project_storage(bad_project)
    with sqlite3.connect(str(bad_database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "session_source_id" in {
            row[1] for row in connection.execute("PRAGMA table_info(messages)")
        }
        assert connection.execute(
            "SELECT count(*) FROM messages WHERE message_id = 'message-a'"
        ).fetchone()[0] == 1

    bad_status_project = tmp_path / "bad-status-project"
    bad_status_offwork = bad_status_project / ".offwork"
    bad_status_offwork.mkdir(parents=True)
    bad_status_database = bad_status_offwork / "state.sqlite3"
    bad_status_database.write_bytes(database.read_bytes())
    with sqlite3.connect(str(bad_status_database)) as connection:
        connection.execute(
            "UPDATE tasks SET status = 'unknown' WHERE task_id = 'task-a'"
        )

    with pytest.raises(
        storage_module.StorageInitializationError, match="unknown task status"
    ):
        storage_module.initialize_project_storage(bad_status_project)
    with sqlite3.connect(str(bad_status_database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM tasks WHERE task_id = 'task-a'"
        ).fetchone()[0] == "unknown"

    storage = storage_module.initialize_project_storage(project)

    with storage.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        task = connection.execute(
            "SELECT goal, acceptance_commands_json, auto_complete, open_loops_json, status "
            "FROM tasks WHERE task_id = 'task-a'"
        ).fetchone()
        assert tuple(task) == ("", "[]", 0, "[]", "in_progress")
        source = connection.execute(
            "SELECT source_id, task_id, source_path, read_offset "
            "FROM session_sources WHERE source_id = 'source-a'"
        ).fetchone()
        assert tuple(source) == ("source-a", "task-a", "/tmp/session.jsonl", 42)
        message = connection.execute(
            "SELECT source_id, source_message_id FROM messages "
            "WHERE message_id = 'message-a'"
        ).fetchone()
        assert tuple(message) == ("source-a", "offset:42:message-a")
        orphan = connection.execute(
            "SELECT messages.source_id, messages.source_message_id, messages.role, "
            "messages.content, session_sources.source_kind, "
            "session_sources.source_session_id, session_sources.source_path, "
            "session_sources.state "
            "FROM messages JOIN session_sources USING (source_id) "
            "WHERE messages.message_id = 'message-orphan'"
        ).fetchone()
        assert tuple(orphan) == (
            legacy_candidate + ":1",
            "offset:99:message-orphan",
            "assistant",
            "discard orphan marker",
            "legacy",
            "legacy:session-a:1",
            "legacy://session-a",
            "active",
        )
        collision_source = connection.execute(
            "SELECT source_id, source_session_id, source_path FROM session_sources "
            "WHERE source_id = ?",
            (legacy_candidate,),
        ).fetchone()
        assert tuple(collision_source) == (
            legacy_candidate,
            "legacy:session-a",
            "/tmp/legacy-collision.jsonl",
        )
        assert connection.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'workspace'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'orphan'"
        ).fetchone()[0] == 1

    state_module = importlib.import_module("offwork_capsule.state")
    service = state_module.StateService(project)
    shown = service.show_task("task-a")
    listed = service.list_tasks()[0]
    status = service.project_status()
    assert shown["status"] == listed["status"] == "in_progress"
    assert shown["computed_state"] == listed["computed_state"] == "actionable"
    assert status["current_focus"]["task_id"] == "task-a"
    assert status["current_focus"]["status"] == "in_progress"


@pytest.mark.parametrize("table", ["capsules", "memories"])
def test_capsule_and_memory_reject_session_task_mismatch(
    tmp_path: Path, table: str
) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    project.mkdir()
    storage = storage_module.initialize_project_storage(project)
    with storage.connect() as connection:
        connection.executemany(
            "INSERT INTO tasks(task_id, title, status, created_at, updated_at) "
            "VALUES (?, ?, 'in_progress', 'now', 'now')",
            [("task-a", "Task A"), ("task-b", "Task B")],
        )
        connection.execute(
            "INSERT INTO sessions("
            "managed_session_id, task_id, provider, cwd, state, created_at, updated_at"
            ") VALUES ('session-a', 'task-a', 'codex', '/tmp', 'active', 'now', 'now')"
        )

        if table == "capsules":
            statement = (
                "INSERT INTO capsules("
                "capsule_id, task_id, managed_session_id, status, content_hash, "
                "archive_path, created_at, updated_at"
                ") VALUES ('capsule-a', 'task-b', 'session-a', 'validated', 'hash', "
                "'/tmp/capsule', 'now', 'now')"
            )
        else:
            statement = (
                "INSERT INTO memories("
                "memory_id, task_id, managed_session_id, content, provenance_kind, "
                "content_hash, created_at, updated_at"
                ") VALUES ('memory-a', 'task-b', 'session-a', 'memory', 'message', "
                "'hash', 'now', 'now')"
            )

        with pytest.raises(sqlite3.IntegrityError, match="task/session mismatch"):
            connection.execute(statement)


@pytest.mark.parametrize("table", ["capsules", "memories"])
def test_referenced_session_cannot_be_rebound_to_a_different_task(
    tmp_path: Path, table: str
) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    project.mkdir()
    storage = storage_module.initialize_project_storage(project)
    with storage.connect() as connection:
        connection.executemany(
            "INSERT INTO tasks(task_id, title, status, created_at, updated_at) "
            "VALUES (?, ?, 'in_progress', 'now', 'now')",
            [("task-a", "Task A"), ("task-b", "Task B")],
        )
        connection.execute(
            "INSERT INTO sessions("
            "managed_session_id, task_id, provider, cwd, state, created_at, updated_at"
            ") VALUES ('session-a', 'task-a', 'codex', '/tmp', 'active', 'now', 'now')"
        )
        if table == "capsules":
            connection.execute(
                "INSERT INTO capsules("
                "capsule_id, task_id, managed_session_id, status, content_hash, "
                "archive_path, created_at, updated_at"
                ") VALUES ('capsule-a', 'task-a', 'session-a', 'validated', 'hash', "
                "'/tmp/capsule', 'now', 'now')"
            )
        else:
            connection.execute(
                "INSERT INTO memories("
                "memory_id, task_id, managed_session_id, content, provenance_kind, "
                "content_hash, created_at, updated_at"
                ") VALUES ('memory-a', 'task-a', 'session-a', 'memory', 'message', "
                "'hash', 'now', 'now')"
            )

        with pytest.raises(sqlite3.IntegrityError, match="session task rebind mismatch"):
            connection.execute(
                "UPDATE sessions SET task_id = 'task-b' "
                "WHERE managed_session_id = 'session-a'"
            )


def test_project_storage_raises_explicit_error_without_fts5_trigram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    project.mkdir()

    def unavailable(connection: sqlite3.Connection) -> None:
        raise storage_module.StorageCapabilityError(
            "SQLite runtime requires FTS5 trigram support"
        )

    monkeypatch.setattr(storage_module, "_require_fts5_trigram", unavailable)

    with pytest.raises(storage_module.StorageCapabilityError, match="FTS5 trigram"):
        storage_module.initialize_project_storage(project)


def test_global_registry_uses_xdg_location_and_minimal_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_module = _storage_module()
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    registry = storage_module.initialize_global_registry()
    again = storage_module.initialize_global_registry()

    assert registry.database_path == data_home / "offwork" / "registry.sqlite3"
    assert again.database_path == registry.database_path
    assert _mode(registry.database_path.parent) == 0o700
    assert _mode(registry.database_path) == 0o600
    assert _mode(registry.lock_path) == 0o600
    with registry.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"projects", "task_summaries"} <= tables
        assert "tasks" not in tables
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "task_summaries_project_status" in indexes


def test_global_registry_migrates_v1_to_composite_task_identity(tmp_path: Path) -> None:
    storage_module = _storage_module()
    data_home = tmp_path / "xdg-data"
    registry_root = data_home / "offwork"
    registry_root.mkdir(parents=True)
    database = registry_root / "registry.sqlite3"
    with sqlite3.connect(str(database)) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY, canonical_path TEXT NOT NULL UNIQUE,
                state_database_path TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            CREATE TABLE task_summaries (
                task_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                title TEXT NOT NULL, status TEXT NOT NULL,
                revision INTEGER NOT NULL, archived_at TEXT, updated_at TEXT NOT NULL
            );
            CREATE INDEX task_summaries_project_status
                ON task_summaries(project_id, status, updated_at);
            INSERT INTO projects VALUES ('project-a', '/tmp/a', '/tmp/a/state', 'before');
            INSERT INTO task_summaries VALUES (
                'task-a', 'project-a', 'Task A', 'active', 2, NULL, 'before'
            );
            PRAGMA user_version = 1;
            """
        )

    registry = storage_module.initialize_global_registry(data_home)

    with registry.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert tuple(
            connection.execute(
                "SELECT project_id, task_id, title, status, revision "
                "FROM task_summaries"
            ).fetchone()
        ) == ("project-a", "task-a", "Task A", "active", 2)
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(task_summaries)")
        }
        assert columns["project_id"][5] == 1
        assert columns["task_id"][5] == 2


def test_capsule_ids_do_not_collide_within_the_same_second(tmp_path: Path) -> None:
    state = {"project_path": str(tmp_path), "is_git_repo": False}

    first = build_capsule(_context(), state, captured_at=NOW)
    second = build_capsule(_context(), state, captured_at=NOW)

    assert first["id"] != second["id"]
    assert re.fullmatch(r"20260826T020304Z-[0-9a-f]{32}", first["id"])


def test_archive_is_private_and_manifest_hashes_payloads(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    capsule = build_capsule(
        _context(), {"project_path": str(project), "is_git_repo": False}, NOW
    )

    archive_dir = archive_capsule(project, capsule, validate_for_restore(capsule))

    assert _mode(project / ".offwork") == 0o700
    assert _mode(project / ".offwork" / "capsules") == 0o700
    assert _mode(archive_dir) == 0o700
    manifest = json.loads((archive_dir / "manifest.json").read_text(encoding="utf-8"))
    for filename in ("capsule.json", "capsule.md", "restore-test.json"):
        payload = (archive_dir / filename).read_bytes()
        assert manifest["files"][filename]["sha256"] == hashlib.sha256(payload).hexdigest()
        assert _mode(archive_dir / filename) == 0o600
    assert _mode(archive_dir / "manifest.json") == 0o600
    assert _mode(project / ".offwork" / "latest.json") == 0o600


def test_archive_rejects_symlinked_capsules_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    offwork = project / ".offwork"
    offwork.mkdir()
    outside = tmp_path / "outside-capsules"
    outside.mkdir()
    (offwork / "capsules").symlink_to(outside, target_is_directory=True)
    capsule = build_capsule(
        _context(), {"project_path": str(project), "is_git_repo": False}, NOW
    )

    with pytest.raises(CapsuleValidationError, match="symlink"):
        archive_capsule(project, capsule, validate_for_restore(capsule))


def test_archive_rejects_symlinked_final_archive_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    capsule = build_capsule(
        _context(), {"project_path": str(project), "is_git_repo": False}, NOW
    )
    capsules = project / ".offwork" / "capsules"
    capsules.mkdir(parents=True)
    outside = tmp_path / "outside-archive"
    outside.mkdir()
    (capsules / capsule["id"]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(CapsuleValidationError, match="symlink"):
        archive_capsule(project, capsule, validate_for_restore(capsule))


def test_archive_holds_state_lock_for_capsule_and_latest_publish(tmp_path: Path) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    project.mkdir()
    storage = storage_module.initialize_project_storage(project)
    capsule = build_capsule(
        _context(), {"project_path": str(project), "is_git_repo": False}, NOW
    )
    descriptor = os.open(str(storage.lock_path), os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        archive_capsule, project, capsule, validate_for_restore(capsule)
    )
    try:
        with pytest.raises(FutureTimeoutError):
            future.result(timeout=0.2)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert future.result(timeout=2).name == capsule["id"]
    executor.shutdown()


def test_concurrent_capsule_publish_has_no_partial_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state = {"project_path": str(project), "is_git_repo": False}
    capsules = [build_capsule(_context(), state, NOW) for _ in range(8)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        archives = list(
            executor.map(
                lambda item: archive_capsule(
                    project, item, validate_for_restore(item)
                ),
                capsules,
            )
        )

    assert len({archive.name for archive in archives}) == 8
    assert not list((project / ".offwork" / "capsules").glob("*.staging-*"))
    assert load_latest_capsule(project)["id"] in {item["id"] for item in capsules}


def test_archive_failure_does_not_replace_latest_or_publish_partial_capsule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state = {"project_path": str(project), "is_git_repo": False}
    first = build_capsule(_context(), state, NOW)
    archive_capsule(project, first, validate_for_restore(first))
    latest_path = project / ".offwork" / "latest.json"
    previous_latest = latest_path.read_bytes()
    second = build_capsule(_context(), state, NOW)

    def fail_render(capsule: dict) -> str:
        raise RuntimeError("render failed")

    monkeypatch.setattr(capsule_module, "render_capsule_markdown", fail_render)

    with pytest.raises(RuntimeError, match="render failed"):
        archive_capsule(project, second, validate_for_restore(second))

    assert latest_path.read_bytes() == previous_latest
    assert not (project / ".offwork" / "capsules" / second["id"]).exists()
    assert not list((project / ".offwork" / "capsules").glob("*.staging-*"))


def test_archive_fsyncs_directories_in_durable_publish_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    capsule = build_capsule(
        _context(), {"project_path": str(project), "is_git_repo": False}, NOW
    )
    latest = project / ".offwork" / "latest.json"
    calls: list[tuple[Path, bool]] = []
    real_fsync_directory = capsule_module._fsync_directory

    def record(path: Path) -> None:
        calls.append((path, latest.exists()))
        real_fsync_directory(path)

    monkeypatch.setattr(capsule_module, "_fsync_directory", record)

    archive_capsule(project, capsule, validate_for_restore(capsule))

    assert calls[0][0].name.startswith(".%s.staging-" % capsule["id"])
    assert [path.name for path, _ in calls[1:]] == ["capsules", ".offwork"]
    assert [latest_exists for _, latest_exists in calls] == [False, False, True]


def test_archive_staging_fsync_failure_leaves_latest_and_final_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state = {"project_path": str(project), "is_git_repo": False}
    first = build_capsule(_context(), state, NOW)
    archive_capsule(project, first, validate_for_restore(first))
    latest = project / ".offwork" / "latest.json"
    previous_latest = latest.read_bytes()
    second = build_capsule(_context(), state, NOW)
    real_fsync_directory = capsule_module._fsync_directory

    def fail_staging(path: Path) -> None:
        if path.name.startswith(".%s.staging-" % second["id"]):
            raise OSError("staging fsync failed")
        real_fsync_directory(path)

    monkeypatch.setattr(capsule_module, "_fsync_directory", fail_staging)

    with pytest.raises(OSError, match="staging fsync failed"):
        archive_capsule(project, second, validate_for_restore(second))

    assert latest.read_bytes() == previous_latest
    assert not (project / ".offwork" / "capsules" / second["id"]).exists()


def test_archive_has_no_failing_chmod_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    capsule = build_capsule(
        _context(), {"project_path": str(project), "is_git_repo": False}, NOW
    )
    final_archive = project / ".offwork" / "capsules" / capsule["id"]
    latest = project / ".offwork" / "latest.json"
    real_chmod = Path.chmod

    def reject_post_publish(self: Path, mode: int, *args, **kwargs) -> None:
        if (self == final_archive or self == latest) and self.exists():
            raise OSError("post-publish chmod attempted")
        real_chmod(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", reject_post_publish)

    archive_dir = archive_capsule(project, capsule, validate_for_restore(capsule))

    assert archive_dir == final_archive
    assert json.loads(latest.read_text(encoding="utf-8"))["capsule_id"] == capsule["id"]


@pytest.mark.parametrize("tamper_target", ["capsule.json", "manifest.json"])
def test_load_latest_rejects_symlinked_new_capsule_files(
    tmp_path: Path, tamper_target: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    capsule = build_capsule(
        _context(), {"project_path": str(project), "is_git_repo": False}, NOW
    )
    archive = archive_capsule(project, capsule, validate_for_restore(capsule))
    target = archive / tamper_target
    original = target.read_bytes()
    outside = tmp_path / ("outside-" + tamper_target)
    outside.write_bytes(original)
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(CapsuleValidationError, match="symlink"):
        load_latest_capsule(project)


def test_load_latest_rejects_tampered_new_capsule_payload(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    capsule = build_capsule(
        _context(), {"project_path": str(project), "is_git_repo": False}, NOW
    )
    archive = archive_capsule(project, capsule, validate_for_restore(capsule))
    (archive / "capsule.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(CapsuleValidationError, match="hash"):
        load_latest_capsule(project)


def test_load_latest_rejects_symlinked_latest_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    offwork = project / ".offwork"
    offwork.mkdir(parents=True)
    outside = tmp_path / "outside-latest.json"
    outside.write_text('{"capsule_id":"20260825T175800Z"}', encoding="utf-8")
    (offwork / "latest.json").symlink_to(outside)

    with pytest.raises(CapsuleValidationError, match="symlink"):
        load_latest_capsule(project)


@pytest.mark.parametrize("capsule_id", ["../outside", "/tmp/outside", "bad-id"])
def test_load_latest_rejects_unsafe_capsule_id(
    tmp_path: Path, capsule_id: str
) -> None:
    project = tmp_path / "project"
    offwork = project / ".offwork"
    offwork.mkdir(parents=True)
    (offwork / "latest.json").write_text(
        json.dumps({"capsule_id": capsule_id}), encoding="utf-8"
    )

    with pytest.raises(CapsuleValidationError, match="capsule_id"):
        load_latest_capsule(project)


def test_load_latest_keeps_legacy_capsule_without_manifest_compatible(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    archive = project / ".offwork" / "capsules" / "20260825T175800Z"
    archive.mkdir(parents=True)
    legacy = {"schema_version": 1, "id": "20260825T175800Z", "goal": "legacy"}
    (archive / "capsule.json").write_text(json.dumps(legacy), encoding="utf-8")
    (project / ".offwork" / "latest.json").write_text(
        json.dumps({"capsule_id": legacy["id"]}), encoding="utf-8"
    )

    assert load_latest_capsule(project)["goal"] == "legacy"


@pytest.mark.parametrize("has_previous", [True, False])
def test_latest_directory_fsync_failure_restores_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_previous: bool
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state = {"project_path": str(project), "is_git_repo": False}
    latest = project / ".offwork" / "latest.json"
    previous_bytes = None
    if has_previous:
        previous = build_capsule(_context(), state, NOW)
        archive_capsule(project, previous, validate_for_restore(previous))
        previous_bytes = latest.read_bytes()

    capsule = build_capsule(_context(), state, NOW)
    real_fsync_directory = capsule_module._fsync_directory
    root_fsync_calls = 0

    def fail_first_latest_fsync(path: Path) -> None:
        nonlocal root_fsync_calls
        if path == project / ".offwork":
            root_fsync_calls += 1
            if root_fsync_calls == 1:
                raise OSError("latest directory fsync failed")
        real_fsync_directory(path)

    monkeypatch.setattr(capsule_module, "_fsync_directory", fail_first_latest_fsync)

    with pytest.raises(OSError, match="latest directory fsync failed"):
        archive_capsule(project, capsule, validate_for_restore(capsule))

    assert root_fsync_calls == 2
    if has_previous:
        assert latest.read_bytes() == previous_bytes
    else:
        assert not latest.exists()


def test_storage_initialization_preserves_existing_v01_capsule_content(
    tmp_path: Path,
) -> None:
    storage_module = _storage_module()
    project = tmp_path / "project"
    legacy = project / ".offwork" / "capsules" / "20260825T175800Z"
    legacy.mkdir(parents=True)
    legacy_payload = legacy / "capsule.json"
    original = b'{"schema_version": 1, "id": "20260825T175800Z"}\n'
    legacy_payload.write_bytes(original)
    latest_payload = project / ".offwork" / "latest.json"
    latest_original = b'{"capsule_id": "20260825T175800Z"}\n'
    latest_payload.write_bytes(latest_original)
    os.chmod(project / ".offwork", 0o755)
    os.chmod(legacy, 0o755)
    os.chmod(legacy_payload, 0o644)
    os.chmod(latest_payload, 0o644)

    storage_module.initialize_project_storage(project)

    assert legacy_payload.read_bytes() == original
    assert latest_payload.read_bytes() == latest_original
    assert _mode(project / ".offwork") == 0o700
    assert _mode(legacy) == 0o700
    assert _mode(legacy_payload) == 0o600
    assert _mode(latest_payload) == 0o600
