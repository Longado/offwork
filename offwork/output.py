from __future__ import annotations

import json
from typing import Any, Dict

from offwork.errors import OffworkError


SCHEMA_VERSION = "offwork.cli/v1"


def success_envelope(command: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "command": command,
        "data": data,
    }


def error_envelope(command: str, error: OffworkError) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "command": command,
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        },
    }


def write_json(value: Dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))

