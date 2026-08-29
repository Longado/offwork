from __future__ import annotations

import json
from typing import Any, Dict

from offwork.errors import OffworkError


SCHEMA_VERSION = "offwork.cli/v1"


def success_envelope(command: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "command": command,
        "data": data,
    }


def error_envelope(command: str, error: OffworkError) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "command": command,
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        },
    }


def write_json(value: Dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _visible(value: Any) -> str:
    rendered = []
    for character in str(value):
        codepoint = ord(character)
        if character == "\n":
            rendered.append("\\n")
        elif character == "\r":
            rendered.append("\\r")
        elif character == "\t":
            rendered.append("\\t")
        elif codepoint < 32 or codepoint == 127:
            rendered.append(f"\\x{codepoint:02x}")
        else:
            rendered.append(character)
    return "".join(rendered)


def render_receipt(receipt: Dict[str, Any]) -> str:
    task = receipt["task"]
    capsule = receipt["capsule"]
    claimed = receipt["agent_claimed"]
    checks = receipt["auto_checked"]
    verified = receipt["handoff_verified"]
    freshness = receipt["workspace_freshness"]
    acceptance = receipt["human_acceptance"]
    lines = [
        "HANDOFF RECEIPT",
        "",
        f"Task: {_visible(task['title'])}",
        f"Goal: {_visible(task['goal'])}",
        f"Capsule: {_visible(capsule['capsule_id'])}",
        f"Task revision: {task['current_revision']} (captured {task['captured_revision']})",
        "",
        "Agent claimed:",
        f"- {_visible(claimed['summary'])}",
    ]
    lines.extend(f"- {_visible(item)}" for item in claimed["items"])
    lines.extend(["", "Verified by Offwork:", f"- Checks: {checks['status']}"])
    lines.extend(
        [
            f"- Integrity: {verified['integrity']['status']}",
            f"- Completeness: {verified['completeness']['status']}",
            f"- Restore: {verified['restore']['status']}",
            "",
            "Unknowns:",
        ]
    )
    lines.extend(f"- {_visible(item)}" for item in receipt["unknowns"])
    lines.extend(["", "Open loops:"])
    lines.extend(f"- {_visible(item['title'])}" for item in receipt["open_loops"])
    lines.extend(
        [
            "",
            f"Next step: {_visible(receipt['next_step'])}",
            f"Workspace freshness: {freshness['status']}",
            f"Human acceptance: {acceptance['status']}",
        ]
    )
    return "\n".join(lines) + "\n"
