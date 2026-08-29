from __future__ import annotations

import os
import sqlite3
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from offwork.errors import OffworkError


SCHEMA_VERSION = 3


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(path: Path) -> None:
    if not path.exists():
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    os.chmod(path, 0o600)
    with connect(path) as connection:
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
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


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
