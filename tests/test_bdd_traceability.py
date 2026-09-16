import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = ROOT / "docs" / "2026-08-31-エージェント艦隊-連携と全体制御.md"
RUNTIME_TESTS = ROOT / "plugins" / "agent-fleet-herdr" / "adapter" / "tests" / "test_fleet_runtime.py"
ADAPTER_TESTS = ROOT / "plugins" / "agent-fleet-herdr" / "adapter" / "tests" / "test_herdr_adapter.py"


class DocumentedBehaviorTraceabilityTest(unittest.TestCase):
    def test_absolute_path_entrypoint_has_executable_evidence(self):
        document = ARCHITECTURE.read_text(encoding="utf-8")
        tests = RUNTIME_TESTS.read_text(encoding="utf-8")
        self.assertIn("絶対パスで指定", document)
        self.assertIn("def test_resolve_requires_an_absolute_fleet_path(", tests)
        self.assertIn("def test_cli_accepts_an_absolute_fleet_path(", tests)

    def test_default_and_custom_view_profiles_have_executable_evidence(self):
        document = ARCHITECTURE.read_text(encoding="utf-8")
        tests = RUNTIME_TESTS.read_text(encoding="utf-8")
        self.assertIn("省略時はplugin内の既定ViewProfile", document)
        self.assertIn("絶対パスだけを受理", document)
        self.assertIn("def test_absolute_fleet_uses_the_default_view_profile(", tests)
        self.assertIn("def test_relative_view_profile_is_rejected(", tests)

    def test_member_runtime_configuration_has_executable_evidence(self):
        document = ARCHITECTURE.read_text(encoding="utf-8")
        tests = ADAPTER_TESTS.read_text(encoding="utf-8")
        for field in ("product", "command", "model", "effort", "fallback"):
            self.assertIn(f"`{field}`", document)
        self.assertIn("def test_plan_uses_each_members_command_model_and_effort(", tests)
        self.assertIn("def test_missing_member_runtime_is_rejected(", tests)

    def test_runtime_layout_validation_has_executable_evidence(self):
        document = ARCHITECTURE.read_text(encoding="utf-8")
        tests = ADAPTER_TESTS.read_text(encoding="utf-8")
        self.assertIn("Fleet member数とpane数が一致", document)
        self.assertIn("各paneのx、y、width、height", document)
        self.assertIn("def test_execute_rejects_pane_count_mismatch_and_closes_workspace(", tests)
        self.assertIn("def test_execute_rejects_width_height_or_position_mismatch(", tests)


if __name__ == "__main__":
    unittest.main()
