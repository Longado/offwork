from __future__ import annotations

import json
import shlex
import sys
import unittest

from tests.helpers import TempProject


CONTEXT = {
    "summary": "已实现 Token 刷新修复并补充测试",
    "agent_claims": ["登录失败已经修复", "测试全部通过"],
    "unknowns": ["旧 Token 迁移行为尚未确认"],
    "open_loops": [
        {
            "title": "确认旧 Token 的迁移行为",
            "disposition": "resolve",
            "note": "先运行迁移测试",
        }
    ],
    "next_step": "运行旧 Token 迁移测试",
}


class CapsuleCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init()
        self.task = self.temp.add_task()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def capture(self, context: dict = CONTEXT, name: str = "context.json") -> dict:
        context_path = self.temp.write_context(context, name)
        result = self.temp.run(
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(context_path),
            "--project",
            str(self.temp.project),
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return self.temp.json_stdout(result)["data"]

    def test_capture_preserves_handoff_context_without_promoting_claims(self) -> None:
        receipt = self.capture()

        self.assertEqual(receipt["agent_claimed"]["items"], CONTEXT["agent_claims"])
        self.assertEqual(receipt["unknowns"], CONTEXT["unknowns"])
        self.assertEqual(receipt["open_loops"], CONTEXT["open_loops"])
        self.assertEqual(receipt["next_step"], CONTEXT["next_step"])
        self.assertEqual(receipt["auto_checked"]["status"], "not_run")
        self.assertEqual(receipt["auto_checked"]["checks"], [])
        self.assertEqual(receipt["human_acceptance"]["status"], "pending")
        self.assertEqual(receipt["workspace_freshness"]["status"], "unavailable")

    def test_capture_returns_explicit_capsule_and_revisions(self) -> None:
        receipt = self.capture()

        self.assertRegex(receipt["capsule"]["capsule_id"], r"^capsule-[0-9a-f]{32}$")
        self.assertEqual(receipt["task"]["captured_revision"], 2)
        self.assertEqual(receipt["task"]["current_revision"], 2)
        capsule_dir = (
            self.temp.project
            / ".offwork"
            / "capsules"
            / receipt["capsule"]["capsule_id"]
        )
        self.assertTrue((capsule_dir / "capsule.json").is_file())
        self.assertTrue((capsule_dir / "checks.json").is_file())
        self.assertTrue((capsule_dir / "restore-test.json").is_file())
        self.assertTrue((capsule_dir / "manifest.json").is_file())

    def test_invalid_context_returns_stable_json_error(self) -> None:
        invalid = dict(CONTEXT)
        del invalid["next_step"]
        path = self.temp.write_context(invalid, "invalid.json")

        result = self.temp.run(
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(path),
            "--project",
            str(self.temp.project),
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "INVALID_CAPTURE_CONTEXT")

    def test_unknown_task_does_not_publish_capsule(self) -> None:
        path = self.temp.write_context(CONTEXT)
        result = self.temp.run(
            "capture",
            "--task",
            "task-missing",
            "--context",
            str(path),
            "--project",
            str(self.temp.project),
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "TASK_NOT_FOUND")
        capsules = self.temp.project / ".offwork" / "capsules"
        self.assertEqual(list(capsules.iterdir()), [])


class TaskCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_task_add_creates_revision_one(self) -> None:
        task = self.temp.add_task()

        self.assertRegex(task["task_id"], r"^task-[0-9a-f]{32}$")
        self.assertEqual(task["title"], "修复登录失败")
        self.assertEqual(task["goal"], "恢复 Token 刷新行为")
        self.assertEqual(task["revision"], 1)


class CheckRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init_git()
        self.temp.init()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def capture_with_checks(self, checks: list[str]) -> tuple[dict, object]:
        task = self.temp.add_task(checks=checks)
        path = self.temp.write_context(CONTEXT)
        result = self.temp.run(
            "capture",
            "--task",
            task["task_id"],
            "--context",
            str(path),
            "--project",
            str(self.temp.project),
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return self.temp.json_stdout(result)["data"], result

    def test_all_checks_must_run_and_pass(self) -> None:
        command = (
            f"{shlex.quote(sys.executable)} -c \"print(''.join(map(chr, "
            "[76,69,65,75,95,83,69,78,84,73,78,69,76])))\""
        )
        receipt, result = self.capture_with_checks([command])

        self.assertEqual(receipt["auto_checked"]["status"], "passed")
        check = receipt["auto_checked"]["checks"][0]
        self.assertEqual(check["argv"][:2], [sys.executable, "-c"])
        self.assertEqual(check["cwd"], str(self.temp.project.resolve()))
        self.assertEqual(check["returncode"], 0)
        self.assertNotIn("LEAK_SENTINEL", result.stdout)

    def test_nonzero_check_is_failed_even_when_another_check_passes(self) -> None:
        passed = f"{shlex.quote(sys.executable)} -c \"pass\""
        failed = f"{shlex.quote(sys.executable)} -c \"import sys; sys.exit(7)\""
        receipt, _ = self.capture_with_checks([passed, failed])

        self.assertEqual(receipt["auto_checked"]["status"], "failed")
        self.assertEqual([item["status"] for item in receipt["auto_checked"]["checks"]], ["passed", "failed"])

    def test_spawn_error_makes_checks_unavailable(self) -> None:
        receipt, _ = self.capture_with_checks(["offwork-command-that-does-not-exist"])

        self.assertEqual(receipt["auto_checked"]["status"], "unavailable")
        self.assertEqual(receipt["auto_checked"]["checks"][0]["status"], "unavailable")

    def test_timeout_makes_checks_unavailable(self) -> None:
        command = f"{shlex.quote(sys.executable)} -c \"import time; time.sleep(5)\""
        receipt, _ = self.capture_with_checks([command])

        self.assertEqual(receipt["auto_checked"]["status"], "unavailable")
        self.assertEqual(receipt["auto_checked"]["checks"][0]["status"], "unavailable")

    def test_snapshot_is_collected_after_check_changes_project(self) -> None:
        command = (
            f"{shlex.quote(sys.executable)} -c \"from pathlib import Path; "
            "Path('tracked.txt').write_text('changed by check\\n')\""
        )
        receipt, _ = self.capture_with_checks([command])

        self.assertEqual(receipt["auto_checked"]["status"], "passed")
        self.assertEqual(receipt["workspace_freshness"]["status"], "fresh")


if __name__ == "__main__":
    unittest.main()
