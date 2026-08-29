from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict

from offwork.errors import OffworkError
from offwork.state import StateService, utc_now


PAYLOAD_NAMES = ("capsule.json", "checks.json", "restore-test.json")


def _json_bytes(value: Dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_context(path: str) -> Dict[str, Any]:
    context_path = Path(path)
    try:
        value = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OffworkError(
            "INVALID_CAPTURE_CONTEXT",
            "Capture context must be readable JSON",
            details={"path": str(context_path)},
        ) from exc
    if not isinstance(value, dict):
        raise OffworkError("INVALID_CAPTURE_CONTEXT", "Capture context must be an object")
    required_strings = ("summary", "next_step")
    required_lists = ("agent_claims", "unknowns", "open_loops")
    if any(not isinstance(value.get(name), str) or not value[name].strip() for name in required_strings):
        raise OffworkError(
            "INVALID_CAPTURE_CONTEXT",
            "Capture context requires non-empty summary and next_step",
        )
    if any(not isinstance(value.get(name), list) for name in required_lists):
        raise OffworkError(
            "INVALID_CAPTURE_CONTEXT",
            "Capture context requires agent_claims, unknowns, and open_loops arrays",
        )
    return value


def capture(
    project: Dict[str, Any], task_id: str, context_path: str
) -> str:
    state = StateService(project["state_dir"])
    task = state.get_task(task_id)
    context = load_context(context_path)
    capsule_id = f"capsule-{uuid.uuid4().hex}"
    captured_at = utc_now()
    captured_revision = task["revision"] + 1
    capsule_value = {
        "schema_version": "offwork.capsule/v1",
        "capsule_id": capsule_id,
        "captured_at": captured_at,
        "task": {
            "task_id": task_id,
            "title": task["title"],
            "goal": task["goal"],
            "captured_revision": captured_revision,
        },
        "context": context,
        "observed": {
            "project_id": project["project_id"],
            "project_path": project["project_path"],
            "git_root": None,
            "branch": None,
            "head": None,
            "changed_paths": [],
        },
        "workspace_snapshot": None,
    }
    checks_value = {
        "schema_version": "offwork.checks/v1",
        "status": "not_run",
        "checks": [],
    }
    restore_value = {
        "schema_version": "offwork.restore-test/v1",
        "status": "passed",
    }
    payloads = {
        "capsule.json": _json_bytes(capsule_value),
        "checks.json": _json_bytes(checks_value),
        "restore-test.json": _json_bytes(restore_value),
    }
    manifest_value = {
        "schema_version": "offwork.manifest/v1",
        "capsule_id": capsule_id,
        "files": {
            name: {
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in payloads.items()
        },
    }
    manifest_payload = _json_bytes(manifest_value)

    capsules_dir = project["state_dir"] / "capsules"
    staging = capsules_dir / f".staging-{uuid.uuid4().hex}"
    final = capsules_dir / capsule_id
    staging.mkdir(mode=0o700)
    try:
        for name, payload in payloads.items():
            _write_private(staging / name, payload)
        _write_private(staging / "manifest.json", manifest_payload)
        os.rename(staging, final)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    state.register_capsule(
        capsule_id=capsule_id,
        task_id=task_id,
        archive_path=f"capsules/{capsule_id}",
        manifest_hash=hashlib.sha256(manifest_payload).hexdigest(),
        expected_revision=task["revision"],
        created_at=captured_at,
    )
    return capsule_id


def load_capsule(state_dir: Path, archive_path: str) -> Dict[str, Any]:
    directory = state_dir / archive_path
    try:
        capsule_value = json.loads((directory / "capsule.json").read_text(encoding="utf-8"))
        checks_value = json.loads((directory / "checks.json").read_text(encoding="utf-8"))
        restore_value = json.loads(
            (directory / "restore-test.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise OffworkError(
            "CAPSULE_LOAD_FAILED",
            "Published Capsule could not be reloaded",
            details={"archive_path": archive_path},
        ) from exc
    return {
        "capsule": capsule_value,
        "checks": checks_value,
        "restore": restore_value,
    }
