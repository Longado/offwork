from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .state import OffworkError
from .storage import initialize_project_storage


SOURCE_KINDS = {"codex", "claude"}
FORMAT_VERSION = 2
HASH_CHUNK_BYTES = 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _history_error(code: str, message: str, *, details: Mapping[str, Any]) -> OffworkError:
    return OffworkError(
        code,
        message,
        exit_code=2,
        details=details,
        recovery="Use one of the supported history sources: codex or claude.",
    )


def _require_source_kind(source_kind: str) -> str:
    normalized = str(source_kind).strip().lower()
    if normalized not in SOURCE_KINDS:
        raise _history_error(
            "INVALID_SOURCE_KIND",
            "Unsupported history source: %s" % source_kind,
            details={"source_kind": source_kind},
        )
    return normalized


def _canonical(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_in_project(candidate: str, project_root: Path) -> bool:
    if not isinstance(candidate, str) or not candidate.strip():
        return False
    try:
        path = _canonical(Path(candidate))
    except (OSError, RuntimeError, ValueError):
        return False
    return path == project_root or project_root in path.parents


def _source_fingerprint(stat_result: os.stat_result, prefix_hash: str) -> str:
    return "prefix-sha256:%d:%d:%s" % (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        prefix_hash,
    )


def _fingerprint_identity(value: Any) -> Optional[Tuple[int, int]]:
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if (
        len(parts) != 4
        or parts[0] != "prefix-sha256"
        or len(parts[3]) != 64
    ):
        return None
    try:
        int(parts[3], 16)
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _stat_key(stat_result: os.stat_result) -> Tuple[int, int, int, int]:
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def _source_snapshot(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    return (
        int(row["revision"]),
        str(row["source_fingerprint"]),
        str(row["source_path"]),
        str(row["state"]),
        int(row["format_version"]),
        int(row["mtime_ns"]),
        int(row["size_bytes"]),
        int(row["read_offset"]),
        row["checkpoint_hash"],
        row["source_session_id"],
    )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return "%s_%s" % (prefix, digest)


def _complete_end(data: bytes, start_offset: int) -> int:
    newline = data.rfind(b"\n", start_offset)
    if newline < start_offset:
        return start_offset
    return newline + 1


def _line_records(
    data: bytes,
    start: int,
    end: int,
    errors: List[Dict[str, Any]],
    *,
    base_offset: int = 0,
) -> Iterable[Tuple[int, Mapping[str, Any]]]:
    offset = start
    while offset < end:
        newline = data.find(b"\n", offset, end)
        if newline < 0:
            break
        raw = data[offset:newline]
        line_offset = base_offset + offset
        offset = newline + 1
        if not raw.strip():
            continue
        try:
            decoded = raw.decode("utf-8")
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append(
                {"code": "MALFORMED_JSON", "source_offset": line_offset}
            )
            continue
        if not isinstance(value, Mapping):
            errors.append(
                {"code": "INVALID_RECORD", "source_offset": line_offset}
            )
            continue
        yield line_offset, value


def _message_timestamp(record: Mapping[str, Any]) -> str:
    timestamp = record.get("timestamp")
    if isinstance(timestamp, str) and timestamp.strip():
        return timestamp
    return _now()


def _join_text_blocks(
    content: Any, allowed_type: str, text_field: str = "text"
) -> Optional[str]:
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return None
    text: List[str] = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != allowed_type:
            continue
        value = block.get(text_field)
        if isinstance(value, str) and value:
            text.append(value)
    if not text:
        return None
    return "\n".join(text)


def _codex_turn_id(
    record: Mapping[str, Any], payload: Mapping[str, Any]
) -> Optional[str]:
    candidates: List[Any] = [
        record.get("internal_chat_message_metadata_passthrough"),
        payload.get("internal_chat_message_metadata_passthrough"),
    ]
    for container in (record.get("metadata"), payload.get("metadata")):
        if isinstance(container, Mapping):
            candidates.append(
                container.get("internal_chat_message_metadata_passthrough")
            )
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        turn_id = candidate.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            return turn_id
    return None


CODEX_SYNTHETIC_USER_PREFIXES = (
    "<environment_context>",
    "<recommended_plugins>",
    "# AGENTS.md instructions",
    "<app-context>",
    "<permissions instructions>",
    "<skills_instructions>",
    "<collaboration_mode>",
    "The following is the Codex agent history whose request actioned a new Codex session:",
    "The following is the Codex agent history added since your last message:",
    "# Files mentioned by the user:",
)


def _is_codex_synthetic_fallback(content: str) -> bool:
    stripped = content.lstrip()
    return any(
        stripped.startswith(prefix) for prefix in CODEX_SYNTHETIC_USER_PREFIXES
    )


@dataclass(frozen=True)
class ParsedMessage:
    source_message_id: str
    role: str
    content: str
    source_offset: int
    created_at: str


@dataclass(frozen=True)
class ParsedChunk:
    valid: bool
    pending: bool
    source_session_id: Optional[str]
    messages: List[ParsedMessage]
    errors: List[Dict[str, Any]]
    reason: Optional[str] = None
    append_safe: bool = True


def _parse_codex(
    records: Iterable[Tuple[int, Mapping[str, Any]]],
    project_root: Path,
    expected_session_id: Optional[str],
    *,
    full_scan: bool,
    message_progress: Optional[Callable[[int], None]] = None,
) -> ParsedChunk:
    session_ids = set()
    cwd_values = set()
    child = False
    incomplete_metadata = False
    messages: List[ParsedMessage] = []
    grouped_events: Dict[str, ParsedMessage] = {}
    grouped_responses: Dict[str, ParsedMessage] = {}
    record_count = 0
    visible_count = 0

    for offset, record in records:
        record_count += 1
        if record.get("type") == "session_meta":
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                incomplete_metadata = True
                continue
            session_id = payload.get("id")
            cwd = payload.get("cwd")
            if isinstance(session_id, str) and session_id:
                session_ids.add(session_id)
                parent = payload.get("parent_thread_id")
                if isinstance(parent, str) and parent and parent != session_id:
                    child = True
            if isinstance(cwd, str) and cwd:
                cwd_values.add(cwd)
            else:
                incomplete_metadata = True
            if not isinstance(session_id, str) or not session_id:
                incomplete_metadata = True
            continue

        if record.get("type") == "event_msg":
            payload = record.get("payload")
            if not isinstance(payload, Mapping) or payload.get("type") != "user_message":
                continue
            content = payload.get("message")
            if not isinstance(content, str) or not content:
                continue
            native_id = payload.get("id")
            if not isinstance(native_id, str) or not native_id:
                native_id = "line:%d" % offset
            parsed_message = ParsedMessage(
                source_message_id=native_id,
                role="user",
                content=content,
                source_offset=offset,
                created_at=_message_timestamp(record),
            )
            turn_id = _codex_turn_id(record, payload)
            if turn_id is not None:
                grouped_events[turn_id] = parsed_message
            else:
                messages.append(parsed_message)
                visible_count += 1
                if message_progress is not None and visible_count % 1000 == 0:
                    message_progress(visible_count)
            continue

        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping) or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role == "user":
            turn_id = _codex_turn_id(record, payload)
            if turn_id is None:
                continue
            content = _join_text_blocks(payload.get("content"), "input_text")
            if not content or _is_codex_synthetic_fallback(content):
                continue
            native_id = payload.get("id")
            if not isinstance(native_id, str) or not native_id:
                native_id = "line:%d" % offset
            grouped_responses[turn_id] = ParsedMessage(
                source_message_id=native_id,
                role="user",
                content=content,
                source_offset=offset,
                created_at=_message_timestamp(record),
            )
            continue
        if role != "assistant":
            continue
        content = _join_text_blocks(payload.get("content"), "output_text")
        if not content:
            continue
        native_id = payload.get("id")
        if not isinstance(native_id, str) or not native_id:
            native_id = "line:%d" % offset
        messages.append(
            ParsedMessage(
                source_message_id=native_id,
                role=str(role),
                content=content,
                source_offset=offset,
                created_at=_message_timestamp(record),
            )
        )
        visible_count += 1
        if message_progress is not None and visible_count % 1000 == 0:
            message_progress(visible_count)

    grouped_visible = list(grouped_events.values()) + [
        message
        for turn_id, message in grouped_responses.items()
        if turn_id not in grouped_events
    ]
    for message in sorted(grouped_visible, key=lambda item: item.source_offset):
        messages.append(message)
        visible_count += 1
        if message_progress is not None and visible_count % 1000 == 0:
            message_progress(visible_count)
    messages.sort(key=lambda item: item.source_offset)

    if child:
        return ParsedChunk(False, False, None, [], [], "CHILD_SESSION")
    if incomplete_metadata:
        return ParsedChunk(False, False, None, [], [], "MISSING_SESSION_METADATA")
    if len(session_ids) > 1 or len(cwd_values) > 1:
        return ParsedChunk(False, False, None, [], [], "AMBIGUOUS_SESSION")
    session_id = next(iter(session_ids), expected_session_id)
    if expected_session_id and session_ids and session_id != expected_session_id:
        return ParsedChunk(False, False, None, [], [], "SESSION_CHANGED")
    if full_scan:
        if record_count == 0:
            return ParsedChunk(False, True, None, [], [])
        if not session_id or len(cwd_values) != 1:
            return ParsedChunk(False, False, None, [], [], "MISSING_SESSION_METADATA")
        cwd = next(iter(cwd_values))
        if not _is_in_project(cwd, project_root):
            return ParsedChunk(False, False, None, [], [], "PROJECT_PATH_MISMATCH")
    elif cwd_values and not _is_in_project(next(iter(cwd_values)), project_root):
        return ParsedChunk(False, False, None, [], [], "PROJECT_PATH_MISMATCH")
    return ParsedChunk(
        True,
        False,
        session_id,
        messages,
        [],
        append_safe=not grouped_events and not grouped_responses,
    )


def _parse_claude(
    records: Iterable[Tuple[int, Mapping[str, Any]]],
    project_root: Path,
    expected_session_id: Optional[str],
    *,
    full_scan: bool,
    message_progress: Optional[Callable[[int], None]] = None,
) -> ParsedChunk:
    session_ids = set()
    cwd_values = set()
    messages: List[ParsedMessage] = []
    candidate_records = 0
    incomplete_metadata = False
    record_count = 0
    visible_count = 0

    for offset, record in records:
        record_count += 1
        if record.get("isMeta") is True:
            continue
        role = record.get("type")
        if role not in ("user", "assistant"):
            continue
        candidate_records += 1
        session_id = record.get("sessionId")
        cwd = record.get("cwd")
        if isinstance(session_id, str) and session_id:
            session_ids.add(session_id)
        else:
            incomplete_metadata = True
        if isinstance(cwd, str) and cwd:
            cwd_values.add(cwd)
        else:
            incomplete_metadata = True

        if record.get("isSidechain") is True or "toolUseResult" in record:
            continue
        message = record.get("message")
        if not isinstance(message, Mapping) or message.get("role") != role:
            continue
        content_value = message.get("content")
        if isinstance(content_value, str):
            content = content_value or None
        else:
            content = _join_text_blocks(content_value, "text")
        if not content:
            continue
        native_id = record.get("uuid")
        if not isinstance(native_id, str) or not native_id:
            message_id = message.get("id")
            native_id = (
                message_id
                if isinstance(message_id, str) and message_id
                else "line:%d" % offset
            )
        messages.append(
            ParsedMessage(
                source_message_id=native_id,
                role=str(role),
                content=content,
                source_offset=offset,
                created_at=_message_timestamp(record),
            )
        )
        visible_count += 1
        if message_progress is not None and visible_count % 1000 == 0:
            message_progress(visible_count)

    if incomplete_metadata:
        return ParsedChunk(False, False, None, [], [], "MISSING_SESSION_METADATA")
    if len(session_ids) > 1 or len(cwd_values) > 1:
        return ParsedChunk(False, False, None, [], [], "AMBIGUOUS_SESSION")
    session_id = next(iter(session_ids), expected_session_id)
    if expected_session_id and session_ids and session_id != expected_session_id:
        return ParsedChunk(False, False, None, [], [], "SESSION_CHANGED")
    if full_scan:
        if record_count == 0:
            return ParsedChunk(False, True, None, [], [])
        if candidate_records == 0 or not session_id or len(cwd_values) != 1:
            return ParsedChunk(False, False, None, [], [], "MISSING_SESSION_METADATA")
        cwd = next(iter(cwd_values))
        if not _is_in_project(cwd, project_root):
            return ParsedChunk(False, False, None, [], [], "PROJECT_PATH_MISMATCH")
    elif cwd_values and not _is_in_project(next(iter(cwd_values)), project_root):
        return ParsedChunk(False, False, None, [], [], "PROJECT_PATH_MISMATCH")
    return ParsedChunk(True, False, session_id, messages, [])


class HistoryIndexService:
    def __init__(self, project_root: Path) -> None:
        self.storage = initialize_project_storage(Path(project_root))
        self.project_root = self.storage.project_root.resolve()

    def _setting_payload(self, row: sqlite3.Row) -> Dict[str, Any]:
        try:
            settings = json.loads(str(row["settings_json"]))
        except (TypeError, json.JSONDecodeError):
            settings = {}
        return {
            "source_kind": str(row["source_kind"]),
            "enabled": bool(row["enabled"]),
            "settings": settings if isinstance(settings, Mapping) else {},
            "revision": int(row["revision"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _set_source(self, source_kind: str, enabled: bool) -> Dict[str, Any]:
        kind = _require_source_kind(source_kind)
        timestamp = _now()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO source_settings(source_kind,enabled,settings_json,revision,created_at,updated_at) "
                "VALUES (?,?,?,1,?,?) "
                "ON CONFLICT(source_kind) DO UPDATE SET "
                "enabled=excluded.enabled,revision=source_settings.revision+1,updated_at=excluded.updated_at",
                (kind, int(enabled), "{}", timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM source_settings WHERE source_kind=?", (kind,)
            ).fetchone()
            connection.commit()
        return self._setting_payload(row)

    def enable_source(self, source_kind: str) -> Dict[str, Any]:
        return self._set_source(source_kind, True)

    def disable_source(self, source_kind: str) -> Dict[str, Any]:
        return self._set_source(source_kind, False)

    def source_settings(self) -> List[Dict[str, Any]]:
        with self.storage.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM source_settings ORDER BY source_kind"
            ).fetchall()
        return [self._setting_payload(row) for row in rows]

    def list_source_settings(self) -> List[Dict[str, Any]]:
        return self.source_settings()

    def _source_record(self, source_path: Path, source_kind: str) -> Optional[sqlite3.Row]:
        path = str(_canonical(Path(source_path)))
        kind = _require_source_kind(source_kind)
        with self.storage.connect() as connection:
            return connection.execute(
                "SELECT * FROM session_sources WHERE source_kind=? AND source_path=?",
                (kind, path),
            ).fetchone()

    def _enabled_kinds(self) -> List[str]:
        with self.storage.connect() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT source_kind FROM source_settings WHERE enabled=1 ORDER BY source_kind"
                )
                if str(row[0]) in SOURCE_KINDS
            ]

    def _default_roots(self, source_kind: str) -> List[Path]:
        if source_kind == "codex":
            return [Path.home() / ".codex" / "sessions"]
        return [Path.home() / ".claude" / "projects"]

    def _roots_for_kind(
        self,
        source_kind: str,
        source_roots: Optional[Mapping[str, Sequence[Path]]],
    ) -> Sequence[Path]:
        if source_roots is None:
            return self._default_roots(source_kind)
        values = source_roots.get(source_kind, [])
        if isinstance(values, (str, Path)):
            return [Path(values)]
        return [Path(value) for value in values]

    def _discover_kind(
        self,
        source_kind: str,
        source_roots: Optional[Mapping[str, Sequence[Path]]],
    ) -> Tuple[List[Path], Optional[Dict[str, Any]]]:
        discovered = set()
        try:
            for root in self._roots_for_kind(source_kind, source_roots):
                expanded = root.expanduser()
                if expanded.is_file():
                    if expanded.suffix == ".jsonl":
                        discovered.add(expanded.resolve())
                    continue
                if not expanded.exists():
                    continue
                if not expanded.is_dir():
                    continue
                for path in expanded.rglob("*.jsonl"):
                    if path.is_file():
                        discovered.add(path.resolve())
        except (OSError, RuntimeError) as error:
            return [], {
                "code": "SOURCE_DISCOVERY_FAILED",
                "source_kind": source_kind,
                "error_type": type(error).__name__,
            }
        return sorted(discovered, key=str), None

    def _read_stable(
        self, path: Path, start_offset: int = 0
    ) -> Tuple[bytes, os.stat_result]:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if start_offset < 0 or start_offset > int(before.st_size):
                raise RuntimeError("source offset is outside the file")
            handle.seek(start_offset)
            data = handle.read()
            after = os.fstat(handle.fileno())
        if (
            _stat_key(before) != _stat_key(after)
            or len(data) != int(after.st_size) - start_offset
        ):
            raise RuntimeError("source changed during read")
        return data, after

    def _hash_prefix(
        self, path: Path, end_offset: int
    ) -> Tuple[str, os.stat_result]:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if end_offset < 0 or end_offset > int(before.st_size):
                raise RuntimeError("source offset is outside the file")
            remaining = end_offset
            while remaining:
                chunk = handle.read(min(HASH_CHUNK_BYTES, remaining))
                if not chunk:
                    raise RuntimeError("source ended before committed offset")
                digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(handle.fileno())
        if _stat_key(before) != _stat_key(after):
            raise RuntimeError("source changed during hash")
        return digest.hexdigest(), after

    def _parse(
        self,
        source_kind: str,
        data: bytes,
        start: int,
        end: int,
        expected_session_id: Optional[str],
        *,
        full_scan: bool,
        base_offset: int = 0,
        message_progress: Optional[Callable[[int], None]] = None,
    ) -> ParsedChunk:
        errors: List[Dict[str, Any]] = []
        records = _line_records(
            data, start, end, errors, base_offset=base_offset
        )
        if source_kind == "codex":
            parsed = _parse_codex(
                records,
                self.project_root,
                expected_session_id,
                full_scan=full_scan,
                message_progress=message_progress,
            )
        else:
            parsed = _parse_claude(
                records,
                self.project_root,
                expected_session_id,
                full_scan=full_scan,
                message_progress=message_progress,
            )
        return ParsedChunk(
            parsed.valid,
            parsed.pending,
            parsed.source_session_id,
            parsed.messages,
            errors + parsed.errors,
            parsed.reason,
            parsed.append_safe,
        )

    def _attachment(
        self,
        connection: sqlite3.Connection,
        source_kind: str,
        source_session_id: Optional[str],
    ) -> Tuple[Optional[str], Optional[str]]:
        if not source_session_id:
            return None, None
        row = connection.execute(
            "SELECT managed_session_id,task_id,cwd FROM sessions "
            "WHERE provider=? AND native_session_id=?",
            (source_kind, source_session_id),
        ).fetchone()
        if row is None:
            return None, None
        try:
            cwd = _canonical(Path(str(row["cwd"])))
        except (OSError, RuntimeError, ValueError):
            return None, None
        if cwd != self.project_root:
            return None, None
        return str(row["managed_session_id"]), str(row["task_id"])

    def _refresh_attachment(
        self, source_kind: str, source_id: str
    ) -> None:
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT managed_session_id,task_id,source_session_id "
                "FROM session_sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return
            source_session_id = (
                str(row["source_session_id"])
                if row["source_session_id"] is not None
                else None
            )
            managed_session_id, task_id = self._attachment(
                connection, source_kind, source_session_id
            )
            if (
                row["managed_session_id"] == managed_session_id
                and row["task_id"] == task_id
            ):
                connection.rollback()
                return
            connection.execute(
                "UPDATE session_sources SET managed_session_id=?,task_id=?,"
                "revision=revision+1,updated_at=? WHERE source_id=?",
                (managed_session_id, task_id, _now(), source_id),
            )
            connection.commit()

    def _matches_file_snapshot(
        self,
        path: Path,
        read_offset: int,
        checkpoint_hash: str,
        stat_result: os.stat_result,
    ) -> bool:
        try:
            current_hash, current_stat = self._hash_prefix(path, read_offset)
        except (OSError, RuntimeError):
            return False
        return (
            current_hash == checkpoint_hash
            and _stat_key(current_stat) == _stat_key(stat_result)
        )

    def _store_source(
        self,
        source_kind: str,
        path: Path,
        stat_result: os.stat_result,
        checkpoint_hash: str,
        fingerprint: str,
        existing: Optional[sqlite3.Row],
        parsed: ParsedChunk,
        read_offset: int,
        *,
        rebuild: bool,
    ) -> Tuple[int, Optional[str], bool]:
        timestamp = _now()
        path_text = str(path)
        state = "indexed" if parsed.valid else ("pending" if parsed.pending else "skipped")
        with self.storage.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                write_existing: Optional[sqlite3.Row] = None
                took_over_missing = False
                if existing is not None:
                    current = connection.execute(
                        "SELECT * FROM session_sources WHERE source_id=?",
                        (str(existing["source_id"]),),
                    ).fetchone()
                    if (
                        current is None
                        or _source_snapshot(current) != _source_snapshot(existing)
                    ):
                        connection.rollback()
                        return 0, "SOURCE_INDEX_CONFLICT", False
                    write_existing = current
                    source_id = str(current["source_id"])
                else:
                    current_path = connection.execute(
                        "SELECT * FROM session_sources "
                        "WHERE source_kind=? AND source_path=?",
                        (source_kind, path_text),
                    ).fetchone()
                    if current_path is not None:
                        connection.rollback()
                        return 0, "SOURCE_INDEX_CONFLICT", False
                    moved = None
                    if parsed.source_session_id is not None:
                        moved = connection.execute(
                            "SELECT * FROM session_sources "
                            "WHERE source_kind=? AND source_session_id=?",
                            (source_kind, parsed.source_session_id),
                        ).fetchone()
                    if moved is not None:
                        if str(moved["state"]) != "missing":
                            connection.rollback()
                            return 0, "SOURCE_SESSION_CONFLICT", False
                        write_existing = moved
                        source_id = str(moved["source_id"])
                        took_over_missing = True
                    else:
                        source_id = _stable_id("src", source_kind, path_text)
                        if connection.execute(
                            "SELECT 1 FROM session_sources WHERE source_id=?",
                            (source_id,),
                        ).fetchone() is not None:
                            connection.rollback()
                            return 0, "SOURCE_INDEX_CONFLICT", False

                if not self._matches_file_snapshot(
                    path, read_offset, checkpoint_hash, stat_result
                ):
                    connection.rollback()
                    return 0, "SOURCE_CHANGED_DURING_INDEX", False

                if write_existing is not None and (
                    rebuild or took_over_missing or not parsed.valid
                ):
                    connection.execute("DELETE FROM messages WHERE source_id=?", (source_id,))
                managed_session_id, task_id = self._attachment(
                    connection, source_kind, parsed.source_session_id
                )
                if write_existing is None:
                    connection.execute(
                        "INSERT INTO session_sources("
                        "source_id,managed_session_id,task_id,source_session_id,source_kind,source_path,"
                        "format_version,source_fingerprint,mtime_ns,size_bytes,read_offset,checkpoint_hash,"
                        "state,revision,archived_at,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,?)",
                        (
                            source_id,
                            managed_session_id,
                            task_id,
                            parsed.source_session_id,
                            source_kind,
                            path_text,
                            FORMAT_VERSION,
                            fingerprint,
                            int(stat_result.st_mtime_ns),
                            int(stat_result.st_size),
                            read_offset,
                            checkpoint_hash,
                            state,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE session_sources SET managed_session_id=?,task_id=?,source_session_id=?,"
                        "source_kind=?,source_path=?,"
                        "format_version=?,source_fingerprint=?,mtime_ns=?,size_bytes=?,read_offset=?,"
                        "checkpoint_hash=?,state=?,revision=revision+1,archived_at=NULL,updated_at=? "
                        "WHERE source_id=?",
                        (
                            managed_session_id,
                            task_id,
                            parsed.source_session_id,
                            source_kind,
                            path_text,
                            FORMAT_VERSION,
                            fingerprint,
                            int(stat_result.st_mtime_ns),
                            int(stat_result.st_size),
                            read_offset,
                            checkpoint_hash,
                            state,
                            timestamp,
                            source_id,
                        ),
                    )
                inserted = 0
                if parsed.valid:
                    for message in parsed.messages:
                        message_id = _stable_id(
                            "msg", source_id, message.source_message_id
                        )
                        cursor = connection.execute(
                            "INSERT OR IGNORE INTO messages("
                            "message_id,source_id,source_message_id,role,content,source_fingerprint,"
                            "source_offset,revision,archived_at,created_at) "
                            "VALUES (?,?,?,?,?,?,?,1,NULL,?)",
                            (
                                message_id,
                                source_id,
                                message.source_message_id,
                                message.role,
                                message.content,
                                fingerprint,
                                message.source_offset,
                                message.created_at,
                            ),
                        )
                        inserted += max(0, int(cursor.rowcount))
                if not self._matches_file_snapshot(
                    path, read_offset, checkpoint_hash, stat_result
                ):
                    connection.rollback()
                    return 0, "SOURCE_CHANGED_DURING_INDEX", False
                connection.commit()
                return inserted, None, took_over_missing
            except sqlite3.IntegrityError:
                connection.rollback()
                return 0, "SOURCE_SESSION_CONFLICT", False

    def _process_source(
        self,
        source_kind: str,
        path: Path,
        message_progress: Optional[Callable[[int], None]] = None,
    ) -> Dict[str, Any]:
        existing = self._source_record(path, source_kind)
        try:
            preliminary_stat = path.stat()
        except OSError as error:
            return {
                "status": "skipped",
                "messages": 0,
                "rebuilt": False,
                "errors": [
                    {
                        "code": "SOURCE_READ_FAILED",
                        "source_kind": source_kind,
                        "source_path": str(path),
                        "error_type": type(error).__name__,
                    }
                ],
            }
        preliminary_identity = (
            int(preliminary_stat.st_dev), int(preliminary_stat.st_ino)
        )
        if (
            existing is not None
            and str(existing["state"]) != "missing"
            and int(existing["format_version"]) == FORMAT_VERSION
            and int(existing["mtime_ns"]) == int(preliminary_stat.st_mtime_ns)
            and int(existing["size_bytes"]) == int(preliminary_stat.st_size)
            and _fingerprint_identity(existing["source_fingerprint"])
            == preliminary_identity
        ):
            self._refresh_attachment(
                source_kind, str(existing["source_id"])
            )
            return {
                "status": "skipped",
                "messages": 0,
                "rebuilt": False,
                "errors": [],
            }

        rebuild = existing is not None
        start_offset = 0
        expected_session_id: Optional[str] = None
        if existing is not None and str(existing["state"]) != "missing":
            old_offset = int(existing["read_offset"])
            old_size = int(existing["size_bytes"])
            expected_checkpoint = existing["checkpoint_hash"]
            can_append = False
            if (
                int(existing["format_version"]) == FORMAT_VERSION
                and int(preliminary_stat.st_size) > old_size
                and old_offset <= int(preliminary_stat.st_size)
                and _fingerprint_identity(existing["source_fingerprint"])
                == preliminary_identity
                and isinstance(expected_checkpoint, str)
            ):
                try:
                    current_checkpoint, checkpoint_stat = self._hash_prefix(
                        path, old_offset
                    )
                    can_append = (
                        _stat_key(checkpoint_stat) == _stat_key(preliminary_stat)
                        and current_checkpoint == expected_checkpoint
                    )
                except (OSError, RuntimeError):
                    can_append = False
            if can_append:
                rebuild = False
                start_offset = old_offset
                if existing["source_session_id"] is not None:
                    expected_session_id = str(existing["source_session_id"])

        try:
            data, stat_result = self._read_stable(path, start_offset)
        except (OSError, RuntimeError) as error:
            return {
                "status": "skipped",
                "messages": 0,
                "rebuilt": False,
                "errors": [
                    {
                        "code": "SOURCE_READ_FAILED",
                        "source_kind": source_kind,
                        "source_path": str(path),
                        "error_type": type(error).__name__,
                    }
                ],
            }
        if _stat_key(stat_result) != _stat_key(preliminary_stat):
            return {
                "status": "skipped",
                "messages": 0,
                "rebuilt": False,
                "errors": [
                    {
                        "code": "SOURCE_CHANGED_DURING_INDEX",
                        "source_kind": source_kind,
                        "source_path": str(path),
                    }
                ],
            }

        local_end = _complete_end(data, 0)
        read_offset = start_offset + local_end
        parsed = self._parse(
            source_kind,
            data,
            0,
            local_end,
            expected_session_id,
            full_scan=start_offset == 0,
            base_offset=start_offset,
            message_progress=message_progress,
        )
        if start_offset > 0 and (
            (not parsed.valid and not parsed.pending) or not parsed.append_safe
        ):
            rebuild = True
            start_offset = 0
            try:
                data, stat_result = self._read_stable(path, 0)
            except (OSError, RuntimeError) as error:
                return {
                    "status": "skipped",
                    "messages": 0,
                    "rebuilt": False,
                    "errors": [
                        {
                            "code": "SOURCE_READ_FAILED",
                            "source_kind": source_kind,
                            "source_path": str(path),
                            "error_type": type(error).__name__,
                        }
                    ],
                }
            if _stat_key(stat_result) != _stat_key(preliminary_stat):
                return {
                    "status": "skipped",
                    "messages": 0,
                    "rebuilt": False,
                    "errors": [
                        {
                            "code": "SOURCE_CHANGED_DURING_INDEX",
                            "source_kind": source_kind,
                            "source_path": str(path),
                        }
                    ],
                }
            local_end = _complete_end(data, 0)
            read_offset = local_end
            parsed = self._parse(
                source_kind,
                data,
                0,
                local_end,
                None,
                full_scan=True,
                message_progress=message_progress,
            )

        try:
            checkpoint_hash, checkpoint_stat = self._hash_prefix(
                path, read_offset
            )
        except (OSError, RuntimeError) as error:
            return {
                "status": "skipped",
                "messages": 0,
                "rebuilt": False,
                "errors": [
                    {
                        "code": "SOURCE_READ_FAILED",
                        "source_kind": source_kind,
                        "source_path": str(path),
                        "error_type": type(error).__name__,
                    }
                ],
            }
        if _stat_key(checkpoint_stat) != _stat_key(stat_result):
            return {
                "status": "skipped",
                "messages": 0,
                "rebuilt": False,
                "errors": [
                    {
                        "code": "SOURCE_CHANGED_DURING_INDEX",
                        "source_kind": source_kind,
                        "source_path": str(path),
                    }
                ],
            }
        fingerprint = _source_fingerprint(stat_result, checkpoint_hash)

        inserted, conflict, took_over_missing = self._store_source(
            source_kind,
            path,
            stat_result,
            checkpoint_hash,
            fingerprint,
            existing,
            parsed,
            read_offset,
            rebuild=rebuild,
        )
        errors = [
            {
                **error,
                "source_kind": source_kind,
                "source_path": str(path),
            }
            for error in parsed.errors
        ]
        if parsed.reason:
            errors.append(
                {
                    "code": parsed.reason,
                    "source_kind": source_kind,
                    "source_path": str(path),
                }
            )
        if conflict:
            errors.append(
                {
                    "code": conflict,
                    "source_kind": source_kind,
                    "source_path": str(path),
                }
            )
            return {
                "status": "skipped",
                "messages": 0,
                "rebuilt": False,
                "errors": errors,
            }
        return {
            "status": "indexed" if parsed.valid else "skipped",
            "messages": inserted,
            "rebuilt": bool(rebuild or took_over_missing),
            "errors": errors,
        }

    def _mark_missing(self, source_kind: str, present: Iterable[Path]) -> int:
        present_paths = {str(path) for path in present}
        timestamp = _now()
        count = 0
        with self.storage.connect() as connection:
            rows = connection.execute(
                "SELECT source_id,source_path,state,revision FROM session_sources WHERE source_kind=?",
                (source_kind,),
            ).fetchall()
        for row in rows:
            if str(row["source_path"]) in present_paths or str(row["state"]) == "missing":
                continue
            with self.storage.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT source_path,state,revision FROM session_sources WHERE source_id=?",
                    (str(row["source_id"]),),
                ).fetchone()
                if (
                    current is None
                    or int(current["revision"]) != int(row["revision"])
                    or str(current["source_path"]) != str(row["source_path"])
                    or str(current["state"]) != str(row["state"])
                ):
                    connection.rollback()
                    continue
                connection.execute(
                    "DELETE FROM messages WHERE source_id=?", (str(row["source_id"]),)
                )
                connection.execute(
                    "UPDATE session_sources SET state='missing',archived_at=?,revision=revision+1,updated_at=? "
                    "WHERE source_id=?",
                    (timestamp, timestamp, str(row["source_id"])),
                )
                connection.commit()
            count += 1
        return count

    def index(
        self,
        source_roots: Optional[Mapping[str, Sequence[Path]]] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "discovered": 0,
            "indexed": 0,
            "skipped": 0,
            "rebuilt": 0,
            "messages": 0,
            "missing": 0,
            "errors": [],
        }
        discovered_by_kind: Dict[str, List[Path]] = {}
        complete_kinds = set()
        for source_kind in self._enabled_kinds():
            paths, discovery_error = self._discover_kind(source_kind, source_roots)
            if discovery_error is not None:
                result["errors"].append(discovery_error)
                continue
            discovered_by_kind[source_kind] = paths
            complete_kinds.add(source_kind)
            result["discovered"] += len(paths)

        processed = 0
        for source_kind in sorted(discovered_by_kind):
            for path in discovered_by_kind[source_kind]:
                progress_failed = False

                def report_message_progress(messages_seen: int) -> None:
                    nonlocal progress_failed
                    if progress_callback is None or progress_failed:
                        return
                    try:
                        progress_callback(
                            {
                                "phase": "parsing",
                                "processed": processed,
                                "discovered": result["discovered"],
                                "source_kind": source_kind,
                                "source_path": str(path),
                                "messages_seen": messages_seen,
                            }
                        )
                    except Exception:
                        progress_failed = True

                outcome = self._process_source(
                    source_kind, path, report_message_progress
                )
                result[outcome["status"]] += 1
                result["messages"] += int(outcome["messages"])
                if outcome["rebuilt"]:
                    result["rebuilt"] += 1
                result["errors"].extend(outcome["errors"])
                if progress_failed:
                    result["errors"].append(
                        {
                            "code": "PROGRESS_CALLBACK_FAILED",
                            "source_kind": source_kind,
                        }
                    )
                    progress_callback = None
                processed += 1
                if progress_callback is not None:
                    try:
                        progress_callback(
                            {
                                "processed": processed,
                                "discovered": result["discovered"],
                                "source_kind": source_kind,
                                "source_path": str(path),
                                "messages": result["messages"],
                            }
                        )
                    except Exception:
                        result["errors"].append(
                            {
                                "code": "PROGRESS_CALLBACK_FAILED",
                                "source_kind": source_kind,
                            }
                        )
                        progress_callback = None

        for source_kind in sorted(complete_kinds):
            result["missing"] += self._mark_missing(
                source_kind, discovered_by_kind[source_kind]
            )
        return result
