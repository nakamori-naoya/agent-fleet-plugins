import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parents[2]
ROLE_CONTEXT = PLUGIN_ROOT / "hooks" / "role_context.py"
CODEX_HOOKS = PLUGIN_ROOT / "hooks" / "codex-hooks.json"
CLAUDE_HOOKS = PLUGIN_ROOT / "hooks" / "claude-hooks.json"


class HookLauncherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _hook(path: Path, event: str = "UserPromptSubmit") -> dict:
        document = json.loads(path.read_text(encoding="utf-8"))
        return document["hooks"][event][0]["hooks"][0]

    def _runtime(self) -> Path:
        path = self.root / "fleet-state/hooks/runtime/role_context.py"
        path.parent.mkdir(parents=True)
        path.write_bytes(ROLE_CONTEXT.read_bytes())
        path.chmod(0o600)
        return path

    def _run_codex(
        self,
        runtime: Path | None,
        *,
        stdin: str = "payload",
        event: str = "UserPromptSubmit",
    ) -> subprocess.CompletedProcess[str]:
        hook = self._hook(CODEX_HOOKS, event)
        env = dict(os.environ)
        if runtime is None:
            env.pop("AGENT_FLEET_HOOK_RUNTIME", None)
        else:
            env["AGENT_FLEET_HOOK_RUNTIME"] = str(runtime)
        return subprocess.run(
            hook["command"],
            shell=True,
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )

    def _run_claude(
        self,
        runtime: Path | None,
        *,
        stdin: str = "payload",
        event: str = "UserPromptSubmit",
    ) -> subprocess.CompletedProcess[str]:
        hook = self._hook(CLAUDE_HOOKS, event)
        env = dict(os.environ)
        if runtime is None:
            env.pop("AGENT_FLEET_HOOK_RUNTIME", None)
        else:
            env["AGENT_FLEET_HOOK_RUNTIME"] = str(runtime)
        return subprocess.run(
            [hook["command"], *hook["args"]],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )

    def test_unbound_process_without_runtime_is_a_noop(self):
        codex = self._run_codex(None)
        claude = self._run_claude(None)

        self.assertEqual(0, codex.returncode, codex.stderr)
        self.assertEqual("", codex.stdout)
        self.assertEqual(0, claude.returncode, claude.stderr)
        self.assertEqual("", claude.stdout)

    def test_codex_executes_verified_fleet_runtime_and_preserves_stdin(self):
        runtime = self._runtime()
        event = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "AGENT_FLEET_COMMAND_V1\n{}",
            }
        )
        completed = self._run_codex(runtime, stdin=event)

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("block", result["decision"])

    def test_claude_executes_verified_fleet_runtime(self):
        runtime = self._runtime()
        event = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "AGENT_FLEET_COMMAND_V1\n{}",
            }
        )
        completed = self._run_claude(runtime, stdin=event)

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("block", result["decision"])

    def test_hook_config_only_dispatches_to_the_materialized_runtime(self):
        codex = self._hook(CODEX_HOOKS)
        claude = self._hook(CLAUDE_HOOKS)
        codex_command = codex["command"]
        claude_program = claude["args"][1]

        self.assertNotIn("PLUGIN_ROOT", codex_command)
        self.assertNotIn("PLUGIN_ROOT", claude_program)
        self.assertNotIn("plugins/cache", codex_command)
        self.assertNotIn("python3 -c", codex_command)
        self.assertNotIn("python3 -c", claude_program)
        self.assertLess(len(codex_command), 160)
        self.assertLess(len(claude_program), 160)
        self.assertIn("AGENT_FLEET_HOOK_RUNTIME", codex_command)
        self.assertIn("AGENT_FLEET_HOOK_RUNTIME", claude_program)
        self.assertEqual(
            codex_command,
            self._hook(CODEX_HOOKS, "SessionStart")["command"],
        )
        self.assertEqual(
            claude["args"],
            self._hook(CLAUDE_HOOKS, "SessionStart")["args"],
        )


if __name__ == "__main__":
    unittest.main()
