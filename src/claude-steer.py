#!/usr/bin/env python3
"""Deliver feedback to an existing Claude Code session over its local inbox."""

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid


def claude_home():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR', os.environ.get('CLAUDE_HOME', Path.home() / '.claude')))


def live_session(session_id):
    matches = []
    for metadata in (claude_home() / 'sessions').glob('[0-9]*.json'):
        try:
            if metadata.stat().st_uid != os.getuid():
                continue
            entry = json.loads(metadata.read_text())
            if entry.get('sessionId') != session_id or entry.get('peerProtocol') != 1:
                continue
            pid = entry['pid']
            if not isinstance(pid, int) or pid <= 0 or metadata.stem != str(pid):
                continue
            os.kill(pid, 0)
            inbox = Path(entry['messagingSocketPath'])
            info = inbox.stat()
            if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                continue
            digest = hashlib.sha256(str(inbox).encode()).hexdigest()
            key = metadata.parent / f'{pid}.{digest}.key'
            info = key.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                continue
            credential = json.loads(key.read_text())
            token = credential.get('peerToken', '')
            if not re.fullmatch('[0-9a-f]{32}', token):
                continue
            if credential.get('procStart') != entry.get('procStart'):
                continue
            matches.append((entry, token))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if len(matches) != 1:
        raise RuntimeError('No unique live Claude session with an authenticated local inbox')
    return matches[0]


def send_feedback(session_id, prompt, *, wait_idle=False):
    entry, token = live_session(session_id)
    if wait_idle and entry.get('status') != 'idle':
        raise RuntimeError('Claude session is not idle; leaving feedback for retry')
    files = list((claude_home() / 'projects').glob(f'*/{session_id}.jsonl'))
    if len(files) != 1:
        raise RuntimeError('Cannot verify delivery without a unique Claude transcript')
    transcript = files[0]
    offset = transcript.stat().st_size
    message_id = str(uuid.uuid4())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(entry['messagingSocketPath'])
        pid, uid, _gid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if pid != entry['pid'] or uid != os.getuid():
            raise RuntimeError('Claude inbox owner changed; not sending feedback')
        frames = [
            {'type': 'auth', 'token': token},
            {'type': 'user', 'uuid': message_id, 'priority': 'now',
             'message': {'role': 'user', 'content': prompt}},
        ]
        connection.sendall(('\n'.join(json.dumps(frame) for frame in frames) + '\n').encode())
    deadline = time.monotonic() + 10
    pending = ''
    with transcript.open() as events:
        events.seek(offset)
        while time.monotonic() < deadline:
            pending += events.read()
            while '\n' in pending:
                line, pending = pending.split('\n', 1)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('uuid') == message_id:
                    return
            time.sleep(0.1)
    raise RuntimeError('Claude did not confirm feedback in its transcript; delivery is unconfirmed')


def session_owner(repo, branch):
    for metadata in (claude_home() / 'sessions').glob('[0-9]*.json'):
        try:
            entry, _token = live_session(json.loads(metadata.read_text())['sessionId'])
            cwd = entry.get('cwd')
            if not isinstance(cwd, str):
                continue
            origin = subprocess.run(['git', '-C', cwd, 'remote', 'get-url', 'origin'],
                                    capture_output=True, text=True, timeout=3)
            repository = re.search(r'github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$', origin.stdout.strip(), re.I)
            if origin.returncode or not repository or repository.group(1).lower() != repo.lower():
                continue
            current = subprocess.run(['git', '-C', cwd, 'branch', '--show-current'],
                                     capture_output=True, text=True, timeout=3)
            if current.returncode == 0 and current.stdout.strip() == branch:
                return 'claude'
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
            continue
    return ''


if __name__ == '__main__':
    try:
        if sys.argv[1] == '--owner':
            print(session_owner(sys.argv[2], sys.argv[3]))
        elif sys.argv[1] == '--can-steer':
            live_session(sys.argv[2])
        else:
            send_feedback(sys.argv[1], sys.stdin.read(), wait_idle='--wait-idle' in sys.argv[2:])
            print('Claude feedback delivery confirmed.')
    except (IndexError, OSError, ValueError, RuntimeError) as error:
        print(f'Claude delegation failed: {error}', file=sys.stderr)
        sys.exit(1)
