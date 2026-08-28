from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__


SCHEMA_VERSION = "offwork.cli/v1"


def run_claude_verifier(capsule: Dict[str, Any]) -> Dict[str, Any]:
    """Lazy, patchable verifier hook used by capture."""

    from .verifier import run_claude_verifier as implementation

    return implementation(capsule)


def update_legacy_latest(project_root: Path, capsule_id: str) -> None:
    """Lazy, patchable legacy pointer hook used by capture."""

    from .capsule import update_legacy_latest as implementation

    implementation(project_root, capsule_id)


def _terminal_text(value: Any, *, multiline: bool = False) -> str:
    """Render untrusted fields without allowing terminal control sequences."""
    rendered: List[str] = []
    for character in str(value):
        codepoint = ord(character)
        if character == "\n":
            rendered.append("\n" if multiline else "\\n")
        elif character == "\r":
            rendered.append("\\r")
        elif character == "\t":
            rendered.append("\t" if multiline else "\\t")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            rendered.append("\\x%02x" % codepoint)
        else:
            rendered.append(character)
    return "".join(rendered)


def _capture_project_path(path: Path) -> Path:
    from .state import OffworkError

    supplied = Path(path).expanduser()
    if supplied.is_symlink() or not supplied.exists() or not supplied.is_dir():
        raise OffworkError(
            "PROJECT_PATH_MISMATCH",
            "--project must be an existing real directory.",
            exit_code=4,
            details={"project_path": str(supplied)},
            recovery="Pass the canonical path of an existing project directory.",
        )
    try:
        canonical = supplied.resolve(strict=True)
    except OSError as error:
        raise OffworkError(
            "PROJECT_PATH_MISMATCH",
            "--project could not be resolved safely.",
            exit_code=4,
            details={"project_path": str(supplied)},
            recovery="Pass the canonical path of an existing project directory.",
        ) from error
    lexical = Path(os.path.abspath(str(supplied)))
    if lexical != canonical:
        raise OffworkError(
            "PROJECT_PATH_MISMATCH",
            "--project must not cross a symlink boundary.",
            exit_code=4,
            details={
                "project_path": str(lexical),
                "canonical_path": str(canonical),
            },
            recovery="Pass the resolved canonical project path explicitly.",
        )
    return canonical


class CLIParseError(ValueError):
    pass


class OffworkArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIParseError(message)


def _project_and_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")


def _revision(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--revision", type=int)


def _parser() -> argparse.ArgumentParser:
    parser = OffworkArgumentParser(
        prog="offwork", description="Create and restore local Work Capsules."
    )
    parser.add_argument(
        "--version", action="version", version="offwork %s" % __version__
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="Create a verified capsule")
    capture.add_argument("--context", required=True, type=Path)
    capture.add_argument("--task")
    _revision(capture)
    capture.add_argument("--project", type=Path, default=Path.cwd())
    capture.add_argument(
        "--verifier",
        choices=("local", "claude"),
        default="local",
        help="Use a schema check or a fresh Claude session to test restoration",
    )
    capture.add_argument("--json", action="store_true")

    resume = subparsers.add_parser("resume", help="Restore the latest capsule")
    resume.add_argument("--task")
    resume.add_argument("--capsule")
    resume.add_argument("--recall", choices=("auto", "none"), default="auto")
    _project_and_json(resume)

    task = subparsers.add_parser("task", help="Manage project tasks")
    task_commands = task.add_subparsers(dest="task_command", required=True)

    add = task_commands.add_parser("add", help="Add a task")
    add.add_argument("title")
    add.add_argument("--goal", required=True)
    add.add_argument("--auto-complete", action="store_true")
    add.add_argument("--require-fresh-verifier", action="store_true")
    add.add_argument("--accept-cmd", action="append", default=[])
    add.add_argument("--open-loop", action="append", default=[])
    _project_and_json(add)

    list_parser = task_commands.add_parser("list", help="List tasks")
    filters = list_parser.add_mutually_exclusive_group()
    filters.add_argument("--actionable", action="store_true")
    filters.add_argument("--blocked", action="store_true")
    filters.add_argument("--waiting", action="store_true")
    filters.add_argument("--archived", action="store_true")
    _project_and_json(list_parser)

    show = task_commands.add_parser("show", help="Show a task")
    show.add_argument("task_id")
    _project_and_json(show)

    start = task_commands.add_parser("start", help="Start a task")
    start.add_argument("task_id")
    _revision(start)
    _project_and_json(start)

    complete = task_commands.add_parser("complete", help="Complete a task")
    complete.add_argument("task_id")
    complete.add_argument("--confirm", action="store_true")
    _revision(complete)
    _project_and_json(complete)

    archive = task_commands.add_parser("archive", help="Archive a task")
    archive.add_argument("task_id")
    _revision(archive)
    _project_and_json(archive)

    unarchive = task_commands.add_parser("unarchive", help="Unarchive a task")
    unarchive.add_argument("task_id")
    _revision(unarchive)
    _project_and_json(unarchive)

    dependency = task_commands.add_parser("dependency", help="Manage dependencies")
    dependency_commands = dependency.add_subparsers(
        dest="dependency_command", required=True
    )
    for name in ("add", "remove"):
        dependency_parser = dependency_commands.add_parser(name)
        dependency_parser.add_argument("task_id")
        dependency_parser.add_argument("dependency_id")
        _revision(dependency_parser)
        _project_and_json(dependency_parser)

    session = subparsers.add_parser("session", help="Manage agent sessions")
    session_commands = session.add_subparsers(dest="session_command", required=True)

    attach = session_commands.add_parser("attach", help="Attach an agent session")
    attach.add_argument("--task", required=True)
    attach.add_argument("--tool", required=True, choices=("codex", "claude"))
    attach.add_argument("--native-id")
    attach.add_argument("--tmux")
    attach.add_argument("--parent-session")
    _project_and_json(attach)

    session_list = session_commands.add_parser("list", help="List task sessions")
    session_list.add_argument("--task", required=True)
    _project_and_json(session_list)

    primary = session_commands.add_parser("primary", help="Set the primary session")
    primary.add_argument("managed_session_id")
    _revision(primary)
    _project_and_json(primary)

    enter = session_commands.add_parser("enter", help="Enter an existing tmux session")
    enter.add_argument("managed_session_id")
    _project_and_json(enter)

    reopen = session_commands.add_parser("reopen", help="Explicitly reopen a native session")
    reopen.add_argument("managed_session_id")
    _project_and_json(reopen)

    source = subparsers.add_parser("source", help="Configure history sources")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    for name in ("enable", "disable"):
        source_parser = source_commands.add_parser(
            name, help="%s a project history source" % name.capitalize()
        )
        source_parser.add_argument("source_kind", choices=("codex", "claude"))
        _project_and_json(source_parser)

    index = subparsers.add_parser("index", help="Index enabled history sources")
    _project_and_json(index)

    search = subparsers.add_parser("search", help="Search indexed history")
    search.add_argument("query")
    search.add_argument("--task")
    search.add_argument("--source", choices=("codex", "claude"))
    _project_and_json(search)

    memory = subparsers.add_parser("memory", help="Manage explicit memories")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)

    memory_add = memory_commands.add_parser("add", help="Save an explicit memory")
    memory_add.add_argument("text")
    memory_add.add_argument("--task")
    memory_add.add_argument("--session")
    memory_add.add_argument("--capsule")
    _project_and_json(memory_add)

    memory_list = memory_commands.add_parser("list", help="List saved memories")
    memory_list.add_argument("--task")
    memory_list.add_argument("--forgotten", action="store_true")
    _project_and_json(memory_list)

    memory_forget = memory_commands.add_parser("forget", help="Forget a memory")
    memory_forget.add_argument("memory_id")
    _revision(memory_forget)
    _project_and_json(memory_forget)

    status = subparsers.add_parser("status", help="Show project task status")
    status.add_argument("--all", action="store_true")
    _project_and_json(status)
    return parser


def _read_context(path: Path) -> Dict[str, Any]:
    from .capsule import CapsuleValidationError

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CapsuleValidationError("context 必须是 JSON 对象")
    return data


def _parse_open_loop(value: str) -> Dict[str, str]:
    from .state import OffworkError, normalize_open_loop

    try:
        decoded = json.loads(value)
        return normalize_open_loop(decoded)
    except (json.JSONDecodeError, ValueError) as error:
        raise OffworkError(
            "INVALID_INPUT",
            "Invalid --open-loop: %s" % error,
            exit_code=2,
            details={"value": value},
            recovery="Pass a JSON object with title, disposition, and note strings.",
        ) from error


def _command_name(args: argparse.Namespace) -> str:
    if args.command == "source":
        return "source.%s" % args.source_command
    if args.command == "memory":
        return "memory.%s" % args.memory_command
    if args.command == "session":
        return "session.%s" % args.session_command
    if args.command != "task":
        return str(args.command)
    if args.task_command != "dependency":
        return "task.%s" % args.task_command
    return "task.dependency.%s" % args.dependency_command


def _command_from_argv(argv: Sequence[str]) -> str:
    if not argv:
        return ""
    if argv[0] != "task":
        nested_commands = {
            "source": {"enable", "disable"},
            "memory": {"add", "list", "forget"},
            "session": {"attach", "list", "primary", "enter", "reopen"},
        }
        known = nested_commands.get(argv[0])
        if known is not None and len(argv) > 1 and argv[1] in known:
            return "%s.%s" % (argv[0], argv[1])
        return argv[0]
    if len(argv) > 2 and argv[1] == "dependency":
        return "task.dependency.%s" % argv[2]
    if len(argv) > 1:
        return "task.%s" % argv[1]
    return "task"


def _envelope(
    command: str,
    *,
    ok: bool,
    data: Any,
    warnings: Optional[Sequence[Dict[str, Any]]] = None,
    error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "ok": ok,
        "data": data,
        "meta": {},
        "warnings": list(warnings or []),
        "error": error,
    }
    return payload


def _print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _human_task(task: Dict[str, Any]) -> str:
    line = "%s  %s  [%s]" % (
        task["task_id"],
        task["title"],
        task["computed_state"],
    )
    if task["blockers"]:
        line += " blockers: " + ", ".join(task["blockers"])
    return line


def _human_task_reference(task: Optional[Dict[str, Any]]) -> str:
    if task is None:
        return "None"
    return "%s  %s  [%s]" % (
        task["task_id"],
        task["title"],
        task["computed_state"],
    )


def _human_task_detail(task: Dict[str, Any]) -> str:
    dependencies = ", ".join(task["dependencies"]) or "None"
    blockers = ", ".join(task["blockers"]) or "None"
    lines = [
        "Title: %s" % task["title"],
        "ID: %s" % task["task_id"],
        "Status: %s" % task["status"],
        "Computed: %s" % task["computed_state"],
        "Goal: %s" % task["goal"],
        "Revision: %s" % task["revision"],
        "Archived at: %s" % (task["archived_at"] or "None"),
        "Dependencies: %s" % dependencies,
        "Blockers: %s" % blockers,
        "Acceptance:",
    ]
    commands = task["acceptance_commands"]
    lines.extend("- %s" % command for command in commands)
    if not commands:
        lines.append("- None")
    lines.append("Open loops:")
    loops = task["open_loops"]
    for loop in loops:
        suffix = " — %s" % loop["note"] if loop["note"] else ""
        lines.append(
            "- [%s] %s%s" % (loop["disposition"], loop["title"], suffix)
        )
    if not loops:
        lines.append("- None")
    return "\n".join(lines)


def _capture(args: argparse.Namespace) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    import subprocess

    from .capsule import (
        CapsuleIntegrityError,
        CapsuleValidationError,
        archive_capsule,
        build_capsule,
        capsule_content_hash,
        capsule_transaction_lock,
        clear_pending_legacy_capture,
        restore_legacy_latest,
        validate_for_restore,
        write_pending_legacy_capture,
    )
    from .project import capture_project_state
    from .state import OffworkError, StateService
    from .verifier import (
        VerifierUnavailableError,
        merge_restore_tests,
        validate_first_step_feasibility,
    )

    project = _capture_project_path(args.project)
    context = _read_context(args.context)
    service = StateService(project)
    legacy_mode = args.task is None
    task_id = args.task
    if task_id is None:
        task_id = service.ensure_default_task(str(context.get("goal", "")))["task_id"]
    warnings: List[Dict[str, Any]] = []
    with capsule_transaction_lock(project):
        task = service.prepare_capture(
            task_id, expected_revision=args.revision
        )
        project_state = capture_project_state(project)
        captured_path = Path(str(project_state.get("project_path", ""))).resolve()
        if captured_path != project:
            raise OffworkError(
                "PROJECT_PATH_MISMATCH",
                "Captured project state does not match --project.",
                exit_code=4,
                details={
                    "project_path": str(project),
                    "captured_path": str(captured_path),
                },
                recovery="Capture with the exact canonical project path.",
            )
        capsule = build_capsule(
            context,
            project_state,
            task_id=task_id,
            managed_session_id=task["primary_session_id"],
            parent_capsule_id=task["parent_capsule_id"],
        )
        restore_test = validate_for_restore(capsule)
        fresh_verified = False
        if args.verifier == "claude":
            preflight_test = validate_first_step_feasibility(capsule, project)
            if preflight_test["ready_to_resume"]:
                try:
                    agent_test = run_claude_verifier(capsule)
                except VerifierUnavailableError as error:
                    raise OffworkError(
                        "VERIFIER_UNAVAILABLE",
                        str(error),
                        exit_code=5,
                        details={"verifier": "claude"},
                        recovery="Install/configure Claude CLI or use --verifier local.",
                    ) from error
                except (FileNotFoundError, PermissionError) as error:
                    raise OffworkError(
                        "VERIFIER_UNAVAILABLE",
                        str(error),
                        exit_code=5,
                        details={"verifier": "claude"},
                        recovery="Install/configure Claude CLI or use --verifier local.",
                    ) from error
                except (CapsuleValidationError, OSError, subprocess.SubprocessError) as error:
                    raise OffworkError(
                        "VERIFICATION_FAILED",
                        str(error),
                        exit_code=4,
                        details={"verifier": "claude"},
                        recovery="Inspect the verifier result and capture again.",
                    ) from error
                restore_test = merge_restore_tests(restore_test, agent_test)
            else:
                restore_test = merge_restore_tests(restore_test, preflight_test)
            fresh_verified = bool(restore_test.get("ready_to_resume"))
            if not fresh_verified:
                capsule["status"] = "rejected"
                capsule["content_hash"] = capsule_content_hash(capsule)
                archive_dir = archive_capsule(
                    project,
                    capsule,
                    restore_test,
                    update_latest=False,
                    assume_locked=True,
                    allow_rejected=True,
                )
                service.register_rejected_capture(
                    task_id=task_id,
                    expected_revision=int(task["revision"]),
                    capsule_id=str(capsule["id"]),
                    managed_session_id=task["primary_session_id"],
                    parent_capsule_id=task["parent_capsule_id"],
                    content_hash=str(capsule["content_hash"]),
                    archive_path=archive_dir,
                )
                raise OffworkError(
                    "VERIFICATION_FAILED",
                    "Fresh-agent restore verification did not pass.",
                    exit_code=4,
                    details={
                        "verifier": "claude",
                        "missing_information": restore_test.get(
                            "missing_information", []
                        ),
                    },
                    recovery="Add the missing recovery context and capture again.",
                )
        if legacy_mode:
            write_pending_legacy_capture(
                project,
                {
                    "schema_version": 1,
                    "capsule_id": str(capsule["id"]),
                    "task_id": task_id,
                    "previous_latest_id": task["parent_capsule_id"],
                    "content_hash": str(capsule["content_hash"]),
                    "archive_path": str(
                        project / ".offwork" / "capsules" / str(capsule["id"])
                    ),
                    "parent_capsule_id": task["parent_capsule_id"],
                    "managed_session_id": task["primary_session_id"],
                    "before_task_status": task["status"],
                    "before_task_revision": task["revision"],
                    "before_task_updated_at": task["updated_at"],
                    "before_session_state": task["primary_session_state"],
                    "before_session_revision": task["primary_session_revision"],
                    "before_session_updated_at": task[
                        "primary_session_updated_at"
                    ],
                },
            )
        try:
            archive_dir = archive_capsule(
                project,
                capsule,
                restore_test,
                update_latest=False,
                assume_locked=True,
            )
        except Exception:
            if legacy_mode:
                clear_pending_legacy_capture(project)
            raise
        reviewed_task = service.publish_capture(
            task_id=task_id,
            expected_revision=int(task["revision"]),
            capsule_id=str(capsule["id"]),
            managed_session_id=task["primary_session_id"],
            parent_capsule_id=task["parent_capsule_id"],
            status="fresh_verified" if fresh_verified else "validated",
            content_hash=str(capsule["content_hash"]),
            archive_path=archive_dir,
        )
        if legacy_mode:
            try:
                update_legacy_latest(project, str(capsule["id"]))
            except Exception:
                service.rollback_published_capture(
                    reviewed_task["_capture_compensation"],
                    before_session_state=task["primary_session_state"],
                    before_session_revision=task["primary_session_revision"],
                    before_session_updated_at=task[
                        "primary_session_updated_at"
                    ],
                )
                restore_legacy_latest(project, task["parent_capsule_id"])
                clear_pending_legacy_capture(project)
                raise
            clear_pending_legacy_capture(project)
    auto_complete = service.evaluate_auto_complete(
        task,
        expected_revision=int(reviewed_task["revision"]),
        fresh_verified=fresh_verified,
        require_fresh_verifier=bool(task.get("require_fresh_verifier")),
        capsule_id=str(capsule["id"]),
        capsule_open_loops=capsule.get("open_loops", []),
    )
    if auto_complete["enabled"] and not auto_complete["passed"]:
        warnings.append(
            {
                "code": "ACCEPTANCE_FAILED",
                "message": (
                    "Capsule is safe to resume, but configured local acceptance "
                    "did not complete the Task."
                ),
                "details": {"reason": auto_complete.get("reason")},
                "recovery": "Resolve the local gate, then capture again.",
            }
        )
    warnings.extend(service.warnings)
    return (
        {
            "status": "WORKSPACE HIBERNATED",
            "capsule_id": capsule["id"],
            "archive_dir": str(archive_dir),
            "task_id": task_id,
            "managed_session_id": task["primary_session_id"],
            "capsule_status": "fresh_verified" if fresh_verified else "validated",
            "restore_test": restore_test,
            "auto_complete": auto_complete,
        },
        warnings,
    )


def _resume(args: argparse.Namespace) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    from .capsule import (
        CapsuleIntegrityError,
        CapsuleValidationError,
        capsule_content_hash,
        load_capsule,
        load_latest_capsule,
        load_latest_task_capsule,
    )
    from .memory import bounded_recall, render_recall
    from .sessions import SessionService
    from .state import OffworkError, StateService

    project = args.project.resolve()
    effective_task_id = args.task
    current_primary_id: Optional[str] = None
    service = StateService(project)
    row = None
    try:
        if args.capsule is not None:
            with service.storage.connect() as connection:
                row = connection.execute(
                    "SELECT task_id, status, content_hash FROM capsules "
                    "WHERE capsule_id = ?",
                    (args.capsule,),
                ).fetchone()
            try:
                capsule = load_capsule(project, args.capsule)
            except FileNotFoundError as error:
                if row is not None:
                    raise CapsuleIntegrityError(
                        "registered capsule archive is missing"
                    ) from error
                raise
        elif effective_task_id is not None:
            service.show_task(effective_task_id)
            capsule = load_latest_task_capsule(project, effective_task_id)
        else:
            capsule = load_latest_capsule(project)
        if row is None:
            with service.storage.connect() as connection:
                row = connection.execute(
                    "SELECT task_id, status, content_hash FROM capsules "
                    "WHERE capsule_id = ?",
                    (str(capsule["id"]),),
                ).fetchone()
        selected_capsule_id = str(capsule["id"])
        if capsule.get("status") == "rejected" or (
            row is not None
            and str(row["status"]) not in {"validated", "fresh_verified"}
        ):
            raise OffworkError(
                "VERIFICATION_FAILED",
                "Rejected capsule cannot be resumed.",
                exit_code=4,
                details={"capsule_id": selected_capsule_id},
                recovery="Choose a validated capsule.",
            )
        if row is not None and capsule_content_hash(capsule) != str(
            row["content_hash"]
        ):
            raise CapsuleIntegrityError("capsule/database content hash mismatch")
        if args.capsule is not None:
            bound_task_id = (
                str(row["task_id"])
                if row is not None and row["task_id"] is not None
                else None
            )
            if effective_task_id is not None and bound_task_id != effective_task_id:
                raise OffworkError(
                    "CAPSULE_TASK_MISMATCH",
                    "Capsule is not bound to the requested Task.",
                    exit_code=4,
                    details={
                        "capsule_id": args.capsule,
                        "task_id": effective_task_id,
                        "capsule_task_id": bound_task_id,
                    },
                    recovery="Choose a capsule belonging to this Task.",
                )
            effective_task_id = effective_task_id or bound_task_id
    except CapsuleValidationError as error:
        raise OffworkError(
            "CAPSULE_INTEGRITY_FAILED",
            str(error),
            exit_code=4,
            details={"capsule_id": args.capsule},
            recovery="Restore an untampered capsule or capture a new one.",
        ) from error
    recall: Optional[Dict[str, Any]] = None
    recall_text = ""
    if args.recall == "auto" and effective_task_id is not None:
        sessions = SessionService(project).list(effective_task_id)
        primary = next((item for item in sessions if item["is_primary"]), None)
        current_primary_id = (
            str(primary["managed_session_id"]) if primary is not None else None
        )
        recall = bounded_recall(
            project,
            effective_task_id,
            current_managed_session_id=current_primary_id,
        )
        recall_text = render_recall(recall)
    return (
        {
            "capsule_id": capsule["id"],
            "task_id": effective_task_id,
            "goal": capsule["goal"],
            "summary": capsule.get("summary", ""),
            "next_step": capsule["next_step"],
            "next_command": capsule.get("next_command", ""),
            "recall": recall,
            "recall_text": recall_text,
        },
        [],
    )


def _task(args: argparse.Namespace) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    from .state import StateService

    open_loops = []
    if args.task_command == "add":
        open_loops = [_parse_open_loop(value) for value in args.open_loop]
    service = StateService(args.project)
    if args.task_command == "add":
        data = service.add_task(
            args.title,
            args.goal,
            auto_complete=args.auto_complete,
            require_fresh_verifier=args.require_fresh_verifier,
            acceptance_commands=args.accept_cmd,
            open_loops=open_loops,
        )
    elif args.task_command == "list":
        selected = next(
            (
                name
                for name in ("actionable", "blocked", "waiting", "archived")
                if getattr(args, name)
            ),
            None,
        )
        data = {"tasks": service.list_tasks(selected)}
    elif args.task_command == "show":
        data = service.show_task(args.task_id)
    elif args.task_command == "start":
        data = service.start_task(args.task_id, expected_revision=args.revision)
    elif args.task_command == "complete":
        data = service.complete_task(
            args.task_id,
            confirmed=args.confirm,
            expected_revision=args.revision,
        )
    elif args.task_command == "archive":
        data = service.archive_task(args.task_id, expected_revision=args.revision)
    elif args.task_command == "unarchive":
        data = service.unarchive_task(args.task_id, expected_revision=args.revision)
    elif args.dependency_command == "add":
        data = service.add_dependency(
            args.task_id,
            args.dependency_id,
            expected_revision=args.revision,
        )
    else:
        data = service.remove_dependency(
            args.task_id,
            args.dependency_id,
            expected_revision=args.revision,
        )
    return data, service.warnings


def _session(args: argparse.Namespace) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    from .sessions import SessionService

    service = SessionService(args.project)
    if args.session_command == "attach":
        data = service.attach(
            args.task,
            args.tool,
            native_id=args.native_id,
            tmux=args.tmux,
            parent_session_id=args.parent_session,
        )
    elif args.session_command == "list":
        data = {"sessions": service.list(args.task)}
    elif args.session_command == "primary":
        data = service.set_primary(
            args.managed_session_id, expected_revision=args.revision
        )
    elif args.session_command == "enter":
        data = service.enter(args.managed_session_id)
    else:
        data = service.reopen(args.managed_session_id)
    return data, []


def _source(args: argparse.Namespace) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    from .history import HistoryIndexService

    service = HistoryIndexService(args.project)
    if args.source_command == "enable":
        data = service.enable_source(args.source_kind)
    else:
        data = service.disable_source(args.source_kind)
    return data, []


def _index(args: argparse.Namespace) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    from .history import HistoryIndexService

    completed_messages = 0
    next_progress = 1000

    def report_progress(event: Dict[str, Any]) -> None:
        nonlocal completed_messages, next_progress
        total = completed_messages
        if event.get("phase") == "parsing":
            messages_seen = event.get("messages_seen")
            if isinstance(messages_seen, int) and not isinstance(messages_seen, bool):
                total += max(0, messages_seen)
        else:
            cumulative = event.get("messages")
            if isinstance(cumulative, int) and not isinstance(cumulative, bool):
                completed_messages = max(completed_messages, cumulative)
                total = completed_messages
        while total >= next_progress:
            print(
                "Index progress: %d visible messages (%s)"
                % (
                    next_progress,
                    _terminal_text(event.get("source_kind", "unknown")),
                ),
                file=sys.stderr,
            )
            next_progress += 1000

    data = HistoryIndexService(args.project).index(
        progress_callback=report_progress
    )
    return data, []


def _search(args: argparse.Namespace) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    from .memory import SearchService

    data = SearchService(args.project).search(
        args.query,
        task_id=args.task,
        source=args.source,
    )
    return data, []


def _memory(args: argparse.Namespace) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    from .memory import MemoryService

    service = MemoryService(args.project)
    if args.memory_command == "add":
        data = service.add(
            args.text,
            task_id=args.task,
            managed_session_id=args.session,
            capsule_id=args.capsule,
            provenance_kind="user_explicit",
        )
    elif args.memory_command == "list":
        data = {
            "memories": service.list(
                task_id=args.task,
                include_forgotten=args.forgotten,
            )
        }
    else:
        data = service.forget(
            args.memory_id,
            expected_revision=args.revision,
        )
    return data, []


def _status(args: argparse.Namespace) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    from .state import StateService, registry_status

    if args.all:
        return registry_status(), []
    return StateService(args.project).project_status(), []


def _execute(args: argparse.Namespace) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if args.command == "capture":
        return _capture(args)
    if args.command == "resume":
        return _resume(args)
    if args.command == "task":
        return _task(args)
    if args.command == "session":
        return _session(args)
    if args.command == "source":
        return _source(args)
    if args.command == "index":
        return _index(args)
    if args.command == "search":
        return _search(args)
    if args.command == "memory":
        return _memory(args)
    return _status(args)


def _print_human(args: argparse.Namespace, data: Dict[str, Any]) -> None:
    if args.command == "capture":
        print("RESTORE TEST PASSED")
        print("0 ORPHANED TASKS")
        print("WORKSPACE HIBERNATED")
        print("Capsule: %s" % _terminal_text(data["archive_dir"]))
    elif args.command == "resume":
        command_line = (
            "\n建议命令：%s" % _terminal_text(data["next_command"])
            if data.get("next_command")
            else ""
        )
        print(
            "WORKSPACE RESTORED\n\n目标：%s\n停留位置：%s\n下一步：%s%s"
            % (
                _terminal_text(data["goal"]),
                _terminal_text(data.get("summary") or "未填写"),
                _terminal_text(data["next_step"]),
                command_line,
            )
        )
        if data.get("recall_text"):
            print(
                "\n" + _terminal_text(data["recall_text"], multiline=True),
                end="",
            )
    elif args.command == "task" and args.task_command == "list":
        tasks = data["tasks"]
        print("\n".join(_human_task(task) for task in tasks) if tasks else "No tasks.")
    elif args.command == "task" and args.task_command == "show":
        print(_human_task_detail(data))
    elif args.command == "session" and args.session_command == "list":
        sessions = data["sessions"]
        if not sessions:
            print("No managed sessions.")
        for session in sessions:
            primary = " primary" if session["is_primary"] else ""
            print(
                "%s  %s  [%s%s]"
                % (
                    session["managed_session_id"],
                    session["provider"],
                    session["state"],
                    primary,
                )
            )
    elif args.command == "session":
        primary = " primary" if data["is_primary"] else ""
        print(
            "%s  %s  [%s%s]"
            % (
                data["managed_session_id"],
                data["provider"],
                data["state"],
                primary,
            )
        )
    elif args.command == "source":
        state = "enabled" if data["enabled"] else "disabled"
        print(
            "%s %s (revision %s)"
            % (
                _terminal_text(data["source_kind"]),
                state,
                _terminal_text(data["revision"]),
            )
        )
    elif args.command == "index":
        print("Discovered: %s" % data["discovered"])
        print("Indexed: %s" % data["indexed"])
        print("Skipped: %s" % data["skipped"])
        print("Rebuilt: %s" % data["rebuilt"])
        print("Messages: %s" % data["messages"])
        print("Missing: %s" % data["missing"])
        if data["errors"]:
            print("Errors: %s" % len(data["errors"]))
            for error in data["errors"]:
                print(
                    "- %s"
                    % _terminal_text(error.get("code", "INDEX_ERROR"))
                )
        else:
            print("Errors: 0")
    elif args.command == "search":
        print(data["warning"])
        results = data["results"]
        if not results:
            print("No history results.")
        for index, result in enumerate(results, start=1):
            evidence = result["evidence"]
            print("Result %d" % index)
            print("Source: %s" % _terminal_text(result["source_kind"]))
            print("Time: %s" % _terminal_text(result["time"]))
            print("Role: %s" % _terminal_text(result["role"]))
            print(
                "Managed session: %s"
                % _terminal_text(result["managed_session_id"] or "None")
            )
            print(
                "Native session: %s"
                % _terminal_text(result["source_session_id"] or "None")
            )
            print("Task: %s" % _terminal_text(result["task_id"] or "None"))
            print("Snippet: %s" % _terminal_text(result["snippet"]))
            print(
                "Evidence: %s:offset=%s:fingerprint=%s"
                % (
                    _terminal_text(evidence["source_path"]),
                    _terminal_text(evidence["source_offset"]),
                    _terminal_text(evidence["source_fingerprint"]),
                )
            )
    elif args.command == "memory" and args.memory_command == "list":
        memories = data["memories"]
        if not memories:
            print("No memories.")
        for memory in memories:
            state = "forgotten" if memory["archived_at"] else "active"
            print(
                "%s  [%s] task=%s session=%s revision=%s"
                % (
                    memory["memory_id"],
                    state,
                    _terminal_text(memory["task_id"] or "None"),
                    _terminal_text(memory["managed_session_id"] or "None"),
                    _terminal_text(memory["revision"]),
                )
            )
            print(_terminal_text(memory["content"]))
    elif args.command == "memory":
        state = "forgotten" if data["archived_at"] else "active"
        print(
            "%s  [%s] task=%s session=%s revision=%s"
            % (
                data["memory_id"],
                state,
                _terminal_text(data["task_id"] or "None"),
                _terminal_text(data["managed_session_id"] or "None"),
                _terminal_text(data["revision"]),
            )
        )
        print(_terminal_text(data["content"]))
    elif args.command == "status" and args.all:
        projects = data["projects"]
        if not projects:
            print("No registered projects.")
        for project in projects:
            print("%s  %d tasks" % (project["canonical_path"], len(project["tasks"])))
    elif args.command == "status":
        print("Project: %s" % data["canonical_path"])
        print(
            "Current focus: %s" % _human_task_reference(data["current_focus"])
        )
        print(
            "Recommended next: %s"
            % _human_task_reference(data["recommended_next"])
        )
        print(
            "Counts: actionable=%d blocked=%d waiting=%d"
            % (
                data["counts"]["actionable"],
                data["counts"]["blocked"],
                data["counts"]["waiting"],
            )
        )
        primary_session = data["primary_session"]
        print(
            "Primary session: %s"
            % (
                "%s  %s  [%s]"
                % (
                    primary_session["managed_session_id"],
                    primary_session["provider"],
                    primary_session["state"],
                )
                if primary_session is not None
                else "None"
            )
        )
        print("Attached sessions: %d" % len(data["attached_sessions"]))
        latest = data["latest_verified_capsule"]
        print(
            "Latest verified capsule: %s"
            % (json.dumps(latest, ensure_ascii=False) if latest is not None else "None")
        )
    else:
        print(_human_task(data))


def _normalized_error(error: Exception) -> Any:
    from .capsule import CapsuleIntegrityError, CapsuleValidationError
    from .state import OffworkError

    if isinstance(error, OffworkError):
        return error
    if isinstance(error, FileNotFoundError):
        return OffworkError(
            "NOT_FOUND",
            str(error),
            exit_code=3,
            details={},
            recovery="Check the supplied path and retry.",
        )
    if isinstance(error, CapsuleIntegrityError):
        return OffworkError(
            "CAPSULE_INTEGRITY_FAILED",
            str(error),
            exit_code=4,
            details={},
            recovery="Restore an untampered capsule or capture a new one.",
        )
    if isinstance(error, (CapsuleValidationError, json.JSONDecodeError, CLIParseError)):
        return OffworkError(
            "INVALID_INPUT",
            str(error),
            exit_code=2,
            details={},
            recovery="Correct the input and retry.",
        )
    return OffworkError(
        "INTERNAL_ERROR",
        str(error) or error.__class__.__name__,
        exit_code=1,
        details={},
        recovery="Retry the command; inspect local state if the error persists.",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    wants_json = "--json" in arguments
    command = _command_from_argv(arguments)
    try:
        args = _parser().parse_args(arguments)
        command = _command_name(args)
        data, warnings = _execute(args)
        if args.json:
            _print_json(
                _envelope(
                    command,
                    ok=True,
                    data=data,
                    warnings=warnings,
                    error=None,
                )
            )
        else:
            _print_human(args, data)
            for warning in warnings:
                print("WARNING: %s" % warning["message"], file=sys.stderr)
        return 0
    except Exception as raw_error:
        error = _normalized_error(raw_error)
        if wants_json:
            _print_json(
                _envelope(
                    command,
                    ok=False,
                    data=None,
                    warnings=[],
                    error=error.as_dict(),
                )
            )
        else:
            print("OFFWORK BLOCKED: %s" % error.message, file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
