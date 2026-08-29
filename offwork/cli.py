from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, Optional, Sequence

from offwork import __version__
from offwork.errors import OffworkError
from offwork.output import error_envelope, render_receipt, success_envelope, write_json
from offwork.project import initialize_project, load_project
from offwork.state import StateService
from offwork.capsule import capture
from offwork.receipt import build_receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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
    return f"Initialized Offwork project {data['project_id']} at {data['project_path']}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if not arguments.command:
        parser.print_help()
        return 0

    try:
        if arguments.command == "init":
            data = initialize_project(arguments.project)
            if arguments.json_output:
                write_json(success_envelope("init", data))
            else:
                print(_human_init(data))
            return 0
        if arguments.command == "task" and arguments.task_command == "add":
            project = load_project(arguments.project)
            task = StateService(project["state_dir"]).add_task(
                arguments.title, arguments.goal, arguments.check
            )
            data = {
                "task_id": task["task_id"],
                "title": task["title"],
                "goal": task["goal"],
                "revision": task["revision"],
            }
            if arguments.json_output:
                write_json(success_envelope("task.add", data))
            else:
                print(f"Created Task {data['task_id']}: {data['title']}")
            return 0
        if arguments.command == "capture":
            project = load_project(arguments.project)
            capsule_id = capture(project, arguments.task_id, arguments.context)
            receipt = build_receipt(project, arguments.task_id, capsule_id)
            if arguments.json_output:
                write_json(success_envelope("capture", receipt))
            else:
                print(render_receipt(receipt), end="")
            return 0
        if arguments.command == "task" and arguments.task_command == "show":
            project = load_project(arguments.project)
            receipt = build_receipt(project, arguments.task_id, arguments.capsule)
            if arguments.json_output:
                write_json(success_envelope("task.show", receipt))
            else:
                print(render_receipt(receipt), end="")
            return 0
        if arguments.command == "resume":
            project = load_project(arguments.project)
            receipt = build_receipt(project, arguments.task_id, arguments.capsule)
            if arguments.json_output:
                write_json(success_envelope("resume", receipt))
            else:
                print(render_receipt(receipt), end="")
            return 0
        raise OffworkError("UNKNOWN_COMMAND", "Unknown command")
    except OffworkError as error:
        if getattr(arguments, "json_output", False):
            write_json(error_envelope(arguments.command, error))
        else:
            print(f"{error.code}: {error.message}", file=sys.stderr)
        return error.exit_code
