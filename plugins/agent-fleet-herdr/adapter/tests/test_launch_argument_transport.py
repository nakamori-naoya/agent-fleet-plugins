"""Execute only capture fixtures; never start Herdr or an agent product."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))
from herdr_adapter import AdapterState, Herdr08Commands, HerdrAdapter


class LaunchArgumentTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.capture = self.root / "capture with spaces.py"
        self.capture.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "print(json.dumps([os.fsencode(x).hex() for x in sys.argv[1:]]))\n",
            encoding="utf-8",
        )
        self.capture.chmod(0o700)
        self.commands = Herdr08Commands(str(self.capture))
        self.adapter = HerdrAdapter(AdapterState(self.root / "state.sqlite3"), self.commands)
        self.values = [
            "--settings", json.dumps({"switchModelsOnFlag": False, "text": "日本語 'single' \"double\"\nline"}, ensure_ascii=False),
            "--config", 'model_reasoning_effort="high"',
            "space 'single' \"double\" \\slash $HOME $(echo forbidden) `echo forbidden`\nsecond\tline",
        ]

    def assert_transport(self, argv):
        completed = self.adapter._execute_argv(argv, "capture fixture")
        self.assertEqual([os.fsencode(value).hex() for value in argv[1:]], json.loads(completed.stdout))

    def test_agent_start_preserves_each_argument_byte(self):
        self.assert_transport(self.commands.agent_start("manager", "claude", "pane", self.values))

    def test_command_profile_preserves_herdr_input_argument_bytes(self):
        self.assert_transport(self.commands.agent_run("claude-personal", "pane", self.values))

    def test_workspace_environment_preserves_value_bytes_at_herdr_input(self):
        self.assert_transport(self.commands.workspace_create(str(self.root), "fixture", {"AGENT_FLEET_CORE_DB": self.values[-1]}))

    def test_profile_manager_receives_json_toml_and_literal_shell_characters(self):
        # Herdr 0.8 pane.rs pane_run joins arguments after pane_id with spaces,
        # then sends that text plus Enter to the pane's shell.
        for shell in ("/bin/bash", "/bin/zsh"):
            if not Path(shell).exists():
                continue
            with self.subTest(shell=shell):
                marker = self.root / "must-not-exist"
                values = [
                    "--settings", json.dumps({"switchModelsOnFlag": False, "text": "日本語 'single' \"double\"\nline"}, ensure_ascii=False),
                    "--config", 'model_reasoning_effort="high"',
                    "white space\ttab\nline", "'single' \"double\" \\literal",
                    '$(touch "' + str(marker) + '")', '`touch "' + str(marker) + '"`', "$HOME; *",
                ]
                argv = self.commands.agent_run(str(self.capture), "pane", values)
                shell_text = " ".join(argv[4:])
                completed = subprocess.run([shell, "-c", shell_text], capture_output=True, text=True, timeout=5)
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual([os.fsencode(value).hex() for value in values], json.loads(completed.stdout))
                self.assertFalse(marker.exists())

    def test_profile_json_and_toml_remain_parseable_at_manager(self):
        capture = self.root / "capture"
        capture.write_bytes(self.capture.read_bytes())
        capture.chmod(0o700)
        values = ["--settings", '{"switchModelsOnFlag":false}', "--config", 'model_reasoning_effort="high"']
        argv = self.commands.agent_run(str(capture), "pane", values)
        completed = subprocess.run(["/bin/bash", "-c", " ".join(argv[4:])], capture_output=True, text=True, check=True)
        actual = [bytes.fromhex(value).decode() for value in json.loads(completed.stdout)]
        self.assertEqual(values, actual)
        self.assertEqual({"switchModelsOnFlag": False}, json.loads(actual[1]))
