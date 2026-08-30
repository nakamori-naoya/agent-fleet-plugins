import importlib.util
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "fleet_control.py"
SPEC = importlib.util.spec_from_file_location("fleet_control", MODULE_PATH)
fleet_control = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(fleet_control)


NORMALIZED = {
    "apiVersion": "fleet.harness/v1",
    "kind": "Fleet",
    "metadata": {"id": "demo"},
    "spec": {
        "members": [
            {"agent_ref": "manager", "role_ref": "manager@1"},
            {"agent_ref": "worker-1", "role_ref": "worker@1"},
        ],
        "tasks": [
            {
                "id": "task-1",
                "assignee": "worker-1",
                "depends_on": [],
                "instructions": "Do the first task.",
            }
        ],
        "collaboration": {"manager": "manager"},
    },
}


class FleetStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "fleet.sqlite3"
        self.config = self.root / "fleet.yml"
        self.config.write_text("fixture: true\n", encoding="utf-8")
        self.validator = self.root / "validate_fleet.py"
        self.validator.write_text(
            "import json\n"
            f"print(json.dumps({NORMALIZED!r}))\n",
            encoding="utf-8",
        )
        self.validator.chmod(self.validator.stat().st_mode | stat.S_IXUSR)
        self.store = fleet_control.FleetStore(self.db)
        self.store.initialize(fleet_control.load_fleet_config(self.config, self.validator))

    def tearDown(self):
        self.temp.cleanup()

    def test_init_creates_logical_core_without_pane_identifiers(self):
        with sqlite3.connect(self.db) as db:
            columns = {
                row[1]
                for table in ("fleets", "members", "tasks", "events", "outbox")
                for row in db.execute(f"PRAGMA table_info({table})")
            }
        self.assertNotIn("pane_id", columns)
        self.assertEqual(2, len(self.store.status("demo")["members"]))

    def test_task_transition_and_explicit_terminal_report(self):
        self.store.assign("demo", "task-1", "worker-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        with self.assertRaisesRegex(fleet_control.FleetError, "payload is required"):
            self.store.transition_task("demo", "task-1", "completed", "worker-1")
        result = self.store.transition_task(
            "demo", "task-1", "completed", "worker-1", {"summary": "done"}
        )
        self.assertEqual("completed", result["status"])

    def test_invalid_task_transition_is_rejected(self):
        self.store.assign("demo", "task-1", "worker-1")
        with self.assertRaisesRegex(fleet_control.FleetError, "assigned -> completed"):
            self.store.transition_task(
                "demo", "task-1", "completed", "worker-1", {"summary": "too early"}
            )

    def test_task_cannot_be_assigned_against_declared_assignee(self):
        with self.assertRaisesRegex(fleet_control.FleetError, "declared for"):
            self.store.assign("demo", "task-1", "manager")

    def test_blocked_task_can_resume_running(self):
        self.store.assign("demo", "task-1", "worker-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.transition_task(
            "demo", "task-1", "blocked", "worker-1", {"reason": "approval"}
        )
        result = self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.assertEqual("running", result["status"])

    def test_only_assignee_can_report_task_state(self):
        self.store.assign("demo", "task-1", "worker-1")
        with self.assertRaisesRegex(fleet_control.FleetError, "only the assigned agent"):
            self.store.transition_task("demo", "task-1", "running", "manager")

    def test_member_schema_and_status_use_role_ref(self):
        with sqlite3.connect(self.db) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(members)")}
        self.assertIn("role_ref", columns)
        self.assertNotIn("role", columns)
        self.assertEqual("manager@1", self.store.status("demo")["members"][0]["role_ref"])

    def test_only_manager_can_enqueue_typed_command_and_target_can_ack(self):
        command = self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "cmd-1"
        )
        self.assertEqual("pending", command["status"])
        with self.assertRaisesRegex(fleet_control.FleetError, "only a fleet manager"):
            self.store.enqueue_command(
                "demo", "worker-1", "manager", "message.send", {"text": "no"}
            )
        with self.assertRaisesRegex(fleet_control.FleetError, "only the target"):
            self.store.acknowledge("demo", "cmd-1", "manager")
        self.assertEqual(
            "acknowledged", self.store.acknowledge("demo", "cmd-1", "worker-1")["status"]
        )

    def test_unknown_command_type_is_rejected(self):
        with self.assertRaisesRegex(fleet_control.FleetError, "unsupported command type"):
            self.store.enqueue_command("demo", "manager", "worker-1", "shell.exec", {})

    def test_status_exposes_pending_command_as_adapter_request_json(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "cmd-json"
        )
        request = self.store.status("demo")["outbox"][0]
        self.assertEqual("fleet.harness/v1", request["apiVersion"])
        self.assertEqual("Command", request["kind"])
        self.assertEqual("worker-1", request["spec"]["target"]["ref"])
        self.assertEqual("message.send", request["spec"]["type"])
        self.assertEqual({"text": "hello"}, request["spec"]["payload"])

    def test_status_exposes_events_with_frozen_api_version(self):
        event = self.store.status("demo")["events"][0]
        self.assertEqual("fleet.harness/v1", event["apiVersion"])
        self.assertEqual("Event", event["kind"])

    def test_validator_failure_never_falls_back_to_yaml_parse(self):
        failing = self.root / "failing_validator.py"
        failing.write_text(
            "import sys\nprint('invalid reference', file=sys.stderr)\nraise SystemExit(2)\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(fleet_control.FleetError, "invalid reference"):
            fleet_control.load_fleet_config(self.config, failing)


if __name__ == "__main__":
    unittest.main()
