from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from offwork_capsule import capsule as capsule_module
from offwork_capsule import cli as cli_module
from offwork_capsule import verifier as verifier_module
from offwork_capsule.sessions import SessionService
from offwork_capsule.memory import MemoryService
from offwork_capsule.state import DEFAULT_TASK_ID, StateService, registry_status
from offwork_capsule.storage import initialize_project_storage


ROOT = Path(__file__).parents[1]


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    return subprocess.run(
        [sys.executable, "-m", "offwork_capsule.cli", *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict:
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


def _context(tmp_path: Path, label: str, *, open_loops: list[dict] | None = None) -> Path:
    path = tmp_path / ("context-%s.json" % label)
    path.write_text(
        json.dumps(
            {
                "goal": "goal " + label,
                "summary": "summary " + label,
                "decisions": [],
                "failed_attempts": [],
                "next_step": "next " + label,
                "next_command": "echo old-command-must-not-run",
                "open_loops": open_loops or [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _add_task(
    tmp_path: Path,
    project: Path,
    title: str,
    *extra: str,
) -> dict:
    result = _run(
        tmp_path,
        "task",
        "add",
        title,
        "--goal",
        "finish " + title,
        "--project",
        str(project),
        "--json",
        *extra,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return _payload(result)["data"]


def _capture(tmp_path: Path, project: Path, task_id: str, context: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return _run(
        tmp_path,
        "capture",
        "--task",
        task_id,
        "--context",
        str(context),
        "--project",
        str(project),
        "--json",
        *extra,
    )


def test_task_capsules_and_resume_are_isolated(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _add_task(tmp_path, project, "first")
    second = _add_task(tmp_path, project, "second")

    first_capture = _payload(
        _capture(tmp_path, project, first["task_id"], _context(tmp_path, "first"))
    )["data"]
    second_capture = _payload(
        _capture(tmp_path, project, second["task_id"], _context(tmp_path, "second"))
    )["data"]

    assert first_capture["capsule_id"] != second_capture["capsule_id"]
    assert capsule_module.load_latest_task_capsule(project, first["task_id"])["goal"] == "goal first"
    assert capsule_module.load_latest_task_capsule(project, second["task_id"])["goal"] == "goal second"
    resumed = _run(
        tmp_path,
        "resume",
        "--task",
        first["task_id"],
        "--project",
        str(project),
        "--json",
    )
    assert _payload(resumed)["data"]["capsule_id"] == first_capture["capsule_id"]


def test_capture_hibernates_only_primary_and_auto_completes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = _add_task(
        tmp_path,
        project,
        "automatic",
        "--auto-complete",
        "--accept-cmd",
        '%s -c "raise SystemExit(0)"' % sys.executable,
    )
    first = _payload(
        _run(
            tmp_path,
            "session",
            "attach",
            "--task",
            task["task_id"],
            "--tool",
            "codex",
            "--project",
            str(project),
            "--json",
        )
    )["data"]
    second = _payload(
        _run(
            tmp_path,
            "session",
            "attach",
            "--task",
            task["task_id"],
            "--tool",
            "claude",
            "--project",
            str(project),
            "--json",
        )
    )["data"]

    captured = _capture(
        tmp_path, project, task["task_id"], _context(tmp_path, "automatic")
    )
    data = _payload(captured)["data"]

    assert captured.returncode == 0
    assert data["auto_complete"]["passed"] is True
    shown = _payload(
        _run(
            tmp_path,
            "task",
            "show",
            task["task_id"],
            "--project",
            str(project),
            "--json",
        )
    )["data"]
    assert shown["status"] == "complete"
    assert shown["archived_at"] is not None
    sessions = _payload(
        _run(
            tmp_path,
            "session",
            "list",
            "--task",
            task["task_id"],
            "--project",
            str(project),
            "--json",
        )
    )["data"]["sessions"]
    states = {item["managed_session_id"]: item["state"] for item in sessions}
    assert states[first["managed_session_id"]] == "hibernated"
    assert states[second["managed_session_id"]] == "attached"


@pytest.mark.parametrize(
    ("acceptance", "open_loop"),
    [
        ('%s -c "raise SystemExit(7)"' % sys.executable, None),
        (
            '%s -c "raise SystemExit(0)"' % sys.executable,
            {"title": "still open", "disposition": "resolve", "note": "todo"},
        ),
    ],
)
def test_failed_auto_complete_keeps_review_capsule(
    tmp_path: Path, acceptance: str, open_loop: dict | None
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    extra = ["--auto-complete", "--accept-cmd", acceptance]
    if open_loop is not None:
        extra += ["--open-loop", json.dumps(open_loop)]
    task = _add_task(tmp_path, project, "gated", *extra)

    captured = _capture(
        tmp_path, project, task["task_id"], _context(tmp_path, "gated")
    )
    payload = _payload(captured)

    assert captured.returncode == 0
    assert payload["data"]["auto_complete"]["passed"] is False
    assert any(item["code"] == "ACCEPTANCE_FAILED" for item in payload["warnings"])
    shown = _payload(
        _run(
            tmp_path,
            "task",
            "show",
            task["task_id"],
            "--project",
            str(project),
            "--json",
        )
    )["data"]
    assert shown["status"] == "review"
    assert shown["archived_at"] is None
    assert capsule_module.load_latest_task_capsule(project, task["task_id"])["id"] == payload["data"]["capsule_id"]


def test_acceptance_command_never_uses_a_shell(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "must-not-exist"
    command = '%s -c "raise SystemExit(0)" ; touch %s' % (sys.executable, marker)
    task = _add_task(
        tmp_path,
        project,
        "argv only",
        "--auto-complete",
        "--accept-cmd",
        command,
    )

    result = _capture(
        tmp_path, project, task["task_id"], _context(tmp_path, "argv")
    )

    assert result.returncode == 0
    assert _payload(result)["data"]["auto_complete"]["passed"] is True
    assert not marker.exists()


def test_stale_revision_fails_before_capsule_publish(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = _add_task(tmp_path, project, "stale")

    result = _capture(
        tmp_path,
        project,
        task["task_id"],
        _context(tmp_path, "stale"),
        "--revision",
        "999",
    )
    payload = _payload(result)

    assert result.returncode == 4
    assert payload["error"]["code"] == "STALE_REVISION"
    capsules = project / ".offwork" / "capsules"
    assert not capsules.exists() or not list(capsules.iterdir())


def test_capsule_integrity_tamper_is_stable_error(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = _add_task(tmp_path, project, "integrity")
    captured = _payload(
        _capture(tmp_path, project, task["task_id"], _context(tmp_path, "integrity"))
    )["data"]
    archive = Path(captured["archive_dir"])
    (archive / "capsule.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(capsule_module.CapsuleValidationError):
        capsule_module.load_capsule(project, captured["capsule_id"])
    resumed = _run(
        tmp_path,
        "resume",
        "--task",
        task["task_id"],
        "--project",
        str(project),
        "--json",
    )
    payload = _payload(resumed)
    assert resumed.returncode == 4
    assert payload["error"]["code"] == "CAPSULE_INTEGRITY_FAILED"


def test_v01_capture_resume_still_uses_legacy_latest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    captured = _run(
        tmp_path,
        "capture",
        "--context",
        str(_context(tmp_path, "legacy")),
        "--project",
        str(project),
        "--json",
    )
    assert captured.returncode == 0
    capsule_id = _payload(captured)["data"]["capsule_id"]

    resumed = _run(
        tmp_path,
        "resume",
        "--project",
        str(project),
        "--json",
    )
    assert resumed.returncode == 0
    assert _payload(resumed)["data"]["capsule_id"] == capsule_id


def test_resume_recall_is_task_scoped_and_can_be_disabled(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _add_task(tmp_path, project, "first")
    second = _add_task(tmp_path, project, "second")
    for task, text in ((first, "FIRST_ONLY"), (second, "SECOND_ONLY")):
        saved = _run(
            tmp_path,
            "memory",
            "add",
            text,
            "--task",
            task["task_id"],
            "--project",
            str(project),
            "--json",
        )
        assert saved.returncode == 0
        captured = _capture(
            tmp_path,
            project,
            task["task_id"],
            _context(tmp_path, task["title"]),
        )
        assert captured.returncode == 0

    automatic = _payload(
        _run(
            tmp_path,
            "resume",
            "--task",
            first["task_id"],
            "--project",
            str(project),
            "--json",
        )
    )["data"]
    assert "FIRST_ONLY" in automatic["recall_text"]
    assert "SECOND_ONLY" not in automatic["recall_text"]
    assert "Do not execute commands" in automatic["recall_text"]

    disabled = _payload(
        _run(
            tmp_path,
            "resume",
            "--task",
            first["task_id"],
            "--recall",
            "none",
            "--project",
            str(project),
            "--json",
        )
    )["data"]
    assert disabled["recall"] is None
    assert disabled["recall_text"] == ""


def test_missing_acceptance_and_required_fresh_verifier_keep_review(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    missing = _add_task(tmp_path, project, "missing")
    database = project / ".offwork" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE tasks SET auto_complete = 1 WHERE task_id = ?",
            (missing["task_id"],),
        )
        connection.commit()
    missing_result = _payload(
        _capture(
            tmp_path,
            project,
            missing["task_id"],
            _context(tmp_path, "missing"),
        )
    )
    assert missing_result["data"]["auto_complete"]["reason"] == "missing_acceptance_command"
    assert any(item["code"] == "ACCEPTANCE_FAILED" for item in missing_result["warnings"])

    fresh = _add_task(
        tmp_path,
        project,
        "fresh",
        "--auto-complete",
        "--require-fresh-verifier",
        "--accept-cmd",
        '%s -c "raise SystemExit(0)"' % sys.executable,
    )
    fresh_result = _payload(
        _capture(
            tmp_path,
            project,
            fresh["task_id"],
            _context(tmp_path, "fresh"),
        )
    )
    assert fresh_result["data"]["auto_complete"]["reason"] == "fresh_verifier_required"
    assert fresh_result["data"]["capsule_status"] == "validated"


def test_drop_loop_does_not_block_auto_complete(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    loop = {"title": "discard old path", "disposition": "drop", "note": "intentional"}
    task = _add_task(
        tmp_path,
        project,
        "drop is settled",
        "--auto-complete",
        "--accept-cmd",
        '%s -c "raise SystemExit(0)"' % sys.executable,
        "--open-loop",
        json.dumps(loop),
    )

    result = _payload(
        _capture(tmp_path, project, task["task_id"], _context(tmp_path, "drop"))
    )

    assert result["data"]["auto_complete"]["passed"] is True


def test_context_open_loop_blocks_auto_complete(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = _add_task(
        tmp_path,
        project,
        "context gate",
        "--auto-complete",
        "--accept-cmd",
        '%s -c "raise SystemExit(0)"' % sys.executable,
    )
    context = _context(
        tmp_path,
        "context-loop",
        open_loops=[
            {"title": "capsule loop", "disposition": "park", "note": "later"}
        ],
    )

    result = _payload(_capture(tmp_path, project, task["task_id"], context))

    assert result["data"]["auto_complete"]["reason"] == "open_loops"
    shown = StateService(project).show_task(task["task_id"])
    assert shown["status"] == "review"


def test_existing_v01_capsule_is_imported_without_rewrite(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    capsule = {
        "schema_version": 1,
        "id": "20260825T175800Z",
        "captured_at": "2026-08-25T17:58:00+00:00",
        "goal": "legacy goal",
        "summary": "legacy summary",
        "decisions": [],
        "failed_attempts": [],
        "next_step": "legacy next",
        "next_command": "",
        "open_loops": [],
        "project": {"project_path": str(project), "is_git_repo": False},
    }
    restore = capsule_module.validate_for_restore(capsule)
    archive = capsule_module.archive_capsule(project, capsule, restore)
    before = {path.name: path.read_bytes() for path in archive.iterdir()}

    task = StateService(project).ensure_default_task("ignored new goal")

    assert task["task_id"] == DEFAULT_TASK_ID
    imported = capsule_module.load_latest_task_capsule(project, DEFAULT_TASK_ID)
    assert imported["id"] == capsule["id"]
    assert {path.name: path.read_bytes() for path in archive.iterdir()} == before
    with StateService(project).storage.connect() as connection:
        row = connection.execute(
            "SELECT task_id, status, content_hash FROM capsules WHERE capsule_id = ?",
            (capsule["id"],),
        ).fetchone()
    assert row["task_id"] == DEFAULT_TASK_ID
    assert row["status"] == "validated"
    assert row["content_hash"]
    memory = MemoryService(project).add("legacy-linked", capsule_id=capsule["id"])
    assert memory["task_id"] == DEFAULT_TASK_ID
    assert memory["capsule_id"] == capsule["id"]


def test_fresh_rejection_is_recorded_but_not_latest_or_task_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("fresh", "fresh goal")
    first_context = _context(tmp_path, "accepted-local")
    assert cli_module.main(
        [
            "capture", "--task", task["task_id"], "--context", str(first_context),
            "--project", str(project), "--json",
        ]
    ) == 0
    first = json.loads(capsys.readouterr().out)["data"]["capsule_id"]
    before = StateService(project).show_task(task["task_id"])

    monkeypatch.setattr(
        cli_module,
        "run_claude_verifier",
        lambda capsule: {
            "mode": "fresh-agent",
            "ready_to_resume": False,
            "understood_goal": capsule["goal"],
            "current_state": capsule["summary"],
            "next_action": "",
            "missing_information": ["missing proof"],
        },
    )
    code = cli_module.main(
        [
            "capture", "--task", task["task_id"], "--context",
            str(_context(tmp_path, "rejected")), "--verifier", "claude",
            "--project", str(project), "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 4
    assert payload["error"]["code"] == "VERIFICATION_FAILED"
    after = StateService(project).show_task(task["task_id"])
    assert (after["status"], after["revision"]) == (before["status"], before["revision"])
    assert capsule_module.load_latest_task_capsule(project, task["task_id"])["id"] == first
    with StateService(project).storage.connect() as connection:
        rejected = connection.execute(
            "SELECT capsule_id FROM capsules WHERE task_id = ? AND status = 'rejected'",
            (task["task_id"],),
        ).fetchall()
    assert len(rejected) == 1


def test_fresh_verifier_cannot_approve_missing_project_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    project = tmp_path / "project"
    project.mkdir()
    task = StateService(project).add_task("fresh", "fresh goal")
    context = _context(tmp_path, "missing-path")
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["next_command"] = "open docs/demo-plan.md"
    context.write_text(json.dumps(payload), encoding="utf-8")
    verifier_called = False

    def approve(capsule: dict) -> dict:
        nonlocal verifier_called
        verifier_called = True
        return {
            "mode": "fresh-agent",
            "ready_to_resume": True,
            "understood_goal": capsule["goal"],
            "current_state": capsule["summary"],
            "next_action": capsule["next_step"],
            "missing_information": [],
        }

    monkeypatch.setattr(cli_module, "run_claude_verifier", approve)
    code = cli_module.main(
        [
            "capture", "--task", task["task_id"], "--context", str(context),
            "--verifier", "claude", "--project", str(project), "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert code == 4
    assert result["error"]["code"] == "VERIFICATION_FAILED"
    assert result["error"]["details"]["missing_information"] == [
        "建议命令引用的项目路径不存在：docs/demo-plan.md"
    ]
    assert verifier_called is False


def test_legacy_latest_failure_compensates_db_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    project = tmp_path / "project"
    project.mkdir()
    assert cli_module.main(
        ["capture", "--context", str(_context(tmp_path, "one")), "--project", str(project), "--json"]
    ) == 0
    first = json.loads(capsys.readouterr().out)["data"]["capsule_id"]
    session = SessionService(project).attach(DEFAULT_TASK_ID, "codex")
    before = StateService(project).show_task(DEFAULT_TASK_ID)
    before_session = SessionService(project).list(DEFAULT_TASK_ID)[0]

    def fail_latest(project_root: Path, capsule_id: str) -> None:
        raise OSError("injected latest failure")

    monkeypatch.setattr(cli_module, "update_legacy_latest", fail_latest)
    code = cli_module.main(
        ["capture", "--context", str(_context(tmp_path, "two")), "--project", str(project), "--json"]
    )
    capsys.readouterr()

    assert code == 1
    after = StateService(project).show_task(DEFAULT_TASK_ID)
    assert (after["status"], after["revision"]) == (before["status"], before["revision"])
    after_session = SessionService(project).list(DEFAULT_TASK_ID)[0]
    assert after_session["managed_session_id"] == session["managed_session_id"]
    assert (after_session["state"], after_session["revision"]) == (
        before_session["state"], before_session["revision"]
    )
    assert capsule_module.load_latest_capsule(project)["id"] == first
    assert capsule_module.load_latest_task_capsule(project, DEFAULT_TASK_ID)["id"] == first
    with StateService(project).storage.connect() as connection:
        failed = connection.execute(
            "SELECT status FROM capsules WHERE task_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (DEFAULT_TASK_ID,),
        ).fetchone()
    assert failed["status"] == "rejected"
    summary = next(
        task
        for item in registry_status()["projects"]
        if item["canonical_path"] == str(project.resolve())
        for task in item["tasks"]
        if task["task_id"] == DEFAULT_TASK_ID
    )
    assert (summary["status"], summary["revision"]) == (
        before["status"], before["revision"]
    )


def test_legacy_latest_written_before_error_is_restored_after_compensation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    project = tmp_path / "project"
    project.mkdir()
    assert cli_module.main(
        ["capture", "--context", str(_context(tmp_path, "first")), "--project", str(project), "--json"]
    ) == 0
    first = json.loads(capsys.readouterr().out)["data"]["capsule_id"]

    def fail_after_write(project_root: Path, capsule_id: str) -> None:
        capsule_module.update_legacy_latest(project_root, capsule_id)
        raise OSError("injected failure after latest write")

    monkeypatch.setattr(cli_module, "update_legacy_latest", fail_after_write)
    code = cli_module.main(
        ["capture", "--context", str(_context(tmp_path, "second")), "--project", str(project), "--json"]
    )
    capsys.readouterr()

    assert code == 1
    assert capsule_module.load_latest_capsule(project)["id"] == first
    assert capsule_module.load_latest_task_capsule(project, DEFAULT_TASK_ID)["id"] == first
    with StateService(project).storage.connect() as connection:
        failed = connection.execute(
            "SELECT status FROM capsules WHERE task_id = ? ORDER BY rowid DESC LIMIT 1",
            (DEFAULT_TASK_ID,),
        ).fetchone()
    assert failed["status"] == "rejected"
    assert not (project / ".offwork" / "pending-legacy-capture.json").exists()


def test_legacy_latest_restore_failure_keeps_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    project = tmp_path / "project"
    project.mkdir()
    assert cli_module.main(
        ["capture", "--context", str(_context(tmp_path, "base")), "--project", str(project), "--json"]
    ) == 0
    capsys.readouterr()
    real_update = capsule_module.update_legacy_latest

    def fail_after_write(project_root: Path, capsule_id: str) -> None:
        real_update(project_root, capsule_id)
        raise OSError("injected failure after latest write")

    def fail_restore(project_root: Path, capsule_id: str | None) -> None:
        raise OSError("injected restore failure")

    monkeypatch.setattr(cli_module, "update_legacy_latest", fail_after_write)
    monkeypatch.setattr(capsule_module, "restore_legacy_latest", fail_restore)
    code = cli_module.main(
        ["capture", "--context", str(_context(tmp_path, "failed")), "--project", str(project), "--json"]
    )
    capsys.readouterr()

    assert code == 1
    assert (project / ".offwork" / "pending-legacy-capture.json").is_file()


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_capture_project_must_be_existing_real_directory(tmp_path: Path, kind: str) -> None:
    target = tmp_path / kind
    if kind == "file":
        target.write_text("not a project", encoding="utf-8")
    elif kind == "symlink":
        real = tmp_path / "real"
        real.mkdir()
        target.symlink_to(real, target_is_directory=True)

    result = _run(
        tmp_path, "capture", "--context", str(_context(tmp_path, kind)),
        "--project", str(target), "--json",
    )

    assert result.returncode == 4
    assert _payload(result)["error"]["code"] == "PROJECT_PATH_MISMATCH"
    if kind == "missing":
        assert not target.exists()


def test_terminal_text_removes_control_sequences() -> None:
    malicious = "safe\rOVER\x1b[31mRED\x1b]0;TITLE\x07\x85END"
    rendered = cli_module._terminal_text(malicious)
    assert rendered == (
        "safe\\rOVER\\x1b[31mRED\\x1b]0;TITLE\\x07\\x85END"
    )
    assert "\x1b" not in rendered
    assert "\r" not in rendered
    assert "\x07" not in rendered
    assert "\x85" not in rendered


def test_acceptance_tamper_blocks_auto_complete(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    script = (
        "from pathlib import Path; "
        "p=next(Path('.offwork/capsules').glob('*/capsule.md')); "
        "p.write_text('tampered')"
    )
    task = _add_task(
        tmp_path, project, "tamper", "--auto-complete", "--accept-cmd",
        '%s -c %s' % (sys.executable, json.dumps(script)),
    )

    payload = _payload(
        _capture(tmp_path, project, task["task_id"], _context(tmp_path, "tamper"))
    )

    assert payload["data"]["auto_complete"]["reason"] == "capsule_integrity_failed"
    assert StateService(project).show_task(task["task_id"])["status"] == "review"


def test_schema_v3_migrates_explicitly_to_v4_without_task_loss(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    storage = initialize_project_storage(project)
    StateService(project).add_task("keep", "keep data")
    with storage.connect() as connection:
        connection.execute("DROP TABLE task_auto_complete_config")
        connection.execute("PRAGMA user_version = 3")
        connection.commit()

    migrated = initialize_project_storage(project)

    with migrated.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT title FROM tasks").fetchone()[0] == "keep"
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'task_auto_complete_config'"
        ).fetchone() is not None


def test_capsule_status_constraint_rejects_unknown_value(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    storage = initialize_project_storage(project)
    with storage.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO capsules(capsule_id, status, content_hash, archive_path, "
            "created_at, updated_at) VALUES ('bad', 'unknown', 'hash', '/tmp/bad', "
            "'now', 'now')"
        )


@pytest.mark.parametrize("failure", [FileNotFoundError("gone"), PermissionError("denied")])
def test_claude_spawn_capability_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, failure: OSError
) -> None:
    monkeypatch.setattr(verifier_module.shutil, "which", lambda name: "/tmp/claude")

    def fail_run(*args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(verifier_module.subprocess, "run", fail_run)
    with pytest.raises(verifier_module.VerifierUnavailableError):
        verifier_module.run_claude_verifier(
            {"goal": "g", "summary": "s", "next_step": "n"}
        )


def test_pending_legacy_publish_rolls_forward_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    project = tmp_path / "project"
    project.mkdir()
    assert cli_module.main(
        ["capture", "--context", str(_context(tmp_path, "base")), "--project", str(project), "--json"]
    ) == 0
    first = json.loads(capsys.readouterr().out)["data"]["capsule_id"]
    service = StateService(project)
    task = service.prepare_capture(DEFAULT_TASK_ID)
    capsule = capsule_module.build_capsule(
        json.loads(_context(tmp_path, "pending").read_text()),
        {"project_path": str(project), "is_git_repo": False},
        task_id=DEFAULT_TASK_ID,
        parent_capsule_id=first,
    )
    restore = capsule_module.validate_for_restore(capsule)
    marker = {
        "schema_version": 1,
        "capsule_id": capsule["id"],
        "task_id": DEFAULT_TASK_ID,
        "previous_latest_id": first,
        "content_hash": capsule["content_hash"],
        "archive_path": str(project / ".offwork" / "capsules" / capsule["id"]),
        "parent_capsule_id": first,
        "managed_session_id": None,
        "before_task_status": task["status"],
        "before_task_revision": task["revision"],
        "before_task_updated_at": task["updated_at"],
    }
    capsule_module.write_pending_legacy_capture(project, marker)
    archive = capsule_module.archive_capsule(
        project, capsule, restore, update_latest=False
    )
    service.publish_capture(
        task_id=DEFAULT_TASK_ID,
        expected_revision=task["revision"],
        capsule_id=capsule["id"],
        managed_session_id=None,
        parent_capsule_id=first,
        status="validated",
        content_hash=capsule["content_hash"],
        archive_path=archive,
    )

    events: list[str] = []
    real_lock = capsule_module.capsule_transaction_lock
    real_load = capsule_module.load_pending_legacy_capture

    @contextmanager
    def tracked_lock(project_root: Path):
        with real_lock(project_root):
            events.append("lock_enter")
            yield
            events.append("lock_exit")

    def tracked_load(project_root: Path) -> dict[str, object] | None:
        events.append("load")
        return real_load(project_root)

    monkeypatch.setattr(capsule_module, "capsule_transaction_lock", tracked_lock)
    monkeypatch.setattr(capsule_module, "load_pending_legacy_capture", tracked_load)

    restarted = StateService(project)

    assert events == ["lock_enter", "load", "lock_exit"]
    assert capsule_module.load_latest_capsule(project)["id"] == capsule["id"]
    assert restarted.show_task(DEFAULT_TASK_ID)["status"] == "review"
    assert not (project / ".offwork" / "pending-legacy-capture.json").exists()


def test_pending_legacy_archive_without_db_rolls_back_as_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    project = tmp_path / "project"
    project.mkdir()
    assert cli_module.main(
        ["capture", "--context", str(_context(tmp_path, "base2")), "--project", str(project), "--json"]
    ) == 0
    first = json.loads(capsys.readouterr().out)["data"]["capsule_id"]
    service = StateService(project)
    task = service.prepare_capture(DEFAULT_TASK_ID)
    capsule = capsule_module.build_capsule(
        json.loads(_context(tmp_path, "orphan").read_text()),
        {"project_path": str(project), "is_git_repo": False},
        task_id=DEFAULT_TASK_ID,
        parent_capsule_id=first,
    )
    restore = capsule_module.validate_for_restore(capsule)
    archive = capsule_module.archive_capsule(project, capsule, restore, update_latest=False)
    capsule_module.write_pending_legacy_capture(
        project,
        {
            "schema_version": 1,
            "capsule_id": capsule["id"],
            "task_id": DEFAULT_TASK_ID,
            "previous_latest_id": first,
            "content_hash": capsule["content_hash"],
            "archive_path": str(archive),
            "parent_capsule_id": first,
            "managed_session_id": None,
            "before_task_status": task["status"],
            "before_task_revision": task["revision"],
            "before_task_updated_at": task["updated_at"],
        },
    )

    restarted = StateService(project)

    assert capsule_module.load_latest_capsule(project)["id"] == first
    assert restarted.show_task(DEFAULT_TASK_ID)["revision"] == task["revision"]
    with restarted.storage.connect() as connection:
        assert connection.execute(
            "SELECT status FROM capsules WHERE capsule_id = ?", (capsule["id"],)
        ).fetchone()["status"] == "rejected"


@pytest.mark.parametrize("preexisting_rejected", [False, True])
def test_pending_archive_without_committed_publish_preserves_concurrent_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    preexisting_rejected: bool,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    project = tmp_path / "project"
    project.mkdir()
    assert cli_module.main(
        ["capture", "--context", str(_context(tmp_path, "base3")), "--project", str(project), "--json"]
    ) == 0
    first = json.loads(capsys.readouterr().out)["data"]["capsule_id"]
    session = SessionService(project).attach(DEFAULT_TASK_ID, "codex")
    service = StateService(project)
    task = service.prepare_capture(DEFAULT_TASK_ID)
    capsule = capsule_module.build_capsule(
        json.loads(_context(tmp_path, "orphan-concurrent").read_text()),
        {"project_path": str(project), "is_git_repo": False},
        task_id=DEFAULT_TASK_ID,
        managed_session_id=session["managed_session_id"],
        parent_capsule_id=first,
    )
    archive = capsule_module.archive_capsule(
        project,
        capsule,
        capsule_module.validate_for_restore(capsule),
        update_latest=False,
    )
    marker = {
        "schema_version": 1,
        "capsule_id": capsule["id"],
        "task_id": DEFAULT_TASK_ID,
        "previous_latest_id": first,
        "content_hash": capsule["content_hash"],
        "archive_path": str(archive),
        "parent_capsule_id": first,
        "managed_session_id": session["managed_session_id"],
        "before_task_status": task["status"],
        "before_task_revision": task["revision"],
        "before_task_updated_at": task["updated_at"],
        "before_session_state": task["primary_session_state"],
        "before_session_revision": task["primary_session_revision"],
        "before_session_updated_at": task["primary_session_updated_at"],
    }
    capsule_module.write_pending_legacy_capture(project, marker)
    if preexisting_rejected:
        service.register_rejected_capture(
            task_id=DEFAULT_TASK_ID,
            expected_revision=task["revision"],
            capsule_id=capsule["id"],
            managed_session_id=session["managed_session_id"],
            parent_capsule_id=first,
            content_hash=capsule["content_hash"],
            archive_path=archive,
        )
    with service.storage.connect() as connection:
        connection.execute(
            "UPDATE tasks SET status = 'in_progress', revision = revision + 1, "
            "updated_at = 'concurrent-task' WHERE task_id = ?",
            (DEFAULT_TASK_ID,),
        )
        connection.execute(
            "UPDATE sessions SET state = 'active', revision = revision + 1, "
            "updated_at = 'concurrent-session' WHERE managed_session_id = ?",
            (session["managed_session_id"],),
        )
        connection.commit()

    restarted = StateService(project)

    concurrent_task = restarted.show_task(DEFAULT_TASK_ID)
    concurrent_session = SessionService(project).list(DEFAULT_TASK_ID)[0]
    assert (concurrent_task["status"], concurrent_task["revision"], concurrent_task["updated_at"]) == (
        "in_progress",
        task["revision"] + 1,
        "concurrent-task",
    )
    assert (
        concurrent_session["state"],
        concurrent_session["revision"],
        concurrent_session["updated_at"],
    ) == ("active", task["primary_session_revision"] + 1, "concurrent-session")
    with restarted.storage.connect() as connection:
        assert connection.execute(
            "SELECT status FROM capsules WHERE capsule_id = ?", (capsule["id"],)
        ).fetchone()["status"] == "rejected"
    assert capsule_module.load_latest_capsule(project)["id"] == first
    assert not (project / ".offwork" / "pending-legacy-capture.json").exists()


def test_acceptance_integrity_failure_rejects_capsule_and_restores_previous_latest(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = _add_task(tmp_path, project, "integrity chain")
    first = _payload(
        _capture(tmp_path, project, task["task_id"], _context(tmp_path, "good"))
    )["data"]["capsule_id"]
    script = (
        "from pathlib import Path; "
        "p=max(Path('.offwork/capsules').glob('*/capsule.md'), "
        "key=lambda item: item.stat().st_mtime_ns); "
        "p.write_text('tampered')"
    )
    storage = StateService(project).storage
    with storage.connect() as connection:
        connection.execute(
            "UPDATE tasks SET auto_complete = 1, acceptance_commands_json = ?, "
            "revision = revision + 1 WHERE task_id = ?",
            (json.dumps(['%s -c %s' % (sys.executable, json.dumps(script))]), task["task_id"]),
        )
        connection.commit()
    result = _payload(
        _capture(tmp_path, project, task["task_id"], _context(tmp_path, "bad"))
    )
    failed_id = result["data"]["capsule_id"]

    assert result["data"]["auto_complete"]["reason"] == "capsule_integrity_failed"
    assert capsule_module.load_latest_task_capsule(project, task["task_id"])["id"] == first
    with storage.connect() as connection:
        assert connection.execute(
            "SELECT status FROM capsules WHERE capsule_id = ?", (failed_id,)
        ).fetchone()["status"] == "rejected"


def test_resume_explicit_rejected_payload_without_db_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    project = tmp_path / "project"
    project.mkdir()
    capsule = capsule_module.build_capsule(
        json.loads(_context(tmp_path, "payload-rejected").read_text()),
        {"project_path": str(project), "is_git_repo": False},
    )
    capsule["status"] = "rejected"
    capsule["content_hash"] = capsule_module.capsule_content_hash(capsule)
    archive = capsule_module.archive_capsule(
        project,
        capsule,
        {"ready_to_resume": False, "missing_information": ["rejected"]},
        update_latest=False,
        allow_rejected=True,
    )
    assert archive.exists()

    code = cli_module.main(
        ["resume", "--capsule", capsule["id"], "--project", str(project), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 4
    assert payload["error"]["code"] == "VERIFICATION_FAILED"


@pytest.mark.parametrize("rejection_source", ["payload", "database"])
def test_resume_implicit_latest_rejects_rejected_capsule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rejection_source: str,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    project = tmp_path / rejection_source
    project.mkdir()
    if rejection_source == "payload":
        capsule = capsule_module.build_capsule(
            json.loads(_context(tmp_path, "implicit-payload-rejected").read_text()),
            {"project_path": str(project), "is_git_repo": False},
        )
        capsule["status"] = "rejected"
        capsule["content_hash"] = capsule_module.capsule_content_hash(capsule)
        capsule_module.archive_capsule(
            project,
            capsule,
            {"ready_to_resume": False, "missing_information": ["rejected"]},
            allow_rejected=True,
        )
    else:
        capsule = {
            "schema_version": 1,
            "id": "20260825T175803Z",
            "captured_at": "2026-08-25T17:58:03+00:00",
            "goal": "implicit database rejected",
            "summary": "legacy",
            "decisions": [],
            "failed_attempts": [],
            "next_step": "continue",
            "next_command": "",
            "open_loops": [],
            "project": {"project_path": str(project), "is_git_repo": False},
        }
        capsule_module.archive_capsule(
            project, capsule, capsule_module.validate_for_restore(capsule)
        )
        service = StateService(project)
        with service.storage.connect() as connection:
            connection.execute(
                "UPDATE capsules SET status = 'rejected' WHERE capsule_id = ?",
                (capsule["id"],),
            )
            connection.commit()

    code = cli_module.main(["resume", "--project", str(project), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 4
    assert payload["error"]["code"] == "VERIFICATION_FAILED"


def test_legacy_artifacts_bootstrap_real_entrypoints_but_empty_project_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    def legacy_project(name: str, capsule_id: str) -> tuple[Path, dict[str, object]]:
        project = tmp_path / name
        project.mkdir()
        legacy: dict[str, object] = {
            "schema_version": 1,
            "id": capsule_id,
            "captured_at": "2026-08-25T17:58:00+00:00",
            "goal": "legacy bootstrap",
            "summary": "legacy",
            "decisions": [],
            "failed_attempts": [],
            "next_step": "continue",
            "next_command": "",
            "open_loops": [],
            "project": {"project_path": str(project), "is_git_repo": False},
        }
        capsule_module.archive_capsule(
            project, legacy, capsule_module.validate_for_restore(legacy)
        )
        return project, legacy

    status_project, _ = legacy_project("status-project", "20260825T175800Z")
    status = StateService(status_project).project_status()
    assert any(
        task["task_id"] == DEFAULT_TASK_ID
        for group in status["tasks"].values()
        for task in group
    )

    resume_project, _ = legacy_project("resume-project", "20260825T175801Z")
    resumed = _run(
        tmp_path,
        "resume",
        "--task",
        DEFAULT_TASK_ID,
        "--project",
        str(resume_project),
        "--json",
    )
    assert resumed.returncode == 0

    memory_project, memory_legacy = legacy_project(
        "memory-project", "20260825T175802Z"
    )
    assert MemoryService(memory_project).add(
        "linked", capsule_id=str(memory_legacy["id"])
    )["task_id"] == DEFAULT_TASK_ID

    empty = tmp_path / "empty"
    empty.mkdir()
    assert StateService(empty).list_tasks() == []
