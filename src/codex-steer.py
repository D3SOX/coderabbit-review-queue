#!/usr/bin/env python3
"""Steer an existing turn on the shared Codex Desktop/CLI app-server."""

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import time


class CodexConnection:
    def __init__(self, timeout=10):
        home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(min(5, timeout))
        self.next_id = 0
        self.deadline = time.monotonic() + timeout
        try:
            self.socket.connect(str(home / 'app-server-control/app-server-control.sock'))
            key = base64.b64encode(os.urandom(16)).decode()
            self.socket.sendall((f'GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                                 f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                                 'Sec-WebSocket-Version: 13\r\n\r\n').encode())
            header = bytearray()
            while not header.endswith(b'\r\n\r\n'):
                header.extend(self.read_exact(1))
                if len(header) > 16384:
                    raise RuntimeError('Oversized Codex handshake')
            expected = base64.b64encode(hashlib.sha1(
                (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            headers = dict(line.split(':', 1) for line in header.decode().split('\r\n')[1:] if ':' in line)
            if (' 101 ' not in header.decode().split('\r\n')[0]
                    or {k.lower(): v.strip() for k, v in headers.items()}.get('sec-websocket-accept') != expected):
                raise RuntimeError('Codex WebSocket handshake rejected')
            self.request('initialize', {'clientInfo': {'name': 'coderabbit_review_queue', 'version': '1'},
                                        'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
        except Exception:
            self.close()
            raise

    def close(self):
        self.socket.close()

    def read_exact(self, count):
        data = bytearray()
        while len(data) < count:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Codex RPC timed out')
            self.socket.settimeout(min(5, remaining))
            chunk = self.socket.recv(count - len(data))
            if not chunk:
                raise RuntimeError('Codex connection closed')
            data.extend(chunk)
        return bytes(data)

    def send_frame(self, payload, opcode=1):
        length = len(payload)
        header = bytes([0x80 | opcode, 0x80 | (length if length < 126 else 126 if length <= 65535 else 127)])
        if length >= 126:
            header += struct.pack('!H' if length <= 65535 else '!Q', length)
        mask = os.urandom(4)
        self.socket.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, message):
        self.send_frame(json.dumps(message).encode())

    def receive(self):
        message = bytearray()
        while True:
            first, second = self.read_exact(2)
            opcode, length = first & 15, second & 127
            if length in (126, 127):
                length = struct.unpack('!H' if length == 126 else '!Q', self.read_exact(2 if length == 126 else 8))[0]
            if length + len(message) > 16 * 1024 * 1024:
                raise RuntimeError('Oversized Codex response')
            mask = self.read_exact(4) if second & 128 else None
            payload = self.read_exact(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 8:
                raise RuntimeError('Codex connection closed')
            if opcode == 9:
                self.send_frame(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise RuntimeError('Unexpected Codex WebSocket frame')
            message.extend(payload)
            if first & 128:
                return json.loads(message)

    def request(self, method, params):
        self.next_id += 1
        ident = self.next_id
        self.send({'id': ident, 'method': method, 'params': params})
        for _ in range(1000):
            reply = self.receive()
            if reply.get('id') != ident:
                continue
            if 'error' in reply:
                raise RuntimeError(f'Codex rejected {method}')
            return reply['result']
        raise RuntimeError('Too many unrelated Codex notifications')


def active_turn(connection, session):
    thread = connection.request('thread/read', {'threadId': session})['thread']
    if thread.get('status', {}).get('type') != 'active':
        raise RuntimeError('Codex thread is not active on this app-server')
    turns = connection.request('thread/turns/list', {
        'threadId': session, 'limit': 1, 'sortDirection': 'desc', 'itemsView': 'notLoaded',
    })['data']
    if len(turns) != 1 or turns[0].get('status') != 'inProgress':
        raise RuntimeError('No steerable Codex turn')
    return turns[0]['id']


def steer(connection, session, prompt):
    turn = active_turn(connection, session)
    result = connection.request('turn/steer', {
        'threadId': session, 'expectedTurnId': turn,
        'input': [{'type': 'text', 'text': prompt}],
    })
    if result.get('turnId') != turn:
        raise RuntimeError('Codex did not acknowledge the expected turn')


if __name__ == '__main__':
    connection = None
    try:
        connection = CodexConnection(timeout=3 if sys.argv[1] == '--can-steer' else 10)
        if sys.argv[1] == '--can-steer':
            active_turn(connection, sys.argv[2])
        else:
            steer(connection, sys.argv[1], sys.stdin.read())
            print('Sent review feedback to the active Codex turn.')
    except (OSError, ValueError, RuntimeError, IndexError, KeyError) as error:
        print(f'Codex steering unavailable: {error}', file=sys.stderr)
        sys.exit(1)
    finally:
        if connection:
            connection.close()
