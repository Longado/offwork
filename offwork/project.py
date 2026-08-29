from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Dict

from offwork.errors import OffworkError
from offwork.state import initialize_database


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


def _reject_symlink(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise OffworkError(
            "UNSAFE_STATE_PATH",
            "Offwork state paths must not be symlinks",
            details={"path": str(path)},
        )


def _ensure_directory(path: Path, mode: int) -> None:
    _reject_symlink(path)
    path.mkdir(mode=mode, exist_ok=True)
    if not path.is_dir():
        raise OffworkError(
            "UNSAFE_STATE_PATH",
            "Expected a private directory",
            details={"path": str(path)},
        )
    os.chmod(path, mode)


def _write_private_json(path: Path, value: Dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def initialize_project(path: str) -> Dict[str, Any]:
    project = canonical_project(path)
    state_dir = project / STATE_DIR_NAME
    _ensure_directory(state_dir, 0o700)
    _ensure_directory(state_dir / "capsules", 0o700)

    project_file = state_dir / "project.json"
    _reject_symlink(project_file)
    if project_file.exists():
        try:
            metadata = json.loads(project_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
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
    _reject_symlink(lock_file)
    descriptor = os.open(lock_file, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(descriptor)
    os.chmod(lock_file, 0o600)

    initialize_database(state_dir / "state.sqlite3")
    return metadata


def load_project(path: str) -> Dict[str, Any]:
    project = canonical_project(path)
    state_dir = project / STATE_DIR_NAME
    _reject_symlink(state_dir)
    project_file = state_dir / "project.json"
    _reject_symlink(project_file)
    if not state_dir.is_dir() or not project_file.is_file():
        raise OffworkError(
            "PROJECT_NOT_INITIALIZED",
            "Project has not been initialized by Offwork",
            details={"project_path": str(project)},
        )
    try:
        metadata = json.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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
