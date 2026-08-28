from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional


ALLOWED_DISPOSITIONS = {"resolve", "park", "drop", "delegate"}
DISPOSITION_LABELS = {
    "resolve": "现在收尾",
    "park": "封存到明天",
    "drop": "明确放弃",
    "delegate": "转交处理",
}


class CapsuleValidationError(ValueError):
    """Raised when a capsule cannot be safely archived for resumption."""


class CapsuleIntegrityError(CapsuleValidationError):
    """Raised when an archived capsule no longer matches its manifest."""


LEGACY_CAPSULE_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
CURRENT_CAPSULE_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$")
PENDING_LEGACY_CAPTURE = "pending-legacy-capture.json"


def _no_follow() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))


def _path_stat(path: Path) -> Optional[os.stat_result]:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _require_regular(path: Path) -> os.stat_result:
    metadata = _path_stat(path)
    if metadata is None:
        raise CapsuleValidationError("missing capsule file: %s" % path)
    if stat.S_ISLNK(metadata.st_mode):
        raise CapsuleValidationError("refusing symlink: %s" % path)
    if not stat.S_ISREG(metadata.st_mode):
        raise CapsuleValidationError("expected regular file: %s" % path)
    return metadata


def _require_directory(path: Path) -> os.stat_result:
    metadata = _path_stat(path)
    if metadata is None:
        raise FileNotFoundError("没有找到可恢复的 Work Capsule")
    if stat.S_ISLNK(metadata.st_mode):
        raise CapsuleValidationError("refusing symlink directory: %s" % path)
    if not stat.S_ISDIR(metadata.st_mode):
        raise CapsuleValidationError("expected directory: %s" % path)
    return metadata


def _read_regular_bytes(path: Path) -> bytes:
    _require_regular(path)
    descriptor = os.open(str(path), os.O_RDONLY | _no_follow())
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise CapsuleValidationError("expected regular file: %s" % path)
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


@contextmanager
def _exclusive_state_lock(path: Path) -> Iterator[None]:
    metadata = _path_stat(path)
    if metadata is not None and stat.S_ISLNK(metadata.st_mode):
        raise CapsuleValidationError("refusing symlink lock: %s" % path)
    try:
        descriptor = os.open(
            str(path),
            os.O_RDWR | os.O_CREAT | _no_follow(),
            0o600,
        )
    except OSError as error:
        raise CapsuleValidationError("cannot safely open state lock") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CapsuleValidationError("expected regular state lock")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _required_text(context: Dict[str, Any], field: str) -> str:
    value = context.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CapsuleValidationError("%s 不能为空" % field)
    return value.strip()


def _text_list(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CapsuleValidationError("%s 必须是字符串列表" % field)
    return [item.strip() for item in value if item.strip()]


def _settled_loops(value: Any) -> List[Dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CapsuleValidationError("open_loops 必须是列表")

    loops: List[Dict[str, str]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise CapsuleValidationError("open_loops 第 %d 项格式错误" % index)
        title = str(item.get("title", "")).strip()
        disposition = str(item.get("disposition", "")).strip().lower()
        note = str(item.get("note", "")).strip()
        if not title:
            raise CapsuleValidationError("open_loops 第 %d 项缺少标题" % index)
        if disposition not in ALLOWED_DISPOSITIONS:
            raise CapsuleValidationError("未安置事项：%s" % title)
        loops.append({"title": title, "disposition": disposition, "note": note})
    return loops


def capsule_content_hash(capsule: Dict[str, Any]) -> str:
    """Return the stable semantic hash used for DB/file binding."""

    unhashed = dict(capsule)
    unhashed.pop("content_hash", None)
    return hashlib.sha256(
        json.dumps(
            unhashed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def build_capsule(
    context: Dict[str, Any],
    project_state: Dict[str, Any],
    captured_at: Optional[datetime] = None,
    *,
    task_id: Optional[str] = None,
    managed_session_id: Optional[str] = None,
    parent_capsule_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(context, dict):
        raise CapsuleValidationError("context 必须是 JSON 对象")
    captured_at = captured_at or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    captured_at = captured_at.astimezone(timezone.utc)
    capsule_id = "%s-%s" % (
        captured_at.strftime("%Y%m%dT%H%M%SZ"),
        uuid.uuid4().hex,
    )

    capsule = {
        "schema_version": 1,
        "id": capsule_id,
        "captured_at": captured_at.isoformat(),
        "goal": _required_text(context, "goal"),
        "summary": str(context.get("summary", "")).strip(),
        "decisions": _text_list(context.get("decisions"), "decisions"),
        "failed_attempts": _text_list(
            context.get("failed_attempts"), "failed_attempts"
        ),
        "next_step": _required_text(context, "next_step"),
        "next_command": str(context.get("next_command", "")).strip(),
        "open_loops": _settled_loops(context.get("open_loops")),
        "project": dict(project_state),
    }
    if task_id is not None:
        capsule["task_id"] = task_id
    if managed_session_id is not None:
        capsule["managed_session_id"] = managed_session_id
    if parent_capsule_id is not None:
        capsule["parent_capsule_id"] = parent_capsule_id
    capsule["content_hash"] = capsule_content_hash(capsule)
    return capsule


def validate_for_restore(capsule: Dict[str, Any]) -> Dict[str, Any]:
    missing: List[str] = []
    for field in ("goal", "next_step"):
        if not str(capsule.get(field, "")).strip():
            missing.append(field)

    unsettled = [
        loop.get("title", "未命名事项")
        for loop in capsule.get("open_loops", [])
        if loop.get("disposition") not in ALLOWED_DISPOSITIONS
    ]
    if unsettled:
        missing.append("未安置事项：" + "、".join(unsettled))

    return {
        "mode": "local-contract",
        "ready_to_resume": not missing,
        "understood_goal": capsule.get("goal", ""),
        "next_action": capsule.get("next_step", ""),
        "missing_information": missing,
    }


def _bullets(items: Iterable[str], empty: str = "无") -> str:
    values = list(items)
    return "\n".join("- " + item for item in values) if values else "- " + empty


def render_capsule_markdown(capsule: Dict[str, Any]) -> str:
    project = capsule.get("project", {})
    loops = []
    for loop in capsule.get("open_loops", []):
        label = DISPOSITION_LABELS.get(loop.get("disposition"), loop.get("disposition"))
        suffix = " — " + loop["note"] if loop.get("note") else ""
        loops.append("[%s] %s%s" % (label, loop.get("title", ""), suffix))

    dirty_files = project.get("dirty_files", [])
    next_command = capsule.get("next_command") or "未设置"
    summary = capsule.get("summary") or "未填写"
    branch = project.get("branch") or "非 Git 项目"
    head = project.get("head") or "无"
    return """# Work Capsule · {id}

## 当前目标

{goal}

## 今日停留位置

{summary}

## 项目现场

- 路径：`{project_path}`
- 分支：`{branch}`
- HEAD：`{head}`
- 变更文件：{dirty_count} 个

{dirty_files}

## 已结算事项

{loops}

## 关键决策

{decisions}

## 失败尝试与踩坑

{failed_attempts}

## 明天第一步

{next_step}

建议命令：`{next_command}`
""".format(
        id=capsule["id"],
        goal=capsule["goal"],
        summary=summary,
        project_path=project.get("project_path", ""),
        branch=branch,
        head=head,
        dirty_count=len(dirty_files),
        dirty_files=_bullets(dirty_files),
        loops=_bullets(loops),
        decisions=_bullets(capsule.get("decisions", [])),
        failed_attempts=_bullets(capsule.get("failed_attempts", [])),
        next_step=capsule["next_step"],
        next_command=next_command,
    )


def render_resume(capsule: Dict[str, Any]) -> str:
    command_line = ""
    if capsule.get("next_command"):
        command_line = "\n建议命令：%s" % capsule["next_command"]
    return """WORKSPACE RESTORED

目标：{goal}
停留位置：{summary}
下一步：{next_step}{command_line}
""".format(
        goal=capsule["goal"],
        summary=capsule.get("summary") or "未填写",
        next_step=capsule["next_step"],
        command_line=command_line,
    )


def archive_capsule(
    project_root: Path,
    capsule: Dict[str, Any],
    restore_test: Dict[str, Any],
    *,
    update_latest: bool = True,
    assume_locked: bool = False,
    allow_rejected: bool = False,
) -> Path:
    if not restore_test.get("ready_to_resume") and not allow_rejected:
        missing = restore_test.get("missing_information", [])
        detail = "；".join(str(item) for item in missing if str(item).strip())
        message = "恢复校验未通过，不能休眠"
        if detail:
            message += "：" + detail
        raise CapsuleValidationError(message)

    root = Path(project_root).resolve() / ".offwork"
    _private_directory(root)
    if assume_locked:
        return _archive_capsule_locked(
            root, capsule, restore_test, update_latest=update_latest
        )
    with _exclusive_state_lock(root / "state.lock"):
        _private_directory(root)
        return _archive_capsule_locked(
            root, capsule, restore_test, update_latest=update_latest
        )


def _archive_capsule_locked(
    root: Path,
    capsule: Dict[str, Any],
    restore_test: Dict[str, Any],
    *,
    update_latest: bool = True,
) -> Path:
    capsule_id = str(capsule.get("id", ""))
    if not (LEGACY_CAPSULE_ID.fullmatch(capsule_id) or CURRENT_CAPSULE_ID.fullmatch(capsule_id)):
        raise CapsuleValidationError("invalid capsule_id")
    capsules_root = root / "capsules"
    _private_directory(capsules_root)
    archive_dir = capsules_root / capsule_id
    archive_metadata = _path_stat(archive_dir)
    if archive_metadata is not None:
        if stat.S_ISLNK(archive_metadata.st_mode):
            raise CapsuleValidationError("refusing symlink capsule directory")
        raise FileExistsError(str(archive_dir))

    staging_dir = capsules_root / (".%s.staging-%s" % (capsule_id, uuid.uuid4().hex))
    _private_directory(staging_dir)
    try:
        payloads = {
            "capsule.json": (
                json.dumps(capsule, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8"),
            "capsule.md": render_capsule_markdown(capsule).encode("utf-8"),
            "restore-test.json": (
                json.dumps(restore_test, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8"),
        }
        for filename, payload in payloads.items():
            _write_private_bytes(staging_dir / filename, payload)
        manifest = {
            "schema_version": 1,
            "capsule_id": capsule_id,
            "files": {
                filename: {"sha256": hashlib.sha256(payload).hexdigest()}
                for filename, payload in payloads.items()
            },
        }
        _write_private_bytes(
            staging_dir / "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
        _fsync_directory(staging_dir)
        os.replace(str(staging_dir), str(archive_dir))
        _fsync_directory(capsules_root)
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(str(staging_dir))
        raise

    if update_latest:
        _atomic_write_json(root / "latest.json", {"capsule_id": capsule_id})
    return archive_dir


@contextmanager
def capsule_transaction_lock(project_root: Path) -> Iterator[None]:
    """Serialize the filesystem and database publish steps of a capture."""

    root = Path(project_root).resolve() / ".offwork"
    _private_directory(root)
    with _exclusive_state_lock(root / "state.lock"):
        yield


def update_legacy_latest(project_root: Path, capsule_id: str) -> None:
    if not (
        LEGACY_CAPSULE_ID.fullmatch(capsule_id)
        or CURRENT_CAPSULE_ID.fullmatch(capsule_id)
    ):
        raise CapsuleValidationError("invalid capsule_id")
    root = Path(project_root).resolve() / ".offwork"
    _atomic_write_json(root / "latest.json", {"capsule_id": capsule_id})


def write_pending_legacy_capture(
    project_root: Path, payload: Dict[str, Any]
) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CapsuleValidationError("invalid pending legacy capture")
    root = Path(project_root).resolve() / ".offwork"
    _private_directory(root)
    _atomic_write_json(root / PENDING_LEGACY_CAPTURE, payload)


def load_pending_legacy_capture(project_root: Path) -> Optional[Dict[str, Any]]:
    path = Path(project_root).resolve() / ".offwork" / PENDING_LEGACY_CAPTURE
    if _path_stat(path) is None:
        return None
    try:
        payload = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CapsuleIntegrityError("invalid pending legacy capture") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CapsuleIntegrityError("invalid pending legacy capture")
    return payload


def clear_pending_legacy_capture(project_root: Path) -> None:
    path = Path(project_root).resolve() / ".offwork" / PENDING_LEGACY_CAPTURE
    metadata = _path_stat(path)
    if metadata is None:
        return
    _require_regular(path)
    path.unlink()
    _fsync_directory(path.parent)


def restore_legacy_latest(
    project_root: Path, capsule_id: Optional[str]
) -> None:
    root = Path(project_root).resolve() / ".offwork"
    if capsule_id:
        update_legacy_latest(project_root, capsule_id)
        return
    latest_path = root / "latest.json"
    if _path_stat(latest_path) is None:
        return
    _require_regular(latest_path)
    latest_path.unlink()
    _fsync_directory(root)


def _private_directory(path: Path) -> None:
    metadata = _path_stat(path)
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode):
            raise CapsuleValidationError("refusing symlink directory: %s" % path)
        if not stat.S_ISDIR(metadata.st_mode):
            raise CapsuleValidationError("expected directory: %s" % path)
    else:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            metadata = _path_stat(path)
            if metadata is None or stat.S_ISLNK(metadata.st_mode):
                raise CapsuleValidationError("refusing symlink directory: %s" % path)
            if not stat.S_ISDIR(metadata.st_mode):
                raise CapsuleValidationError("expected directory: %s" % path)
    path.chmod(0o700)


def _write_private_bytes(path: Path, payload: bytes) -> None:
    if _path_stat(path) is not None:
        raise CapsuleValidationError("refusing existing capsule file: %s" % path)
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow(),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CapsuleValidationError("expected regular capsule file: %s" % path)
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    previous = _read_regular_bytes(path) if _path_stat(path) is not None else None
    temporary = path.parent / (".%s.tmp-%s" % (path.name, uuid.uuid4().hex))
    try:
        _write_private_bytes(
            temporary,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
        os.replace(str(temporary), str(path))
        try:
            _fsync_directory(path.parent)
        except Exception:
            try:
                _restore_atomic_file(path, previous)
            except Exception:
                pass
            raise
    finally:
        if temporary.exists():
            temporary.unlink()


def _restore_atomic_file(path: Path, previous: Optional[bytes]) -> None:
    if previous is None:
        if path.exists():
            path.unlink()
        _fsync_directory(path.parent)
        return

    rollback = path.parent / (".%s.rollback-%s" % (path.name, uuid.uuid4().hex))
    try:
        _write_private_bytes(rollback, previous)
        os.replace(str(rollback), str(path))
        _fsync_directory(path.parent)
    finally:
        if rollback.exists():
            rollback.unlink()


def load_latest_capsule(project_root: Path) -> Dict[str, Any]:
    root = Path(project_root).resolve() / ".offwork"
    _require_directory(root)
    latest_path = root / "latest.json"
    if _path_stat(latest_path) is None:
        raise FileNotFoundError("没有找到可恢复的 Work Capsule")
    try:
        latest = json.loads(_read_regular_bytes(latest_path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CapsuleValidationError("invalid latest.json") from error
    if not isinstance(latest, dict):
        raise CapsuleIntegrityError("invalid latest.json")
    capsule_id = str(latest.get("capsule_id", ""))
    return load_capsule(project_root, capsule_id)


def load_capsule(project_root: Path, capsule_id: str) -> Dict[str, Any]:
    root = Path(project_root).resolve() / ".offwork"
    legacy = LEGACY_CAPSULE_ID.fullmatch(capsule_id) is not None
    current = CURRENT_CAPSULE_ID.fullmatch(capsule_id) is not None
    if not (legacy or current):
        raise CapsuleIntegrityError("invalid capsule_id")

    capsules_root = root / "capsules"
    _require_directory(capsules_root)
    archive_dir = capsules_root / capsule_id
    archive_metadata = _path_stat(archive_dir)
    if archive_metadata is None:
        raise FileNotFoundError("没有找到可恢复的 Work Capsule")
    if stat.S_ISLNK(archive_metadata.st_mode):
        raise CapsuleIntegrityError("refusing symlink capsule directory")
    if not stat.S_ISDIR(archive_metadata.st_mode):
        raise CapsuleIntegrityError("invalid capsule directory")

    capsule_path = archive_dir / "capsule.json"
    capsule_bytes = _read_regular_bytes(capsule_path)
    try:
        capsule = json.loads(capsule_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CapsuleIntegrityError("invalid capsule.json") from error
    if not isinstance(capsule, dict):
        raise CapsuleIntegrityError("invalid capsule.json")
    if str(capsule.get("id", "")) != capsule_id:
        raise CapsuleIntegrityError("capsule ID mismatch")
    if legacy:
        return capsule

    manifest_path = archive_dir / "manifest.json"
    try:
        manifest = json.loads(_read_regular_bytes(manifest_path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CapsuleIntegrityError("invalid manifest.json") from error
    if not isinstance(manifest, dict):
        raise CapsuleIntegrityError("invalid manifest.json")
    if manifest.get("schema_version") != 1 or capsule.get("schema_version") != 1:
        raise CapsuleIntegrityError("manifest/capsule schema mismatch")
    if str(manifest.get("capsule_id", "")) != capsule_id:
        raise CapsuleIntegrityError("manifest capsule ID mismatch")

    payloads = {
        "capsule.json": capsule_bytes,
        "capsule.md": _read_regular_bytes(archive_dir / "capsule.md"),
        "restore-test.json": _read_regular_bytes(archive_dir / "restore-test.json"),
    }
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, dict):
        raise CapsuleIntegrityError("invalid manifest files")
    for filename, payload in payloads.items():
        entry = manifest_files.get(filename)
        expected = entry.get("sha256") if isinstance(entry, dict) else None
        actual = hashlib.sha256(payload).hexdigest()
        if expected != actual:
            raise CapsuleIntegrityError("capsule payload hash mismatch: %s" % filename)
    stored_hash = capsule.get("content_hash")
    if stored_hash is not None:
        unhashed = dict(capsule)
        unhashed.pop("content_hash", None)
        actual_hash = capsule_content_hash(unhashed)
        if stored_hash != actual_hash:
            raise CapsuleIntegrityError("capsule content hash mismatch")
    return capsule


def load_latest_task_capsule(project_root: Path, task_id: str) -> Dict[str, Any]:
    # Import lazily so the V0.1 load path never opens SQLite.
    from .storage import initialize_project_storage

    storage = initialize_project_storage(Path(project_root))
    with storage.connect() as connection:
        row = connection.execute(
            "SELECT capsule_id, content_hash FROM capsules WHERE task_id = ? "
            "AND status IN ('validated', 'fresh_verified') "
            "AND archived_at IS NULL ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    if row is None:
        raise FileNotFoundError("没有找到可恢复的 Work Capsule")
    try:
        capsule = load_capsule(storage.project_root, str(row["capsule_id"]))
    except FileNotFoundError as error:
        raise CapsuleIntegrityError(
            "registered capsule archive is missing"
        ) from error
    if capsule_content_hash(capsule) != str(row["content_hash"]):
        raise CapsuleIntegrityError("capsule/database content hash mismatch")
    return capsule
