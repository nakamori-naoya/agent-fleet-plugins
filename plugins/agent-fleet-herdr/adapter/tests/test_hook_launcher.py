import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parents[2]
CODEX_HOOKS = PLUGIN_ROOT / "hooks" / "codex-hooks.json"
CLAUDE_HOOKS = PLUGIN_ROOT / "hooks" / "claude-hooks.json"


class HookLauncherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _implementation(self, product: str, version: str, marker: str) -> Path:
        config_root = self.root / product
        path = (
            config_root
            / "plugins/cache/agent-fleet/agent-fleet-herdr"
            / version
            / "hooks/role_context.py"
        )
        path.parent.mkdir(parents=True)
        path.write_text(
            "import json,sys\n"
            f"print(json.dumps({{'marker': {marker!r}, 'argv': sys.argv[1:], "
            "'stdin': sys.stdin.read()}))\n",
            encoding="utf-8",
        )
        manifest_dir = path.parents[1] / (
            ".codex-plugin" if product == "codex" else ".claude-plugin"
        )
        manifest_dir.mkdir()
        hooks_file = "codex-hooks.json" if product == "codex" else "claude-hooks.json"
        (manifest_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "agent-fleet-herdr",
                    "version": version,
                    "hooks": f"./hooks/{hooks_file}",
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _hook(path: Path, event: str = "UserPromptSubmit") -> dict:
        document = json.loads(path.read_text(encoding="utf-8"))
        return document["hooks"][event][0]["hooks"][0]

    def _run_codex(
        self,
        direct_path: Path,
        *,
        stdin: str = "payload",
        event: str = "UserPromptSubmit",
        hook_trust: str = "review",
    ) -> subprocess.CompletedProcess[str]:
        hook = self._hook(CODEX_HOOKS, event)
        command = hook["command"].replace("${PLUGIN_ROOT}", str(direct_path.parent.parent))
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self.root / "codex")
        env["AGENT_FLEET_CODEX_HOOK_TRUST"] = hook_trust
        return subprocess.run(
            command,
            shell=True,
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )

    def _run_claude(
        self,
        direct_path: Path,
        *,
        stdin: str = "payload",
        event: str = "UserPromptSubmit",
    ) -> subprocess.CompletedProcess[str]:
        hook = self._hook(CLAUDE_HOOKS, event)
        argv = [
            hook["command"],
            *[
                arg.replace("${CLAUDE_PLUGIN_ROOT}", str(direct_path.parent.parent))
                for arg in hook["args"]
            ],
        ]
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(self.root / "claude")
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )

    def test_codex_missing_loaded_version_falls_back_to_latest_installed_version(self):
        self._implementation("codex", "0.2.7", "current")
        missing = self.root / "codex/plugins/cache/agent-fleet/agent-fleet-herdr/0.2.1/hooks/role_context.py"

        completed = self._run_codex(missing, hook_trust="preapproved")

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("current", result["marker"])
        self.assertEqual(["--runtime-product", "codex"], result["argv"])
        self.assertEqual("payload", result["stdin"])

    def test_claude_missing_loaded_version_falls_back_to_latest_installed_version(self):
        self._implementation("claude", "0.2.7", "current")
        missing = self.root / "claude/plugins/cache/agent-fleet/agent-fleet-herdr/0.2.1/hooks/role_context.py"

        completed = self._run_claude(missing)

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("current", result["marker"])
        self.assertEqual(["--runtime-product", "claude"], result["argv"])

    def test_direct_loaded_version_is_preferred_while_it_exists(self):
        direct = self._implementation("codex", "0.2.6", "loaded")
        self._implementation("codex", "0.2.7", "newer")

        completed = self._run_codex(direct)

        self.assertEqual("loaded", json.loads(completed.stdout)["marker"])

    def test_codex_review_mode_blocks_cross_version_fallback(self):
        self._implementation("codex", "0.2.7", "current")
        missing = self.root / "codex/plugins/cache/agent-fleet/agent-fleet-herdr/0.2.1/hooks/role_context.py"
        prompt = json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "ordinary prompt"}
        )

        completed = self._run_codex(missing, stdin=prompt, hook_trust="review")

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("block", result["decision"])
        self.assertIn("再起動", result["reason"])

    def test_fallback_uses_semantic_version_order_and_ignores_unversioned_directory(self):
        self._implementation("codex", "0.9.0", "older")
        self._implementation("codex", "0.10.0", "newer")
        self._implementation("codex", "current", "untrusted")
        missing = self.root / "codex/plugins/cache/agent-fleet/agent-fleet-herdr/0.2.1/hooks/role_context.py"

        completed = self._run_codex(missing, hook_trust="preapproved")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("newer", json.loads(completed.stdout)["marker"])

    def test_invalid_manifest_and_symlink_candidate_are_not_executed(self):
        current = self._implementation("codex", "0.2.7", "current")
        invalid = self._implementation("codex", "0.3.0", "invalid")
        manifest = invalid.parents[1] / ".codex-plugin/plugin.json"
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        metadata["name"] = "another-plugin"
        manifest.write_text(json.dumps(metadata), encoding="utf-8")
        external = self.root / "external.py"
        external.write_text("raise SystemExit(91)\n", encoding="utf-8")
        linked = self._implementation("codex", "0.4.0", "linked")
        linked.unlink()
        linked.symlink_to(external)
        missing = current.parents[2] / "0.2.1/hooks/role_context.py"

        completed = self._run_codex(missing, hook_trust="preapproved")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("current", json.loads(completed.stdout)["marker"])

    def test_invalid_direct_manifest_is_not_executed(self):
        direct = self._implementation("codex", "0.2.6", "invalid-direct")
        manifest = direct.parents[1] / ".codex-plugin/plugin.json"
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        metadata["version"] = "9.9.9"
        manifest.write_text(json.dumps(metadata), encoding="utf-8")
        self._implementation("codex", "0.2.7", "current")

        completed = self._run_codex(direct, hook_trust="preapproved")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("current", json.loads(completed.stdout)["marker"])

    def test_missing_plugin_blocks_with_actionable_feedback(self):
        codex_missing = self.root / "codex/agent-fleet-herdr/0.2.1/hooks/role_context.py"
        claude_missing = self.root / "claude/agent-fleet-herdr/0.2.1/hooks/role_context.py"
        prompt = json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "ordinary prompt"}
        )
        session = json.dumps({"hook_event_name": "SessionStart", "source": "resume"})

        codex = self._run_codex(codex_missing, stdin=prompt)
        claude = self._run_claude(claude_missing, stdin=prompt)
        codex_session = self._run_codex(
            codex_missing, stdin=session, event="SessionStart"
        )

        self.assertEqual(0, codex.returncode, codex.stderr)
        self.assertEqual("block", json.loads(codex.stdout)["decision"])
        self.assertIn("再起動", json.loads(codex.stdout)["reason"])
        self.assertEqual(0, claude.returncode, claude.stderr)
        self.assertEqual("block", json.loads(claude.stdout)["decision"])
        self.assertFalse(json.loads(codex_session.stdout)["continue"])
        self.assertIn("再起動", json.loads(codex_session.stdout)["stopReason"])
