from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


CURRENT_PROJECT_SCHEMA_VERSION = 4
CURRENT_REGISTRY_SCHEMA_VERSION = 2
PROJECT_SCHEMA_VERSION = CURRENT_PROJECT_SCHEMA_VERSION
REGISTRY_SCHEMA_VERSION = CURRENT_REGISTRY_SCHEMA_VERSION
CAPSULE_SCHEMA_STATUSES = ("validated", "fresh_verified", "rejected")


class StorageCapabilityError(RuntimeError):
    """Raised when the local SQLite runtime lacks a required capability."""


class StorageInitializationError(RuntimeError):
    """Raised when existing storage metadata cannot be safely initialized."""


class StorageVersionError(StorageInitializationError):
    """Raised when a database schema version is newer or unsupported."""


def _no_follow() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))


def _path_stat(path: Path) -> Optional[os.stat_result]:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _require_regular(path: Path) -> os.stat_result:
    metadata = _path_stat(path)
    if metadata is None:
        raise StorageInitializationError("missing required file: %s" % path)
    if stat.S_ISLNK(metadata.st_mode):
        raise StorageInitializationError("refusing symlink: %s" % path)
    if not stat.S_ISREG(metadata.st_mode):
        raise StorageInitializationError("expected regular file: %s" % path)
    return metadata


def _open_regular(path: Path, flags: int, create: bool = False) -> int:
    open_flags = flags | _no_follow()
    if create:
        try:
            descriptor = os.open(
                str(path), open_flags | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            descriptor = os.open(str(path), open_flags)
    else:
        descriptor = os.open(str(path), open_flags)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise StorageInitializationError("expected regular file: %s" % path)
    return descriptor


def _read_regular_bytes(path: Path) -> bytes:
    _require_regular(path)
    descriptor = _open_regular(path, os.O_RDONLY)
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    descriptor = _open_regular(path, os.O_RDWR, create=True)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _private_directory(path: Path) -> None:
    metadata = _path_stat(path)
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode):
            raise StorageInitializationError("refusing symlink directory: %s" % path)
        if not stat.S_ISDIR(metadata.st_mode):
            raise StorageInitializationError("expected directory: %s" % path)
    else:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            metadata = _path_stat(path)
            if metadata is None or stat.S_ISLNK(metadata.st_mode):
                raise StorageInitializationError(
                    "refusing symlink directory: %s" % path
                )
            if not stat.S_ISDIR(metadata.st_mode):
                raise StorageInitializationError("expected directory: %s" % path)
    path.chmod(0o700)


def _private_file(path: Path) -> None:
    metadata = _path_stat(path)
    if metadata is not None and stat.S_ISLNK(metadata.st_mode):
        raise StorageInitializationError("refusing symlink file: %s" % path)
    descriptor = _open_regular(path, os.O_RDWR, create=True)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _repair_existing_capsule_permissions(offwork_root: Path) -> None:
    for path in offwork_root.iterdir():
        if path.suffix in {".json", ".sqlite3", ".lock"}:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise StorageInitializationError(
                    "refusing symlink file: %s" % path
                )
            if not stat.S_ISREG(metadata.st_mode):
                raise StorageInitializationError(
                    "expected regular file: %s" % path
                )
            path.chmod(0o600)

    capsules = offwork_root / "capsules"
    capsules_metadata = _path_stat(capsules)
    if capsules_metadata is None:
        return
    if stat.S_ISLNK(capsules_metadata.st_mode):
        raise StorageInitializationError("refusing symlink directory: %s" % capsules)
    if not stat.S_ISDIR(capsules_metadata.st_mode):
        raise StorageInitializationError("expected directory: %s" % capsules)
    capsules.chmod(0o700)
    for current_root, directory_names, file_names in os.walk(
        str(capsules), followlinks=False
    ):
        current = Path(current_root)
        current.chmod(0o700)
        for name in directory_names:
            directory = current / name
            metadata = directory.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise StorageInitializationError(
                    "refusing symlink capsule directory: %s" % directory
                )
            directory.chmod(0o700)
        for name in file_names:
            path = current / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise StorageInitializationError(
                    "refusing symlink capsule file: %s" % path
                )
            if not stat.S_ISREG(metadata.st_mode):
                raise StorageInitializationError(
                    "expected regular capsule file: %s" % path
                )
            path.chmod(0o600)


def _configure_connection(database_path: Path) -> sqlite3.Connection:
    before = _require_regular(database_path)
    connection = sqlite3.connect(str(database_path), timeout=5.0)
    try:
        after = _require_regular(database_path)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise StorageInitializationError("database path changed while opening")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        os.chmod(str(database_path), 0o600, follow_symlinks=False)
    except Exception:
        connection.close()
        raise
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database_path) + suffix)
        if sidecar.exists():
            sidecar.chmod(0o600)
    return connection


@dataclass(frozen=True)
class ProjectStorage:
    project_id: str
    project_root: Path
    offwork_root: Path
    project_file: Path
    database_path: Path
    lock_path: Path

    def connect(self) -> sqlite3.Connection:
        return _configure_connection(self.database_path)


@dataclass(frozen=True)
class RegistryStorage:
    database_path: Path
    lock_path: Path

    def connect(self) -> sqlite3.Connection:
        return _configure_connection(self.database_path)


def _read_or_create_project_id(project_file: Path) -> str:
    metadata = _path_stat(project_file)
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode):
            raise StorageInitializationError("refusing symlink file: %s" % project_file)
        _require_regular(project_file)
        os.chmod(str(project_file), 0o600, follow_symlinks=False)
        try:
            payload = json.loads(_read_regular_bytes(project_file).decode("utf-8"))
            project_id = str(payload["project_id"])
            uuid.UUID(project_id)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise StorageInitializationError(
                "invalid .offwork/project.json: expected a UUID project_id"
            ) from error
        return project_id

    project_id = str(uuid.uuid4())
    payload = json.dumps({"project_id": project_id}, indent=2) + "\n"
    try:
        descriptor = os.open(
            str(project_file),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow(),
            0o600,
        )
    except FileExistsError:
        return _read_or_create_project_id(project_file)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(str(project_file), 0o600, follow_symlinks=False)
    return project_id


def _require_fts5_trigram(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.__offwork_trigram_check "
            "USING fts5(content, tokenize='trigram')"
        )
        connection.execute("DROP TABLE temp.__offwork_trigram_check")
    except sqlite3.OperationalError as error:
        raise StorageCapabilityError(
            "SQLite runtime requires FTS5 trigram support for messages"
        ) from error


PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    acceptance_commands_json TEXT NOT NULL DEFAULT '[]'
        CHECK (
            json_valid(acceptance_commands_json)
            AND json_type(acceptance_commands_json) = 'array'
        ),
    auto_complete INTEGER NOT NULL DEFAULT 0 CHECK (auto_complete IN (0, 1)),
    open_loops_json TEXT NOT NULL DEFAULT '[]'
        CHECK (
            json_valid(open_loops_json)
            AND json_type(open_loops_json) = 'array'
        ),
    status TEXT NOT NULL CHECK (
        status IN ('todo', 'in_progress', 'review', 'complete')
    ),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id <> depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS task_auto_complete_config (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    require_fresh_verifier INTEGER NOT NULL DEFAULT 0
        CHECK (require_fresh_verifier IN (0, 1))
);

CREATE TABLE IF NOT EXISTS sessions (
    managed_session_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
    native_session_id TEXT,
    tmux_socket TEXT,
    tmux_name TEXT,
    cwd TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    parent_session_id TEXT REFERENCES sessions(managed_session_id) ON DELETE SET NULL,
    state TEXT NOT NULL CHECK (
        state IN ('attached', 'active', 'hibernated', 'stopped', 'failed')
    ),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS sessions_provider_native_unique
    ON sessions(provider, native_session_id)
    WHERE native_session_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_primary_per_task
    ON sessions(task_id)
    WHERE is_primary = 1;
CREATE UNIQUE INDEX IF NOT EXISTS sessions_tmux_handle_unique
    ON sessions(tmux_socket, tmux_name)
    WHERE tmux_socket IS NOT NULL AND tmux_name IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS sessions_id_task_unique
    ON sessions(managed_session_id, task_id);
CREATE INDEX IF NOT EXISTS sessions_task_state ON sessions(task_id, state);

CREATE TABLE IF NOT EXISTS capsules (
    capsule_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    managed_session_id TEXT REFERENCES sessions(managed_session_id) ON DELETE SET NULL,
    parent_capsule_id TEXT REFERENCES capsules(capsule_id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (
        status IN ('validated', 'fresh_verified', 'rejected')
    ),
    content_hash TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS capsules_task_created ON capsules(task_id, created_at);
CREATE INDEX IF NOT EXISTS capsules_session_created
    ON capsules(managed_session_id, created_at);

CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    managed_session_id TEXT REFERENCES sessions(managed_session_id) ON DELETE SET NULL,
    capsule_id TEXT REFERENCES capsules(capsule_id) ON DELETE SET NULL,
    content TEXT NOT NULL,
    provenance_kind TEXT NOT NULL,
    provenance_ref TEXT,
    content_hash TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_task_created ON memories(task_id, created_at);

CREATE TABLE IF NOT EXISTS source_settings (
    source_kind TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    settings_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_sources (
    source_id TEXT PRIMARY KEY,
    managed_session_id TEXT
        REFERENCES sessions(managed_session_id) ON DELETE SET NULL,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    source_session_id TEXT,
    source_kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    format_version INTEGER NOT NULL CHECK (format_version > 0),
    source_fingerprint TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    read_offset INTEGER NOT NULL DEFAULT 0 CHECK (read_offset >= 0),
    checkpoint_hash TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (managed_session_id, task_id)
        REFERENCES sessions(managed_session_id, task_id)
);
CREATE INDEX IF NOT EXISTS session_sources_fingerprint
    ON session_sources(source_fingerprint);
CREATE INDEX IF NOT EXISTS session_sources_attachment
    ON session_sources(task_id, managed_session_id);
CREATE UNIQUE INDEX IF NOT EXISTS session_sources_kind_session_unique
    ON session_sources(source_kind, source_session_id)
    WHERE source_session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL
        REFERENCES session_sources(source_id) ON DELETE CASCADE,
    source_message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    source_offset INTEGER NOT NULL CHECK (source_offset >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS messages_session_offset
    ON messages(source_id, source_offset);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='rowid',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS capsules_task_session_insert
BEFORE INSERT ON capsules
WHEN new.task_id IS NOT NULL
    AND new.managed_session_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM sessions
        WHERE managed_session_id = new.managed_session_id
            AND task_id = new.task_id
    )
BEGIN
    SELECT RAISE(ABORT, 'capsule task/session mismatch');
END;
CREATE TRIGGER IF NOT EXISTS capsules_task_session_update
BEFORE UPDATE OF task_id, managed_session_id ON capsules
WHEN new.task_id IS NOT NULL
    AND new.managed_session_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM sessions
        WHERE managed_session_id = new.managed_session_id
            AND task_id = new.task_id
    )
BEGIN
    SELECT RAISE(ABORT, 'capsule task/session mismatch');
END;
CREATE TRIGGER IF NOT EXISTS memories_task_session_insert
BEFORE INSERT ON memories
WHEN new.task_id IS NOT NULL
    AND new.managed_session_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM sessions
        WHERE managed_session_id = new.managed_session_id
            AND task_id = new.task_id
    )
BEGIN
    SELECT RAISE(ABORT, 'memory task/session mismatch');
END;
CREATE TRIGGER IF NOT EXISTS memories_task_session_update
BEFORE UPDATE OF task_id, managed_session_id ON memories
WHEN new.task_id IS NOT NULL
    AND new.managed_session_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM sessions
        WHERE managed_session_id = new.managed_session_id
            AND task_id = new.task_id
    )
BEGIN
    SELECT RAISE(ABORT, 'memory task/session mismatch');
END;
CREATE TRIGGER IF NOT EXISTS sessions_task_rebind_guard
BEFORE UPDATE OF task_id ON sessions
WHEN old.task_id <> new.task_id
    AND (
        EXISTS (
            SELECT 1 FROM capsules
            WHERE managed_session_id = old.managed_session_id
                AND task_id IS NOT NULL
                AND task_id <> new.task_id
        )
        OR EXISTS (
            SELECT 1 FROM memories
            WHERE managed_session_id = old.managed_session_id
                AND task_id IS NOT NULL
                AND task_id <> new.task_id
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'session task rebind mismatch');
END;
CREATE TRIGGER IF NOT EXISTS sessions_parent_task_insert_guard
BEFORE INSERT ON sessions
WHEN new.parent_session_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM sessions AS parent
        WHERE parent.managed_session_id = new.parent_session_id
            AND parent.task_id = new.task_id
    )
BEGIN
    SELECT RAISE(ABORT, 'session parent task mismatch');
END;
CREATE TRIGGER IF NOT EXISTS sessions_parent_task_update_guard
BEFORE UPDATE OF parent_session_id, task_id ON sessions
WHEN new.parent_session_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM sessions AS parent
        WHERE parent.managed_session_id = new.parent_session_id
            AND parent.task_id = new.task_id
    )
BEGIN
    SELECT RAISE(ABORT, 'session parent task mismatch');
END;
CREATE TRIGGER IF NOT EXISTS sessions_parent_cycle_guard
BEFORE UPDATE OF parent_session_id ON sessions
WHEN new.parent_session_id IS NOT NULL
    AND (
        new.parent_session_id = new.managed_session_id
        OR EXISTS (
            WITH RECURSIVE ancestors(managed_session_id) AS (
                SELECT new.parent_session_id
                UNION
                SELECT sessions.parent_session_id
                FROM sessions
                JOIN ancestors
                    ON sessions.managed_session_id = ancestors.managed_session_id
                WHERE sessions.parent_session_id IS NOT NULL
            )
            SELECT 1 FROM ancestors
            WHERE managed_session_id = new.managed_session_id
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'session parent cycle');
END;
"""


PROJECT_MIGRATION_1_TO_2 = """
CREATE TABLE tasks_v2 (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    acceptance_commands_json TEXT NOT NULL DEFAULT '[]'
        CHECK (
            json_valid(acceptance_commands_json)
            AND json_type(acceptance_commands_json) = 'array'
        ),
    auto_complete INTEGER NOT NULL DEFAULT 0 CHECK (auto_complete IN (0, 1)),
    open_loops_json TEXT NOT NULL DEFAULT '[]'
        CHECK (
            json_valid(open_loops_json)
            AND json_type(open_loops_json) = 'array'
        ),
    status TEXT NOT NULL CHECK (
        status IN ('todo', 'in_progress', 'review', 'complete')
    ),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO tasks_v2(
    task_id, title, description, goal, acceptance_commands_json,
    auto_complete, open_loops_json, status, revision, archived_at,
    created_at, updated_at
)
SELECT
    task_id, title, description, '', '[]', 0, '[]',
    CASE WHEN status = 'active' THEN 'in_progress' ELSE status END,
    revision, archived_at, created_at, updated_at
FROM tasks;
DROP TABLE tasks;
ALTER TABLE tasks_v2 RENAME TO tasks;

DROP TRIGGER IF EXISTS messages_fts_insert;
DROP TRIGGER IF EXISTS messages_fts_delete;
DROP TRIGGER IF EXISTS messages_fts_update;
DROP TABLE IF EXISTS messages_fts;
DROP INDEX IF EXISTS messages_session_offset;
DROP INDEX IF EXISTS session_sources_fingerprint;

ALTER TABLE source_settings RENAME TO source_settings_v1;
ALTER TABLE session_sources RENAME TO session_sources_v1;
ALTER TABLE messages RENAME TO messages_v1;

CREATE TABLE source_settings (
    source_kind TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    settings_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO source_settings(
    source_kind, enabled, settings_json, revision, created_at, updated_at
)
SELECT source_kind, enabled, settings_json, revision, created_at, updated_at
FROM source_settings_v1;

CREATE TABLE session_sources (
    source_id TEXT PRIMARY KEY,
    managed_session_id TEXT
        REFERENCES sessions(managed_session_id) ON DELETE SET NULL,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    source_session_id TEXT,
    source_kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    format_version INTEGER NOT NULL CHECK (format_version > 0),
    source_fingerprint TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    read_offset INTEGER NOT NULL DEFAULT 0 CHECK (read_offset >= 0),
    checkpoint_hash TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (managed_session_id, task_id)
        REFERENCES sessions(managed_session_id, task_id)
);
INSERT INTO session_sources(
    source_id, managed_session_id, task_id, source_session_id,
    source_kind, source_path, format_version, source_fingerprint,
    mtime_ns, size_bytes, read_offset, checkpoint_hash, state,
    revision, archived_at, created_at, updated_at
)
SELECT
    old.session_source_id,
    old.managed_session_id,
    sessions.task_id,
    sessions.native_session_id,
    old.source_kind,
    old.source_locator,
    1,
    old.source_fingerprint,
    0,
    0,
    old.last_offset,
    NULL,
    CASE WHEN old.archived_at IS NULL THEN 'indexed' ELSE 'archived' END,
    old.revision,
    old.archived_at,
    old.created_at,
    old.updated_at
FROM session_sources_v1 AS old
JOIN sessions ON sessions.managed_session_id = old.managed_session_id;

INSERT INTO session_sources(
    source_id, managed_session_id, task_id, source_session_id,
    source_kind, source_path, format_version, source_fingerprint,
    mtime_ns, size_bytes, read_offset, checkpoint_hash, state,
    revision, archived_at, created_at, updated_at
)
SELECT
    legacy_map.source_id,
    messages_v1.managed_session_id,
    sessions.task_id,
    legacy_map.source_session_id,
    'legacy',
    'legacy://' || messages_v1.managed_session_id,
    1,
    legacy_map.source_id,
    0,
    0,
    MAX(messages_v1.source_offset),
    NULL,
    'active',
    1,
    NULL,
    MIN(messages_v1.created_at),
    MAX(messages_v1.created_at)
FROM messages_v1
JOIN sessions
    ON sessions.managed_session_id = messages_v1.managed_session_id
JOIN temp.__offwork_legacy_source_map AS legacy_map
    ON legacy_map.managed_session_id = messages_v1.managed_session_id
WHERE messages_v1.session_source_id IS NULL
GROUP BY
    messages_v1.managed_session_id,
    sessions.task_id,
    legacy_map.source_id,
    legacy_map.source_session_id;

CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL
        REFERENCES session_sources(source_id) ON DELETE CASCADE,
    source_message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    source_offset INTEGER NOT NULL CHECK (source_offset >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_id, source_message_id)
);
INSERT INTO messages(
    message_id, source_id, source_message_id, role, content,
    source_fingerprint, source_offset, revision, archived_at, created_at
)
SELECT
    message_id,
    COALESCE(
        session_source_id,
        (
            SELECT source_id FROM temp.__offwork_legacy_source_map
            WHERE managed_session_id = messages_v1.managed_session_id
        )
    ),
    COALESCE(
        source_message_id,
        'offset:' || CAST(source_offset AS TEXT) || ':' || message_id
    ),
    role,
    content,
    source_fingerprint,
    source_offset,
    revision,
    archived_at,
    created_at
FROM messages_v1
WHERE EXISTS (
        SELECT 1 FROM session_sources
        WHERE source_id = COALESCE(
            messages_v1.session_source_id,
            (
                SELECT source_id FROM temp.__offwork_legacy_source_map
                WHERE managed_session_id = messages_v1.managed_session_id
            )
        )
    );

DROP TABLE messages_v1;
DROP TABLE session_sources_v1;
DROP TABLE source_settings_v1;
"""


PROJECT_MIGRATION_2_TO_3 = """
CREATE TABLE tasks_v3 (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    acceptance_commands_json TEXT NOT NULL DEFAULT '[]'
        CHECK (
            json_valid(acceptance_commands_json)
            AND json_type(acceptance_commands_json) = 'array'
        ),
    auto_complete INTEGER NOT NULL DEFAULT 0 CHECK (auto_complete IN (0, 1)),
    open_loops_json TEXT NOT NULL DEFAULT '[]'
        CHECK (
            json_valid(open_loops_json)
            AND json_type(open_loops_json) = 'array'
        ),
    status TEXT NOT NULL CHECK (
        status IN ('todo', 'in_progress', 'review', 'complete')
    ),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO tasks_v3(
    task_id, title, description, goal, acceptance_commands_json,
    auto_complete, open_loops_json, status, revision, archived_at,
    created_at, updated_at
)
SELECT
    task_id, title, description, goal, acceptance_commands_json,
    auto_complete, open_loops_json,
    CASE WHEN status = 'active' THEN 'in_progress' ELSE status END,
    revision, archived_at, created_at, updated_at
FROM tasks;
DROP TABLE tasks;
ALTER TABLE tasks_v3 RENAME TO tasks;
"""


PROJECT_MIGRATION_3_TO_4 = """
CREATE TABLE IF NOT EXISTS task_auto_complete_config (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    require_fresh_verifier INTEGER NOT NULL DEFAULT 0
        CHECK (require_fresh_verifier IN (0, 1))
);

CREATE TABLE IF NOT EXISTS capsules (
    capsule_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    managed_session_id TEXT REFERENCES sessions(managed_session_id) ON DELETE SET NULL,
    parent_capsule_id TEXT REFERENCES capsules(capsule_id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

DROP INDEX IF EXISTS capsules_task_created;
DROP INDEX IF EXISTS capsules_session_created;
CREATE TABLE capsules_v4 (
    capsule_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    managed_session_id TEXT REFERENCES sessions(managed_session_id) ON DELETE SET NULL,
    parent_capsule_id TEXT REFERENCES capsules_v4(capsule_id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (
        status IN ('validated', 'fresh_verified', 'rejected')
    ),
    content_hash TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO capsules_v4(
    capsule_id, task_id, managed_session_id, parent_capsule_id,
    status, content_hash, archive_path, revision, archived_at,
    created_at, updated_at
)
SELECT
    capsule_id, task_id, managed_session_id, parent_capsule_id,
    CASE WHEN status = 'ready' THEN 'validated' ELSE status END,
    content_hash, archive_path, revision, archived_at,
    created_at, updated_at
FROM capsules;
DROP TABLE capsules;
ALTER TABLE capsules_v4 RENAME TO capsules;
"""


REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE,
    state_database_path TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_summaries (
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    archived_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, task_id)
);
CREATE INDEX IF NOT EXISTS task_summaries_project_status
    ON task_summaries(project_id, status, updated_at);
"""


REGISTRY_MIGRATION_1_TO_2 = """
DROP INDEX IF EXISTS task_summaries_project_status;
CREATE TABLE task_summaries_v2 (
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    archived_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, task_id)
);
INSERT INTO task_summaries_v2(
    task_id, project_id, title, status, revision, archived_at, updated_at
)
SELECT task_id, project_id, title, status, revision, archived_at, updated_at
FROM task_summaries;
DROP TABLE task_summaries;
ALTER TABLE task_summaries_v2 RENAME TO task_summaries;
"""


def _schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _apply_schema_transaction(
    connection: sqlite3.Connection,
    script: str,
    version: int,
    check_foreign_keys: bool = False,
) -> None:
    try:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        pending = ""
        for line in script.splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                connection.execute(pending)
                pending = ""
        if pending.strip():
            raise StorageInitializationError("incomplete schema migration statement")
        if check_foreign_keys:
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise StorageInitializationError(
                    "schema migration produced foreign key violations"
                )
        connection.execute("PRAGMA user_version = %d" % version)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _validate_v1_migration(connection: sqlite3.Connection) -> None:
    checks = (
        (
            "session task",
            "SELECT 1 FROM sessions LEFT JOIN tasks USING (task_id) "
            "WHERE tasks.task_id IS NULL LIMIT 1",
        ),
        (
            "source session",
            "SELECT 1 FROM session_sources "
            "LEFT JOIN sessions USING (managed_session_id) "
            "WHERE sessions.managed_session_id IS NULL LIMIT 1",
        ),
        (
            "message session",
            "SELECT 1 FROM messages LEFT JOIN sessions USING (managed_session_id) "
            "WHERE sessions.managed_session_id IS NULL LIMIT 1",
        ),
        (
            "message source",
            "SELECT 1 FROM messages LEFT JOIN session_sources "
            "ON session_sources.session_source_id = messages.session_source_id "
            "WHERE messages.session_source_id IS NOT NULL "
            "AND session_sources.session_source_id IS NULL LIMIT 1",
        ),
        (
            "message/source session",
            "SELECT 1 FROM messages JOIN session_sources "
            "ON session_sources.session_source_id = messages.session_source_id "
            "WHERE messages.session_source_id IS NOT NULL "
            "AND messages.managed_session_id <> session_sources.managed_session_id LIMIT 1",
        ),
    )
    for label, statement in checks:
        if connection.execute(statement).fetchone() is not None:
            raise StorageInitializationError(
                "dangling v1 %s association; migration rolled back" % label
            )
    unknown_status = connection.execute(
        "SELECT status FROM tasks WHERE status NOT IN "
        "('active', 'todo', 'in_progress', 'review', 'complete') LIMIT 1"
    ).fetchone()
    if unknown_status is not None:
        raise StorageInitializationError(
            "unknown task status in v1 data: %s" % unknown_status[0]
        )


def _validate_v2_migration(connection: sqlite3.Connection) -> None:
    unknown_status = connection.execute(
        "SELECT status FROM tasks WHERE status NOT IN "
        "('active', 'todo', 'in_progress', 'review', 'complete') LIMIT 1"
    ).fetchone()
    if unknown_status is not None:
        raise StorageInitializationError(
            "unknown task status in v2 data: %s" % unknown_status[0]
        )


def _validate_v4_schema(connection: sqlite3.Connection) -> None:
    required = {"tasks", "task_auto_complete_config", "capsules"}
    present = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = sorted(required - present)
    if missing:
        raise StorageInitializationError(
            "incomplete v4 schema: missing " + ", ".join(missing)
        )
    capsules_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'capsules'"
    ).fetchone()
    capsules_sql = str(capsules_sql_row[0] or "") if capsules_sql_row else ""
    if not all(status in capsules_sql for status in CAPSULE_SCHEMA_STATUSES):
        raise StorageInitializationError(
            "incomplete v4 capsules status constraint"
        )


def _available_identifier(base: str, occupied: set[str]) -> str:
    candidate = base
    suffix = 1
    while candidate in occupied:
        candidate = "%s:%d" % (base, suffix)
        suffix += 1
    occupied.add(candidate)
    return candidate


def _prepare_legacy_source_map(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS temp.__offwork_legacy_source_map")
    connection.execute(
        "CREATE TEMP TABLE __offwork_legacy_source_map("
        "managed_session_id TEXT PRIMARY KEY, "
        "source_id TEXT NOT NULL UNIQUE, "
        "source_session_id TEXT NOT NULL UNIQUE)"
    )
    orphan_sessions = [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT managed_session_id FROM messages "
            "WHERE session_source_id IS NULL ORDER BY managed_session_id"
        )
    ]
    occupied_source_ids = {
        str(row[0]) for row in connection.execute("SELECT session_source_id FROM session_sources")
    }
    occupied_legacy_session_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT sessions.native_session_id "
            "FROM session_sources "
            "JOIN sessions USING (managed_session_id) "
            "WHERE session_sources.source_kind = 'legacy' "
            "AND sessions.native_session_id IS NOT NULL"
        )
    }

    for managed_session_id in orphan_sessions:
        digest = hashlib.sha256(managed_session_id.encode("utf-8")).hexdigest()
        source_id = _available_identifier(
            "legacy:" + digest, occupied_source_ids
        )
        source_session_id = _available_identifier(
            "legacy:" + managed_session_id, occupied_legacy_session_ids
        )
        connection.execute(
            "INSERT INTO temp.__offwork_legacy_source_map("
            "managed_session_id, source_id, source_session_id) VALUES (?, ?, ?)",
            (managed_session_id, source_id, source_session_id),
        )


def _initialize_project_schema(connection: sqlite3.Connection) -> None:
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = _schema_version(connection)
        if version > CURRENT_PROJECT_SCHEMA_VERSION:
            raise StorageVersionError(
                "state.sqlite3 schema version %d is newer than supported version %d"
                % (version, CURRENT_PROJECT_SCHEMA_VERSION)
            )
        if version not in (0, 1, 2, 3, CURRENT_PROJECT_SCHEMA_VERSION):
            raise StorageVersionError(
                "state.sqlite3 schema version %d is not supported" % version
            )

        _require_fts5_trigram(connection)
        if version == 0:
            _apply_schema_transaction(
                connection, PROJECT_SCHEMA, CURRENT_PROJECT_SCHEMA_VERSION
            )
            return
        if version == 1:
            _validate_v1_migration(connection)
            _prepare_legacy_source_map(connection)
            migration = (
                PROJECT_MIGRATION_1_TO_2
                + "\n"
                + PROJECT_MIGRATION_2_TO_3
                + "\n"
                + PROJECT_MIGRATION_3_TO_4
                + "\n"
                + PROJECT_SCHEMA
                + "\nINSERT INTO messages_fts(messages_fts) VALUES ('rebuild');\n"
            )
            _apply_schema_transaction(
                connection,
                migration,
                CURRENT_PROJECT_SCHEMA_VERSION,
                check_foreign_keys=True,
            )
            return
        if version == 2:
            _validate_v2_migration(connection)
            migration = (
                PROJECT_MIGRATION_2_TO_3
                + "\n"
                + PROJECT_MIGRATION_3_TO_4
                + "\n"
                + PROJECT_SCHEMA
            )
            _apply_schema_transaction(
                connection,
                migration,
                CURRENT_PROJECT_SCHEMA_VERSION,
                check_foreign_keys=True,
            )
            return

        if version == 3:
            migration = PROJECT_MIGRATION_3_TO_4 + "\n" + PROJECT_SCHEMA
            _apply_schema_transaction(
                connection,
                migration,
                CURRENT_PROJECT_SCHEMA_VERSION,
                check_foreign_keys=True,
            )
            return

        _validate_v4_schema(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("DROP TABLE IF EXISTS temp.__offwork_legacy_source_map")
        connection.execute("PRAGMA foreign_keys = ON")


def _initialize_registry_schema(connection: sqlite3.Connection) -> None:
    connection.commit()
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = _schema_version(connection)
        if version > CURRENT_REGISTRY_SCHEMA_VERSION:
            raise StorageVersionError(
                "registry.sqlite3 schema version %d is newer than supported version %d"
                % (version, CURRENT_REGISTRY_SCHEMA_VERSION)
            )
        if version not in (0, 1, CURRENT_REGISTRY_SCHEMA_VERSION):
            raise StorageVersionError(
                "registry.sqlite3 schema version %d is not supported" % version
            )
        if version == 1:
            _apply_schema_transaction(
                connection,
                REGISTRY_MIGRATION_1_TO_2 + "\n" + REGISTRY_SCHEMA,
                CURRENT_REGISTRY_SCHEMA_VERSION,
                check_foreign_keys=True,
            )
            return
        _apply_schema_transaction(
            connection, REGISTRY_SCHEMA, CURRENT_REGISTRY_SCHEMA_VERSION
        )
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def initialize_project_storage(project_root: Path) -> ProjectStorage:
    project = Path(project_root).resolve()
    offwork_root = project / ".offwork"
    _private_directory(offwork_root)

    project_file = offwork_root / "project.json"
    database_path = offwork_root / "state.sqlite3"
    lock_path = offwork_root / "state.lock"
    _private_file(lock_path)
    with _exclusive_file_lock(lock_path):
        _private_directory(offwork_root)
        _repair_existing_capsule_permissions(offwork_root)
        project_id = _read_or_create_project_id(project_file)
        _private_file(database_path)
        connection = _configure_connection(database_path)
        try:
            _initialize_project_schema(connection)
        finally:
            connection.close()
        _require_regular(database_path)

    return ProjectStorage(
        project_id=project_id,
        project_root=project,
        offwork_root=offwork_root,
        project_file=project_file,
        database_path=database_path,
        lock_path=lock_path,
    )


def initialize_global_registry(data_home: Optional[Path] = None) -> RegistryStorage:
    if data_home is None:
        configured = os.environ.get("XDG_DATA_HOME", "").strip()
        base = Path(configured).expanduser() if configured else Path.home() / ".local/share"
    else:
        base = Path(data_home).expanduser()
    registry_root = base.resolve() / "offwork"
    _private_directory(registry_root)
    database_path = registry_root / "registry.sqlite3"
    lock_path = registry_root / "registry.lock"
    _private_file(lock_path)
    with _exclusive_file_lock(lock_path):
        _private_directory(registry_root)
        _private_file(database_path)
        connection = _configure_connection(database_path)
        try:
            _initialize_registry_schema(connection)
        finally:
            connection.close()
        _require_regular(database_path)

    return RegistryStorage(database_path=database_path, lock_path=lock_path)
