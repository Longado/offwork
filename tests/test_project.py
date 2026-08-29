from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import unittest
from unittest import mock

import offwork.project as project_module
from offwork.errors import OffworkError
from offwork.project import (
    _write_private_json,
    capture_workspace,
    compare_workspace,
    load_project,
)
from offwork.state import StateService
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

        connection = sqlite3.connect(str(state_dir / "state.sqlite3"))
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(version, 3)

    def test_init_rejects_unsupported_database_versions_without_rewriting_them(self) -> None:
        self.temp.init()
        database = self.temp.project / ".offwork" / "state.sqlite3"

        for version in (999, 2):
            with self.subTest(version=version):
                connection = sqlite3.connect(str(database))
                try:
                    connection.execute(f"PRAGMA user_version = {version}")
                    connection.commit()
                finally:
                    connection.close()

                result = self.temp.run(
                    "init", "--project", str(self.temp.project), "--json"
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout.count("\n"), 1)
                envelope = self.temp.json_stdout(result)
                self.assertEqual(envelope["error"]["code"], "UNSUPPORTED_STATE_SCHEMA")
                self.assertEqual(
                    envelope["error"]["details"],
                    {"actual_version": version, "supported_version": 3},
                )
                connection = sqlite3.connect(str(database))
                try:
                    unchanged = connection.execute("PRAGMA user_version").fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(unchanged, version)

    def test_private_metadata_write_retries_short_writes(self) -> None:
        destination = self.temp.root / "short-write-project.json"
        value = {
            "schema_version": "offwork.project/v1",
            "project_id": "project-short-write",
            "project_path": str(self.temp.project),
        }
        real_write = os.write
        write_sizes = []

        def short_first_write(descriptor: int, payload: bytes) -> int:
            requested = 7 if not write_sizes else len(payload)
            written = real_write(descriptor, payload[:requested])
            write_sizes.append(written)
            return written

        with mock.patch("offwork.project.os.write", side_effect=short_first_write):
            _write_private_json(destination, value)

        self.assertGreater(len(write_sizes), 1)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), value)

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

    def test_task_add_rejects_unsupported_schema_before_writing(self) -> None:
        database = self.temp.project / ".offwork" / "state.sqlite3"
        connection = sqlite3.connect(str(database))
        try:
            task_count_before = connection.execute(
                "SELECT COUNT(*) FROM tasks"
            ).fetchone()[0]
            connection.execute("PRAGMA user_version = 999")
            connection.commit()
        finally:
            connection.close()

        result = self.temp.run(
            "task",
            "add",
            "must not be written",
            "--goal",
            "fail closed",
            "--project",
            str(self.temp.project),
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count("\n"), 1)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "UNSUPPORTED_STATE_SCHEMA")
        connection = sqlite3.connect(str(database))
        try:
            task_count_after = connection.execute(
                "SELECT COUNT(*) FROM tasks"
            ).fetchone()[0]
            version_after = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(task_count_after, task_count_before)
        self.assertEqual(version_after, 999)

    def test_state_read_rejects_unsupported_schema(self) -> None:
        task = self.temp.add_task()
        project = load_project(str(self.temp.project))
        database = project["state_dir"] / "state.sqlite3"
        connection = sqlite3.connect(str(database))
        try:
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(OffworkError) as raised:
            StateService(project["state_dir"]).get_task(task["task_id"])

        self.assertEqual(raised.exception.code, "UNSUPPORTED_STATE_SCHEMA")
        self.assertEqual(
            raised.exception.details,
            {"actual_version": 2, "supported_version": 3},
        )

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


class WorkspaceObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init_git()
        self.temp.init()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_git_invocation_disables_fsmonitor_with_empty_config_value(self) -> None:
        project = load_project(str(self.temp.project))
        completed = subprocess.CompletedProcess(
            args=["git"],
            returncode=1,
            stdout=b"",
            stderr=b"",
        )

        with mock.patch("offwork.project.subprocess.run", return_value=completed) as run:
            capture_workspace(project)

        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["git", "-c", "core.fsmonitor="])

    def test_git_invocation_disables_optional_locks_in_copied_environment(self) -> None:
        project = load_project(str(self.temp.project))
        completed = subprocess.CompletedProcess(
            args=["git"],
            returncode=1,
            stdout=b"",
            stderr=b"",
        )

        with mock.patch("offwork.project.subprocess.run", return_value=completed) as run:
            capture_workspace(project)

        keyword_arguments = run.call_args.kwargs
        self.assertIn("env", keyword_arguments)
        environment = keyword_arguments["env"]
        self.assertIsNot(environment, os.environ)
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")

    def test_git_timeout_returns_structured_unavailable_snapshot(self) -> None:
        project = load_project(str(self.temp.project))
        timeout = subprocess.TimeoutExpired(cmd=["git"], timeout=5.0)

        with mock.patch("offwork.project.subprocess.run", side_effect=timeout) as run:
            try:
                snapshot = capture_workspace(project)
            except subprocess.TimeoutExpired:
                self.fail("Git timeout escaped receipt inspection")

        self.assertFalse(snapshot["reliable"])
        self.assertEqual(snapshot["reason"], "git_timeout")
        self.assertEqual(run.call_args.kwargs["timeout"], 5.0)

    def test_descriptor_dup_failure_returns_structured_unavailable_snapshot(self) -> None:
        project = load_project(str(self.temp.project))
        git_results = (
            subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=os.fsencode(str(self.temp.project)),
                stderr=b"",
            ),
            subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=b"",
                stderr=b"",
            ),
            subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=b"tracked.txt\0",
                stderr=b"",
            ),
        )

        with mock.patch("offwork.project._git", side_effect=git_results), mock.patch(
            "offwork.project.os.dup",
            side_effect=OSError("descriptor limit reached"),
        ):
            try:
                snapshot = capture_workspace(project)
            except OSError:
                self.fail("descriptor duplication failure escaped workspace inspection")

        self.assertFalse(snapshot["reliable"])
        self.assertEqual(snapshot["reason"], "unsafe_workspace_path")

    def test_pathspec_magic_project_name_cannot_hide_workspace_change(self) -> None:
        parent = self.temp.root / "magic-parent"
        project_path = parent / ":(literal)nested"
        project_path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(parent)], check=True)
        subprocess.run(
            ["git", "-C", str(parent), "config", "user.email", "offwork@example.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(parent), "config", "user.name", "Offwork Tests"],
            check=True,
        )
        tracked = project_path / "tracked.txt"
        tracked.write_text("captured\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(parent), "add", "--all"], check=True)
        subprocess.run(["git", "-C", str(parent), "commit", "-qm", "initial"], check=True)
        metadata = self.temp.run("init", "--project", str(project_path), "--json")
        self.assertEqual(metadata.returncode, 0, metadata.stderr or metadata.stdout)
        project = load_project(str(project_path))

        captured = capture_workspace(project)
        tracked.write_text("changed\n", encoding="utf-8")
        current = capture_workspace(project)

        self.assertTrue(captured["reliable"])
        self.assertIn("tracked.txt", captured["entries"])
        freshness = compare_workspace(captured, current)
        self.assertEqual(freshness["status"], "changed")
        self.assertIn("tracked.txt", freshness["changes"])

    @unittest.skipIf(os.name == "nt", "backslash is not a legal filename character on Windows")
    def test_backslash_project_name_cannot_hide_workspace_change(self) -> None:
        parent = self.temp.root / "backslash-parent"
        project_path = parent / "nested\\project"
        project_path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(parent)], check=True)
        subprocess.run(
            ["git", "-C", str(parent), "config", "user.email", "offwork@example.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(parent), "config", "user.name", "Offwork Tests"],
            check=True,
        )
        tracked = project_path / "tracked.txt"
        tracked.write_text("captured\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(parent), "add", "--all"], check=True)
        subprocess.run(["git", "-C", str(parent), "commit", "-qm", "initial"], check=True)
        metadata = self.temp.run("init", "--project", str(project_path), "--json")
        self.assertEqual(metadata.returncode, 0, metadata.stderr or metadata.stdout)
        project = load_project(str(project_path))

        captured = capture_workspace(project)
        tracked.write_text("changed\n", encoding="utf-8")
        current = capture_workspace(project)

        self.assertTrue(captured["reliable"])
        self.assertIn("tracked.txt", captured["entries"])
        freshness = compare_workspace(captured, current)
        self.assertEqual(freshness["status"], "changed")
        self.assertIn("tracked.txt", freshness["changes"])

    def test_new_nested_git_root_makes_workspace_unavailable(self) -> None:
        nested = self.temp.project / "nested"
        nested.mkdir()
        (nested / "tracked.txt").write_text("captured\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.temp.project), "add", "nested/tracked.txt"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.temp.project), "commit", "-qm", "add nested file"],
            check=True,
        )
        project = load_project(str(self.temp.project))
        captured = capture_workspace(project)

        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        current = capture_workspace(project)

        self.assertTrue(captured["reliable"])
        self.assertFalse(current["reliable"])
        self.assertEqual(current["reason"], "nested_git_repository")
        freshness = compare_workspace(captured, current)
        self.assertEqual(freshness["status"], "unavailable")
        self.assertIn("nested_git_repository", freshness["limitations"])

    def test_project_becoming_git_root_makes_comparison_unavailable(self) -> None:
        parent = self.temp.root / "boundary-parent"
        project_path = parent / "nested"
        project_path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(parent)], check=True)
        subprocess.run(
            ["git", "-C", str(parent), "config", "user.email", "offwork@example.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(parent), "config", "user.name", "Offwork Tests"],
            check=True,
        )
        (project_path / "tracked.txt").write_text("captured\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(parent), "add", "--all"], check=True)
        subprocess.run(["git", "-C", str(parent), "commit", "-qm", "initial"], check=True)
        metadata = self.temp.run("init", "--project", str(project_path), "--json")
        self.assertEqual(metadata.returncode, 0, metadata.stderr or metadata.stdout)
        project = load_project(str(project_path))
        captured = capture_workspace(project)

        subprocess.run(["git", "init", "-q", str(project_path)], check=True)
        current = capture_workspace(project)

        self.assertTrue(captured["reliable"])
        self.assertTrue(current["reliable"])
        freshness = compare_workspace(captured, current)
        self.assertEqual(freshness["status"], "unavailable")
        self.assertIn("git_boundary_changed", freshness["limitations"])

    def test_change_during_scan_cannot_return_false_fresh(self) -> None:
        project = load_project(str(self.temp.project))
        captured = capture_workspace(project)
        original_snapshot_entry = project_module._snapshot_entry
        mutation_done = False

        def snapshot_then_mutate(project_descriptor: int, relative: str) -> dict:
            nonlocal mutation_done
            entry = original_snapshot_entry(project_descriptor, relative)
            if relative == "tracked.txt" and not mutation_done:
                mutation_done = True
                (self.temp.project / relative).write_text("changed during scan\n", encoding="utf-8")
            return entry

        with mock.patch(
            "offwork.project._snapshot_entry",
            side_effect=snapshot_then_mutate,
        ):
            current = capture_workspace(project)

        self.assertTrue(mutation_done)
        self.assertFalse(current["reliable"])
        self.assertEqual(current["reason"], "unstable_workspace_scan")
        freshness = compare_workspace(captured, current)
        self.assertEqual(freshness["status"], "unavailable")
        self.assertIn("unstable_workspace_scan", freshness["limitations"])

    def test_git_index_change_during_scan_makes_workspace_unavailable(self) -> None:
        project = load_project(str(self.temp.project))
        original_git = project_module._git
        mutation_done = False

        def git_then_mutate_index(git_project, *arguments):
            nonlocal mutation_done
            result = original_git(git_project, *arguments)
            if arguments[:2] == ("ls-files", "--stage") and not mutation_done:
                mutation_done = True
                blob = subprocess.run(
                    ["git", "-C", str(self.temp.project), "hash-object", "-w", "--stdin"],
                    input=b"staged-only change\n",
                    stdout=subprocess.PIPE,
                    check=True,
                ).stdout.decode("ascii").strip()
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.temp.project),
                        "update-index",
                        "--cacheinfo",
                        "100644",
                        blob,
                        "tracked.txt",
                    ],
                    check=True,
                )
            return result

        with mock.patch("offwork.project._git", side_effect=git_then_mutate_index):
            current = capture_workspace(project)

        self.assertTrue(mutation_done)
        self.assertFalse(current["reliable"])
        self.assertEqual(current["reason"], "unstable_workspace_scan")

    def test_git_status_failure_cannot_produce_reliable_snapshot(self) -> None:
        project = load_project(str(self.temp.project))
        original_git = project_module._git

        def fail_status(git_project, *arguments):
            if arguments and arguments[0] == "status":
                return subprocess.CompletedProcess(
                    args=["git", *arguments],
                    returncode=1,
                    stdout=b"",
                    stderr=b"status failed",
                )
            return original_git(git_project, *arguments)

        with mock.patch("offwork.project._git", side_effect=fail_status):
            current = capture_workspace(project)

        self.assertFalse(current["reliable"])
        self.assertEqual(current["reason"], "git_scan_failed")

    def test_internal_state_change_during_scan_remains_excluded(self) -> None:
        project = load_project(str(self.temp.project))
        original_git = project_module._git
        mutation_done = False

        def git_then_mutate_state(git_project, *arguments):
            nonlocal mutation_done
            result = original_git(git_project, *arguments)
            if arguments and arguments[0] == "ls-files" and "--others" in arguments and not mutation_done:
                mutation_done = True
                (self.temp.project / ".offwork" / "scan-probe").write_text(
                    "ignored state\n",
                    encoding="utf-8",
                )
            return result

        with mock.patch("offwork.project._git", side_effect=git_then_mutate_state):
            current = capture_workspace(project)

        self.assertTrue(mutation_done)
        self.assertTrue(current["reliable"])


if __name__ == "__main__":
    unittest.main()
