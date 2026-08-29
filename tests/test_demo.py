from __future__ import annotations

import shlex
import sys
import unittest
from pathlib import Path

from tests.helpers import TempProject


class FiveMinutePrototypeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init_git()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_complete_handoff_receipt_story(self) -> None:
        initialized = self.temp.init()
        self.assertRegex(initialized["project_id"], r"^project-")
        check = f"{shlex.quote(sys.executable)} -c \"assert False, 'controlled failure'\""
        task = self.temp.add_task(checks=[check])
        sentinel = self.temp.project / "next-step-was-executed"
        context = {
            "summary": "已实现登录修复并声称测试通过",
            "agent_claims": ["登录失败已经修复", "测试全部通过"],
            "unknowns": ["旧 Token 迁移行为尚未确认"],
            "open_loops": [
                {
                    "title": "确认旧 Token 的迁移行为",
                    "disposition": "resolve",
                    "note": "先运行迁移测试",
                }
            ],
            "next_step": f"touch {sentinel}",
        }
        context_path = self.temp.write_context(context)

        capture = self.temp.run(
            "capture", "--task", task["task_id"], "--context", str(context_path),
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(capture.returncode, 0, capture.stderr or capture.stdout)
        captured = self.temp.json_stdout(capture)["data"]
        capsule_id = captured["capsule"]["capsule_id"]
        self.assertEqual(captured["agent_claimed"]["items"], context["agent_claims"])
        self.assertEqual(captured["auto_checked"]["status"], "failed")
        self.assertEqual(captured["unknowns"], context["unknowns"])
        self.assertEqual(captured["open_loops"], context["open_loops"])
        self.assertEqual(captured["workspace_freshness"]["status"], "fresh")
        self.assertEqual(captured["human_acceptance"]["status"], "pending")

        resume = self.temp.run(
            "resume", "--task", task["task_id"], "--capsule", capsule_id,
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(resume.returncode, 0, resume.stderr or resume.stdout)
        self.assertFalse(sentinel.exists(), "resume must not execute next_step")

        (self.temp.project / "tracked.txt").write_text("changed after capture\n", encoding="utf-8")
        changed_result = self.temp.run(
            "task", "show", task["task_id"], "--capsule", capsule_id,
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(changed_result.returncode, 0, changed_result.stderr or changed_result.stdout)
        changed = self.temp.json_stdout(changed_result)["data"]
        self.assertEqual(changed["handoff_verified"]["integrity"]["status"], "passed")
        self.assertEqual(changed["handoff_verified"]["restore"]["status"], "passed")
        self.assertEqual(changed["workspace_freshness"]["status"], "changed")
        self.assertEqual(changed["human_acceptance"]["status"], "pending")

        accepted_result = self.temp.run(
            "task", "accept", task["task_id"], "--capsule", capsule_id,
            "--if-revision", str(changed["task"]["current_revision"]),
            "--note", "reviewed after workspace warning",
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(accepted_result.returncode, 0, accepted_result.stderr or accepted_result.stdout)
        accepted = self.temp.json_stdout(accepted_result)["data"]
        self.assertEqual(accepted["human_acceptance"]["status"], "accepted")
        self.assertEqual(accepted["workspace_freshness"]["status"], "changed")

        human = self.temp.run(
            "task", "show", task["task_id"], "--capsule", capsule_id,
            "--project", str(self.temp.project)
        )
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("Observed by Offwork:", human.stdout)
        self.assertIn(str(self.temp.project.resolve()), human.stdout)
        self.assertIn("tracked.txt", human.stdout)
        self.assertIn("Checks: failed", human.stdout)
        self.assertIn(check, human.stdout)
        for fact in (
            capsule_id,
            context["summary"],
            context["unknowns"][0],
            context["open_loops"][0]["title"],
            context["next_step"],
            "changed",
            "accepted",
        ):
            self.assertIn(fact, human.stdout)

    def test_readme_demo_is_copy_pasteable_and_shows_claim_check_contradiction(self) -> None:
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("cat > \"$DEMO_PROJECT/context.json\" <<'JSON'", readme)
        self.assertIn("CHECK_STATUS", readme)
        self.assertIn("assert False", readme)
        self.assertIn('"测试全部通过"', readme)
        self.assertNotIn("OFFWORK=/absolute/path", readme)
        self.assertNotIn("--task TASK_ID", readme)
        self.assertNotIn("--capsule CAPSULE_ID", readme)


if __name__ == "__main__":
    unittest.main()
