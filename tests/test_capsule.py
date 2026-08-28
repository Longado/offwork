import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from offwork_capsule.capsule import (
    CapsuleValidationError,
    archive_capsule,
    build_capsule,
    load_latest_capsule,
    render_capsule_markdown,
    render_resume,
    validate_for_restore,
)
from offwork_capsule.project import capture_project_state
from offwork_capsule.verifier import (
    build_restore_prompt,
    merge_restore_tests,
    parse_claude_result,
    run_command_verifier,
)


NOW = datetime(2026, 8, 25, 17, 58, tzinfo=timezone.utc)


def complete_context() -> dict:
    return {
        "goal": "完成客户演示方案的最终确认",
        "summary": "已完成流程设计，仍需确认报价口径。",
        "decisions": ["演示只覆盖审批主流程"],
        "failed_attempts": ["直接复用旧报价模板，字段不匹配"],
        "next_step": "确认报价口径并更新演示材料",
        "next_command": "open docs/demo-plan.md",
        "open_loops": [
            {
                "title": "报价口径待确认",
                "disposition": "park",
                "note": "明早联系产品经理",
            },
            {
                "title": "旧模板兼容",
                "disposition": "drop",
                "note": "不再复用旧模板",
            },
        ],
    }


def test_build_archive_and_resume_complete_capsule(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "demo@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Demo"], cwd=project, check=True)
    (project / "proposal.md").write_text("draft\n", encoding="utf-8")

    state = capture_project_state(project)
    capsule = build_capsule(complete_context(), state, captured_at=NOW)
    restore_test = validate_for_restore(capsule)
    archive_dir = archive_capsule(project, capsule, restore_test)

    assert restore_test["ready_to_resume"] is True
    assert (archive_dir / "capsule.json").exists()
    assert (archive_dir / "capsule.md").exists()
    assert (archive_dir / "restore-test.json").exists()
    assert load_latest_capsule(project)["id"] == capsule["id"]
    assert "报价口径待确认" in render_capsule_markdown(capsule)
    assert "确认报价口径并更新演示材料" in render_resume(capsule)
    assert "open docs/demo-plan.md" in render_resume(capsule)


def test_unsettled_open_loop_blocks_hibernation(tmp_path: Path) -> None:
    context = complete_context()
    context["open_loops"].append(
        {"title": "客户问题未回复", "disposition": "unresolved", "note": ""}
    )

    with pytest.raises(CapsuleValidationError, match="未安置"):
        build_capsule(
            context,
            {"project_path": str(tmp_path), "is_git_repo": False},
            captured_at=NOW,
        )


@pytest.mark.parametrize("missing_field", ["goal", "next_step"])
def test_required_context_is_enforced(tmp_path: Path, missing_field: str) -> None:
    context = complete_context()
    context[missing_field] = ""

    with pytest.raises(CapsuleValidationError, match=missing_field):
        build_capsule(
            context,
            {"project_path": str(tmp_path), "is_git_repo": False},
            captured_at=NOW,
        )


def test_project_capture_is_read_only(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "notes.md").write_text("unfinished\n", encoding="utf-8")

    before = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    state = capture_project_state(project)
    after = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert state["dirty_files"] == ["notes.md"]
    assert before == after


def test_cli_capture_and_resume_json(tmp_path: Path) -> None:
    project = tmp_path / "workspace"
    project.mkdir()
    context_file = tmp_path / "context.json"
    context_file.write_text(
        json.dumps(complete_context(), ensure_ascii=False), encoding="utf-8"
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    capture = subprocess.run(
        [
            sys.executable,
            "-m",
            "offwork_capsule.cli",
            "capture",
            "--project",
            str(project),
            "--context",
            str(context_file),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    capture_result = json.loads(capture.stdout)

    envelope_keys = {
        "schema_version",
        "command",
        "ok",
        "data",
        "meta",
        "warnings",
        "error",
    }
    assert set(capture_result) == envelope_keys
    assert capture_result["schema_version"] == "offwork.cli/v1"
    assert capture_result["ok"] is True
    assert capture_result["data"]["status"] == "WORKSPACE HIBERNATED"
    assert capture_result["data"]["restore_test"]["ready_to_resume"] is True

    resume = subprocess.run(
        [
            sys.executable,
            "-m",
            "offwork_capsule.cli",
            "resume",
            "--project",
            str(project),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    resume_result = json.loads(resume.stdout)

    assert set(resume_result) == envelope_keys
    assert resume_result["schema_version"] == "offwork.cli/v1"
    assert resume_result["ok"] is True
    assert resume_result["data"]["goal"] == complete_context()["goal"]
    assert resume_result["data"]["next_step"] == complete_context()["next_step"]


def test_independent_verifier_receives_capsule_and_can_resume(tmp_path: Path) -> None:
    project_state = {"project_path": str(tmp_path), "is_git_repo": False}
    capsule = build_capsule(complete_context(), project_state, captured_at=NOW)
    verifier_program = """
import json
import sys

payload = json.loads(sys.stdin.read())
capsule = payload["capsule"]
print(json.dumps({
    "ready_to_resume": bool(capsule["goal"] and capsule["next_step"]),
    "understood_goal": capsule["goal"],
    "current_state": capsule["summary"],
    "next_action": capsule["next_step"],
    "missing_information": []
}))
"""

    agent_test = run_command_verifier(
        capsule, [sys.executable, "-c", verifier_program]
    )
    combined = merge_restore_tests(validate_for_restore(capsule), agent_test)

    assert agent_test["mode"] == "fresh-agent"
    assert agent_test["ready_to_resume"] is True
    assert combined["ready_to_resume"] is True


def test_agent_missing_information_blocks_restore(tmp_path: Path) -> None:
    capsule = build_capsule(
        complete_context(),
        {"project_path": str(tmp_path), "is_git_repo": False},
        captured_at=NOW,
    )
    agent_test = {
        "mode": "fresh-agent",
        "ready_to_resume": False,
        "understood_goal": capsule["goal"],
        "current_state": "",
        "next_action": "",
        "missing_information": ["缺少客户联系人"],
    }

    combined = merge_restore_tests(validate_for_restore(capsule), agent_test)

    assert combined["ready_to_resume"] is False
    assert combined["missing_information"] == ["缺少客户联系人"]

    with pytest.raises(CapsuleValidationError, match="缺少客户联系人"):
        archive_capsule(tmp_path, capsule, combined)


def test_parse_claude_structured_result() -> None:
    wrapper = {
        "type": "result",
        "subtype": "success",
        "structured_output": {
            "ready_to_resume": True,
            "understood_goal": "确认客户方案",
            "current_state": "等待报价口径",
            "next_action": "联系产品经理",
            "missing_information": [],
        },
    }

    result = parse_claude_result(json.dumps(wrapper, ensure_ascii=False))

    assert result["mode"] == "fresh-agent"
    assert result["ready_to_resume"] is True


def test_restore_prompt_tests_safe_restart_not_full_completion(tmp_path: Path) -> None:
    capsule = build_capsule(
        complete_context(),
        {"project_path": str(tmp_path), "is_git_repo": False},
        captured_at=NOW,
    )

    prompt = build_restore_prompt(capsule)

    assert "开始第一步" in prompt
    assert "完成整个任务" in prompt
    assert json.dumps(capsule, ensure_ascii=False, indent=2) in prompt
