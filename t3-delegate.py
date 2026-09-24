#!/usr/bin/env python3
"""Send a Codex follow-up through the running T3 Code server."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid
from datetime import datetime, timezone


def t3_home():
    return Path(os.environ.get('T3CODE_HOME', Path.home() / '.t3')) / 'userdata'


def t3_cli():
    configured = os.environ.get('T3CODE_CLI')
    if configured:
        return [configured] if os.access(configured, os.X_OK) else ['node', configured]
    installed = shutil.which('t3')
    if installed:
        return [installed]
    source_bundle = Path.home() / 'projects/t3code/apps/server/dist/bin.mjs'
    if source_bundle.is_file():
        return ['node', str(source_bundle)]
    raise RuntimeError('T3 Code CLI not found; set T3CODE_CLI to its CLI bundle')


def matching_thread(database, session_id):
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=5) as connection:
        rows = connection.execute('''
            SELECT t.thread_id, t.runtime_mode, t.interaction_mode,
                   t.model_selection_json, s.status
            FROM provider_session_runtime r
            JOIN projection_threads t ON t.thread_id = r.thread_id
            JOIN projection_thread_sessions s ON s.thread_id = t.thread_id
            WHERE r.provider_name = 'codex'
              AND json_extract(r.resume_cursor_json, '$.threadId') = ?
              AND t.deleted_at IS NULL AND t.archived_at IS NULL
        ''', (session_id,)).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f'Expected one active T3 thread for Codex session {session_id}; found {len(rows)}')
    thread_id, runtime_mode, interaction_mode, model_json, status = rows[0]
    if status not in ('ready', 'stopped'):
        raise RuntimeError(f'T3 thread {thread_id} is {status}; not sending another turn')
    return thread_id, runtime_mode, interaction_mode, json.loads(model_json)


def send_turn(session_id, prompt):
    home = t3_home()
    thread_id, runtime_mode, interaction_mode, model = matching_thread(
        home / 'state.sqlite', session_id
    )
    origin = json.loads((home / 'server-runtime.json').read_text())['origin']
    parsed = urlparse(origin)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise RuntimeError('T3 Code server must be reachable on a local loopback origin')
    cli = t3_cli()
    issue = subprocess.run(
        [*cli, 'auth', 'session', 'issue', '--base-dir', str(home.parent),
         '--json', '--ttl', '5m', '--label', 'CodeRabbit Review Queue'],
        capture_output=True, text=True, check=True, timeout=15,
    )
    credential = json.loads(issue.stdout)
    try:
        now = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        command = {
            'type': 'thread.turn.start',
            'commandId': str(uuid.uuid4()),
            'threadId': thread_id,
            'message': {
                'messageId': str(uuid.uuid4()), 'role': 'user',
                'text': prompt, 'attachments': [],
            },
            'modelSelection': model,
            'runtimeMode': runtime_mode,
            'interactionMode': interaction_mode,
            'createdAt': now,
        }
        request = Request(
            origin.rstrip('/') + '/api/orchestration/dispatch',
            data=json.dumps(command).encode(),
            headers={
                'Authorization': 'Bearer ' + credential['token'],
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        with urlopen(request, timeout=15) as response:
            response.read()
    finally:
        subprocess.run(
            [*cli, 'auth', 'session', 'revoke', '--base-dir', str(home.parent),
             credential['sessionId']],
            capture_output=True, text=True, timeout=15, check=False,
        )
    print(f'Sent review follow-up to T3 Code thread {thread_id}.')


if __name__ == '__main__':
    try:
        send_turn(sys.argv[1], sys.stdin.read())
    except (IndexError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'T3 delegation failed: {error}', file=sys.stderr)
        sys.exit(1)
