from __future__ import annotations

import json
import unicodedata
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
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
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
        elif 128 <= codepoint <= 159:
            rendered.append(f"\\u{codepoint:04x}")
        elif unicodedata.category(character) == "Cf" or codepoint in (0x2028, 0x2029):
            if codepoint <= 0xFFFF:
                rendered.append(f"\\u{codepoint:04x}")
            else:
                rendered.append(f"\\U{codepoint:08x}")
        else:
            rendered.append(character)
    return "".join(rendered)


def visible_text(value: Any) -> str:
    return _visible(value)


def _visible_list(values: Any) -> str:
    return _visible(json.dumps(values, ensure_ascii=False, separators=(",", ":")))


def render_error(error: OffworkError) -> str:
    lines = [f"{_visible(error.code)}: {_visible(error.message)}"]
    labels = {
        "capsule_id": "Capsule ID",
        "integrity": "Integrity",
        "freshness": "Workspace freshness",
    }
    for key, value in error.details.items():
        label = labels.get(key, key.replace("_", " ").capitalize())
        rendered_value = (
            _visible_list(value)
            if isinstance(value, (list, dict))
            else _visible(value)
        )
        lines.append(f"{label}: {rendered_value}")
    return "\n".join(lines) + "\n"


def render_receipt(receipt: Dict[str, Any]) -> str:
    task = receipt["task"]
    capsule = receipt["capsule"]
    claimed = receipt["agent_claimed"]
    observed = receipt["offwork_observed"]
    checks = receipt["auto_checked"]
    verified = receipt["handoff_verified"]
    freshness = receipt["workspace_freshness"]
    acceptance = receipt["human_acceptance"]
    lines = [
        "HANDOFF RECEIPT",
        "",
        "Task:",
        f"- ID: {_visible(task['task_id'])}",
        f"- Title: {_visible(task['title'])}",
        f"- Goal: {_visible(task['goal'])}",
        f"- Current revision: {_visible(task['current_revision'])}",
        f"- Captured revision: {_visible(task['captured_revision'])}",
        "",
        "Capsule:",
        f"- ID: {_visible(capsule['capsule_id'])}",
        f"- Captured at: {_visible(capsule['captured_at'])}",
        "",
        "Agent claimed:",
        f"- Source: {_visible(claimed['source'])}",
        f"- {_visible(claimed['summary'])}",
    ]
    lines.extend(f"- {_visible(item)}" for item in claimed["items"])
    lines.extend(
        [
            "",
            "Observed by Offwork:",
            f"- Project ID: {_visible(observed.get('project_id'))}",
            f"- Project: {_visible(observed['project_path'])}",
            f"- Git root: {_visible(observed.get('git_root'))}",
            f"- Branch: {_visible(observed.get('branch'))}",
            f"- HEAD: {_visible(observed.get('head'))}",
        ]
    )
    lines.append(
        "- Captured changes: "
        + (
            ", ".join(_visible(path) for path in observed.get("changed_paths", []))
            or "none"
        )
    )
    lines.extend(["", "Verified by Offwork:", f"- Checks: {_visible(checks['status'])}"])
    for number, check in enumerate(checks["checks"], start=1):
        lines.extend(
            [
                f"- Check {number}:",
                f"  - Command: {_visible(check['command'])}",
                f"  - Argv: {_visible_list(check['argv'])}",
                f"  - CWD: {_visible(check['cwd'])}",
                f"  - Status: {_visible(check['status'])}",
                f"  - Return code: {_visible(check['returncode'])}",
                f"  - Started at: {_visible(check['started_at'])}",
                f"  - Finished at: {_visible(check['finished_at'])}",
            ]
        )
    lines.extend(
        [
            f"- Integrity: {_visible(verified['integrity']['status'])}",
            f"- Completeness: {_visible(verified['completeness']['status'])}",
            "- Missing information: "
            + (_visible_list(verified["completeness"]["missing_information"])),
            f"- Restore: {_visible(verified['restore']['status'])}",
            "",
            "Unknowns:",
        ]
    )
    if receipt["unknowns"]:
        lines.extend(f"- {_visible(item)}" for item in receipt["unknowns"])
    else:
        lines.append("- none")
    lines.extend(["", "Open loops:"])
    if receipt["open_loops"]:
        for number, item in enumerate(receipt["open_loops"], start=1):
            lines.extend(
                [
                    f"- Loop {number}:",
                    f"  - Title: {_visible(item['title'])}",
                    f"  - Disposition: {_visible(item['disposition'])}",
                    f"  - Note: {_visible(item['note'])}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            f"Next step: {_visible(receipt['next_step'])}",
            "",
            "Workspace freshness:",
            f"- Status: {_visible(freshness['status'])}",
            f"- Scope: {_visible(freshness['scope'])}",
            f"- Checked at: {_visible(freshness['checked_at'])}",
            "Workspace changes: "
            + (", ".join(_visible(path) for path in freshness["changes"]) or "none"),
            "Workspace limitations: "
            + (", ".join(_visible(item) for item in freshness["limitations"]) or "none"),
            "",
            "Human acceptance:",
            f"- Status: {_visible(acceptance['status'])}",
            f"- Acted at: {_visible(acceptance.get('acted_at'))}",
            f"- Note: {_visible(acceptance.get('note'))}",
        ]
    )
    return "\n".join(lines) + "\n"
