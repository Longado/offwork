from __future__ import annotations

import json
import hashlib
import os
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict

from offwork.errors import OffworkError
from offwork.state import (
    PRIVATE_FILE_MODE,
    create_private_directory,
    initialize_database,
    validate_database_paths,
    validate_private_path,
)


STATE_DIR_NAME = ".offwork"


def canonical_project(path: str) -> Path:
    project = Path(path).expanduser().resolve()
    if not project.is_dir():
        raise OffworkError(
            "PROJECT_NOT_FOUND",
            "Project path must be an existing directory",
            details={"project_path": str(project)},
        )
    return project


def _ensure_directory(path: Path, mode: int) -> None:
    existing = validate_private_path(
        path,
        expected_type="directory",
        expected_mode=mode,
        required=False,
    )
    if existing is not None:
        return
    create_private_directory(path, mode)
    validate_private_path(path, expected_type="directory", expected_mode=mode)


def _write_private_json(path: Path, value: Dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            PRIVATE_FILE_MODE,
        )
    except OSError as exc:
        raise OffworkError(
            "UNSAFE_STATE_PATH",
            "Offwork project metadata could not be created safely",
            details={"path": str(path)},
        ) from exc
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_private_json(path: Path) -> Dict[str, Any]:
    validate_private_path(
        path,
        expected_type="file",
        expected_mode=PRIVATE_FILE_MODE,
    )
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise OffworkError(
            "UNSAFE_STATE_PATH",
            "Offwork project metadata could not be opened safely",
            details={"path": str(path)},
        ) from exc
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.getuid()
            or stat.S_IMODE(current.st_mode) != PRIVATE_FILE_MODE
        ):
            raise OffworkError(
                "UNSAFE_STATE_PATH",
                "Offwork project metadata changed during validation",
                details={"path": str(path)},
            )
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise json.JSONDecodeError("project metadata must be an object", "", 0)
    return value


def _ensure_lock_file(path: Path) -> None:
    existing = validate_private_path(
        path,
        expected_type="file",
        expected_mode=PRIVATE_FILE_MODE,
        required=False,
    )
    if existing is not None:
        return
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            PRIVATE_FILE_MODE,
        )
    except OSError as exc:
        raise OffworkError(
            "UNSAFE_STATE_PATH",
            "Offwork lock file could not be created safely",
            details={"path": str(path)},
        ) from exc
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)
    validate_private_path(
        path,
        expected_type="file",
        expected_mode=PRIVATE_FILE_MODE,
    )


def initialize_project(path: str) -> Dict[str, Any]:
    project = canonical_project(path)
    state_dir = project / STATE_DIR_NAME
    _ensure_directory(state_dir, 0o700)
    _ensure_directory(state_dir / "capsules", 0o700)

    project_file = state_dir / "project.json"
    existing_project_file = validate_private_path(
        project_file,
        expected_type="file",
        expected_mode=PRIVATE_FILE_MODE,
        required=False,
    )
    if existing_project_file is not None:
        try:
            metadata = _read_private_json(project_file)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OffworkError(
                "INVALID_PROJECT_STATE",
                "Existing project metadata is invalid",
                details={"path": str(project_file)},
            ) from exc
        if metadata.get("project_path") != str(project):
            raise OffworkError(
                "PROJECT_IDENTITY_MISMATCH",
                "Existing Offwork state belongs to a different project path",
                details={"project_path": str(project)},
            )
    else:
        metadata = {
            "schema_version": "offwork.project/v1",
            "project_id": f"project-{uuid.uuid4().hex}",
            "project_path": str(project),
        }
        _write_private_json(project_file, metadata)

    lock_file = state_dir / "state.lock"
    _ensure_lock_file(lock_file)

    initialize_database(state_dir / "state.sqlite3")
    return metadata


def load_project(path: str) -> Dict[str, Any]:
    project = canonical_project(path)
    state_dir = project / STATE_DIR_NAME
    project_file = state_dir / "project.json"
    state_exists = validate_private_path(
        state_dir,
        expected_type="directory",
        expected_mode=0o700,
        required=False,
    )
    project_exists = validate_private_path(
        project_file,
        expected_type="file",
        expected_mode=PRIVATE_FILE_MODE,
        required=False,
    )
    if state_exists is None or project_exists is None:
        raise OffworkError(
            "PROJECT_NOT_INITIALIZED",
            "Project has not been initialized by Offwork",
            details={"project_path": str(project)},
        )
    validate_private_path(
        state_dir / "state.lock",
        expected_type="file",
        expected_mode=PRIVATE_FILE_MODE,
    )
    validate_database_paths(state_dir / "state.sqlite3")
    validate_private_path(
        state_dir / "capsules",
        expected_type="directory",
        expected_mode=0o700,
    )
    try:
        metadata = _read_private_json(project_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OffworkError(
            "INVALID_PROJECT_STATE",
            "Project metadata is invalid",
            details={"path": str(project_file)},
        ) from exc
    if metadata.get("project_path") != str(project):
        raise OffworkError(
            "PROJECT_IDENTITY_MISMATCH",
            "Offwork state does not match the requested project path",
            details={"project_path": str(project)},
        )
    metadata["path"] = project
    metadata["state_dir"] = state_dir
    return metadata


def _git(project: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(project), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


def _metadata(project: Path, name: str, *arguments: str) -> str | None:
    result = _git(project, *arguments)
    if result.returncode != 0:
        return None
    value = os.fsdecode(result.stdout).strip()
    return value or None


def _project_relative(repo_path: str, prefix: str) -> str | None:
    normalized = repo_path.replace("\\", "/")
    if prefix:
        marker = prefix.rstrip("/") + "/"
        if not normalized.startswith(marker):
            return None
        normalized = normalized[len(marker) :]
    if not normalized or normalized == ".offwork" or normalized.startswith(".offwork/"):
        return None
    return normalized


def capture_workspace(project: Dict[str, Any]) -> Dict[str, Any]:
    project_path: Path = project["path"]
    root_text = _metadata(project_path, "root", "rev-parse", "--show-toplevel")
    if root_text is None:
        return {
            "schema_version": "offwork.workspace/v1",
            "reliable": False,
            "reason": "git_unavailable",
            "project_id": project["project_id"],
            "project_path": project["project_path"],
        }
    git_root = Path(root_text).resolve()
    try:
        relative_prefix = project_path.relative_to(git_root).as_posix()
    except ValueError:
        return {
            "schema_version": "offwork.workspace/v1",
            "reliable": False,
            "reason": "project_outside_git_root",
            "project_id": project["project_id"],
            "project_path": project["project_path"],
        }
    prefix = "" if relative_prefix == "." else relative_prefix
    pathspec = "." if not prefix else prefix

    staged = _git(git_root, "ls-files", "--stage", "-z", "--", pathspec)
    listed = _git(
        git_root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        pathspec,
    )
    if staged.returncode != 0 or listed.returncode != 0:
        return {
            "schema_version": "offwork.workspace/v1",
            "reliable": False,
            "reason": "git_scan_failed",
            "project_id": project["project_id"],
            "project_path": project["project_path"],
        }
    for record in staged.stdout.split(b"\0"):
        if record.startswith(b"160000 "):
            return {
                "schema_version": "offwork.workspace/v1",
                "reliable": False,
                "reason": "gitlink_unsupported",
                "project_id": project["project_id"],
                "project_path": project["project_path"],
            }

    entries: Dict[str, Dict[str, Any]] = {}
    for raw_path in listed.stdout.split(b"\0"):
        if not raw_path:
            continue
        repo_path = os.fsdecode(raw_path)
        relative = _project_relative(repo_path, prefix)
        if relative is None:
            continue
        absolute = project_path / relative
        try:
            path_stat = absolute.lstat()
        except FileNotFoundError:
            entries[relative] = {"type": "missing", "mode": None, "sha256": None}
            continue
        mode = stat.S_IMODE(path_stat.st_mode)
        if stat.S_ISLNK(path_stat.st_mode):
            target = os.fsencode(os.readlink(absolute))
            entries[relative] = {
                "type": "symlink",
                "mode": mode,
                "sha256": hashlib.sha256(target).hexdigest(),
            }
        elif stat.S_ISREG(path_stat.st_mode):
            try:
                content = absolute.read_bytes()
            except OSError:
                return {
                    "schema_version": "offwork.workspace/v1",
                    "reliable": False,
                    "reason": "required_path_unreadable",
                    "project_id": project["project_id"],
                    "project_path": project["project_path"],
                }
            entries[relative] = {
                "type": "file",
                "mode": mode,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        else:
            return {
                "schema_version": "offwork.workspace/v1",
                "reliable": False,
                "reason": "unsupported_path_type",
                "project_id": project["project_id"],
                "project_path": project["project_path"],
            }

    status_result = _git(
        git_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        pathspec,
    )
    changed_paths = []
    if status_result.returncode == 0:
        for record in status_result.stdout.split(b"\0"):
            if len(record) < 4:
                continue
            relative = _project_relative(os.fsdecode(record[3:]), prefix)
            if relative is not None:
                changed_paths.append(relative)

    branch = _metadata(project_path, "branch", "symbolic-ref", "--short", "-q", "HEAD")
    head = _metadata(project_path, "head", "rev-parse", "HEAD")
    return {
        "schema_version": "offwork.workspace/v1",
        "reliable": True,
        "project_id": project["project_id"],
        "project_path": project["project_path"],
        "git_root": str(git_root),
        "project_is_git_root": project_path == git_root,
        "branch": branch,
        "head": head,
        "entries": entries,
        "changed_paths": sorted(set(changed_paths)),
    }


def compare_workspace(captured: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    if not captured.get("reliable") or not current.get("reliable"):
        return {
            "status": "unavailable",
            "changes": [],
            "limitations": [captured.get("reason") or current.get("reason") or "snapshot_unavailable"],
        }
    if (
        captured.get("project_id") != current.get("project_id")
        or captured.get("project_path") != current.get("project_path")
    ):
        return {
            "status": "unavailable",
            "changes": [],
            "limitations": ["project_identity_mismatch"],
        }
    captured_entries = captured.get("entries", {})
    current_entries = current.get("entries", {})
    changes = sorted(
        path
        for path in set(captured_entries) | set(current_entries)
        if captured_entries.get(path) != current_entries.get(path)
    )
    if captured.get("project_is_git_root"):
        if captured.get("head") != current.get("head") or captured.get("branch") != current.get("branch"):
            changes.insert(0, "@git")
    return {
        "status": "changed" if changes else "fresh",
        "changes": changes,
        "limitations": ["ignored files and external state are not checked"],
    }
