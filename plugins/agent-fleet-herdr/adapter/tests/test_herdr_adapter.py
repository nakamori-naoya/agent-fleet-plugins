import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "herdr_adapter.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("herdr_adapter", MODULE_PATH)
herdr_adapter = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = herdr_adapter
SPEC.loader.exec_module(herdr_adapter)


def runtime(product="codex", command="codex", model="example-model", effort="medium"):
    return {"product": product, "command": command, "model": model,
            "effort": effort, "fallback": "fail"}


FLEET = {
    "apiVersion": "fleet.harness/v3", "kind": "Fleet",
    "metadata": {"id": "demo-fleet"},
    "spec": {
        "codex_hook_trust": "preapproved",
        "members": [
            {"agent_ref": "manager", "role_ref": "manager@1", "runtime": runtime()},
            {"agent_ref": "worker", "role_ref": "worker@1", "runtime": runtime()},
            {"agent_ref": "advisor", "role_ref": "advisor@1", "runtime": runtime()},
        ],
        "collaboration": {"manager": "manager"},
    },
}

VIEW_PROFILE = {
    "apiVersion": "fleet.herdr.harness/v2", "kind": "ViewProfile",
    "metadata": {"id": "local/role-columns", "version": 1},
    "spec": {
        "constraints": {"min_members": 3, "max_members": 7},
        "layout": {"type": "split", "direction": "horizontal", "children": [
            {"type": "stack", "id": "management", "selector": {"role_ids": ["manager"]}, "weight": 34, "direction": "vertical", "distribution": "equal"},
            {"type": "stack", "id": "workers", "selector": {"role_ids": ["worker"]}, "weight": 33, "direction": "vertical", "distribution": "equal"},
            {"type": "stack", "id": "support", "selector": {"remaining": True}, "weight": 33, "direction": "vertical", "distribution": "equal"},
        ]},
    },
}


class LayoutRunner:
    def __init__(self, *, wrong_width=False, missing_pane=False):
        self.calls = []
        self.next_pane = 2
        self.wrong_width = wrong_width
        self.missing_pane = missing_pane

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        command = list(argv[:3])
        if command == ["herdr", "workspace", "create"]:
            value = {"result": {"workspace": {"workspace_id": "w1"},
                      "tab": {"tab_id": "t1"}, "root_pane": {"pane_id": "p1"}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        if command == ["herdr", "pane", "split"]:
            pane = f"p{self.next_pane}"
            self.next_pane += 1
            value = {"result": {"pane": {"pane_id": pane}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        if command == ["herdr", "pane", "layout"]:
            panes = [
                {"pane_id": "p1", "rect": {"x": 0, "y": 0, "width": 102, "height": 100}},
                {"pane_id": "p2", "rect": {"x": 102, "y": 0, "width": 99, "height": 100}},
                {"pane_id": "p3", "rect": {"x": 201, "y": 0, "width": 99, "height": 100}},
            ]
            if self.missing_pane:
                panes.pop()
            if self.wrong_width:
                panes[0]["rect"]["width"] = 101
                panes[1]["rect"]["x"] = 101
                panes[1]["rect"]["width"] = 100
            value = {"result": {"layout": {
                "workspace_id": "w1", "tab_id": "t1", "zoomed": False,
                "area": {"x": 0, "y": 0, "width": 300, "height": 100},
                "panes": panes,
                "splits": [{"direction": "right", "ratio": 0.34},
                           {"direction": "right", "ratio": 0.5}],
            }}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")


class HerdrAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = herdr_adapter.AdapterState(Path(self.temp.name) / "herdr.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_uses_each_members_command_model_and_effort(self):
        fleet = json.loads(json.dumps(FLEET))
        fleet["spec"]["members"][0]["runtime"] = runtime(
            "claude", "claude", "claude-model", "high"
        )
        plan = herdr_adapter.HerdrAdapter(self.state).plan_provision(
            fleet, "/repo", "codex", VIEW_PROFILE
        )
        manager = next(op["argv"] for op in plan.operations if op["id"] == "agent.run:manager")
        worker = next(op["argv"] for op in plan.operations if op["id"] == "agent.run:worker")
        self.assertIn("claude", manager[-1])
        self.assertIn("claude-model", manager[-1])
        self.assertIn("--effort high", manager[-1])
        self.assertIn("codex", worker[-1])
        self.assertIn("model_reasoning_effort=", worker[-1])

    def test_dry_run_is_deterministic_and_creates_no_state(self):
        adapter = herdr_adapter.HerdrAdapter(self.state)
        first = adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE)
        second = adapter.provision(FLEET, "/repo", "codex", VIEW_PROFILE)
        self.assertEqual(first, second)
        self.assertEqual("planned", first["status"])
        self.assertEqual(3, len(first["plan"]["placements"]))
        self.assertEqual([], self.state.status("demo-fleet")["bindings"])

    def test_missing_member_runtime_is_rejected(self):
        fleet = json.loads(json.dumps(FLEET))
        fleet["spec"]["members"][1].pop("runtime")
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "runtime is required"):
            herdr_adapter.HerdrAdapter(self.state).plan_provision(
                fleet, "/repo", "codex", VIEW_PROFILE
            )

    def test_view_profile_member_constraint_is_checked_at_runtime(self):
        profile = json.loads(json.dumps(VIEW_PROFILE))
        profile["spec"]["constraints"]["min_members"] = 4
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "does not support 3 members"):
            herdr_adapter.HerdrAdapter(self.state).plan_provision(
                FLEET, "/repo", "codex", profile
            )

    def test_execute_saves_only_a_verified_one_to_one_layout(self):
        result = herdr_adapter.HerdrAdapter(
            self.state, runner=LayoutRunner()
        ).provision(FLEET, "/repo", "codex", VIEW_PROFILE, execute=True)
        self.assertEqual("provisioned", result["status"])
        self.assertEqual(3, len(result["bindings"]))
        self.assertEqual(3, len(self.state.status("demo-fleet")["placements"]))

    def test_execute_rejects_pane_count_mismatch_and_closes_workspace(self):
        runner = LayoutRunner(missing_pane=True)
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "pane count"):
            herdr_adapter.HerdrAdapter(self.state, runner=runner).provision(
                FLEET, "/repo", "codex", VIEW_PROFILE, execute=True
            )
        self.assertTrue(any(call[:3] == ["herdr", "workspace", "close"] for call in runner.calls))
        self.assertEqual([], self.state.status("demo-fleet")["bindings"])

    def test_execute_rejects_width_height_or_position_mismatch(self):
        runner = LayoutRunner(wrong_width=True)
        with self.assertRaisesRegex(herdr_adapter.HerdrAdapterError, "x/y/width/height"):
            herdr_adapter.HerdrAdapter(self.state, runner=runner).provision(
                FLEET, "/repo", "codex", VIEW_PROFILE, execute=True
            )
        self.assertEqual([], self.state.status("demo-fleet")["placements"])



if __name__ == "__main__":
    unittest.main()
