from __future__ import annotations

import os
import sqlite3
from pathlib import Path


SCHEMA_VERSION = 1


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(path: Path) -> None:
    if not path.exists():
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    os.chmod(path, 0o600)
    with connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                goal TEXT NOT NULL,
                check_commands_json TEXT NOT NULL,
                revision INTEGER NOT NULL,
                current_capsule_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

