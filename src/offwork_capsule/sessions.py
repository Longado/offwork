from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .state import OffworkError
from .storage import initialize_project_storage


PROVIDERS = {"codex", "claude"}
SESSION_STATES = {"attached", "active", "hibernated", "stopped", "failed"}
PROVIDER_ENV = {
    "codex": "OFFWORK_CODEX_SESSION_ID",
    "claude": "OFFWORK_CLAUDE_SESSION_ID",
}
REOPEN_STARTUP_GRACE_SECONDS = 0.2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _error(
    code: str,
    message: str,
    exit_code: int,
    *,
    details: Optional[Mapping[str, Any]] = None,
    recovery: str = "",
) -> OffworkError:
    return OffworkError(
        code,
        message,
        exit_code=exit_code,
        details=details,
        recovery=recovery,
    )


def parse_tmux_handle(value: str) -> Tuple[str, str]:
    if not isinstance(value, str) or ":" not in value:
        raise _error(
            "INVALID_TMUX_HANDLE",
            "Tmux handle must be an absolute SOCKET:NAME pair.",
            2,
            details={"value": value},
            recovery="Pass a handle such as /tmp/offwork.sock:agent-name.",
        )
    socket_value, name = value.rsplit(":", 1)
    socket_path = Path(socket_value).expanduser()
    if not socket_value or not socket_path.is_absolute() or not name:
        raise _error(
            "INVALID_TMUX_HANDLE",
            "Tmux handle must contain an absolute socket path and a name.",
            2,
            details={"value": value},
            recovery="Pass a handle such as /tmp/offwork.sock:agent-name.",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise _error(
            "INVALID_TMUX_HANDLE",
            "Tmux session name must not contain control characters.",
            2,
            details={"value": value},
            recovery="Use a printable tmux session name.",
        )
    return str(socket_path.resolve()), name


def session_row_payload(row: sqlite3.Row, project_root: Path) -> Dict[str, Any]:
    cwd = Path(str(row["cwd"])).resolve()
    canonical_project = Path(project_root).resolve()
    if cwd != canonical_project:
        raise _error(
            "PROJECT_PATH_MISMATCH",
            "Managed session belongs to a different project path.",
            4,
            details={
                "managed_session_id": str(row["managed_session_id"]),
                "session_cwd": str(cwd),
                "project_path": str(canonical_project),
            },
            recovery="Run the command with the session's exact project path.",
        )
    provider = str(row["provider"])
    if provider not in PROVIDERS:
        raise _error(
            "SESSION_ID_CONFLICT",
            "Managed session contains an invalid provider.",
            4,
            details={"managed_session_id": str(row["managed_session_id"])},
            recovery="Repair the session record before retrying.",
        )
    state = str(row["state"])
    if state not in SESSION_STATES:
        raise _error(
            "SESSION_ID_CONFLICT",
            "Managed session contains an invalid state.",
            4,
            details={"managed_session_id": str(row["managed_session_id"])},
            recovery="Repair the session record before retrying.",
        )
    is_primary = row["is_primary"]
    if is_primary not in (0, 1):
        raise _error(
            "SESSION_ID_CONFLICT",
            "Managed session contains an invalid primary flag.",
            4,
            details={"managed_session_id": str(row["managed_session_id"])},
            recovery="Repair the session record before retrying.",
        )
    return {
        "managed_session_id": str(row["managed_session_id"]),
        "task_id": str(row["task_id"]),
        "provider": provider,
        "native_session_id": row["native_session_id"],
        "tmux_socket": row["tmux_socket"],
        "tmux_name": row["tmux_name"],
        "cwd": str(cwd),
        "is_primary": bool(is_primary),
        "parent_session_id": row["parent_session_id"],
        "state": state,
        "revision": int(row["revision"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


class SessionService:
    def __init__(self, project_root: Path) -> None:
        self.storage = initialize_project_storage(Path(project_root))

    def _require_task(self, connection: sqlite3.Connection, task_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone() is None:
            raise _error(
                "TASK_NOT_FOUND",
                "Task not found: %s" % task_id,
                3,
                details={"task_id": task_id},
                recovery="Check the task ID with `offwork task list`.",
            )

    def _require_session(
        self, connection: sqlite3.Connection, managed_session_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM sessions WHERE managed_session_id = ?",
            (managed_session_id,),
        ).fetchone()
        if row is None:
            raise _error(
                "SESSION_NOT_FOUND",
                "Managed session not found: %s" % managed_session_id,
                3,
                details={"managed_session_id": managed_session_id},
                recovery="Check the managed ID with `offwork session list`.",
            )
        session_row_payload(row, self.storage.project_root)
        return row

    def _translate_integrity_error(self, error: sqlite3.IntegrityError) -> OffworkError:
        message = str(error)
        if "sessions.provider, sessions.native_session_id" in message:
            return _error(
                "SESSION_ID_CONFLICT",
                "Native session ID is already attached for this provider.",
                4,
                details={},
                recovery="Use the existing managed session or a different native ID.",
            )
        if "sessions.tmux_socket, sessions.tmux_name" in message:
            return _error(
                "TMUX_SESSION_CONFLICT",
                "Tmux socket and session name are already attached.",
                4,
                details={},
                recovery="Use the existing managed session or another tmux handle.",
            )
        if "sessions.task_id" in message:
            return _error(
                "ACTIVE_PRIMARY_EXISTS",
                "Task already has a primary managed session.",
                4,
                details={},
                recovery="Use `offwork session primary` to switch atomically.",
            )
        if "session parent" in message:
            return _error(
                "SESSION_ID_CONFLICT",
                "Parent session must belong to the same task and remain acyclic.",
                4,
                details={},
                recovery="Choose an existing parent session on the same task.",
            )
        return _error(
            "SESSION_ID_CONFLICT",
            "Managed session binding conflicts with existing state.",
            4,
            details={"reason": message},
            recovery="Inspect existing managed sessions before retrying.",
        )

    def attach(
        self,
        task_id: str,
        provider: str,
        *,
        native_id: Optional[str] = None,
        tmux: Optional[str] = None,
        parent_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if provider not in PROVIDERS:
            raise _error(
                "INVALID_PROVIDER",
                "Provider must be codex or claude.",
                2,
                details={"provider": provider},
                recovery="Choose --tool codex or --tool claude.",
            )
        clean_native_id = native_id.strip() if isinstance(native_id, str) else ""
        if not clean_native_id:
            clean_native_id = os.environ.get(PROVIDER_ENV[provider], "").strip()
        socket: Optional[str] = None
        name: Optional[str] = None
        if tmux is not None:
            socket, name = parse_tmux_handle(tmux)
            self._validate_attach_tmux(socket, name)

        managed_session_id = "msn_" + uuid.uuid4().hex
        timestamp = _now()
        try:
            with self.storage.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_task(connection, task_id)
                if parent_session_id is not None:
                    parent = self._require_session(connection, parent_session_id)
                    if str(parent["task_id"]) != task_id:
                        raise _error(
                            "SESSION_ID_CONFLICT",
                            "Parent session belongs to another task.",
                            4,
                            details={
                                "parent_session_id": parent_session_id,
                                "task_id": task_id,
                            },
                            recovery="Choose a parent attached to the same task.",
                        )
                is_primary = (
                    connection.execute(
                        "SELECT 1 FROM sessions WHERE task_id = ? AND is_primary = 1",
                        (task_id,),
                    ).fetchone()
                    is None
                )
                connection.execute(
                    "INSERT INTO sessions("
                    "managed_session_id, task_id, provider, native_session_id, "
                    "tmux_socket, tmux_name, cwd, is_primary, parent_session_id, "
                    "state, revision, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'attached', 1, ?, ?)",
                    (
                        managed_session_id,
                        task_id,
                        provider,
                        clean_native_id or None,
                        socket,
                        name,
                        str(self.storage.project_root),
                        int(is_primary),
                        parent_session_id,
                        timestamp,
                        timestamp,
                    ),
                )
                row = self._require_session(connection, managed_session_id)
                result = session_row_payload(row, self.storage.project_root)
                connection.commit()
        except sqlite3.IntegrityError as error:
            raise self._translate_integrity_error(error) from error
        return result

    def list(self, task_id: str) -> List[Dict[str, Any]]:
        with self.storage.connect() as connection:
            self._require_task(connection, task_id)
            rows = connection.execute(
                "SELECT * FROM sessions WHERE task_id = ? "
                "ORDER BY is_primary DESC, created_at, managed_session_id",
                (task_id,),
            ).fetchall()
            return [
                session_row_payload(row, self.storage.project_root) for row in rows
            ]

    def set_primary(
        self,
        managed_session_id: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_session(connection, managed_session_id)
            actual_revision = int(row["revision"])
            if expected_revision is not None and actual_revision != expected_revision:
                raise _error(
                    "STALE_REVISION",
                    "Managed session revision is %d, not %d."
                    % (actual_revision, expected_revision),
                    4,
                    details={
                        "managed_session_id": managed_session_id,
                        "expected_revision": expected_revision,
                        "actual_revision": actual_revision,
                    },
                    recovery="Reload the session and retry with its current revision.",
                )
            task_id = str(row["task_id"])
            timestamp = _now()
            connection.execute(
                "UPDATE sessions SET is_primary = 0, revision = revision + 1, "
                "updated_at = ? WHERE task_id = ? AND is_primary = 1 "
                "AND managed_session_id <> ?",
                (timestamp, task_id, managed_session_id),
            )
            if not bool(row["is_primary"]):
                connection.execute(
                    "UPDATE sessions SET is_primary = 1, revision = revision + 1, "
                    "updated_at = ? WHERE managed_session_id = ?",
                    (timestamp, managed_session_id),
                )
            updated = self._require_session(connection, managed_session_id)
            result = session_row_payload(updated, self.storage.project_root)
            connection.commit()
            return result

    def _binary(self, name: str) -> str:
        resolved = shutil.which(name)
        if resolved is None:
            raise _error(
                "BINARY_NOT_FOUND",
                "Required executable is unavailable: %s" % name,
                5,
                details={"binary": name},
                recovery="Install the executable or fix PATH before retrying.",
            )
        return resolved

    def _tmux_binding(self, row: sqlite3.Row) -> Tuple[str, str]:
        socket = row["tmux_socket"]
        name = row["tmux_name"]
        if not isinstance(socket, str) or not socket or not isinstance(name, str) or not name:
            raise _error(
                "TMUX_HANDLE_REQUIRED",
                "Managed session has no tmux handle.",
                4,
                details={"managed_session_id": str(row["managed_session_id"])},
                recovery="Attach a managed session with --tmux SOCKET:NAME.",
            )
        return socket, name

    def _run_process(
        self,
        argv: Sequence[str],
        *,
        capture_output: bool,
        terminal_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        options: Dict[str, Any] = {
            "cwd": str(self.storage.project_root),
            "shell": False,
            "check": False,
            "text": True,
        }
        if capture_output:
            options["capture_output"] = True
        elif terminal_output:
            options["stdin"] = sys.stdin
            options["stdout"] = sys.stderr
            options["stderr"] = sys.stderr
        else:
            options["capture_output"] = False
        try:
            return subprocess.run(list(argv), **options)
        except OSError as error:
            raise _error(
                "BINARY_NOT_FOUND",
                "Required executable could not be started: %s" % argv[0],
                5,
                details={"binary": argv[0], "reason": str(error)},
                recovery="Install the executable or fix PATH before retrying.",
            ) from error

    def _probe(self, tmux_binary: str, socket: str, name: str) -> bool:
        result = self._run_process(
            [tmux_binary, "-S", socket, "has-session", "-t", "=" + name],
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            stderr = (result.stderr or "").lower()
            missing_markers = (
                "can't find session",
                "no server running",
                "no such file or directory",
            )
            if any(marker in stderr for marker in missing_markers):
                return False
        raise _error(
            "TMUX_PROBE_FAILED",
            "Unable to inspect the tmux session.",
            5,
            details={"returncode": result.returncode, "stderr": result.stderr},
            recovery="Check the tmux socket and retry.",
        )

    def _verify_existing_tmux_cwd(
        self, tmux_binary: str, socket: str, name: str
    ) -> None:
        result = self._run_process(
            [
                tmux_binary,
                "-S",
                socket,
                "display-message",
                "-p",
                "-t",
                "=" + name + ":.",
                "#{pane_current_path}",
            ],
            capture_output=True,
        )
        pane_cwd = result.stdout.strip() if result.stdout is not None else ""
        if result.returncode != 0 or not pane_cwd or not Path(pane_cwd).is_absolute():
            raise _error(
                "TMUX_PROBE_FAILED",
                "Unable to verify the tmux pane project path.",
                5,
                details={
                    "returncode": result.returncode,
                    "stderr": result.stderr,
                    "tmux_socket": socket,
                    "tmux_name": name,
                },
                recovery="Inspect the tmux pane and retry.",
            )
        canonical_pane_cwd = Path(pane_cwd).resolve()
        if canonical_pane_cwd != self.storage.project_root:
            raise _error(
                "PROJECT_PATH_MISMATCH",
                "Tmux pane belongs to a different project path.",
                4,
                details={
                    "tmux_socket": socket,
                    "tmux_name": name,
                    "pane_cwd": str(canonical_pane_cwd),
                    "project_path": str(self.storage.project_root),
                },
                recovery="Attach a tmux session whose pane cwd is the project root.",
            )

    def _validate_attach_tmux(self, socket: str, name: str) -> None:
        try:
            Path(socket).lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise _error(
                "TMUX_PROBE_FAILED",
                "Unable to inspect the tmux socket path.",
                5,
                details={"tmux_socket": socket, "reason": str(error)},
                recovery="Check the tmux socket permissions and retry.",
            ) from error
        tmux_binary = self._binary("tmux")
        if not self._probe(tmux_binary, socket, name):
            return
        self._verify_existing_tmux_cwd(tmux_binary, socket, name)

    def _attach_existing(self, tmux_binary: str, socket: str, name: str) -> None:
        tmux_environment = os.environ.get("TMUX", "").strip()
        if tmux_environment:
            current_socket_value = tmux_environment.split(",", 1)[0].strip()
            current_socket_path = Path(current_socket_value).expanduser()
            if not current_socket_value or not current_socket_path.is_absolute():
                raise _error(
                    "TMUX_ENV_INVALID",
                    "Current TMUX environment does not identify an absolute socket.",
                    5,
                    details={"tmux": tmux_environment},
                    recovery="Start from a valid tmux client or unset the stale TMUX value.",
                )
            current_socket = str(current_socket_path.resolve())
            if current_socket != socket:
                raise _error(
                    "TMUX_CROSS_SERVER_ATTACH",
                    "Refusing to attach a different tmux server from inside tmux.",
                    4,
                    details={
                        "current_socket": current_socket,
                        "target_socket": socket,
                    },
                    recovery="Leave the current tmux client, then enter the target session.",
                )
            result = self._run_process(
                [
                    tmux_binary,
                    "-S",
                    socket,
                    "switch-client",
                    "-t",
                    "=" + name,
                ],
                capture_output=True,
            )
            if result.returncode != 0:
                raise _error(
                    "TMUX_SWITCH_FAILED",
                    "Tmux client switch failed.",
                    5,
                    details={"returncode": result.returncode, "stderr": result.stderr},
                    recovery="Inspect the current tmux client and retry.",
                )
            return
        if not sys.stdin.isatty():
            raise _error(
                "ATTACH_REQUIRES_TTY",
                "Entering an existing tmux session requires an interactive TTY.",
                4,
                details={"tmux_socket": socket, "tmux_name": name},
                recovery="Run `offwork session enter` from an interactive terminal.",
            )
        result = self._run_process(
            [tmux_binary, "-S", socket, "attach-session", "-t", "=" + name],
            capture_output=False,
            terminal_output=True,
        )
        if result.returncode != 0:
            raise _error(
                "TMUX_ATTACH_FAILED",
                "Tmux attach failed.",
                5,
                details={"returncode": result.returncode},
                recovery="Inspect the tmux session and retry.",
            )

    def _mark_active(self, managed_session_id: str) -> Dict[str, Any]:
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, managed_session_id)
            timestamp = _now()
            connection.execute(
                "UPDATE sessions SET state = 'active', revision = revision + 1, "
                "updated_at = ? WHERE managed_session_id = ?",
                (timestamp, managed_session_id),
            )
            row = self._require_session(connection, managed_session_id)
            result = session_row_payload(row, self.storage.project_root)
            connection.commit()
            return result

    def enter(self, managed_session_id: str) -> Dict[str, Any]:
        with self.storage.connect() as connection:
            row = self._require_session(connection, managed_session_id)
            socket, name = self._tmux_binding(row)
        tmux_binary = self._binary("tmux")
        if not self._probe(tmux_binary, socket, name):
            raise _error(
                "TMUX_SESSION_NOT_FOUND",
                "Tmux session does not exist.",
                3,
                details={"tmux_socket": socket, "tmux_name": name},
                recovery="Use `offwork session reopen` for an explicit native resume.",
            )
        self._verify_existing_tmux_cwd(tmux_binary, socket, name)
        self._attach_existing(tmux_binary, socket, name)
        return self._mark_active(managed_session_id)

    def _adapter_argv(self, row: Mapping[str, Any]) -> List[str]:
        native_id = row["native_session_id"]
        if not isinstance(native_id, str) or not native_id:
            raise _error(
                "NATIVE_SESSION_REQUIRED",
                "Managed session has no explicit native session ID.",
                4,
                details={"managed_session_id": str(row["managed_session_id"])},
                recovery="Attach the native ID explicitly before reopening.",
            )
        provider = str(row["provider"])
        if provider not in PROVIDERS:
            raise _error(
                "SESSION_ID_CONFLICT",
                "Managed session contains an invalid provider.",
                4,
                details={"managed_session_id": str(row["managed_session_id"])},
                recovery="Repair the session record before retrying.",
            )
        binary = self._binary(provider)
        if provider == "codex":
            return [
                binary,
                "resume",
                "-C",
                str(self.storage.project_root),
                "--no-alt-screen",
                native_id,
            ]
        if provider == "claude":
            return [binary, "--resume", native_id]
        raise AssertionError("unreachable provider")

    def reopen(self, managed_session_id: str) -> Dict[str, Any]:
        with self.storage.connect() as connection:
            row = self._require_session(connection, managed_session_id)
            socket, name = self._tmux_binding(row)
            row_values = dict(row)
        tmux_binary = self._binary("tmux")
        if self._probe(tmux_binary, socket, name):
            self._verify_existing_tmux_cwd(tmux_binary, socket, name)
            self._attach_existing(tmux_binary, socket, name)
            return self._mark_active(managed_session_id)

        adapter = self._adapter_argv(row_values)
        result = self._run_process(
            [
                tmux_binary,
                "-S",
                socket,
                "new-session",
                "-d",
                "-s",
                name,
                "-c",
                str(self.storage.project_root),
                "--",
                *adapter,
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            raise _error(
                "NATIVE_RESUME_FAILED",
                "Native session resume could not be started.",
                5,
                details={"returncode": result.returncode, "stderr": result.stderr},
                recovery="Check the provider and tmux output before retrying.",
            )
        time.sleep(REOPEN_STARTUP_GRACE_SECONDS)
        if not self._probe(tmux_binary, socket, name):
            raise _error(
                "NATIVE_RESUME_FAILED",
                "Native session exited before startup verification completed.",
                5,
                details={"tmux_socket": socket, "tmux_name": name},
                recovery="Check the native session ID and provider output before retrying.",
            )
        return self._mark_active(managed_session_id)
