# SPDX-License-Identifier: GPL-3.0-or-later

matching_codex_session() {
  local branch_name=$1
  local head_sha=$2
  local repo_lower=${repo,,}
  local excluded_sessions
  excluded_sessions=$(python3 "$script_dir/t3-delegate.py" --subagent-sessions 2>/dev/null) || return 1

  # The rollout scan below can take longer than the remote status timeout once
  # many tasks exist. Use the recent task index first, checking each recorded
  # worktree's *current* branch/head (the stored values may predate a push).
  if [[ -f $codex_state_db ]]; then
    local indexed_match
    indexed_match=$(python3 - "$codex_state_db" "$repo_lower" "$branch_name" "$head_sha" "$excluded_sessions" <<'PY'
import sqlite3
import subprocess
import sys
import json

database, repo, branch, head, excluded = sys.argv[1:]
excluded = set(json.loads(excluded))
git_results = {}

def git(cwd, *args):
    key = (cwd, args)
    if key not in git_results:
        try:
            result = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, timeout=3)
        except subprocess.TimeoutExpired:
            # A hung worktree might be a second match; leave the decision to
            # the full scan instead of picking the other one as unique.
            sys.exit(0)
        git_results[key] = result.stdout.strip() if result.returncode == 0 else ''
    return git_results[key]

try:
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
        rows = connection.execute('''
            SELECT id, cwd, git_branch, git_sha, rollout_path FROM threads
            WHERE originator IN ('Codex Desktop', 't3code_desktop', 'T3 Code', 'codex-tui', 'codex_exec')
              AND (thread_source IS NULL OR thread_source != 'subagent')
              AND id NOT IN (SELECT value FROM json_each(?))
              AND (git_origin_url LIKE ? OR git_origin_url LIKE ?)
            ORDER BY updated_at DESC LIMIT 100
        ''', (json.dumps(list(excluded)), f'%github.com:{repo}.git', f'%github.com/{repo}.git')).fetchall()
except sqlite3.Error:
    sys.exit(0)

matches = [[], [], [], []]
seen = set()
for session_id, cwd, stored_branch, stored_head, _rollout in rows:
    if cwd in seen:
        continue
    seen.add(cwd)
    origin = git(cwd, 'remote', 'get-url', 'origin').lower()
    if not origin.endswith((f'github.com:{repo}.git', f'github.com/{repo}.git', f'github.com/{repo}')):
        continue
    live_branch = git(cwd, 'branch', '--show-current')
    live_head = git(cwd, 'rev-parse', 'HEAD')
    for index, matched in enumerate((live_branch == branch, live_head == head,
                                     stored_branch == branch, stored_head == head)):
        if matched:
            matches[index].append((session_id, cwd))
# Agents can resolve reviews in a temporary worktree without changing the
# session's original cwd. Check only indexed rollouts before scanning history.
if not matches[0] and not matches[1]:
    event_matches = [{}, {}]
    for session_id, _cwd, _branch, _head, rollout in rows:
        if not rollout:
            continue
        paths = set()
        try:
            with open(rollout) as events:
                for line in events:
                    if '"cwd"' not in line:
                        continue
                    try:
                        cwd = json.loads(line).get('payload', {}).get('item', {}).get('cwd')
                    except (ValueError, AttributeError):
                        continue
                    if isinstance(cwd, str):
                        paths.add(cwd.removeprefix('file://'))
        except OSError:
            continue
        for cwd in sorted(paths):
            root = git(cwd, 'rev-parse', '--show-toplevel')
            if not root:
                continue
            origin = git(root, 'remote', 'get-url', 'origin').lower()
            if not origin.endswith((f'github.com:{repo}.git', f'github.com/{repo}.git', f'github.com/{repo}')):
                continue
            for index, matched in enumerate((git(root, 'branch', '--show-current') == branch,
                                             git(root, 'rev-parse', 'HEAD') == head)):
                if matched:
                    event_matches[index].setdefault(root, (session_id, root))
    for index, group in enumerate(event_matches):
        matches[index].extend(group.values())
for group in matches:
    if len(group) == 1:
        print(*group[0], sep='\t')
        break
    if group:
        break
PY
    )
    if [[ -n $indexed_match ]]; then
      printf '%s\n' "$indexed_match"
      return 0
    fi
  fi

  # One rg pass and one Python process for the whole history: a head, jq,
  # and git pipeline per rollout took most of the remote status budget once
  # hundreds of tasks existed.
  python3 - "$codex_sessions_root" "$repo_lower" "$branch_name" "$head_sha" "$excluded_sessions" <<'PY'
import json
import subprocess
import sys

root, repo, branch, head, excluded = sys.argv[1:]
excluded = set(json.loads(excluded))
originators = {'Codex Desktop', 't3code_desktop', 'T3 Code', 'codex-tui', 'codex_exec'}
worktrees = {}


def origin_matches(url):
    return url.lower().endswith((f'github.com:{repo}.git', f'github.com/{repo}.git', f'github.com/{repo}'))


def git(cwd, *args):
    try:
        result = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout.strip() if result.returncode == 0 else ''


def worktree(cwd):
    """The live (branch, head) of a checkout of this repository, else None."""
    if cwd not in worktrees:
        worktrees[cwd] = (
            (git(cwd, 'branch', '--show-current'), git(cwd, 'rev-parse', 'HEAD'))
            if origin_matches(git(cwd, 'remote', 'get-url', 'origin')) else None
        )
    return worktrees[cwd]


def rg(*args):
    result = subprocess.run(['rg', *args], capture_output=True, text=True, errors='replace')
    return result.stdout


files = [path for path in rg('-i', '-l', '-0', '--fixed-strings', repo, root).split('\0') if path]
# Agents can resolve reviews in another worktree than the session's cwd.
event_cwds = {}
for start in range(0, len(files), 500):
    output = rg('-o', '-r', '$1', '--with-filename', '--null', '--no-line-number',
                r'"item":\{.*?"cwd":"([^"]*)"', '--', *files[start:start + 500])
    for line in output.splitlines():
        path, _, cwd = line.partition('\0')
        event_cwds.setdefault(path, set()).add(cwd.removeprefix('file://'))

# Each group maps a worktree to the newest matching (timestamp, session).
groups = {name: {} for name in ('branch', 'head', 'metadata_branch', 'metadata_head')}


def record(name, cwd, timestamp, session):
    if timestamp > groups[name].get(cwd, ('',))[0]:
        groups[name][cwd] = (timestamp, session)


for path in files:
    try:
        with open(path, errors='replace') as events:
            meta = json.loads(events.readline())
    except (OSError, ValueError):
        continue
    if not isinstance(meta, dict) or meta.get('type') != 'session_meta':
        continue
    payload = meta.get('payload') or {}
    git_meta = payload.get('git') or {}
    session = payload.get('session_id') or payload.get('id') or ''
    if (payload.get('originator') not in originators or payload.get('thread_source') == 'subagent'
            or session in excluded or not origin_matches(git_meta.get('repository_url') or '')):
        continue
    timestamp, cwd = meta.get('timestamp') or '', payload.get('cwd') or ''
    if not (timestamp and session and cwd):
        continue
    if git_meta.get('branch') == branch:
        record('metadata_branch', cwd, timestamp, session)
    if git_meta.get('commit_hash') == head:
        record('metadata_head', cwd, timestamp, session)
    if worktree(cwd) is None:
        continue
    for checkout in [cwd, *sorted(event_cwds.get(path, set()) - {cwd})]:
        current = worktree(checkout)
        if current and current[0] == branch:
            record('branch', checkout, timestamp, session)
        if current and current[1] == head:
            record('head', checkout, timestamp, session)

for group in groups.values():
    sessions = {session: cwd for cwd, (_timestamp, session) in group.items()}
    if len(sessions) == 1:
        print(*next(iter(sessions.items())), sep='\t')
        sys.exit(0)
    if sessions:
        sys.exit(1)
sys.exit(1)
PY
}

claude_agents_json() {
  if [[ -z $_claude_agents_json ]]; then
    if ! _claude_agents_json=$(claude agents --json 2>/dev/null); then
      _claude_agents_json='[]'
    fi
    [[ -n $_claude_agents_json ]] || _claude_agents_json='[]'
  fi
  printf '%s\n' "$_claude_agents_json"
}

matching_claude_session() {
  [[ -d $claude_projects_root ]] || return 1
  local excluded_sessions
  excluded_sessions=$(python3 "$script_dir/t3-delegate.py" --subagent-sessions --provider claudeAgent 2>/dev/null) || return 1
  # A shell/jq/git pipeline per transcript exceeded the SSH status budget.
  # Parse history in one process and inspect each worktree only once.
  python3 - "$claude_projects_root" "$repo" "$1" "$2" "$excluded_sessions" <<'PY'
import json
from pathlib import Path
import re
import subprocess
import sys

projects, repo, branch, head, excluded = sys.argv[1:]
excluded = set(json.loads(excluded))
repo = repo.lower()
worktrees = {}
matches = [{}, {}, {}]

def git(cwd, *args):
    try:
        result = subprocess.run(['git', '-C', cwd, *args], capture_output=True,
                                text=True, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''

for file in Path(projects).glob('*/*.jsonl'):
    entry = None
    try:
        with file.open() as events:
            for line in events:
                if '"cwd"' not in line or '"sessionId"' not in line:
                    continue
                try:
                    candidate = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(candidate, dict) and candidate.get('cwd')
                        and candidate.get('sessionId')):
                    entry = candidate
                    break
    except (OSError, UnicodeError):
        continue
    if entry is None or entry.get('isSidechain') or entry.get('sessionId') in excluded:
        continue
    cwd, session = entry['cwd'], entry['sessionId']
    if not isinstance(cwd, str) or not isinstance(session, str):
        continue
    if cwd not in worktrees:
        origin = git(cwd, 'remote', 'get-url', 'origin')
        repository = re.search(r'github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$', origin, re.I)
        worktrees[cwd] = (
            (git(cwd, 'branch', '--show-current'), git(cwd, 'rev-parse', 'HEAD'))
            if repository and repository.group(1).lower() == repo else None
        )
    current = worktrees[cwd]
    if current is None:
        continue
    timestamp = entry.get('timestamp') or '1970-01-01T00:00:00.000Z'
    if not isinstance(timestamp, str):
        continue
    for group, matched in zip(matches, (current[0] == branch, current[1] == head,
                                       entry.get('gitBranch') == branch)):
        if matched and (cwd not in group or timestamp > group[cwd][0]):
            group[cwd] = (timestamp, session)

for group in matches:
    if len(group) == 1:
        cwd, (_timestamp, session) = next(iter(group.items()))
        print(session, cwd, sep='\t')
        sys.exit(0)
    if group:
        break
sys.exit(1)
PY
}

claude_session_file() {
  local session_id=$1
  local session_file

  session_file=$(
    find "$claude_projects_root" -mindepth 2 -maxdepth 2 -type f \
      -name "${session_id}.jsonl" -print 2>/dev/null |
      head -n 1
  )
  [[ -n $session_file ]] || return 1
  printf '%s\n' "$session_file"
}

claude_session_state() {
  local session_id=$1
  local session_file=${2:-}
  local last_op last_stop pending mtime now age state

  if state=$(python3 "$script_dir/t3-delegate.py" --state "$session_id" --provider claudeAgent 2>/dev/null); then
    case $state in
      idle | running) printf '%s\n' "$state"; return 0 ;;
    esac
  fi

  state=$(jq -r --arg id "$session_id" '
      .[] | select(.sessionId == $id) | .status // .state // empty
    ' <<<"$(claude_agents_json)" 2>/dev/null || true)
  case $state in
    busy | running | starting) printf 'running\n'; return 0 ;;
    idle | ready | done | stopped) printf 'idle\n'; return 0 ;;
  esac

  if [[ -z $session_file ]]; then
    session_file=$(claude_session_file "$session_id" 2>/dev/null || true)
  fi
  if [[ -z $session_file || ! -f $session_file ]]; then
    printf 'unknown\n'
    return 0
  fi

  last_op=$(
    jq -r 'select(.type == "queue-operation") | .operation' "$session_file" 2>/dev/null |
      tail -n 1
  )
  if [[ $last_op == enqueue ]]; then
    printf 'running\n'
    return 0
  fi

  # The transcript is not written while a tool runs, so a long test run looks
  # stale. Each call ends with its tool result (including an interrupted one).
  IFS=$'\t' read -r last_stop pending < <(
    jq -n -R -r '
      def blocks($kind): [.message.content[]? | select(type == "object" and .type == $kind)];
      reduce (inputs | fromjson? | select(.type == "assistant" or .type == "user")) as $entry
        ({stop: "", pending: []};
         if $entry.type == "assistant" then
           .stop = ($entry.message.stop_reason // .stop)
           | .pending += [$entry | blocks("tool_use")[].id]
         else
           .pending -= [$entry | blocks("tool_result")[].tool_use_id]
         end)
      | "\(.stop)\t\(.pending | length)"
    ' "$session_file" 2>/dev/null
  ) || true
  mtime=$(stat -c %Y "$session_file" 2>/dev/null || printf '0')
  now=$(date +%s)
  age=$((now - mtime))
  # An unanswered call is still running unless the process died long ago.
  if [[ $last_stop == tool_use ]] &&
    { (( age < 120 )) || { (( ${pending:-0} > 0 )) && (( age < 1800 )); }; }; then
    printf 'running\n'
    return 0
  fi
  printf 'idle\n'
}
