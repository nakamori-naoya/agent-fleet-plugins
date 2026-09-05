import importlib.util
import json
import os
import shutil
import sqlite3
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
SESSION_HOOK_PLUGIN = Path(__file__).parents[2] / "session-hooks-plugin"
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
        if list(argv[:3]) == ["claude", "auth", "status"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), ""
            )
        if list(argv[:3]) == ["codex", "plugin", "list"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "installed": [
                            {
                                "pluginId": "agent-fleet-herdr@agent-fleet",
                                "installed": True,
                                "version": "test",
                            }
                        ]
                    }
                ),
                "",
            )
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
    def test_interactive_shell_ignores_untrusted_shell_path(self):
        with mock.patch.dict(os.environ, {"SHELL": "/private/tmp/untrusted/bash"}):
            argv = fleet_runtime.FleetRuntime._interactive_shell_argv(
                "codex-personal", ["plugin", "list", "--json"]
            )

        self.assertEqual("/bin/bash", argv[0])
        self.assertEqual("-lic", argv[1])
        self.assertEqual("codex-personal plugin list --json", argv[2])

    def test_agent_core_command_preserves_an_executable_path_with_spaces(self):
        executable = self.root / "fixed runtime/core/fleet-control"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o500)
        runtime = fleet_runtime.FleetRuntime(
            [str(executable)], ["fleet-herdr"], ["fleet-controller"]
        )

        self.assertEqual(str(executable.resolve()), runtime._agent_core_command())

    def test_agent_core_command_rejects_arguments_in_environment_value(self):
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control", "--unsafe-extra"],
            ["fleet-herdr"],
            ["fleet-controller"],
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "one executable path"
        ):
            runtime._agent_core_command()

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
        with (
            mock.patch.object(
                runtime, "_execution_bundle_from_manifest", return_value=mock.sentinel.bundle
            ),
            mock.patch.object(runtime, "_with_execution_bundle", return_value=runtime),
            mock.patch.object(
                runtime,
                "_run_json",
                side_effect=[
                    {"status": "idle"},
                    {"status": "idle"},
                    {"status": "delivered"},
                    {"status": "idle"},
                ],
            ),
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
        self.command_profiles = self.root / "agent-command-profiles"
        self.state = self.root / "state"
        self.role_catalog = self.root / "role-catalog.yml"
        self.fleets.mkdir()
        self.profiles.mkdir()
        self.launches.mkdir()
        self.command_profiles.mkdir()
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
        required_fixtures = {
            "spec/scripts/validate_fleet.py": "# validator fixture\n",
            "spec/schema/envelopes.schema.yml": "{}\n",
            "spec/schema/fleet.schema.yml": "{}\n",
            "spec/config/defaults.yml": "{}\n",
            "config/defaults.yml": "{}\n",
            "adapter/launch_profiles.py": "# launch fixture\n",
            "adapter/agent_command_profiles.py": "# command profile fixture\n",
            "adapter/schema/agent-command-profile.schema.yml": "{}\n",
            "adapter/schema/launch-profile.schema.yml": "{}\n",
            "adapter/schema/view-profile.schema.yml": "{}\n",
            "adapter/scripts/fleet-runtime": "#!/bin/sh\n",
            "session-hooks-plugin/hooks/claude-hooks.json": (
                SESSION_HOOK_PLUGIN / "hooks" / "claude-hooks.json"
            ).read_text(encoding="utf-8"),
            "session-hooks-plugin/hooks/codex-hooks.json": "{}\n",
            "session-hooks-plugin/.claude-plugin/plugin.json": (
                SESSION_HOOK_PLUGIN / ".claude-plugin" / "plugin.json"
            ).read_text(encoding="utf-8"),
            "session-hooks-plugin/.codex-plugin/plugin.json": "{}\n",
        }
        for relative, content in required_fixtures.items():
            fixture = self.root / relative
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text(content, encoding="utf-8")
            if fixture.parent.name == "scripts":
                fixture.chmod(0o700)
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

    def test_launch_resolves_agent_commands_and_passes_composed_json_to_adapter(self):
        launch = json.loads(json.dumps(LAUNCH_PROFILE))
        launch["spec"]["agent_command_profiles"] = {
            "manager": "local/claude-personal@1",
            "worker": "local/codex-personal@1",
        }
        (self.launches / "review.yml").write_text(json.dumps(launch), encoding="utf-8")
        for product, command in (
            ("claude", "claude-personal"),
            ("codex", "codex-personal"),
        ):
            document = {
                "apiVersion": "fleet.runtime.harness/v1",
                "kind": "AgentCommandProfile",
                "metadata": {"id": f"local/{command}", "version": 1},
                "spec": {"product": product, "command": command},
            }
            (self.command_profiles / f"{command}.yml").write_text(
                json.dumps(document), encoding="utf-8"
            )
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=runner,
            role_catalog=self.role_catalog,
            agent_command_profile_dirs=[self.command_profiles],
        )

        result = runtime.plan(
            "review", [self.fleets], [self.profiles], self.state, str(self.root), "codex"
        )

        provision = next(call for call in runner.calls if "provision" in call)
        profiles = json.loads(
            provision[provision.index("--agent-command-profiles-json") + 1]
        )
        self.assertEqual("claude-personal", profiles["manager"]["command"])
        self.assertEqual("codex-personal", profiles["worker"]["command"])
        self.assertEqual(
            "local/claude-personal@1", profiles["manager"]["profile_ref"]
        )
        self.assertEqual("planned", result["status"])

    def test_missing_agent_command_profile_is_rejected_before_state_creation(self):
        launch = json.loads(json.dumps(LAUNCH_PROFILE))
        launch["spec"]["agent_command_profiles"] = {
            "worker": "local/codex-missing@1"
        }
        (self.launches / "review.yml").write_text(json.dumps(launch), encoding="utf-8")
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=FakeRunner(),
            agent_command_profile_dirs=[self.command_profiles],
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "AgentCommandProfile not found"
        ):
            runtime.resolve("review", [self.fleets], [self.profiles], self.state)

        self.assertFalse(self.state.exists())

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

    def test_boolean_view_profile_version_is_not_an_integer_identity(self):
        profile = json.loads(json.dumps(PROFILE))
        profile["metadata"]["version"] = True
        (self.profiles / "review-grid.yml").write_text(
            json.dumps(profile), encoding="utf-8"
        )
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=FakeRunner()
        )

        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "identity is invalid"):
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
            mock.patch.object(
                fleet_runtime, "FleetRuntime", return_value=runtime
            ) as runtime_type,
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
        self.assertEqual(
            (self.root / ".config/agent-fleet/agent-command-profiles",),
            tuple(runtime_type.call_args.kwargs["agent_command_profile_dirs"]),
        )

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
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=FakeRunner(),
            role_catalog=self.role_catalog,
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
        controller = next(
            call
            for call in runner.calls
            if Path(call[0]).name == "fleet-controller" and "--execute" in call
        )
        self.assertIn("--execute", controller)
        self.assertEqual(
            "fleet-control",
            Path(controller[controller.index("--core-command") + 1]).name,
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
        self.assertEqual(0o400, hook_runtime.stat().st_mode & 0o777)
        self.assertTrue(
            hook_runtime.is_relative_to(
                (self.state / "fleets/review/hook-runtimes").resolve()
            )
        )
        provision_calls = [call for call in runner.calls if "provision" in call]
        planned_call = next(
            call
            for call in provision_calls
            if "--execute" not in call and "--agent-core-command" in call
        )
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

    def test_repeated_stop_is_idempotent_without_repeating_external_changes(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        runtime.stop("review", self.state, execute=True)
        runner.calls.clear()

        repeated = runtime.stop("review", self.state, execute=True)

        self.assertEqual("stopped", repeated["status"])
        self.assertEqual("already_stopped", repeated["herdr"]["status"])
        self.assertEqual([], runner.calls)

    def test_stop_retry_reuses_context_invalidation_operation_identity(self):
        class LostResponseRunner(FakeRunner):
            response_lost = False

            def __call__(self, argv, **kwargs):
                if "context.invalidate" in argv and not self.response_lost:
                    self.calls.append(list(argv))
                    self.response_lost = True
                    return subprocess.CompletedProcess(
                        argv, 2, "", "response was lost"
                    )
                return super().__call__(argv, **kwargs)

        runner = LostResponseRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        (self.state / "fleets/review/core.sqlite3").touch()

        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "response was lost"):
            runtime.stop("review", self.state, execute=True)
        stopped = runtime.stop("review", self.state, execute=True)

        invalidations = [
            call for call in runner.calls if "context.invalidate" in call
        ]
        self.assertEqual("stopped", stopped["status"])
        self.assertEqual(2, len(invalidations))
        self.assertEqual(
            invalidations[0][invalidations[0].index("--operation-id") + 1],
            invalidations[1][invalidations[1].index("--operation-id") + 1],
        )

    def test_remove_resumes_from_durable_removing_phase(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        runtime.stop("review", self.state, execute=True)
        manifest_path = self.state / "runtimes/review.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["phase"] = "removing"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        core_db = self.state / "fleets/review/core.sqlite3"
        database = sqlite3.connect(core_db)
        try:
            database.execute("CREATE TABLE initialized_before_interruption(marker TEXT)")
            database.commit()
        finally:
            database.close()
        runner.calls.clear()

        removed = runtime.remove("review", self.state, execute=True)

        self.assertEqual("removed", removed["status"])
        self.assertFalse(manifest_path.exists())
        self.assertFalse(any("deprovision" in call for call in runner.calls))
        self.assertEqual(1, sum("fleet.remove" in call for call in runner.calls))

    def test_remove_completes_when_stopped_before_core_database_existed(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        manifest_path = self.state / "runtimes/review.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["phase"] = "stopped"
        manifest["stop_from_phase"] = "planned"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        core_db = self.state / "fleets/review/core.sqlite3"
        core_db.unlink(missing_ok=True)
        runner.calls.clear()

        removed = runtime.remove("review", self.state, execute=True)

        self.assertEqual("removed", removed["status"])
        self.assertEqual("absent", removed["core"]["status"])
        self.assertTrue(removed["core"]["idempotent"])
        self.assertFalse(manifest_path.exists())
        self.assertFalse(any("fleet.remove" in call for call in runner.calls))

    def test_remove_delegates_a_partially_initialized_database_to_core(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        manifest_path = self.state / "runtimes/review.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["phase"] = "stopped"
        manifest["stop_from_phase"] = "planned"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        core_db = self.state / "fleets/review/core.sqlite3"
        core_db.unlink(missing_ok=True)
        database = sqlite3.connect(core_db)
        try:
            database.execute("CREATE TABLE interrupted_initialization(marker TEXT)")
            database.commit()
        finally:
            database.close()
        runner.calls.clear()

        removed = runtime.remove("review", self.state, execute=True)

        self.assertEqual("removed", removed["status"])
        self.assertFalse(manifest_path.exists())
        self.assertEqual(1, sum("fleet.remove" in call for call in runner.calls))

    def test_start_rejects_an_allowlisted_file_reached_through_a_symlink_directory(self):
        external_hook_plugin = self.root / "external-session-hooks-plugin"
        (self.root / "session-hooks-plugin").rename(external_hook_plugin)
        (self.root / "session-hooks-plugin").symlink_to(
            external_hook_plugin, target_is_directory=True
        )
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=FakeRunner()
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "symbolic or invalid directory"
        ):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

        self.assertFalse((self.state / "runtimes/review.json").exists())

    def test_start_rejects_an_unrunnable_fixed_controller_before_state_creation(self):
        class BrokenControllerRunner(FakeRunner):
            def __call__(self, argv, **kwargs):
                if (
                    Path(argv[0]).name == "fleet-controller"
                    and "--execute" not in argv
                ):
                    self.calls.append(list(argv))
                    return subprocess.CompletedProcess(
                        argv, 2, "", "controller import failed"
                    )
                return super().__call__(argv, **kwargs)

        runner = BrokenControllerRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError,
            "Fleet controller executable preflight failed",
        ):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

        self.assertFalse((self.state / "runtimes/review.json").exists())
        self.assertFalse((self.state / "fleets/review").exists())

    def test_start_rejects_invalid_hook_syntax_before_state_creation(self):
        invalid_hook = self.root / "invalid-role-context.py"
        invalid_hook.write_text("def broken(:\n", encoding="utf-8")
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"],
            runner=FakeRunner(),
            hook_source=invalid_hook,
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "invalid Python syntax"
        ):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

        self.assertFalse((self.state / "runtimes/review.json").exists())
        self.assertFalse((self.state / "fleets/review").exists())

    def test_start_rejects_invalid_claude_hook_registration_before_state_creation(self):
        registration = self.root / "session-hooks-plugin/hooks/claude-hooks.json"
        registration.write_text("{}\n", encoding="utf-8")
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"],
            runner=FakeRunner(),
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "invalid event bindings"
        ):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

        self.assertFalse((self.state / "runtimes/review.json").exists())
        self.assertFalse((self.state / "fleets/review").exists())

    def test_start_rejects_a_noop_claude_hook_registration_before_state_creation(self):
        registration = self.root / "session-hooks-plugin/hooks/claude-hooks.json"
        document = json.loads(registration.read_text(encoding="utf-8"))
        for event_name in ("SessionStart", "UserPromptSubmit"):
            document["hooks"][event_name][0]["hooks"][0]["args"][1] = (
                "exit 0 # AGENT_FLEET_HOOK_RUNTIME --runtime-product claude"
            )
        registration.write_text(json.dumps(document), encoding="utf-8")
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"],
            runner=FakeRunner(),
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "invalid SessionStart command"
        ):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

        self.assertFalse((self.state / "runtimes/review.json").exists())
        self.assertFalse((self.state / "fleets/review").exists())

    def test_start_rejects_a_claude_hook_timeout_above_the_nfr_limit(self):
        registration = self.root / "session-hooks-plugin/hooks/claude-hooks.json"
        document = json.loads(registration.read_text(encoding="utf-8"))
        document["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 999
        registration.write_text(json.dumps(document), encoding="utf-8")
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"],
            runner=FakeRunner(),
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "invalid SessionStart command"
        ):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

        self.assertFalse((self.state / "runtimes/review.json").exists())

    def test_stop_during_pre_manifest_validation_cancels_start(self):
        validation_started = threading.Event()
        release_validation = threading.Event()
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        original_preflight = runtime._preflight_runtime

        def blocking_preflight(resolved, cwd):
            validation_started.set()
            if not release_validation.wait(timeout=3):
                raise AssertionError("test did not release runtime preflight")
            return original_preflight(resolved, cwd)

        with (
            mock.patch.object(
                runtime, "_preflight_runtime", side_effect=blocking_preflight
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
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
            self.assertTrue(validation_started.wait(timeout=3))
            stopping = executor.submit(
                runtime.stop, "review", self.state, execute=True
            )
            request_dir = self.state / "stop-requests/review"
            for _ in range(100):
                if request_dir.is_dir() and any(request_dir.glob("*.request")):
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("stop request was not published before a manifest existed")
            release_validation.set()

            with self.assertRaisesRegex(
                fleet_runtime.FleetRuntimeError,
                "cancelled by a stop request",
            ):
                starting.result(timeout=3)
            stopped = stopping.result(timeout=3)

        self.assertEqual("inactive", stopped["status"])
        self.assertFalse((self.state / "runtimes/review.json").exists())
        self.assertFalse((self.state / "fleets/review").exists())
        self.assertFalse(request_dir.exists())

    def test_stop_request_survives_a_crash_until_a_successful_stop_clears_it(self):
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

        self.assertTrue(runtime._stop_requested(self.state, "review"))
        self.assertTrue(stale_request.exists())
        with runtime._publish_stop_request(self.state, "review"):
            pass
        self.assertFalse(runtime._stop_requested(self.state, "review"))
        self.assertFalse(stale_request.exists())
        self.assertFalse(request_dir.exists())

    def test_one_completed_stop_does_not_clear_another_live_stop_request(self):
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=FakeRunner()
        )
        first = runtime._publish_stop_request(self.state, "review")
        second = runtime._publish_stop_request(self.state, "review")
        first_request = first.__enter__()
        second_request = second.__enter__()
        try:
            first.__exit__(None, None, None)
            self.assertFalse(first_request.exists())
            self.assertTrue(second_request.exists())
            self.assertTrue(runtime._stop_requested(self.state, "review"))
        finally:
            second.__exit__(None, None, None)

        self.assertFalse(runtime._stop_requested(self.state, "review"))

    def test_successful_stop_clears_a_dangling_stop_request_symlink(self):
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=FakeRunner()
        )
        request_dir = self.state / "stop-requests/review"
        request_dir.mkdir(parents=True)
        dangling = request_dir / "tampered.request"
        dangling.symlink_to(self.root / "missing-request-target")

        self.assertTrue(runtime._stop_requested(self.state, "review"))
        stopped = runtime.stop("review", self.state, execute=True)

        self.assertEqual("inactive", stopped["status"])
        self.assertFalse(dangling.exists())
        self.assertFalse(dangling.is_symlink())
        self.assertFalse(request_dir.exists())

    def test_timed_out_stop_request_prevents_start_until_stop_is_retried(self):
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=FakeRunner()
        )

        @contextmanager
        def timed_out_lock(*args, **kwargs):
            raise fleet_runtime.FleetRuntimeError("did not stop within 30 seconds")
            yield

        with (
            mock.patch.object(runtime, "_hold_launch_lock", new=timed_out_lock),
            self.assertRaisesRegex(
                fleet_runtime.FleetRuntimeError, "did not stop within 30 seconds"
            ),
        ):
            runtime.stop("review", self.state, execute=True)

        request_dir = self.state / "stop-requests/review"
        self.assertTrue(any(request_dir.glob("*.request")))
        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "cancelled by a stop request"
        ):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )
        self.assertFalse((self.state / "runtimes/review.json").exists())

        stopped = runtime.stop("review", self.state, execute=True)
        self.assertEqual("inactive", stopped["status"])
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
        original_hold = runtime._hold_fleet_lock
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
            mock.patch.object(runtime, "_hold_fleet_lock", new=recording_lock),
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
            call for call in runner.calls
            if Path(call[0]).name == "fleet-controller" and "--execute" in call
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
        hook_runtime.chmod(0o600)
        hook_runtime.write_text("raise SystemExit(91)\n", encoding="utf-8")

        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "hook runtime"):
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

    def test_status_rejects_extra_file_in_hook_runtime(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        started = runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        hook_dir = Path(started["hook_runtime"]).parent
        hook_dir.chmod(0o700)
        (hook_dir / "json.py").write_text("raise SystemExit(91)\n", encoding="utf-8")
        (hook_dir / "json.py").chmod(0o400)
        hook_dir.chmod(0o500)
        runner.calls.clear()

        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "hook runtime"):
            runtime.status("review", self.state)

        self.assertEqual([], runner.calls)

    def test_status_rejects_special_permission_bits_on_hook_runtime(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        started = runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        hook_runtime = Path(started["hook_runtime"])
        runner.calls.clear()
        original_stat = Path.stat

        def stat_with_setuid(path, *args, **kwargs):
            metadata = original_stat(path, *args, **kwargs)
            if str(path) == str(hook_runtime):
                values = list(metadata)
                values[0] |= 0o4000
                return os.stat_result(values)
            return metadata

        with (
            mock.patch.object(Path, "stat", autospec=True, side_effect=stat_with_setuid),
            self.assertRaisesRegex(
                fleet_runtime.FleetRuntimeError, "unsafe ownership or mode"
            ),
        ):
            runtime.status("review", self.state)

        self.assertEqual([], runner.calls)

    def test_start_rejects_symlink_execution_runtime_directory(self):
        outside = self.root / "outside-execution"
        outside.mkdir()
        fleet_state = self.state / "fleets/review"
        fleet_state.mkdir(parents=True)
        (fleet_state / "execution-runtimes").symlink_to(outside, target_is_directory=True)
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=FakeRunner()
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "must not be a symbolic link"
        ):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

        self.assertEqual([], list(outside.iterdir()))

    def test_start_rejects_special_permission_bits_on_execution_runtime_directory(self):
        fleet_state = self.state / "fleets/review"
        execution_runtimes = fleet_state / "execution-runtimes"
        execution_runtimes.mkdir(parents=True, mode=0o700)
        execution_runtimes.chmod(0o1700)
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=FakeRunner()
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "unsafe ownership or mode"
        ):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

    def test_start_rejects_symlink_hook_runtime_directory(self):
        outside = self.root / "outside-hook"
        outside.mkdir()
        fleet_state = self.state / "fleets/review"
        fleet_state.mkdir(parents=True)
        (fleet_state / "hook-runtimes").symlink_to(outside, target_is_directory=True)
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=FakeRunner()
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "must not be a symbolic link"
        ):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

        self.assertEqual([], list(outside.iterdir()))

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

    def test_start_rejects_missing_codex_hook_registration_before_state_creation(self):
        class MissingRegistrationRunner(FakeRunner):
            def __call__(self, argv, **kwargs):
                if list(argv[:3]) == ["codex", "plugin", "list"]:
                    self.calls.append(list(argv))
                    return subprocess.CompletedProcess(
                        argv, 0, json.dumps({"installed": []}), ""
                    )
                return super().__call__(argv, **kwargs)

        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=MissingRegistrationRunner(),
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError,
            "agent-fleet-herdr@agent-fleet must be installed",
        ):
            runtime.start(
                "review", [self.fleets], [self.profiles], self.state,
                str(self.root), "codex", execute=True, once=True,
            )

        self.assertFalse(self.state.exists())

    def test_start_rejects_unavailable_agent_command_before_state_creation(self):
        launch = json.loads(json.dumps(LAUNCH_PROFILE))
        launch["spec"]["agent_command_profiles"] = {
            "worker": "local/codex-personal@1"
        }
        (self.launches / "review.yml").write_text(json.dumps(launch), encoding="utf-8")
        command_profile = {
            "apiVersion": "fleet.runtime.harness/v1",
            "kind": "AgentCommandProfile",
            "metadata": {"id": "local/codex-personal", "version": 1},
            "spec": {"product": "codex", "command": "codex-personal"},
        }
        (self.command_profiles / "codex-personal.yml").write_text(
            json.dumps(command_profile), encoding="utf-8"
        )

        class MissingAliasRunner(FakeRunner):
            def __call__(self, argv, **kwargs):
                if len(argv) >= 3 and argv[1] == "-lic" and argv[2].startswith(
                    "command -v "
                ):
                    self.calls.append(list(argv))
                    return subprocess.CompletedProcess(argv, 1, "", "not found")
                return super().__call__(argv, **kwargs)

        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=MissingAliasRunner(),
            agent_command_profile_dirs=[self.command_profiles],
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError,
            "AgentCommandProfile command is unavailable.*codex-personal",
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

    def test_start_rejects_unauthenticated_claude_command_before_state_creation(self):
        launch = json.loads(json.dumps(LAUNCH_PROFILE))
        launch["spec"]["agent_command_profiles"] = {
            "manager": "local/claude-personal@1"
        }
        (self.launches / "review.yml").write_text(json.dumps(launch), encoding="utf-8")
        command_profile = {
            "apiVersion": "fleet.runtime.harness/v1",
            "kind": "AgentCommandProfile",
            "metadata": {"id": "local/claude-personal", "version": 1},
            "spec": {"product": "claude", "command": "claude-personal"},
        }
        (self.command_profiles / "claude-personal.yml").write_text(
            json.dumps(command_profile), encoding="utf-8"
        )

        class UnauthenticatedRunner(FakeRunner):
            def __call__(self, argv, **kwargs):
                if len(argv) >= 3 and argv[1] == "-lic" and argv[2].endswith(
                    " auth status"
                ):
                    self.calls.append(list(argv))
                    return subprocess.CompletedProcess(
                        argv, 0, json.dumps({"loggedIn": False}), ""
                    )
                return super().__call__(argv, **kwargs)

        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"],
            ["fleet-herdr"],
            ["fleet-controller"],
            runner=UnauthenticatedRunner(),
            agent_command_profile_dirs=[self.command_profiles],
        )

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError,
            "claude-personal is not authenticated.*claude-personal auth login",
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

    def test_active_resume_does_not_require_codex_registration_again(self):
        class ToggleRegistrationRunner(FakeRunner):
            registration_available = True

            def __call__(self, argv, **kwargs):
                if (
                    list(argv[:3]) == ["codex", "plugin", "list"]
                    and not self.registration_available
                ):
                    self.calls.append(list(argv))
                    return subprocess.CompletedProcess(
                        argv, 0, json.dumps({"installed": []}), ""
                    )
                return super().__call__(argv, **kwargs)

        runner = ToggleRegistrationRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        runner.registration_available = False
        runner.calls.clear()

        resumed = runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )

        self.assertEqual("resumed", resumed["status"])
        self.assertFalse(
            any(list(call[:3]) == ["codex", "plugin", "list"] for call in runner.calls)
        )

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

    def test_resume_rejects_tampered_execution_identity_before_side_effects(self):
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
            fleet_runtime.FleetRuntimeError, "path does not match its identity"
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
        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "manifest format is unsupported"
        ):
            runtime.start("review", [self.fleets], [self.profiles], self.state,
                          str(self.root), "codex", execute=True, once=True)
        self.assertEqual([], runner.calls)

    def test_boolean_manifest_format_version_is_rejected(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        manifest_path = self.state / "runtimes/review.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["manifest_format_version"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        runner.calls.clear()

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "manifest format is unsupported"
        ):
            runtime.status("review", self.state)

        self.assertEqual([], runner.calls)

    def test_status_rejects_manifest_without_a_known_phase(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        manifest_path = self.state / "runtimes/review.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("phase")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        runner.calls.clear()

        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "missing phase"):
            runtime.status("review", self.state)

        self.assertEqual([], runner.calls)

    def test_status_rejects_a_non_string_manifest_phase(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        manifest_path = self.state / "runtimes/review.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["phase"] = ["active"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        runner.calls.clear()

        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "invalid or missing phase"):
            runtime.status("review", self.state)

        self.assertEqual([], runner.calls)

    def test_status_rejects_a_manifest_with_another_launch_identity(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        manifest_path = self.state / "runtimes/review.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["launch_id"] = "another-launch"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        runner.calls.clear()

        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "launch identity"):
            runtime.status("review", self.state)

        self.assertEqual([], runner.calls)

    def test_status_reports_incomplete_removal_without_querying_runtime(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        manifest_path = self.state / "runtimes/review.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["phase"] = "removing"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        runner.calls.clear()

        result = runtime.status("review", self.state)

        self.assertEqual("removing", result["status"])
        self.assertTrue(result["recovery_required"])
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

    def test_start_uses_snapshot_when_executable_changes_after_capture(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        original_preflight = runtime._preflight_runtime
        implementation = (
            self.commands["fleet-control"].parent.parent / "fleet_control.py"
        )

        def change_executable_after_preflight(resolved, cwd):
            result = original_preflight(resolved, cwd)
            implementation.write_text("# updated during launch\n", encoding="utf-8")
            return result

        with mock.patch.object(
            runtime,
            "_preflight_runtime",
            side_effect=change_executable_after_preflight,
        ):
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
        snapshot_root = Path(result["execution_snapshot_root"]).resolve()
        snapshot_core = Path(result["runtime_commands"]["core"][0]).resolve()
        self.assertTrue(snapshot_core.is_relative_to(snapshot_root))
        self.assertEqual(
            "# fleet-control fixture\n",
            (snapshot_core.parent.parent / "fleet_control.py").read_text(
                encoding="utf-8"
            ),
        )
        self.assertEqual("# updated during launch\n", implementation.read_text())
        for call in runner.calls:
            if Path(call[0]).name in {
                "fleet-control",
                "fleet-herdr",
                "fleet-controller",
            }:
                self.assertNotIn(Path(call[0]).resolve(), self.commands.values())
                if "spec.validate" not in call and "--agent-core-command" in call:
                    self.assertTrue(
                        Path(call[0]).resolve().is_relative_to(snapshot_root)
                    )

    def test_monitor_keeps_using_snapshot_after_source_changes(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        started = runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        snapshot_root = Path(started["execution_snapshot_root"]).resolve()
        (self.root / "adapter" / "fleet_controller.py").write_text(
            "# source B remains installed\n", encoding="utf-8"
        )
        runner.calls.clear()

        runtime.monitor("review", self.state, once=True, poll_seconds=0.01)

        controller_call = next(
            call for call in runner.calls if Path(call[0]).name == "fleet-controller"
        )
        self.assertTrue(Path(controller_call[0]).resolve().is_relative_to(snapshot_root))
        self.assertEqual(
            "# fleet-controller fixture\n",
            (Path(controller_call[0]).parent.parent / "fleet_controller.py").read_text(
                encoding="utf-8"
            ),
        )

    def test_status_rejects_unexpected_file_in_execution_snapshot_before_runner(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        started = runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        snapshot_root = Path(started["execution_snapshot_root"])
        snapshot_root.chmod(0o700)
        (snapshot_root / "json.py").write_text("raise SystemExit(91)\n", encoding="utf-8")
        (snapshot_root / "json.py").chmod(0o400)
        snapshot_root.chmod(0o500)
        runner.calls.clear()

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "unexpected or missing path"
        ):
            runtime.status("review", self.state)

        self.assertEqual([], runner.calls)

    def test_status_rejects_execution_snapshot_mode_change_before_runner(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        started = runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        snapshot_root = Path(started["execution_snapshot_root"])
        declared_file = next(
            snapshot_root / item["path"]
            for item in started["execution_identity"]["files"]
            if item["mode"] == 0o400
        )
        declared_file.chmod(0o600)
        runner.calls.clear()

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "does not match its descriptor"
        ):
            runtime.status("review", self.state)

        self.assertEqual([], runner.calls)

    def test_status_rejects_special_permission_bits_on_an_execution_file(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        started = runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        snapshot_root = Path(started["execution_snapshot_root"])
        declared_file = next(
            snapshot_root / item["path"]
            for item in started["execution_identity"]["files"]
            if item["mode"] == 0o500
        )
        runner.calls.clear()

        original_stat = Path.stat

        def stat_with_setuid(path, *args, **kwargs):
            metadata = original_stat(path, *args, **kwargs)
            if str(path) == str(declared_file):
                values = list(metadata)
                values[0] |= 0o4000
                return os.stat_result(values)
            return metadata

        with (
            mock.patch.object(Path, "stat", autospec=True, side_effect=stat_with_setuid),
            self.assertRaisesRegex(
                fleet_runtime.FleetRuntimeError, "does not match its descriptor"
            ),
        ):
            runtime.status("review", self.state)

        self.assertEqual([], runner.calls)

    def test_status_rejects_manifest_command_change_before_runner(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        manifest_path = self.state / "runtimes/review.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["runtime_commands"]["controller"].append("--changed")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        runner.calls.clear()

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "commands do not match"
        ):
            runtime.status("review", self.state)

        self.assertEqual([], runner.calls)

    def test_status_rejects_manifest_identity_change_before_runner(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        manifest_path = self.state / "runtimes/review.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["execution_identity"]["commands"]["controller"].append("--changed")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        runner.calls.clear()

        with self.assertRaisesRegex(
            fleet_runtime.FleetRuntimeError, "path does not match its identity"
        ):
            runtime.status("review", self.state)

        self.assertEqual([], runner.calls)

    def test_status_does_not_recreate_missing_execution_state(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        fleet_state = self.state / "fleets/review"
        for directory in [fleet_state, *fleet_state.rglob("*")]:
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o700)
        shutil.rmtree(fleet_state)
        manifest_path = self.state / "runtimes/review.json"
        before_manifest = manifest_path.read_bytes()
        runner.calls.clear()

        with self.assertRaisesRegex(fleet_runtime.FleetRuntimeError, "is missing"):
            runtime.status("review", self.state)

        self.assertFalse(fleet_state.exists())
        self.assertEqual(before_manifest, manifest_path.read_bytes())
        self.assertEqual([], runner.calls)

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
            manifest["execution_identity"]["hook_sha256"],
            manifest["hook_sha256"],
        )
        self.assertEqual(hook_a, Path(manifest["hook_runtime"]).read_bytes())

    def test_active_resume_uses_saved_adapter_when_installed_adapter_changes(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start("review", [self.fleets], [self.profiles], self.state,
                      str(self.root), "codex", execute=True, once=True)
        manifest = self.state / "runtimes/review.json"
        before_manifest = manifest.read_bytes()
        (self.root / "adapter" / "view_profiles.py").write_text("# changed\n", encoding="utf-8")
        runner.calls.clear()

        resumed = runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )

        self.assertEqual("resumed", resumed["status"])
        self.assertEqual(before_manifest, manifest.read_bytes())
        snapshot_root = Path(resumed["execution_snapshot_root"]).resolve()
        self.assertTrue(
            all(
                Path(call[0]).resolve().is_relative_to(snapshot_root)
                for call in runner.calls
                if Path(call[0]).name in {
                    "fleet-control", "fleet-herdr", "fleet-controller"
                }
            )
        )

    def test_coordinator_source_is_outside_the_fixed_execution_bundle(self):
        runner = FakeRunner()
        runtime = fleet_runtime.FleetRuntime(
            ["fleet-control"], ["fleet-herdr"], ["fleet-controller"], runner=runner
        )
        runtime.start("review", [self.fleets], [self.profiles], self.state,
                      str(self.root), "codex", execute=True, once=True)
        manifest = self.state / "runtimes/review.json"
        before_manifest = manifest.read_bytes()
        (self.root / "adapter" / "fleet_runtime.py").write_text(
            "# changed runtime\n", encoding="utf-8"
        )
        runner.calls.clear()

        resumed = runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )

        self.assertEqual("resumed", resumed["status"])
        self.assertEqual(before_manifest, manifest.read_bytes())
        self.assertFalse(
            any(
                item["path"].endswith("adapter/fleet_runtime.py")
                for item in resumed["execution_identity"]["files"]
            )
        )

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

    def test_same_content_at_different_install_path_resumes_same_snapshot(self):
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
        relocated = self.root / "relocated"
        shutil.copytree(self.root / "core", relocated / "core")
        shutil.copytree(self.root / "adapter", relocated / "adapter")
        shutil.copytree(self.root / "spec", relocated / "spec")
        shutil.copytree(self.root / "config", relocated / "config")
        shutil.copytree(
            self.root / "session-hooks-plugin",
            relocated / "session-hooks-plugin",
        )
        moved = fleet_runtime.FleetRuntime(
            [str(relocated / "core/scripts/fleet-control")],
            [str(relocated / "adapter/scripts/fleet-herdr")],
            [str(relocated / "adapter/scripts/fleet-controller")],
            runner=runner,
        )

        resumed = moved.start(
            "review",
            [self.fleets],
            [self.profiles],
            self.state,
            str(self.root),
            "codex",
            execute=True,
            once=True,
        )

        self.assertEqual("resumed", resumed["status"])
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["execution_snapshot_root"], resumed["execution_snapshot_root"]
        )

    def test_active_resume_keeps_saved_hook_and_stopped_restart_uses_new_hook(self):
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
        resumed = updated_runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        self.assertEqual("resumed", resumed["status"])
        self.assertEqual(str(first_hook), resumed["hook_runtime"])
        self.assertEqual("print('v1')\n", first_hook.read_text(encoding="utf-8"))

        updated_runtime.stop("review", self.state, execute=True)
        restarted = updated_runtime.start(
            "review", [self.fleets], [self.profiles], self.state,
            str(self.root), "codex", execute=True, once=True,
        )
        self.assertEqual("started", restarted["status"])
        self.assertNotEqual(str(first_hook), restarted["hook_runtime"])
        self.assertEqual(
            "print('v2')\n",
            Path(restarted["hook_runtime"]).read_text(encoding="utf-8"),
        )

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
