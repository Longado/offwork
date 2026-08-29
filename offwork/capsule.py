from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any, Dict

from offwork.errors import OffworkError
from offwork.checks import run_checks
from offwork.project import capture_workspace
from offwork.state import (
    StateService,
    create_private_directory,
    utc_now,
    validate_private_path,
)


PAYLOAD_NAMES = ("capsule.json", "checks.json", "restore-test.json")
CAPSULE_MEMBERS = frozenset((*PAYLOAD_NAMES, "manifest.json"))


def _json_bytes(value: Dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _integrity_error(capsule_id: str, message: str) -> OffworkError:
    return OffworkError(
        "CAPSULE_INTEGRITY_FAILED",
        message,
        details={
            "capsule_id": capsule_id,
            "integrity": "failed",
            "freshness": "not_evaluated",
        },
    )


def _read_private_member(path: Path, capsule_id: str, name: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _integrity_error(
            capsule_id, f"Capsule member {name} cannot be opened safely"
        ) from exc
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode):
            raise _integrity_error(
                capsule_id, f"Capsule member {name} is not a regular file"
            )
        if current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) != 0o600:
            raise _integrity_error(
                capsule_id, f"Capsule member {name} permissions are unsafe"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    except OSError as exc:
        raise _integrity_error(capsule_id, f"Capsule member {name} cannot be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _cleanup_staging(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


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
    reconcile_capsules(project, task_id)
    task = state.get_task(task_id)
    context = load_context(context_path)
    checks_value = run_checks(task["check_commands"], project["path"])
    workspace_snapshot = capture_workspace(project)
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
            "git_root": workspace_snapshot.get("git_root"),
            "branch": workspace_snapshot.get("branch"),
            "head": workspace_snapshot.get("head"),
            "changed_paths": workspace_snapshot.get("changed_paths", []),
        },
        "workspace_snapshot": workspace_snapshot,
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
    validate_private_path(
        capsules_dir,
        expected_type="directory",
        expected_mode=0o700,
    )
    staging = capsules_dir / f".staging-{uuid.uuid4().hex}"
    final = capsules_dir / capsule_id
    create_private_directory(staging)
    try:
        for name, payload in payloads.items():
            _write_private(staging / name, payload)
        _write_private(staging / "manifest.json", manifest_payload)
        _fsync_directory(staging)
        os.rename(staging, final)
        _fsync_directory(capsules_dir)
    except OffworkError:
        _cleanup_staging(staging)
        raise
    except OSError as exc:
        _cleanup_staging(staging)
        raise OffworkError(
            "CAPSULE_PUBLICATION_FAILED",
            "Capsule could not be published safely",
            details={"path": str(staging)},
        ) from exc
    except Exception:
        _cleanup_staging(staging)
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


def _validated_archive_path(state_dir: Path, archive_path: str, capsule_id: str) -> Path:
    relative = Path(archive_path)
    if relative.is_absolute() or relative.parts != ("capsules", capsule_id):
        raise _integrity_error(capsule_id, "Capsule archive path is invalid")
    directory = state_dir / relative
    try:
        directory_stat = directory.lstat()
    except OSError as exc:
        raise _integrity_error(capsule_id, "Capsule directory is missing") from exc
    if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
        raise _integrity_error(capsule_id, "Capsule directory must be a real directory")
    if directory_stat.st_uid != os.getuid() or stat.S_IMODE(directory_stat.st_mode) != 0o700:
        raise _integrity_error(capsule_id, "Capsule directory permissions are unsafe")
    return directory


def _verify_directory(
    directory: Path, capsule_id: str, expected_manifest_hash: str | None
) -> Dict[str, Any]:
    try:
        members = {entry.name for entry in directory.iterdir()}
    except OSError as exc:
        raise _integrity_error(capsule_id, "Capsule directory cannot be read") from exc
    if members != CAPSULE_MEMBERS:
        raise _integrity_error(capsule_id, "Capsule members do not match the fixed contract")

    payload_bytes: Dict[str, bytes] = {}
    for name in CAPSULE_MEMBERS:
        path = directory / name
        payload_bytes[name] = _read_private_member(path, capsule_id, name)

    manifest_payload = payload_bytes["manifest.json"]
    manifest_hash = hashlib.sha256(manifest_payload).hexdigest()
    if expected_manifest_hash is not None and manifest_hash != expected_manifest_hash:
        raise _integrity_error(capsule_id, "Manifest hash does not match registered state")
    try:
        manifest = json.loads(manifest_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _integrity_error(capsule_id, "Manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise _integrity_error(capsule_id, "Manifest must be a JSON object")
    if (
        manifest.get("schema_version") != "offwork.manifest/v1"
        or manifest.get("capsule_id") != capsule_id
    ):
        raise _integrity_error(capsule_id, "Manifest schema or identity is invalid")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, dict):
        raise _integrity_error(capsule_id, "Manifest files must be a JSON object")
    if set(manifest_files) != set(PAYLOAD_NAMES):
        raise _integrity_error(capsule_id, "Manifest files do not match the fixed contract")

    values: Dict[str, Any] = {}
    expected_schemas = {
        "capsule.json": "offwork.capsule/v1",
        "checks.json": "offwork.checks/v1",
        "restore-test.json": "offwork.restore-test/v1",
    }
    for name in PAYLOAD_NAMES:
        payload = payload_bytes[name]
        declared = manifest_files[name]
        if not isinstance(declared, dict):
            raise _integrity_error(
                capsule_id, f"Manifest declaration for {name} must be a JSON object"
            )
        if declared.get("size") != len(payload) or declared.get("sha256") != hashlib.sha256(payload).hexdigest():
            raise _integrity_error(capsule_id, f"Capsule member {name} failed verification")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _integrity_error(capsule_id, f"Capsule member {name} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise _integrity_error(capsule_id, f"Capsule member {name} must be a JSON object")
        if value.get("schema_version") != expected_schemas[name]:
            raise _integrity_error(capsule_id, f"Capsule member {name} has an unsupported schema")
        values[name] = value
    if values["capsule.json"].get("capsule_id") != capsule_id:
        raise _integrity_error(capsule_id, "Capsule payload identity does not match its directory")
    return {
        "capsule": values["capsule.json"],
        "checks": values["checks.json"],
        "restore": values["restore-test.json"],
        "manifest_hash": manifest_hash,
    }


def load_capsule(
    state_dir: Path,
    archive_path: str,
    capsule_id: str,
    expected_manifest_hash: str,
) -> Dict[str, Any]:
    directory = _validated_archive_path(state_dir, archive_path, capsule_id)
    return _verify_directory(directory, capsule_id, expected_manifest_hash)


def reconcile_capsules(project: Dict[str, Any], task_id: str) -> None:
    state = StateService(project["state_dir"])
    task = state.get_task(task_id)
    expected_revision = task["revision"]
    next_revision = expected_revision + 1
    capsules_dir = project["state_dir"] / "capsules"
    next_candidates: list[Dict[str, Any]] = []
    skipped_candidates: list[Dict[str, Any]] = []
    for directory in capsules_dir.iterdir():
        capsule_id = directory.name
        if not capsule_id.startswith("capsule-") or state.capsule_registered(capsule_id):
            continue
        archive_path = f"capsules/{capsule_id}"
        try:
            validated = _validated_archive_path(project["state_dir"], archive_path, capsule_id)
            loaded = _verify_directory(validated, capsule_id, None)
        except OffworkError:
            continue
        capsule = loaded["capsule"]
        capsule_task = capsule.get("task", {})
        observed = capsule.get("observed", {})
        if (
            not isinstance(capsule_task, dict)
            or capsule_task.get("task_id") != task_id
            or not isinstance(observed, dict)
            or observed.get("project_id") != project["project_id"]
            or observed.get("project_path") != project["project_path"]
            or not isinstance(capsule.get("captured_at"), str)
            or not capsule["captured_at"]
        ):
            continue
        captured_revision = capsule_task.get("captured_revision")
        if not isinstance(captured_revision, int) or isinstance(captured_revision, bool):
            continue
        candidate = {
            "capsule_id": capsule_id,
            "archive_path": archive_path,
            "manifest_hash": loaded["manifest_hash"],
            "captured_revision": captured_revision,
            "created_at": capsule["captured_at"],
        }
        if captured_revision == next_revision:
            next_candidates.append(candidate)
        elif captured_revision > next_revision:
            skipped_candidates.append(candidate)

    if skipped_candidates:
        raise OffworkError(
            "CAPSULE_RECONCILIATION_GAP",
            "Published Capsules skip the Task's next revision",
            details={
                "task_id": task_id,
                "expected_captured_revision": next_revision,
                "candidate_capsule_ids": sorted(
                    candidate["capsule_id"] for candidate in skipped_candidates
                ),
            },
        )
    if len(next_candidates) > 1:
        raise OffworkError(
            "CAPSULE_RECONCILIATION_AMBIGUOUS",
            "Multiple published Capsules claim the Task's next revision",
            details={
                "task_id": task_id,
                "expected_captured_revision": next_revision,
                "candidate_capsule_ids": sorted(
                    candidate["capsule_id"] for candidate in next_candidates
                ),
            },
        )
    if not next_candidates:
        return

    candidate = next_candidates[0]
    state.reconcile_capsule(
        capsule_id=candidate["capsule_id"],
        task_id=task_id,
        archive_path=candidate["archive_path"],
        manifest_hash=candidate["manifest_hash"],
        captured_revision=candidate["captured_revision"],
        expected_revision=expected_revision,
        expected_current_capsule_id=task["current_capsule_id"],
        created_at=candidate["created_at"],
    )
