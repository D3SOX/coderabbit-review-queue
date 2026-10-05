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


def t3_database():
    home = t3_home()
    return home / ('statev2.sqlite' if (home / 'statev2.sqlite').is_file() else 'state.sqlite')


def thread_title(session_id):
    database = t3_database()
    if not database.is_file():
        return ''
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=1) as connection:
        if database.name == 'statev2.sqlite':
            rows = connection.execute('''
                SELECT t.title FROM orchestration_v2_projection_provider_threads p
                JOIN orchestration_v2_projection_threads t ON t.thread_id = p.thread_id
                WHERE p.provider = 'codex'
                  AND json_extract(p.payload_json, '$.nativeThreadRef.nativeId') = ?
                  AND t.active_provider_thread_id = p.provider_thread_id
                  AND t.deleted_at IS NULL
            ''', (session_id,)).fetchall()
            return ' '.join((rows[0][0] or '').split()) if len(rows) == 1 else ''
        rows = connection.execute('''
            SELECT t.title
            FROM provider_session_runtime r
            JOIN projection_threads t ON t.thread_id = r.thread_id
            WHERE r.provider_name = 'codex'
              AND json_extract(r.resume_cursor_json, '$.threadId') = ?
              AND t.deleted_at IS NULL
        ''', (session_id,)).fetchall()
    if len(rows) != 1:
        return ''
    return ' '.join((rows[0][0] or '').split())


def t3_cli():
    configured = os.environ.get('T3CODE_CLI')
    if configured:
        return [configured] if os.access(configured, os.X_OK) else ['node', configured]
    # Use the CLI shipped with the live desktop server, not a possibly stale
    # source checkout. Electron can execute its bundled CLI in Node mode.
    for process in Path('/proc').glob('[0-9]*'):
        try:
            args = (process / 'cmdline').read_bytes().decode().split('\0')
            bundle = next((arg for arg in args if arg.startswith('/')
                           and '/app.asar/apps/server/dist/bin.mjs' in arg), None)
            if bundle:
                return ['env', 'ELECTRON_RUN_AS_NODE=1',
                        os.readlink(process / 'exe').removesuffix(' (deleted)'), bundle]
        except (OSError, UnicodeError):
            continue
    installed = shutil.which('t3')
    if installed:
        return [installed]
    source_bundle = Path.home() / 'projects/t3code/apps/server/dist/bin.mjs'
    if source_bundle.is_file():
        return ['node', str(source_bundle)]
    raise RuntimeError('T3 Code CLI not found; set T3CODE_CLI to its CLI bundle')


def matching_thread(database, session_id):
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=5) as connection:
        if database.name == 'statev2.sqlite':
            rows = connection.execute('''
                SELECT t.thread_id, t.runtime_mode, t.interaction_mode,
                       json_extract(t.payload_json, '$.modelSelection')
                FROM orchestration_v2_projection_provider_threads p
                JOIN orchestration_v2_projection_threads t ON t.thread_id = p.thread_id
                WHERE p.provider = 'codex'
                  AND json_extract(p.payload_json, '$.nativeThreadRef.nativeId') = ?
                  AND t.active_provider_thread_id = p.provider_thread_id
                  AND t.deleted_at IS NULL AND t.archived_at IS NULL
            ''', (session_id,)).fetchall()
            if len(rows) != 1:
                raise RuntimeError(f'Expected one active T3 v2 thread; found {len(rows)}')
            thread_id, runtime_mode, interaction_mode, model_json = rows[0]
            return thread_id, runtime_mode, interaction_mode, json.loads(model_json) if model_json else None
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


def dispatch_v2(origin, token, command, method='orchestration.dispatchCommand'):
    request = Request(origin.rstrip('/') + '/api/auth/websocket-ticket',
                      data=b'', headers={'Authorization': 'Bearer ' + token}, method='POST')
    with urlopen(request, timeout=15) as response:
        ticket = json.loads(response.read())['ticket']
    # Node's built-in WebSocket avoids an additional Python dependency. Secrets
    # travel over stdin, never command-line arguments or error output.
    program = r'''
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const url = new URL('/ws', input.origin);
url.protocol = 'ws:';
url.searchParams.set('wsTicket', input.ticket);
url.searchParams.set('orchestrationProtocol', '2');
const socket = new WebSocket(url);
const timer = setTimeout(() => { console.error('T3 RPC timed out'); process.exit(1); }, 15000);
socket.onopen = () => socket.send(JSON.stringify({
  _tag: 'Request', id: '1', tag: input.method,
  payload: input.command, headers: []
}));
socket.onmessage = event => {
  for (const message of [].concat(JSON.parse(event.data))) {
    if (message._tag === 'Exit' && message.requestId === '1') {
      clearTimeout(timer);
      socket.close();
      if (message.exit._tag !== 'Success') {
        console.error('T3 rejected message dispatch'); process.exitCode = 1;
      }
    }
  }
};
socket.onerror = () => { console.error('T3 WebSocket failed'); process.exit(1); };
'''
    result = subprocess.run(['node', '-e', program], input=json.dumps({
        'origin': origin, 'ticket': ticket, 'command': command, 'method': method,
    }), capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'T3 v2 message dispatch failed')


def send_turn(session_id, prompt):
    home = t3_home()
    database = t3_database()
    thread_id, runtime_mode, interaction_mode, model = matching_thread(
        database, session_id
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
        if database.name == 'statev2.sqlite':
            dispatch_v2(origin, credential['token'], {
                'type': 'message.dispatch', 'commandId': str(uuid.uuid4()),
                'threadId': thread_id, 'messageId': str(uuid.uuid4()),
                'text': prompt, 'attachments': [],
                'createdBy': 'user', 'creationSource': 'server',
                'deliveryIntent': 'auto', 'dispatchMode': {'type': 'start_immediately'},
            })
            print(f'Sent review follow-up to T3 Code v2 thread {thread_id}.')
            return
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
        if sys.argv[1] == '--title':
            print(thread_title(sys.argv[2]))
        elif sys.argv[1] == '--can-steer':
            database = t3_database()
            if database.name != 'statev2.sqlite':
                sys.exit(1)
            matching_thread(database, sys.argv[2])
        else:
            send_turn(sys.argv[1], sys.stdin.read())
    except (IndexError, OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f'T3 delegation failed: {error}', file=sys.stderr)
        sys.exit(1)
