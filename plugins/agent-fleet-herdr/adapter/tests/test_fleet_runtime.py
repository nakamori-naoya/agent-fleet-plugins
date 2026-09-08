import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "fleet_runtime.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("fleet_runtime", MODULE_PATH)
fleet_runtime = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = fleet_runtime
SPEC.loader.exec_module(fleet_runtime)


FLEET = {
    "apiVersion": "fleet.harness/v3", "kind": "Fleet",
    "metadata": {"id": "demo-fleet"},
    "spec": {
        "objective": "Complete the work.",
        "completion_criteria": ["Manager accepts the result."],
        "stop_conditions": ["Unsafe operation is required."],
        "members": [
            {"agent_ref": "manager", "role_ref": "manager@1", "runtime": {
                "product": "claude", "command": "claude", "model": "claude-model",
                "effort": "high", "fallback": "fail"}},
            {"agent_ref": "advisor", "role_ref": "advisor@1", "runtime": {
                "product": "codex", "command": "codex", "model": "codex-model",
                "effort": "medium", "fallback": "fail"}},
        ],
        "tasks": [{"id": "advise", "assignee": "advisor", "depends_on": [],
                   "instructions": "Advise.", "expected_output": "Advice.",
                   "completion_criteria": ["Manager reviews it."]}],
        "collaboration": {"manager": "manager"},
    },
}

PROFILE = {
    "apiVersion": "fleet.herdr.harness/v2", "kind": "ViewProfile",
    "metadata": {"id": "local/test", "version": 1},
    "spec": {"constraints": {"min_members": 2, "max_members": 4},
             "layout": {"type": "split", "direction": "horizontal", "children": [
                 {"type": "stack", "id": "manager", "selector": {"role_ids": ["manager"]},
                  "weight": 40, "direction": "vertical", "distribution": "equal"},
                 {"type": "stack", "id": "support", "selector": {"remaining": True},
                  "weight": 60, "direction": "vertical", "distribution": "equal"},
             ]}},
}


class ConfigRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if "spec.validate" in argv:
            path = Path(argv[argv.index("--config") + 1])
            result = json.loads(path.read_text(encoding="utf-8"))
        elif "provision" in argv:
            fleet = json.loads(argv[argv.index("--fleet-json") + 1])
            profile = json.loads(argv[argv.index("--view-profile-json") + 1])
            result = {"mode": "dry-run", "status": "planned",
                      "fleet_id": fleet["metadata"]["id"],
                      "profile_ref": f'{profile["metadata"]["id"]}@{profile["metadata"]["version"]}',
                      "plan": {"placements": [{"agent_ref": member["agent_ref"]}
                                               for member in fleet["spec"]["members"]]}}
        else:
            result = {"status": "idle"}
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "result": result}), "")


class IsolatedRuntime(fleet_runtime.FleetRuntime):
    def monitor(self, fleet_id, state_dir, *, once, poll_seconds):
        return {"status": "idle", "processed": 0}

    def _execution_bundle_from_manifest(self, manifest, state_dir, fleet_id):
        return self.test_bundle

    def _with_execution_bundle(self, bundle):
        return self


class FleetRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fleets = self.root / "fleets"
        self.profiles = self.root / "profiles"
        self.state = self.root / "state"
        self.fleets.mkdir()
        self.profiles.mkdir()
        self.fleet_path = self.fleets / "demo.yml"
        self.fleet_path.write_text(json.dumps(FLEET), encoding="utf-8")
        self.profile_path = self.profiles / "view.yml"
        self.profile_path.write_text(json.dumps(PROFILE), encoding="utf-8")
        self.runner = ConfigRunner()
        self.runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"],
            runner=self.runner,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_resolve_requires_an_absolute_fleet_path(self):
        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "must be absolute"):
            self.runtime.resolve("demo.yml", [self.fleets], [self.profiles], self.state)

    def test_absolute_fleet_uses_the_default_view_profile(self):
        resolved = self.runtime.resolve(
            str(self.fleet_path.resolve()), [self.fleets], [self.profiles], self.state
        )
        self.assertEqual("demo-fleet", resolved.fleet_id)
        self.assertEqual(fleet_runtime.DEFAULT_VIEW_PROFILE.resolve(), resolved.profile_path)
        self.assertEqual("claude", resolved.fleet["spec"]["members"][0]["runtime"]["command"])

    def test_relative_view_profile_is_resolved_from_the_fleet_directory(self):
        local_profile = self.fleets / "view.yml"
        local_profile.write_text(json.dumps(PROFILE), encoding="utf-8")
        fleet = json.loads(json.dumps(FLEET))
        fleet["spec"]["view_profile"] = "view.yml"
        self.fleet_path.write_text(json.dumps(fleet), encoding="utf-8")
        resolved = self.runtime.resolve(
            str(self.fleet_path.resolve()), [self.fleets], [self.profiles], self.state
        )
        self.assertEqual(local_profile.resolve(), resolved.profile_path)

    def test_missing_view_profile_fails_before_runtime_state_is_created(self):
        fleet = json.loads(json.dumps(FLEET))
        fleet["spec"]["view_profile"] = "missing.yml"
        self.fleet_path.write_text(json.dumps(fleet), encoding="utf-8")
        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "unavailable or unsafe"):
            self.runtime.resolve(
                str(self.fleet_path.resolve()), [self.fleets], [self.profiles], self.state
            )
        self.assertFalse(self.state.exists())

    def test_list_emits_a_stable_absolute_start_command(self):
        rows = self.runtime.list_configs([self.fleets], [self.profiles], self.state)
        self.assertEqual(1, len(rows))
        self.assertIn(str(self.fleet_path.resolve()), rows[0]["start_command"])
        self.assertEqual("claude", rows[0]["member_runtimes"]["manager"]["command"])
        self.assertFalse(self.state.exists())

    def test_plan_passes_fleet_and_view_profile_to_the_adapter(self):
        result = self.runtime.plan(
            str(self.fleet_path.resolve()), [self.fleets], [self.profiles], self.state,
            str(self.root), "codex",
        )
        self.assertEqual("planned", result["status"])
        provision = next(call for call in self.runner.calls if "provision" in call)
        self.assertIn("--fleet-json", provision)
        self.assertIn("--view-profile-json", provision)
        self.assertFalse(self.state.exists())

    def test_init_creates_only_fleet_profile_and_state_directories(self):
        new_fleets = self.root / "new-fleets"
        new_profiles = self.root / "new-profiles"
        new_state = self.root / "new-state"
        result = self.runtime.initialize_user_config([new_fleets], [new_profiles], new_state)
        self.assertEqual({str(new_fleets), str(new_profiles), str(new_state)}, set(result["created"]))

    def test_cli_accepts_an_absolute_fleet_path(self):
        parser = fleet_runtime.build_parser()
        args = parser.parse_args(["plan", str(self.fleet_path.resolve())])
        self.assertEqual(str(self.fleet_path.resolve()), args.fleet)

    def test_each_start_instance_has_a_unique_runtime_identity(self):
        definition = self.runtime.resolve(
            str(self.fleet_path.resolve()), [self.fleets], [self.profiles], self.state
        )
        first = self.runtime._instantiate_run(definition, "demo-fleet-runone")
        second = self.runtime._instantiate_run(definition, "demo-fleet-runtwo")
        self.assertEqual("demo-fleet", first.definition_id)
        self.assertNotEqual(first.fleet_id, second.fleet_id)
        self.assertEqual(first.fleet_id, first.fleet["metadata"]["id"])
        self.assertEqual(second.fleet_id, second.fleet_source["metadata"]["id"])
        self.assertEqual(definition.fleet_source_hash, first.fleet_source_hash)

    def test_two_runs_of_one_definition_have_separate_state_and_registry_rows(self):
        runtime = IsolatedRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"],
            runner=self.runner,
        )
        bundle_root = self.root / "bundle"
        bundle_root.mkdir()
        runtime.test_bundle = fleet_runtime.ExecutionBundle(
            bundle_root, {}, {
                "core": ("fleet-control",), "herdr": ("fleet-herdr",),
                "controller": ("fleet-controller",),
            }
        )
        definition = runtime.resolve(
            str(self.fleet_path.resolve()), [self.fleets], [self.profiles], self.state
        )
        results = []
        for run_id in ("demo-fleet-runone", "demo-fleet-runtwo"):
            run = runtime._instantiate_run(definition, run_id)
            results.append(runtime._start_locked(
                self.state, str(self.root), "codex", {}, b"hook", run,
                {"validated": True}, runtime.test_bundle, once=True,
            ))

        self.assertEqual(["demo-fleet-runone", "demo-fleet-runtwo"],
                         [result["run_id"] for result in results])
        for run_id in ("demo-fleet-runone", "demo-fleet-runtwo"):
            run_root = self.state / "runs" / run_id
            self.assertTrue((run_root / "manifest.json").is_file())
            manifest = json.loads((run_root / "manifest.json").read_text())
            self.assertEqual(run_id, manifest["fleet_id"])
            self.assertEqual("demo-fleet", manifest["definition_id"])
            snapshot = json.loads(Path(manifest["fleet_snapshot_path"]).read_text())
            self.assertEqual(run_id, snapshot["metadata"]["id"])
        rows = runtime.runs(self.state, "demo-fleet")
        self.assertEqual(2, len(rows))
        self.assertEqual({"active"}, {row["phase"] for row in rows})
        core_databases = {
            call[call.index("--db") + 1]
            for call in self.runner.calls if "fleet.provision" in call
        }
        self.assertEqual(2, len(core_databases))

        runtime.stop("demo-fleet-runone", self.state, execute=True)
        self.assertEqual("stopped", runtime.status("demo-fleet-runone", self.state)["status"])
        self.assertEqual("active", runtime.runs(self.state, "demo-fleet")[1]["phase"])

    def test_explicit_run_id_cannot_be_reused(self):
        run_root = self.state / "runs" / "demo-fleet-fixed"
        run_root.mkdir(parents=True)
        (run_root / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "already exists"):
            self.runtime.start(
                str(self.fleet_path.resolve()), [self.fleets], [self.profiles],
                self.state, str(self.root), "codex", execute=True,
                run_id="demo-fleet-fixed",
            )

    def test_cli_addresses_runs_not_definition_ids(self):
        parser = fleet_runtime.build_parser()
        self.assertEqual("demo-fleet-runone", parser.parse_args(
            ["status", "demo-fleet-runone"]
        ).run_id)
        self.assertEqual("demo-fleet", parser.parse_args(
            ["runs", "demo-fleet"]
        ).definition_id)
        self.assertEqual("demo-fleet-runone", parser.parse_args(
            ["resume", "demo-fleet-runone"]
        ).run_id)

    def test_registry_accepts_concurrent_independent_run_updates(self):
        def publish(index):
            run_id = f"demo-fleet-run{index}"
            path = self.state / "runs" / run_id / "manifest.json"
            self.runtime._write_manifest(path, {
                "run_id": run_id, "definition_id": "demo-fleet",
                "fleet_path": str(self.fleet_path), "fleet_source_hash": "hash",
                "runtime_generation": f"generation-{index}",
            }, "active")

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(publish, range(16)))
        rows = self.runtime.runs(self.state, "demo-fleet")
        self.assertEqual(16, len(rows))
        self.assertEqual(16, len({row["run_id"] for row in rows}))



if __name__ == "__main__":
    unittest.main()
