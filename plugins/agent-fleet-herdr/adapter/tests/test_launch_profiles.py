from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "launch_profiles.py"
SPEC = importlib.util.spec_from_file_location("launch_profiles", MODULE_PATH)
launch_profiles = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(launch_profiles)


PROFILE = {
    "apiVersion": "fleet.herdr.harness/v1",
    "kind": "LaunchProfile",
    "metadata": {"id": "development-three-columns"},
    "spec": {
        "fleet_ref": "development-squad",
        "view_profile_ref": "local/role-columns@1",
        "codex_hook_trust": "preapproved",
    },
}


class LaunchProfileTest(unittest.TestCase):
    def test_valid_profile_composes_portable_fleet_and_herdr_view(self):
        self.assertEqual([], launch_profiles.validate_document(PROFILE))
        self.assertEqual(
            "development-three-columns",
            launch_profiles.profile_identity(PROFILE),
        )

    def test_profile_rejects_backend_or_pane_fields_inside_fleet_reference(self):
        invalid = json.loads(json.dumps(PROFILE))
        invalid["spec"]["provider"] = "herdr"
        invalid["spec"]["pane_id"] = "pane-42"

        errors = launch_profiles.validate_document(invalid)

        self.assertIn("$.spec.pane_id: is not allowed", errors)
        self.assertIn("$.spec.provider: is not allowed", errors)

    def test_profile_requires_exact_fleet_and_versioned_view_references(self):
        invalid = json.loads(json.dumps(PROFILE))
        invalid["spec"]["fleet_ref"] = "../fleet"
        invalid["spec"]["view_profile_ref"] = "local/role-columns@latest"

        errors = launch_profiles.validate_document(invalid)

        self.assertTrue(any("fleet_ref" in error for error in errors))
        self.assertTrue(any("view_profile_ref" in error for error in errors))

    def test_profile_rejects_ambiguous_view_reference_segments(self):
        invalid = json.loads(json.dumps(PROFILE))
        invalid["spec"]["view_profile_ref"] = "local/role-@1"

        errors = launch_profiles.validate_document(invalid)

        self.assertTrue(any("view_profile_ref" in error for error in errors))

    def test_catalog_rejects_duplicate_launch_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.yml").write_text(json.dumps(PROFILE), encoding="utf-8")
            (root / "two.yml").write_text(json.dumps(PROFILE), encoding="utf-8")

            with self.assertRaisesRegex(
                launch_profiles.LaunchProfileError,
                "duplicate LaunchProfile identity",
            ):
                launch_profiles.LaunchProfileCatalog.from_directories([root])


if __name__ == "__main__":
    unittest.main()
