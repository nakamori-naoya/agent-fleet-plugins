import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "fleet_controller.py"
SPEC = importlib.util.spec_from_file_location("fleet_controller", MODULE_PATH)
fleet_controller = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = fleet_controller
SPEC.loader.exec_module(fleet_controller)


def completed(payload, returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, json.dumps(payload), stderr)


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return self.results.pop(0)


class FleetControllerTest(unittest.TestCase):
    def test_run_once_claims_dispatches_without_wait_and_records_delivery(self):
        command = {
            "apiVersion": "fleet.harness/v1",
            "kind": "Command",
            "metadata": {
                "id": "cmd-1",
                "fleet_id": "demo",
                "timestamp": "2026-09-01T12:00:00Z",
            },
            "spec": {
                "source": {"type": "member", "ref": "manager"},
                "target": {"type": "member", "ref": "worker-1"},
                "type": "message.send",
                "payload": {"text": "hello"},
            },
        }
        runner = FakeRunner(
            [
                completed(
                    {
                        "ok": True,
                        "result": {
                            "command": command,
                            "delivery": {"lease_token": "lease-1"},
                        },
                    }
                ),
                completed(
                    {
                        "ok": True,
                        "result": {"command_id": "cmd-1", "status": "sending"},
                    }
                ),
                completed({"ok": True, "result": {"status": "submitted"}}),
                completed(
                    {
                        "ok": True,
                        "result": {"command_id": "cmd-1", "status": "delivered"},
                    }
                ),
            ]
        )
        controller = fleet_controller.FleetController(
            ["fleet-control"], ["fleet-herdr"], runner=runner
        )

        result = controller.run_once(
            core_db="/tmp/core.sqlite3",
            herdr_db="/tmp/herdr.sqlite3",
            fleet_id="demo",
            worker_id="delivery-1",
        )

        self.assertEqual("delivered", result["status"])
        self.assertIn("delivery.claim", runner.calls[0][0])
        self.assertIn("delivery.begin", runner.calls[1][0])
        self.assertIn("--no-wait", runner.calls[2][0])
        self.assertIn("delivery.result", runner.calls[3][0])

    def test_dispatch_failure_after_send_begins_is_recorded_as_unknown(self):
        command = {
            "apiVersion": "fleet.harness/v1",
            "kind": "Command",
            "metadata": {
                "id": "cmd-1",
                "fleet_id": "demo",
                "timestamp": "2026-09-01T12:00:00Z",
            },
            "spec": {
                "source": {"type": "member", "ref": "manager"},
                "target": {"type": "member", "ref": "worker-1"},
                "type": "message.send",
                "payload": {"text": "hello"},
            },
        }
        runner = FakeRunner(
            [
                completed(
                    {
                        "ok": True,
                        "result": {
                            "command": command,
                            "delivery": {"lease_token": "lease-1"},
                        },
                    }
                ),
                completed(
                    {
                        "ok": True,
                        "result": {"command_id": "cmd-1", "status": "sending"},
                    }
                ),
                completed({}, returncode=2, stderr="adapter failed"),
                completed(
                    {
                        "ok": True,
                        "result": {"command_id": "cmd-1", "status": "unknown"},
                    }
                ),
            ]
        )
        controller = fleet_controller.FleetController(
            ["fleet-control"], ["fleet-herdr"], runner=runner
        )

        result = controller.run_once(
            core_db="/tmp/core.sqlite3",
            herdr_db="/tmp/herdr.sqlite3",
            fleet_id="demo",
            worker_id="delivery-1",
        )

        self.assertEqual("unknown", result["status"])
        result_call = runner.calls[3][0]
        self.assertEqual("unknown", result_call[result_call.index("--result") + 1])

    def test_run_once_is_idle_when_no_command_is_available(self):
        runner = FakeRunner([completed({"ok": True, "result": None})])
        controller = fleet_controller.FleetController(
            ["fleet-control"], ["fleet-herdr"], runner=runner
        )

        result = controller.run_once(
            core_db="/tmp/core.sqlite3",
            herdr_db="/tmp/herdr.sqlite3",
            fleet_id="demo",
            worker_id="delivery-1",
        )

        self.assertEqual("idle", result["status"])
        self.assertEqual(1, len(runner.calls))


if __name__ == "__main__":
    unittest.main()
