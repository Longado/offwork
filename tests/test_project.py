from __future__ import annotations

import json
import os
import shutil
import sqlite3
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

    def test_init_creates_exact_private_directories_under_restrictive_umask(self) -> None:
        result = self.temp.run(
            "init",
            "--project",
            str(self.temp.project),
            "--json",
            umask=0o777,
        )

        state_dir = self.temp.project / ".offwork"
        state_mode = os.stat(state_dir).st_mode & 0o777
        if state_mode != 0o700:
            os.chmod(state_dir, 0o700)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(state_mode, 0o700)
        self.assertEqual(os.stat(state_dir / "capsules").st_mode & 0o777, 0o700)

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

    def test_init_rejects_wide_project_file_without_changing_its_mode(self) -> None:
        self.temp.init()
        project_file = self.temp.project / ".offwork" / "project.json"
        os.chmod(project_file, 0o644)

        result = self.temp.run("init", "--project", str(self.temp.project), "--json")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(os.stat(project_file).st_mode & 0o777, 0o644)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "UNSAFE_STATE_PATH")

    def test_init_rejects_symlinked_database_without_touching_outside_target(self) -> None:
        self.temp.init()
        database = self.temp.project / ".offwork" / "state.sqlite3"
        outside = self.temp.root / "outside.sqlite3"
        with sqlite3.connect(str(outside)) as connection:
            connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES ('unchanged')")
        os.chmod(outside, 0o640)
        original_bytes = outside.read_bytes()
        original_mode = os.stat(outside).st_mode & 0o777
        database.unlink()
        database.symlink_to(outside)

        result = self.temp.run("init", "--project", str(self.temp.project), "--json")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outside.read_bytes(), original_bytes)
        self.assertEqual(os.stat(outside).st_mode & 0o777, original_mode)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "UNSAFE_STATE_PATH")

    def test_init_rejects_symlinked_lock(self) -> None:
        self.temp.init()
        lock_file = self.temp.project / ".offwork" / "state.lock"
        outside = self.temp.root / "outside.lock"
        outside.write_text("unchanged", encoding="utf-8")
        lock_file.unlink()
        lock_file.symlink_to(outside)

        result = self.temp.run("init", "--project", str(self.temp.project), "--json")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged")
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "UNSAFE_STATE_PATH")


class LoadedProjectBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_task_add_rejects_unsafe_state(self) -> None:
        result = self.temp.run(
            "task",
            "add",
            "unsafe state must fail closed",
            "--goal",
            "do not reuse it",
            "--project",
            str(self.temp.project),
            "--json",
        )
        self.assertNotEqual(result.returncode, 0)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "UNSAFE_STATE_PATH")

    def test_task_add_rejects_database_mode_changed_after_init(self) -> None:
        database = self.temp.project / ".offwork" / "state.sqlite3"
        os.chmod(database, 0o644)

        self.assert_task_add_rejects_unsafe_state()

        self.assertEqual(os.stat(database).st_mode & 0o777, 0o644)

    def test_task_add_rejects_lock_mode_changed_after_init(self) -> None:
        lock_file = self.temp.project / ".offwork" / "state.lock"
        os.chmod(lock_file, 0o644)

        self.assert_task_add_rejects_unsafe_state()

        self.assertEqual(os.stat(lock_file).st_mode & 0o777, 0o644)

    def test_task_add_rejects_capsules_mode_changed_after_init(self) -> None:
        capsules = self.temp.project / ".offwork" / "capsules"
        os.chmod(capsules, 0o755)

        self.assert_task_add_rejects_unsafe_state()

        self.assertEqual(os.stat(capsules).st_mode & 0o777, 0o755)

    def test_task_add_rejects_wide_sqlite_auxiliary_files(self) -> None:
        state_dir = self.temp.project / ".offwork"
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                auxiliary = state_dir / f"state.sqlite3{suffix}"
                auxiliary.write_bytes(b"unsafe auxiliary")
                os.chmod(auxiliary, 0o644)

                self.assert_task_add_rejects_unsafe_state()

                self.assertEqual(os.stat(auxiliary).st_mode & 0o777, 0o644)
                auxiliary.unlink()

    def test_task_add_rejects_symlinked_sqlite_auxiliary_files(self) -> None:
        state_dir = self.temp.project / ".offwork"
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                auxiliary = state_dir / f"state.sqlite3{suffix}"
                outside = self.temp.root / f"outside{suffix}"
                outside.write_bytes(b"outside unchanged")
                os.chmod(outside, 0o640)
                original_bytes = outside.read_bytes()
                original_mode = os.stat(outside).st_mode & 0o777
                auxiliary.symlink_to(outside)

                self.assert_task_add_rejects_unsafe_state()

                self.assertEqual(outside.read_bytes(), original_bytes)
                self.assertEqual(os.stat(outside).st_mode & 0o777, original_mode)
                auxiliary.unlink()

    def test_capture_rejects_symlinked_capsules_before_publication(self) -> None:
        task = self.temp.add_task()
        capsules = self.temp.project / ".offwork" / "capsules"
        outside = self.temp.root / "outside-capsules"
        outside.mkdir()
        shutil.rmtree(capsules)
        capsules.symlink_to(outside, target_is_directory=True)
        context = self.temp.write_context(
            {
                "summary": "ready",
                "agent_claims": [],
                "unknowns": [],
                "open_loops": [],
                "next_step": "stop before publication",
            }
        )

        result = self.temp.run(
            "capture",
            "--task",
            task["task_id"],
            "--context",
            str(context),
            "--project",
            str(self.temp.project),
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "UNSAFE_STATE_PATH")


if __name__ == "__main__":
    unittest.main()
