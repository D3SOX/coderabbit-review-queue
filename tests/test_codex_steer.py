import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('codex_steer', Path(__file__).resolve().parents[1] / 'src/codex-steer.py')
codex_steer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(codex_steer)


class CodexSteeringTests(unittest.TestCase):
    def test_websocket_client_masks_extended_length_messages(self):
        connection = object.__new__(codex_steer.CodexConnection)
        connection.socket = Mock()
        payload = b'x' * 200
        with patch.object(codex_steer.os, 'urandom', return_value=b'abcd'):
            connection.send_frame(payload)
        frame = connection.socket.sendall.call_args.args[0]
        self.assertEqual(frame[:4], b'\x81\xfe\x00\xc8')
        mask = frame[4:8]
        self.assertEqual(bytes(b ^ mask[i % 4] for i, b in enumerate(frame[8:])), payload)

    def test_fragmented_reply_with_interleaved_ping(self):
        connection = object.__new__(codex_steer.CodexConnection)
        connection.read_exact = Mock(side_effect=[
            b'\x01\x04', b'{"id', b'\x89\x01', b'?', b'\x80\x04', b'":1}',
        ])
        connection.send_frame = Mock()
        self.assertEqual(connection.receive(), {'id': 1})
        connection.send_frame.assert_called_once_with(b'?', 10)

    def test_steering_targets_existing_turn_without_settings_overrides(self):
        connection = Mock()
        connection.request.side_effect = [
            {'thread': {'status': {'type': 'active'}}},
            {'data': [{'id': 'turn', 'status': 'inProgress'}]},
            {'turnId': 'turn'},
        ]
        codex_steer.steer(connection, 'session', 'Review feedback')
        method, params = connection.request.call_args.args
        self.assertEqual(method, 'turn/steer')
        self.assertEqual(params, {'threadId': 'session', 'expectedTurnId': 'turn',
                                 'input': [{'type': 'text', 'text': 'Review feedback'}]})

    def test_unloaded_or_finished_turn_is_never_resumed(self):
        for status in ('idle', 'notLoaded'):
            connection = Mock()
            connection.request.return_value = {'thread': {'status': {'type': status}}}
            with self.assertRaisesRegex(RuntimeError, 'not active'):
                codex_steer.steer(connection, 'session', 'Review feedback')
            self.assertEqual(connection.request.call_count, 1)
