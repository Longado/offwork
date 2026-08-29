from __future__ import annotations

from typing import Any, Dict, Optional

from offwork.capsule import load_capsule, reconcile_capsules
from offwork.project import capture_workspace, compare_workspace
from offwork.state import StateService, utc_now


def build_receipt(
    project: Dict[str, Any], task_id: str, capsule_id: Optional[str] = None
) -> Dict[str, Any]:
    reconcile_capsules(project, task_id)
    state = StateService(project["state_dir"])
    task = state.get_task(task_id)
    capsule_row = state.get_capsule(task_id, capsule_id)
    loaded = load_capsule(
        project["state_dir"],
        capsule_row["archive_path"],
        capsule_row["capsule_id"],
        capsule_row["manifest_hash"],
    )
    capsule = loaded["capsule"]
    context = capsule["context"]
    comparison = compare_workspace(capsule["workspace_snapshot"], capture_workspace(project))
    return {
        "schema_version": "offwork.receipt/v1",
        "task": {
            "task_id": task["task_id"],
            "title": task["title"],
            "goal": task["goal"],
            "current_revision": task["revision"],
            "captured_revision": capsule_row["captured_task_revision"],
        },
        "capsule": {
            "capsule_id": capsule["capsule_id"],
            "captured_at": capsule["captured_at"],
        },
        "agent_claimed": {
            "source": "capture_context",
            "summary": context["summary"],
            "items": context["agent_claims"],
        },
        "offwork_observed": capsule["observed"],
        "auto_checked": loaded["checks"],
        "handoff_verified": {
            "integrity": {"status": "passed"},
            "completeness": {"status": "complete", "missing_information": []},
            "restore": {"status": loaded["restore"]["status"]},
        },
        "unknowns": context["unknowns"],
        "open_loops": context["open_loops"],
        "next_step": context["next_step"],
        "workspace_freshness": {
            "status": comparison["status"],
            "scope": "explicit_git_project",
            "checked_at": utc_now(),
            "changes": comparison["changes"],
            "limitations": comparison["limitations"],
        },
        "human_acceptance": {"status": "pending", "acted_at": None, "note": None},
    }
