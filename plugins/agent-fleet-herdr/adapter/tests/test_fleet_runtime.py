import importlib.util
import json
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock


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
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, "herdr 0.8.0\n", "")
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

    def test_cli_defaults_only_to_user_configuration_directories(self):
        runtime = mock.Mock()
        runtime.list_configs.return_value = []
        stdout = StringIO()
        with (
            mock.patch.object(fleet_runtime.Path, "home", return_value=self.root),
            mock.patch.object(fleet_runtime, "FleetRuntime", return_value=runtime),
            mock.patch("sys.stdout", stdout),
        ):
            result = fleet_runtime.main(["list"])

        self.assertEqual(0, result)
        fleet_dirs, profile_dirs, _state_dir = runtime.list_configs.call_args.args
        self.assertEqual([self.root / ".config/agent-fleet/fleets"], fleet_dirs)
        self.assertEqual([self.root / ".config/agent-fleet/view-profiles"], profile_dirs)
        self.assertNotIn("plugins", str(profile_dirs))

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
        self.assertEqual(
            str((self.state / "fleets/review/core.sqlite3").resolve()),
            payload["control"]["core_db"],
        )
        self.assertNotIn("context_confirm_argv", payload["control"])
        self.assertEqual("manager", payload["control"]["reporting"]["manager_ref"])
        controller = next(call for call in runner.calls if call[0] == "fleet-controller")
        self.assertIn("--execute", controller)
        self.assertEqual(
            "fleet-control", controller[controller.index("--core-command") + 1]
        )
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

        stopped = runtime.stop("review", self.state, execute=True)
        self.assertEqual("stopped", stopped["status"])
        manifest = json.loads((self.state / "runtimes/review.json").read_text())
        self.assertEqual("stopped", manifest["phase"])
        self.assertTrue(any("deprovision" in call for call in runner.calls))

        first_activation_ids = {
            call[call.index("--command-id") + 1]
            for call in runner.calls
            if "context.sync" in call
        }
        restarted = runtime.start(
            "review",
            [self.fleets],
            [self.profiles],
            self.state,
            str(self.root),
            "codex",
            execute=True,
            once=True,
        )
        self.assertEqual("started", restarted["status"])
        all_activation_ids = {
            call[call.index("--command-id") + 1]
            for call in runner.calls
            if "context.sync" in call
        }
        self.assertTrue(all_activation_ids - first_activation_ids)

        removed = runtime.remove("review", self.state, execute=True)
        self.assertEqual("removed", removed["status"])
        self.assertFalse((self.state / "runtimes/review.json").exists())

    def test_init_and_doctor_keep_configuration_outside_the_plugin(self):
        runtime = fleet_runtime.FleetRuntime(
            [str(Path(__file__))], ["fleet-herdr"], ["fleet-controller"], runner=FakeRunner()
        )
        fleets = self.root / "user-config/fleets"
        profiles = self.root / "user-config/view-profiles"
        state = self.root / "user-state"
        initialized = runtime.initialize_user_config([fleets], [profiles], state)
        self.assertEqual("initialized", initialized["status"])
        self.assertTrue(fleets.is_dir())
        self.assertTrue(profiles.is_dir())
        diagnosis = runtime.doctor([fleets], [profiles], state)
        self.assertIn(diagnosis["status"], {"healthy", "issues"})
        self.assertNotIn("plugins/agent-fleet", " ".join(initialized["created"]))

    def test_doctor_rejects_incompatible_herdr_version(self):
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(
                ["herdr", "--version"], 0, "herdr 0.9.0\n", ""
            )
        )
        runtime = fleet_runtime.FleetRuntime(
            [str(Path(__file__))], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        with mock.patch.object(fleet_runtime.shutil, "which", return_value="/bin/herdr"):
            diagnosis = runtime.doctor([self.fleets], [self.profiles], self.state)

        herdr = next(check for check in diagnosis["checks"] if check["check"] == "herdr")
        self.assertFalse(herdr["ok"])
        self.assertEqual("Herdr 0.8.x is required", herdr["reason"])


if __name__ == "__main__":
    unittest.main()
