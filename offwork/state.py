from __future__ import annotations

import fcntl
import os
import sqlite3
import json
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from offwork.errors import OffworkError


SCHEMA_VERSION = 3
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
SQLITE_AUXILIARY_SUFFIXES = ("-journal", "-wal", "-shm")


def validate_private_path(
    path: Path,
    *,
    expected_type: str,
    expected_mode: int,
    required: bool = True,
) -> os.stat_result | None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        if not required:
            return None
        raise OffworkError(
            "UNSAFE_STATE_PATH",
            "Required Offwork state path is missing",
            details={"path": str(path)},
        )
    except OSError as exc:
        raise OffworkError(
            "UNSAFE_STATE_PATH",
            "Offwork state path cannot be inspected safely",
            details={"path": str(path)},
        ) from exc

    type_matches = (
        stat.S_ISDIR(current.st_mode)
        if expected_type == "directory"
        else stat.S_ISREG(current.st_mode)
    )
    if (
        not type_matches
        or stat.S_ISLNK(current.st_mode)
        or current.st_uid != os.getuid()
        or stat.S_IMODE(current.st_mode) != expected_mode
    ):
        raise OffworkError(
            "UNSAFE_STATE_PATH",
            "Existing Offwork state path has unsafe type, owner, or permissions",
            details={"path": str(path)},
        )
    return current


def validate_database_paths(path: Path, *, required: bool = True) -> None:
    validate_private_path(
        path,
        expected_type="file",
        expected_mode=PRIVATE_FILE_MODE,
        required=required,
    )
    for suffix in SQLITE_AUXILIARY_SUFFIXES:
        validate_private_path(
            Path(f"{path}{suffix}"),
            expected_type="file",
            expected_mode=PRIVATE_FILE_MODE,
            required=False,
        )


@contextmanager
def state_lock(state_dir: Path):
    lock_path = state_dir / "state.lock"
    validate_private_path(
        lock_path,
        expected_type="file",
        expected_mode=PRIVATE_FILE_MODE,
    )
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    except OSError as exc:
        raise OffworkError(
            "UNSAFE_STATE_PATH",
            "Offwork state lock could not be opened safely",
            details={"path": str(lock_path)},
        ) from exc
    locked = False
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.getuid()
            or stat.S_IMODE(current.st_mode) != PRIVATE_FILE_MODE
        ):
            raise OffworkError(
                "UNSAFE_STATE_PATH",
                "Offwork state lock changed during validation",
                details={"path": str(lock_path)},
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    except OSError as exc:
        raise OffworkError(
            "STATE_LOCK_FAILED",
            "Offwork state lock could not be acquired safely",
            details={"path": str(lock_path)},
        ) from exc
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def create_private_directory(
    path: Path,
    mode: int = PRIVATE_DIRECTORY_MODE,
) -> None:
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise OffworkError(
            "UNSAFE_STATE_PATH",
            "Private directory parent could not be opened safely",
            details={"path": str(path)},
        ) from exc

    created = False
    try:
        os.mkdir(path.name, mode=mode, dir_fd=parent_descriptor)
        created = True
        os.chmod(
            path.name,
            mode,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )

        directory_descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        try:
            current = os.fstat(directory_descriptor)
            if (
                not stat.S_ISDIR(current.st_mode)
                or current.st_uid != os.getuid()
                or stat.S_IMODE(current.st_mode) != mode
            ):
                raise OffworkError(
                    "UNSAFE_STATE_PATH",
                    "New private directory has unsafe owner or permissions",
                    details={"path": str(path)},
                )
        finally:
            os.close(directory_descriptor)
    except (OSError, OffworkError) as exc:
        if created:
            try:
                os.rmdir(path.name, dir_fd=parent_descriptor)
            except OSError:
                pass
        if isinstance(exc, OffworkError):
            raise
        raise OffworkError(
            "UNSAFE_STATE_PATH",
            "Private directory could not be created safely",
            details={"path": str(path)},
        ) from exc
    finally:
        os.close(parent_descriptor)


def connect(path: Path) -> sqlite3.Connection:
    validate_database_paths(path)
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(path: Path) -> None:
    existing = validate_private_path(
        path,
        expected_type="file",
        expected_mode=PRIVATE_FILE_MODE,
        required=False,
    )
    is_new_database = existing is None
    if is_new_database:
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                PRIVATE_FILE_MODE,
            )
        except OSError as exc:
            raise OffworkError(
                "UNSAFE_STATE_PATH",
                "Offwork database could not be created safely",
                details={"path": str(path)},
            ) from exc
        try:
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        finally:
            os.close(descriptor)
    validate_database_paths(path)
    with connect(path) as connection:
        if not is_new_database:
            actual_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if actual_version != SCHEMA_VERSION:
                raise OffworkError(
                    "UNSUPPORTED_STATE_SCHEMA",
                    "Offwork database schema version is not supported",
                    details={
                        "actual_version": actual_version,
                        "supported_version": SCHEMA_VERSION,
                    },
                )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                goal TEXT NOT NULL,
                check_commands_json TEXT NOT NULL,
                revision INTEGER NOT NULL,
                current_capsule_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS human_acceptance_events (
                event_id TEXT PRIMARY KEY,
                capsule_id TEXT NOT NULL REFERENCES capsules(capsule_id),
                status TEXT NOT NULL CHECK(status IN ('accepted', 'rejected')),
                note TEXT,
                task_revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(capsule_id, task_revision)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS capsules (
                capsule_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                archive_path TEXT NOT NULL UNIQUE,
                manifest_hash TEXT NOT NULL,
                captured_task_revision INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        if is_new_database:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    validate_database_paths(path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "title": row["title"],
        "goal": row["goal"],
        "check_commands": json.loads(row["check_commands_json"]),
        "revision": row["revision"],
        "current_capsule_id": row["current_capsule_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class StateService:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.database_path = state_dir / "state.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        connection = connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def add_task(self, title: str, goal: str, checks: List[str]) -> Dict[str, Any]:
        task_id = f"task-{uuid.uuid4().hex}"
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, title, goal, check_commands_json, revision,
                    current_capsule_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, NULL, ?, ?)
                """,
                (task_id, title, goal, json.dumps(checks), created_at, created_at),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise OffworkError(
                "TASK_NOT_FOUND",
                "Task does not exist",
                details={"task_id": task_id},
            )
        return _task_row(row)

    def register_capsule(
        self,
        *,
        capsule_id: str,
        task_id: str,
        archive_path: str,
        manifest_hash: str,
        expected_revision: int,
        created_at: str,
    ) -> int:
        captured_revision = expected_revision + 1
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE tasks
                SET revision = ?, current_capsule_id = ?, updated_at = ?
                WHERE task_id = ? AND revision = ?
                """,
                (captured_revision, capsule_id, created_at, task_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise OffworkError(
                    "TASK_REVISION_CONFLICT",
                    "Task changed while the Capsule was being captured",
                    details={"task_id": task_id, "expected_revision": expected_revision},
                )
            connection.execute(
                """
                INSERT INTO capsules (
                    capsule_id, task_id, archive_path, manifest_hash,
                    captured_task_revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    capsule_id,
                    task_id,
                    archive_path,
                    manifest_hash,
                    captured_revision,
                    created_at,
                ),
            )
        return captured_revision

    def reconcile_capsule(
        self,
        *,
        capsule_id: str,
        task_id: str,
        archive_path: str,
        manifest_hash: str,
        captured_revision: int,
        expected_revision: int,
        expected_current_capsule_id: Optional[str],
        created_at: str,
    ) -> int:
        def is_same_registration(row: sqlite3.Row | None) -> bool:
            return row is not None and all(
                (
                    row["task_id"] == task_id,
                    row["archive_path"] == archive_path,
                    row["manifest_hash"] == manifest_hash,
                    row["captured_task_revision"] == captured_revision,
                    row["created_at"] == created_at,
                )
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM capsules WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()
            if is_same_registration(existing):
                return captured_revision
            if existing is not None:
                raise OffworkError(
                    "CAPSULE_REGISTRATION_CONFLICT",
                    "Capsule identity is already registered with different state",
                    details={"task_id": task_id, "capsule_id": capsule_id},
                )

            changed = connection.execute(
                """
                UPDATE tasks
                SET revision = ?, current_capsule_id = ?, updated_at = ?
                WHERE task_id = ? AND revision = ? AND current_capsule_id IS ?
                """,
                (
                    captured_revision,
                    capsule_id,
                    created_at,
                    task_id,
                    expected_revision,
                    expected_current_capsule_id,
                ),
            ).rowcount
            if changed != 1:
                existing = connection.execute(
                    "SELECT * FROM capsules WHERE capsule_id = ?", (capsule_id,)
                ).fetchone()
                if is_same_registration(existing):
                    return captured_revision
                raise OffworkError(
                    "TASK_REVISION_CONFLICT",
                    "Task changed while a published Capsule was being reconciled",
                    details={"task_id": task_id, "expected_revision": expected_revision},
                )
            connection.execute(
                """
                INSERT INTO capsules (
                    capsule_id, task_id, archive_path, manifest_hash,
                    captured_task_revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    capsule_id,
                    task_id,
                    archive_path,
                    manifest_hash,
                    captured_revision,
                    created_at,
                ),
            )
        return captured_revision

    def get_capsule(self, task_id: str, capsule_id: Optional[str]) -> Dict[str, Any]:
        task = self.get_task(task_id)
        resolved = capsule_id or task["current_capsule_id"]
        if not resolved:
            raise OffworkError(
                "CAPSULE_NOT_FOUND",
                "Task has no captured Capsule",
                details={"task_id": task_id},
            )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM capsules WHERE capsule_id = ? AND task_id = ?",
                (resolved, task_id),
            ).fetchone()
        if row is None:
            raise OffworkError(
                "CAPSULE_NOT_FOUND",
                "Capsule does not exist for this Task",
                details={"task_id": task_id, "capsule_id": resolved},
            )
        return dict(row)

    def get_receipt_state(
        self,
        task_id: str,
        capsule_id: Optional[str],
    ) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            task_row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise OffworkError(
                    "TASK_NOT_FOUND",
                    "Task does not exist",
                    details={"task_id": task_id},
                )

            task = _task_row(task_row)
            resolved_capsule_id = capsule_id or task["current_capsule_id"]
            if not resolved_capsule_id:
                raise OffworkError(
                    "CAPSULE_NOT_FOUND",
                    "Task has no captured Capsule",
                    details={"task_id": task_id},
                )
            capsule_row = connection.execute(
                "SELECT * FROM capsules WHERE capsule_id = ? AND task_id = ?",
                (resolved_capsule_id, task_id),
            ).fetchone()
            if capsule_row is None:
                raise OffworkError(
                    "CAPSULE_NOT_FOUND",
                    "Capsule does not exist for this Task",
                    details={
                        "task_id": task_id,
                        "capsule_id": resolved_capsule_id,
                    },
                )
            acceptance_row = connection.execute(
                """
                SELECT status, note, created_at, task_revision
                FROM human_acceptance_events
                WHERE capsule_id = ?
                ORDER BY task_revision DESC
                LIMIT 1
                """,
                (resolved_capsule_id,),
            ).fetchone()

        acceptance = (
            {"status": "pending", "acted_at": None, "note": None}
            if acceptance_row is None
            else {
                "status": acceptance_row["status"],
                "acted_at": acceptance_row["created_at"],
                "note": acceptance_row["note"],
            }
        )
        return {
            "task": task,
            "capsule": dict(capsule_row),
            "acceptance": acceptance,
        }

    def capsule_registered(self, capsule_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM capsules WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()
        return row is not None

    def validate_acceptance_target(self, task_id: str, capsule_id: str) -> None:
        with self._connect() as connection:
            capsule = connection.execute(
                "SELECT task_id FROM capsules WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()
        if capsule is None:
            raise OffworkError(
                "CAPSULE_NOT_FOUND",
                "Capsule does not exist",
                details={"capsule_id": capsule_id},
            )
        if capsule["task_id"] != task_id:
            raise OffworkError(
                "CAPSULE_TASK_MISMATCH",
                "Capsule does not belong to the requested Task",
                details={"task_id": task_id, "capsule_id": capsule_id},
            )

    def record_acceptance(
        self,
        *,
        task_id: str,
        capsule_id: str,
        expected_revision: int,
        status: str,
        note: Optional[str],
    ) -> Dict[str, Any]:
        if status not in {"accepted", "rejected"}:
            raise ValueError("invalid acceptance status")
        acted_at = utc_now()
        new_revision = expected_revision + 1
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            capsule = connection.execute(
                "SELECT task_id FROM capsules WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()
            if capsule is None:
                raise OffworkError(
                    "CAPSULE_NOT_FOUND",
                    "Capsule does not exist",
                    details={"capsule_id": capsule_id},
                )
            if capsule["task_id"] != task_id:
                raise OffworkError(
                    "CAPSULE_TASK_MISMATCH",
                    "Capsule does not belong to the requested Task",
                    details={"task_id": task_id, "capsule_id": capsule_id},
                )
            changed = connection.execute(
                """
                UPDATE tasks SET revision = ?, updated_at = ?
                WHERE task_id = ? AND revision = ?
                """,
                (new_revision, acted_at, task_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise OffworkError(
                    "TASK_REVISION_CONFLICT",
                    "Task revision changed; review the current Receipt before deciding",
                    details={
                        "task_id": task_id,
                        "capsule_id": capsule_id,
                        "expected_revision": expected_revision,
                    },
                )
            connection.execute(
                """
                INSERT INTO human_acceptance_events (
                    event_id, capsule_id, status, note, task_revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"acceptance-{uuid.uuid4().hex}",
                    capsule_id,
                    status,
                    note,
                    new_revision,
                    acted_at,
                ),
            )
        return {
            "status": status,
            "acted_at": acted_at,
            "note": note,
            "task_revision": new_revision,
        }

    def get_acceptance(self, capsule_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status, note, created_at, task_revision
                FROM human_acceptance_events
                WHERE capsule_id = ?
                ORDER BY task_revision DESC
                LIMIT 1
                """,
                (capsule_id,),
            ).fetchone()
        if row is None:
            return {"status": "pending", "acted_at": None, "note": None}
        return {
            "status": row["status"],
            "acted_at": row["created_at"],
            "note": row["note"],
        }
