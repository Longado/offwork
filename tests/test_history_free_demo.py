from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Dict

from tests.helpers import TempProject


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "history_free"


class HistoryFreeDecisionFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_projects: list[TempProject] = []
        self.cases = json.loads(
            (FIXTURE_DIR / "cases.json").read_text(encoding="utf-8")
        )

    def tearDown(self) -> None:
        for temporary_project in self.temporary_projects:
            temporary_project.cleanup()

    def case(self, case_id: str) -> Dict[str, Any]:
        return next(case for case in self.cases["cases"] if case["case_id"] == case_id)

    def create_case(self, case_id: str) -> tuple[TempProject, Dict[str, Any], Dict[str, Any]]:
        case = self.case(case_id)
        temporary_project = TempProject()
        self.temporary_projects.append(temporary_project)
        temporary_project.init_git()
        initialized = temporary_project.init()
        self.assertRegex(initialized["project_id"], r"^project-[0-9a-f]{32}$")

        check = (
            f"{shlex.quote(sys.executable)} -c "
            f"{shlex.quote(case['check_script'])}"
        )
        task = temporary_project.add_task(
            title=case["task"]["title"],
            goal=case["task"]["goal"],
            checks=[check],
        )
        context = temporary_project.write_context(
            case["context"], f"{case_id}-context.json"
        )
        captured_result = temporary_project.run(
            "capture",
            "--task",
            task["task_id"],
            "--context",
            str(context),
            "--project",
            str(temporary_project.project),
            "--json",
        )
        self.assertEqual(
            captured_result.returncode,
            0,
            captured_result.stderr or captured_result.stdout,
        )
        captured = temporary_project.json_stdout(captured_result)["data"]
        self.assertEqual(captured["schema_version"], "offwork.receipt/v1")
        self.assertEqual(captured["auto_checked"]["status"], "passed")
        self.assertEqual(captured["auto_checked"]["checks"][0]["command"], check)
        self.assertEqual(captured["human_acceptance"]["status"], "pending")
        return temporary_project, task, captured

    def run_read_command(
        self,
        temporary_project: TempProject,
        task_id: str,
        capsule_id: str,
        command: str,
    ) -> subprocess.CompletedProcess[str]:
        if command == "resume":
            arguments = ["resume", "--task", task_id]
        elif command == "show":
            arguments = ["task", "show", task_id]
        else:
            raise AssertionError(f"unsupported read command: {command}")
        return temporary_project.run(
            *arguments,
            "--capsule",
            capsule_id,
            "--project",
            str(temporary_project.project),
            "--json",
        )

    def git_head(self, project: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def index_digest(self, project: Path) -> str:
        return hashlib.sha256((project / ".git" / "index").read_bytes()).hexdigest()

    def project_files(self, project: Path) -> Dict[str, tuple[str, int, str]]:
        snapshot: Dict[str, tuple[str, int, str]] = {}
        for path in sorted(project.rglob("*")):
            relative = path.relative_to(project)
            if relative.parts[0] in {".git", ".offwork"}:
                continue
            current = path.lstat()
            mode = stat.S_IMODE(current.st_mode)
            if path.is_symlink():
                snapshot[relative.as_posix()] = ("symlink", mode, os.readlink(path))
            elif path.is_file():
                snapshot[relative.as_posix()] = (
                    "file",
                    mode,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
        return snapshot

    def acceptance_state(
        self, project: Path, task_id: str
    ) -> tuple[tuple[Any, ...], tuple[tuple[Any, ...], ...]]:
        database = project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            task = connection.execute(
                "SELECT revision, current_capsule_id FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            events = connection.execute(
                "SELECT capsule_id, status, note, task_revision "
                "FROM human_acceptance_events ORDER BY task_revision, capsule_id"
            ).fetchall()
        if task is None:
            raise AssertionError("generated task is missing")
        return task, tuple(events)

    def mutable_state(self, project: Path, task_id: str) -> Dict[str, Any]:
        return {
            "head": self.git_head(project),
            "index": self.index_digest(project),
            "project_files": self.project_files(project),
            "acceptance": self.acceptance_state(project, task_id),
        }

    def assert_required_citations(
        self, envelope: Dict[str, Any], case: Dict[str, Any]
    ) -> None:
        for citation in case["expected"]["required_citations"]:
            value: Any = envelope
            for component in citation["receipt_path"].removeprefix("$.").split("."):
                value = value[component]
            self.assertEqual(value, citation["value"], citation["receipt_path"])

    def assert_read_commands_are_inert(
        self,
        temporary_project: TempProject,
        task: Dict[str, Any],
        capsule_id: str,
        *,
        expected_returncode: int,
        expected_error: str | None = None,
    ) -> Dict[str, Dict[str, Any]]:
        baseline = self.mutable_state(temporary_project.project, task["task_id"])
        envelopes: Dict[str, Dict[str, Any]] = {}
        for command in ("resume", "show"):
            result = self.run_read_command(
                temporary_project, task["task_id"], capsule_id, command
            )
            self.assertEqual(
                result.returncode,
                expected_returncode,
                result.stderr or result.stdout,
            )
            envelope = temporary_project.json_stdout(result)
            envelopes[command] = envelope
            if expected_error is None:
                self.assertTrue(envelope["ok"])
                self.assertEqual(
                    envelope["data"]["human_acceptance"]["status"], "pending"
                )
            else:
                self.assertFalse(envelope["ok"])
                self.assertEqual(envelope["error"]["code"], expected_error)
            self.assertEqual(
                self.mutable_state(temporary_project.project, task["task_id"]),
                baseline,
                f"{command} changed HEAD, index, project files, or acceptance state",
            )
        return envelopes

    def test_continue_case_has_fresh_complete_receipt_and_inert_reads(self) -> None:
        case = self.case("continue")
        temporary_project, task, captured = self.create_case("continue")
        capsule_id = captured["capsule"]["capsule_id"]

        self.assertEqual(captured["handoff_verified"]["integrity"]["status"], "passed")
        self.assertEqual(captured["handoff_verified"]["restore"]["status"], "passed")
        self.assertEqual(captured["workspace_freshness"]["status"], "fresh")
        self.assertEqual(captured["unknowns"], [])
        self.assertEqual(captured["open_loops"], [])
        self.assertEqual(captured["next_step"], case["expected"]["next_step_evidence"])
        self.assertEqual(case["expected"]["decision"], "continue")

        envelopes = self.assert_read_commands_are_inert(
            temporary_project, task, capsule_id, expected_returncode=0
        )
        for envelope in envelopes.values():
            self.assertEqual(envelope["data"]["workspace_freshness"]["status"], "fresh")
            self.assert_required_citations(envelope, case)

    def test_verify_case_has_changed_workspace_and_exact_safe_next_step(self) -> None:
        case = self.case("verify")
        temporary_project, task, captured = self.create_case("verify")
        capsule_id = captured["capsule"]["capsule_id"]
        mutation = case["after_capture"]
        (temporary_project.project / mutation["path"]).write_text(
            mutation["content"], encoding="utf-8"
        )

        envelopes = self.assert_read_commands_are_inert(
            temporary_project, task, capsule_id, expected_returncode=0
        )
        for envelope in envelopes.values():
            receipt = envelope["data"]
            self.assertEqual(receipt["handoff_verified"]["integrity"]["status"], "passed")
            self.assertEqual(receipt["handoff_verified"]["restore"]["status"], "passed")
            self.assertEqual(receipt["workspace_freshness"]["status"], "changed")
            self.assertIn(mutation["path"], receipt["workspace_freshness"]["changes"])
            self.assertIn(case["expected"]["material_unknown"], receipt["unknowns"])
            self.assertEqual(receipt["next_step"], case["expected"]["next_step_evidence"])
            self.assert_required_citations(envelope, case)
        self.assertEqual(case["expected"]["decision"], "verify")

    def test_stop_case_has_stable_integrity_failure_and_no_project_action(self) -> None:
        case = self.case("stop")
        temporary_project, task, captured = self.create_case("stop")
        capsule_id = captured["capsule"]["capsule_id"]
        manifest = (
            temporary_project.project
            / ".offwork"
            / "capsules"
            / capsule_id
            / "manifest.json"
        )
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + case["after_capture"]["append"],
            encoding="utf-8",
        )

        envelopes = self.assert_read_commands_are_inert(
            temporary_project,
            task,
            capsule_id,
            expected_returncode=2,
            expected_error="CAPSULE_INTEGRITY_FAILED",
        )
        for envelope in envelopes.values():
            details = envelope["error"]["details"]
            self.assertEqual(details["capsule_id"], capsule_id)
            self.assertEqual(details["integrity"], "failed")
            self.assertEqual(details["freshness"], "not_evaluated")
            self.assert_required_citations(envelope, case)
        self.assertEqual(case["expected"]["decision"], "stop")
        self.assertEqual(case["expected"]["proposed_first_action"]["mode"], "none")
        self.assertIsNone(case["expected"]["proposed_first_action"]["value"])

    def test_agent_contract_is_versioned_and_records_no_synthetic_run(self) -> None:
        prompt = (FIXTURE_DIR / "agent-prompt.md").read_text(encoding="utf-8")
        response_schema = json.loads(
            (FIXTURE_DIR / "response.schema.json").read_text(encoding="utf-8")
        )
        run_record = json.loads(
            (FIXTURE_DIR / "run-record.template.json").read_text(encoding="utf-8")
        )
        run_record_schema = json.loads(
            (FIXTURE_DIR / "run-record.schema.json").read_text(encoding="utf-8")
        )

        self.assertIn("only the explicit project path and CLI JSON envelope", prompt)
        self.assertIn("Do not open or infer prior Session history", prompt)
        self.assertIn("Do not execute the proposed first action", prompt)
        decision = response_schema["properties"]["decision"]
        self.assertEqual(decision["enum"], ["continue", "verify", "stop"])
        self.assertEqual(
            set(response_schema["required"]),
            {"decision", "cited_receipt_facts", "proposed_first_action"},
        )
        self.assertEqual(run_record["schema_version"], "offwork.history-free-run/v1")
        self.assertEqual(run_record["status"], "not_run")
        self.assertEqual(run_record["commands"], [])
        self.assertIsNone(run_record["overall_elapsed_ms"])
        self.assertIsNone(run_record["cli_json_envelope"])
        self.assertIsNone(run_record["response"])
        command_schema = run_record_schema["properties"]["commands"]["items"]
        self.assertEqual(
            set(command_schema["required"]),
            {"purpose", "argv", "elapsed_ms", "exit_code"},
        )
        self.assertEqual(
            run_record_schema["properties"]["status"]["enum"],
            ["not_run", "completed", "failed"],
        )
        self.assertIn("cli_json_envelope", run_record_schema["required"])
        envelope_schema = run_record_schema["properties"]["cli_json_envelope"]
        self.assertIn("exact", envelope_schema["description"])
        response_options = run_record_schema["properties"]["response"]["oneOf"]
        self.assertIn({"$ref": "response.schema.json"}, response_options)
        self.assertEqual(run_record_schema["$id"], "run-record.schema.json")
        self.assertEqual(response_schema["$id"], "response.schema.json")
        executed = run_record_schema["allOf"][0]
        self.assertEqual(
            executed["if"]["properties"]["status"]["enum"],
            ["completed", "failed"],
        )
        self.assertEqual(
            executed["then"]["properties"]["cli_json_envelope"]["type"],
            "object",
        )
        completed = run_record_schema["allOf"][1]
        self.assertEqual(
            completed["if"]["properties"]["status"]["const"], "completed"
        )
        completed_properties = completed["then"]["properties"]
        self.assertEqual(
            completed_properties["response"], {"$ref": "response.schema.json"}
        )

    def test_response_schema_binds_each_decision_to_its_case_action(self) -> None:
        response_schema = json.loads(
            (FIXTURE_DIR / "response.schema.json").read_text(encoding="utf-8")
        )
        branches = response_schema["oneOf"]
        action_contracts: Dict[str, Dict[str, Any]] = {}
        for branch in branches:
            decision = branch["properties"]["decision"]["const"]
            action = branch["properties"]["proposed_first_action"]["properties"]
            action_contracts[decision] = {
                "mode": action["mode"]["const"],
                "value": action["value"]["const"],
                "receipt_path": action["receipt_path"]["const"],
            }

        self.assertEqual(set(action_contracts), {"continue", "verify", "stop"})
        for case in self.cases["cases"]:
            decision = case["expected"]["decision"]
            expected_action = case["expected"]["proposed_first_action"]
            self.assertEqual(action_contracts[decision]["mode"], expected_action["mode"])
            self.assertEqual(action_contracts[decision]["value"], expected_action["value"])
            self.assertEqual(
                action_contracts[decision]["receipt_path"],
                "$.data.next_step" if decision != "stop" else None,
            )


if __name__ == "__main__":
    unittest.main()
