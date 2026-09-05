"""Registration retry uses virtual time and fixture results, never Herdr."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))
from herdr_adapter import AdapterState, HerdrAdapter, HerdrAdapterError


class RegistrationWaitTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = AdapterState(Path(temp.name) / 'state.sqlite3')
        self.now = 0.0
        self.calls = []
        self.results = []
        self.sleeps = []
        self.adapter = HerdrAdapter(self.state, runner=self.run_fixture,
                                    sleeper=self.sleep, clock=lambda: self.now)
        self.argv = ['herdr', 'agent', 'wait', 'pane', '--until', 'idle', '--timeout', '3000']

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def run_fixture(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    @staticmethod
    def error(code):
        return subprocess.CompletedProcess([], 1, '', json.dumps({'error': {'code': code}}))

    def wait(self):
        return self.adapter._execute_argv(self.argv, 'herdr agent.wait:manager', wait_for_registration=True)

    def test_late_registration_retries_only_wait_with_remaining_deadline(self):
        ready = subprocess.CompletedProcess([], 0, '{"agent_status":"idle"}', '')
        self.results = [self.error('agent_not_found'), self.error('agent_not_found'), ready]
        self.assertIs(ready, self.wait())
        self.assertEqual([3000, 2000, 1000], [int(argv[-1]) for argv, _ in self.calls])
        self.assertEqual([3, 2, 1], [kw['timeout'] for _, kw in self.calls])
        self.assertTrue(all(argv[:3] == ['herdr', 'agent', 'wait'] for argv, _ in self.calls))

    def test_permanent_missing_has_one_total_time_budget(self):
        self.results = [self.error('agent_not_found')] * 10
        with self.assertRaisesRegex(HerdrAdapterError, 'registration wait timed out'):
            self.wait()
        self.assertEqual(3, self.now)
        self.assertEqual(3, len(self.calls))

    def test_non_registration_errors_and_unstructured_mentions_fail_immediately(self):
        for error in [self.error('permission_denied'), self.error('timeout'),
                      subprocess.CompletedProcess([], 1, '', 'agent_not_found'),
                      subprocess.CompletedProcess([], 1, '', '{"message":"agent_not_found"}'),
                      subprocess.CompletedProcess([], 1, '', '{"error":null}')]:
            with self.subTest(stderr=error.stderr):
                self.calls.clear(); self.sleeps.clear(); self.results = [error]
                with self.assertRaises(HerdrAdapterError): self.wait()
                self.assertEqual(1, len(self.calls)); self.assertEqual([], self.sleeps)

    def test_transport_timeout_is_not_retried(self):
        self.results = [subprocess.TimeoutExpired('fixture', 3)]
        with self.assertRaisesRegex(HerdrAdapterError, 'timed out'): self.wait()
        self.assertEqual(1, len(self.calls)); self.assertEqual([], self.sleeps)

    def test_already_registered_succeeds_without_sleep(self):
        self.results = [subprocess.CompletedProcess([], 0, '{}', '')]
        self.wait()
        self.assertEqual(1, len(self.calls)); self.assertEqual([], self.sleeps)

    def test_startup_ready_accepts_idle_or_done_but_not_blocked(self):
        # Official src/api/wait.rs matches the explicit status list exactly.
        for status in ('idle', 'done', 'blocked', 'working', 'unknown'):
            with self.subTest(status=status):
                self.argv = self.adapter.commands.agent_wait('pane', timeout_ms=3000)
                until = [self.argv[index + 1] for index, value in enumerate(self.argv) if value == '--until']
                self.assertEqual(['idle', 'done'], until)
                self.results = [subprocess.CompletedProcess([], 0, json.dumps({'agent_status': status}), '')
                                if status in until else self.error('timeout')]
                if status in ('idle', 'done'):
                    self.assertEqual(status, json.loads(self.wait().stdout)['agent_status'])
                else:
                    with self.assertRaises(HerdrAdapterError): self.wait()
