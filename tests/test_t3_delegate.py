import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'src/t3-delegate.py'
SPEC = importlib.util.spec_from_file_location('t3_delegate', SCRIPT)
t3_delegate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t3_delegate)


class T3DelegateTests(unittest.TestCase):
    def test_subagent_session_cannot_receive_review_followups(self):
        database = self.create_v2_database()
        with sqlite3.connect(database) as db:
            raw = db.execute('SELECT payload_json FROM orchestration_v2_projection_threads').fetchone()[0]
            payload = json.loads(raw)
            payload['lineage'] = {'parentThreadId': 'parent-thread',
                                  'relationshipToParent': 'subagent', 'rootThreadId': 'parent-thread'}
            db.execute('UPDATE orchestration_v2_projection_threads SET payload_json=?', (json.dumps(payload),))
        with patch.object(t3_delegate, 't3_home', return_value=self.home), \
             patch.object(t3_delegate, 't3_cli', return_value=['t3']) as cli, \
             patch.object(t3_delegate.subprocess, 'run', return_value=type('Result', (), {
                 'stdout': '{"token":"test","sessionId":"auth-session"}'})()), \
             patch.object(t3_delegate, 'dispatch_v2') as dispatch:
            with self.assertRaisesRegex(RuntimeError, 'subagent'):
                t3_delegate.send_turn('codex-session', 'Continue babysitting this PR.')
        cli.assert_not_called()
        dispatch.assert_not_called()

    def test_subagent_hierarchy_excludes_nested_tasks_but_keeps_user_forks(self):
        database = self.create_v2_database()
        with sqlite3.connect(database) as db:
            for ident, parent, relation, provider in (
                ('child', 'v2-thread', 'subagent', 'codex'),
                ('nested', 'child', 'fork', 'claudeAgent'),
                ('user-fork', 'v2-thread', 'fork', 'codex'),
            ):
                db.execute('INSERT INTO orchestration_v2_projection_threads (thread_id,payload_json) VALUES (?,?)',
                           (ident, json.dumps({'lineage': {'parentThreadId': parent,
                                                         'relationshipToParent': relation}})))
                db.execute('INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)',
                           (ident + '-provider', ident, provider, json.dumps({'driver': provider,
                               'nativeThreadRef': {'nativeId': ident + '-session'}})))
            self.assertEqual(t3_delegate.subagent_thread_ids(db), {'child', 'nested'})
        with patch.object(t3_delegate, 't3_home', return_value=self.home):
            self.assertEqual(t3_delegate.subagent_session_ids(), ['child-session'])
            self.assertEqual(t3_delegate.subagent_session_ids(provider='claudeAgent'), ['nested-session'])

    def test_migrated_match_ignores_subagents_sharing_the_parent_worktree(self):
        database, codex_home = self.create_migrated_thread()
        with sqlite3.connect(database) as db:
            payload = json.loads(db.execute('SELECT payload_json FROM orchestration_v2_projection_threads').fetchone()[0])
            payload['lineage'] = {'parentThreadId': 'v2-thread', 'relationshipToParent': 'subagent'}
            db.execute('INSERT INTO orchestration_v2_projection_threads '
                       '(thread_id,runtime_mode,interaction_mode,payload_json,default_provider) '
                       'VALUES (?,?,?,?,?)', ('child', 'full-access', 'default', json.dumps(payload), 'codex'))
        with patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}):
            self.assertEqual(t3_delegate.matching_thread(database, 'old-session')[0], 'v2-thread')

    def test_native_codex_matching_excludes_t3_subagents_with_no_native_flag(self):
        database, codex_home = self.create_migrated_thread()
        worktree = str(Path(self.temp.name) / 'worktree')
        payload = {'worktreePath': worktree, 'branch': 'pr-branch',
                   'lineage': {'parentThreadId': 'v2-thread', 'relationshipToParent': 'subagent',
                               'rootThreadId': 'v2-thread'}}
        with sqlite3.connect(database) as db:
            db.execute('INSERT INTO orchestration_v2_projection_threads '
                       '(thread_id,title,runtime_mode,interaction_mode,active_provider_thread_id,payload_json) '
                       'VALUES (?,?,?,?,?,?)', ('child', 'Reviewer', 'full-access', 'default',
                                              'child-provider', json.dumps(payload)))
            db.execute('INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)',
                       ('child-provider', 'child', 'codex', json.dumps({'driver': 'codex',
                           'nativeThreadRef': {'nativeId': 'child-session'}})))
        with sqlite3.connect(codex_home / 'state_5.sqlite') as db:
            db.execute('DELETE FROM threads')
            for column in ('git_branch TEXT', 'git_sha TEXT', 'rollout_path TEXT',
                           'thread_source TEXT', 'git_origin_url TEXT', 'updated_at INTEGER'):
                db.execute('ALTER TABLE threads ADD COLUMN ' + column)
            db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)',
                       ('child-session', worktree, 'T3 Code', 'pr-branch', 'head', None,
                        None, 'git@github.com:example/repo.git', 1))
        sessions = codex_home / 'sessions'
        sessions.mkdir()
        (sessions / 'child.jsonl').write_text(json.dumps({'timestamp': '2026-10-29T06:00:00Z',
            'type': 'session_meta', 'payload': {'id': 'child-session', 'originator': 'T3 Code',
                'cwd': worktree, 'git': {'repository_url': 'git@github.com:example/repo.git',
                                       'branch': 'pr-branch', 'commit_hash': 'head'}}}) + '\n')
        for indexed in (True, False):
            if not indexed:
                (codex_home / 'state_5.sqlite').rename(codex_home / 'index-disabled.sqlite')
            result = subprocess.run(['bash', '-c',
                'source "$1"\nconfigure_repo example/repo\nmatching_codex_session pr-branch head',
                'test', str(SCRIPT.parent / 'coderabbit-review-queue')], capture_output=True, text=True,
                env={**os.environ, 'CODEX_HOME': str(codex_home), 'T3CODE_HOME': str(self.home.parent),
                     'XDG_STATE_HOME': str(Path(self.temp.name) / 'state')}, timeout=5)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(result.stdout, '')

    def test_claude_matching_and_cli_fallback_reject_t3_subagent_sessions(self):
        database, _codex_home = self.create_migrated_thread()
        worktree = str(Path(self.temp.name) / 'worktree')
        with sqlite3.connect(database) as db:
            payload = json.loads(db.execute('SELECT payload_json FROM orchestration_v2_projection_threads').fetchone()[0])
            payload['lineage'] = {'parentThreadId': 'parent', 'relationshipToParent': 'subagent'}
            db.execute('UPDATE orchestration_v2_projection_threads SET active_provider_thread_id=?,payload_json=?',
                       ('child-provider', json.dumps(payload)))
            db.execute('INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)',
                       ('child-provider', 'v2-thread', 'claudeAgent', json.dumps({'driver': 'claudeAgent',
                           'nativeThreadRef': {'nativeId': 'child-session'}})))
        projects = Path(self.temp.name) / 'claude-projects'
        (projects / 'project').mkdir(parents=True)
        (projects / 'project/child-session.jsonl').write_text(json.dumps({
            'cwd': worktree, 'sessionId': 'child-session', 'gitBranch': 'pr-branch'}) + '\n')
        environment = {**os.environ, 'T3CODE_HOME': str(self.home.parent),
                       'XDG_STATE_HOME': str(Path(self.temp.name) / 'state')}
        for action in ('matching_claude_session pr-branch head',
                       'ids=(); resume_claude_session 42 Title child-session /nonexistent "Continue babysitting" ids head',
                       'ids=(); resume_codex_session 42 Title head child-session /nonexistent "Continue babysitting" ids'):
            if 'codex_session' in action:
                with sqlite3.connect(database) as db:
                    db.execute("UPDATE orchestration_v2_projection_provider_threads SET provider='codex',payload_json=?",
                               (json.dumps({'driver': 'codex', 'nativeThreadRef': {'nativeId': 'child-session'}}),))
            result = subprocess.run(['bash', '-c',
                'source "$1"\nconfigure_repo example/repo\nclaude_projects_root="$2"\n' + action,
                'test', str(SCRIPT.parent / 'coderabbit-review-queue'), str(projects)],
                capture_output=True, text=True, env=environment, timeout=5)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertNotIn('Delegation confirmed', result.stdout)


    def test_claude_t3_dispatch_preserves_thread_settings_and_supports_steering(self):
        database = self.create_v2_database()
        with sqlite3.connect(database) as connection:
            connection.execute('UPDATE orchestration_v2_projection_provider_threads SET provider=?,payload_json=?',
                               ('claude_subscription', json.dumps({'driver': 'claudeAgent',
                                   'nativeThreadRef': {'nativeId': 'claude-session'}})))
            connection.execute("INSERT INTO orchestration_v2_projection_runs (thread_id,status) VALUES ('v2-thread','running')")
        credential = type('Result', (), {'stdout': json.dumps({
            'token': 'secret', 'sessionId': 'auth-session'})})()
        with patch.object(t3_delegate, 't3_home', return_value=self.home), \
             patch.object(t3_delegate, 't3_cli', return_value=['t3']), \
             patch.object(t3_delegate.subprocess, 'run', return_value=credential), \
             patch.object(t3_delegate, 'dispatch_v2') as dispatch:
            self.assertEqual(t3_delegate.thread_title('claude-session', provider='claudeAgent'), 'Review task')
            self.assertEqual(t3_delegate.thread_state('claude-session', provider='claudeAgent'), 'running')
            with self.assertRaises(RuntimeError):
                t3_delegate.matching_thread(database, 'claude-session')
            t3_delegate.send_turn('claude-session', 'Resolve review', provider='claudeAgent')
            command = dispatch.call_args.args[2]
            self.assertEqual(command['deliveryIntent'], 'auto')
            self.assertEqual(command['threadId'], 'v2-thread')
            for setting in ('modelSelection', 'runtimeMode', 'interactionMode'):
                self.assertNotIn(setting, command)
            dispatch.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'not idle'):
                t3_delegate.send_turn('claude-session', 'Resolve review', provider='claudeAgent', steer=False)
            dispatch.assert_not_called()
            with sqlite3.connect(database) as connection:
                connection.execute('DELETE FROM orchestration_v2_projection_runs')
            t3_delegate.send_turn('claude-session', 'Resolve review', provider='claudeAgent', steer=False)
            dispatch.assert_called_once()
            self.assertNotIn('deliveryIntent', dispatch.call_args.args[2])

    def test_held_queued_runs_are_idle_but_runnable_turns_remain_busy(self):
        database = self.create_v2_database()
        with patch.object(t3_delegate, 't3_home', return_value=self.home):
            for status, payload, expected in [
                ('queued', {'queueHeld': True}, 'idle'),
                ('queued', {'queueHeld': False}, 'running'),
                ('queued', {}, 'running'),
                ('preparing', {}, 'running'),
                ('starting', {}, 'running'),
                ('running', {'queueHeld': True}, 'running'),
                ('waiting', {}, 'idle'),
                ('completed', {}, 'idle'),
            ]:
                with self.subTest(status=status, payload=payload):
                    with sqlite3.connect(database) as connection:
                        connection.execute('DELETE FROM orchestration_v2_projection_runs')
                        connection.execute('INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?)',
                                           ('v2-thread', status, json.dumps(payload)))
                    self.assertEqual(t3_delegate.thread_state('codex-session'), expected)

    def test_idle_only_dispatch_ignores_held_queue_but_defers_for_actual_work(self):
        database = self.create_v2_database()
        with sqlite3.connect(database) as connection:
            connection.execute('INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?)',
                               ('v2-thread', 'queued', '{"queueHeld":true}'))
        credential = type('Result', (), {'stdout': json.dumps({
            'token': 'secret', 'sessionId': 'auth-session'})})()
        with patch.object(t3_delegate, 't3_home', return_value=self.home), \
             patch.object(t3_delegate, 't3_cli', return_value=['t3']), \
             patch.object(t3_delegate.subprocess, 'run', return_value=credential), \
             patch.object(t3_delegate, 'dispatch_v2') as dispatch:
            t3_delegate.send_turn('codex-session', 'Resolve review', steer=False)
            dispatch.assert_called_once()
            dispatch.reset_mock()
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO orchestration_v2_projection_runs (thread_id,status) VALUES ('v2-thread','running')")
            self.assertEqual(t3_delegate.thread_state('codex-session'), 'running')
            with self.assertRaisesRegex(RuntimeError, 'not idle'):
                t3_delegate.send_turn('codex-session', 'Resolve review', steer=False)
            dispatch.assert_not_called()

    def test_pr_watch_wait_is_idle_but_an_actual_turn_is_running(self):
        database = self.create_v2_database()
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO orchestration_v2_projection_runs (thread_id,status) VALUES ('v2-thread', 'waiting')")
            connection.execute('UPDATE orchestration_v2_projection_provider_threads SET payload_json=?',
                               (json.dumps({'nativeThreadRef': {'nativeId': 'codex-session'},
                                            'status': 'idle', 'pendingBackgroundTasks': [
                                                {'taskId': 'pull-request-watch:github.com/example/repo#42',
                                                 'kind': 'monitor'}]}),))
        with patch.object(t3_delegate, 't3_home', return_value=self.home):
            self.assertEqual(t3_delegate.thread_state('codex-session'), 'idle')
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO orchestration_v2_projection_runs (thread_id,status) VALUES ('v2-thread', 'running')")
            self.assertEqual(t3_delegate.thread_state('codex-session'), 'running')

    def test_idle_only_dispatch_can_resume_while_pr_watch_waits(self):
        database = self.create_v2_database()
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO orchestration_v2_projection_runs (thread_id,status) VALUES ('v2-thread', 'waiting')")
        credential = type('Result', (), {'stdout': json.dumps({
            'token': 'secret', 'sessionId': 'auth-session'})})()
        with patch.object(t3_delegate, 't3_home', return_value=self.home), \
             patch.object(t3_delegate, 't3_cli', return_value=['t3']), \
             patch.object(t3_delegate.subprocess, 'run', return_value=credential), \
             patch.object(t3_delegate, 'dispatch_v2') as dispatch:
            t3_delegate.send_turn('codex-session', 'Resolve review', steer=False)
        dispatch.assert_called_once()
        self.assertNotIn('deliveryIntent', dispatch.call_args.args[2])

    def create_migrated_thread(self):
        database = self.create_v2_database()
        worktree = Path(self.temp.name) / 'worktree'
        subprocess.run(['git', 'init', '-q', '--initial-branch=pr-branch', str(worktree)], check=True)
        subprocess.run(['git', '-C', str(worktree), 'remote', 'add', 'origin',
                        'git@github.com:Example/Repo.git'], check=True)
        codex_home = Path(self.temp.name) / 'codex'
        codex_home.mkdir()
        with sqlite3.connect(codex_home / 'state_5.sqlite') as connection:
            connection.execute('CREATE TABLE threads (id TEXT, cwd TEXT, originator TEXT)')
            connection.execute('INSERT INTO threads VALUES (?,?,?)', ('old-session', str(worktree), 'T3 Code'))
        payload = {'worktreePath': str(worktree), 'branch': 'pr-branch',
                   'branchPullRequest': {'repository': 'example/repo', 'number': 42},
                   'modelSelection': {'instanceId': 'codex', 'model': 'test-model'}}
        with sqlite3.connect(database) as connection:
            connection.execute('DELETE FROM orchestration_v2_projection_provider_threads')
            connection.execute('UPDATE orchestration_v2_projection_threads SET '
                               'active_provider_thread_id=NULL,default_provider=\'codex\',payload_json=?',
                               (json.dumps(payload),))
        return database, codex_home

    def test_migrated_pr_thread_matches_old_session_by_verified_worktree(self):
        database, codex_home = self.create_migrated_thread()
        with patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}):
            thread = t3_delegate.matching_thread(database, 'old-session')
        self.assertEqual(thread[0], 'v2-thread')

    def test_t3_owner_checks_the_live_repo_and_branch(self):
        database, _codex_home = self.create_migrated_thread()
        with sqlite3.connect(database) as connection:
            connection.execute('INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)',
                               ('claude-thread', 'v2-thread', 'claude_subscription',
                                json.dumps({'driver': 'claudeAgent', 'nativeThreadRef': {'nativeId': 'claude-session'}})))
            connection.execute('UPDATE orchestration_v2_projection_threads SET active_provider_thread_id=?',
                               ('claude-thread',))
        with patch.object(t3_delegate, 't3_home', return_value=self.home):
            self.assertEqual(t3_delegate.thread_owner('EXAMPLE/REPO', 'pr-branch'), 'claudeAgent')
            self.assertEqual(t3_delegate.thread_owner('other/repo', 'pr-branch'), '')
            self.assertEqual(t3_delegate.thread_owner('example/repo', 'other-branch'), '')
            subprocess.run(['git', '-C', str(Path(self.temp.name) / 'worktree'), 'symbolic-ref',
                            'HEAD', 'refs/heads/changed-branch'], check=True)
            self.assertEqual(t3_delegate.thread_owner('example/repo', 'pr-branch'), '')

    def test_migrated_dispatch_reaches_app_thread_and_inherits_settings(self):
        database, codex_home = self.create_migrated_thread()
        real_run = subprocess.run
        credential = type('Result', (), {'stdout': json.dumps({
            'token': 'secret', 'sessionId': 'auth-session'})})()
        def run(args, **kwargs):
            return real_run(args, **kwargs) if args[0] == 'git' else credential
        with patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}), \
             patch.object(t3_delegate, 't3_home', return_value=self.home), \
             patch.object(t3_delegate, 't3_cli', return_value=['t3']), \
             patch.object(t3_delegate.subprocess, 'run', side_effect=run), \
             patch.object(t3_delegate, 'dispatch_v2') as dispatch:
            t3_delegate.send_turn('old-session', 'Resolve conflicts; rerun merge guard')
            self.assertEqual(dispatch.call_args.args[2]['threadId'], 'v2-thread')
            self.assertNotIn('modelSelection', dispatch.call_args.args[2])
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO orchestration_v2_projection_runs (thread_id,status) VALUES ('v2-thread','running')")
            self.assertEqual(t3_delegate.thread_state('old-session'), 'running')
            dispatch.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'not idle'):
                t3_delegate.send_turn('old-session', 'Duplicate repair', steer=False)
            dispatch.assert_not_called()

    def test_native_matching_accepts_named_codex_provider_instance(self):
        database = self.create_v2_database()
        with sqlite3.connect(database) as db:
            db.execute('UPDATE orchestration_v2_projection_provider_threads '
                       'SET provider=\'codex_second\',payload_json=?',
                       (json.dumps({'driver': 'codex', 'nativeThreadRef': {'nativeId': 'codex-session'}}),))
        self.assertEqual(t3_delegate.matching_thread(database, 'codex-session')[0], 'v2-thread')

    def test_migrated_match_rejects_wrong_repo_branch_archive_and_duplicates(self):
        database, codex_home = self.create_migrated_thread()
        with patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}), sqlite3.connect(database) as db:
            original = db.execute('SELECT payload_json FROM orchestration_v2_projection_threads').fetchone()[0]
            for key, value in [('branch', 'other'), ('branchPullRequest', {'repository': 'other/repo'})]:
                payload = json.loads(original)
                payload[key] = value
                db.execute('UPDATE orchestration_v2_projection_threads SET payload_json=?', (json.dumps(payload),))
                db.commit()
                with self.assertRaises(RuntimeError):
                    t3_delegate.matching_thread(database, 'old-session')
            db.execute('UPDATE orchestration_v2_projection_threads SET payload_json=?,archived_at=\'today\'', (original,))
            db.commit()
            with self.assertRaises(RuntimeError):
                t3_delegate.matching_thread(database, 'old-session')
            db.execute('UPDATE orchestration_v2_projection_threads SET archived_at=NULL')
            db.execute('INSERT INTO orchestration_v2_projection_threads SELECT * FROM orchestration_v2_projection_threads')
            db.commit()
            with self.assertRaises(RuntimeError):
                t3_delegate.matching_thread(database, 'old-session')

    def test_cli_session_cannot_fall_back_to_unrelated_t3_app_thread(self):
        database, codex_home = self.create_migrated_thread()
        with sqlite3.connect(codex_home / 'state_5.sqlite') as db:
            db.execute("UPDATE threads SET originator='codex-tui'")
        with patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}):
            with self.assertRaises(RuntimeError):
                t3_delegate.matching_thread(database, 'old-session')

    def create_v2_database(self):
        database = self.home / 'statev2.sqlite'
        with sqlite3.connect(database) as connection:
            connection.executescript('''
                CREATE TABLE orchestration_v2_projection_threads
                  (thread_id TEXT, title TEXT, runtime_mode TEXT, interaction_mode TEXT,
                   active_provider_thread_id TEXT, payload_json TEXT, deleted_at TEXT, archived_at TEXT);
                CREATE TABLE orchestration_v2_projection_provider_threads
                  (provider_thread_id TEXT, thread_id TEXT, provider TEXT, payload_json TEXT);
                CREATE TABLE orchestration_v2_projection_runs
                  (thread_id TEXT, status TEXT, payload_json TEXT NOT NULL DEFAULT '{}');
                INSERT INTO orchestration_v2_projection_threads VALUES
                  ('v2-thread', 'Review task', 'full-access', 'default', 'provider-thread',
                   '{"modelSelection":{"instanceId":"codex_second","model":"gpt-6-sol","options":[{"id":"reasoningEffort","value":"high"}]}}', NULL, NULL);
                INSERT INTO orchestration_v2_projection_provider_threads VALUES
                  ('provider-thread', 'v2-thread', 'codex',
                   '{"nativeThreadRef":{"nativeId":"codex-session"},"status":"running"}');
            ''')
            connection.execute('ALTER TABLE orchestration_v2_projection_threads ADD COLUMN default_provider TEXT')
        return database

    def test_v2_thread_matches_native_session_including_running_tasks(self):
        result = t3_delegate.matching_thread(self.create_v2_database(), 'codex-session')
        self.assertEqual(result[0], 'v2-thread')
        self.assertEqual(result[3]['instanceId'], 'codex_second')

    def test_v2_dispatch_uses_server_auto_delivery_and_retains_thread_settings(self):
        self.create_v2_database()
        credential = type('Result', (), {'stdout': json.dumps({
            'token': 'secret', 'sessionId': 'auth-session'})})()
        with patch.object(t3_delegate, 't3_home', return_value=self.home), \
             patch.object(t3_delegate, 't3_cli', return_value=['t3']), \
             patch.object(t3_delegate.subprocess, 'run', return_value=credential) as run, \
             patch.object(t3_delegate, 'dispatch_v2') as dispatch:
            t3_delegate.send_turn('codex-session', 'Resolve review')
        command = dispatch.call_args.args[2]
        self.assertEqual(command['type'], 'message.dispatch')
        self.assertEqual(command['deliveryIntent'], 'auto')
        self.assertEqual(command['threadId'], 'v2-thread')
        self.assertEqual(command['text'], 'Resolve review')
        # Omit overrides: v2 inherits model/options/permissions from the thread.
        self.assertNotIn('modelSelection', command)
        self.assertNotIn('runtimeMode', command)
        issue_args = run.call_args_list[0].args[0]
        self.assertEqual([issue_args[index + 1] for index, arg in enumerate(issue_args)
                          if arg == '--scope'], ['orchestration:read', 'orchestration:operate'])
        self.assertIn('revoke', run.call_args.args[0])

    def test_idle_only_dispatch_defers_if_task_became_busy_and_revokes_auth(self):
        database = self.create_v2_database()
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO orchestration_v2_projection_runs (thread_id,status) VALUES ('v2-thread', 'running')")
        credential = type('Result', (), {'stdout': json.dumps({
            'token': 'secret', 'sessionId': 'auth-session'})})()
        with patch.object(t3_delegate, 't3_home', return_value=self.home), \
             patch.object(t3_delegate, 't3_cli', return_value=['t3']), \
             patch.object(t3_delegate.subprocess, 'run', return_value=credential) as run, \
             patch.object(t3_delegate, 'dispatch_v2') as dispatch:
            with self.assertRaisesRegex(RuntimeError, 'not idle'):
                t3_delegate.send_turn('codex-session', 'Resolve review', steer=False)
        dispatch.assert_not_called()
        self.assertIn('revoke', run.call_args.args[0])

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
