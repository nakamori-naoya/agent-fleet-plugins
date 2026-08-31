import copy
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[2] / "hooks" / "role_context.py"
SPEC = importlib.util.spec_from_file_location("role_context_hook", MODULE_PATH)
role_context_hook = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(role_context_hook)


class RoleContextHookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "session-context.sqlite3"
        self.context = {
            "fleet_id": "demo",
            "context_revision": 3,
            "agent": {"agent_ref": "worker-1", "role_ref": "worker@1"},
            "fleet": {
                "objective": "Ship verified changes.",
                "completion_criteria": ["All checks pass."],
                "stop_conditions": ["Context is inconsistent."],
            },
            "assignments": [
                {
                    "task_id": "task-1",
                    "status": "assigned",
                    "instructions": "Run tests.",
                    "expected_output": "Test result",
                    "completion_criteria": ["Tests pass."],
                }
            ],
            "reporting": {
                "manager_ref": "manager",
                "strategy": "manager",
                "completion_requires_manager_acceptance": True,
            },
        }

    def command(self, *, context=None, fleet_id="demo", agent_ref="worker-1"):
        return {
            "apiVersion": "fleet.harness/v1",
            "kind": "Command",
            "metadata": {
                "fleet_id": fleet_id,
                "id": "cmd-1",
                "timestamp": "2026-09-01T00:00:00+00:00",
            },
            "spec": {
                "source": {"type": "member", "ref": "manager"},
                "type": "context.sync",
                "target": {"type": "member", "ref": agent_ref},
                "context": context or self.context,
                "payload": {"control": {"report_command": "fleet-control task.report"}},
            },
        }

    def submit(self, command, *, product="codex", session_id="session-1"):
        with mock.patch.object(role_context_hook, "_confirm_context", return_value=None):
            return role_context_hook.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session_id,
                    "prompt": role_context_hook.encode_fleet_prompt(command),
                },
                self.db,
                runtime_product=product,
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_fleet_prompt_binds_session_and_compaction_restores_context(self):
        submitted = self.submit(self.command())
        restored = role_context_hook.handle(
            {
                "hook_event_name": "SessionStart",
                "session_id": "session-1",
                "source": "compact",
            },
            self.db,
            runtime_product="codex",
        )

        self.assertIn("worker-1", submitted["hookSpecificOutput"]["additionalContext"])
        restored_text = restored["hookSpecificOutput"]["additionalContext"]
        self.assertIn("worker@1", restored_text)
        self.assertIn("fleet-control task.report", restored_text)

    def test_unrelated_prompt_and_unknown_session_receive_no_fleet_context(self):
        self.assertEqual(
            {},
            role_context_hook.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "prompt": "ordinary prompt",
                },
                self.db,
                runtime_product="codex",
            ),
        )
        self.assertEqual(
            {},
            role_context_hook.handle(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "unknown",
                    "source": "resume",
                },
                self.db,
                runtime_product="codex",
            ),
        )

    def test_same_session_id_is_isolated_by_runtime_product(self):
        claude_context = dict(self.context)
        claude_context["agent"] = {"agent_ref": "worker-2", "role_ref": "reviewer@1"}
        self.submit(self.command(), product="codex")
        self.submit(
            self.command(context=claude_context, agent_ref="worker-2"), product="claude"
        )

        codex = role_context_hook.handle(
            {"hook_event_name": "SessionStart", "session_id": "session-1", "source": "resume"},
            self.db,
            runtime_product="codex",
        )
        claude = role_context_hook.handle(
            {"hook_event_name": "SessionStart", "session_id": "session-1", "source": "resume"},
            self.db,
            runtime_product="claude",
        )

        self.assertIn("worker-1", codex["hookSpecificOutput"]["additionalContext"])
        self.assertIn("worker-2", claude["hookSpecificOutput"]["additionalContext"])

    def test_active_session_refreshes_latest_context_on_every_ordinary_prompt(self):
        self.submit(self.command())
        revision_four = copy.deepcopy(self.context)
        revision_four["context_revision"] = 4
        revision_four["assignments"][0]["instructions"] = "Run the complete suite."
        self.submit(self.command(context=revision_four))

        refreshed = role_context_hook.handle(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-1",
                "prompt": "ordinary follow-up",
            },
            self.db,
            runtime_product="codex",
        )

        text = refreshed["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(
            "UserPromptSubmit", refreshed["hookSpecificOutput"]["hookEventName"]
        )
        self.assertIn('"context_revision": 4', text)
        self.assertIn("Run the complete suite.", text)

    def test_active_session_blocks_ordinary_prompt_when_context_cannot_be_read(self):
        self.submit(self.command())

        with mock.patch.object(
            role_context_hook, "_connect", side_effect=sqlite3.OperationalError("locked")
        ):
            result = role_context_hook.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "prompt": "ordinary follow-up",
                },
                self.db,
                runtime_product="codex",
            )

        self.assertEqual("block", result["decision"])
        self.assertIn("確認", result["reason"])

    def test_stale_revision_and_unconditional_rebind_are_blocked(self):
        self.submit(self.command())
        stale = dict(self.context)
        stale["context_revision"] = 2
        stale_result = self.submit(self.command(context=stale))

        other = dict(self.context)
        other["fleet_id"] = "other"
        other["agent"] = {"agent_ref": "other-worker", "role_ref": "worker@1"}
        rebound = self.submit(
            self.command(context=other, fleet_id="other", agent_ref="other-worker")
        )

        self.assertEqual("block", stale_result["decision"])
        self.assertEqual("block", rebound["decision"])

    def test_claude_fork_gets_a_durable_unbound_state_under_its_new_session_id(self):
        self.submit(self.command(), product="claude", session_id="source-session")

        forked = role_context_hook.handle(
            {"hook_event_name": "SessionStart", "session_id": "fork-session", "source": "fork"},
            self.db,
            runtime_product="claude",
        )
        resumed = role_context_hook.handle(
            {"hook_event_name": "SessionStart", "session_id": "fork-session", "source": "resume"},
            self.db,
            runtime_product="claude",
        )
        source = role_context_hook.handle(
            {"hook_event_name": "SessionStart", "session_id": "source-session", "source": "resume"},
            self.db,
            runtime_product="claude",
        )

        self.assertIn("unbound", forked["hookSpecificOutput"]["additionalContext"])
        self.assertIn("旧役割文脈は無効", forked["hookSpecificOutput"]["additionalContext"])
        self.assertIn("unbound", resumed["hookSpecificOutput"]["additionalContext"])
        self.assertIn("worker-1", source["hookSpecificOutput"]["additionalContext"])

        activated = self.submit(
            self.command(), product="claude", session_id="fork-session"
        )
        self.assertIn("worker-1", activated["hookSpecificOutput"]["additionalContext"])

    def test_malformed_or_invalid_fleet_activation_prompt_is_blocked(self):
        malformed = role_context_hook.handle(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-1",
                "prompt": role_context_hook.PROMPT_PREFIX + "{not-json",
            },
            self.db,
            runtime_product="codex",
        )
        invalid = self.submit({"kind": "Command", "metadata": {}, "spec": {}})
        wrong_contract = self.command()
        del wrong_contract["apiVersion"]
        wrong_contract_result = self.submit(wrong_contract, session_id="session-2")

        self.assertEqual("block", malformed["decision"])
        self.assertEqual("block", invalid["decision"])
        self.assertEqual("block", wrong_contract_result["decision"])

    def test_activation_db_failure_is_blocked(self):
        with mock.patch.object(
            role_context_hook, "_connect", side_effect=sqlite3.OperationalError("locked")
        ):
            result = self.submit(self.command())

        self.assertEqual("block", result["decision"])
        self.assertIn("保存", result["reason"])

    def test_activation_is_blocked_when_core_cannot_confirm_current_context(self):
        with mock.patch.object(
            role_context_hook,
            "_confirm_context",
            return_value="役割文脈の受領をCoreへ確認できませんでした。",
        ):
            result = role_context_hook.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "prompt": role_context_hook.encode_fleet_prompt(self.command()),
                },
                self.db,
                runtime_product="codex",
            )

        self.assertEqual("block", result["decision"])
        subsequent = role_context_hook.handle(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-1",
                "prompt": "ordinary follow-up",
            },
            self.db,
            runtime_product="codex",
        )
        self.assertEqual("block", subsequent["decision"])

    def test_context_confirmation_uses_argv_without_a_shell(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(role_context_hook.subprocess, "run", return_value=completed) as run:
            error = role_context_hook._confirm_context(
                {"context_confirm_argv": ["fleet-control", "context.confirm"]}, 4
            )

        self.assertIsNone(error)
        self.assertEqual(
            ["fleet-control", "context.confirm", "--revision", "4"],
            run.call_args.args[0],
        )
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_session_failure_output_respects_each_product_contract(self):
        claude = role_context_hook._session_failure("claude", "failed")
        codex = role_context_hook._session_failure("codex", "failed")

        self.assertEqual({"systemMessage": "failed"}, claude)
        self.assertFalse(codex["continue"])
        self.assertEqual("failed", codex["stopReason"])

    def test_default_db_uses_runtime_specific_plugin_data(self):
        with mock.patch.dict(
            os.environ,
            {"PLUGIN_DATA": "/tmp/codex-data", "CLAUDE_PLUGIN_DATA": "/tmp/claude-data"},
            clear=True,
        ):
            self.assertEqual(
                Path("/tmp/codex-data/session-context.sqlite3"),
                role_context_hook._default_db("codex"),
            )
            self.assertEqual(
                Path("/tmp/claude-data/session-context.sqlite3"),
                role_context_hook._default_db("claude"),
            )

    def test_product_manifests_use_product_specific_hook_configs(self):
        plugin_root = MODULE_PATH.parents[1]
        claude_manifest = json.loads(
            (plugin_root / ".claude-plugin" / "plugin.json").read_text()
        )
        codex_manifest = json.loads(
            (plugin_root / ".codex-plugin" / "plugin.json").read_text()
        )
        claude_hooks = json.loads((plugin_root / "hooks" / "claude-hooks.json").read_text())
        codex_hooks = json.loads((plugin_root / "hooks" / "codex-hooks.json").read_text())

        self.assertEqual("./hooks/claude-hooks.json", claude_manifest["hooks"])
        self.assertEqual("./hooks/codex-hooks.json", codex_manifest["hooks"])
        self.assertEqual(
            "startup|resume|clear|compact|fork",
            claude_hooks["hooks"]["SessionStart"][0]["matcher"],
        )
        self.assertEqual(
            "startup|resume|clear|compact",
            codex_hooks["hooks"]["SessionStart"][0]["matcher"],
        )
        claude_handler = claude_hooks["hooks"]["SessionStart"][0]["hooks"][0]
        codex_handler = codex_hooks["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual("python3", claude_handler["command"])
        self.assertIn("--runtime-product", claude_handler["args"])
        self.assertNotIn("additionalContextLimit", claude_handler)
        self.assertEqual(5000, codex_handler["additionalContextLimit"])


if __name__ == "__main__":
    unittest.main()
