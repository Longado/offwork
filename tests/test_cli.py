from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import TempProject


class StartupCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_help_runs_outside_repository_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = self.temp.run("--help", cwd=cwd)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("trusted handoff receipts", result.stdout)
            self.assertFalse((cwd / ".offwork").exists())

    def test_version_runs_outside_repository_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = self.temp.run("--version", cwd=cwd)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"offwork \d+\.\d+\.\d+")
            self.assertFalse((cwd / ".offwork").exists())

    def test_init_json_returns_one_envelope(self) -> None:
        result = self.temp.run(
            "init", "--project", str(self.temp.project), "--json"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["schema_version"], "offwork.cli/v1")
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["command"], "init")
        self.assertEqual(envelope["data"]["project_path"], str(self.temp.project.resolve()))
        self.assertEqual(result.stderr, "")

    def test_argument_error_still_returns_one_json_envelope(self) -> None:
        result = self.temp.run("capture", "--json")

        self.assertNotEqual(result.returncode, 0)
        envelope = self.temp.json_stdout(result)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
