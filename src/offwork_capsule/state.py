from __future__ import annotations

import json
import shlex
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .storage import initialize_global_registry, initialize_project_storage


TASK_STATUSES = {"todo", "in_progress", "review", "complete"}
WAITING_DISPOSITIONS = {"park", "delegate"}
OPEN_LOOP_DISPOSITIONS = {"resolve", "park", "drop", "delegate"}
DEFAULT_TASK_ID = "tsk_default"
CAPSULE_STATUSES = {"validated", "fresh_verified", "rejected"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


class OffworkError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int,
        details: Optional[Mapping[str, Any]] = None,
        recovery: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = dict(details or {})
        self.recovery = recovery

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "recovery": self.recovery,
        }


def _task_not_found(task_id: str) -> OffworkError:
    return OffworkError(
        "TASK_NOT_FOUND",
        "Task not found: %s" % task_id,
        exit_code=3,
        details={"task_id": task_id},
        recovery="Check the task ID with `offwork task list`.",
    )


def _invalid_state(task_id: str, status: str, action: str) -> OffworkError:
    return OffworkError(
        "INVALID_TASK_STATE",
        "Task %s cannot %s from state %s." % (task_id, action, status),
        exit_code=4,
        details={"task_id": task_id, "status": status, "action": action},
        recovery="Inspect the task with `offwork task show` before retrying.",
    )


def _row_payload(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        commands = json.loads(row["acceptance_commands_json"])
        open_loops = json.loads(row["open_loops_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise OffworkError(
            "INVALID_TASK_STATE",
            "Task %s contains invalid JSON state." % row["task_id"],
            exit_code=4,
            details={"task_id": row["task_id"]},
            recovery="Repair the task record before retrying.",
        ) from error
    return {
        "task_id": str(row["task_id"]),
        "title": str(row["title"]),
        "description": str(row["description"]),
        "goal": str(row["goal"]),
        "acceptance_commands": commands,
        "auto_complete": bool(row["auto_complete"]),
        "open_loops": open_loops,
        "status": str(row["status"]),
        "revision": int(row["revision"]),
        "archived_at": row["archived_at"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def compute_task_state(
    task: Mapping[str, Any], dependency_statuses: Mapping[str, str]
) -> Dict[str, Any]:
    blockers = sorted(
        task_id
        for task_id, status in dependency_statuses.items()
        if status != "complete"
    )
    if task.get("archived_at") is not None:
        computed_state = "archived"
    elif task.get("status") == "complete":
        computed_state = "terminal"
    elif blockers:
        computed_state = "blocked"
    elif any(
        isinstance(loop, Mapping)
        and str(loop.get("disposition", "")) in WAITING_DISPOSITIONS
        for loop in task.get("open_loops", [])
    ):
        computed_state = "waiting"
    else:
        computed_state = "actionable"
    result = dict(task)
    result["dependencies"] = sorted(dependency_statuses)
    result["blockers"] = blockers
    result["computed_state"] = computed_state
    return result


def normalize_open_loop(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("expected a JSON object")
    title = value.get("title")
    disposition = value.get("disposition")
    note = value.get("note")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    if not isinstance(disposition, str) or disposition not in OPEN_LOOP_DISPOSITIONS:
        raise ValueError(
            "disposition must be one of resolve, park, drop, delegate"
        )
    if not isinstance(note, str):
        raise ValueError("note must be a string")
    return {
        "title": title.strip(),
        "disposition": disposition,
        "note": note,
    }


class StateService:
    def __init__(self, project_root: Path) -> None:
        self.storage = initialize_project_storage(Path(project_root))
        self.warnings: List[Dict[str, Any]] = []
        self._recover_pending_legacy_capture()
        self._bootstrap_v01_artifacts()

    def _reset_warnings(self) -> None:
        self.warnings = []

    def _bootstrap_v01_artifacts(self) -> None:
        """Import an existing V0.1 latest capsule, but never invent a default Task."""

        from .capsule import load_latest_capsule

        latest_path = self.storage.offwork_root / "latest.json"
        if not latest_path.is_file():
            return
        capsule = load_latest_capsule(self.storage.project_root)
        if capsule.get("task_id") not in (None, DEFAULT_TASK_ID):
            return
        self.ensure_default_task(str(capsule.get("goal", "")))

    def _recover_pending_legacy_capture(self) -> None:
        """Settle the small filesystem/SQLite handoff left by an interrupted capture."""

        from .capsule import (
            capsule_transaction_lock,
            load_pending_legacy_capture,
        )

        marker_path = self.storage.offwork_root / "pending-legacy-capture.json"
        try:
            marker_path.lstat()
        except FileNotFoundError:
            return
        with capsule_transaction_lock(self.storage.project_root):
            marker = load_pending_legacy_capture(self.storage.project_root)
            if marker is not None:
                self._recover_pending_legacy_capture_locked(marker)

    def _recover_pending_legacy_capture_locked(
        self, marker: Mapping[str, Any]
    ) -> None:
        from .capsule import (
            CapsuleValidationError,
            capsule_content_hash,
            clear_pending_legacy_capture,
            load_capsule,
            restore_legacy_latest,
            update_legacy_latest,
        )

        capsule_id = str(marker.get("capsule_id", ""))
        task_id = str(marker.get("task_id", ""))
        if not capsule_id or task_id != DEFAULT_TASK_ID:
            raise OffworkError(
                "CAPSULE_INTEGRITY_FAILED",
                "Pending legacy capture record is invalid.",
                exit_code=4,
                details={},
                recovery="Inspect the private pending capture record.",
            )
        with self.storage.connect() as connection:
            capsule_row = connection.execute(
                "SELECT task_id, status, content_hash FROM capsules "
                "WHERE capsule_id = ?",
                (capsule_id,),
            ).fetchone()
        published_and_valid = False
        committed_status = (
            capsule_row is not None
            and str(capsule_row["task_id"]) == task_id
            and str(capsule_row["status"]) in {"validated", "fresh_verified"}
            and str(capsule_row["content_hash"])
            == str(marker.get("content_hash", ""))
        )
        if committed_status:
            try:
                capsule = load_capsule(self.storage.project_root, capsule_id)
                published_and_valid = capsule_content_hash(capsule) == str(
                    capsule_row["content_hash"]
                )
            except (CapsuleValidationError, FileNotFoundError, OSError):
                published_and_valid = False
        if published_and_valid:
            update_legacy_latest(self.storage.project_root, capsule_id)
            self._force_sync_registry()
            clear_pending_legacy_capture(self.storage.project_root)
            return

        before_revision = int(marker.get("before_task_revision", 0))
        before_status = str(marker.get("before_task_status", ""))
        before_updated_at = str(marker.get("before_task_updated_at", ""))
        timestamp = _now()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_capsule = connection.execute(
                "SELECT task_id, status, content_hash FROM capsules "
                "WHERE capsule_id = ?",
                (capsule_id,),
            ).fetchone()
            owns_committed_publish = (
                current_capsule is not None
                and str(current_capsule["task_id"]) == task_id
                and str(current_capsule["status"])
                in {"validated", "fresh_verified"}
                and str(current_capsule["content_hash"])
                == str(marker.get("content_hash", ""))
            )
            managed_session_id = marker.get("managed_session_id")
            before_session_revision = marker.get("before_session_revision")
            if owns_committed_publish:
                task = self._require_task(connection, task_id)
                task_matches_publish = (
                    int(task["revision"]) == before_revision + 1
                    and str(task["status"]) == "review"
                )
                session_matches_publish = managed_session_id is None
                if managed_session_id is not None and before_session_revision is not None:
                    session = connection.execute(
                        "SELECT state, revision FROM sessions "
                        "WHERE managed_session_id = ? AND task_id = ? "
                        "AND is_primary = 1",
                        (managed_session_id, task_id),
                    ).fetchone()
                    session_matches_publish = (
                        session is not None
                        and int(session["revision"])
                        == int(before_session_revision) + 1
                        and str(session["state"]) == "hibernated"
                    )
                if task_matches_publish and session_matches_publish:
                    connection.execute(
                        "UPDATE tasks SET status = ?, revision = ?, updated_at = ? "
                        "WHERE task_id = ? AND revision = ? AND status = 'review'",
                        (
                            before_status,
                            before_revision,
                            before_updated_at,
                            task_id,
                            before_revision + 1,
                        ),
                    )
            if (
                owns_committed_publish
                and managed_session_id is not None
                and before_session_revision is not None
                and task_matches_publish
                and session_matches_publish
            ):
                connection.execute(
                    "UPDATE sessions SET state = ?, revision = ?, updated_at = ? "
                    "WHERE managed_session_id = ? AND task_id = ? "
                    "AND revision = ? AND state = 'hibernated' AND is_primary = 1",
                    (
                        marker.get("before_session_state"),
                        int(before_session_revision),
                        marker.get("before_session_updated_at"),
                        managed_session_id,
                        task_id,
                        int(before_session_revision) + 1,
                    ),
                )
            archive_path = str(marker.get("archive_path", ""))
            content_hash = str(marker.get("content_hash", ""))
            if current_capsule is None and archive_path and content_hash:
                connection.execute(
                    "INSERT INTO capsules(capsule_id, task_id, managed_session_id, "
                    "parent_capsule_id, status, content_hash, archive_path, revision, "
                    "archived_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'rejected', ?, ?, 1, NULL, ?, ?)",
                    (
                        capsule_id,
                        task_id,
                        managed_session_id,
                        marker.get("parent_capsule_id"),
                        content_hash,
                        archive_path,
                        timestamp,
                        timestamp,
                    ),
                )
            elif (
                current_capsule is not None
                and str(current_capsule["task_id"]) == task_id
            ):
                connection.execute(
                    "UPDATE capsules SET status = 'rejected', updated_at = ? "
                    "WHERE capsule_id = ?",
                    (timestamp, capsule_id),
                )
            connection.commit()
        restore_legacy_latest(
            self.storage.project_root,
            marker.get("previous_latest_id")
            if isinstance(marker.get("previous_latest_id"), str)
            else None,
        )
        self._force_sync_registry()
        clear_pending_legacy_capture(self.storage.project_root)

    def _require_task(
        self, connection: sqlite3.Connection, task_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise _task_not_found(task_id)
        if str(row["status"]) not in TASK_STATUSES:
            raise _invalid_state(task_id, str(row["status"]), "be used")
        return row

    def _require_revision(
        self, row: sqlite3.Row, expected_revision: Optional[int]
    ) -> None:
        if expected_revision is None:
            return
        actual = int(row["revision"])
        if actual != expected_revision:
            raise OffworkError(
                "STALE_REVISION",
                "Task %s revision is %d, not %d."
                % (row["task_id"], actual, expected_revision),
                exit_code=4,
                details={
                    "task_id": row["task_id"],
                    "expected_revision": expected_revision,
                    "actual_revision": actual,
                },
                recovery="Reload the task and retry with its current revision.",
            )

    def _decorate_rows(
        self, connection: sqlite3.Connection, rows: Sequence[sqlite3.Row]
    ) -> List[Dict[str, Any]]:
        task_ids = [str(row["task_id"]) for row in rows]
        dependencies: Dict[str, Dict[str, str]] = {
            task_id: {} for task_id in task_ids
        }
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            statement = (
                "SELECT d.task_id, d.depends_on_task_id, t.status "
                "FROM task_dependencies AS d "
                "JOIN tasks AS t ON t.task_id = d.depends_on_task_id "
                "WHERE d.task_id IN (%s)" % placeholders
            )
            for dependency in connection.execute(statement, task_ids):
                dependencies[str(dependency["task_id"])][
                    str(dependency["depends_on_task_id"])
                ] = str(dependency["status"])
        fresh_requirements: Dict[str, bool] = {}
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            for config in connection.execute(
                "SELECT task_id, require_fresh_verifier "
                "FROM task_auto_complete_config WHERE task_id IN (%s)"
                % placeholders,
                task_ids,
            ):
                fresh_requirements[str(config["task_id"])] = bool(
                    config["require_fresh_verifier"]
                )
        decorated: List[Dict[str, Any]] = []
        for row in rows:
            task_id = str(row["task_id"])
            payload = _row_payload(row)
            payload["require_fresh_verifier"] = fresh_requirements.get(
                task_id, False
            )
            decorated.append(
                compute_task_state(payload, dependencies.get(task_id, {}))
            )
        return decorated

    def _task_from_connection(
        self, connection: sqlite3.Connection, task_id: str
    ) -> Dict[str, Any]:
        row = self._require_task(connection, task_id)
        return self._decorate_rows(connection, [row])[0]

    def _sync_registry(self, *, force: bool = False) -> None:
        timestamp = _now()
        with self.storage.connect() as project_connection:
            tasks = project_connection.execute(
                "SELECT task_id, title, status, revision, archived_at, updated_at "
                "FROM tasks ORDER BY created_at, task_id"
            ).fetchall()
        registry = initialize_global_registry()
        with registry.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO projects(project_id, canonical_path, "
                "state_database_path, last_seen_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "canonical_path = excluded.canonical_path, "
                "state_database_path = excluded.state_database_path, "
                "last_seen_at = excluded.last_seen_at",
                (
                    self.storage.project_id,
                    str(self.storage.project_root),
                    str(self.storage.database_path),
                    timestamp,
                ),
            )
            if force:
                connection.execute(
                    "DELETE FROM task_summaries WHERE project_id = ?",
                    (self.storage.project_id,),
                )
            connection.executemany(
                "INSERT INTO task_summaries(task_id, project_id, title, status, "
                "revision, archived_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id, task_id) DO UPDATE SET "
                "title = excluded.title, status = excluded.status, "
                "revision = excluded.revision, "
                "archived_at = excluded.archived_at, updated_at = excluded.updated_at "
                "WHERE excluded.revision > task_summaries.revision "
                "OR (excluded.revision = task_summaries.revision "
                "AND task_summaries.status = 'active' "
                "AND excluded.status IN ('todo', 'in_progress', 'review', 'complete'))",
                [
                    (
                        row["task_id"],
                        self.storage.project_id,
                        row["title"],
                        row["status"],
                        row["revision"],
                        row["archived_at"],
                        row["updated_at"],
                    )
                    for row in tasks
                ],
            )
            connection.commit()

    def _force_sync_registry(self) -> None:
        self._sync_registry(force=True)

    def _sync_registry_safely(self) -> None:
        try:
            self._sync_registry()
        except Exception as error:
            self.warnings.append(
                {
                    "code": "REGISTRY_SYNC_FAILED",
                    "message": "Project state committed, but the global registry was not updated.",
                    "details": {"reason": str(error)},
                    "recovery": "Run another project mutation later to rebuild the summary.",
                }
            )

    def add_task(
        self,
        title: str,
        goal: str,
        *,
        auto_complete: bool = False,
        require_fresh_verifier: bool = False,
        acceptance_commands: Optional[Sequence[str]] = None,
        open_loops: Optional[Sequence[Mapping[str, Any]]] = None,
        description: str = "",
    ) -> Dict[str, Any]:
        self._reset_warnings()
        clean_title = str(title).strip()
        clean_goal = str(goal).strip()
        raw_commands = list(acceptance_commands or [])
        if any(
            not isinstance(command, str) or not command.strip()
            for command in raw_commands
        ):
            raise OffworkError(
                "INVALID_ARGUMENT",
                "Acceptance commands must not be blank.",
                exit_code=2,
                details={},
                recovery="Remove blank --accept-cmd values and retry.",
            )
        commands = [command.strip() for command in raw_commands]
        try:
            loops = [normalize_open_loop(loop) for loop in (open_loops or [])]
        except ValueError as error:
            raise OffworkError(
                "INVALID_ARGUMENT",
                "Invalid open loop: %s" % error,
                exit_code=2,
                details={},
                recovery="Provide title, disposition, and note strings.",
            ) from error
        if not clean_title or not clean_goal:
            raise OffworkError(
                "INVALID_ARGUMENT",
                "Task title and goal must not be empty.",
                exit_code=2,
                details={},
                recovery="Provide a title and a non-empty --goal.",
            )
        if auto_complete and not commands:
            raise OffworkError(
                "AUTO_COMPLETE_REQUIRES_ACCEPTANCE",
                "Auto-complete requires at least one acceptance command.",
                exit_code=4,
                details={},
                recovery="Add --accept-cmd or omit --auto-complete.",
            )
        if require_fresh_verifier and not auto_complete:
            raise OffworkError(
                "INVALID_ARGUMENT",
                "Fresh-verifier completion requires --auto-complete.",
                exit_code=2,
                details={},
                recovery="Add --auto-complete or remove --require-fresh-verifier.",
            )
        task_id = "tsk_" + uuid.uuid4().hex
        timestamp = _now()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO tasks(task_id, title, description, goal, "
                "acceptance_commands_json, auto_complete, open_loops_json, status, "
                "revision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'todo', 1, ?, ?)",
                (
                    task_id,
                    clean_title,
                    description,
                    clean_goal,
                    json.dumps(commands, ensure_ascii=False),
                    int(auto_complete),
                    json.dumps(loops, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO task_auto_complete_config("
                "task_id, require_fresh_verifier) VALUES (?, ?)",
                (task_id, int(require_fresh_verifier)),
            )
            task = self._task_from_connection(connection, task_id)
            connection.commit()
        self._sync_registry_safely()
        return task

    def ensure_default_task(self, goal: str) -> Dict[str, Any]:
        """Return the reserved V0.1 compatibility task, creating it once."""

        clean_goal = str(goal).strip() or "Resume the latest Work Capsule"
        timestamp = _now()
        created = False
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (DEFAULT_TASK_ID,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO tasks(task_id, title, description, goal, "
                    "acceptance_commands_json, auto_complete, open_loops_json, "
                    "status, revision, archived_at, created_at, updated_at) "
                    "VALUES (?, 'Default task', '', ?, '[]', 0, '[]', "
                    "'in_progress', 1, NULL, ?, ?)",
                    (DEFAULT_TASK_ID, clean_goal, timestamp, timestamp),
                )
                created = True
            connection.execute(
                "INSERT OR IGNORE INTO task_auto_complete_config("
                "task_id, require_fresh_verifier) VALUES (?, 0)",
                (DEFAULT_TASK_ID,),
            )
            self._import_v01_capsules(connection)
            task = self._task_from_connection(connection, DEFAULT_TASK_ID)
            connection.commit()
        if created:
            self._sync_registry_safely()
        return task

    def _import_v01_capsules(self, connection: sqlite3.Connection) -> None:
        """Bind immutable pre-Task capsules to the reserved Task without rewriting."""

        from .capsule import (
            CURRENT_CAPSULE_ID,
            LEGACY_CAPSULE_ID,
            capsule_content_hash,
            load_capsule,
        )

        capsules_root = self.storage.offwork_root / "capsules"
        if not capsules_root.is_dir():
            return
        candidates = sorted(
            path
            for path in capsules_root.iterdir()
            if path.is_dir()
            and (
                LEGACY_CAPSULE_ID.fullmatch(path.name)
                or CURRENT_CAPSULE_ID.fullmatch(path.name)
            )
        )
        latest_id: Optional[str] = None
        latest_path = self.storage.offwork_root / "latest.json"
        if latest_path.is_file():
            try:
                latest_payload = json.loads(latest_path.read_text(encoding="utf-8"))
                if isinstance(latest_payload, dict):
                    latest_id = str(latest_payload.get("capsule_id", ""))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                latest_id = None
        candidates.sort(key=lambda path: (path.name == latest_id, path.name))
        parent_row = connection.execute(
            "SELECT capsule_id FROM capsules WHERE task_id = ? "
            "AND status IN ('validated', 'fresh_verified') "
            "ORDER BY rowid DESC LIMIT 1",
            (DEFAULT_TASK_ID,),
        ).fetchone()
        parent_id = str(parent_row["capsule_id"]) if parent_row is not None else None
        for archive_path in candidates:
            existing = connection.execute(
                "SELECT task_id, status FROM capsules WHERE capsule_id = ?",
                (archive_path.name,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["task_id"] == DEFAULT_TASK_ID
                    and str(existing["status"]) in {"validated", "fresh_verified"}
                ):
                    parent_id = archive_path.name
                continue
            capsule = load_capsule(self.storage.project_root, archive_path.name)
            capsule_task = capsule.get("task_id")
            if capsule_task not in (None, DEFAULT_TASK_ID):
                continue
            timestamp = str(capsule.get("captured_at") or _now())
            imported_status = str(capsule.get("status", "validated"))
            if imported_status not in CAPSULE_STATUSES:
                imported_status = "validated"
            requested_parent = capsule.get("parent_capsule_id")
            if not isinstance(requested_parent, str) or connection.execute(
                "SELECT 1 FROM capsules WHERE capsule_id = ?",
                (requested_parent,),
            ).fetchone() is None:
                requested_parent = parent_id
            connection.execute(
                "INSERT INTO capsules(capsule_id, task_id, managed_session_id, "
                "parent_capsule_id, status, content_hash, archive_path, revision, "
                "archived_at, created_at, updated_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, 1, NULL, ?, ?)",
                (
                    archive_path.name,
                    DEFAULT_TASK_ID,
                    requested_parent,
                    imported_status,
                    capsule_content_hash(capsule),
                    str(archive_path.resolve()),
                    timestamp,
                    timestamp,
                ),
            )
            if imported_status in {"validated", "fresh_verified"}:
                parent_id = archive_path.name

    def prepare_capture(
        self,
        task_id: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Validate capture preconditions and snapshot task/session bindings."""

        with self.storage.connect() as connection:
            row = self._require_task(connection, task_id)
            self._require_revision(row, expected_revision)
            if row["archived_at"] is not None or row["status"] == "complete":
                raise _invalid_state(task_id, str(row["status"]), "capture")
            blockers = [
                str(item["depends_on_task_id"])
                for item in connection.execute(
                    "SELECT d.depends_on_task_id FROM task_dependencies AS d "
                    "JOIN tasks AS t ON t.task_id = d.depends_on_task_id "
                    "WHERE d.task_id = ? AND t.status <> 'complete' "
                    "ORDER BY d.depends_on_task_id",
                    (task_id,),
                )
            ]
            if blockers:
                raise OffworkError(
                    "DEPENDENCY_NOT_COMPLETE",
                    "Task dependencies are not complete.",
                    exit_code=4,
                    details={"task_id": task_id, "blockers": blockers},
                    recovery="Complete the blocker tasks before capturing this task.",
                )
            task = self._task_from_connection(connection, task_id)
            primary = connection.execute(
                "SELECT managed_session_id, cwd, state, revision, updated_at "
                "FROM sessions "
                "WHERE task_id = ? AND is_primary = 1",
                (task_id,),
            ).fetchone()
            if primary is not None:
                session_cwd = Path(str(primary["cwd"])).resolve()
                if session_cwd != self.storage.project_root:
                    raise OffworkError(
                        "PROJECT_PATH_MISMATCH",
                        "Primary session belongs to a different project path.",
                        exit_code=4,
                        details={
                            "task_id": task_id,
                            "session_cwd": str(session_cwd),
                            "project_path": str(self.storage.project_root),
                        },
                        recovery="Use the exact project path attached to the session.",
                    )
            parent = connection.execute(
                "SELECT capsule_id FROM capsules WHERE task_id = ? "
                "AND status IN ('validated', 'fresh_verified') "
                "AND archived_at IS NULL ORDER BY rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        task["primary_session_id"] = (
            str(primary["managed_session_id"]) if primary is not None else None
        )
        task["primary_session_state"] = (
            str(primary["state"]) if primary is not None else None
        )
        task["primary_session_revision"] = (
            int(primary["revision"]) if primary is not None else None
        )
        task["primary_session_updated_at"] = (
            str(primary["updated_at"]) if primary is not None else None
        )
        task["parent_capsule_id"] = (
            str(parent["capsule_id"]) if parent is not None else None
        )
        return task

    def publish_capture(
        self,
        *,
        task_id: str,
        expected_revision: int,
        capsule_id: str,
        managed_session_id: Optional[str],
        parent_capsule_id: Optional[str],
        status: str,
        content_hash: str,
        archive_path: Path,
    ) -> Dict[str, Any]:
        """Atomically register a complete capsule and settle task/session state."""

        if status not in CAPSULE_STATUSES:
            raise ValueError("invalid capsule status")
        timestamp = _now()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_task(connection, task_id)
            self._require_revision(row, expected_revision)
            if row["archived_at"] is not None or row["status"] == "complete":
                raise _invalid_state(task_id, str(row["status"]), "capture")
            blockers = [
                str(item["depends_on_task_id"])
                for item in connection.execute(
                    "SELECT d.depends_on_task_id FROM task_dependencies AS d "
                    "JOIN tasks AS t ON t.task_id = d.depends_on_task_id "
                    "WHERE d.task_id = ? AND t.status <> 'complete' "
                    "ORDER BY d.depends_on_task_id",
                    (task_id,),
                )
            ]
            if blockers:
                raise OffworkError(
                    "DEPENDENCY_NOT_COMPLETE",
                    "Task dependencies changed during capture.",
                    exit_code=4,
                    details={"task_id": task_id, "blockers": blockers},
                    recovery="Complete the blocker tasks before capturing again.",
                )
            current_primary = connection.execute(
                "SELECT managed_session_id FROM sessions "
                "WHERE task_id = ? AND is_primary = 1",
                (task_id,),
            ).fetchone()
            current_primary_id = (
                str(current_primary["managed_session_id"])
                if current_primary is not None
                else None
            )
            if current_primary_id != managed_session_id:
                raise OffworkError(
                    "SESSION_ID_CONFLICT",
                    "Primary session changed during capture.",
                    exit_code=4,
                    details={
                        "expected_primary": managed_session_id,
                        "actual_primary": current_primary_id,
                    },
                    recovery="Reload task sessions and capture again.",
                )
            connection.execute(
                "INSERT INTO capsules(capsule_id, task_id, managed_session_id, "
                "parent_capsule_id, status, content_hash, archive_path, revision, "
                "archived_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)",
                (
                    capsule_id,
                    task_id,
                    managed_session_id,
                    parent_capsule_id,
                    status,
                    content_hash,
                    str(Path(archive_path).resolve()),
                    timestamp,
                    timestamp,
                ),
            )
            if managed_session_id is not None:
                connection.execute(
                    "UPDATE sessions SET state = 'hibernated', "
                    "revision = revision + 1, updated_at = ? "
                    "WHERE managed_session_id = ? AND task_id = ? AND is_primary = 1",
                    (timestamp, managed_session_id, task_id),
                )
            connection.execute(
                "UPDATE tasks SET status = 'review', revision = revision + 1, "
                "updated_at = ? WHERE task_id = ?",
                (timestamp, task_id),
            )
            task = self._task_from_connection(connection, task_id)
            connection.commit()
        self._sync_registry_safely()
        task["_capture_compensation"] = {
            "capsule_id": capsule_id,
            "task_id": task_id,
            "before_task_status": str(row["status"]),
            "before_task_revision": int(row["revision"]),
            "before_task_updated_at": str(row["updated_at"]),
            "published_task_revision": int(task["revision"]),
            "managed_session_id": managed_session_id,
        }
        return task

    def register_rejected_capture(
        self,
        *,
        task_id: str,
        expected_revision: int,
        capsule_id: str,
        managed_session_id: Optional[str],
        parent_capsule_id: Optional[str],
        content_hash: str,
        archive_path: Path,
    ) -> None:
        """Register an immutable rejected recovery attempt without settling work."""

        timestamp = _now()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_task(connection, task_id)
            self._require_revision(row, expected_revision)
            current_primary = connection.execute(
                "SELECT managed_session_id FROM sessions "
                "WHERE task_id = ? AND is_primary = 1",
                (task_id,),
            ).fetchone()
            actual_primary = (
                str(current_primary["managed_session_id"])
                if current_primary is not None
                else None
            )
            if actual_primary != managed_session_id:
                raise OffworkError(
                    "SESSION_ID_CONFLICT",
                    "Primary session changed during verification.",
                    exit_code=4,
                    details={
                        "expected_primary": managed_session_id,
                        "actual_primary": actual_primary,
                    },
                    recovery="Reload task sessions and capture again.",
                )
            connection.execute(
                "INSERT INTO capsules(capsule_id, task_id, managed_session_id, "
                "parent_capsule_id, status, content_hash, archive_path, revision, "
                "archived_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'rejected', ?, ?, 1, NULL, ?, ?)",
                (
                    capsule_id,
                    task_id,
                    managed_session_id,
                    parent_capsule_id,
                    content_hash,
                    str(Path(archive_path).resolve()),
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()

    def rollback_published_capture(
        self,
        compensation: Mapping[str, Any],
        *,
        before_session_state: Optional[str],
        before_session_revision: Optional[int],
        before_session_updated_at: Optional[str],
    ) -> None:
        """Compensate a legacy pointer failure before returning an error."""

        task_id = str(compensation["task_id"])
        capsule_id = str(compensation["capsule_id"])
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_task(connection, task_id)
            if int(row["revision"]) != int(compensation["published_task_revision"]):
                raise OffworkError(
                    "STALE_REVISION",
                    "Task changed before capture compensation completed.",
                    exit_code=4,
                    details={"task_id": task_id},
                    recovery="Inspect the Task and latest capsule before retrying.",
                )
            connection.execute(
                "UPDATE capsules SET status = 'rejected', updated_at = ? "
                "WHERE capsule_id = ? AND task_id = ?",
                (_now(), capsule_id, task_id),
            )
            connection.execute(
                "UPDATE tasks SET status = ?, revision = ?, updated_at = ? "
                "WHERE task_id = ?",
                (
                    compensation["before_task_status"],
                    compensation["before_task_revision"],
                    compensation["before_task_updated_at"],
                    task_id,
                ),
            )
            managed_session_id = compensation.get("managed_session_id")
            if managed_session_id is not None and before_session_state is not None:
                session = connection.execute(
                    "SELECT revision FROM sessions WHERE managed_session_id = ?",
                    (managed_session_id,),
                ).fetchone()
                expected = (
                    int(before_session_revision) + 1
                    if before_session_revision is not None
                    else None
                )
                if session is None or expected is None or int(session["revision"]) != expected:
                    raise OffworkError(
                        "STALE_REVISION",
                        "Primary session changed before capture compensation completed.",
                        exit_code=4,
                        details={"managed_session_id": managed_session_id},
                        recovery="Inspect the managed session before retrying.",
                    )
                connection.execute(
                    "UPDATE sessions SET state = ?, revision = ?, updated_at = ? "
                    "WHERE managed_session_id = ?",
                    (
                        before_session_state,
                        before_session_revision,
                        before_session_updated_at,
                        managed_session_id,
                    ),
                )
            connection.commit()
        self._force_sync_registry()

    def _reject_capsule_after_failed_acceptance(
        self, capsule_id: str, task_id: str
    ) -> None:
        timestamp = _now()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE capsules SET status = 'rejected', updated_at = ? "
                "WHERE capsule_id = ? AND task_id = ? "
                "AND status IN ('validated', 'fresh_verified')",
                (timestamp, capsule_id, task_id),
            )
            connection.commit()

    def evaluate_auto_complete(
        self,
        task_snapshot: Mapping[str, Any],
        *,
        expected_revision: int,
        fresh_verified: bool,
        require_fresh_verifier: bool = False,
        capsule_id: Optional[str] = None,
        capsule_open_loops: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Run configured local gates and use revision CAS to finish the task."""

        enabled = bool(task_snapshot.get("auto_complete"))
        result: Dict[str, Any] = {
            "enabled": enabled,
            "passed": False,
            "checks": [],
            "local_acceptance_only": True,
        }
        if not enabled:
            result["reason"] = "not_enabled"
            return result
        commands = task_snapshot.get("acceptance_commands")
        if not isinstance(commands, list) or not commands:
            result["reason"] = "missing_acceptance_command"
            return result
        all_loops = list(task_snapshot.get("open_loops", [])) + list(
            capsule_open_loops or []
        )
        blocking_loops = [
            loop
            for loop in all_loops
            if isinstance(loop, Mapping)
            and loop.get("disposition") in {"resolve", "park", "delegate"}
        ]
        if blocking_loops:
            result["reason"] = "open_loops"
            result["blocking_open_loops"] = blocking_loops
            return result
        if require_fresh_verifier and not fresh_verified:
            result["reason"] = "fresh_verifier_required"
            return result
        for command in commands:
            try:
                argv = shlex.split(str(command))
            except ValueError as error:
                result["checks"].append(
                    {"command": str(command), "argv": [], "returncode": None}
                )
                result["reason"] = "invalid_acceptance_command"
                result["detail"] = str(error)
                return result
            if not argv:
                result["reason"] = "missing_acceptance_command"
                return result
            try:
                completed = subprocess.run(
                    argv,
                    cwd=self.storage.project_root,
                    shell=False,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                check = {
                    "command": str(command),
                    "argv": argv,
                    "returncode": completed.returncode,
                }
            except OSError as error:
                check = {
                    "command": str(command),
                    "argv": argv,
                    "returncode": None,
                }
                result["checks"].append(check)
                result["reason"] = "acceptance_command_unavailable"
                result["detail"] = str(error)
                return result
            result["checks"].append(check)
            if completed.returncode != 0:
                result["reason"] = "acceptance_command_failed"
                return result

        from .capsule import (
            CapsuleValidationError,
            capsule_content_hash,
            load_capsule,
        )

        if not isinstance(capsule_id, str) or not capsule_id:
            result["reason"] = "capsule_integrity_failed"
            return result
        try:
            verified_capsule = load_capsule(self.storage.project_root, capsule_id)
            verified_hash = capsule_content_hash(verified_capsule)
        except (CapsuleValidationError, FileNotFoundError, OSError):
            self._reject_capsule_after_failed_acceptance(
                capsule_id, str(task_snapshot["task_id"])
            )
            result["reason"] = "capsule_integrity_failed"
            return result

        timestamp = _now()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_task(connection, str(task_snapshot["task_id"]))
            try:
                self._require_revision(row, expected_revision)
            except OffworkError:
                connection.rollback()
                result["reason"] = "stale_revision"
                return result
            if row["status"] != "review" or row["archived_at"] is not None:
                connection.rollback()
                result["reason"] = "task_state_changed"
                return result
            capsule_row = connection.execute(
                "SELECT task_id, status, content_hash FROM capsules "
                "WHERE capsule_id = ?",
                (capsule_id,),
            ).fetchone()
            if (
                capsule_row is None
                or str(capsule_row["task_id"]) != str(task_snapshot["task_id"])
                or str(capsule_row["status"])
                not in {"validated", "fresh_verified"}
                or str(capsule_row["content_hash"]) != verified_hash
            ):
                if (
                    capsule_row is not None
                    and str(capsule_row["task_id"])
                    == str(task_snapshot["task_id"])
                ):
                    connection.execute(
                        "UPDATE capsules SET status = 'rejected', updated_at = ? "
                        "WHERE capsule_id = ? AND task_id = ?",
                        (timestamp, capsule_id, task_snapshot["task_id"]),
                    )
                    connection.commit()
                else:
                    connection.rollback()
                result["reason"] = "capsule_integrity_failed"
                return result
            connection.execute(
                "UPDATE tasks SET status = 'complete', archived_at = ?, "
                "revision = revision + 1, updated_at = ? WHERE task_id = ?",
                (timestamp, timestamp, task_snapshot["task_id"]),
            )
            task = self._task_from_connection(
                connection, str(task_snapshot["task_id"])
            )
            connection.commit()
        self._sync_registry_safely()
        result["passed"] = True
        result["reason"] = "all_local_gates_passed"
        result["task"] = task
        return result

    def show_task(self, task_id: str) -> Dict[str, Any]:
        with self.storage.connect() as connection:
            return self._task_from_connection(connection, task_id)

    def list_tasks(self, computed_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.storage.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at, task_id"
            ).fetchall()
            tasks = self._decorate_rows(connection, rows)
        if computed_filter is None:
            return [task for task in tasks if task["computed_state"] != "archived"]
        return [task for task in tasks if task["computed_state"] == computed_filter]

    def _change_status(
        self,
        task_id: str,
        target_status: str,
        action: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._reset_warnings()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_task(connection, task_id)
            self._require_revision(row, expected_revision)
            if (
                row["archived_at"] is not None
                or row["status"] == target_status
                or (target_status == "in_progress" and row["status"] not in {"todo", "review"})
            ):
                raise _invalid_state(task_id, str(row["status"]), action)
            timestamp = _now()
            connection.execute(
                "UPDATE tasks SET status = ?, revision = revision + 1, "
                "updated_at = ? WHERE task_id = ?",
                (target_status, timestamp, task_id),
            )
            task = self._task_from_connection(connection, task_id)
            connection.commit()
        self._sync_registry_safely()
        return task

    def start_task(
        self, task_id: str, *, expected_revision: Optional[int] = None
    ) -> Dict[str, Any]:
        return self._change_status(
            task_id, "in_progress", "start", expected_revision=expected_revision
        )

    def complete_task(
        self,
        task_id: str,
        *,
        confirmed: bool,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._reset_warnings()
        if not confirmed:
            raise OffworkError(
                "CONFIRMATION_REQUIRED",
                "Completing a task requires --confirm.",
                exit_code=4,
                details={"task_id": task_id},
                recovery="Review the task, then retry with --confirm.",
            )
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_task(connection, task_id)
            self._require_revision(row, expected_revision)
            if row["archived_at"] is not None or row["status"] == "complete":
                raise _invalid_state(task_id, str(row["status"]), "complete")
            blockers = [
                str(dependency["depends_on_task_id"])
                for dependency in connection.execute(
                    "SELECT d.depends_on_task_id FROM task_dependencies AS d "
                    "JOIN tasks AS t ON t.task_id = d.depends_on_task_id "
                    "WHERE d.task_id = ? AND t.status <> 'complete' "
                    "ORDER BY d.depends_on_task_id",
                    (task_id,),
                )
            ]
            if blockers:
                raise OffworkError(
                    "DEPENDENCY_NOT_COMPLETE",
                    "Task dependencies are not complete.",
                    exit_code=4,
                    details={"task_id": task_id, "blockers": blockers},
                    recovery="Complete the blocker tasks before this task.",
                )
            timestamp = _now()
            connection.execute(
                "UPDATE tasks SET status = 'complete', revision = revision + 1, "
                "updated_at = ? WHERE task_id = ?",
                (timestamp, task_id),
            )
            task = self._task_from_connection(connection, task_id)
            connection.commit()
        self._sync_registry_safely()
        return task

    def archive_task(
        self, task_id: str, *, expected_revision: Optional[int] = None
    ) -> Dict[str, Any]:
        return self._set_archived(task_id, True, expected_revision=expected_revision)

    def unarchive_task(
        self, task_id: str, *, expected_revision: Optional[int] = None
    ) -> Dict[str, Any]:
        return self._set_archived(task_id, False, expected_revision=expected_revision)

    def _set_archived(
        self,
        task_id: str,
        archived: bool,
        *,
        expected_revision: Optional[int],
    ) -> Dict[str, Any]:
        self._reset_warnings()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_task(connection, task_id)
            self._require_revision(row, expected_revision)
            is_archived = row["archived_at"] is not None
            if is_archived == archived:
                raise _invalid_state(
                    task_id,
                    str(row["status"]),
                    "archive" if archived else "unarchive",
                )
            timestamp = _now()
            connection.execute(
                "UPDATE tasks SET archived_at = ?, revision = revision + 1, "
                "updated_at = ? WHERE task_id = ?",
                (timestamp if archived else None, timestamp, task_id),
            )
            task = self._task_from_connection(connection, task_id)
            connection.commit()
        self._sync_registry_safely()
        return task

    def add_dependency(
        self,
        task_id: str,
        dependency_id: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._reset_warnings()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._require_task(connection, task_id)
            self._require_task(connection, dependency_id)
            self._require_revision(task, expected_revision)
            if task_id == dependency_id or self._path_exists(
                connection, dependency_id, task_id
            ):
                raise OffworkError(
                    "DEPENDENCY_CYCLE",
                    "Dependency would create a cycle.",
                    exit_code=4,
                    details={"task_id": task_id, "dependency_id": dependency_id},
                    recovery="Choose a prerequisite that does not depend on this task.",
                )
            if connection.execute(
                "SELECT 1 FROM task_dependencies WHERE task_id = ? "
                "AND depends_on_task_id = ?",
                (task_id, dependency_id),
            ).fetchone() is not None:
                raise OffworkError(
                    "DEPENDENCY_EXISTS",
                    "Dependency already exists.",
                    exit_code=4,
                    details={"task_id": task_id, "dependency_id": dependency_id},
                    recovery="No change is required.",
                )
            timestamp = _now()
            connection.execute(
                "INSERT INTO task_dependencies(task_id, depends_on_task_id, created_at) "
                "VALUES (?, ?, ?)",
                (task_id, dependency_id, timestamp),
            )
            connection.execute(
                "UPDATE tasks SET revision = revision + 1, updated_at = ? "
                "WHERE task_id = ?",
                (timestamp, task_id),
            )
            result = self._task_from_connection(connection, task_id)
            connection.commit()
        self._sync_registry_safely()
        return result

    def _path_exists(
        self, connection: sqlite3.Connection, start: str, target: str
    ) -> bool:
        row = connection.execute(
            "WITH RECURSIVE reachable(task_id) AS ("
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? "
            "UNION "
            "SELECT d.depends_on_task_id FROM task_dependencies AS d "
            "JOIN reachable AS r ON d.task_id = r.task_id"
            ") SELECT 1 FROM reachable WHERE task_id = ? LIMIT 1",
            (start, target),
        ).fetchone()
        return row is not None

    def remove_dependency(
        self,
        task_id: str,
        dependency_id: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._reset_warnings()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._require_task(connection, task_id)
            self._require_task(connection, dependency_id)
            self._require_revision(task, expected_revision)
            cursor = connection.execute(
                "DELETE FROM task_dependencies WHERE task_id = ? "
                "AND depends_on_task_id = ?",
                (task_id, dependency_id),
            )
            if cursor.rowcount == 0:
                raise OffworkError(
                    "DEPENDENCY_NOT_FOUND",
                    "Dependency does not exist.",
                    exit_code=4,
                    details={"task_id": task_id, "dependency_id": dependency_id},
                    recovery="Inspect the task before retrying.",
                )
            timestamp = _now()
            connection.execute(
                "UPDATE tasks SET revision = revision + 1, updated_at = ? "
                "WHERE task_id = ?",
                (timestamp, task_id),
            )
            result = self._task_from_connection(connection, task_id)
            connection.commit()
        self._sync_registry_safely()
        return result

    def project_status(self) -> Dict[str, Any]:
        with self.storage.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at, task_id"
            ).fetchall()
            tasks = self._decorate_rows(connection, rows)
            latest_capsule_row = connection.execute(
                "SELECT capsule_id, task_id, managed_session_id, status, "
                "content_hash, archive_path, created_at FROM capsules "
                "WHERE status IN ('validated', 'fresh_verified') "
                "AND archived_at IS NULL ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        active = [task for task in tasks if task["archived_at"] is None]
        current_focus = next(
            (task for task in active if task["status"] == "in_progress"), None
        )
        if current_focus is None:
            current_focus = next(
                (
                    task
                    for preferred in ("todo", "review")
                    for task in active
                    if task["status"] == preferred
                ),
                None,
            )
        grouped = {
            state: [task for task in active if task["computed_state"] == state]
            for state in ("actionable", "blocked", "waiting")
        }
        attached_sessions: List[Dict[str, Any]] = []
        primary_session: Optional[Dict[str, Any]] = None
        if current_focus is not None:
            from .sessions import SessionService

            attached_sessions = SessionService(self.storage.project_root).list(
                current_focus["task_id"]
            )
            primary_session = next(
                (session for session in attached_sessions if session["is_primary"]),
                None,
            )
        return {
            "project_id": self.storage.project_id,
            "canonical_path": str(self.storage.project_root),
            "current_focus": current_focus,
            "recommended_next": grouped["actionable"][0]
            if grouped["actionable"]
            else None,
            "counts": {state: len(items) for state, items in grouped.items()},
            "tasks": grouped,
            "primary_session": primary_session,
            "attached_sessions": attached_sessions,
            "latest_verified_capsule": (
                {
                    "capsule_id": str(latest_capsule_row["capsule_id"]),
                    "task_id": latest_capsule_row["task_id"],
                    "managed_session_id": latest_capsule_row[
                        "managed_session_id"
                    ],
                    "status": str(latest_capsule_row["status"]),
                    "content_hash": str(latest_capsule_row["content_hash"]),
                    "archive_path": str(latest_capsule_row["archive_path"]),
                    "created_at": str(latest_capsule_row["created_at"]),
                }
                if latest_capsule_row is not None
                else None
            ),
        }


def registry_status() -> Dict[str, Any]:
    registry = initialize_global_registry()
    with registry.connect() as connection:
        projects = connection.execute(
            "SELECT project_id, canonical_path, state_database_path, last_seen_at "
            "FROM projects ORDER BY canonical_path"
        ).fetchall()
        summaries = connection.execute(
            "SELECT task_id, project_id, title, status, revision, archived_at, "
            "updated_at FROM task_summaries ORDER BY updated_at, task_id"
        ).fetchall()
    by_project: Dict[str, List[Dict[str, Any]]] = {}
    for row in summaries:
        by_project.setdefault(str(row["project_id"]), []).append(
            {
                "task_id": str(row["task_id"]),
                "title": str(row["title"]),
                "status": str(row["status"]),
                "revision": int(row["revision"]),
                "archived_at": row["archived_at"],
                "updated_at": str(row["updated_at"]),
            }
        )
    return {
        "projects": [
            {
                "project_id": str(row["project_id"]),
                "canonical_path": str(row["canonical_path"]),
                "state_database_path": str(row["state_database_path"]),
                "last_seen_at": str(row["last_seen_at"]),
                "tasks": by_project.get(str(row["project_id"]), []),
            }
            for row in projects
        ]
    }
