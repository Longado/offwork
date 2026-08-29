from __future__ import annotations

import json
import os
import subprocess
import unittest

from tests.helpers import TempProject


class ProjectInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_init_creates_private_project_state(self) -> None:
        result = self.temp.run("init", "--project", str(self.temp.project), "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        state_dir = self.temp.project / ".offwork"
        self.assertEqual(os.stat(state_dir).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(state_dir / "capsules").st_mode & 0o777, 0o700)
        for name in ("project.json", "state.sqlite3", "state.lock"):
            self.assertEqual(os.stat(state_dir / name).st_mode & 0o777, 0o600)

        metadata = json.loads((state_dir / "project.json").read_text())
        self.assertEqual(metadata["schema_version"], "offwork.project/v1")
        self.assertEqual(metadata["project_path"], str(self.temp.project.resolve()))
        self.assertRegex(metadata["project_id"], r"^project-[0-9a-f]{32}$")

    def test_nested_project_does_not_adopt_parent_git_root(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.temp.project)], check=True)
        nested = self.temp.project / "nested"
        nested.mkdir()

        result = self.temp.run("init", "--project", str(nested), "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads((nested / ".offwork" / "project.json").read_text())
        self.assertEqual(metadata["project_path"], str(nested.resolve()))

    def test_init_rejects_symlinked_state_directory(self) -> None:
        target = self.temp.root / "elsewhere"
        target.mkdir()
        (self.temp.project / ".offwork").symlink_to(target, target_is_directory=True)

        result = self.temp.run("init", "--project", str(self.temp.project), "--json")

        self.assertNotEqual(result.returncode, 0)
        envelope = self.temp.json_stdout(result)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "UNSAFE_STATE_PATH")

    def test_init_rejects_preexisting_wide_state_directory(self) -> None:
        state_dir = self.temp.project / ".offwork"
        state_dir.mkdir(mode=0o755)
        os.chmod(state_dir, 0o755)

        result = self.temp.run("init", "--project", str(self.temp.project), "--json")

        self.assertNotEqual(result.returncode, 0)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "UNSAFE_STATE_PATH")


if __name__ == "__main__":
    unittest.main()
