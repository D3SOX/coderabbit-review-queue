import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'src/claude-steer.py'
SPEC = importlib.util.spec_from_file_location('claude_steer', SCRIPT)
claude_steer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(claude_steer)


class ClaudeSteerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.session = '11111111-2222-3333-4444-555555555555'
        sessions = self.root / 'sessions'
        sessions.mkdir()
        self.inbox = self.root / 'agent.sock'
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.inbox))
        self.server.listen()
        self.server.settimeout(2)
        self.addCleanup(self.server.close)
        self.entry = {'sessionId': self.session, 'pid': os.getpid(), 'peerProtocol': 1,
                      'status': 'busy', 'procStart': 'test-start', 'messagingSocketPath': str(self.inbox)}
        self.metadata = sessions / f'{os.getpid()}.json'
        self.metadata.write_text(json.dumps(self.entry))
        digest = hashlib.sha256(str(self.inbox).encode()).hexdigest()
        self.key = sessions / f'{os.getpid()}.{digest}.key'
        self.key.write_text(json.dumps({'peerToken': 'a' * 32, 'procStart': 'test-start'}))
        self.key.chmod(0o600)
        self.transcript = self.root / 'projects/worktree' / f'{self.session}.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.transcript.touch()
        self.home = patch.object(claude_steer, 'claude_home', return_value=self.root)
        self.home.start()
        self.addCleanup(self.home.stop)

    def test_busy_session_receives_authenticated_feedback_and_confirms_it(self):
        received = []
        def receive():
            with self.server.accept()[0] as connection, connection.makefile() as frames:
                received.extend(json.loads(frames.readline()) for _ in range(2))
            self.transcript.write_text(json.dumps({'type': 'user', 'uuid': received[1]['uuid']}) + '\n')
        worker = threading.Thread(target=receive)
        worker.start()
        try:
            claude_steer.send_feedback(self.session, 'Resolve review')
        finally:
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(received[0], {'type': 'auth', 'token': 'a' * 32})
        self.assertEqual(received[1]['message']['content'], 'Resolve review')
        self.assertEqual(received[1]['priority'], 'now')

    def test_wait_idle_does_not_send_to_a_busy_session(self):
        with self.assertRaisesRegex(RuntimeError, 'not idle'):
            claude_steer.send_feedback(self.session, 'Resolve review', wait_idle=True)

    def test_live_native_owner_checks_repository_and_branch(self):
        self.entry['cwd'] = str(self.root)
        self.metadata.write_text(json.dumps(self.entry))
        origin = type('Result', (), {'returncode': 0, 'stdout': 'git@github.com:Example/Repo.git\n'})()
        branch = type('Result', (), {'returncode': 0, 'stdout': 'pr-branch\n'})()
        with patch.object(claude_steer.subprocess, 'run', side_effect=[origin, branch]):
            self.assertEqual(claude_steer.session_owner('EXAMPLE/REPO', 'pr-branch'), 'claude')
        with patch.object(claude_steer.subprocess, 'run', return_value=origin):
            self.assertEqual(claude_steer.session_owner('other/repo', 'pr-branch'), '')
        with patch.object(claude_steer.subprocess, 'run', side_effect=[origin, branch]):
            self.assertEqual(claude_steer.session_owner('example/repo', 'other-branch'), '')

    def test_insecure_key_wrong_session_and_protocol_are_rejected(self):
        self.key.chmod(0o644)
        with self.assertRaises(RuntimeError):
            claude_steer.live_session(self.session)
        self.key.chmod(0o600)
        with self.assertRaises(RuntimeError):
            claude_steer.live_session('unrelated-session')
        self.entry['peerProtocol'] = 2
        self.metadata.write_text(json.dumps(self.entry))
        with self.assertRaises(RuntimeError):
            claude_steer.live_session(self.session)

    def test_feedback_is_not_confirmed_without_transcript_acknowledgment(self):
        def receive():
            with self.server.accept()[0] as connection:
                connection.recv(65536)
        worker = threading.Thread(target=receive)
        worker.start()
        try:
            with patch.object(claude_steer.time, 'monotonic', side_effect=[0, 11]):
                with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                    claude_steer.send_feedback(self.session, 'Resolve review')
        finally:
            worker.join(timeout=3)
