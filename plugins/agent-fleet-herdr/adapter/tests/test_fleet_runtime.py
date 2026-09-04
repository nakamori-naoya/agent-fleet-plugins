import importlib.util
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "fleet_runtime.py"
ROLE_CONTEXT = Path(__file__).parents[2] / "hooks" / "role_context.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("fleet_runtime", MODULE_PATH)
fleet_runtime = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(fleet_runtime)


FLEET = {
    "apiVersion": "fleet.harness/v2",
    "kind": "Fleet",
    "metadata": {"id": "review"},
    "spec": {
        "objective": "Review the change",
        "completion_criteria": ["Manager accepted the result"],
        "stop_conditions": ["Unsafe change is required"],
        "members": [
            {
                "agent_ref": "manager",
                "role_ref": "manager@1",
                "runtime": {
                    "product": "claude",
                    "model": "claude-fable-5-1",
                    "effort": "high",
                    "fallback": "fail",
                },
            },
            {
                "agent_ref": "worker",
                "role_ref": "worker@1",
                "runtime": {
                    "product": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "medium",
                    "fallback": "fail",
                },
            },
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
    },
}

LAUNCH_PROFILE = {
    "apiVersion": "fleet.herdr.harness/v1",
    "kind": "LaunchProfile",
    "metadata": {"id": "review"},
    "spec": {
        "fleet_ref": "review",
        "view_profile_ref": "local/review-grid@1",
        "codex_hook_trust": "preapproved",
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
    def test_monitor_backs_off_only_while_idle_and_resets_after_delivery(self):
        manifest = self.state / "runtimes" / "review.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps({"phase": "running", "fleet_id": "review"}),
            encoding="utf-8",
        )
        delays = []

        def sleeper(delay):
            delays.append(delay)
            if len(delays) == 3:
                manifest.write_text(json.dumps({"phase": "stopping"}), encoding="utf-8")

        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], sleeper=sleeper
        )
        with mock.patch.object(
            runtime,
            "_run_json",
            side_effect=[
                {"status": "idle"},
                {"status": "idle"},
                {"status": "delivered"},
                {"status": "idle"},
            ],
        ):
            result = runtime.monitor("review", self.state, once=False, poll_seconds=0.25)

        self.assertEqual([0.25, 0.5, 0.25], delays)
        self.assertEqual(1, result["processed"])

    def test_wrapper_style_module_path_still_resolves_hook_source(self):
        wrapper_path = Path(__file__).parents[1] / "scripts" / ".." / "fleet_runtime.py"
        wrapper_spec = importlib.util.spec_from_file_location(
            "fleet_runtime_from_wrapper_path", str(wrapper_path)
        )
        wrapper_module = importlib.util.module_from_spec(wrapper_spec)
        assert wrapper_spec.loader
        wrapper_spec.loader.exec_module(wrapper_module)

        self.assertEqual(ROLE_CONTEXT.resolve(), wrapper_module.DEFAULT_HOOK_SOURCE)
        self.assertTrue(wrapper_module.DEFAULT_HOOK_SOURCE.is_file())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fleets = self.root / "fleets"
        self.profiles = self.root / "profiles"
        self.launches = self.root / "herdr-launch-profiles"
        self.state = self.root / "state"
        self.role_catalog = self.root / "role-catalog.yml"
        self.fleets.mkdir()
        self.profiles.mkdir()
        self.launches.mkdir()
        self.role_catalog.write_text("fixture: true\n", encoding="utf-8")
        (self.fleets / "review.yml").write_text(json.dumps(FLEET), encoding="utf-8")
        (self.profiles / "review-grid.yml").write_text(
            json.dumps(PROFILE), encoding="utf-8"
        )
        (self.launches / "review.yml").write_text(
            json.dumps(LAUNCH_PROFILE), encoding="utf-8"
        )
        self.commands = {}
        for name, implementation, tree in (
            ("fleet-control", "fleet_control.py", "core"),
            ("fleet-herdr", "herdr_adapter.py", "adapter"),
            ("fleet-controller", "fleet_controller.py", "adapter"),
        ):
            command = self.root / tree / "scripts" / name
            command.parent.mkdir(parents=True, exist_ok=True)
            command.write_text("#!/bin/sh\n", encoding="utf-8")
            command.chmod(0o700)
            (command.parent.parent / implementation).write_text(
                f"# {name} fixture\n", encoding="utf-8"
            )
            self.commands[name] = command
        (self.root / "adapter" / "view_profiles.py").write_text("# view fixture\n", encoding="utf-8")
        (self.root / "adapter" / "fleet_runtime.py").write_text("# runtime fixture\n", encoding="utf-8")
        original_which = fleet_runtime.shutil.which
        self.which = mock.patch.object(
            fleet_runtime.shutil,
            "which",
            side_effect=lambda name: str(self.commands[name])
            if name in {"fleet-control", "fleet-herdr", "fleet-controller"}
            else original_which(name),
        )
        self.which.start()

    def tearDown(self):
        self.which.stop()
        self.temp.cleanup()

    def test_launch_profile_composes_fleet_and_versioned_view_profile(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=runner,
            role_catalog=self.role_catalog,
        )
        resolved = runtime.resolve(
            "review", [self.fleets], [self.profiles], self.state
        )
        self.assertEqual("review", resolved.fleet_id)
        self.assertEqual("review", resolved.launch_id)
        self.assertEqual(
            (self.launches / "review.yml").resolve(), resolved.launch_path
        )
        self.assertFalse(resolved.legacy)
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

    def test_portable_fleet_without_launch_profile_is_not_startable(self):
        (self.launches / "review.yml").unlink()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=FakeRunner(),
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "LaunchProfile not found"
        ):
            runtime.resolve("review", [self.fleets], [self.profiles], self.state)

        self.assertFalse(self.state.exists())

    def test_legacy_fleet_requires_explicit_compatibility_switch(self):
        legacy = json.loads(json.dumps(FLEET))
        legacy["apiVersion"] = "fleet.harness/v1"
        legacy["spec"]["runtime"] = {
            "provider": "herdr",
            "codex_hook_trust": "review",
        }
        legacy["spec"]["view"] = {"profile_ref": "local/review-grid@1"}
        (self.fleets / "review.yml").write_text(json.dumps(legacy), encoding="utf-8")
        (self.launches / "review.yml").unlink()

        class ConfigAwareRunner(FakeRunner):
            def __call__(self, argv, **kwargs):
                if "spec.validate" in argv:
                    self.calls.append(list(argv))
                    config = Path(argv[argv.index("--config") + 1])
                    payload = {
                        "ok": True,
                        "result": json.loads(config.read_text(encoding="utf-8")),
                    }
                    return subprocess.CompletedProcess(
                        argv, 0, json.dumps(payload), ""
                    )
                return super().__call__(argv, **kwargs)

        strict = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"],
            runner=ConfigAwareRunner(),
        )
        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "LaunchProfile not found"
        ):
            strict.resolve("review", [self.fleets], [self.profiles], self.state)

        compatible = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"],
            runner=ConfigAwareRunner(), allow_legacy_fleet=True,
        )
        resolved = compatible.resolve(
            "review", [self.fleets], [self.profiles], self.state
        )
        self.assertTrue(resolved.legacy)
        self.assertEqual("local/review-grid@1", resolved.profile_ref)

    def test_plan_is_read_only_and_passes_all_three_documents_to_adapter(self):
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
        self.assertIn("--launch-profile-json", provision)
        self.assertIn("--view-profile-json", provision)
        self.assertIn("--agent-core-command", provision)
        self.assertIn("--agent-core-db", provision)
        self.assertIn("--agent-hook-runtime", provision)
        self.assertEqual(
            str((self.state / "fleets/review/herdr.sqlite3").resolve()),
            provision[provision.index("--state-db") + 1],
        )
        self.assertFalse(self.state.exists())

    def test_each_fleet_file_has_a_stable_start_command(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=runner,
            role_catalog=self.role_catalog,
        )
        result = runtime.list_configs([self.fleets], [self.profiles], self.state)
        self.assertEqual(
            f"fleet-runtime start review --role-catalog {self.role_catalog} --execute",
            result[0]["start_command"],
        )

    def test_cli_defaults_only_to_user_configuration_directories(self):
        runtime = mock.Mock()
        runtime.list_configs.return_value = []
        stdout = StringIO()
        with (
            mock.patch.object(fleet_runtime.Path, "home", return_value=self.root),
            mock.patch.object(fleet_runtime, "FleetRuntime", return_value=runtime),
            mock.patch("sys.stdout", stdout),
        ):
            result = fleet_runtime.main(
                ["list", "--role-catalog", str(self.role_catalog)]
            )

        self.assertEqual(0, result)
        fleet_dirs, profile_dirs, _state_dir = runtime.list_configs.call_args.args
        self.assertEqual([self.root / ".config/agent-fleet/fleets"], fleet_dirs)
        self.assertEqual([self.root / ".config/agent-fleet/view-profiles"], profile_dirs)
        self.assertNotIn("plugins", str(profile_dirs))

    def test_runtime_passes_immutable_role_catalog_snapshot_to_core(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=runner,
            role_catalog=self.role_catalog,
        )
        runtime.resolve("review", [self.fleets], [self.profiles], self.state)
        validation = next(call for call in runner.calls if "spec.validate" in call)
        catalog_snapshot = Path(
            validation[validation.index("--role-catalog") + 1]
        )
        self.assertNotEqual(self.role_catalog.resolve(), catalog_snapshot.resolve())
        self.assertEqual("role-catalog.json", catalog_snapshot.name)
        self.assertTrue(catalog_snapshot.parent.name.startswith("agent-fleet-validation-"))
        self.assertFalse(catalog_snapshot.exists())

    def test_status_detects_role_catalog_drift(self):
        manifest = self.state / "runtimes" / "review.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps(
                {
                    "phase": "active",
                    "launch_id": "review",
                    "launch_path": str(self.launches / "review.yml"),
                    "launch_hash": fleet_runtime._content_hash(LAUNCH_PROFILE),
                    "fleet_id": "review",
                    "fleet_path": str(self.fleets / "review.yml"),
                    "fleet_source_hash": fleet_runtime._content_hash(FLEET),
                    "profile_path": str(self.profiles / "review-grid.yml"),
                    "profile_hash": fleet_runtime._content_hash(PROFILE),
                    "role_catalog_path": str(self.role_catalog),
                    "role_catalog_hash": fleet_runtime._content_hash(
                        {"fixture": True}
                    ),
                }
            ),
            encoding="utf-8",
        )
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=FakeRunner(),
            role_catalog=self.role_catalog,
        )

        self.assertEqual("active", runtime.status("review", self.state)["status"])
        self.role_catalog.write_text("fixture: changed\n", encoding="utf-8")
        self.assertEqual(
            "configuration_drift", runtime.status("review", self.state)["status"]
        )

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
        self.assertEqual("task.list", payload["control"]["monitoring"]["action"])
        self.assertEqual(
            ["sqlite-direct", "external-json-filter"],
            payload["control"]["monitoring"]["prohibited_methods"],
        )
        worker_activation = next(
            call
            for call in runner.calls
            if "context.sync" in call
            and json.loads(call[call.index("--payload") + 1])["control"][
                "reporting"
            ]["required_identity"]
            == "worker"
        )
        worker_payload = json.loads(
            worker_activation[worker_activation.index("--payload") + 1]
        )
        self.assertNotIn("monitoring", worker_payload["control"])
        controller = next(call for call in runner.calls if call[0] == "fleet-controller")
        self.assertIn("--execute", controller)
        self.assertEqual(
            "fleet-control", controller[controller.index("--core-command") + 1]
        )
        self.assertTrue((self.state / "runtimes/review.json").is_file())
        provision = next(
            call for call in runner.calls
            if "provision" in call and "--execute" in call
        )
        hook_runtime = Path(
            provision[provision.index("--agent-hook-runtime") + 1]
        )
        self.assertEqual(ROLE_CONTEXT.read_bytes(), hook_runtime.read_bytes())
        self.assertEqual(0o600, hook_runtime.stat().st_mode & 0o777)
        self.assertTrue(
            hook_runtime.is_relative_to(
                (self.state / "fleets/review/hook-runtimes").resolve()
            )
        )
        provision_calls = [call for call in runner.calls if "provision" in call]
        planned_call = next(call for call in provision_calls if "--execute" not in call)
        executed_call = next(call for call in provision_calls if "--execute" in call)
        for option in (
            "--state-db",
            "--agent-core-command",
            "--agent-core-db",
            "--agent-hook-runtime",
        ):
            self.assertEqual(
                planned_call[planned_call.index(option) + 1],
                executed_call[executed_call.index(option) + 1],
            )

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
        core_provisions_before_restart = sum(
            "fleet.provision" in call for call in runner.calls
        )
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
        self.assertEqual(
            core_provisions_before_restart + 1,
            sum("fleet.provision" in call for call in runner.calls),
        )
        all_activation_ids = {
            call[call.index("--command-id") + 1]
            for call in runner.calls
            if "context.sync" in call
        }
        self.assertTrue(all_activation_ids - first_activation_ids)

        removed = runtime.remove("review", self.state, execute=True)
        self.assertEqual("removed", removed["status"])
        self.assertFalse((self.state / "runtimes/review.json").exists())

    def test_start_and_stop_use_the_same_fleet_lock_identity(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )

        with (
            mock.patch.object(
                runtime,
                "_fleet_lock_path",
                wraps=runtime._fleet_lock_path,
            ) as fleet_lock_path,
            mock.patch.object(
                runtime,
                "_launch_lock_path",
                wraps=runtime._launch_lock_path,
            ) as launch_lock_path,
        ):
            runtime.start(
                "review",
                [self.fleets],
                [self.profiles],
                self.state,
                str(self.root),
                "codex",
                execute=True,
                once=True,
            )
            runtime.stop("review", self.state, execute=True)

        self.assertEqual(
            [mock.call(self.state, "review"), mock.call(self.state, "review")],
            fleet_lock_path.call_args_list,
        )
        self.assertEqual(
            [mock.call(self.state, "review"), mock.call(self.state, "review")],
            launch_lock_path.call_args_list,
        )
        self.assertFalse((self.state / "locks").exists())

    def test_same_launch_is_locked_even_when_its_fleet_reference_changes(self):
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=FakeRunner()
        )

        with runtime._hold_runtime_locks(
            self.state,
            "review",
            "fleet-a",
            timeout_seconds=0,
            timeout_message="first lock failed",
        ):
            with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "launch busy"):
                with runtime._hold_runtime_locks(
                    self.state,
                    "review",
                    "fleet-b",
                    timeout_seconds=0,
                    timeout_message="launch busy",
                ):
                    self.fail("same launch identity was locked twice")

        self.assertFalse(self.state.exists())

    def test_stop_during_core_provision_is_not_overwritten_by_start(self):
        core_provision_started = threading.Event()
        release_core_provision = threading.Event()

        class BlockingRunner(FakeRunner):
            def __call__(self, argv, **kwargs):
                if "fleet.provision" in argv:
                    core_provision_started.set()
                    if not release_core_provision.wait(timeout=3):
                        raise AssertionError("test did not release Core provision")
                return super().__call__(argv, **kwargs)

        runner = BlockingRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            starting = executor.submit(
                runtime.start,
                "review",
                [self.fleets],
                [self.profiles],
                self.state,
                str(self.root),
                "codex",
                execute=True,
                once=True,
            )
            self.assertTrue(core_provision_started.wait(timeout=3))
            stopping = executor.submit(
                runtime.stop, "review", self.state, execute=True
            )
            request_dir = self.state / "stop-requests/review"
            for _ in range(100):
                if request_dir.is_dir() and any(request_dir.glob("*.request")):
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("stop request was not published")
            release_core_provision.set()

            with self.assertRaisesRegex(
                fleet_runtime.FleetRuntimeError,
                "cancelled by a stop request",
            ):
                starting.result(timeout=3)
            stopped = stopping.result(timeout=3)

        self.assertEqual("stopped", stopped["status"])
        manifest = json.loads(
            (self.state / "runtimes/review.json").read_text(encoding="utf-8")
        )
        self.assertEqual("stopped", manifest["phase"])
        self.assertFalse(request_dir.exists())

    def test_live_stop_request_is_observed_and_crashed_request_is_removed(self):
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=FakeRunner()
        )
        request_dir = self.state / "stop-requests/review"

        with runtime._publish_stop_request(self.state, "review") as request_path:
            self.assertTrue(request_path.exists())
            self.assertTrue(runtime._stop_requested(self.state, "review"))

        self.assertFalse(runtime._stop_requested(self.state, "review"))
        request_dir.mkdir(parents=True)
        stale_request = request_dir / "crashed.request"
        stale_request.write_text("stop\n", encoding="utf-8")

        self.assertFalse(runtime._stop_requested(self.state, "review"))
        self.assertFalse(stale_request.exists())
        self.assertFalse(request_dir.exists())

    def test_remove_holds_fleet_lock_through_stop_and_core_removal(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review",
            [self.fleets],
            [self.profiles],
            self.state,
            str(self.root),
            "codex",
            execute=True,
            once=True,
        )
        original_hold = runtime._hold_runtime_locks
        original_run = runtime._run_json
        lock_held = False

        @contextmanager
        def recording_lock(*args, **kwargs):
            nonlocal lock_held
            with original_hold(*args, **kwargs):
                lock_held = True
                try:
                    yield
                finally:
                    lock_held = False

        def require_lock(argv, context, **kwargs):
            if "deprovision" in argv or "fleet.remove" in argv:
                self.assertTrue(lock_held)
            return original_run(argv, context, **kwargs)

        with (
            mock.patch.object(runtime, "_hold_runtime_locks", new=recording_lock),
            mock.patch.object(runtime, "_run_json", side_effect=require_lock),
        ):
            result = runtime.remove("review", self.state, execute=True)

        self.assertEqual("removed", result["status"])
        self.assertFalse(lock_held)
        self.assertFalse((self.state / "runtimes/review.json").exists())

    def test_launch_identity_may_differ_from_fleet_identity_without_mixing_state_paths(self):
        launch = json.loads(json.dumps(LAUNCH_PROFILE))
        launch["metadata"]["id"] = "review-local"
        (self.launches / "review-local.yml").write_text(
            json.dumps(launch), encoding="utf-8"
        )
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )

        result = runtime.start(
            "review-local",
            [self.fleets],
            [self.profiles],
            self.state,
            str(self.root),
            "codex",
            execute=True,
            once=True,
        )

        self.assertEqual("review-local", result["launch_id"])
        self.assertEqual("review", result["fleet_id"])
        self.assertTrue((self.state / "runtimes/review-local.json").is_file())
        self.assertFalse((self.state / "runtimes/review.json").exists())
        controller = next(
            call for call in runner.calls if call[0] == "fleet-controller"
        )
        self.assertEqual("review", controller[controller.index("--fleet") + 1])
        self.assertEqual(
            str((self.state / "fleets/review-local/core.sqlite3").resolve()),
            controller[controller.index("--core-db") + 1],
        )

    def test_second_launch_profile_for_active_fleet_is_rejected_before_new_state(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review",
            [self.fleets],
            [self.profiles],
            self.state,
            str(self.root),
            "codex",
            execute=True,
            once=True,
        )
        launch = json.loads(json.dumps(LAUNCH_PROFILE))
        launch["metadata"]["id"] = "review-other"
        (self.launches / "review-other.yml").write_text(
            json.dumps(launch), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError,
            "already has active LaunchProfile 'review'",
        ):
            runtime.start(
                "review-other",
                [self.fleets],
                [self.profiles],
                self.state,
                str(self.root),
                "codex",
                execute=True,
                once=True,
            )

        self.assertFalse((self.state / "runtimes/review-other.json").exists())
        self.assertFalse((self.state / "fleets/review-other").exists())

    def test_other_launch_becoming_active_while_waiting_for_lock_is_rejected(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        original_assert = runtime._assert_no_other_active_launch
        calls = 0

        def activate_other_launch_before_locked_check(resolved, state_dir):
            nonlocal calls
            calls += 1
            if calls == 2:
                other_manifest = state_dir / "runtimes/review-other.json"
                other_manifest.parent.mkdir(parents=True)
                other_manifest.write_text(
                    json.dumps(
                        {
                            "launch_id": "review-other",
                            "fleet_id": "review",
                            "phase": "active",
                        }
                    ),
                    encoding="utf-8",
                )
            return original_assert(resolved, state_dir)

        with (
            mock.patch.object(
                runtime,
                "_assert_no_other_active_launch",
                side_effect=activate_other_launch_before_locked_check,
            ),
            self.assertRaisesRegex(
                fleet_runtime.FleetRuntimeError,
                "already has active LaunchProfile 'review-other'",
            ),
        ):
            runtime.start(
                "review",
                [self.fleets],
                [self.profiles],
                self.state,
                str(self.root),
                "codex",
                execute=True,
                once=True,
            )

        self.assertEqual(2, calls)
        self.assertFalse((self.state / "runtimes/review.json").exists())
        self.assertFalse((self.state / "fleets/review").exists())

    def test_start_rejects_a_modified_materialized_hook_runtime(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review",
            [self.fleets],
            [self.profiles],
            self.state,
            str(self.root),
            "codex",
            execute=True,
            once=True,
        )
        hook_runtime = next(
            (self.state / "fleets/review/hook-runtimes").glob("*/role_context.py")
        )
        hook_runtime.write_text("raise SystemExit(91)\n", encoding="utf-8")

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "hook runtime content"
        ):
            runtime.start(
                "review",
                [self.fleets],
                [self.profiles],
                self.state,
                str(self.root),
                "codex",
                execute=True,
                once=True,
            )

    def test_start_rejects_missing_execution_prerequisite_before_state_creation(self):
        runtime = fleet_runtime.FleetRuntime(
            ["missing-fleet-control"],
            ["missing-fleet-herdr"],
            ["missing-fleet-controller"],
        )

        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "required executable"):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

        self.assertFalse(self.state.exists())

    def test_start_uses_fixed_configuration_snapshots_after_launch_commit(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=runner,
            role_catalog=self.role_catalog,
        )
        original_materialize_hook = runtime._materialize_hook_runtime

        def mutate_sources_after_snapshots(fleet_state_dir, hook_payload):
            changed = json.loads(json.dumps(FLEET))
            changed["spec"]["objective"] = "Changed after snapshot commit"
            (self.fleets / "review.yml").write_text(
                json.dumps(changed), encoding="utf-8"
            )
            self.role_catalog.write_text("fixture: changed\n", encoding="utf-8")
            return original_materialize_hook(fleet_state_dir, hook_payload)

        with mock.patch.object(
            runtime,
            "_materialize_hook_runtime",
            side_effect=mutate_sources_after_snapshots,
        ):
            runtime.start(
                "review",
                [self.fleets],
                [self.profiles],
                self.state,
                str(self.root),
                "codex",
                execute=True,
                once=True,
            )

        core_provision = next(
            call
            for call in runner.calls
            if "fleet.provision" in call
        )
        fleet_snapshot = Path(
            core_provision[core_provision.index("--config") + 1]
        )
        role_snapshot = Path(
            core_provision[core_provision.index("--role-catalog") + 1]
        )
        snapshot_root = (
            self.state / "fleets/review/config-snapshots"
        ).resolve()
        self.assertTrue(fleet_snapshot.resolve().is_relative_to(snapshot_root))
        self.assertTrue(role_snapshot.resolve().is_relative_to(snapshot_root))
        self.assertEqual(FLEET, json.loads(fleet_snapshot.read_text(encoding="utf-8")))
        self.assertEqual(
            {"fixture": True},
            json.loads(role_snapshot.read_text(encoding="utf-8")),
        )
        adapter_provision = next(
            call
            for call in runner.calls
            if "provision" in call and "--execute" in call
        )
        adapter_fleet = json.loads(
            adapter_provision[adapter_provision.index("--fleet-json") + 1]
        )
        self.assertEqual(FLEET["spec"]["objective"], adapter_fleet["spec"]["objective"])

    def test_start_rejects_config_changed_during_preflight_before_state_creation(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )

        def mutate_after_runtime_preflight(_resolved, _cwd):
            changed = json.loads(json.dumps(FLEET))
            changed["spec"]["objective"] = "Changed after composition"
            (self.fleets / "review.yml").write_text(
                json.dumps(changed), encoding="utf-8"
            )
            return {
                "herdr_version": "herdr 0.8.0",
                "products": ["claude", "codex"],
                "cwd": str(self.root.resolve()),
            }

        with (
            mock.patch.object(
                runtime, "_preflight_runtime", side_effect=mutate_after_runtime_preflight
            ),
            self.assertRaisesRegex(
                fleet_runtime.FleetRuntimeError,
                "configuration changed during launch preflight: Fleet",
            ),
        ):
            runtime.start(
                "review",
                [self.fleets],
                [self.profiles],
                self.state,
                str(self.root),
                "codex",
                execute=True,
                once=True,
            )

        self.assertFalse(self.state.exists())

    def test_start_rejects_view_profile_changed_after_catalog_read(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        original_profiles = runtime._profiles

        def profiles_then_mutate(profile_dirs):
            catalog = original_profiles(profile_dirs)
            changed = json.loads(json.dumps(PROFILE))
            changed["spec"]["layout"]["children"][0]["weight"] = 41
            (self.profiles / "review-grid.yml").write_text(
                json.dumps(changed), encoding="utf-8"
            )
            return catalog

        with (
            mock.patch.object(runtime, "_profiles", side_effect=profiles_then_mutate),
            self.assertRaisesRegex(
                fleet_runtime.FleetRuntimeError,
                "configuration changed during launch preflight: ViewProfile",
            ),
        ):
            runtime.start(
                "review",
                [self.fleets],
                [self.profiles],
                self.state,
                str(self.root),
                "codex",
                execute=True,
                once=True,
            )

        self.assertFalse(self.state.exists())

    def test_start_rejects_launch_profile_changed_after_catalog_read(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        original_resolve = fleet_runtime.LaunchProfileCatalog.resolve

        def resolve_then_mutate(catalog, launch_id):
            resolved = original_resolve(catalog, launch_id)
            changed = json.loads(json.dumps(LAUNCH_PROFILE))
            changed["spec"]["codex_hook_trust"] = "review"
            (self.launches / "review.yml").write_text(
                json.dumps(changed), encoding="utf-8"
            )
            return resolved

        with (
            mock.patch.object(
                fleet_runtime.LaunchProfileCatalog,
                "resolve",
                new=resolve_then_mutate,
            ),
            self.assertRaisesRegex(
                fleet_runtime.FleetRuntimeError,
                "configuration changed during launch preflight: LaunchProfile",
            ),
        ):
            runtime.start(
                "review",
                [self.fleets],
                [self.profiles],
                self.state,
                str(self.root),
                "codex",
                execute=True,
                once=True,
            )

        self.assertFalse(self.state.exists())

    def test_start_rejects_change_under_lock_without_creating_fleet_state(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        original_assert = runtime._assert_config_snapshot
        calls = 0

        def mutate_before_locked_check(resolved):
            nonlocal calls
            calls += 1
            if calls == 2:
                changed = json.loads(json.dumps(FLEET))
                changed["spec"]["objective"] = "Changed before locked check"
                (self.fleets / "review.yml").write_text(
                    json.dumps(changed), encoding="utf-8"
                )
            return original_assert(resolved)

        with (
            mock.patch.object(
                runtime,
                "_assert_config_snapshot",
                side_effect=mutate_before_locked_check,
            ),
            self.assertRaisesRegex(
                fleet_runtime.FleetRuntimeError,
                "configuration changed during launch preflight: Fleet",
            ),
        ):
            runtime.start(
                "review",
                [self.fleets],
                [self.profiles],
                self.state,
                str(self.root),
                "codex",
                execute=True,
                once=True,
            )

        self.assertEqual(2, calls)
        self.assertFalse(self.state.exists())

    def test_resolve_rejects_fleet_changed_while_core_validates_it(self):
        fleet_path = self.fleets / "review.yml"

        class MutatingValidationRunner(FakeRunner):
            def __call__(self, argv, **kwargs):
                completed = super().__call__(argv, **kwargs)
                if "spec.validate" in argv:
                    changed = json.loads(json.dumps(FLEET))
                    changed["spec"]["objective"] = "Changed during validation"
                    fleet_path.write_text(json.dumps(changed), encoding="utf-8")
                return completed

        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=MutatingValidationRunner(),
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError,
            "configuration changed during Fleet validation",
        ):
            runtime.resolve("review", [self.fleets], [self.profiles], self.state)

        self.assertFalse(self.state.exists())

    def test_core_validates_an_immutable_snapshot_during_source_aba_change(self):
        fleet_path = self.fleets / "review.yml"
        role_catalog_path = self.role_catalog
        test_case = self

        class AbaValidationRunner(FakeRunner):
            def __call__(self, argv, **kwargs):
                if "spec.validate" not in argv:
                    return super().__call__(argv, **kwargs)
                self.calls.append(list(argv))
                changed = json.loads(json.dumps(FLEET))
                changed["spec"]["objective"] = "Temporary B value"
                fleet_path.write_text(json.dumps(changed), encoding="utf-8")
                config_snapshot = Path(argv[argv.index("--config") + 1])
                catalog_snapshot = Path(argv[argv.index("--role-catalog") + 1])
                try:
                    validated = json.loads(config_snapshot.read_text(encoding="utf-8"))
                    test_case.assertNotEqual(fleet_path.resolve(), config_snapshot.resolve())
                    test_case.assertNotEqual(
                        role_catalog_path.resolve(), catalog_snapshot.resolve()
                    )
                    test_case.assertEqual(
                        {"fixture": True},
                        json.loads(catalog_snapshot.read_text(encoding="utf-8")),
                    )
                finally:
                    fleet_path.write_text(json.dumps(FLEET), encoding="utf-8")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({"ok": True, "result": validated}),
                    "",
                )

        runner = AbaValidationRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=runner,
            role_catalog=self.role_catalog,
        )

        resolved = runtime.resolve(
            "review", [self.fleets], [self.profiles], self.state
        )

        self.assertEqual(FLEET["spec"]["objective"], resolved.fleet["spec"]["objective"])
        self.assertFalse(self.state.exists())

    def test_resume_rejects_different_execution_identity_with_recovery_guidance(self):
        runner = FakeRunner()
        original = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        original.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        manifest = self.state / "runtimes/review.json"
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        saved["execution_identity"] = {"core": {"implementation_sha256": "old"}}
        manifest.write_text(json.dumps(saved), encoding="utf-8")

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "new fleet ID"
        ):
            original.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

    def test_legacy_manifest_is_rejected_before_runner_side_effects(self):
        runner = FakeRunner()
        manifest = self.state / "runtimes/review.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"fleet_id": "review", "phase": "active"}), encoding="utf-8")
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "legacy manifest"):
            runtime.start("review", [self.fleets], [self.profiles], self.state,
                          str(self.root), "codex", execute=True, once=True)
        self.assertEqual([], runner.calls)

    def test_execution_identity_includes_wrapper_implementation_content(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            [str(self.commands["fleet-control"])],
            [str(self.commands["fleet-herdr"])],
            [str(self.commands["fleet-controller"])],
            runner=runner,
        )
        first = runtime._preflight_execution()
        (self.commands["fleet-control"].parent.parent / "fleet_control.py").write_text("# changed\n", encoding="utf-8")
        second = runtime._preflight_execution()

        self.assertNotEqual(first["core"], second["core"])
        self.assertEqual(first["adapter"], second["adapter"])

    def test_start_materializes_the_same_hook_bytes_captured_by_preflight(self):
        hook_source = self.root / "role_context.py"
        hook_a = b"# hook A\n"
        hook_source.write_bytes(hook_a)
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=runner,
            hook_source=hook_source,
        )
        original_preflight = runtime._preflight_execution

        def change_source_after_preflight(hook_payload):
            identity = original_preflight(hook_payload)
            hook_source.write_bytes(b"# hook B\n")
            return identity

        with mock.patch.object(
            runtime,
            "_preflight_execution",
            side_effect=change_source_after_preflight,
        ):
            runtime.start(
                "review",
                [self.fleets],
                [self.profiles],
                self.state,
                str(self.root),
                "codex",
                execute=True,
                once=True,
            )

        manifest = json.loads(
            (self.state / "runtimes/review.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["execution_identity"]["hook"]["sha256"],
            manifest["hook_sha256"],
        )
        self.assertEqual(hook_a, Path(manifest["hook_runtime"]).read_bytes())

    def test_execution_identity_covers_adapter_tree_and_rejects_before_side_effects(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start("review", [self.fleets], [self.profiles], self.state,
                      str(self.root), "codex", execute=True, once=True)
        manifest = self.state / "runtimes/review.json"
        before_manifest = manifest.read_bytes()
        before_calls = len(runner.calls)
        (self.root / "adapter" / "view_profiles.py").write_text("# changed\n", encoding="utf-8")
        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "identity conflict"):
            runtime.start("review", [self.fleets], [self.profiles], self.state,
                          str(self.root), "codex", execute=True, once=True)
        self.assertEqual(before_manifest, manifest.read_bytes())
        self.assertEqual(before_calls, len(runner.calls))

    def test_runtime_module_change_rejects_without_side_effects(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start("review", [self.fleets], [self.profiles], self.state,
                      str(self.root), "codex", execute=True, once=True)
        manifest = self.state / "runtimes/review.json"
        before_manifest = manifest.read_bytes()
        before_calls = len(runner.calls)
        (self.root / "adapter" / "fleet_runtime.py").write_text(
            "# changed runtime\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "identity conflict"):
            runtime.start("review", [self.fleets], [self.profiles], self.state,
                          str(self.root), "codex", execute=True, once=True)

        self.assertEqual(before_manifest, manifest.read_bytes())
        self.assertEqual(before_calls, len(runner.calls))

    def test_same_install_resumes_after_pyc_generation(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start("review", [self.fleets], [self.profiles], self.state,
                      str(self.root), "codex", execute=True, once=True)
        cache = self.root / "adapter" / "__pycache__"
        cache.mkdir()
        (cache / "view_profiles.cpython-314.pyc").write_bytes(b"generated")
        resumed = runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )

        self.assertEqual("resumed", resumed["status"])

    def test_same_content_at_different_install_path_rejects_resume(self):
        runner = FakeRunner()
        original = fleet_runtime.FleetRuntime(
            [str(self.commands["fleet-control"])],
            [str(self.commands["fleet-herdr"])],
            [str(self.commands["fleet-controller"])],
            runner=runner,
        )
        original.start("review", [self.fleets], [self.profiles], self.state,
                       str(self.root), "codex", execute=True, once=True)
        manifest = self.state / "runtimes/review.json"
        before_manifest = manifest.read_bytes()
        before_calls = len(runner.calls)
        relocated = self.root / "relocated"
        shutil.copytree(self.root / "core", relocated / "core")
        shutil.copytree(self.root / "adapter", relocated / "adapter")
        moved = fleet_runtime.FleetRuntime(
            [str(relocated / "core/scripts/fleet-control")],
            [str(relocated / "adapter/scripts/fleet-herdr")],
            [str(relocated / "adapter/scripts/fleet-controller")],
            runner=runner,
        )

        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "identity conflict"):
            moved.start("review", [self.fleets], [self.profiles], self.state,
                        str(self.root), "codex", execute=True, once=True)

        self.assertEqual(before_manifest, manifest.read_bytes())
        self.assertEqual(before_calls, len(runner.calls))

    def test_different_hook_rejects_same_fleet_id(self):
        runner = FakeRunner()
        first_source = self.root / "role-context-v1.py"
        second_source = self.root / "role-context-v2.py"
        first_source.write_text("print('v1')\n", encoding="utf-8")
        second_source.write_text("print('v2')\n", encoding="utf-8")
        first_runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=runner,
            hook_source=first_source,
        )
        started = first_runtime.start(
            "review",
            [self.fleets],
            [self.profiles],
            self.state,
            str(self.root),
            "codex",
            execute=True,
            once=True,
        )
        first_hook = Path(started["hook_runtime"])

        updated_runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=runner,
            hook_source=second_source,
        )
        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "identity conflict"):
            updated_runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )
        self.assertEqual("print('v1')\n", first_hook.read_text(encoding="utf-8"))

    def test_different_hook_can_start_with_new_fleet_id(self):
        class ConfigAwareRunner(FakeRunner):
            def __call__(self, argv, **kwargs):
                if "spec.validate" in argv:
                    self.calls.append(list(argv))
                    config = Path(argv[argv.index("--config") + 1])
                    payload = {
                        "ok": True,
                        "result": json.loads(config.read_text(encoding="utf-8")),
                    }
                    return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
                return super().__call__(argv, **kwargs)

        updated_fleet = json.loads(json.dumps(FLEET))
        updated_fleet["metadata"]["id"] = "review-v2"
        (self.fleets / "review-v2.yml").write_text(
            json.dumps(updated_fleet), encoding="utf-8"
        )
        updated_launch = json.loads(json.dumps(LAUNCH_PROFILE))
        updated_launch["metadata"]["id"] = "review-v2"
        updated_launch["spec"]["fleet_ref"] = "review-v2"
        (self.launches / "review-v2.yml").write_text(
            json.dumps(updated_launch), encoding="utf-8"
        )
        first_source = self.root / "role-context-v1.py"
        second_source = self.root / "role-context-v2.py"
        first_source.write_text("print('v1')\n", encoding="utf-8")
        second_source.write_text("print('v2')\n", encoding="utf-8")
        runner = ConfigAwareRunner()
        first_runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"],
            runner=runner, hook_source=first_source,
        )
        second_runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"],
            runner=runner, hook_source=second_source,
        )

        started = first_runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        updated = second_runtime.start(
            "review-v2", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )

        self.assertEqual("started", started["status"])
        self.assertEqual("started", updated["status"])
        self.assertNotEqual(started["hook_runtime"], updated["hook_runtime"])

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
