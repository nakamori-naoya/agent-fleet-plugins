import importlib.util
import json
import sqlite3
import subprocess
import stat
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "herdr_adapter.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("herdr_adapter", MODULE_PATH)
herdr_adapter = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = herdr_adapter
SPEC.loader.exec_module(herdr_adapter)


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


FLEET = {
    "apiVersion": "fleet.harness/v1",
    "kind": "Fleet",
    "metadata": {"id": "demo-fleet"},
    "spec": {
        "members": [
            {"agent_ref": "manager-1", "role_ref": "manager@1"},
            {"agent_ref": "worker-1", "role_ref": "worker@1"},
            {"agent_ref": "worker-2", "role_ref": "worker@1"},
        ],
        "collaboration": {"manager": "manager-1"},
        "runtime": {"provider": "herdr", "codex_hook_trust": "preapproved"},
        "view": {"profile_ref": "local/test-deck@1"},
    },
}

VIEW_PROFILE = {
    "apiVersion": "fleet.herdr.harness/v1",
    "kind": "ViewProfile",
    "metadata": {"id": "local/test-deck", "version": 1},
    "spec": {
        "constraints": {"min_members": 2, "max_members": 5},
        "layout": {
            "type": "split",
            "direction": "horizontal",
            "children": [
                {"type": "slot", "selector": "manager", "weight": 32, "pane_slot": "left"},
                {"type": "stack", "selector": "non-manager", "weight": 68,
                 "direction": "vertical", "distribution": "equal", "pane_slot_prefix": "right"},
            ],
        },
    },
}


class HerdrAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = herdr_adapter.AdapterState(Path(self.temp.name) / "herdr.sqlite3")
        self.state.bind("worker-1", "w1", "t1", "p1", "codex-worker")

    def tearDown(self):
        self.temp.cleanup()

    def test_runtime_binding_and_view_placement_are_adapter_state(self):
        binding = self.state.resolve("worker-1")
        self.assertEqual("p1", binding.pane_id)
        placement = self.state.place_view(
            "worker-1",
            "main",
            "workers",
            "right",
            {"rank": 1},
            profile_ref="local/test-deck@1",
        )
        self.assertEqual("right", placement["pane_slot"])
        db_path = Path(self.state.db_path)
        self.assertEqual(0o600, stat.S_IMODE(db_path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(db_path.parent.stat().st_mode))

    def test_same_agent_ref_in_two_fleets_resolves_to_each_fleets_pane(self):
        self.state.bind("worker-shared", "wa", "ta", "pa", fleet_id="fleet-a")
        self.state.bind("worker-shared", "wb", "tb", "pb", fleet_id="fleet-b")

        self.assertEqual("pa", self.state.resolve("worker-shared", "fleet-a").pane_id)
        self.assertEqual("pb", self.state.resolve("worker-shared", "fleet-b").pane_id)

    def test_provision_dry_run_is_deterministic_command_deck_plan(self):
        runner = FakeRunner([])
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)
        first = adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE)
        second = adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE)
        self.assertEqual(first, second)
        self.assertEqual("dry-run", first["mode"])
        self.assertEqual([], runner.calls)
        operations = first["plan"]["operations"]
        self.assertEqual(
            [
                "herdr", "workspace", "create", "--cwd", "/repo",
                "--label", "agent-fleet:demo-fleet:<operation-id>",
                "--env", "AGENT_FLEET_CODEX_HOOK_TRUST=preapproved", "--no-focus",
            ],
            operations[0]["argv"],
        )
        self.assertEqual(
            ["herdr", "pane", "split", "$workspace.root_pane", "--direction", "right"],
            operations[2]["argv"][:6],
        )
        self.assertIn("0.68", operations[2]["argv"])
        self.assertEqual("down", operations[4]["argv"][5])
        self.assertEqual("left", first["plan"]["placements"][0]["pane_slot"])
        self.assertEqual("right.2", first["plan"]["placements"][2]["pane_slot"])
        self.assertEqual("local/test-deck@1", first["plan"]["profile_ref"])

    def test_provision_plan_puts_trusted_core_location_in_every_agent_shell(self):
        environment = {
            "AGENT_FLEET_CORE_COMMAND": "/opt/agent-fleet/fleet-control",
            "AGENT_FLEET_CORE_DB": "/state/demo/core.sqlite3",
        }

        plan = herdr_adapter.HerdrAdapter(self.state).plan_provision(
            FLEET, "/repo", "codex", VIEW_PROFILE, environment
        )

        shell_operations = [
            operation
            for operation in plan.operations
            if operation["id"] == "workspace.create"
            or operation["id"].startswith("pane.split:")
        ]
        for operation in shell_operations:
            argv = operation["argv"]
            self.assertIn(
                "AGENT_FLEET_CORE_COMMAND=/opt/agent-fleet/fleet-control", argv
            )
            self.assertIn("AGENT_FLEET_CORE_DB=/state/demo/core.sqlite3", argv)
            self.assertIn("AGENT_FLEET_CODEX_HOOK_TRUST=preapproved", argv)

    def test_member_model_is_forwarded_to_supported_agent(self):
        fleet = json.loads(json.dumps(FLEET))
        fleet["spec"]["members"][0]["model"] = "gpt-5.6-sol"

        plan = herdr_adapter.HerdrAdapter(self.state).plan_provision(
            fleet, "/repo", "codex", VIEW_PROFILE
        )

        manager_start = next(
            operation for operation in plan.operations
            if operation["id"] == "agent.start:manager-1"
        )
        self.assertEqual(
            ["--", "--dangerously-bypass-hook-trust", "--model", "gpt-5.6-sol"],
            manager_start["argv"][-4:],
        )

    def test_codex_fleet_preapproves_hook_trust_without_bypassing_other_approvals(self):
        plan = herdr_adapter.HerdrAdapter(self.state).plan_provision(
            FLEET, "/repo", "codex", VIEW_PROFILE
        )

        starts = [
            operation["argv"]
            for operation in plan.operations
            if operation["id"].startswith("agent.start:")
        ]
        for argv in starts:
            self.assertEqual(["--", "--dangerously-bypass-hook-trust"], argv[-2:])
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertNotIn("--approve-for-me", argv)

    def test_codex_hook_review_can_be_restored_per_fleet(self):
        fleet = json.loads(json.dumps(FLEET))
        fleet["spec"]["runtime"]["codex_hook_trust"] = "review"

        plan = herdr_adapter.HerdrAdapter(self.state).plan_provision(
            fleet, "/repo", "codex", VIEW_PROFILE
        )

        starts = [
            operation["argv"]
            for operation in plan.operations
            if operation["id"].startswith("agent.start:")
        ]
        self.assertTrue(all("--dangerously-bypass-hook-trust" not in argv for argv in starts))
        shells = [
            operation["argv"]
            for operation in plan.operations
            if operation["id"] == "workspace.create"
            or operation["id"].startswith("pane.split:")
        ]
        self.assertTrue(
            all("AGENT_FLEET_CODEX_HOOK_TRUST=review" in argv for argv in shells)
        )

    def test_codex_hook_review_is_the_safe_default(self):
        fleet = json.loads(json.dumps(FLEET))
        del fleet["spec"]["runtime"]["codex_hook_trust"]

        plan = herdr_adapter.HerdrAdapter(self.state).plan_provision(
            fleet, "/repo", "codex", VIEW_PROFILE
        )

        starts = [
            operation["argv"]
            for operation in plan.operations
            if operation["id"].startswith("agent.start:")
        ]
        self.assertTrue(all("--dangerously-bypass-hook-trust" not in argv for argv in starts))
        workspace = next(
            operation["argv"]
            for operation in plan.operations
            if operation["id"] == "workspace.create"
        )
        self.assertIn("AGENT_FLEET_CODEX_HOOK_TRUST=review", workspace)

    def test_layout_tree_compiles_equal_three_member_stack_to_sequential_ratios(self):
        fleet = json.loads(json.dumps(FLEET))
        fleet["spec"]["members"].append(
            {"agent_ref": "worker-3", "role_ref": "worker@1"}
        )
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=FakeRunner([]))
        plan = adapter.provision(fleet, "/repo", "codex", VIEW_PROFILE)["plan"]
        splits = [
            operation
            for operation in plan["operations"]
            if operation["id"].startswith("pane.split:")
        ]
        self.assertIn(str(2 / 3), splits[1]["argv"])
        self.assertIn("0.5", splits[2]["argv"])

    def test_provision_rejects_profile_identity_mismatch(self):
        profile = json.loads(json.dumps(VIEW_PROFILE))
        profile["metadata"]["version"] = 2
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=FakeRunner([]))
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "does not match"):
            adapter.provision(FLEET, "/repo", "codex", profile)

    def _save_existing_fleet(self):
        for index, agent_ref in enumerate(("manager-1", "worker-1", "worker-2")):
            self.state.bind(
                agent_ref,
                "w-existing",
                "t-existing",
                f"p-existing-{index}",
                fleet_id="demo-fleet",
            )
            self.state.place_view(
                agent_ref,
                "demo-fleet",
                "local/test-deck@1",
                "left" if index == 0 else f"right.{index}",
                fleet_id="demo-fleet",
                profile_ref="local/test-deck@1",
            )

    def test_provision_is_idempotent_for_same_existing_profile_and_members(self):
        self._save_existing_fleet()
        runner = FakeRunner([])
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)
        result = adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE, execute=True)
        self.assertEqual("already_provisioned", result["status"])
        self.assertEqual([], runner.calls)

    def test_provision_rejects_existing_fleet_with_different_profile(self):
        self._save_existing_fleet()
        fleet = json.loads(json.dumps(FLEET))
        fleet["spec"]["view"]["profile_ref"] = "local/test-deck@2"
        profile = json.loads(json.dumps(VIEW_PROFILE))
        profile["metadata"]["version"] = 2
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=FakeRunner([]))
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "Profile conflict"):
            adapter.provision(fleet, "/repo", "codex", profile, execute=True)

    def test_provision_execute_parses_ids_and_saves_bindings_and_views(self):
        workspace = json.dumps(
            {
                "result": {
                    "workspace": {"workspace_id": "w-created"},
                    "tab": {"tab_id": "t-created"},
                    "root_pane": {"pane_id": "p-manager"},
                }
            }
        )
        runner = FakeRunner(
            [
                completed(stdout=workspace),
                completed(stdout="started"),
                completed(stdout=json.dumps({"result": {"pane": {"pane_id": "p-worker-1"}}})),
                completed(stdout="started"),
                completed(stdout="p-worker-2\n"),
                completed(stdout="started"),
            ]
        )
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)
        result = adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE, execute=True)
        self.assertEqual("provisioned", result["status"])
        self.assertEqual("p-manager", self.state.resolve("manager-1", "demo-fleet").pane_id)
        self.assertEqual("p-worker-1", self.state.resolve("worker-1", "demo-fleet").pane_id)
        self.assertEqual("p-worker-2", self.state.resolve("worker-2", "demo-fleet").pane_id)
        self.assertEqual(
            [
                "herdr",
                "agent",
                "start",
                "manager-1",
                "--kind",
                "codex",
                "--pane",
                "p-manager",
                "--",
                "--dangerously-bypass-hook-trust",
            ],
            runner.calls[1][0],
        )
        self.assertEqual("p-worker-1", runner.calls[4][0][3])
        with closing(sqlite3.connect(self.state.db_path)) as db:
            placements = db.execute(
                "SELECT agent_ref,pane_slot FROM view_placements ORDER BY agent_ref"
            ).fetchall()
        self.assertEqual(
            [("manager-1", "left"), ("worker-1", "right.1"), ("worker-2", "right.2")],
            placements,
        )

        closer = FakeRunner([completed(stdout="closed")])
        stopped = herdr_adapter.HerdrAdapter(self.state, runner=closer).deprovision(
            "demo-fleet", execute=True
        )
        self.assertEqual("deprovisioned", stopped["status"])
        self.assertEqual(
            ["herdr", "workspace", "close", "w-created"], closer.calls[0][0]
        )
        self.assertEqual([], self.state.status("demo-fleet")["bindings"])

    def test_two_direct_provisions_of_same_fleet_create_only_one_workspace(self):
        workspace = json.dumps(
            {
                "result": {
                    "workspace": {"workspace_id": "w-locked"},
                    "tab": {"tab_id": "t-locked"},
                    "root_pane": {"pane_id": "p-manager"},
                }
            }
        )
        entered = threading.Event()
        release = threading.Event()

        class BlockingRunner(FakeRunner):
            def __call__(self, argv, **kwargs):
                if not self.calls:
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError("test did not release workspace creation")
                return super().__call__(argv, **kwargs)

        class ProbeRunner(FakeRunner):
            def __init__(self):
                super().__init__([])
                self.called = threading.Event()

            def __call__(self, argv, **kwargs):
                self.called.set()
                return completed(returncode=1, stderr="duplicate external call")

        first_runner = BlockingRunner(
            [
                completed(stdout=workspace),
                completed(stdout="started"),
                completed(stdout=json.dumps({"result": {"pane": {"pane_id": "p-worker-1"}}})),
                completed(stdout="started"),
                completed(stdout=json.dumps({"result": {"pane": {"pane_id": "p-worker-2"}}})),
                completed(stdout="started"),
            ]
        )
        second_runner = ProbeRunner()
        first_adapter = herdr_adapter.HerdrAdapter(self.state, runner=first_runner)
        second_adapter = herdr_adapter.HerdrAdapter(self.state, runner=second_runner)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                first_adapter.provision,
                FLEET,
                "/repo",
                "codex",
                VIEW_PROFILE,
                execute=True,
            )
            self.assertTrue(entered.wait(1))
            second = executor.submit(
                second_adapter.provision,
                FLEET,
                "/repo",
                "codex",
                VIEW_PROFILE,
                execute=True,
            )
            self.assertFalse(second_runner.called.wait(0.1))
            release.set()
            first_result = first.result(timeout=3)
            second_result = second.result(timeout=3)

        self.assertEqual("provisioned", first_result["status"])
        self.assertEqual("already_provisioned", second_result["status"])
        self.assertFalse(second_runner.called.is_set())

    def test_deprovision_clears_state_when_workspace_is_already_absent(self):
        self._save_existing_fleet()
        missing = FakeRunner(
            [completed(returncode=1, stderr='{"error":{"code":"workspace_not_found"}}')]
        )

        result = herdr_adapter.HerdrAdapter(
            self.state, runner=missing
        ).deprovision("demo-fleet", execute=True)

        self.assertEqual("deprovisioned", result["status"])
        self.assertEqual([], self.state.status("demo-fleet")["bindings"])

    def test_deprovision_recovers_workspace_known_only_by_creation_intent(self):
        self.state.save_workspace_intent(
            "demo-fleet", "agent-fleet:demo-fleet"
        )
        workspace_list = json.dumps(
            {
                "result": {
                    "workspaces": [
                        {
                            "label": "agent-fleet:demo-fleet",
                            "workspace_id": "w-unrecorded",
                        }
                    ]
                }
            }
        )
        runner = FakeRunner(
            [completed(stdout=workspace_list), completed(stdout="closed")]
        )

        result = herdr_adapter.HerdrAdapter(
            self.state, runner=runner
        ).deprovision("demo-fleet", execute=True)

        self.assertEqual("deprovisioned", result["status"])
        self.assertEqual("w-unrecorded", result["workspace_id"])
        self.assertEqual(["herdr", "workspace", "list"], runner.calls[0][0])
        self.assertEqual(
            ["herdr", "workspace", "close", "w-unrecorded"], runner.calls[1][0]
        )
        self.assertIsNone(self.state.provisioning_intent("demo-fleet"))

    def test_interrupted_provision_closes_recorded_workspace_before_retry(self):
        workspace = json.dumps(
            {
                "result": {
                    "workspace": {"workspace_id": "w-orphan"},
                    "tab": {"tab_id": "t-orphan"},
                    "root_pane": {"pane_id": "p-orphan"},
                }
            }
        )
        first = FakeRunner(
            [completed(stdout=workspace), completed(returncode=1, stderr="start failed")]
        )
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "start failed"):
            herdr_adapter.HerdrAdapter(self.state, runner=first).provision(
                FLEET, "/repo", "codex", VIEW_PROFILE, execute=True
            )
        self.assertEqual(
            "workspace_created",
            self.state.provisioning_journal("demo-fleet")["state"],
        )

        retry = FakeRunner(
            [
                completed(stdout="closed"),
                completed(stdout=workspace.replace("w-orphan", "w-retry")),
                completed(returncode=1, stderr="retry stopped"),
            ]
        )
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "retry stopped"):
            herdr_adapter.HerdrAdapter(self.state, runner=retry).provision(
                FLEET, "/repo", "codex", VIEW_PROFILE, execute=True
            )
        self.assertEqual(
            ["herdr", "workspace", "close", "w-orphan"], retry.calls[0][0]
        )

    def test_retry_recovers_workspace_created_before_journal_was_saved(self):
        workspace = json.dumps(
            {
                "result": {
                    "workspace": {"workspace_id": "w-unrecorded"},
                    "tab": {"tab_id": "t-unrecorded"},
                    "root_pane": {"pane_id": "p-unrecorded"},
                }
            }
        )
        original_save = self.state.save_workspace_journal

        def fail_to_save(*_args):
            raise RuntimeError("simulated process boundary")

        self.state.save_workspace_journal = fail_to_save
        with self.assertRaisesRegex(RuntimeError, "simulated process boundary"):
            herdr_adapter.HerdrAdapter(
                self.state, runner=FakeRunner([completed(stdout=workspace)])
            ).provision(FLEET, "/repo", "codex", VIEW_PROFILE, execute=True)
        self.state.save_workspace_journal = original_save
        intent_label = self.state.provisioning_intent("demo-fleet")["workspace_label"]
        self.assertRegex(intent_label, r"^agent-fleet:demo-fleet:[0-9a-f]{32}$")

        workspace_list = json.dumps(
            {
                "result": {
                    "type": "workspace_list",
                    "workspaces": [
                        {"label": "other", "workspace_id": "w-other"},
                        {
                            "label": intent_label,
                            "workspace_id": "w-unrecorded",
                        },
                    ],
                }
            }
        )
        retry = FakeRunner(
            [
                completed(stdout=workspace_list),
                completed(stdout="closed"),
                completed(stdout=workspace.replace("w-unrecorded", "w-retry")),
                completed(returncode=1, stderr="retry stopped"),
            ]
        )
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "retry stopped"):
            herdr_adapter.HerdrAdapter(self.state, runner=retry).provision(
                FLEET, "/repo", "codex", VIEW_PROFILE, execute=True
            )
        self.assertEqual(["herdr", "workspace", "list"], retry.calls[0][0])
        self.assertEqual(
            ["herdr", "workspace", "close", "w-unrecorded"], retry.calls[1][0]
        )
        self.assertIsNone(self.state.provisioning_intent("demo-fleet"))

    def test_workspace_intent_recovery_rejects_ambiguous_labels(self):
        self.state.save_workspace_intent(
            "demo-fleet", "agent-fleet:demo-fleet"
        )
        duplicate_list = json.dumps(
            {
                "result": {
                    "workspaces": [
                        {
                            "label": "agent-fleet:demo-fleet",
                            "workspace_id": "w-one",
                        },
                        {
                            "label": "agent-fleet:demo-fleet",
                            "workspace_id": "w-two",
                        },
                    ]
                }
            }
        )
        adapter = herdr_adapter.HerdrAdapter(
            self.state, runner=FakeRunner([completed(stdout=duplicate_list)])
        )
        with self.assertRaisesRegex(
            herdr_adapter.HerdrAdapterError, "multiple Herdr workspaces"
        ):
            adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE, execute=True)

    def test_provision_retries_agent_start_when_new_split_pane_is_temporarily_busy(self):
        workspace = json.dumps(
            {
                "result": {
                    "workspace": {"workspace_id": "w-created"},
                    "tab": {"tab_id": "t-created"},
                    "root_pane": {"pane_id": "p-manager"},
                }
            }
        )
        runner = FakeRunner(
            [
                completed(stdout=workspace),
                completed(stdout="started"),
                completed(stdout=json.dumps({"result": {"pane": {"pane_id": "p-worker-1"}}})),
                completed(returncode=1, stderr="agent_pane_busy"),
                completed(stdout="started"),
                completed(stdout=json.dumps({"result": {"pane": {"pane_id": "p-worker-2"}}})),
                completed(stdout="started"),
            ]
        )
        retry_delays = []
        adapter = herdr_adapter.HerdrAdapter(
            self.state, runner=runner, sleeper=retry_delays.append
        )

        result = adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE, execute=True)

        self.assertEqual("provisioned", result["status"])
        worker_1_start = [
            call for call, _ in runner.calls if call[:4] == ["herdr", "agent", "start", "worker-1"]
        ]
        worker_2_start = [
            call for call, _ in runner.calls if call[:4] == ["herdr", "agent", "start", "worker-2"]
        ]
        self.assertEqual(2, len(worker_1_start))
        self.assertEqual(1, len(worker_2_start))
        self.assertEqual([1.0], retry_delays)

    def test_provision_stops_retrying_new_split_pane_after_three_busy_results(self):
        workspace = json.dumps(
            {
                "result": {
                    "workspace": {"workspace_id": "w-created"},
                    "tab": {"tab_id": "t-created"},
                    "root_pane": {"pane_id": "p-manager"},
                }
            }
        )
        runner = FakeRunner(
            [
                completed(stdout=workspace),
                completed(stdout="started"),
                completed(stdout=json.dumps({"result": {"pane": {"pane_id": "p-worker-1"}}})),
                completed(returncode=1, stderr="agent_pane_busy"),
                completed(returncode=1, stderr="agent_pane_busy"),
                completed(returncode=1, stderr="agent_pane_busy"),
            ]
        )
        retry_delays = []
        adapter = herdr_adapter.HerdrAdapter(
            self.state, runner=runner, sleeper=retry_delays.append
        )

        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "agent_pane_busy"):
            adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE, execute=True)

        worker_starts = [
            call for call, _ in runner.calls if call[:4] == ["herdr", "agent", "start", "worker-1"]
        ]
        self.assertEqual(3, len(worker_starts))
        self.assertEqual([1.0, 1.0], retry_delays)

    def test_provision_retries_manager_start_when_root_pane_is_temporarily_busy(self):
        workspace = json.dumps(
            {
                "result": {
                    "workspace": {"workspace_id": "w-created"},
                    "tab": {"tab_id": "t-created"},
                    "root_pane": {"pane_id": "p-manager"},
                }
            }
        )
        runner = FakeRunner(
            [
                completed(stdout=workspace),
                completed(returncode=1, stderr="agent_pane_busy"),
                completed(stdout="started"),
                completed(stdout=json.dumps({"result": {"pane": {"pane_id": "p-worker-1"}}})),
                completed(stdout="started"),
                completed(stdout=json.dumps({"result": {"pane": {"pane_id": "p-worker-2"}}})),
                completed(stdout="started"),
            ]
        )
        retry_delays = []
        adapter = herdr_adapter.HerdrAdapter(
            self.state, runner=runner, sleeper=retry_delays.append
        )

        result = adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE, execute=True)

        manager_starts = [
            call for call, _ in runner.calls if call[:4] == ["herdr", "agent", "start", "manager-1"]
        ]
        self.assertEqual("provisioned", result["status"])
        self.assertEqual(2, len(manager_starts))
        self.assertEqual([1.0], retry_delays)

    def test_provision_does_not_retry_other_new_pane_start_errors(self):
        workspace = json.dumps(
            {
                "result": {
                    "workspace": {"workspace_id": "w-created"},
                    "tab": {"tab_id": "t-created"},
                    "root_pane": {"pane_id": "p-manager"},
                }
            }
        )
        runner = FakeRunner(
            [
                completed(stdout=workspace),
                completed(stdout="started"),
                completed(stdout=json.dumps({"result": {"pane": {"pane_id": "p-worker-1"}}})),
                completed(returncode=1, stderr="server unavailable"),
            ]
        )
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)

        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "server unavailable"):
            adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE, execute=True)

        worker_starts = [
            call for call, _ in runner.calls if call[:4] == ["herdr", "agent", "start", "worker-1"]
        ]
        self.assertEqual(1, len(worker_starts))

    def test_provision_unparseable_output_does_not_save_new_bindings(self):
        runner = FakeRunner([completed(stdout='{"result":{"workspace":{}}}')])
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "bindings were not saved"):
            adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE, execute=True)
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "is not bound"):
            self.state.resolve("manager-1", "demo-fleet")

    def test_provision_rejects_unvalidated_runtime_or_view_contract(self):
        invalid = json.loads(json.dumps(FLEET))
        invalid["spec"]["view"] = {"profile_ref": "local/tiled@1"}
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=FakeRunner([]))
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "does not match"):
            adapter.provision(invalid, "/repo", "codex", VIEW_PROFILE)

    def test_status_is_read_only_and_reports_profile_bindings_and_placements(self):
        self.state.bind(
            "worker-status", "w-status", "t-status", "p-status", fleet_id="demo"
        )
        self.state.place_view(
            "worker-status",
            "main",
            "members",
            "right.1",
            {"profile_ref": "local/test-deck@1"},
            fleet_id="demo",
            profile_ref="local/test-deck@1",
        )
        before = Path(self.state.db_path).read_bytes()
        status = self.state.status("demo")
        after = Path(self.state.db_path).read_bytes()
        self.assertEqual(before, after)
        self.assertEqual("local/test-deck@1", status["profile_ref"])
        self.assertEqual("worker-status", status["bindings"][0]["agent_ref"])
        self.assertEqual("worker-status", status["placements"][0]["agent_ref"])

    def test_cli_status_uses_existing_adapter_state(self):
        self.state.bind("worker-status", "ws", "ts", "ps", fleet_id="demo-status")
        self.assertEqual(
            0,
            herdr_adapter.main(
                ["--state-db", self.state.db_path, "status", "--fleet", "demo-status"]
            ),
        )

    def test_provision_dry_run_does_not_create_requested_state_database(self):
        state_path = Path(self.temp.name) / "missing" / "herdr.sqlite3"
        self.assertEqual(
            0,
            herdr_adapter.main(
                [
                    "--state-db",
                    str(state_path),
                    "provision",
                    "--fleet-json",
                    json.dumps(FLEET),
                    "--view-profile-json",
                    json.dumps(VIEW_PROFILE),
                    "--cwd",
                    "/repo",
                    "--agent-kind",
                    "codex",
                ]
            ),
        )
        self.assertFalse(state_path.exists())
        self.assertFalse(state_path.parent.exists())

    def test_command_types_include_context_sync(self):
        self.assertIn("context.sync", herdr_adapter.COMMAND_TYPES)

    def test_dry_run_is_default_and_does_not_call_runner(self):
        runner = FakeRunner([])
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)
        result = adapter.dispatch(
            "worker-1", "cmd-1", "message.send", {"text": "hello; rm -rf /"}
        )
        self.assertEqual("dry-run", result["mode"])
        self.assertEqual([], runner.calls)
        argv = result["plan"]["command_argv"]
        self.assertEqual("p1", argv[3])
        self.assertIn("hello; rm -rf /", argv[4])

    def test_execute_checks_pane_then_dispatches_once(self):
        runner = FakeRunner([completed(), completed(stdout="ok")])
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)
        result = adapter.dispatch(
            "worker-1", "cmd-1", "task.assign", {"task_id": "task-1"}, execute=True
        )
        self.assertEqual("submitted", result["status"])
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(["herdr", "pane", "get", "p1"], runner.calls[0][0])
        self.assertEqual(["herdr", "agent", "prompt", "p1"], runner.calls[1][0][:4])

    def test_missing_pane_is_detected_and_requires_rebind(self):
        runner = FakeRunner([completed(returncode=1, stderr="not found")])
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "run bind/rebind"):
            adapter.dispatch(
                "worker-1", "cmd-1", "fleet.reconcile", {}, execute=True
            )
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "is lost"):
            self.state.resolve("worker-1")

    def test_prompt_timeout_is_unknown_and_never_retried(self):
        timeout = subprocess.TimeoutExpired(["herdr", "agent", "prompt"], 30)
        runner = FakeRunner([completed(), timeout])
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)
        result = adapter.dispatch(
            "worker-1", "cmd-1", "message.send", {"text": "hello"}, execute=True
        )
        self.assertEqual("unknown", result["status"])
        self.assertEqual(1, result["attempts"])
        self.assertEqual(2, len(runner.calls))

    def test_transient_pane_check_error_does_not_destroy_binding(self):
        runner = FakeRunner([completed(returncode=1, stderr="server unavailable")])
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "left unchanged"):
            adapter.dispatch("worker-1", "cmd-1", "fleet.reconcile", {}, execute=True)
        self.assertEqual("bound", self.state.resolve("worker-1").status)

    def test_argv_builder_rejects_invalid_direction_and_nul(self):
        commands = herdr_adapter.Herdr08Commands()
        with self.assertRaises(herdr_adapter.HerdrAdapterError):
            commands.pane_split("p1", "left", "/tmp")
        with self.assertRaises(herdr_adapter.HerdrAdapterError):
            commands.agent_prompt("p1", "bad\x00prompt", 10)

    def test_cli_accepts_core_request_json_without_core_import(self):
        state_path = Path(self.temp.name) / "cli.sqlite3"
        self.assertEqual(
            0,
            herdr_adapter.main(
                [
                    "--state-db",
                    str(state_path),
                    "bind",
                    "--agent-ref",
                    "worker-json",
                    "--fleet",
                    "demo",
                    "--workspace",
                    "w1",
                    "--tab",
                    "t1",
                    "--pane",
                    "p-json",
                ]
            ),
        )
        request = (
            '{"apiVersion":"fleet.harness/v1","kind":"Command",'
            '"metadata":{"id":"cmd-json","fleet_id":"demo","timestamp":"2026-08-30T00:00:00Z"},'
            '"spec":{"source":{"type":"member","ref":"manager"},'
            '"target":{"type":"member","ref":"worker-json"},'
            '"type":"message.send","payload":{"text":"hello"}}}'
        )
        self.assertEqual(
            0,
            herdr_adapter.main(
                ["--state-db", str(state_path), "dispatch", "--request-json", request]
            ),
        )


if __name__ == "__main__":
    unittest.main()
