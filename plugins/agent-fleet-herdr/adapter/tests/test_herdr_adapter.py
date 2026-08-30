import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "herdr_adapter.py"
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
        "runtime": {"provider": "herdr"},
        "view": {"profile": "command-deck"},
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
        placement = self.state.place_view("worker-1", "main", "workers", "right", {"rank": 1})
        self.assertEqual("right", placement["pane_slot"])

    def test_provision_dry_run_is_deterministic_command_deck_plan(self):
        runner = FakeRunner([])
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)
        first = adapter.provision(FLEET, "/repo", "codex")
        second = adapter.provision(FLEET, "/repo", "codex")
        self.assertEqual(first, second)
        self.assertEqual("dry-run", first["mode"])
        self.assertEqual([], runner.calls)
        operations = first["plan"]["operations"]
        self.assertEqual(
            ["herdr", "workspace", "create", "--cwd", "/repo", "--label", "demo-fleet", "--no-focus"],
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
        result = adapter.provision(FLEET, "/repo", "codex", execute=True)
        self.assertEqual("provisioned", result["status"])
        self.assertEqual("p-manager", self.state.resolve("manager-1").pane_id)
        self.assertEqual("p-worker-1", self.state.resolve("worker-1").pane_id)
        self.assertEqual("p-worker-2", self.state.resolve("worker-2").pane_id)
        self.assertEqual(
            ["herdr", "agent", "start", "manager-1", "--kind", "codex", "--pane", "p-manager"],
            runner.calls[1][0],
        )
        self.assertEqual("p-worker-1", runner.calls[4][0][3])
        with sqlite3.connect(self.state.db_path) as db:
            placements = db.execute(
                "SELECT agent_ref,pane_slot FROM view_placements ORDER BY agent_ref"
            ).fetchall()
        self.assertEqual(
            [("manager-1", "left"), ("worker-1", "right.1"), ("worker-2", "right.2")],
            placements,
        )

    def test_provision_unparseable_output_does_not_save_new_bindings(self):
        runner = FakeRunner([completed(stdout='{"result":{"workspace":{}}}')])
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=runner)
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "bindings were not saved"):
            adapter.provision(FLEET, "/repo", "codex", execute=True)
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "is not bound"):
            self.state.resolve("manager-1")

    def test_provision_rejects_unvalidated_runtime_or_view_contract(self):
        invalid = json.loads(json.dumps(FLEET))
        invalid["spec"]["view"] = {"profile": "tiled"}
        adapter = herdr_adapter.HerdrAdapter(self.state, runner=FakeRunner([]))
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "command-deck"):
            adapter.provision(invalid, "/repo", "codex")

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
