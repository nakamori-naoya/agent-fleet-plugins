import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "view_profiles.py"
BUILTIN_DIR = Path(__file__).parents[2] / "view-profiles"
SPEC = importlib.util.spec_from_file_location("view_profiles", MODULE_PATH)
view_profiles = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = view_profiles
SPEC.loader.exec_module(view_profiles)


PROFILE = {
    "apiVersion": "fleet.herdr.harness/v1",
    "kind": "ViewProfile",
    "metadata": {"id": "local/team-grid", "version": 2},
    "spec": {
        "constraints": {"min_members": 2, "max_members": 5},
        "layout": {
            "type": "split",
            "direction": "horizontal",
            "children": [
                {"type": "slot", "selector": "manager", "weight": 40, "pane_slot": "lead"},
                {"type": "stack", "selector": "non-manager", "weight": 60,
                 "direction": "vertical", "distribution": "equal", "pane_slot_prefix": "team"},
            ],
        },
    },
}


class ViewProfileTest(unittest.TestCase):
    def test_builtin_profile_is_valid_and_catalogued(self):
        catalog = view_profiles.ViewProfileCatalog.from_directories([BUILTIN_DIR])
        self.assertEqual(["builtin/command-deck@1"], catalog.identities())

    def test_valid_profile_has_versioned_identity(self):
        self.assertEqual([], view_profiles.validate_document(PROFILE))
        self.assertEqual("local/team-grid@2", view_profiles.profile_identity(PROFILE))

    def test_profile_rejects_reverse_fleet_and_concrete_pane_references(self):
        invalid = json.loads(json.dumps(PROFILE))
        invalid["spec"]["fleet_id"] = "fleet-a"
        invalid["spec"]["layout"]["children"][0]["pane_id"] = "p1"
        errors = view_profiles.validate_document(invalid)
        self.assertTrue(any("fleet_id" in error for error in errors))
        self.assertTrue(any("pane_id" in error for error in errors))

    def test_catalog_resolves_exact_version_and_rejects_duplicate_identity(self):
        catalog = view_profiles.ViewProfileCatalog([PROFILE])
        self.assertEqual(PROFILE, catalog.resolve("local/team-grid@2"))
        with self.assertRaisesRegex(view_profiles.ViewProfileError, "duplicate"):
            view_profiles.ViewProfileCatalog([PROFILE, json.loads(json.dumps(PROFILE))])
        with self.assertRaisesRegex(view_profiles.ViewProfileError, "not found"):
            catalog.resolve("local/team-grid@1")

    def test_catalog_loads_profile_files_deterministically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "z.yml").write_text(json.dumps(PROFILE), encoding="utf-8")
            other = json.loads(json.dumps(PROFILE))
            other["metadata"] = {"id": "local/other", "version": 1}
            (root / "a.yaml").write_text(json.dumps(other), encoding="utf-8")
            catalog = view_profiles.ViewProfileCatalog.from_directories([root])
        self.assertEqual(["local/other@1", "local/team-grid@2"], catalog.identities())


if __name__ == "__main__":
    unittest.main()
