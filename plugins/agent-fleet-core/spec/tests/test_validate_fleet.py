from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SPEC_DIR = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = SPEC_DIR / "scripts" / "validate_fleet.py"
EXAMPLE_PATH = SPEC_DIR.parents[2] / "configs" / "fleets" / "release-readiness.yml"

module_spec = importlib.util.spec_from_file_location("validate_fleet", VALIDATOR_PATH)
assert module_spec is not None and module_spec.loader is not None
validator = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(validator)


class FleetValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.valid = validator.load_yaml(EXAMPLE_PATH)

    def errors_for(self, mutate):
        document = copy.deepcopy(self.valid)
        mutate(document)
        return validator.validate_document(document)

    def test_yaml_documents_load(self) -> None:
        self.assertIsInstance(self.valid, dict)
        self.assertIsInstance(
            validator.load_yaml(SPEC_DIR / "config" / "defaults.yml"), dict
        )
        self.assertIsInstance(
            validator.load_yaml(SPEC_DIR / "schema" / "fleet.schema.yml"), dict
        )
        self.assertIsInstance(
            validator.load_yaml(SPEC_DIR / "schema" / "envelopes.schema.yml"), dict
        )

    def test_example_is_valid(self) -> None:
        self.assertEqual([], validator.validate_document(self.valid))

    def test_codex_hook_trust_accepts_only_preapproved_or_review(self) -> None:
        for value in ("preapproved", "review"):
            with self.subTest(value=value):
                errors = self.errors_for(
                    lambda doc, selected=value: doc["spec"]["runtime"].__setitem__(
                        "codex_hook_trust", selected
                    )
                )
                self.assertEqual([], errors)

        errors = self.errors_for(
            lambda doc: doc["spec"]["runtime"].__setitem__(
                "codex_hook_trust", "bypass-everything"
            )
        )
        self.assertIn(
            "spec.runtime.codex_hook_trust: must be 'preapproved' or 'review'",
            errors,
        )

    def test_requires_fleet_and_task_completion_contracts(self) -> None:
        def mutate(doc):
            del doc["spec"]["completion_criteria"]
            del doc["spec"]["stop_conditions"]
            del doc["spec"]["tasks"][0]["expected_output"]
            del doc["spec"]["tasks"][0]["completion_criteria"]

        errors = self.errors_for(mutate)
        self.assertIn("spec.completion_criteria: is required", errors)
        self.assertIn("spec.stop_conditions: is required", errors)
        self.assertIn("spec.tasks[0].expected_output: is required", errors)
        self.assertIn("spec.tasks[0].completion_criteria: is required", errors)

    def test_cli_emits_normalized_json(self) -> None:
        result = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH), str(EXAMPLE_PATH), "--output-json"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        normalized = json.loads(result.stdout)
        self.assertEqual("fleet.harness/v1", normalized["apiVersion"])
        self.assertEqual("Fleet", normalized["kind"])
        self.assertEqual("release-readiness", normalized["metadata"]["id"])
        self.assertEqual("manager-1", normalized["spec"]["members"][0]["agent_ref"])
        self.assertEqual("manager@1", normalized["spec"]["members"][0]["role_ref"])
        self.assertEqual("verify-candidate", normalized["spec"]["tasks"][0]["id"])

    def test_invalid_cli_uses_exit_2_stderr_and_empty_stdout(self) -> None:
        document = copy.deepcopy(self.valid)
        document["spec"]["tasks"][0]["assignee"] = "missing"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet.yml"
            path.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), str(path), "--output-json"],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("unknown agent_ref 'missing'", result.stderr)

    def test_requires_output_json_flag(self) -> None:
        result = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH), str(EXAMPLE_PATH)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("--output-json", result.stderr)

    def test_requires_at_least_one_member(self) -> None:
        errors = self.errors_for(lambda doc: doc["spec"].__setitem__("members", []))
        self.assertIn("spec.members: must contain at least one member", errors)

    def test_rejects_duplicate_agent_ref(self) -> None:
        def mutate(doc):
            doc["spec"]["members"][1]["agent_ref"] = "manager-1"

        self.assertTrue(
            any(
                "duplicate agent_ref 'manager-1'" in error
                for error in self.errors_for(mutate)
            )
        )

    def test_accepts_each_versioned_role_ref_kind(self) -> None:
        for role_ref in (
            "manager@1",
            "advisor@2",
            "worker@10",
            "reviewer@3",
            "researcher@99",
        ):
            with self.subTest(role_ref=role_ref):
                errors = self.errors_for(
                    lambda doc, ref=role_ref: doc["spec"]["members"][1].__setitem__(
                        "role_ref", ref
                    )
                )
                self.assertFalse(
                    any("spec.members[1].role_ref: must match" in error for error in errors)
                )

    def test_rejects_unversioned_or_unknown_role_ref(self) -> None:
        for role_ref in (
            "manager",
            "manager@0",
            "worker@01",
            "custom@1",
            "worker@1.0",
        ):
            with self.subTest(role_ref=role_ref):
                errors = self.errors_for(
                    lambda doc, ref=role_ref: doc["spec"]["members"][1].__setitem__(
                        "role_ref", ref
                    )
                )
                self.assertTrue(
                    any("spec.members[1].role_ref: must match" in error for error in errors)
                )

    def test_rejects_free_text_role_field(self) -> None:
        def mutate(doc):
            member = doc["spec"]["members"][1]
            member["role"] = "Do arbitrary work."
            del member["role_ref"]

        errors = self.errors_for(mutate)
        self.assertIn("spec.members[1].role_ref: is required", errors)
        self.assertIn("spec.members[1].role: is not allowed", errors)

    def test_rejects_duplicate_task_id(self) -> None:
        def mutate(doc):
            doc["spec"]["tasks"][1]["id"] = "verify-candidate"

        self.assertTrue(
            any(
                "duplicate task id 'verify-candidate'" in error
                for error in self.errors_for(mutate)
            )
        )

    def test_rejects_unknown_assignee_and_dependency(self) -> None:
        def mutate(doc):
            doc["spec"]["tasks"][0]["assignee"] = "missing-agent"
            doc["spec"]["tasks"][0]["depends_on"] = ["missing-task"]

        errors = self.errors_for(mutate)
        self.assertIn("spec.tasks[0].assignee: unknown agent_ref 'missing-agent'", errors)
        self.assertIn(
            "spec.tasks[0].depends_on: unknown task id 'missing-task'", errors
        )

    def test_rejects_dependency_cycle_deterministically(self) -> None:
        def mutate(doc):
            doc["spec"]["tasks"][0]["depends_on"] = ["decide-readiness"]

        cycles = [error for error in self.errors_for(mutate) if "dependency cycle" in error]
        self.assertEqual(
            [
                "spec.tasks: dependency cycle "
                "decide-readiness -> review-evidence -> verify-candidate -> "
                "decide-readiness"
            ],
            cycles,
        )

    def test_manager_is_exactly_one_known_member(self) -> None:
        list_errors = self.errors_for(
            lambda doc: doc["spec"]["collaboration"].__setitem__(
                "manager", ["manager-1", "worker-1"]
            )
        )
        self.assertIn("spec.collaboration.manager: must be a non-empty string", list_errors)
        unknown_errors = self.errors_for(
            lambda doc: doc["spec"]["collaboration"].__setitem__("manager", "missing")
        )
        self.assertIn("spec.collaboration.manager: unknown agent_ref 'missing'", unknown_errors)

    def test_manager_member_requires_manager_role_ref(self) -> None:
        errors = self.errors_for(
            lambda doc: doc["spec"]["members"][0].__setitem__("role_ref", "worker@1")
        )
        self.assertIn(
            "spec.collaboration.manager: member 'manager-1' must use a "
            "manager@<version> role_ref",
            errors,
        )

    def test_advisor_member_requires_advisor_role_ref(self) -> None:
        def valid_advisor(doc):
            doc["spec"]["members"][2]["role_ref"] = "advisor@2"
            doc["spec"]["collaboration"]["advisor"] = "worker-2"

        self.assertEqual([], self.errors_for(valid_advisor))

        mismatch_errors = self.errors_for(
            lambda doc: doc["spec"]["collaboration"].__setitem__(
                "advisor", "worker-2"
            )
        )
        self.assertIn(
            "spec.collaboration.advisor: member 'worker-2' must use an "
            "advisor@<version> role_ref",
            mismatch_errors,
        )

    def test_runtime_is_adapter_neutral_but_rejects_unknown_fields(self) -> None:
        def add_state(doc):
            doc["spec"]["runtime"]["status"] = "running"

        self.assertIn("spec.runtime.status: is not allowed", self.errors_for(add_state))
        provider_errors = self.errors_for(
            lambda doc: doc["spec"]["runtime"].__setitem__("provider", "unknown")
        )
        self.assertEqual([], provider_errors)

    def test_headless_core_fleet_does_not_require_runtime_or_view(self) -> None:
        def mutate(doc):
            doc["spec"].pop("runtime")
            doc["spec"].pop("view")

        self.assertEqual([], self.errors_for(mutate))

    def test_rejects_concrete_pane_id(self) -> None:
        errors = self.errors_for(
            lambda doc: doc["spec"]["view"].__setitem__("pane_id", "pane-42")
        )
        self.assertIn("spec.view.pane_id: is not allowed", errors)

    def test_requires_versioned_view_profile_reference(self) -> None:
        def old_layout(doc):
            doc["spec"]["view"] = {"layout": "tiled"}

        errors = self.errors_for(old_layout)
        self.assertIn("spec.view.profile_ref: is required", errors)
        self.assertIn("spec.view.layout: is not allowed", errors)

        for invalid in ("command-deck", "command-deck@0", "../deck@1", "deck@latest"):
            with self.subTest(profile_ref=invalid):
                errors = self.errors_for(
                    lambda doc, ref=invalid: doc["spec"]["view"].__setitem__(
                        "profile_ref", ref
                    )
                )
                self.assertTrue(
                    any("spec.view.profile_ref: must match" in error for error in errors)
                )

    def test_core_accepts_unknown_but_well_formed_view_profile_reference(self) -> None:
        errors = self.errors_for(
            lambda doc: doc["spec"]["view"].__setitem__(
                "profile_ref", "local/team-grid@9"
            )
        )
        self.assertEqual([], errors)

    def test_rejects_role_definition_or_task_state_fields(self) -> None:
        def mutate(doc):
            doc["spec"]["members"][0]["role_definition"] = {"prompt": "..."}
            doc["spec"]["tasks"][0]["status"] = "queued"

        errors = self.errors_for(mutate)
        self.assertIn("spec.members[0].role_definition: is not allowed", errors)
        self.assertIn("spec.tasks[0].status: is not allowed", errors)

    def test_path_like_identifiers_are_rejected(self) -> None:
        mutations = (
            lambda doc: doc["metadata"].__setitem__("id", "../outside"),
            lambda doc: doc["spec"]["members"][0].__setitem__(
                "agent_ref", "manager/../../outside"
            ),
            lambda doc: doc["spec"]["tasks"][0].__setitem__("id", "task/one"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                errors = self.errors_for(mutate)
                self.assertTrue(
                    any("safe identifier" in error for error in errors), errors
                )


if __name__ == "__main__":
    unittest.main()
