import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 't3-delegate.py'
SPEC = importlib.util.spec_from_file_location('t3_delegate', SCRIPT)
t3_delegate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t3_delegate)


class T3DelegateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'userdata'
        self.home.mkdir()
        with sqlite3.connect(self.home / 'state.sqlite') as connection:
            connection.executescript('''
                CREATE TABLE provider_session_runtime
                  (thread_id TEXT, provider_name TEXT, resume_cursor_json TEXT);
                CREATE TABLE projection_threads
                  (thread_id TEXT, runtime_mode TEXT, interaction_mode TEXT,
                   model_selection_json TEXT, deleted_at TEXT, archived_at TEXT);
                CREATE TABLE projection_thread_sessions
                  (thread_id TEXT, status TEXT);
                INSERT INTO provider_session_runtime VALUES
                  ('t3-thread', 'codex', '{"threadId":"codex-session"}');
                INSERT INTO projection_threads VALUES
                  ('t3-thread', 'full-access', 'default',
                   '{"instanceId":"codex","model":"gpt-6-sol"}', NULL, NULL);
                INSERT INTO projection_thread_sessions VALUES ('t3-thread', 'ready');
            ''')
        (self.home / 'server-runtime.json').write_text(
            json.dumps({'origin': 'http://127.0.0.1:3773'})
        )

    def test_matching_session_uses_t3_runtime_and_model(self):
        result = t3_delegate.matching_thread(self.home / 'state.sqlite', 'codex-session')
        self.assertEqual(result, (
            't3-thread', 'full-access', 'default',
            {'instanceId': 'codex', 'model': 'gpt-6-sol'},
        ))

    def test_running_thread_is_not_sent_duplicate_prompt(self):
        with sqlite3.connect(self.home / 'state.sqlite') as connection:
            connection.execute("UPDATE projection_thread_sessions SET status = 'running'")
        with self.assertRaisesRegex(RuntimeError, 'not sending another turn'):
            t3_delegate.matching_thread(self.home / 'state.sqlite', 'codex-session')

    def test_dispatch_uses_t3_command_and_revokes_credential(self):
        calls = []

        def run(args, **_kwargs):
            calls.append(args)
            if 'issue' in args:
                return type('Result', (), {'stdout': json.dumps({
                    'token': 'secret', 'sessionId': 'auth-session',
                })})()
            return type('Result', (), {'stdout': ''})()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{}'

        requests = []

        def open_request(request, **_kwargs):
            requests.append(request)
            return Response()

        with patch.object(t3_delegate, 't3_home', return_value=self.home), \
             patch.object(t3_delegate, 't3_cli', return_value=['t3']), \
             patch.object(t3_delegate.subprocess, 'run', side_effect=run), \
             patch.object(t3_delegate, 'urlopen', side_effect=open_request):
            t3_delegate.send_turn('codex-session', 'Fix the review')

        self.assertEqual(requests[0].full_url, 'http://127.0.0.1:3773/api/orchestration/dispatch')
        command = json.loads(requests[0].data)
        self.assertEqual(command['type'], 'thread.turn.start')
        self.assertEqual(command['threadId'], 't3-thread')
        self.assertEqual(command['message']['text'], 'Fix the review')
        self.assertEqual(command['runtimeMode'], 'full-access')
        self.assertEqual(command['modelSelection']['model'], 'gpt-6-sol')
        self.assertIn('issue', calls[0])
        self.assertIn('revoke', calls[1])
        self.assertEqual(calls[1][-1], 'auth-session')


if __name__ == '__main__':
    unittest.main()
