import importlib.util
import copy
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import closing
import sqlite3
import stat
import tempfile
from threading import Event
import unittest
from datetime import datetime, timedelta, timezone
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
            {
                "agent_ref": "manager",
                "role_ref": "coordinator@1",
                "role_definition": {
                    "id": "coordinator",
                    "version": 1,
                    "mission": "Keep the objective and make final decisions.",
                    "responsibilities": ["Accept evidence"],
                    "forbidden": ["Implement worker output"],
                    "authority": ["assign", "accept"],
                },
            },
            {
                "agent_ref": "worker-1",
                "role_ref": "builder@1",
                "role_definition": {
                    "id": "builder",
                    "version": 1,
                    "mission": "Create the assigned artifact.",
                    "responsibilities": ["Report verification evidence"],
                    "forbidden": ["Rewrite completion criteria"],
                    "authority": ["work"],
                },
            },
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
        self.catalog = self.root / "role-catalog.yml"
        self.catalog.write_text("fixture: true\n", encoding="utf-8")
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
        with closing(sqlite3.connect(self.db)) as db:
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
        self.assertEqual(0o600, stat.S_IMODE(self.db.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))

    def test_role_definition_snapshot_is_in_session_context(self):
        with self.store.connect() as db:
            context = self.store._context_capsule(db, "demo", "worker-1")
        self.assertEqual("builder@1", context["agent"]["role_ref"])
        self.assertEqual(
            "Create the assigned artifact.",
            context["agent"]["role_definition"]["mission"],
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
                    [
                        "--db",
                        str(cli_db),
                        "spec.validate",
                        "--config",
                        str(self.config),
                        "--role-catalog",
                        str(self.catalog),
                    ]
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

    def test_task_transition_operation_id_makes_lost_response_retry_idempotent(self):
        self.store.assign("demo", "task-1", "worker-1", "manager")
        first = self.store.transition_task(
            "demo", "task-1", "running", "worker-1", operation_id="run-task-1"
        )
        second = self.store.transition_task(
            "demo", "task-1", "running", "worker-1", operation_id="run-task-1"
        )
        self.assertEqual("running", first["status"])
        self.assertTrue(second["idempotent"])

    def test_invalid_task_transition_is_rejected(self):
        self.store.assign("demo", "task-1", "worker-1", "manager")
        with self.assertRaisesRegex(fleet_control.FleetError, "assigned -> reported"):
            self.store.transition_task(
                "demo", "task-1", "completed", "worker-1", {"summary": "too early"}
            )

    def test_task_cannot_be_assigned_against_declared_assignee(self):
        with self.assertRaisesRegex(fleet_control.FleetError, "declared for"):
            self.store.assign("demo", "task-1", "manager", "manager")

    def test_non_manager_cannot_assign_and_pending_task_is_unchanged(self):
        with self.assertRaisesRegex(fleet_control.FleetError, "only a fleet manager"):
            self.store.assign("demo", "task-1", "worker-1", "worker-1")

        task = self.store.status("demo")["tasks"][0]
        self.assertEqual("pending", task["status"])
        self.assertIsNone(task["assignee_ref"])

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
        activation = self.store.claim_delivery("demo", "controller")
        self.assertEqual("context.sync", activation["command"]["spec"]["type"])
        self.store.consume_context_activation(
            "demo",
            activation["command"]["metadata"]["id"],
            activation["command"]["spec"]["payload"]["activation_token"],
            "session-wait",
            "codex",
        )
        self.store.begin_delivery(
            "demo", activation["command"]["metadata"]["id"], activation["delivery"]["lease_token"]
        )
        self.store.record_delivery_result(
            "demo",
            activation["command"]["metadata"]["id"],
            activation["delivery"]["lease_token"],
            "unknown",
        )
        claimed = self.store.claim_delivery("demo", "controller")
        self.assertEqual("task.assign", claimed["command"]["spec"]["type"])

    def test_stale_role_context_confirmation_is_rejected(self):
        self.store.assign("demo", "task-1", "worker-1", "manager")
        with self.assertRaisesRegex(fleet_control.FleetError, "not current"):
            self.store.confirm_context("demo", "worker-1", 1)

    def test_running_report_does_not_interrupt_the_agent_with_context_sync(self):
        self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:context"
        )
        first = self.store.claim_delivery("demo", "controller")
        self.assertEqual("context.sync", first["command"]["spec"]["type"])
        revision = first["command"]["spec"]["context"]["context_revision"]
        self.assertEqual(2, revision)
        self.store.consume_context_activation(
            "demo",
            first["command"]["metadata"]["id"],
            first["command"]["spec"]["payload"]["activation_token"],
            "session-1",
            "codex",
        )
        with self.store.connect() as db:
            before = db.execute(
                "SELECT context_revision FROM member_context_state "
                "WHERE fleet_id='demo' AND agent_ref='worker-1'"
            ).fetchone()[0]
            pending_before = db.execute(
                "SELECT COUNT(*) FROM outbox WHERE fleet_id='demo' "
                "AND target_agent_ref='worker-1' AND command_type='context.sync' "
                "AND status IN ('pending','retry')"
            ).fetchone()[0]
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        with self.store.connect() as db:
            after = db.execute(
                "SELECT context_revision FROM member_context_state "
                "WHERE fleet_id='demo' AND agent_ref='worker-1'"
            ).fetchone()[0]
            pending_after = db.execute(
                "SELECT COUNT(*) FROM outbox WHERE fleet_id='demo' "
                "AND target_agent_ref='worker-1' AND command_type='context.sync' "
                "AND status IN ('pending','retry')"
            ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(pending_before, pending_after)
        second = self.store.claim_delivery("demo", "controller")
        self.assertEqual("task.assign", second["command"]["spec"]["type"])
        self.assertEqual(
            2, second["command"]["spec"]["context"]["context_revision"]
        )

    def test_resuming_after_block_or_rework_does_not_enqueue_a_second_context_sync(self):
        self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:resume"
        )
        self.store.transition_task("demo", "task-1", "running", "worker-1")

        self.store.transition_task(
            "demo", "task-1", "blocked", "worker-1", {"reason": "waiting"}
        )
        with self.store.connect() as db:
            blocked_revision = db.execute(
                "SELECT context_revision FROM member_context_state "
                "WHERE fleet_id='demo' AND agent_ref='worker-1'"
            ).fetchone()[0]
            blocked_syncs = db.execute(
                "SELECT COUNT(*) FROM outbox WHERE fleet_id='demo' "
                "AND target_agent_ref='worker-1' AND command_type='context.sync' "
                "AND status IN ('pending','retry')"
            ).fetchone()[0]
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        with self.store.connect() as db:
            self.assertEqual(
                blocked_revision,
                db.execute(
                    "SELECT context_revision FROM member_context_state "
                    "WHERE fleet_id='demo' AND agent_ref='worker-1'"
                ).fetchone()[0],
            )
            self.assertEqual(
                blocked_syncs,
                db.execute(
                    "SELECT COUNT(*) FROM outbox WHERE fleet_id='demo' "
                    "AND target_agent_ref='worker-1' AND command_type='context.sync' "
                    "AND status IN ('pending','retry')"
                ).fetchone()[0],
            )
        blocked_report = self.store.claim_delivery("demo", "controller-blocked-report")
        self.assertEqual("task.report", blocked_report["command"]["spec"]["type"])
        self.store.begin_delivery(
            "demo",
            blocked_report["command"]["metadata"]["id"],
            blocked_report["delivery"]["lease_token"],
        )
        self.store.record_delivery_result(
            "demo",
            blocked_report["command"]["metadata"]["id"],
            blocked_report["delivery"]["lease_token"],
            "unknown",
        )
        blocked_context = self.store.claim_delivery("demo", "controller-blocked")
        self.assertEqual(
            "context.sync", blocked_context["command"]["spec"]["type"]
        )
        self.store.consume_context_activation(
            "demo",
            blocked_context["command"]["metadata"]["id"],
            blocked_context["command"]["spec"]["payload"]["activation_token"],
            "session-blocked",
            "codex",
        )
        current = self.store.current_session_context(
            "demo", "worker-1", "session-blocked", "codex"
        )
        self.assertEqual("running", current["context"]["assignments"][0]["status"])

        self.store.transition_task(
            "demo", "task-1", "completed", "worker-1", {"summary": "first"}
        )
        with self.store.connect() as db:
            reported_revision = db.execute(
                "SELECT context_revision FROM member_context_state "
                "WHERE fleet_id='demo' AND agent_ref='worker-1'"
            ).fetchone()[0]
            reported_syncs = db.execute(
                "SELECT COUNT(*) FROM outbox WHERE fleet_id='demo' "
                "AND target_agent_ref='worker-1' AND command_type='context.sync' "
                "AND status IN ('pending','retry')"
            ).fetchone()[0]
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        with self.store.connect() as db:
            self.assertEqual(
                reported_revision,
                db.execute(
                    "SELECT context_revision FROM member_context_state "
                    "WHERE fleet_id='demo' AND agent_ref='worker-1'"
                ).fetchone()[0],
            )
            self.assertEqual(
                reported_syncs,
                db.execute(
                    "SELECT COUNT(*) FROM outbox WHERE fleet_id='demo' "
                    "AND target_agent_ref='worker-1' AND command_type='context.sync' "
                    "AND status IN ('pending','retry')"
                ).fetchone()[0],
            )
        manager_report = self.store.claim_delivery("demo", "controller-manager")
        self.assertEqual("task.report", manager_report["command"]["spec"]["type"])
        self.store.begin_delivery(
            "demo",
            manager_report["command"]["metadata"]["id"],
            manager_report["delivery"]["lease_token"],
        )
        self.store.record_delivery_result(
            "demo",
            manager_report["command"]["metadata"]["id"],
            manager_report["delivery"]["lease_token"],
            "unknown",
        )
        reported_context = self.store.claim_delivery("demo", "controller-reported")
        self.assertEqual(
            "context.sync", reported_context["command"]["spec"]["type"]
        )
        self.store.consume_context_activation(
            "demo",
            reported_context["command"]["metadata"]["id"],
            reported_context["command"]["spec"]["payload"]["activation_token"],
            "session-reported",
            "codex",
        )
        current = self.store.current_session_context(
            "demo", "worker-1", "session-reported", "codex"
        )
        self.assertEqual("running", current["context"]["assignments"][0]["status"])

    def test_later_context_sync_preserves_manager_monitoring_control(self):
        monitoring = {
            "action": "task.list",
            "prohibited_methods": ["sqlite-direct", "external-json-filter"],
        }
        self.store.enqueue_command(
            "demo",
            "manager",
            "worker-1",
            "context.sync",
            {"control": {"monitoring": monitoring}},
            "context:monitoring",
        )

        self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:monitoring"
        )
        generated = self.store.claim_delivery("demo", "controller")

        self.assertEqual("context.sync", generated["command"]["spec"]["type"])
        self.assertEqual(
            monitoring,
            generated["command"]["spec"]["payload"]["control"]["monitoring"],
        )

    def test_terminal_worker_report_enqueues_manager_review_command(self):
        self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:review"
        )
        self.store.transition_task("demo", "task-1", "running", "worker-1")

        self.store.transition_task(
            "demo",
            "task-1",
            "reported",
            "worker-1",
            {"summary": "verified result"},
        )

        pending = self.store.status("demo")["outbox"]
        review = next(
            command
            for command in pending
            if command["spec"]["type"] == "task.report"
        )
        self.assertEqual("worker-1", review["spec"]["source"]["ref"])
        self.assertEqual("manager", review["spec"]["target"]["ref"])
        self.assertEqual("task-1", review["spec"]["payload"]["task_id"])
        self.assertEqual("verified result", review["spec"]["payload"]["report"]["summary"])

    def test_manager_review_does_not_wait_for_worker_context_refresh(self):
        self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:fast-review"
        )
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.transition_task(
            "demo",
            "task-1",
            "reported",
            "worker-1",
            {"summary": "ready for review"},
        )

        claimed = self.store.claim_delivery("demo", "controller")

        self.assertEqual("task.report", claimed["command"]["spec"]["type"])
        self.assertEqual("manager", claimed["command"]["spec"]["target"]["ref"])

    def test_context_activation_is_authoritative_one_use_and_session_bound(self):
        self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:activation"
        )
        claimed = self.store.claim_delivery("demo", "controller")
        command = claimed["command"]
        token = command["spec"]["payload"]["activation_token"]
        activated = self.store.consume_context_activation(
            "demo", command["metadata"]["id"], token, "session-1", "codex"
        )
        self.assertEqual("worker-1", activated["context"]["agent"]["agent_ref"])
        with self.assertRaisesRegex(fleet_control.FleetError, "already consumed"):
            self.store.consume_context_activation(
                "demo", command["metadata"]["id"], token, "session-2", "codex"
            )
        with self.assertRaisesRegex(fleet_control.FleetError, "invalid"):
            self.store.consume_context_activation(
                "demo", command["metadata"]["id"], "forged", "session-1", "codex"
            )

    def test_non_activation_command_is_core_verified_and_session_bound(self):
        self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:receipt"
        )
        context_delivery = self.store.claim_delivery("demo", "controller")
        context_command = context_delivery["command"]
        self.store.begin_delivery(
            "demo",
            context_command["metadata"]["id"],
            context_delivery["delivery"]["lease_token"],
        )
        self.store.consume_context_activation(
            "demo",
            context_command["metadata"]["id"],
            context_command["spec"]["payload"]["activation_token"],
            "session-1",
            "codex",
        )
        self.store.record_delivery_result(
            "demo",
            context_command["metadata"]["id"],
            context_delivery["delivery"]["lease_token"],
            "delivered",
        )
        task_delivery = self.store.claim_delivery("demo", "controller")
        task_command = task_delivery["command"]
        self.store.begin_delivery(
            "demo",
            task_command["metadata"]["id"],
            task_delivery["delivery"]["lease_token"],
        )

        prepared = self.store.prepare_command(
            "demo",
            task_command["metadata"]["id"],
            task_command,
            "session-1",
            "codex",
        )
        self.assertEqual("prepared", prepared["status"])
        with self.store.connect() as db:
            before_confirm = db.execute(
                "SELECT status,activation_consumed_at FROM outbox "
                "WHERE fleet_id='demo' AND command_id=?",
                (task_command["metadata"]["id"],),
            ).fetchone()
        self.assertEqual("sending", before_confirm["status"])
        self.assertIsNone(before_confirm["activation_consumed_at"])

        receipt = self.store.consume_command(
            "demo",
            task_command["metadata"]["id"],
            task_command,
            "session-1",
            "codex",
        )

        self.assertEqual("received", receipt["status"])
        self.assertEqual("worker-1", receipt["agent_ref"])
        late_result = self.store.record_delivery_result(
            "demo",
            task_command["metadata"]["id"],
            task_delivery["delivery"]["lease_token"],
            "unknown",
        )
        self.assertEqual("delivered", late_result["status"])
        self.assertTrue(late_result["idempotent"])
        with self.assertRaisesRegex(fleet_control.FleetError, "another session"):
            self.store.consume_command(
                "demo",
                task_command["metadata"]["id"],
                task_command,
                "session-2",
                "codex",
            )
        forged = copy.deepcopy(task_command)
        forged["spec"]["payload"]["task_id"] = "forged"
        with self.assertRaisesRegex(fleet_control.FleetError, "does not match"):
            self.store.consume_command(
                "demo",
                task_command["metadata"]["id"],
                forged,
                "session-1",
                "codex",
            )

    def test_late_hook_receipt_corrects_unknown_delivery(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "late"
        )
        self.store.enqueue_command(
            "demo",
            "manager",
            "worker-1",
            "context.sync",
            {"reason": "bind receipt session"},
            "late-context",
        )
        activation = self.store.claim_delivery("demo", "context-controller")
        self.store.begin_delivery(
            "demo", "late-context", activation["delivery"]["lease_token"]
        )
        self.store.consume_context_activation(
            "demo",
            "late-context",
            activation["command"]["spec"]["payload"]["activation_token"],
            "session-1",
            "codex",
        )
        claimed = self.store.claim_delivery("demo", "controller")
        command = claimed["command"]
        self.store.begin_delivery(
            "demo", "late", claimed["delivery"]["lease_token"]
        )
        self.store.record_delivery_result(
            "demo", "late", claimed["delivery"]["lease_token"], "unknown"
        )

        receipt = self.store.consume_command(
            "demo", "late", command, "session-1", "codex"
        )

        self.assertEqual("received", receipt["status"])
        self.assertEqual(2, self.store.status("demo")["delivery_counts"]["delivered"])

    def test_concurrent_hook_receipt_cannot_be_overwritten_by_unknown_result(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "race"
        )
        self.store.enqueue_command(
            "demo",
            "manager",
            "worker-1",
            "context.sync",
            {"reason": "bind receipt session"},
            "race-context",
        )
        activation = self.store.claim_delivery("demo", "context-controller")
        self.store.begin_delivery(
            "demo", "race-context", activation["delivery"]["lease_token"]
        )
        self.store.consume_context_activation(
            "demo",
            "race-context",
            activation["command"]["spec"]["payload"]["activation_token"],
            "session-1",
            "codex",
        )
        claimed = self.store.claim_delivery("demo", "controller")
        command = claimed["command"]
        self.store.begin_delivery(
            "demo", "race", claimed["delivery"]["lease_token"]
        )

        result_is_resolving = Event()
        release_result = Event()
        receipt_started = Event()
        original_parse_timestamp = self.store._parse_timestamp

        def pause_delivery_result(value, field):
            parsed = original_parse_timestamp(value, field)
            if field == "now":
                result_is_resolving.set()
                if not release_result.wait(5):
                    raise AssertionError("timed out waiting to release delivery result")
            return parsed

        def consume_receipt():
            receipt_started.set()
            return self.store.consume_command(
                "demo", "race", command, "session-1", "codex"
            )

        with mock.patch.object(
            self.store, "_parse_timestamp", side_effect=pause_delivery_result
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                result_future = executor.submit(
                    self.store.record_delivery_result,
                    "demo",
                    "race",
                    claimed["delivery"]["lease_token"],
                    "unknown",
                )
                self.assertTrue(result_is_resolving.wait(2))
                receipt_future = executor.submit(consume_receipt)
                self.assertTrue(receipt_started.wait(2))
                try:
                    receipt_future.result(timeout=0.5)
                except TimeoutError:
                    pass
                release_result.set()
                result_future.result(timeout=5)
                receipt = receipt_future.result(timeout=5)

        self.assertEqual("received", receipt["status"])
        status = self.store.status("demo")
        self.assertEqual(2, status["delivery_counts"]["delivered"])
        self.assertNotIn("unknown", status["delivery_counts"])
        with self.store.connect() as db:
            row = db.execute(
                "SELECT status,activation_consumed_at,lease_owner,lease_token,"
                "lease_expires_at FROM outbox WHERE fleet_id='demo' AND command_id='race'"
            ).fetchone()
            events = [
                event["event_type"]
                for event in db.execute(
                    "SELECT event_type FROM events WHERE fleet_id='demo' "
                    "AND entity_id='race' AND event_type LIKE 'delivery.%' "
                    "ORDER BY event_id"
                )
            ]
        self.assertEqual("delivered", row["status"])
        self.assertIsNotNone(row["activation_consumed_at"])
        self.assertIsNone(row["lease_owner"])
        self.assertIsNone(row["lease_token"])
        self.assertIsNone(row["lease_expires_at"])
        self.assertEqual("delivery.delivered", events[-1])

    def test_current_session_context_rejects_stale_or_unbound_sessions(self):
        self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:current"
        )
        activation = self.store.claim_delivery("demo", "controller")
        command = activation["command"]
        self.store.begin_delivery(
            "demo", command["metadata"]["id"], activation["delivery"]["lease_token"]
        )
        self.store.consume_context_activation(
            "demo",
            command["metadata"]["id"],
            command["spec"]["payload"]["activation_token"],
            "session-1",
            "codex",
        )
        current = self.store.current_session_context(
            "demo", "worker-1", "session-1", "codex"
        )
        self.assertEqual(2, current["context"]["context_revision"])
        with self.assertRaisesRegex(fleet_control.FleetError, "not bound"):
            self.store.current_session_context(
                "demo", "worker-1", "other-session", "codex"
            )

        self.store.transition_task("demo", "task-1", "running", "worker-1")
        current = self.store.current_session_context(
            "demo", "worker-1", "session-1", "codex"
        )
        self.assertEqual(2, current["context"]["context_revision"])
        self.assertEqual("running", current["context"]["assignments"][0]["status"])

    def test_context_invalidation_closes_delivery_gate(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "cmd"
        )
        self.store.invalidate_contexts("demo")

        self.assertIsNone(self.store.claim_delivery("demo", "controller"))

    def test_context_invalidation_rejects_an_activation_from_the_old_runtime(self):
        self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:old-runtime"
        )
        activation = self.store.claim_delivery("demo", "controller")
        command = activation["command"]
        self.store.begin_delivery(
            "demo", command["metadata"]["id"], activation["delivery"]["lease_token"]
        )
        self.store.invalidate_contexts("demo")

        with self.assertRaisesRegex(fleet_control.FleetError, "revision is invalid"):
            self.store.consume_context_activation(
                "demo",
                command["metadata"]["id"],
                command["spec"]["payload"]["activation_token"],
                "old-session",
                "codex",
            )

    def test_new_runtime_confirmation_does_not_reactivate_old_session(self):
        self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:new-runtime"
        )
        old = self.store.claim_delivery("demo", "controller")
        self.store.begin_delivery(
            "demo", old["command"]["metadata"]["id"], old["delivery"]["lease_token"]
        )
        self.store.consume_context_activation(
            "demo",
            old["command"]["metadata"]["id"],
            old["command"]["spec"]["payload"]["activation_token"],
            "old-session",
            "codex",
        )
        self.store.invalidate_contexts("demo")
        self.store.enqueue_command(
            "demo",
            "manager",
            "worker-1",
            "context.sync",
            {"reason": "new runtime"},
            "new-runtime-context",
        )
        new = self.store.claim_delivery("demo", "controller-2")
        self.store.begin_delivery(
            "demo", new["command"]["metadata"]["id"], new["delivery"]["lease_token"]
        )
        self.store.consume_context_activation(
            "demo",
            new["command"]["metadata"]["id"],
            new["command"]["spec"]["payload"]["activation_token"],
            "new-session",
            "codex",
        )

        with self.assertRaisesRegex(fleet_control.FleetError, "not current"):
            self.store.current_session_context(
                "demo", "worker-1", "old-session", "codex"
            )
        current = self.store.current_session_context(
            "demo", "worker-1", "new-session", "codex"
        )
        self.assertEqual("current", current["status"])

    def test_delayed_command_cannot_reactivate_an_old_runtime_session(self):
        self.store.assign(
            "demo", "task-1", "worker-1", "manager", "assignment:delayed"
        )
        old_context = self.store.claim_delivery("demo", "controller-old-context")
        self.store.begin_delivery(
            "demo",
            old_context["command"]["metadata"]["id"],
            old_context["delivery"]["lease_token"],
        )
        self.store.consume_context_activation(
            "demo",
            old_context["command"]["metadata"]["id"],
            old_context["command"]["spec"]["payload"]["activation_token"],
            "old-session",
            "codex",
        )
        delayed = self.store.claim_delivery("demo", "controller-delayed")
        self.store.begin_delivery(
            "demo",
            delayed["command"]["metadata"]["id"],
            delayed["delivery"]["lease_token"],
        )

        self.store.invalidate_contexts("demo")
        self.store.enqueue_command(
            "demo",
            "manager",
            "worker-1",
            "context.sync",
            {"reason": "new runtime"},
            "new-runtime-delayed-context",
        )
        new_context = self.store.claim_delivery("demo", "controller-new-context")
        self.store.begin_delivery(
            "demo",
            new_context["command"]["metadata"]["id"],
            new_context["delivery"]["lease_token"],
        )
        self.store.consume_context_activation(
            "demo",
            new_context["command"]["metadata"]["id"],
            new_context["command"]["spec"]["payload"]["activation_token"],
            "new-session",
            "codex",
        )

        with self.assertRaisesRegex(fleet_control.FleetError, "session context is not current"):
            self.store.prepare_command(
                "demo",
                delayed["command"]["metadata"]["id"],
                delayed["command"],
                "old-session",
                "codex",
            )

    def test_accepting_task_releases_newly_unblocked_dependents(self):
        config = copy.deepcopy(NORMALIZED)
        config["metadata"]["id"] = "dependent"
        config["spec"]["tasks"].append(
            {
                "id": "task-2",
                "assignee": "worker-1",
                "depends_on": ["task-1"],
                "instructions": "Do the second task.",
                "expected_output": "A second verified result.",
                "completion_criteria": ["The second result includes evidence."],
            }
        )
        self.store.initialize(config)
        self.store.confirm_context("dependent", "worker-1", 1)
        self.store.assign(
            "dependent", "task-1", "worker-1", "manager", "dependent:first"
        )
        self.store.transition_task(
            "dependent", "task-1", "running", "worker-1"
        )
        self.store.transition_task(
            "dependent",
            "task-1",
            "reported",
            "worker-1",
            {"summary": "done"},
        )
        result = self.store.accept_task("dependent", "task-1", "manager")
        self.assertEqual(["task-2"], result["released_tasks"])
        tasks = {item["task_id"]: item for item in self.store.status("dependent")["tasks"]}
        self.assertEqual("assigned", tasks["task-2"]["status"])

    def test_manual_assignment_rejects_pending_and_reported_dependencies(self):
        config = copy.deepcopy(NORMALIZED)
        config["metadata"]["id"] = "dependent"
        config["spec"]["tasks"].extend(
            [
                {
                    "id": "task-2",
                    "assignee": "worker-1",
                    "depends_on": [],
                    "instructions": "Do the second task.",
                    "expected_output": "A second verified result.",
                    "completion_criteria": ["The result includes evidence."],
                },
                {
                    "id": "task-3",
                    "assignee": "worker-1",
                    "depends_on": ["task-2", "task-1"],
                    "instructions": "Do the third task.",
                    "expected_output": "A third verified result.",
                    "completion_criteria": ["The result includes evidence."],
                },
            ]
        )
        self.store.initialize(config)
        self.store.assign("dependent", "task-2", "worker-1", "manager")
        self.store.transition_task("dependent", "task-2", "running", "worker-1")
        self.store.transition_task(
            "dependent", "task-2", "reported", "worker-1", {"summary": "done"}
        )
        with self.assertRaisesRegex(
            fleet_control.FleetError,
            "task-2 \\(reported\\), task-1 \\(pending\\)",
        ):
            self.store.assign("dependent", "task-3", "worker-1", "manager")

        task = next(
            item for item in self.store.task_list("dependent")["tasks"]
            if item["task_id"] == "task-3"
        )
        self.assertEqual("pending", task["status"])

    def test_acceptance_releases_multiple_dependencies_and_assignment_retry_is_idempotent(self):
        config = copy.deepcopy(NORMALIZED)
        config["metadata"]["id"] = "multiple-dependencies"
        config["spec"]["tasks"].extend(
            [
                {
                    "id": "task-2",
                    "assignee": "worker-1",
                    "depends_on": [],
                    "instructions": "Do the second task.",
                    "expected_output": "A second verified result.",
                    "completion_criteria": ["The result includes evidence."],
                },
                {
                    "id": "task-3",
                    "assignee": "worker-1",
                    "depends_on": ["task-2", "task-1"],
                    "instructions": "Do the third task.",
                    "expected_output": "A third verified result.",
                    "completion_criteria": ["The result includes evidence."],
                },
            ]
        )
        self.store.initialize(config)
        for task_id in ("task-1", "task-2"):
            self.store.assign("multiple-dependencies", task_id, "worker-1", "manager")
            self.store.transition_task(
                "multiple-dependencies", task_id, "running", "worker-1"
            )
            self.store.transition_task(
                "multiple-dependencies",
                task_id,
                "reported",
                "worker-1",
                {"summary": f"{task_id} done"},
            )
            result = self.store.accept_task(
                "multiple-dependencies", task_id, "manager", f"accept:{task_id}"
            )
            self.assertEqual(
                [] if task_id == "task-1" else ["task-3"], result["released_tasks"]
            )

        retry = self.store.assign(
            "multiple-dependencies",
            "task-3",
            "worker-1",
            "manager",
            "task-assign:multiple-dependencies:task-3:dependency-release",
        )
        self.assertEqual("assigned", retry["status"])
        self.assertTrue(retry["idempotent"])

    def test_task_list_includes_declared_dependencies_in_order_after_acceptance(self):
        config = copy.deepcopy(NORMALIZED)
        config["metadata"]["id"] = "dependency-monitoring"
        config["spec"]["tasks"].extend(
            [
                {
                    "id": "task-2",
                    "assignee": "worker-1",
                    "depends_on": ["task-1"],
                    "instructions": "Do the second task.",
                    "expected_output": "A second verified result.",
                    "completion_criteria": ["The result includes evidence."],
                },
                {
                    "id": "task-3",
                    "assignee": "worker-1",
                    "depends_on": ["task-2", "task-1"],
                    "instructions": "Do the third task.",
                    "expected_output": "A third verified result.",
                    "completion_criteria": ["The result includes evidence."],
                },
            ]
        )
        self.store.initialize(config)
        self.store.assign("dependency-monitoring", "task-1", "worker-1", "manager")
        self.store.transition_task("dependency-monitoring", "task-1", "running", "worker-1")
        self.store.transition_task(
            "dependency-monitoring", "task-1", "reported", "worker-1", {"summary": "done"}
        )
        self.store.accept_task("dependency-monitoring", "task-1", "manager")

        tasks = {
            item["task_id"]: item
            for item in self.store.task_list("dependency-monitoring")["tasks"]
        }

        self.assertEqual([], tasks["task-1"]["depends_on"])
        self.assertEqual(["task-1"], tasks["task-2"]["depends_on"])
        self.assertEqual(["task-2", "task-1"], tasks["task-3"]["depends_on"])

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
        with closing(sqlite3.connect(self.db)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(members)")}
        self.assertIn("role_ref", columns)
        self.assertNotIn("role", columns)
        self.assertEqual(
            "coordinator@1", self.store.status("demo")["members"][0]["role_ref"]
        )

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
        self.assertEqual("builder@1", context["agent"]["role_ref"])
        self.assertEqual("Complete the demo safely.", context["fleet"]["objective"])
        self.assertEqual("manager", context["reporting"]["manager_ref"])
        self.assertEqual([], context["assignments"])

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

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(
            {key: value for key, value in first.items() if key != "idempotent"},
            {key: value for key, value in second.items() if key != "idempotent"},
        )
        status = self.store.status("demo")
        self.assertEqual("running", status["tasks"][0]["status"])
        self.assertEqual("report-1", status["tasks"][0]["latest_report"]["report_id"])
        self.assertEqual("2026-09-01T12:10:00+00:00", status["tasks"][0]["next_report_at"])
        with closing(sqlite3.connect(self.db)) as db:
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

    def test_first_missed_report_deadline_requests_a_status_check_once(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.report_progress(
            "demo",
            "task-1",
            "worker-1",
            "report-1",
            {"summary": "working"},
            "2026-09-01T12:10:00+00:00",
        )

        first = self.store.check_report_deadlines(
            "demo", "2026-09-01T12:10:00.001+00:00"
        )
        second = self.store.check_report_deadlines(
            "demo", "2026-09-01T12:20:00+00:00"
        )

        self.assertEqual(1, first["tasks"][0]["consecutive_missed_deadlines"])
        self.assertFalse(first["tasks"][0]["requires_user_decision"])
        self.assertTrue(second["idempotent"])
        status = self.store.status("demo")["tasks"][0]
        self.assertEqual(1, status["consecutive_missed_deadlines"])
        reminders = [
            command
            for command in self.store.status("demo")["outbox"]
            if command["spec"]["payload"].get("notification_type")
            == "task.progress.check_required"
        ]
        self.assertEqual(1, len(reminders))
        self.assertEqual("worker-1", reminders[0]["spec"]["target"]["ref"])

    def test_two_consecutive_missed_deadlines_require_user_decision(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.report_progress(
            "demo",
            "task-1",
            "worker-1",
            "report-1",
            {"summary": "first"},
            "2026-09-01T12:10:00+00:00",
        )
        self.store.report_progress(
            "demo",
            "task-1",
            "worker-1",
            "report-2",
            {"summary": "second, but late"},
            "2026-09-01T12:20:00+00:00",
        )
        with self.store.connect() as db:
            db.execute(
                "UPDATE task_reports SET created_at='2026-09-01T12:00:00+00:00' "
                "WHERE fleet_id='demo' AND report_id='report-1'"
            )
            db.execute(
                "UPDATE task_reports SET created_at='2026-09-01T12:10:00.001+00:00' "
                "WHERE fleet_id='demo' AND report_id='report-2'"
            )

        result = self.store.check_report_deadlines(
            "demo", "2026-09-01T12:20:00.001+00:00"
        )

        overdue = result["tasks"][0]
        self.assertEqual(2, overdue["consecutive_missed_deadlines"])
        self.assertTrue(overdue["requires_user_decision"])
        task = self.store.status("demo")["tasks"][0]
        self.assertEqual("running", task["status"])
        self.assertEqual(2, task["consecutive_missed_deadlines"])
        escalations = [
            command
            for command in self.store.status("demo")["outbox"]
            if command["spec"]["payload"].get("notification_type")
            == "task.progress.user_decision_required"
        ]
        self.assertEqual(1, len(escalations))
        self.assertEqual("manager", escalations[0]["spec"]["target"]["ref"])

    def test_deadline_boundary_and_terminal_task_do_not_escalate(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.report_progress(
            "demo",
            "task-1",
            "worker-1",
            "report-1",
            {"summary": "done at boundary"},
            "2026-09-01T12:10:00+00:00",
        )

        boundary = self.store.check_report_deadlines(
            "demo", "2026-09-01T12:10:00+00:00"
        )
        self.assertEqual([], boundary["tasks"])

        self.store.transition_task(
            "demo", "task-1", "reported", "worker-1", {"summary": "done"}
        )
        terminal = self.store.check_report_deadlines(
            "demo", "2026-09-01T12:10:00.001+00:00"
        )
        self.assertEqual([], terminal["tasks"])
        status = self.store.status("demo")
        self.assertEqual(0, status["tasks"][0]["consecutive_missed_deadlines"])
        self.assertFalse(status["tasks"][0]["requires_user_decision"])
        self.assertFalse(
            any(
                item["spec"]["payload"].get("notification_type")
                == "task.progress.check_required"
                for item in status["outbox"]
            )
        )

    def test_on_time_report_clears_a_previous_missed_deadline(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.report_progress(
            "demo", "task-1", "worker-1", "report-1", {"summary": "working"},
            "2026-09-01T12:10:00+00:00",
        )
        self.store.check_report_deadlines("demo", "2026-09-01T12:10:00.001+00:00")

        self.store.report_progress(
            "demo", "task-1", "worker-1", "report-2", {"summary": "recovered"},
            "2026-09-01T12:30:00+00:00",
        )
        result = self.store.check_report_deadlines("demo", "2026-09-01T12:20:00+00:00")

        self.assertEqual([], result["tasks"])
        task = self.store.status("demo")["tasks"][0]
        self.assertEqual(0, task["consecutive_missed_deadlines"])
        self.assertFalse(task["requires_user_decision"])

    def test_manager_can_read_a_report_saved_while_no_delivery_worker_runs(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.report_progress(
            "demo", "task-1", "worker-1", "offline-report", {"summary": "saved"},
            "2026-09-01T12:30:00+00:00",
        )

        status = self.store.status("demo")
        self.assertEqual("offline-report", status["tasks"][0]["latest_report"]["report_id"])
        notification = next(
            item for item in status["outbox"]
            if item["metadata"]["id"] == "report-notification:offline-report"
        )
        self.assertEqual("manager", notification["spec"]["target"]["ref"])

    def test_task_list_exposes_a_compact_manager_monitoring_view(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.report_progress(
            "demo", "task-1", "worker-1", "progress-1", {"summary": "working"},
            "2026-09-01T12:30:00+00:00",
        )
        self.store.check_report_deadlines(
            "demo", "2026-09-01T12:30:00.001+00:00"
        )

        result = self.store.task_list("demo")

        self.assertEqual("demo", result["fleet_id"])
        self.assertEqual(1, len(result["tasks"]))
        self.assertEqual("running", result["tasks"][0]["status"])
        self.assertEqual("A verified result.", result["tasks"][0]["expected_output"])
        self.assertEqual(
            ["The result includes test evidence."],
            result["tasks"][0]["completion_criteria"],
        )
        self.assertEqual(
            "progress-1", result["tasks"][0]["latest_report"]["report_id"]
        )
        self.assertEqual(1, result["tasks"][0]["consecutive_missed_deadlines"])
        self.assertFalse(result["tasks"][0]["requires_user_decision"])
        self.assertEqual(
            "2026-09-01T12:30:00.001+00:00",
            result["tasks"][0]["deadline_checked_at"],
        )
        self.assertNotIn("members", result)
        self.assertNotIn("outbox", result)
        self.assertNotIn("events", result)

    def test_task_list_keeps_the_terminal_report_visible_after_acceptance(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.transition_task(
            "demo", "task-1", "completed", "worker-1", {"summary": "done"}
        )
        self.store.accept_task("demo", "task-1", "manager")

        task = self.store.task_list("demo")["tasks"][0]

        self.assertEqual("accepted", task["status"])
        self.assertEqual("reported", task["latest_state_report"]["status"])
        self.assertEqual({"summary": "done"}, task["latest_state_report"]["report"])
        self.assertEqual("worker-1", task["latest_state_report"]["reporter_ref"])

    def test_task_list_cli_does_not_require_external_json_processing(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.report_progress(
            "demo", "task-1", "worker-1", "progress-1", {"summary": "working"},
            "2026-09-01T12:30:00+00:00",
        )
        self.store.transition_task(
            "demo", "task-1", "completed", "worker-1", {"summary": "done"}
        )
        stdout = __import__("io").StringIO()
        with mock.patch("sys.stdout", stdout):
            exit_code = fleet_control.main(
                ["--db", str(self.db), "task.list", "--fleet", "demo"]
            )

        result = __import__("json").loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(result["ok"])
        self.assertEqual("demo", result["result"]["fleet_id"])
        self.assertEqual(["fleet_id", "tasks"], sorted(result["result"]))
        task = result["result"]["tasks"][0]
        self.assertEqual("reported", task["status"])
        self.assertEqual("A verified result.", task["expected_output"])
        self.assertEqual(
            ["The result includes test evidence."], task["completion_criteria"]
        )
        self.assertEqual("reported", task["latest_state_report"]["status"])
        self.assertEqual("progress-1", task["latest_report"]["report_id"])
        self.assertEqual(0, task["consecutive_missed_deadlines"])
        self.assertFalse(task["requires_user_decision"])
        self.assertIsNone(task["deadline_checked_at"])

    def test_reported_task_can_be_returned_to_the_assignee_but_failed_task_stays_terminal(self):
        self.store.assign("demo", "task-1", "worker-1", "manager", "assign-1")
        self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.store.transition_task(
            "demo", "task-1", "completed", "worker-1", {"summary": "first result"}
        )
        resumed = self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.assertEqual("running", resumed["status"])

        self.store.transition_task(
            "demo", "task-1", "failed", "worker-1", {"summary": "cannot continue"}
        )
        with self.assertRaisesRegex(fleet_control.FleetError, "failed -> running"):
            self.store.transition_task("demo", "task-1", "running", "worker-1")
        self.assertEqual("failed", self.store.status("demo")["tasks"][0]["status"])

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
            "demo",
            "cmd-1",
            claimed["delivery"]["lease_token"],
            now="2026-09-01T12:00:10+00:00",
        )
        result = self.store.record_delivery_result(
            "demo",
            "cmd-1",
            claimed["delivery"]["lease_token"],
            "unknown",
            "timeout",
            now="2026-09-01T12:00:20+00:00",
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

    def test_delivery_lease_comparison_normalizes_equivalent_timezones(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "cmd-1"
        )
        first = self.store.claim_delivery(
            "demo", "delivery-1", "2026-09-01T12:00:00+00:00", 30
        )

        second = self.store.claim_delivery(
            "demo", "delivery-2", "2026-09-01T21:00:10+09:00", 30
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)

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
            "demo",
            "cmd-1",
            claimed["delivery"]["lease_token"],
            now="2026-09-01T12:00:10+00:00",
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

    def test_delivery_result_cannot_claim_hook_receipt(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "cmd-1"
        )
        claimed = self.store.claim_delivery("demo", "delivery-1")
        self.store.begin_delivery(
            "demo", "cmd-1", claimed["delivery"]["lease_token"]
        )

        with self.assertRaisesRegex(fleet_control.FleetError, "hook receipt"):
            self.store.record_delivery_result(
                "demo", "cmd-1", claimed["delivery"]["lease_token"], "delivered"
            )

    def test_expired_lease_cannot_begin_or_record_a_result(self):
        self.store.enqueue_command(
            "demo", "manager", "worker-1", "message.send", {"text": "hello"}, "late"
        )
        now = datetime.now(timezone.utc)
        claimed = self.store.claim_delivery(
            "demo", "delivery-1", now.isoformat(), 1
        )
        expired = (now + timedelta(seconds=2)).isoformat()
        with self.assertRaisesRegex(fleet_control.FleetError, "expired"):
            self.store.begin_delivery(
                "demo", "late", claimed["delivery"]["lease_token"], now=expired
            )


if __name__ == "__main__":
    unittest.main()
