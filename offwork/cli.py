from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, Optional, Sequence

from offwork import __version__
from offwork.errors import OffworkError
from offwork.output import error_envelope, success_envelope, write_json
from offwork.project import initialize_project


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
        raise OffworkError("UNKNOWN_COMMAND", "Unknown command")
    except OffworkError as error:
        if getattr(arguments, "json_output", False):
            write_json(error_envelope(arguments.command, error))
        else:
            print(f"{error.code}: {error.message}", file=sys.stderr)
        return error.exit_code
