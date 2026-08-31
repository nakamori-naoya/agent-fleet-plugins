import importlib.util
import copy
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock
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
        "objective": "Complete the demo safely.",
        "completion_criteria": ["The manager accepts the task evidence."],
        "stop_conditions": ["An irreversible side effect becomes uncertain."],
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
                "expected_output": "A verified result.",
                "completion_criteria": ["The result includes test evidence."],
            }
        ],
        "collaboration": {"manager": "manager"},
        "view": {"profile_ref": "local/test-deck@1"},
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
        self.store.confirm_context("demo", "manager", 1)
        self.store.confirm_context("demo", "worker-1", 1)

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
        self.assertEqual(
            "local/test-deck@1", self.store.status("demo")["fleet"]["profile_ref"]
        )

    def test_same_fleet_config_initialization_is_idempotent(self):
        result = self.store.initialize(NORMALIZED)
        self.assertTrue(result["idempotent"])
        changed = copy.deepcopy(NORMALIZED)
        changed["spec"]["objective"] = "A different objective"
        with self.assertRaisesRegex(fleet_control.FleetError, "different configuration"):
            self.store.initialize(changed)

    def test_spec_validate_cli_returns_normalized_fleet_without_creating_database(self):
        cli_db = self.root / "validate-only.sqlite3"
        with mock.patch.object(
            fleet_control,
            "load_fleet_config",
            return_value=NORMALIZED,
        ):
            stdout = __import__("io").StringIO()
            with mock.patch("sys.stdout", stdout):
                result = fleet_control.main(
                    ["--db", str(cli_db), "spec.validate", "--config", str(self.config)]
                )
        self.assertEqual(0, result)
        self.assertEqual(NORMALIZED, __import__("json").loads(stdout.getvalue())["result"])
        self.assertFalse(cli_db.exists())

    def test_task_transition_and_explicit_terminal_report(self):
        self.store.assign("demo", "task-1", "worker-1", "manager")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        with self.assertRaisesRegex(fleet_control.FleetError, "payload is required"):
            self.store.transition_task("demo", "task-1", "completed", "worker-1")
        result = self.store.transition_task(
            "demo", "task-1", "completed", "worker-1", {"summary": "done"}
        )
        self.assertEqual("reported", result["status"])

    def test_invalid_task_transition_is_rejected(self):
        self.store.assign("demo", "task-1", "worker-1", "manager")
        with self.assertRaisesRegex(fleet_control.FleetError, "assigned -> reported"):
            self.store.transition_task(
                "demo", "task-1", "completed", "worker-1", {"summary": "too early"}
            )

    def test_task_cannot_be_assigned_against_declared_assignee(self):
        with self.assertRaisesRegex(fleet_control.FleetError, "declared for"):
            self.store.assign("demo", "task-1", "manager", "manager")

    def test_deterministic_assignment_and_outbox_commands_are_idempotent(self):
        first = self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:1"
        )
        second = self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:1"
        )
        self.assertEqual(first["command_id"], second["command_id"])
        self.assertTrue(second["idempotent"])
        initial = self.store.enqueue_command(
            "demo",
            "manager",
            "worker-1",
            "context.sync",
            {"reason": "start"},
            "sync:1",
        )
        repeated = self.store.enqueue_command(
            "demo",
            "manager",
            "worker-1",
            "context.sync",
            {"reason": "start"},
            "sync:1",
        )
        self.assertEqual(initial["command_id"], repeated["command_id"])
        self.assertTrue(repeated["idempotent"])

    def test_role_context_is_delivered_before_earlier_task_command(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assignment:first")
        self.store.enqueue_command(
            "demo",
            "manager",
            "worker-1",
            "context.sync",
            {"reason": "activate"},
            "context:later",
        )
        claimed = self.store.claim_delivery("demo", "controller")
        self.assertEqual("context.sync", claimed["command"]["spec"]["type"])

    def test_work_command_waits_until_current_role_context_is_confirmed(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assignment:wait")
        self.assertIsNone(self.store.claim_delivery("demo", "controller"))
        self.store.enqueue_command(
            "demo",
            "manager",
            "worker-1",
            "context.sync",
            {"reason": "activate"},
            "context:confirm",
        )
        activation = self.store.claim_delivery("demo", "controller")
        self.assertEqual("context.sync", activation["command"]["spec"]["type"])
        revision = activation["command"]["spec"]["context"]["context_revision"]
        self.store.confirm_context("demo", "worker-1", revision)
        self.store.begin_delivery(
            "demo", activation["command"]["metadata"]["id"], activation["delivery"]["lease_token"]
        )
        self.store.record_delivery_result(
            "demo",
            activation["command"]["metadata"]["id"],
            activation["delivery"]["lease_token"],
            "delivered",
        )
        claimed = self.store.claim_delivery("demo", "controller")
        self.assertEqual("task.assign", claimed["command"]["spec"]["type"])

    def test_stale_role_context_confirmation_is_rejected(self):
        self.store.assign("demo", "task-1", "worker-1", "manager")
        with self.assertRaisesRegex(fleet_control.FleetError, "not current"):
            self.store.confirm_context("demo", "worker-1", 1)

    def test_blocked_task_can_resume_running(self):
        self.store.assign("demo", "task-1", "worker-1", "manager")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.transition_task(
            "demo", "task-1", "blocked", "worker-1", {"reason": "approval"}
        )
        result = self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.assertEqual("running", result["status"])

    def test_only_assignee_can_report_task_state(self):
        self.store.assign("demo", "task-1", "worker-1", "manager")
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
        context = request["spec"]["context"]
        self.assertEqual("worker-1", context["agent"]["agent_ref"])
        self.assertEqual("worker@1", context["agent"]["role_ref"])
        self.assertEqual("Complete the demo safely.", context["fleet"]["objective"])
        self.assertEqual("manager", context["reporting"]["manager_ref"])
        self.assertEqual("task-1", context["assignments"][0]["task_id"])
        self.assertEqual(
            ["The result includes test evidence."],
            context["assignments"][0]["completion_criteria"],
        )

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

    def test_two_fleets_may_reuse_logical_ids_without_cross_talk(self):
        second = copy.deepcopy(NORMALIZED)
        second["metadata"]["id"] = "demo-two"
        self.store.initialize(second)

        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.assign("demo-two", "task-1", "worker-1", "manager", "assign-1")

        first = self.store.status("demo")
        second_status = self.store.status("demo-two")
        self.assertEqual("demo", first["fleet"]["fleet_id"])
        self.assertEqual("demo-two", second_status["fleet"]["fleet_id"])
        self.assertEqual("assign-1", first["outbox"][0]["metadata"]["id"])
        self.assertEqual("assign-1", second_status["outbox"][0]["metadata"]["id"])

    def test_progress_report_is_idempotent_and_notifies_manager_atomically(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        report = {"summary": "half done", "completed_milestones": ["parse"]}

        first = self.store.report_progress(
            "demo",
            "task-1",
            "worker-1",
            "report-1",
            report,
            "2026-09-01T12:10:00+00:00",
        )
        second = self.store.report_progress(
            "demo",
            "task-1",
            "worker-1",
            "report-1",
            report,
            "2026-09-01T12:10:00+00:00",
        )

        self.assertEqual(first, second)
        self.assertTrue(second["idempotent"])
        status = self.store.status("demo")
        self.assertEqual("running", status["tasks"][0]["status"])
        self.assertEqual("report-1", status["tasks"][0]["latest_report"]["report_id"])
        self.assertEqual("2026-09-01T12:10:00+00:00", status["tasks"][0]["next_report_at"])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM task_reports").fetchone()[0])
            self.assertEqual(
                1,
                db.execute(
                    "SELECT count(*) FROM outbox WHERE fleet_id='demo' "
                    "AND command_id='report-notification:report-1'"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                db.execute(
                    "SELECT count(*) FROM events WHERE fleet_id='demo' "
                    "AND event_type='task.progress.reported'"
                ).fetchone()[0],
            )

        with self.assertRaisesRegex(fleet_control.FleetError, "different content"):
            self.store.report_progress(
                "demo",
                "task-1",
                "worker-1",
                "report-1",
                {"summary": "changed"},
                "2026-09-01T12:10:00+00:00",
            )

    def test_completion_report_requires_manager_acceptance(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")

        reported = self.store.transition_task(
            "demo", "task-1", "completed", "worker-1", {"summary": "done"}
        )
        self.assertEqual("reported", reported["status"])
        self.assertEqual("reported", self.store.status("demo")["tasks"][0]["status"])

        with self.assertRaisesRegex(fleet_control.FleetError, "only a fleet manager"):
            self.store.accept_task("demo", "task-1", "worker-1")
        accepted = self.store.accept_task("demo", "task-1", "manager")
        self.assertEqual("accepted", accepted["status"])

    def test_delivery_claim_has_a_fenced_lease_and_unknown_is_not_retried(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "cmd-1"
        )

        claimed = self.store.claim_delivery(
            "demo", "delivery-1", "2026-09-01T12:00:00+00:00", 30
        )
        self.assertEqual("cmd-1", claimed["command"]["metadata"]["id"])
        self.assertEqual("processing", claimed["delivery"]["status"])
        self.assertEqual(1, claimed["delivery"]["attempt_count"])
        self.assertIsNone(
            self.store.claim_delivery(
                "demo", "delivery-2", "2026-09-01T12:00:10+00:00", 30
            )
        )
        with self.assertRaisesRegex(fleet_control.FleetError, "lease token"):
            self.store.record_delivery_result(
                "demo", "cmd-1", "stale-token", "delivered"
            )

        self.store.begin_delivery(
            "demo", "cmd-1", claimed["delivery"]["lease_token"]
        )
        result = self.store.record_delivery_result(
            "demo", "cmd-1", claimed["delivery"]["lease_token"], "unknown", "timeout"
        )
        self.assertEqual("unknown", result["status"])
        self.assertIsNone(
            self.store.claim_delivery(
                "demo", "delivery-2", "2026-09-01T12:01:00+00:00", 30
            )
        )

    def test_two_delivery_workers_cannot_claim_the_same_command(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "cmd-1"
        )

        def claim(worker_id):
            return self.store.claim_delivery(
                "demo", worker_id, "2026-09-01T12:00:00+00:00", 30
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ["delivery-1", "delivery-2"]))

        claimed = [result for result in results if result is not None]
        self.assertEqual(1, len(claimed))
        self.assertEqual("cmd-1", claimed[0]["command"]["metadata"]["id"])

    def test_expired_delivery_lease_is_reclaimed_and_old_token_is_fenced(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "cmd-1"
        )
        first = self.store.claim_delivery(
            "demo", "delivery-1", "2026-09-01T12:00:00+00:00", 30
        )
        second = self.store.claim_delivery(
            "demo", "delivery-2", "2026-09-01T12:00:31+00:00", 30
        )

        self.assertEqual(2, second["delivery"]["attempt_count"])
        self.assertNotEqual(
            first["delivery"]["lease_token"], second["delivery"]["lease_token"]
        )
        with self.assertRaisesRegex(fleet_control.FleetError, "lease token"):
            self.store.record_delivery_result(
                "demo", "cmd-1", first["delivery"]["lease_token"], "delivered"
            )

    def test_delivery_started_before_external_send_is_not_retried_after_lease_expiry(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "cmd-1"
        )
        claimed = self.store.claim_delivery(
            "demo", "delivery-1", "2026-09-01T12:00:00+00:00", 30
        )

        sending = self.store.begin_delivery(
            "demo", "cmd-1", claimed["delivery"]["lease_token"]
        )

        self.assertEqual("sending", sending["status"])
        self.assertIsNone(
            self.store.claim_delivery(
                "demo", "delivery-2", "2026-09-01T12:00:31+00:00", 30
            )
        )
        status = self.store.status("demo")
        self.assertEqual(1, status["delivery_counts"]["unknown"])

    def test_delivery_result_requires_send_to_have_started(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "cmd-1"
        )
        claimed = self.store.claim_delivery(
            "demo", "delivery-1", "2026-09-01T12:00:00+00:00", 30
        )

        with self.assertRaisesRegex(fleet_control.FleetError, "delivery has not started"):
            self.store.record_delivery_result(
                "demo", "cmd-1", claimed["delivery"]["lease_token"], "delivered"
            )


if __name__ == "__main__":
    unittest.main()
