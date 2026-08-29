from __future__ import annotations

import argparse
import contextlib
import io
import sys
from typing import Any, Dict, Optional, Sequence

from offwork import __version__
from offwork.checks import validate_check_commands
from offwork.errors import OffworkError
from offwork.output import (
    error_envelope,
    render_error,
    render_receipt,
    success_envelope,
    visible_text,
    write_json,
)
from offwork.project import initialize_project, load_project
from offwork.capsule import (
    _require_no_pending_capsule_reconciliation_locked,
    capture,
)
from offwork.receipt import build_receipt
from offwork.state import StateService, state_lock


class OffworkArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise OffworkError(
            "INVALID_ARGUMENT",
            message,
            details={"usage": self.format_usage().strip()},
        )


def build_parser() -> argparse.ArgumentParser:
    parser = OffworkArgumentParser(
        prog="offwork",
        description="Local trusted handoff receipts for interrupted Agent work.",
    )
    parser.add_argument("--version", action="version", version=f"offwork {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="initialize project-local state")
    init_parser.add_argument("--project", required=True)
    init_parser.add_argument("--json", action="store_true", dest="json_output")

    task_parser = subparsers.add_parser("task", help="manage handoff tasks")
    task_subparsers = task_parser.add_subparsers(dest="task_command", required=True)
    add_parser = task_subparsers.add_parser("add", help="create a Task")
    add_parser.add_argument("title")
    add_parser.add_argument("--goal", required=True)
    add_parser.add_argument("--check", action="append", default=[])
    add_parser.add_argument("--project", required=True)
    add_parser.add_argument("--json", action="store_true", dest="json_output")
    show_parser = task_subparsers.add_parser("show", help="show a Task Receipt")
    show_parser.add_argument("task_id")
    show_parser.add_argument("--capsule")
    show_parser.add_argument("--project", required=True)
    show_parser.add_argument("--json", action="store_true", dest="json_output")
    for action in ("accept", "reject"):
        decision_parser = task_subparsers.add_parser(
            action, help=f"{action} a specific Capsule handoff"
        )
        decision_parser.add_argument("task_id")
        decision_parser.add_argument("--capsule", required=True)
        decision_parser.add_argument("--if-revision", required=True, type=int)
        decision_parser.add_argument("--note")
        decision_parser.add_argument("--project", required=True)
        decision_parser.add_argument("--json", action="store_true", dest="json_output")

    capture_parser = subparsers.add_parser("capture", help="capture a handoff Capsule")
    capture_parser.add_argument("--task", required=True, dest="task_id")
    capture_parser.add_argument("--context", required=True)
    capture_parser.add_argument("--project", required=True)
    capture_parser.add_argument("--json", action="store_true", dest="json_output")

    resume_parser = subparsers.add_parser("resume", help="render a safe handoff Receipt")
    resume_parser.add_argument("--task", required=True, dest="task_id")
    resume_parser.add_argument("--capsule")
    resume_parser.add_argument("--project", required=True)
    resume_parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _human_init(data: Dict[str, Any]) -> str:
    return (
        f"Initialized Offwork project {visible_text(data['project_id'])} "
        f"at {visible_text(data['project_path'])}"
    )


def _command_hint(arguments: Sequence[str]) -> str:
    positional = [item for item in arguments if item != "--json"]
    if not positional:
        return "cli"
    if "--version" in positional:
        return "version"
    top_level = positional[0]
    if top_level == "task" and len(positional) > 1:
        nested = positional[1]
        if nested in {"add", "show", "accept", "reject"}:
            return f"task.{nested}"
    if not top_level.startswith("-"):
        return top_level
    return "cli"


def _parsed_command(arguments: Any, fallback: str) -> str:
    command = getattr(arguments, "command", None)
    if command == "task":
        task_command = getattr(arguments, "task_command", None)
        if task_command:
            return f"task.{task_command}"
    return command or fallback


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_arguments = list(argv) if argv is not None else sys.argv[1:]
    json_requested = "--json" in raw_arguments
    command_hint = _command_hint(raw_arguments)
    parse_arguments = [item for item in raw_arguments if item != "--json"]
    arguments = None
    try:
        parser = build_parser()
        if json_requested:
            captured_output = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured_output):
                    arguments = parser.parse_args(parse_arguments)
            except SystemExit as exit_request:
                if exit_request.code != 0:
                    raise
                output_command = command_hint
                if output_command == "cli":
                    output_command = "help"
                write_json(
                    success_envelope(
                        output_command,
                        {"text": captured_output.getvalue().strip()},
                    )
                )
                return 0
        else:
            arguments = parser.parse_args(parse_arguments)
        if not arguments.command:
            if json_requested:
                write_json(success_envelope("help", {"text": parser.format_help().strip()}))
            else:
                parser.print_help()
            return 0
        if arguments.command == "init":
            data = initialize_project(arguments.project)
            if json_requested:
                write_json(success_envelope("init", data))
            else:
                print(_human_init(data))
            return 0
        if arguments.command == "task" and arguments.task_command == "add":
            project = load_project(arguments.project)
            validate_check_commands(arguments.check)
            task = StateService(project["state_dir"]).add_task(
                arguments.title, arguments.goal, arguments.check
            )
            data = {
                "task_id": task["task_id"],
                "title": task["title"],
                "goal": task["goal"],
                "revision": task["revision"],
            }
            if json_requested:
                write_json(success_envelope("task.add", data))
            else:
                print(
                    f"Created Task {visible_text(data['task_id'])}: "
                    f"{visible_text(data['title'])}"
                )
            return 0
        if arguments.command == "capture":
            project = load_project(arguments.project)
            capsule_id = capture(project, arguments.task_id, arguments.context)
            receipt = build_receipt(project, arguments.task_id, capsule_id)
            if json_requested:
                write_json(success_envelope("capture", receipt))
            else:
                print(render_receipt(receipt), end="")
            return 0
        if arguments.command == "task" and arguments.task_command == "show":
            project = load_project(arguments.project)
            receipt = build_receipt(project, arguments.task_id, arguments.capsule)
            if json_requested:
                write_json(success_envelope("task.show", receipt))
            else:
                print(render_receipt(receipt), end="")
            return 0
        if arguments.command == "task" and arguments.task_command in {"accept", "reject"}:
            project = load_project(arguments.project)
            state = StateService(project["state_dir"])
            with state_lock(project["state_dir"]):
                state.validate_acceptance_target(arguments.task_id, arguments.capsule)
                receipt = build_receipt(
                    project,
                    arguments.task_id,
                    arguments.capsule,
                    reconcile_orphans=False,
                )
                _require_no_pending_capsule_reconciliation_locked(
                    project, arguments.task_id
                )
                acceptance = state.record_acceptance(
                    task_id=arguments.task_id,
                    capsule_id=arguments.capsule,
                    expected_revision=arguments.if_revision,
                    status=(
                        "accepted"
                        if arguments.task_command == "accept"
                        else "rejected"
                    ),
                    note=arguments.note,
                )
            receipt["task"]["current_revision"] = acceptance["task_revision"]
            receipt["human_acceptance"] = {
                "status": acceptance["status"],
                "acted_at": acceptance["acted_at"],
                "note": acceptance["note"],
            }
            command = f"task.{arguments.task_command}"
            if json_requested:
                write_json(success_envelope(command, receipt))
            else:
                print(render_receipt(receipt), end="")
            return 0
        if arguments.command == "resume":
            project = load_project(arguments.project)
            receipt = build_receipt(project, arguments.task_id, arguments.capsule)
            if json_requested:
                write_json(success_envelope("resume", receipt))
            else:
                print(render_receipt(receipt), end="")
            return 0
        raise OffworkError("UNKNOWN_COMMAND", "Unknown command")
    except OffworkError as error:
        if json_requested or getattr(arguments, "json_output", False):
            command = _parsed_command(arguments, command_hint)
            write_json(error_envelope(command, error))
        else:
            print(render_error(error), end="", file=sys.stderr)
        return error.exit_code
