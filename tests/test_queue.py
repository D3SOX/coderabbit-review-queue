import json
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'coderabbit-review-queue'


class QueueTests(unittest.TestCase):
    def run_shell(self, body, data=None):
        with tempfile.TemporaryDirectory() as state:
            return subprocess.run(
                ['bash', '-c', 'source "$1"\nconfigure_repo example/repo\n' + body,
                 'test', str(SCRIPT)],
                input=json.dumps(data) if data is not None else '',
                text=True, capture_output=True, timeout=5,
                env={**os.environ, 'XDG_STATE_HOME': state},
            )

    def test_file_limit_ends_completion_wait(self):
        result = self.run_shell('''
wait_for_github_quota() { :; }
desktop_notify() { :; }
gh() {
  printf '%s\\n' '{"statuses":[{"context":"CodeRabbit","created_at":"2026-09-06T21:05:19Z","state":"success","description":"Review skipped: 112 files exceed the limit of 100"}]}'
}
sleep() { echo 'Still waiting after file-limit rejection' >&2; exit 99; }
if wait_for_review_completion 1162 2026-09-06T21:05:08Z head title; then
  exit 0
else
  exit $?
fi
''')
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn('112 files exceed the limit of 100', result.stdout)

    @staticmethod
    def pr(number, author='human', description='', head='head'):
        return {
            'number': number, 'author': {'login': author}, 'title': '[pull] example',
            'isDraft': False, 'headRefName': f'branch-{number}', 'headRefOid': head,
            'updatedAt': '2026-09-06T21:05:19Z',
            'comments': {'nodes': []}, 'reviews': {'nodes': []},
            'commits': {'nodes': [{'commit': {'statusCheckRollup': {'contexts': {
                'nodes': [{'context': 'CodeRabbit', 'creator': {'login': 'coderabbitai'},
                           'state': 'SUCCESS', 'description': description}]
            }}}}]},
        }

    def rows(self, function, prs, setup=''):
        result = self.run_shell(setup + '\n' + function,
                                {'data': {'repository': {'pullRequests': {'nodes': prs}}}})
        self.assertEqual(result.returncode, 0, result.stderr)
        return [int(line.split('\t')[0]) for line in result.stdout.splitlines()]

    def test_default_authors_excluded_from_queue_and_delegation(self):
        prs = [self.pr(1, 'pull'), self.pr(2, 'dependabot'), self.pr(3)]
        for function in ('stale_rows', 'eligible_rows'):
            with self.subTest(function=function):
                self.assertEqual(self.rows(function, prs), [3])

    def test_clearing_authors_includes_bots_even_with_pull_title(self):
        self.assertEqual(self.rows('stale_rows', [self.pr(1, 'pull')],
                                   ': >"$excluded_authors_file"'), [1])

    def test_custom_authors_and_bot_aliases(self):
        setup = "printf '%s\\n' ' @APP/Dependabot[bot] ' '# comment' 'HUMAN' >\"$excluded_authors_file\""
        self.assertEqual(self.rows('eligible_rows', [self.pr(1, 'pull'),
                         self.pr(2, 'dependabot'), self.pr(3, 'Human')], setup), [1])

    def test_branch_and_draft_filters_still_apply(self):
        draft = self.pr(2)
        draft['isDraft'] = True
        self.assertEqual(self.rows('eligible_rows', [self.pr(1), draft, self.pr(3)],
                         'printf "branch-1\\n" >"$excluded_branches_file"'), [3])

    def test_file_limit_status_excludes_only_rejected_head(self):
        rejected = self.pr(1, description='Review skipped: 112 files exceed the limit of 100')
        self.assertEqual(self.rows('stale_rows', [rejected, self.pr(2)]), [2])
        self.assertEqual(self.rows('stale_rows', [self.pr(1, head='new-head')]), [1])
        # A manual review may still be possible when automatic reviews are disabled.
        self.assertEqual(self.rows('stale_rows', [self.pr(1, description='Review skipped: Auto reviews disabled')]), [1])

    def test_excluded_authors_and_skipped_heads_not_active_from_request_history(self):
        prs = [self.pr(1, 'dependabot'), self.pr(2),
               self.pr(3, description='Review skipped: 112 files exceed the limit of 100')]
        setup = 'for pr in 1 2 3; do printf "%s\\thead\\t%s\\n" "$pr" "$(date +%s)"; done >"$review_requests_file"'
        self.assertEqual(self.rows('state=$(cat); active_review_rows "$state"', prs, setup), [2])

    def test_excluded_authors_not_active_from_pending_status(self):
        prs = [self.pr(1, 'dependabot'), self.pr(2)]
        for pr in prs:
            pr['commits']['nodes'][0]['commit']['statusCheckRollup']['contexts']['nodes'][0]['state'] = 'PENDING'
        self.assertEqual(self.rows('state=$(cat); active_review_rows "$state"', prs), [2])

    def test_cli_setting_persists_and_empty_overrides_defaults(self):
        result = self.run_shell('''
main --repo example/repo --set-excluded-authors 'Pull, @Dependabot[bot],app/pull'
excluded_authors_json
main --repo example/repo --set-excluded-authors ''
excluded_authors_json
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"dependabot"', result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith('[]'), result.stdout)

    def test_old_file_limit_status_does_not_end_new_request(self):
        result = self.run_shell('''
wait_for_github_quota() { :; }
gh() { printf '%s\\n' '{"statuses":[{"context":"CodeRabbit","created_at":"2026-09-05T21:05:19Z","description":"Review skipped: 112 files exceed the limit of 100"}]}'; }
sleep() { exit 99; }
wait_for_review_completion 1162 2026-09-06T21:05:08Z head title
''')
        self.assertEqual(result.returncode, 99, result.stdout + result.stderr)

    def test_recording_review_request_moves_pr_to_bottom(self):
        result = self.run_shell(r'''
printf '%s\n' 4 2 9 >"$queue_order_file"
record_review_request 2 head-2 123
cat "$queue_order_file"
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['4', '9', '2'])

    def test_monitor_order_update_does_not_overwrite_concurrent_gui_order(self):
        with tempfile.TemporaryDirectory() as state:
            state_root = Path(state) / 'coderabbit-review-queue'
            state_root.mkdir()
            order = state_root / 'example__repo-order.txt'
            lock_path = order.with_suffix(order.suffix + '.lock')
            order.write_text('1\n2\n3\n')
            with lock_path.open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                process = subprocess.Popen(
                    ['bash', '-c', (
                        'source "$1"\nconfigure_repo example/repo\n'
                        'record_review_request 2 head-2 123'
                    ), 'test', str(SCRIPT)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, 'XDG_STATE_HOME': state},
                )
                order.write_text('3\n2\n1\n')
                fcntl.flock(lock, fcntl.LOCK_UN)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(order.read_text().splitlines(), ['3', '1', '2'])

    def test_monitor_continues_after_skipped_review(self):
        result = self.run_shell(r'''
claim_monitor() { :; }
cleanup_monitor() { :; }
claim_dispatch() { :; }
release_dispatch() { echo released; }
monitor_snapshot() { printf '{}'; }
route_all_unresolved() { :; }
route_unresolved_review() { :; }
load_stale_rows() {
  if [[ -f $state_root/skipped ]]; then
    stale=($'2\tnow\tbranch-2\thead-2\tsecond\t-')
  else
    stale=($'1\tnow\tbranch-1\thead-1\tfirst\t-')
  fi
}
latest_expiry() { echo 0; }
shared_expiry() { echo 0; }
remember_shared_expiry() { :; }
wait_until() { :; }
query_quota() { quota_remaining=1; }
recent_review_request_expiry() { echo 0; }
desktop_notify() { :; }
gh() { :; }
wait_for_acceptance() { :; }
wait_for_review_completion() {
  if [[ $1 == 1 ]]; then
    touch "$state_root/skipped"
    return 3
  fi
  awk -F '\t' '$1 == 1 { ts=$3 } END { exit(ts != 0) }' "$review_requests_file" || exit 98
  echo 'Reached second PR after clearing skipped request'
  exit 0
}
main --repo example/repo
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('released', result.stdout)
        self.assertIn('Reached second PR after clearing skipped request', result.stdout)

    def test_approved_review_is_merged_immediately_after_completion(self):
        result = self.run_shell(r'''
claim_monitor() { :; }
cleanup_monitor() { :; }
claim_dispatch() { :; }
release_dispatch() { :; }
monitor_snapshot() {
  if [[ -f $state_root/completed ]]; then
    printf 'approved snapshot\n'
  else
    printf 'initial snapshot\n'
  fi
}
route_all_unresolved() {
  if [[ -f $state_root/completed && ! -f $state_root/merged ]]; then
    echo 'Processed unresolved reviews before approved merge' >&2
    exit 98
  fi
}
load_stale_rows() {
  if [[ -f $state_root/completed ]]; then
    stale=()
  else
    stale=($'42\tnow\tbranch-42\thead-42\tapproved change\t-')
  fi
}
merge_approved_reviews() {
  if [[ $1 == 'approved snapshot' ]]; then
    touch "$state_root/merged"
    echo 'Merged approved review immediately'
    exit 0
  fi
}
latest_expiry() { echo 0; }
shared_expiry() { echo 0; }
remember_shared_expiry() { :; }
wait_until() { :; }
query_quota() { quota_remaining=1; }
recent_review_request_expiry() { echo 0; }
desktop_notify() { :; }
gh() { :; }
wait_for_acceptance() { :; }
wait_for_review_completion() { touch "$state_root/completed"; }
route_unresolved_review() { :; }
main --repo example/repo
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Merged approved review immediately', result.stdout)

    def test_snapshot_cache_is_shared(self):
        result = self.run_shell(r'''
calls="$state_root/calls"
snapshot_remote() { echo called >>"$calls"; printf '{"value":1}\n'; }
snapshot 30
snapshot 30
wc -l <"$calls"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], '1')

    def test_monitor_state_records_reviewing_pr(self):
        result = self.run_shell(r'''
write_monitor_state reviewing 42 'fix the queue' 0
cat "$monitor_state_file"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'reviewing\t42\tfix the queue\t0\n')

    def test_remote_agent_progress_uses_ssh(self):
        result = self.run_shell(r'''
printf '%s\n' desktop >"$agent_host_file"
ssh() { printf '%s\n' "$*"; printf 'Codex Idle (12345678)\n'; }
agent_task_progress feature abc123
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('-o BatchMode=yes', result.stdout)
        self.assertIn('--task-progress', result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith('Codex Idle (12345678)'))

    def test_validate_repo_rejects_missing_repository(self):
        result = self.run_shell(r'''
gh() { return 1; }
main --repo example/missing --validate-repo
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not be found', result.stderr)

    def test_validate_repo_accepts_exact_repository(self):
        result = self.run_shell(r'''
gh() { printf 'example/repo\n'; }
main --repo example/repo --validate-repo
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_monitor_restart_waits_for_saved_quota_before_refreshing(self):
        result = self.run_shell(r'''
claim_monitor() { :; }
cleanup_monitor() { :; }
monitor_snapshot() { echo 'Refreshed before saved expiry' >&2; exit 99; }
wait_until() { echo "Waited for saved expiry $1"; exit 0; }
printf '%s\n' "$(( $(date +%s) + 600 ))" >"$quota_expiry_file"
main --repo example/repo
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Waited for saved expiry', result.stdout)
        self.assertNotIn('Refreshed before saved expiry', result.stderr)

    def test_monitor_restart_restores_saved_wait_without_notification(self):
        result = self.run_shell(r'''
claim_monitor() { :; }
cleanup_monitor() { :; }
wait_until() { printf 'mode=%s\n' "${2:-notify}"; exit 0; }
printf '%s\n' "$(( $(date +%s) + 600 ))" >"$quota_expiry_file"
main --repo example/repo
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('mode=quiet', result.stdout)

    def test_approved_review_rows_include_current_head(self):
        approved = self.pr(1)
        approved['reviews']['nodes'] = [{
            'author': {'login': 'coderabbitai'},
            'body': 'No issues found',
            'state': 'APPROVED',
            'commit': {'oid': 'head'},
            'submittedAt': '2026-09-10T10:00:00Z',
        }]
        old_head = self.pr(2)
        old_head['reviews']['nodes'] = [{
            'author': {'login': 'coderabbitai'},
            'body': 'No issues found',
            'state': 'APPROVED',
            'commit': {'oid': 'old'},
            'submittedAt': '2026-09-10T10:00:00Z',
        }]
        self.assertEqual(self.rows('state=$(cat); approved_review_rows "$state"',
                                   [approved, old_head]), [1])

    def test_reviewed_rows_include_only_substantive_current_head_reviews(self):
        current = self.pr(1)
        current['reviews']['nodes'] = [{
            'author': {'login': 'coderabbitai'}, 'body': 'Review findings',
            'state': 'COMMENTED', 'commit': {'oid': 'head'},
        }]
        old = self.pr(2)
        old['reviews']['nodes'] = [{
            'author': {'login': 'coderabbitai'}, 'body': 'Old findings',
            'state': 'COMMENTED', 'commit': {'oid': 'old'},
        }]
        empty = self.pr(3)
        empty['reviews']['nodes'] = [{
            'author': {'login': 'coderabbitai'}, 'body': '  ',
            'state': 'COMMENTED', 'commit': {'oid': 'head'},
        }]
        self.assertEqual(self.rows('reviewed_rows', [current, old, empty]), [1])

    def test_cached_status_reuses_one_minute_snapshot(self):
        result = self.run_shell(r'''
snapshot() { printf '%s\n' "$1" >"$state_root/snapshot-age"; printf '{}'; }
status_quota_available() { :; }
load_stale_rows() { stale=(); }
active_review_rows() { :; }
approved_review_rows() { :; }
reviewed_rows() { :; }
show_status 60
cat "$state_root/snapshot-age"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], '60')

    def test_custom_delegation_prompt_replaces_project_placeholders(self):
        result = self.run_shell(r'''
delegation_prompt_mode_override=custom
delegation_prompt_template_override='Fix {repo} PR #{pr_number}: {pr_title}\n{pr_url}\n{threads}'
render_delegation_prompt 42 'title & details' 'thread-1 (src/app.py:7)'
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Fix example/repo PR #42: title & details', result.stdout)
        self.assertIn('https://github.com/example/repo/pull/42', result.stdout)
        self.assertIn('thread-1 (src/app.py:7)', result.stdout)

    def test_quota_wait_replaces_checking_monitor_state(self):
        result = self.run_shell(r'''
claim_monitor() { :; }
cleanup_monitor() { :; }
monitor_snapshot() { printf '{}'; }
route_all_unresolved() { :; }
load_stale_rows() { stale=($'42\tnow\tbranch\thead\ttitle\t-'); }
latest_expiry() { echo 0; }
shared_expiry() { echo 0; }
remember_shared_expiry() { :; }
claim_dispatch() { :; }
release_dispatch() { :; }
query_quota() {
  quota_remaining=0
  quota_expiry=$(( $(date +%s) + 600 ))
}
wait_until() {
  cat "$monitor_state_file"
  exit 0
}
main --repo example/repo
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'waiting\t\t\t\d+')
        self.assertNotIn('checking\t42', result.stdout)

    def test_normal_quota_checks_do_not_send_desktop_notifications(self):
        for row in ('-1\tnow\t0\tminutes', '0\tnow\t10\tminutes'):
            with self.subTest(row=row):
                result = self.run_shell(rf'''
desktop_notify() {{ echo "unexpected notification: $1" >&2; return 99; }}
gh() {{
  if [[ $* == *"-X POST"* ]]; then
    printf '%s\n' '{{"created_at":"now"}}'
  else
    printf '[]\n'
  fi
}}
quota_row_from_comments() {{ printf '%b\n' '{row}'; }}
query_quota 42 title
''')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('unexpected notification', result.stderr)

    def test_merge_uses_selected_method_without_github_auto_merge(self):
        result = self.run_shell(r'''
gh() {
  if [[ $1 == pr && $2 == view ]]; then
    printf '%s\n' '{"state":"OPEN","headRefOid":"head","mergeable":"MERGEABLE","statusCheckRollup":[{"status":"COMPLETED","conclusion":"SUCCESS"}]}'
  else
    printf '%s\n' "$*"
  fi
}
merge_pr_now 42 head squash 1
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('pr merge 42 --repo example/repo --squash --delete-branch', result.stdout)
        self.assertNotIn('--auto', result.stdout)

    def test_merge_refuses_pending_ci(self):
        result = self.run_shell(r'''
gh() {
  printf '%s\n' '{"state":"OPEN","headRefOid":"head","mergeable":"MERGEABLE","statusCheckRollup":[{"status":"IN_PROGRESS","conclusion":""}]}'
}
merge_pr_now 42 head rebase 0
''')
        self.assertNotEqual(result.returncode, 0)

    def test_merge_runs_on_configured_agent_host(self):
        result = self.run_shell(r'''
printf '%s\n' desktop >"$agent_host_file"
printf '%s\n' rebase >"$merge_method_file"
printf '%s\n' 0 >"$delete_branch_file"
ssh() { printf '%s\n' "$*"; }
route_merge_pr 42 head
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('desktop', result.stdout)
        self.assertIn('--merge-now', result.stdout)
        self.assertIn('rebase', result.stdout)

    def test_post_delegation_merge_defaults_on(self):
        result = self.run_shell('merge_after_delegation_enabled')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
