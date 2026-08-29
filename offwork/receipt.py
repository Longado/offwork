from __future__ import annotations

from typing import Any, Dict, Optional

from offwork.capsule import load_capsule, reconcile_capsules
from offwork.project import capture_workspace, compare_workspace
from offwork.state import StateService, utc_now


def build_receipt(
    project: Dict[str, Any],
    task_id: str,
    capsule_id: Optional[str] = None,
    *,
    reconcile_orphans: bool = True,
) -> Dict[str, Any]:
    if reconcile_orphans:
        reconcile_capsules(project, task_id)
    state = StateService(project["state_dir"])
    receipt_state = state.get_receipt_state(task_id, capsule_id)
    task = receipt_state["task"]
    capsule_row = receipt_state["capsule"]
    loaded = load_capsule(
        project["state_dir"],
        capsule_row["archive_path"],
        capsule_row["capsule_id"],
        capsule_row["manifest_hash"],
    )
    receipt_input = loaded["receipt_input"]
    capsule = receipt_input["capsule"]
    comparison = compare_workspace(
        receipt_input["workspace_snapshot"], capture_workspace(project)
    )
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
        "agent_claimed": receipt_input["agent_claimed"],
        "offwork_observed": receipt_input["offwork_observed"],
        "auto_checked": receipt_input["auto_checked"],
        "handoff_verified": loaded["handoff_verification"],
        "unknowns": receipt_input["unknowns"],
        "open_loops": receipt_input["open_loops"],
        "next_step": receipt_input["next_step"],
        "workspace_freshness": {
            "status": comparison["status"],
            "scope": "explicit_git_project",
            "checked_at": utc_now(),
            "changes": comparison["changes"],
            "limitations": comparison["limitations"],
        },
        "human_acceptance": receipt_state["acceptance"],
    }
