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
CATALOG_PATH = SPEC_DIR.parents[2] / "tests" / "fixtures" / "role-catalog.yml"

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

    def test_member_runtime_requires_supported_product_model_and_effort(self) -> None:
        for product, model in (
            ("codex", "gpt-5.6-sol"),
            ("claude", "claude-fable-5-1"),
        ):
            with self.subTest(product=product):
                errors = self.errors_for(
                    lambda doc, selected_product=product, selected_model=model: doc["spec"][
                        "members"
                    ][0].__setitem__(
                        "runtime",
                        {
                            "product": selected_product,
                            "model": selected_model,
                            "effort": "medium",
                            "fallback": "fail",
                        },
                    )
                )
                self.assertEqual([], errors)

        errors = self.errors_for(
            lambda doc: doc["spec"]["members"][0]["runtime"].__setitem__(
                "product", "fable"
            )
        )
        self.assertIn(
            "spec.members[0].runtime.product: must be 'codex' or 'claude'",
            errors,
        )

        errors = self.errors_for(
            lambda doc: doc["spec"]["members"][0]["runtime"].__setitem__(
                "effort", "ancient"
            )
        )
        self.assertIn(
            "spec.members[0].runtime.effort: must be low, medium, high, xhigh, or max",
            errors,
        )

        errors = self.errors_for(
            lambda doc: doc["spec"]["members"][0]["runtime"].__setitem__(
                "fallback", "silent-old-model"
            )
        )
        self.assertIn(
            "spec.members[0].runtime.fallback: must be 'fail' or 'product-default'",
            errors,
        )

    def test_member_runtime_is_required_and_legacy_top_level_model_is_rejected(self) -> None:
        errors = self.errors_for(
            lambda doc: doc["spec"]["members"][0].pop("runtime")
        )
        self.assertIn("spec.members[0].runtime: is required", errors)

        def use_legacy_model(doc):
            member = doc["spec"]["members"][0]
            member["model"] = member.pop("runtime")["model"]

        errors = self.errors_for(use_legacy_model)
        self.assertIn("spec.members[0].model: is not allowed", errors)

    def test_fleet_rejects_herdr_runtime_and_view_configuration(self) -> None:
        def add_adapter_configuration(doc):
            doc["spec"]["runtime"] = {
                "provider": "herdr",
                "codex_hook_trust": "preapproved",
            }
            doc["spec"]["view"] = {"profile_ref": "local/review-grid@1"}

        errors = self.errors_for(add_adapter_configuration)

        self.assertIn("spec.runtime: is not allowed", errors)
        self.assertIn("spec.view: is not allowed", errors)

    def test_legacy_v1_accepts_adapter_fields_during_migration(self) -> None:
        def use_legacy_contract(doc):
            doc["apiVersion"] = "fleet.harness/v1"
            doc["spec"]["runtime"] = {
                "provider": "herdr",
                "codex_hook_trust": "preapproved",
            }
            doc["spec"]["view"] = {"profile_ref": "local/review-grid@1"}

        self.assertEqual([], self.errors_for(use_legacy_contract))

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
        document = copy.deepcopy(self.valid)
        document["spec"]["members"][0]["role_ref"] = "coordinator@1"
        for member in document["spec"]["members"][1:]:
            member["role_ref"] = "builder@1"
        with tempfile.TemporaryDirectory() as directory:
            fleet_path = Path(directory) / "fleet.yml"
            fleet_path.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    str(fleet_path),
                    "--role-catalog",
                    str(CATALOG_PATH),
                    "--output-json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        normalized = json.loads(result.stdout)
        self.assertEqual("fleet.harness/v2", normalized["apiVersion"])
        self.assertEqual("Fleet", normalized["kind"])
        self.assertEqual("release-readiness", normalized["metadata"]["id"])
        self.assertEqual("manager-1", normalized["spec"]["members"][0]["agent_ref"])
        manager = normalized["spec"]["members"][0]
        self.assertEqual("coordinator@1", manager["role_ref"])
        self.assertEqual(
            "目的と完了条件を保持して最終判断を行う",
            manager["role_definition"]["mission"],
        )
        self.assertEqual("test@1", normalized["resolved_role_catalog"]["ref"])
        self.assertEqual("verify-candidate", normalized["spec"]["tasks"][0]["id"])

    def test_invalid_cli_uses_exit_2_stderr_and_empty_stdout(self) -> None:
        document = copy.deepcopy(self.valid)
        document["spec"]["tasks"][0]["assignee"] = "missing"
        document["spec"]["members"][0]["role_ref"] = "coordinator@1"
        for member in document["spec"]["members"][1:]:
            member["role_ref"] = "builder@1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet.yml"
            path.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    str(path),
                    "--role-catalog",
                    str(CATALOG_PATH),
                    "--output-json",
                ],
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

    def test_accepts_any_safe_versioned_role_ref_shape(self) -> None:
        for role_ref in (
            "manager@1",
            "builder@2",
            "security-reviewer@10",
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

    def test_rejects_malformed_role_ref(self) -> None:
        for role_ref in (
            "manager",
            "manager@0",
            "worker@01",
            "Custom@1",
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

    def test_catalog_rejects_missing_role_ref(self) -> None:
        document = copy.deepcopy(self.valid)
        document["spec"]["members"][0]["role_ref"] = "coordinator@1"
        document["spec"]["members"][1]["role_ref"] = "missing@1"
        errors = validator.resolve_role_definitions(
            document, validator.load_yaml(CATALOG_PATH)
        )[1]
        self.assertIn(
            "spec.members[1].role_ref: 'missing@1' does not exist in Role Catalog test@1",
            errors,
        )

    def test_manager_is_selected_by_authority_not_role_name(self) -> None:
        document = copy.deepcopy(self.valid)
        document["spec"]["members"][0]["role_ref"] = "coordinator@1"
        for member in document["spec"]["members"][1:]:
            member["role_ref"] = "builder@1"
        normalized, errors = validator.resolve_role_definitions(
            document, validator.load_yaml(CATALOG_PATH)
        )
        self.assertEqual([], errors)
        self.assertEqual(
            ["assign", "accept", "reject"],
            normalized["spec"]["members"][0]["role_definition"]["authority"],
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

    def test_fleet_does_not_infer_responsibility_from_role_name(self) -> None:
        def use_catalog_owned_names(doc):
            doc["spec"]["members"][0]["role_ref"] = "coordinator@1"
            doc["spec"]["members"][2]["role_ref"] = "consultant@1"
            doc["spec"]["collaboration"]["advisor"] = "worker-2"

        self.assertEqual([], self.errors_for(use_catalog_owned_names))

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
