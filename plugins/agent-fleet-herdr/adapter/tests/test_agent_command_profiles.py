from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "agent_command_profiles.py"
SPEC = importlib.util.spec_from_file_location("agent_command_profiles", MODULE_PATH)
agent_command_profiles = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(agent_command_profiles)


PROFILE = {
    "apiVersion": "fleet.runtime.harness/v1",
    "kind": "AgentCommandProfile",
    "metadata": {"id": "local/codex-personal", "version": 1},
    "spec": {"product": "codex", "command": "codex-personal"},
}


class AgentCommandProfileTest(unittest.TestCase):
    def test_profile_keeps_account_command_separate_from_fleet_and_adapter(self):
        self.assertEqual([], agent_command_profiles.validate_document(PROFILE))
        self.assertEqual(
            "local/codex-personal@1",
            agent_command_profiles.profile_identity(PROFILE),
        )

    def test_profile_rejects_a_shell_program_instead_of_one_command_name(self):
        invalid = json.loads(json.dumps(PROFILE))
        invalid["spec"]["command"] = "CODEX_HOME=/tmp/profile codex"

        errors = agent_command_profiles.validate_document(invalid)

        self.assertTrue(any("command" in error for error in errors))

    def test_catalog_rejects_duplicate_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.yml").write_text(json.dumps(PROFILE), encoding="utf-8")
            (root / "two.yml").write_text(json.dumps(PROFILE), encoding="utf-8")

            with self.assertRaisesRegex(
                agent_command_profiles.AgentCommandProfileError,
                "duplicate AgentCommandProfile identity",
            ):
                agent_command_profiles.AgentCommandProfileCatalog.from_directories(
                    [root]
                )


if __name__ == "__main__":
    unittest.main()
