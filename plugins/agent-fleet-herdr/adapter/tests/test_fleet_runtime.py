import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "fleet_runtime.py"
SPEC = importlib.util.spec_from_file_location("fleet_runtime", MODULE_PATH)
fleet_runtime = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(fleet_runtime)


FLEET = {
    "apiVersion": "fleet.harness/v1",
    "kind": "Fleet",
    "metadata": {"id": "review"},
    "spec": {
        "objective": "Review the change",
        "completion_criteria": ["Manager accepted the result"],
        "stop_conditions": ["Unsafe change is required"],
        "members": [
            {"agent_ref": "manager", "role_ref": "manager@1"},
            {"agent_ref": "worker", "role_ref": "worker@1"},
        ],
        "tasks": [
            {
                "id": "inspect",
                "assignee": "worker",
                "depends_on": [],
                "instructions": "Inspect it",
                "expected_output": "Review report",
                "completion_criteria": ["Evidence included"],
            }
        ],
        "collaboration": {
            "manager": "manager",
            "reporting": {"strategy": "manager", "include_task_updates": True},
        },
        "runtime": {"provider": "herdr"},
        "view": {"profile_ref": "local/review-grid@1"},
    },
}

PROFILE = {
    "apiVersion": "fleet.herdr.harness/v1",
    "kind": "ViewProfile",
    "metadata": {"id": "local/review-grid", "version": 1},
    "spec": {
        "constraints": {"min_members": 2, "max_members": 5},
        "layout": {
            "type": "split",
            "direction": "horizontal",
            "children": [
                {"type": "slot", "selector": "manager", "weight": 40},
                {
                    "type": "stack",
                    "selector": "non-manager",
                    "weight": 60,
                    "direction": "vertical",
                    "distribution": "equal",
                },
            ],
        },
    },
}


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if "spec.validate" in argv:
            payload = {"ok": True, "result": FLEET}
        elif "provision" in argv:
            payload = {"ok": True, "result": {"mode": "dry-run", "status": "planned"}}
        elif "status" in argv:
            payload = {"ok": True, "result": {"fleet_id": "review"}}
        else:
            payload = {"ok": True, "result": {"status": "ok"}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


class FleetRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fleets = self.root / "fleets"
        self.profiles = self.root / "profiles"
        self.state = self.root / "state"
        self.fleets.mkdir()
        self.profiles.mkdir()
        (self.fleets / "review.yml").write_text(json.dumps(FLEET), encoding="utf-8")
        (self.profiles / "review-grid.yml").write_text(
            json.dumps(PROFILE), encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_resolves_fleet_to_versioned_profile(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        resolved = runtime.resolve(
            "review", [self.fleets], [self.profiles], self.state
        )
        self.assertEqual("review", resolved.fleet_id)
        self.assertEqual("local/review-grid@1", resolved.profile_ref)
        self.assertEqual((self.profiles / "review-grid.yml").resolve(), resolved.profile_path)

    def test_duplicate_profile_identity_is_rejected(self):
        (self.profiles / "duplicate.yml").write_text(
            json.dumps(PROFILE), encoding="utf-8"
        )
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=FakeRunner(),
        )
        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "duplicate"):
            runtime.resolve("review", [self.fleets], [self.profiles], self.state)

    def test_plan_is_read_only_and_passes_both_documents_to_adapter(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        result = runtime.plan(
            "review",
            [self.fleets],
            [self.profiles],
            self.state,
            str(self.root),
            "codex",
        )
        self.assertEqual("planned", result["status"])
        provision = next(call for call in runner.calls if "provision" in call)
        self.assertIn("--fleet-json", provision)
        self.assertIn("--view-profile-json", provision)
        self.assertFalse(self.state.exists())

    def test_each_fleet_file_has_a_stable_start_command(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        result = runtime.list_configs([self.fleets], [self.profiles], self.state)
        self.assertEqual("fleet-runtime start review --execute", result[0]["start_command"])

    def test_start_provisions_context_and_tasks_then_runs_paneless_controller(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        result = runtime.start(
            "review",
            [self.fleets],
            [self.profiles],
            self.state,
            str(self.root),
            "codex",
            execute=True,
            once=True,
        )
        self.assertEqual("started", result["status"])
        self.assertEqual(2, sum("context.sync" in call for call in runner.calls))
        self.assertEqual(1, sum("task.assign" in call for call in runner.calls))
        activation = next(call for call in runner.calls if "context.sync" in call)
        payload = json.loads(activation[activation.index("--payload") + 1])
        self.assertEqual(str(self.state / "core.sqlite3"), payload["control"]["core_db"])
        self.assertEqual("manager", payload["control"]["reporting"]["manager_ref"])
        controller = next(call for call in runner.calls if call[0] == "fleet-controller")
        self.assertIn("--execute", controller)
        self.assertTrue((self.state / "runtimes/review.json").is_file())

        repeat = runtime.start(
            "review",
            [self.fleets],
            [self.profiles],
            self.state,
            str(self.root),
            "codex",
            execute=True,
            once=True,
        )
        self.assertEqual("resumed", repeat["status"])


if __name__ == "__main__":
    unittest.main()
