from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest


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


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "offwork.cli/v1"
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


def _add(tmp_path: Path, project: Path, title: str, *extra: str) -> dict:
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
    return _json(result)["data"]


def test_help_does_not_create_project_or_registry_database(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = _run(tmp_path, "task", "add", "--help")

    assert result.returncode == 0
    assert not (tmp_path / ".offwork").exists()
    assert not (project / ".offwork").exists()
    assert not (tmp_path / "xdg-data").exists()


def test_task_mutations_keep_ids_states_and_revisions_isolated(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _add(tmp_path, project, "first")
    second = _add(tmp_path, project, "second")

    assert re.fullmatch(r"tsk_[0-9a-f]{32}", first["task_id"])
    assert first["task_id"] != second["task_id"]
    assert first["status"] == second["status"] == "todo"
    assert first["revision"] == second["revision"] == 1
    assert first["created_at"].endswith("Z")

    started = _run(
        tmp_path,
        "task",
        "start",
        first["task_id"],
        "--project",
        str(project),
        "--json",
    )
    assert started.returncode == 0
    assert _json(started)["data"]["revision"] == 2

    first_show = _run(
        tmp_path,
        "task",
        "show",
        first["task_id"],
        "--project",
        str(project),
        "--json",
    )
    second_show = _run(
        tmp_path,
        "task",
        "show",
        second["task_id"],
        "--project",
        str(project),
        "--json",
    )
    assert _json(first_show)["data"]["status"] == "in_progress"
    assert _json(second_show)["data"]["status"] == "todo"
    assert _json(second_show)["data"]["revision"] == 1


def test_auto_complete_requires_at_least_one_acceptance_command(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    rejected = _run(
        tmp_path,
        "task",
        "add",
        "automatic",
        "--goal",
        "verify it",
        "--auto-complete",
        "--project",
        str(project),
        "--json",
    )

    payload = _json(rejected)
    assert rejected.returncode == 4
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTO_COMPLETE_REQUIRES_ACCEPTANCE"
    assert payload["data"] is None

    accepted = _run(
        tmp_path,
        "task",
        "add",
        "automatic",
        "--goal",
        "verify it",
        "--auto-complete",
        "--accept-cmd",
        "python3 -m pytest -q",
        "--project",
        str(project),
        "--json",
    )
    assert accepted.returncode == 0
    assert _json(accepted)["data"]["acceptance_commands"] == [
        "python3 -m pytest -q"
    ]


def test_blank_acceptance_command_is_rejected_by_service_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_module = importlib.import_module("offwork_capsule.state")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    service = state_module.StateService(project)

    with pytest.raises(state_module.OffworkError) as caught:
        service.add_task(
            "blank acceptance",
            "reject it",
            acceptance_commands=["   "],
        )
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.exit_code == 2

    result = _run(
        tmp_path,
        "task",
        "add",
        "blank automatic acceptance",
        "--goal",
        "reject it",
        "--auto-complete",
        "--accept-cmd",
        "   ",
        "--project",
        str(project),
        "--json",
    )
    assert result.returncode == 2
    assert _json(result)["error"]["code"] == "INVALID_ARGUMENT"


def test_cli_open_loops_can_make_a_task_waiting(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    waiting_loop = json.dumps(
        {"title": "owner reply", "disposition": "delegate", "note": "ask Alice"}
    )
    resolved_loop = json.dumps(
        {"title": "local check", "disposition": "resolve", "note": "done"}
    )

    created = _run(
        tmp_path,
        "task",
        "add",
        "waiting task",
        "--goal",
        "continue after reply",
        "--open-loop",
        waiting_loop,
        "--open-loop",
        resolved_loop,
        "--project",
        str(project),
        "--json",
    )

    assert created.returncode == 0
    task = _json(created)["data"]
    assert task["computed_state"] == "waiting"
    assert task["open_loops"] == [
        {"title": "owner reply", "disposition": "delegate", "note": "ask Alice"},
        {"title": "local check", "disposition": "resolve", "note": "done"},
    ]
    waiting = _run(
        tmp_path,
        "task",
        "list",
        "--waiting",
        "--project",
        str(project),
        "--json",
    )
    assert [item["task_id"] for item in _json(waiting)["data"]["tasks"]] == [
        task["task_id"]
    ]


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "[]",
        '{"title":"","disposition":"park","note":"later"}',
        '{"title":"reply","disposition":"unknown","note":"later"}',
        '{"title":"reply","disposition":"park","note":3}',
    ],
)
def test_cli_invalid_open_loop_is_input_error_envelope(
    tmp_path: Path, value: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = _run(
        tmp_path,
        "task",
        "add",
        "invalid loop",
        "--goal",
        "reject malformed input",
        "--open-loop",
        value,
        "--project",
        str(project),
        "--json",
    )

    assert result.returncode == 2
    payload = _json(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["error"]["message"].startswith("Invalid --open-loop:")


def test_dependency_missing_and_cycle_are_rejected_atomically(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _add(tmp_path, project, "first")
    second = _add(tmp_path, project, "second")
    third = _add(tmp_path, project, "third")

    missing = _run(
        tmp_path,
        "task",
        "dependency",
        "add",
        first["task_id"],
        "tsk_" + "0" * 32,
        "--project",
        str(project),
        "--json",
    )
    assert missing.returncode == 3
    assert _json(missing)["error"]["code"] == "TASK_NOT_FOUND"

    for task_id, dependency_id in (
        (first["task_id"], second["task_id"]),
        (second["task_id"], third["task_id"]),
    ):
        added = _run(
            tmp_path,
            "task",
            "dependency",
            "add",
            task_id,
            dependency_id,
            "--project",
            str(project),
            "--json",
        )
        assert added.returncode == 0

    cycle = _run(
        tmp_path,
        "task",
        "dependency",
        "add",
        third["task_id"],
        first["task_id"],
        "--project",
        str(project),
        "--json",
    )
    assert cycle.returncode == 4
    assert _json(cycle)["error"]["code"] == "DEPENDENCY_CYCLE"

    third_after = _run(
        tmp_path,
        "task",
        "show",
        third["task_id"],
        "--project",
        str(project),
        "--json",
    )
    task = _json(third_after)["data"]
    assert task["revision"] == 1
    assert task["dependencies"] == []


def test_actionable_list_and_show_share_computed_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    prerequisite = _add(tmp_path, project, "prerequisite")
    dependent = _add(tmp_path, project, "dependent")
    added = _run(
        tmp_path,
        "task",
        "dependency",
        "add",
        dependent["task_id"],
        prerequisite["task_id"],
        "--project",
        str(project),
        "--json",
    )
    assert added.returncode == 0

    blocked = _run(
        tmp_path,
        "task",
        "show",
        dependent["task_id"],
        "--project",
        str(project),
        "--json",
    )
    blocked_task = _json(blocked)["data"]
    assert blocked_task["computed_state"] == "blocked"
    assert blocked_task["blockers"] == [prerequisite["task_id"]]

    actionable = _run(
        tmp_path,
        "task",
        "list",
        "--actionable",
        "--project",
        str(project),
        "--json",
    )
    actionable_tasks = _json(actionable)["data"]["tasks"]
    assert [task["task_id"] for task in actionable_tasks] == [
        prerequisite["task_id"]
    ]
    assert actionable_tasks[0]["computed_state"] == "actionable"


def test_waiting_and_blocked_are_distinct_with_blocked_precedence(tmp_path: Path) -> None:
    state_module = importlib.import_module("offwork_capsule.state")
    project = tmp_path / "project"
    project.mkdir()
    os.environ["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    service = state_module.StateService(project)
    prerequisite = service.add_task("prerequisite", "finish prerequisite")
    waiting = service.add_task(
        "waiting",
        "wait for owner",
        open_loops=[{"title": "owner", "disposition": "delegate", "note": ""}],
    )
    both = service.add_task(
        "both",
        "wait after prerequisite",
        open_loops=[{"title": "later", "disposition": "park", "note": ""}],
    )
    service.add_dependency(both["task_id"], prerequisite["task_id"])

    assert service.show_task(waiting["task_id"])["computed_state"] == "waiting"
    both_state = service.show_task(both["task_id"])
    assert both_state["computed_state"] == "blocked"
    assert both_state["blockers"] == [prerequisite["task_id"]]


def test_complete_requires_confirmation_and_complete_dependencies(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    prerequisite = _add(tmp_path, project, "prerequisite")
    dependent = _add(tmp_path, project, "dependent")
    assert _run(
        tmp_path,
        "task",
        "dependency",
        "add",
        dependent["task_id"],
        prerequisite["task_id"],
        "--project",
        str(project),
        "--json",
    ).returncode == 0

    no_confirmation = _run(
        tmp_path,
        "task",
        "complete",
        prerequisite["task_id"],
        "--project",
        str(project),
        "--json",
    )
    assert no_confirmation.returncode == 4
    assert _json(no_confirmation)["error"]["code"] == "CONFIRMATION_REQUIRED"

    blocked = _run(
        tmp_path,
        "task",
        "complete",
        dependent["task_id"],
        "--confirm",
        "--project",
        str(project),
        "--json",
    )
    assert blocked.returncode == 4
    assert _json(blocked)["error"]["code"] == "DEPENDENCY_NOT_COMPLETE"

    completed = _run(
        tmp_path,
        "task",
        "complete",
        prerequisite["task_id"],
        "--confirm",
        "--project",
        str(project),
        "--json",
    )
    assert completed.returncode == 0
    assert _json(completed)["data"]["computed_state"] == "terminal"

    restart = _run(
        tmp_path,
        "task",
        "start",
        prerequisite["task_id"],
        "--project",
        str(project),
        "--json",
    )
    assert restart.returncode == 4
    assert _json(restart)["error"]["code"] == "INVALID_TASK_STATE"


def test_archive_and_unarchive_preserve_task_history(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    created = _add(tmp_path, project, "history")

    archived = _run(
        tmp_path,
        "task",
        "archive",
        created["task_id"],
        "--project",
        str(project),
        "--json",
    )
    archived_task = _json(archived)["data"]
    assert archived_task["computed_state"] == "archived"
    assert archived_task["archived_at"].endswith("Z")
    assert archived_task["revision"] == 2

    listed = _run(
        tmp_path,
        "task",
        "list",
        "--archived",
        "--project",
        str(project),
        "--json",
    )
    assert _json(listed)["data"]["tasks"][0]["task_id"] == created["task_id"]

    restored = _run(
        tmp_path,
        "task",
        "unarchive",
        created["task_id"],
        "--project",
        str(project),
        "--json",
    )
    restored_task = _json(restored)["data"]
    assert restored_task["archived_at"] is None
    assert restored_task["revision"] == 3
    assert restored_task["status"] == "todo"


def test_status_project_and_all_use_registry_summaries(tmp_path: Path) -> None:
    first_project = tmp_path / "first-project"
    second_project = tmp_path / "second-project"
    first_project.mkdir()
    second_project.mkdir()
    first = _add(tmp_path, first_project, "first")
    second = _add(tmp_path, second_project, "second")
    _run(
        tmp_path,
        "task",
        "start",
        second["task_id"],
        "--project",
        str(second_project),
        "--json",
    )

    project_status = _run(
        tmp_path,
        "status",
        "--project",
        str(first_project),
        "--json",
    )
    status_data = _json(project_status)["data"]
    assert status_data["current_focus"]["task_id"] == first["task_id"]
    assert status_data["recommended_next"]["task_id"] == first["task_id"]
    assert status_data["counts"] == {"actionable": 1, "blocked": 0, "waiting": 0}
    assert status_data["latest_verified_capsule"] is None

    all_status = _run(tmp_path, "status", "--all", "--json")
    all_data = _json(all_status)["data"]
    assert {project["canonical_path"] for project in all_data["projects"]} == {
        str(first_project.resolve()),
        str(second_project.resolve()),
    }
    summaries = [
        task
        for project in all_data["projects"]
        for task in project["tasks"]
    ]
    assert {task["task_id"] for task in summaries} == {
        first["task_id"],
        second["task_id"],
    }


def test_json_empty_and_error_are_single_parseable_envelopes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    empty = _run(
        tmp_path,
        "task",
        "list",
        "--project",
        str(project),
        "--json",
    )
    assert empty.returncode == 0
    assert _json(empty)["data"] == {"tasks": []}

    missing = _run(
        tmp_path,
        "task",
        "show",
        "tsk_" + "f" * 32,
        "--project",
        str(project),
        "--json",
    )
    payload = _json(missing)
    assert missing.returncode == 3
    assert payload["error"] == {
        "code": "TASK_NOT_FOUND",
        "message": "Task not found: tsk_" + "f" * 32,
        "details": {"task_id": "tsk_" + "f" * 32},
        "recovery": "Check the task ID with `offwork task list`.",
    }


def test_task_show_human_output_contains_complete_task_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    created = _add(
        tmp_path,
        project,
        "human detail",
        "--accept-cmd",
        "python3 -m pytest -q",
        "--open-loop",
        '{"title":"reply","disposition":"park","note":"tomorrow"}',
    )

    result = _run(
        tmp_path,
        "task",
        "show",
        created["task_id"],
        "--project",
        str(project),
    )

    assert result.returncode == 0
    assert result.stderr == ""
    for expected in (
        "Title: human detail",
        "ID: " + created["task_id"],
        "Status: todo",
        "Computed: waiting",
        "Goal: finish human detail",
        "Revision: 1",
        "Archived at: None",
        "Dependencies: None",
        "Blockers: None",
        "Acceptance:\n- python3 -m pytest -q",
        "Open loops:\n- [park] reply — tomorrow",
    ):
        assert expected in result.stdout


def test_project_status_human_output_names_focus_next_counts_and_capsule(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    created = _add(tmp_path, project, "human status")

    result = _run(
        tmp_path,
        "status",
        "--project",
        str(project),
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert "Current focus: %s  human status" % created["task_id"] in result.stdout
    assert "Recommended next: %s  human status" % created["task_id"] in result.stdout
    assert "Counts: actionable=1 blocked=0 waiting=0" in result.stdout
    assert "Latest verified capsule: None" in result.stdout


def test_registry_failure_warns_after_project_transaction_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_module = importlib.import_module("offwork_capsule.state")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    service = state_module.StateService(project)

    def fail_registry() -> None:
        raise sqlite3.OperationalError("registry unavailable")

    monkeypatch.setattr(service, "_sync_registry", fail_registry)
    task = service.add_task("durable", "commit locally")

    assert service.warnings[0]["code"] == "REGISTRY_SYNC_FAILED"
    assert state_module.StateService(project).show_task(task["task_id"])["title"] == "durable"


def test_stale_revision_is_rejected_without_mutation(tmp_path: Path) -> None:
    state_module = importlib.import_module("offwork_capsule.state")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch_home = tmp_path / "xdg-data"
    os.environ["XDG_DATA_HOME"] = str(monkeypatch_home)
    service = state_module.StateService(project)
    task = service.add_task("revisioned", "use optimistic revision")
    service.start_task(task["task_id"], expected_revision=1)

    with pytest.raises(state_module.OffworkError) as caught:
        service.archive_task(task["task_id"], expected_revision=1)

    assert caught.value.code == "STALE_REVISION"
    current = service.show_task(task["task_id"])
    assert current["revision"] == 2
    assert current["archived_at"] is None


def test_cli_rejects_stale_revision_without_mutation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = _add(tmp_path, project, "revisioned")
    started = _run(
        tmp_path,
        "task",
        "start",
        task["task_id"],
        "--revision",
        "1",
        "--project",
        str(project),
        "--json",
    )
    assert started.returncode == 0

    stale = _run(
        tmp_path,
        "task",
        "archive",
        task["task_id"],
        "--revision",
        "1",
        "--project",
        str(project),
        "--json",
    )
    assert stale.returncode == 4
    assert _json(stale)["error"]["code"] == "STALE_REVISION"

    shown = _run(
        tmp_path,
        "task",
        "show",
        task["task_id"],
        "--project",
        str(project),
        "--json",
    )
    current = _json(shown)["data"]
    assert current["revision"] == 2
    assert current["archived_at"] is None


def test_older_registry_sync_cannot_overwrite_newer_task_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_module = importlib.import_module("offwork_capsule.state")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    old_service = state_module.StateService(project)
    task = old_service.add_task("ordered", "keep newest summary")
    new_service = state_module.StateService(project)
    real_initialize = state_module.initialize_global_registry
    old_snapshot_ready = threading.Event()
    release_old_sync = threading.Event()
    failures: list[BaseException] = []

    def delayed_initialize():
        if threading.current_thread().name == "old-sync":
            old_snapshot_ready.set()
            if not release_old_sync.wait(timeout=5):
                raise RuntimeError("timed out waiting to release old sync")
        return real_initialize()

    def start_old_mutation() -> None:
        try:
            old_service.start_task(task["task_id"], expected_revision=1)
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(state_module, "initialize_global_registry", delayed_initialize)
    worker = threading.Thread(target=start_old_mutation, name="old-sync")
    worker.start()
    assert old_snapshot_ready.wait(timeout=5)
    archived = new_service.archive_task(task["task_id"], expected_revision=2)
    assert archived["revision"] == 3
    release_old_sync.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert failures == []

    summary = state_module.registry_status()["projects"][0]["tasks"][0]
    assert summary["revision"] == 3
    assert summary["archived_at"] is not None


def test_v2_status_migration_repairs_equal_revision_legacy_registry_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_module = importlib.import_module("offwork_capsule.state")
    project = tmp_path / "project"
    offwork = project / ".offwork"
    offwork.mkdir(parents=True)
    project_id = "11111111-1111-4111-8111-111111111111"
    (offwork / "project.json").write_text(
        json.dumps({"project_id": project_id}), encoding="utf-8"
    )
    database = offwork / "state.sqlite3"
    with sqlite3.connect(str(database)) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', goal TEXT NOT NULL DEFAULT '',
                acceptance_commands_json TEXT NOT NULL DEFAULT '[]',
                auto_complete INTEGER NOT NULL DEFAULT 0,
                open_loops_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, archived_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO tasks VALUES (
                'task-a', 'Legacy task', '', 'Migrate it', '[]', 0, '[]',
                'active', 2, NULL, 'before', 'before'
            );
            PRAGMA user_version = 2;
            """
        )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    registry = state_module.initialize_global_registry()
    with registry.connect() as connection:
        connection.execute(
            "INSERT INTO projects(project_id, canonical_path, state_database_path, "
            "last_seen_at) VALUES (?, ?, ?, 'before')",
            (project_id, str(project.resolve()), str(database.resolve())),
        )
        connection.execute(
            "INSERT INTO task_summaries(task_id, project_id, title, status, revision, "
            "archived_at, updated_at) VALUES "
            "('task-a', ?, 'Legacy task', 'active', 2, NULL, 'before')",
            (project_id,),
        )

    service = state_module.StateService(project)
    assert service.show_task("task-a")["status"] == "in_progress"
    service._sync_registry()

    all_status = _run(tmp_path, "status", "--all", "--json")
    summary = _json(all_status)["data"]["projects"][0]["tasks"][0]
    assert summary["status"] == "in_progress"
    assert summary["revision"] == 2


def test_equal_revision_nonlegacy_registry_summary_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_module = importlib.import_module("offwork_capsule.state")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    service = state_module.StateService(project)
    task = service.add_task("old project title", "keep monotonic registry")
    registry = state_module.initialize_global_registry()
    with registry.connect() as connection:
        connection.execute(
            "UPDATE task_summaries SET title = 'new registry title', "
            "status = 'complete', archived_at = 'new-archive', updated_at = 'new' "
            "WHERE task_id = ?",
            (task["task_id"],),
        )

    service._sync_registry()

    summary = state_module.registry_status()["projects"][0]["tasks"][0]
    assert summary["revision"] == 1
    assert summary["title"] == "new registry title"
    assert summary["status"] == "complete"
    assert summary["archived_at"] == "new-archive"


def test_registry_keeps_same_task_id_from_two_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_module = importlib.import_module("offwork_capsule.state")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    projects = [tmp_path / "first", tmp_path / "second"]
    services = []
    for index, project in enumerate(projects, start=1):
        project.mkdir()
        service = state_module.StateService(project)
        with service.storage.connect() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id, title, goal, status, created_at, updated_at) "
                "VALUES ('shared-task', ?, ?, 'todo', 'before', 'before')",
                ("Project %d task" % index, "Project %d goal" % index),
            )
        service._sync_registry()
        services.append(service)

    all_status = state_module.registry_status()["projects"]
    summaries = [
        (project["project_id"], task["task_id"], task["title"])
        for project in all_status
        for task in project["tasks"]
    ]
    assert summaries == [
        (services[0].storage.project_id, "shared-task", "Project 1 task"),
        (services[1].storage.project_id, "shared-task", "Project 2 task"),
    ]
