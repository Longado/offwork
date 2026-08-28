from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from offwork_capsule.sessions import SessionService, parse_tmux_handle
from offwork_capsule.state import OffworkError, StateService


ROOT = Path(__file__).parents[1]


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    return subprocess.run(
        [sys.executable, "-m", "offwork_capsule.cli", *arguments],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=tmp_path,
    )


def _json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "schema_version",
        "command",
        "ok",
        "data",
        "meta",
        "warnings",
        "error",
    }
    return payload


def _task(tmp_path: Path, project: Path, title: str = "task") -> dict[str, Any]:
    result = _run(
        tmp_path,
        "task",
        "add",
        title,
        "--goal",
        "finish it",
        "--project",
        str(project),
        "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return _json(result)["data"]


def test_cli_attaches_multiple_sessions_and_switches_one_primary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = _task(tmp_path, project)

    first_result = _run(
        tmp_path,
        "session",
        "attach",
        "--task",
        task["task_id"],
        "--tool",
        "codex",
        "--native-id",
        "codex-1",
        "--project",
        str(project),
        "--json",
    )
    second_result = _run(
        tmp_path,
        "session",
        "attach",
        "--task",
        task["task_id"],
        "--tool",
        "claude",
        "--native-id",
        "claude-1",
        "--project",
        str(project),
        "--json",
    )

    first = _json(first_result)["data"]
    second = _json(second_result)["data"]
    assert first_result.returncode == second_result.returncode == 0
    assert first["managed_session_id"].startswith("msn_")
    assert first["is_primary"] is True
    assert second["is_primary"] is False

    switched = _run(
        tmp_path,
        "session",
        "primary",
        second["managed_session_id"],
        "--revision",
        str(second["revision"]),
        "--project",
        str(project),
        "--json",
    )
    assert switched.returncode == 0
    assert _json(switched)["data"]["is_primary"] is True

    listed = _run(
        tmp_path,
        "session",
        "list",
        "--task",
        task["task_id"],
        "--project",
        str(project),
        "--json",
    )
    sessions = _json(listed)["data"]["sessions"]
    assert sum(item["is_primary"] for item in sessions) == 1
    assert next(item for item in sessions if item["is_primary"])[
        "managed_session_id"
    ] == second["managed_session_id"]


def test_stale_primary_switch_is_atomic(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    service = SessionService(project)
    first = service.attach(task["task_id"], "codex")
    second = service.attach(task["task_id"], "claude")

    with pytest.raises(OffworkError) as stale:
        service.set_primary(
            second["managed_session_id"], expected_revision=second["revision"] + 1
        )

    assert stale.value.code == "STALE_REVISION"
    sessions = service.list(task["task_id"])
    assert next(item for item in sessions if item["is_primary"])[
        "managed_session_id"
    ] == first["managed_session_id"]


def test_native_and_tmux_identity_conflicts_fail_closed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = _task(tmp_path, project)
    other = _task(tmp_path, project, "other")
    socket = tmp_path / "tmux.sock"

    first = _run(
        tmp_path,
        "session",
        "attach",
        "--task",
        task["task_id"],
        "--tool",
        "codex",
        "--native-id",
        "native-1",
        "--tmux",
        f"{socket}:agent:one",
        "--project",
        str(project),
        "--json",
    )
    assert first.returncode == 0

    native_conflict = _run(
        tmp_path,
        "session",
        "attach",
        "--task",
        other["task_id"],
        "--tool",
        "codex",
        "--native-id",
        "native-1",
        "--project",
        str(project),
        "--json",
    )
    assert native_conflict.returncode == 4
    assert _json(native_conflict)["error"]["code"] == "SESSION_ID_CONFLICT"

    tmux_conflict = _run(
        tmp_path,
        "session",
        "attach",
        "--task",
        other["task_id"],
        "--tool",
        "claude",
        "--tmux",
        f"{socket}:agent:one",
        "--project",
        str(project),
        "--json",
    )
    assert tmux_conflict.returncode == 4
    assert _json(tmux_conflict)["error"]["code"] == "TMUX_SESSION_CONFLICT"


def test_native_id_uses_provider_specific_trusted_env_with_explicit_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("OFFWORK_CODEX_SESSION_ID", "from-env")
    task = StateService(project).add_task("one", "finish")
    service = SessionService(project)

    env_bound = service.attach(task["task_id"], "codex")
    explicit = service.attach(task["task_id"], "codex", native_id="explicit")

    assert env_bound["native_session_id"] == "from-env"
    assert explicit["native_session_id"] == "explicit"


def test_tmux_handle_uses_last_colon_and_rejects_unsafe_values(tmp_path: Path) -> None:
    socket, name = parse_tmux_handle(f"{tmp_path / 'socket'}:group:agent")
    assert socket == str(Path(str(tmp_path / "socket") + ":group").resolve())
    assert name == "agent"

    with pytest.raises(OffworkError) as relative:
        parse_tmux_handle("relative.sock:agent")
    assert relative.value.exit_code == 2

    with pytest.raises(OffworkError):
        parse_tmux_handle(f"{tmp_path / 'socket'}:bad\nname")


def test_parent_must_exist_in_same_task_and_database_rejects_cycle(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first_task = StateService(project).add_task("one", "finish")
    second_task = StateService(project).add_task("two", "finish")
    service = SessionService(project)
    parent = service.attach(first_task["task_id"], "codex")

    with pytest.raises(OffworkError) as cross_task:
        service.attach(
            second_task["task_id"],
            "claude",
            parent_session_id=parent["managed_session_id"],
        )
    assert cross_task.value.code == "SESSION_ID_CONFLICT"

    child = service.attach(
        first_task["task_id"],
        "claude",
        parent_session_id=parent["managed_session_id"],
    )
    with service.storage.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE managed_session_id = ?",
            (child["managed_session_id"], parent["managed_session_id"]),
        )


def test_first_primary_is_unique_under_concurrent_attach(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    barrier = threading.Barrier(2)
    results: list[dict[str, Any]] = []

    def attach(provider: str) -> None:
        barrier.wait()
        results.append(SessionService(project).attach(task["task_id"], provider))

    threads = [
        threading.Thread(target=attach, args=("codex",)),
        threading.Thread(target=attach, args=("claude",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert sum(item["is_primary"] for item in results) == 1


def test_cwd_tampering_blocks_reads_and_operations(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    service = SessionService(project)
    session = service.attach(task["task_id"], "codex")
    with service.storage.connect() as connection:
        connection.execute(
            "UPDATE sessions SET cwd = ? WHERE managed_session_id = ?",
            (str(tmp_path), session["managed_session_id"]),
        )

    with pytest.raises(OffworkError) as caught:
        service.list(task["task_id"])
    assert caught.value.code == "PROJECT_PATH_MISMATCH"
    assert caught.value.exit_code == 4


def test_legacy_invalid_provider_fails_closed_on_read(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    service = SessionService(project)
    session = service.attach(task["task_id"], "codex")
    with service.storage.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE sessions SET provider = 'unknown' WHERE managed_session_id = ?",
            (session["managed_session_id"],),
        )

    with pytest.raises(OffworkError) as invalid:
        service.list(task["task_id"])
    assert invalid.value.code == "SESSION_ID_CONFLICT"


def test_legacy_invalid_primary_flag_fails_closed_on_read(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    service = SessionService(project)
    session = service.attach(task["task_id"], "codex")
    with service.storage.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE sessions SET is_primary = 2 WHERE managed_session_id = ?",
            (session["managed_session_id"],),
        )

    with pytest.raises(OffworkError) as invalid:
        service.list(task["task_id"])
    assert invalid.value.code == "SESSION_ID_CONFLICT"


class _FakeCompleted:
    def __init__(
        self, returncode: int, stderr: str = "", stdout: str = ""
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def test_attach_existing_tmux_verifies_pane_cwd_before_registering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket_path = tmp_path / "tmux.sock"
    socket_path.touch()
    socket = str(socket_path.resolve())
    calls: list[list[str]] = []
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: "/bin/tmux")

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(argv)
        if "display-message" in argv:
            return _FakeCompleted(0, stdout=str(project.resolve()) + "\n")
        return _FakeCompleted(0)

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    attached = SessionService(project).attach(
        task["task_id"], "codex", tmux=f"{socket}:agent"
    )

    assert attached["tmux_socket"] == socket
    assert calls == [
        ["/bin/tmux", "-S", socket, "has-session", "-t", "=agent"],
        [
            "/bin/tmux",
            "-S",
            socket,
            "display-message",
            "-p",
            "-t",
            "=agent:.",
            "#{pane_current_path}",
        ],
    ]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")
def test_attach_existing_real_tmux_reads_current_pane_cwd(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket_path = Path("/tmp") / ("offwork-tmux-" + uuid.uuid4().hex[:12] + ".sock")
    socket = str(socket_path.resolve())
    tmux = shutil.which("tmux")
    assert tmux is not None
    subprocess.run(
        [tmux, "-S", socket, "new-session", "-d", "-s", "agent", "-c", str(project)],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        attached = SessionService(project).attach(
            task["task_id"], "codex", tmux=f"{socket}:agent"
        )
        assert attached["cwd"] == str(project.resolve())
        assert attached["tmux_name"] == "agent"
    finally:
        subprocess.run(
            [tmux, "-S", socket, "kill-server"],
            check=False,
            capture_output=True,
            text=True,
        )
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


def test_attach_rejects_existing_tmux_from_another_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket_path = tmp_path / "tmux.sock"
    socket_path.touch()
    socket = str(socket_path.resolve())
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: "/bin/tmux")

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        if "display-message" in argv:
            return _FakeCompleted(0, stdout=str(tmp_path.resolve()) + "\n")
        return _FakeCompleted(0)

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    service = SessionService(project)
    with pytest.raises(OffworkError) as mismatch:
        service.attach(task["task_id"], "codex", tmux=f"{socket}:agent")

    assert mismatch.value.code == "PROJECT_PATH_MISMATCH"
    assert mismatch.value.exit_code == 4
    assert service.list(task["task_id"]) == []


def test_attach_allows_planned_missing_tmux_without_external_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket = str((tmp_path / "planned.sock").resolve())

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("missing planned socket must not require tmux")

    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", forbidden)
    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", forbidden)
    attached = SessionService(project).attach(
        task["task_id"], "codex", tmux=f"{socket}:planned"
    )

    assert attached["tmux_socket"] == socket
    assert attached["state"] == "attached"


def test_attach_allows_planned_name_missing_from_existing_tmux_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket_path = tmp_path / "tmux.sock"
    socket_path.touch()
    socket = str(socket_path.resolve())
    calls: list[list[str]] = []
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: "/bin/tmux")

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(argv)
        return _FakeCompleted(1, "can't find session")

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    attached = SessionService(project).attach(
        task["task_id"], "codex", tmux=f"{socket}:planned"
    )

    assert attached["state"] == "attached"
    assert calls == [
        ["/bin/tmux", "-S", socket, "has-session", "-t", "=planned"]
    ]


def test_attach_fails_closed_when_existing_tmux_cwd_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket_path = tmp_path / "tmux.sock"
    socket_path.touch()
    socket = str(socket_path.resolve())
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: "/bin/tmux")
    calls = 0

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        nonlocal calls
        calls += 1
        return _FakeCompleted(0 if calls == 1 else 2, "display failed")

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    with pytest.raises(OffworkError) as failed:
        SessionService(project).attach(
            task["task_id"], "codex", tmux=f"{socket}:agent"
        )

    assert failed.value.code == "TMUX_PROBE_FAILED"
    assert failed.value.exit_code == 5


def test_attach_treats_unrecognized_probe_failure_as_capability_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket_path = tmp_path / "tmux.sock"
    socket_path.touch()
    socket = str(socket_path.resolve())
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: "/bin/tmux")
    monkeypatch.setattr(
        "offwork_capsule.sessions.subprocess.run",
        lambda *args, **kwargs: _FakeCompleted(1, "Permission denied"),
    )

    with pytest.raises(OffworkError) as failed:
        SessionService(project).attach(
            task["task_id"], "codex", tmux=f"{socket}:agent"
        )

    assert failed.value.code == "TMUX_PROBE_FAILED"
    assert failed.value.exit_code == 5


@pytest.mark.parametrize("operation", ["enter", "reopen"])
def test_planned_handle_created_later_in_wrong_project_is_rejected_before_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket_path = tmp_path / "planned.sock"
    socket = str(socket_path.resolve())
    service = SessionService(project)
    session = service.attach(task["task_id"], "codex", tmux=f"{socket}:agent")
    socket_path.touch()
    calls: list[list[str]] = []
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: "/bin/tmux")

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(argv)
        if "display-message" in argv:
            return _FakeCompleted(0, stdout=str(tmp_path.resolve()) + "\n")
        return _FakeCompleted(0)

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    with pytest.raises(OffworkError) as mismatch:
        getattr(service, operation)(session["managed_session_id"])

    assert mismatch.value.code == "PROJECT_PATH_MISMATCH"
    assert mismatch.value.exit_code == 4
    assert calls == [
        ["/bin/tmux", "-S", socket, "has-session", "-t", "=agent"],
        [
            "/bin/tmux",
            "-S",
            socket,
            "display-message",
            "-p",
            "-t",
            "=agent:.",
            "#{pane_current_path}",
        ],
    ]


def test_enter_probes_then_attaches_with_exact_argv_and_no_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket = str((tmp_path / "tmux.sock").resolve())
    service = SessionService(project)
    session = service.attach(task["task_id"], "codex", tmux=f"{socket}:agent")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: "/bin/tmux")
    monkeypatch.setattr("offwork_capsule.sessions.sys.stdin.isatty", lambda: True)

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append((argv, kwargs))
        if "display-message" in argv:
            return _FakeCompleted(0, stdout=str(project.resolve()) + "\n")
        return _FakeCompleted(0)

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    entered = service.enter(session["managed_session_id"])

    assert entered["state"] == "active"
    assert calls[0][0] == ["/bin/tmux", "-S", socket, "has-session", "-t", "=agent"]
    assert calls[1][0] == [
        "/bin/tmux",
        "-S",
        socket,
        "display-message",
        "-p",
        "-t",
        "=agent:.",
        "#{pane_current_path}",
    ]
    assert calls[2][0] == ["/bin/tmux", "-S", socket, "attach-session", "-t", "=agent"]
    assert all(call[1]["shell"] is False for call in calls)
    assert all(call[1]["cwd"] == str(project.resolve()) for call in calls)


def test_enter_from_same_tmux_server_switches_client_instead_of_attaching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket = str((tmp_path / "tmux.sock").resolve())
    service = SessionService(project)
    session = service.attach(task["task_id"], "codex", tmux=f"{socket}:agent")
    calls: list[list[str]] = []
    monkeypatch.setenv("TMUX", f"{socket},123,0")
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: "/bin/tmux")

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(argv)
        if "display-message" in argv:
            return _FakeCompleted(0, stdout=str(project.resolve()) + "\n")
        return _FakeCompleted(0)

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    entered = service.enter(session["managed_session_id"])

    assert entered["state"] == "active"
    assert calls == [
        ["/bin/tmux", "-S", socket, "has-session", "-t", "=agent"],
        [
            "/bin/tmux",
            "-S",
            socket,
            "display-message",
            "-p",
            "-t",
            "=agent:.",
            "#{pane_current_path}",
        ],
        ["/bin/tmux", "-S", socket, "switch-client", "-t", "=agent"],
    ]


def test_enter_from_different_tmux_server_fails_closed_without_nested_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket = str((tmp_path / "target.sock").resolve())
    other_socket = str((tmp_path / "current.sock").resolve())
    service = SessionService(project)
    session = service.attach(task["task_id"], "codex", tmux=f"{socket}:agent")
    calls: list[list[str]] = []
    monkeypatch.setenv("TMUX", f"{other_socket},123,0")
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: "/bin/tmux")

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(argv)
        if "display-message" in argv:
            return _FakeCompleted(0, stdout=str(project.resolve()) + "\n")
        return _FakeCompleted(0)

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    with pytest.raises(OffworkError) as cross_server:
        service.enter(session["managed_session_id"])

    assert cross_server.value.code == "TMUX_CROSS_SERVER_ATTACH"
    assert cross_server.value.exit_code == 4
    assert calls == [
        ["/bin/tmux", "-S", socket, "has-session", "-t", "=agent"],
        [
            "/bin/tmux",
            "-S",
            socket,
            "display-message",
            "-p",
            "-t",
            "=agent:.",
            "#{pane_current_path}",
        ],
    ]


def test_enter_distinguishes_missing_handle_from_no_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket = str((tmp_path / "tmux.sock").resolve())
    service = SessionService(project)
    session = service.attach(task["task_id"], "codex", tmux=f"{socket}:agent")
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: "/bin/tmux")

    monkeypatch.setattr(
        "offwork_capsule.sessions.subprocess.run",
        lambda *args, **kwargs: _FakeCompleted(1, "can't find session"),
    )
    with pytest.raises(OffworkError) as missing:
        service.enter(session["managed_session_id"])
    assert missing.value.code == "TMUX_SESSION_NOT_FOUND"
    assert missing.value.exit_code == 3

    def existing(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        if "display-message" in argv:
            return _FakeCompleted(0, stdout=str(project.resolve()) + "\n")
        return _FakeCompleted(0)

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", existing)
    monkeypatch.setattr("offwork_capsule.sessions.sys.stdin.isatty", lambda: False)
    with pytest.raises(OffworkError) as no_tty:
        service.enter(session["managed_session_id"])
    assert no_tty.value.code == "ATTACH_REQUIRES_TTY"


def test_binary_disappearing_after_resolution_is_capability_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket = str((tmp_path / "tmux.sock").resolve())
    service = SessionService(project)
    session = service.attach(task["task_id"], "codex", tmux=f"{socket}:agent")
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: "/bin/tmux")

    def missing(*args: Any, **kwargs: Any) -> _FakeCompleted:
        raise FileNotFoundError("disappeared")

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", missing)
    with pytest.raises(OffworkError) as caught:
        service.enter(session["managed_session_id"])

    assert caught.value.code == "BINARY_NOT_FOUND"
    assert caught.value.exit_code == 5


@pytest.mark.parametrize(
    ("provider", "adapter"),
    [
        ("codex", ["/bin/codex", "resume", "-C", "PROJECT", "--no-alt-screen", "native"]),
        ("claude", ["/bin/claude", "--resume", "native"]),
    ],
)
def test_reopen_missing_handle_uses_exact_provider_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    adapter: list[str],
) -> None:
    project = tmp_path / provider
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket = str((tmp_path / (provider + ".sock")).resolve())
    service = SessionService(project)
    session = service.attach(
        task["task_id"], provider, native_id="native", tmux=f"{socket}:agent"
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    paths = {"tmux": "/bin/tmux", "codex": "/bin/codex", "claude": "/bin/claude"}
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", paths.get)
    monkeypatch.setattr("offwork_capsule.sessions.time.sleep", lambda seconds: None)
    has_session_calls = 0

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        nonlocal has_session_calls
        calls.append((argv, kwargs))
        if "has-session" in argv:
            has_session_calls += 1
            if has_session_calls == 1:
                return _FakeCompleted(1, "can't find session")
        return _FakeCompleted(0)

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    reopened = service.reopen(session["managed_session_id"])

    expected_adapter = [str(project.resolve()) if item == "PROJECT" else item for item in adapter]
    assert calls[1][0] == [
        "/bin/tmux",
        "-S",
        socket,
        "new-session",
        "-d",
        "-s",
        "agent",
        "-c",
        str(project.resolve()),
        "--",
        *expected_adapter,
    ]
    assert calls[1][1]["shell"] is False
    assert reopened["state"] == "active"


def test_reopen_existing_tmux_never_invokes_native_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket = str((tmp_path / "tmux.sock").resolve())
    service = SessionService(project)
    session = service.attach(
        task["task_id"], "codex", native_id="native", tmux=f"{socket}:agent"
    )
    calls: list[list[str]] = []
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("offwork_capsule.sessions.sys.stdin.isatty", lambda: True)

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(argv)
        if "display-message" in argv:
            return _FakeCompleted(0, stdout=str(project.resolve()) + "\n")
        return _FakeCompleted(0)

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    service.reopen(session["managed_session_id"])

    assert len(calls) == 3
    assert calls[2][1:4] == ["-S", socket, "attach-session"]
    assert all("resume" not in call for call in calls)


def test_reopen_existing_handle_switches_same_server_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket = str((tmp_path / "tmux.sock").resolve())
    service = SessionService(project)
    session = service.attach(
        task["task_id"], "codex", native_id="native", tmux=f"{socket}:agent"
    )
    calls: list[list[str]] = []
    monkeypatch.setenv("TMUX", f"{socket},123,0")
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: f"/bin/{name}")

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(argv)
        if "display-message" in argv:
            return _FakeCompleted(0, stdout=str(project.resolve()) + "\n")
        return _FakeCompleted(0)

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    reopened = service.reopen(session["managed_session_id"])

    assert reopened["state"] == "active"
    assert calls == [
        ["/bin/tmux", "-S", socket, "has-session", "-t", "=agent"],
        [
            "/bin/tmux",
            "-S",
            socket,
            "display-message",
            "-p",
            "-t",
            "=agent:.",
            "#{pane_current_path}",
        ],
        ["/bin/tmux", "-S", socket, "switch-client", "-t", "=agent"],
    ]
    assert all("resume" not in call for call in calls)


def test_reopen_requires_binary_handle_and_native_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    service = SessionService(project)
    unbound = service.attach(task["task_id"], "codex")

    with pytest.raises(OffworkError) as no_handle:
        service.reopen(unbound["managed_session_id"])
    assert no_handle.value.code == "TMUX_HANDLE_REQUIRED"

    socket = str((tmp_path / "tmux.sock").resolve())
    bound = service.attach(task["task_id"], "claude", tmux=f"{socket}:agent")
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: None)
    with pytest.raises(OffworkError) as no_binary:
        service.reopen(bound["managed_session_id"])
    assert no_binary.value.code == "BINARY_NOT_FOUND"
    assert no_binary.value.exit_code == 5

    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        "offwork_capsule.sessions.subprocess.run",
        lambda *args, **kwargs: _FakeCompleted(1, "can't find session"),
    )
    with pytest.raises(OffworkError) as no_native:
        service.reopen(bound["managed_session_id"])
    assert no_native.value.code == "NATIVE_SESSION_REQUIRED"


def test_failed_native_resume_does_not_mark_session_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket = str((tmp_path / "tmux.sock").resolve())
    service = SessionService(project)
    session = service.attach(
        task["task_id"], "codex", native_id="native", tmux=f"{socket}:agent"
    )
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: f"/bin/{name}")
    calls = 0

    def fake_run(*args: Any, **kwargs: Any) -> _FakeCompleted:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _FakeCompleted(1, "can't find session")
        return _FakeCompleted(9, "failed")

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    with pytest.raises(OffworkError) as caught:
        service.reopen(session["managed_session_id"])

    assert caught.value.code == "NATIVE_RESUME_FAILED"
    current = service.list(task["task_id"])[0]
    assert current["state"] == "attached"
    assert current["revision"] == session["revision"]


def test_native_resume_that_disappears_during_grace_is_not_marked_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket = str((tmp_path / "tmux.sock").resolve())
    service = SessionService(project)
    session = service.attach(
        task["task_id"], "codex", native_id="native", tmux=f"{socket}:agent"
    )
    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", lambda name: f"/bin/{name}")
    sleeps: list[float] = []
    monkeypatch.setattr("offwork_capsule.sessions.time.sleep", sleeps.append)
    has_session_calls = 0

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        nonlocal has_session_calls
        if "has-session" in argv:
            has_session_calls += 1
            return _FakeCompleted(1, "can't find session")
        return _FakeCompleted(0)

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", fake_run)
    with pytest.raises(OffworkError) as vanished:
        service.reopen(session["managed_session_id"])

    assert vanished.value.code == "NATIVE_RESUME_FAILED"
    assert vanished.value.exit_code == 5
    assert sleeps == [pytest.approx(0.2)]
    assert has_session_calls == 2
    current = service.list(task["task_id"])[0]
    assert current["state"] == "attached"
    assert current["revision"] == session["revision"]


@pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("false") is None,
    reason="tmux or false is unavailable",
)
def test_real_tmux_immediate_provider_exit_fails_startup_health_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    socket_path = Path("/tmp") / ("offwork-tmux-" + uuid.uuid4().hex[:12] + ".sock")
    socket = str(socket_path.resolve())
    service = SessionService(project)
    session = service.attach(
        task["task_id"], "codex", native_id="native", tmux=f"{socket}:agent"
    )
    tmux = shutil.which("tmux")
    false = shutil.which("false")
    assert tmux is not None and false is not None

    def binaries(name: str) -> str | None:
        return tmux if name == "tmux" else false if name == "codex" else None

    monkeypatch.setattr("offwork_capsule.sessions.shutil.which", binaries)
    try:
        with pytest.raises(OffworkError) as vanished:
            service.reopen(session["managed_session_id"])
        assert vanished.value.code == "NATIVE_RESUME_FAILED"
        assert service.list(task["task_id"])[0]["state"] == "attached"
    finally:
        subprocess.run(
            [tmux, "-S", socket, "kill-server"],
            check=False,
            capture_output=True,
            text=True,
        )
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


def test_status_reports_sessions_without_probing_external_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("one", "finish")
    session = SessionService(project).attach(task["task_id"], "codex")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("status must not probe external processes")

    monkeypatch.setattr("offwork_capsule.sessions.subprocess.run", forbidden)
    status = StateService(project).project_status()

    assert status["primary_session"]["managed_session_id"] == session["managed_session_id"]
    assert len(status["attached_sessions"]) == 1


def test_human_status_surfaces_primary_and_attached_session_count(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = _task(tmp_path, project)
    attached = _run(
        tmp_path,
        "session",
        "attach",
        "--task",
        task["task_id"],
        "--tool",
        "codex",
        "--project",
        str(project),
    )
    assert attached.returncode == 0

    status = _run(tmp_path, "status", "--project", str(project))

    assert status.returncode == 0
    assert "Primary session: msn_" in status.stdout
    assert "Attached sessions: 1" in status.stdout


def test_cli_session_errors_keep_json_envelope(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    missing = _run(
        tmp_path,
        "session",
        "primary",
        "msn_missing",
        "--project",
        str(project),
        "--json",
    )

    payload = _json(missing)
    assert missing.returncode == 3
    assert payload["command"] == "session.primary"
    assert payload["error"]["code"] == "SESSION_NOT_FOUND"
