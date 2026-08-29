from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "offwork-capsule"


class CodexPluginTests(unittest.TestCase):
    def test_plugin_packages_the_offwork_skill(self) -> None:
        manifest = json.loads(
            (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["name"], "offwork-capsule")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["interface"]["displayName"], "Offwork Capsule")
        self.assertIn("capture", manifest["interface"]["defaultPrompt"][0].lower())

    def test_skill_preserves_offwork_evidence_boundaries(self) -> None:
        skill = (PLUGIN / "skills" / "offwork" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("name: offwork", skill)
        for state in (
            "agent_claimed",
            "offwork_observed",
            "auto_checked",
            "handoff_verified",
            "human_acceptance",
        ):
            self.assertIn(state, skill)
        self.assertIn("$offwork capture", skill)
        self.assertIn("$offwork resume", skill)
        self.assertIn("explicit accept or reject", skill)
        self.assertIn("not_run", skill)

    def test_repo_marketplace_exposes_the_plugin(self) -> None:
        marketplace = json.loads(
            (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        entry = next(
            plugin
            for plugin in marketplace["plugins"]
            if plugin["name"] == "offwork-capsule"
        )

        self.assertEqual(entry["source"]["path"], "./plugins/offwork-capsule")
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertEqual(entry["policy"]["authentication"], "ON_INSTALL")


if __name__ == "__main__":
    unittest.main()
