# SPDX-License-Identifier: GPL-3.0-only

auto_delegation_mode() {
  local value='disabled'

  if [[ -f $auto_delegate_file ]]; then
    value=$(head -n 1 "$auto_delegate_file")
  fi
  case $value in
    0 | disabled | '')
      printf 'disabled\n'
      ;;
    1 | codex)
      # Legacy "1" meant Codex-only automatic delegation.
      printf 'codex\n'
      ;;
    auto | both)
      printf 'auto\n'
      ;;
    claude)
      printf 'claude\n'
      ;;
    *)
      printf 'disabled\n'
      ;;
  esac
}

auto_delegation_enabled() {
  [[ $(auto_delegation_mode) != disabled ]]
}

auto_merge_enabled() {
  if [[ -n $auto_merge_override ]]; then
    [[ $auto_merge_override == 1 ]]
    return
  fi
  [[ -f $auto_merge_file ]] && [[ $(head -n 1 "$auto_merge_file") == 1 ]]
}

merge_method() {
  local method='squash'
  if [[ -n $merge_method_override ]]; then
    method=$merge_method_override
  elif [[ -f $merge_method_file ]]; then
    method=$(head -n 1 "$merge_method_file")
  fi
  case $method in merge | squash | rebase) printf '%s\n' "$method" ;;
    *) printf 'squash\n' ;;
  esac
}

delete_branch_enabled() {
  if [[ -n $delete_branch_override ]]; then
    [[ $delete_branch_override == 1 ]]
    return
  fi
  [[ ! -f $delete_branch_file ]] || [[ $(head -n 1 "$delete_branch_file") != 0 ]]
}

merge_after_delegation_mode() {
  local mode=1
  if [[ -n $merge_after_delegation_override ]]; then
    mode=$merge_after_delegation_override
  elif [[ -f $merge_after_delegation_file ]]; then
    mode=$(head -n 1 "$merge_after_delegation_file")
  fi
  case $mode in 0 | 1 | agent) printf '%s\n' "$mode" ;;
    *) printf '1\n' ;;
  esac
}

merge_after_approval_enabled() {
  [[ ! -f $merge_after_approval_file ]] ||
    [[ $(head -n 1 "$merge_after_approval_file") != 0 ]]
}

merge_admin_enabled() {
  if [[ -n $merge_admin_override ]]; then
    [[ $merge_admin_override == 1 ]]
    return
  fi
  [[ -f $merge_admin_file ]] && [[ $(head -n 1 "$merge_admin_file") == 1 ]]
}

auto_merge_instruction() {
  auto_merge_enabled || return 0
  local pr=${1:-PR} method review_mode decision_text='' delete_text=' Keep the branch after merging.' delete_branch=0 admin=0
  review_mode=$(merge_after_delegation_mode)
  if [[ $review_mode == 0 ]]; then
    printf '\n\nDo not merge this PR after this pass. Let the queue request another CodeRabbit review; auto-merge requires CodeRabbit approval of the updated head.\n'
    return 0
  fi
  if [[ $review_mode == agent ]]; then
    decision_text='After pushing the fix, explicitly decide whether another CodeRabbit review is worthwhile for the fixes to the last CodeRabbit findings. Judge what those findings asked for and how you addressed them, not the overall PR size or risk; report your decision and reason. If another review of those fixes is worthwhile, leave the PR open for the queue to request it and stop; do not merge. Never trigger the review yourself. Otherwise, proceed with the guarded merge below. '
  fi
  method=$(merge_method)
  if delete_branch_enabled; then
    delete_branch=1
    delete_text=' Delete the branch after merging.'
  fi
  merge_admin_enabled && admin=1
  printf '\n\n%sAfter the task work is complete, wait for CI and every review bot to finish and resolve all review threads. Use the queue merge guard on this agent host: `~/.local/bin/coderabbit-review-queue --repo %s --merge-now %s "$(gh pr view %s --repo %s --json headRefOid --jq .headRefOid)" %s %s %s --local-agents`. The command uses the selected merge method without GitHub auto-merge and checks the live head, reviews, checks, and threads before merging.%s If it refuses, stop and report the blocker.\n' \
    "$decision_text" "$repo" "$pr" "$pr" "$repo" "$method" "$delete_branch" "$admin" "$delete_text"
}

default_delegation_prompt() {
  cat <<'PROMPT'
Address unresolved CodeRabbit feedback on PR #{pr_number} ({pr_title}).
PR: {pr_url}
Threads that triggered this delegation: {threads}

Follow repository instructions and existing task context.

1. Use gh to fetch the live PR and unresolved CodeRabbit threads; the list above is a snapshot. Stop if the PR is closed or merged. Ignore non-CodeRabbit feedback.
2. Confirm the PR branch and inspect the working tree. Preserve unrelated work. Verify every finding against current code, including outdated findings, and make the smallest necessary fixes.
3. Validate changes and recheck feedback before pushing. Bundle related fixes, commit only this task's changes using repository signing conventions, and push to the PR branch. If signing times out, stop and ask the user to say continue.
4. Reply with the fix and validation or a concrete dismissal reason, using repository attribution rules. Resolve fixes after pushing and dismissals after explaining. Leave uncertain or blocked findings open.
5. Reply directly to existing threads without creating or submitting reviews. Verify no pending reviews remain; delete accidental pending reviews only if they contain no unrelated user comments.

Never trigger CodeRabbit reviews through comments; the queue handles requests. Finish this pass without waiting for another review. Report changes, validation, the pushed commit, and blockers.
PROMPT
}

delegation_prompt_mode() {
  local mode=${delegation_prompt_mode_override:-default}
  if [[ -z $delegation_prompt_mode_override && -f $delegation_prompt_mode_file ]]; then
    mode=$(head -n 1 "$delegation_prompt_mode_file")
  fi
  case $mode in
    default | short | babysit | custom) printf '%s\n' "$mode" ;;
    *) printf 'default\n' ;;
  esac
}

delegation_prompt_template() {
  case $(delegation_prompt_mode) in
    short) printf 'Resolve the CodeRabbit review and stop.\n' ;;
    babysit)
      printf '%s\n' 'Continue babysitting this PR. Resolve the current CodeRabbit feedback, monitor CI and new review feedback, fix actionable failures and comments, and continue until the PR is merge-ready. Never trigger CodeRabbit reviews through comments.'
      ;;
    custom)
      if [[ -n $delegation_prompt_template_override ]]; then
        printf '%s\n' "$delegation_prompt_template_override"
      elif [[ -s $delegation_prompt_template_file ]]; then
        cat "$delegation_prompt_template_file"
      else
        default_delegation_prompt
      fi
      ;;
    *) default_delegation_prompt ;;
  esac
}

render_delegation_prompt() {
  local pr=$1 title=$2 threads=$3 template
  template=$(delegation_prompt_template)
  python3 -c '
import sys
template = sys.stdin.read()
values = {
    "{pr_number}": sys.argv[1],
    "{pr_title}": sys.argv[2],
    "{repo}": sys.argv[3],
    "{pr_url}": f"https://github.com/{sys.argv[3]}/pull/{sys.argv[1]}",
    "{threads}": sys.argv[4],
}
for placeholder, value in values.items():
    template = template.replace(placeholder, value)
sys.stdout.write(template)
' "$pr" "$title" "$repo" "$threads" <<<"$template"
}

stop_when_empty_enabled() {
  [[ -f $stop_when_empty_file ]] &&
    [[ $(head -n 1 "$stop_when_empty_file") == 1 ]]
}

wait_on_empty_queue() {
  if stop_when_empty_enabled; then
    printf 'Every open PR has a current CodeRabbit review.\n'
    desktop_notify \
      'CodeRabbit queue complete' \
      "Every queued $repo PR has a current CodeRabbit review."
    return 1
  fi

  if (( empty_announced == 0 )); then
    printf 'Queue is empty; continuing to monitor %s for future work.\n' "$repo"
    empty_announced=1
  fi
  sleep 60
  return 0
}

codex_thread_metadata() {
  local session_id=$1 metadata='' session_file
  if [[ -f $codex_state_db ]]; then
    metadata=$(python3 - "$codex_state_db" "$session_id" <<'PY'
import json
import sqlite3
import sys

database, session_id = sys.argv[1:]
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
    row = connection.execute(
        """
        SELECT COALESCE(NULLIF(name, ''), title), model, reasoning_effort,
               sandbox_policy, approval_mode
        FROM threads
        WHERE id = ?
        """,
        (session_id,),
    ).fetchone()
if row is not None:
    values = [
        *row[:3],
        json.dumps(json.loads(row[3]), separators=(",", ":")),
        row[4],
    ]
    print("\t".join((value or "").replace("\t", " ").replace("\n", " ") for value in values))
PY
    ) || return 1
  fi
  if [[ -n $metadata ]]; then
    printf '%s\n' "$metadata"
    return 0
  fi
  session_file=$(codex_session_file "$session_id") || return 1
  [[ -n $session_file ]] || return 1
  jq -R -s -r '
    [split("\n")[] | fromjson? | select(.type == "turn_context") | .payload]
    | last // empty
    | [
        "—",
        (.model // ""),
        (.effort // ""),
        (.sandbox_policy // {} | tojson),
        (.approval_policy // "")
      ]
    | map(tostring | gsub("[\\t\\n]"; " "))
    | join("\t")
  ' "$session_file"
}

codex_session_file() {
  local session_id=$1 path=''
  if [[ -f $codex_state_db ]]; then
    path=$(python3 - "$codex_state_db" "$session_id" <<'PY'
import sqlite3
import sys

database, session_id = sys.argv[1:]
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
    row = connection.execute(
        "SELECT rollout_path FROM threads WHERE id = ?", (session_id,)
    ).fetchone()
if row is not None and row[0]:
    print(row[0])
PY
)
    if [[ -n $path && -f $path ]]; then
      printf '%s\n' "$path"
      return 0
    fi
  fi
  rg --files "$codex_sessions_root" 2>/dev/null |
    rg "/rollout-.*-${session_id}[^/]*\\.jsonl$" |
    xargs -r stat -c $'%Y\t%n' 2>/dev/null |
    sort -nr |
    head -n 1 |
    cut -f2-
}

codex_resume_permission_args() {
  local sandbox_policy=$1 approval_mode=$2 policy_type
  local -n args=$3
  policy_type=$(jq -r '.type // empty' <<<"$sandbox_policy") || return 1
  case $policy_type in
    disabled)
      args+=(--dangerously-bypass-approvals-and-sandbox)
      ;;
    danger-full-access)
      args+=(--sandbox danger-full-access)
      [[ -z $approval_mode ]] || args+=(-c "approval_policy=$approval_mode")
      ;;
    read-only)
      args+=(--sandbox read-only)
      [[ -z $approval_mode ]] || args+=(-c "approval_policy=$approval_mode")
      ;;
    workspace-write)
      args+=(--sandbox workspace-write)
      [[ -z $approval_mode ]] || args+=(-c "approval_policy=$approval_mode")
      ;;
    *)
      printf 'Cannot safely reproduce Codex sandbox policy %q for delegation.\n' \
        "$policy_type" >&2
      return 1
      ;;
  esac
}

codex_session_state() {
  local session_id=$1
  local session_file state

  if ! session_file=$(codex_session_file "$session_id") || \
    [[ -z $session_file ]]; then
    printf 'unknown\n'
    return 0
  fi
  if ! state=$(jq -R -s -r '
    reduce (
      split("\n")[]
      | fromjson?
      | select(
          .type == "event_msg"
          and (
            .payload.type == "task_started"
            or .payload.type == "task_complete"
            or .payload.type == "turn_aborted"
          )
        )
    ) as $event (
      {};
      if $event.payload.type == "task_started" then
        .[$event.payload.turn_id] = true
      else
        del(.[$event.payload.turn_id])
      end
    )
    | if length > 0 then "running" else "idle" end
  ' "$session_file" 2>/dev/null); then
    printf 'unknown\n'
    return 0
  fi
  case $state in
    idle | running) printf '%s\n' "$state" ;;
    *) printf 'unknown\n' ;;
  esac
}

codex_thread_is_archived() {
  local session_id=$1
  [[ -f $codex_state_db ]] || return 1
  [[ $(python3 - "$codex_state_db" "$session_id" <<'PY'
import sqlite3
import sys

database, session_id = sys.argv[1:]
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
    row = connection.execute(
        "SELECT archived FROM threads WHERE id = ?", (session_id,)
    ).fetchone()
print(row[0] if row else 0)
PY
  ) == 1 ]]
}

archive_codex_thread() {
  local session_id=$1 socket_path
  codex_thread_is_archived "$session_id" && return 0
  socket_path="$codex_home/app-server-control/app-server-control.sock"
  if [[ -S $socket_path ]]; then
    codex archive --remote "unix://$socket_path" "$session_id"
  else
    codex archive "$session_id"
  fi
}

find_codex_session_id() {
  local branch_name=$1 head_sha=$2 match session_id file
  local -A candidates=()
  if match=$(matching_codex_session "$branch_name" "$head_sha" 2>/dev/null); then
    IFS=$'\t' read -r session_id _ <<<"$match"
    printf '%s\n' "$session_id"
    return 0
  fi
  [[ -f $codex_state_db ]] || return 1
  if python3 - "$codex_state_db" "$repo" "$branch_name" "$head_sha" <<'PY'
import sqlite3
import sys

database, repo, branch, head = sys.argv[1:]
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
    rows = connection.execute(
        """SELECT id, git_branch, git_sha FROM threads
        WHERE originator IN ('Codex Desktop', 't3code_desktop', 'codex-tui', 'codex_exec')
          AND (git_origin_url LIKE ? OR git_origin_url LIKE ?)
          AND (git_branch = ? OR git_sha = ?)""",
        (f"%github.com:{repo}.git", f"%github.com/{repo}.git", branch, head),
    ).fetchall()
for matches in (
    [row[0] for row in rows if row[2] == head],
    [row[0] for row in rows if row[1] == branch],
):
    if len(matches) == 1:
        print(matches[0])
        raise SystemExit(0)
    if matches:
        break
raise SystemExit(1)
PY
  then
    return 0
  fi
  # A task can create several PRs; its stored branch then reflects only the
  # last one. Search its rollout when the exact stored branch/head is gone.
  while IFS= read -r file; do
    [[ -f $file ]] || continue
    session_id=$(head -n 1 "$file" 2>/dev/null | jq -r --arg repo "$repo" '
      select(.type == "session_meta"
        and (.payload.originator | IN("Codex Desktop", "t3code_desktop", "codex-tui", "codex_exec"))
        and (.payload.thread_source // "") != "subagent"
        and ((.payload.git.repository_url // "")
          | endswith("github.com:" + $repo + ".git")
            or endswith("github.com/" + $repo + ".git")))
      | .payload.session_id // .payload.id // empty
    ' 2>/dev/null) || continue
    [[ -n $session_id ]] && candidates["$session_id"]=1
  done < <(
    rg -l --fixed-strings -- "$branch_name" \
      "$codex_sessions_root" "$codex_home/archived_sessions" 2>/dev/null || true
  )
  (( ${#candidates[@]} == 1 )) || return 1
  printf '%s\n' "${!candidates[@]}"
}

archive_matching_codex_thread() {
  local branch_name=$1 head_sha=$2 session_id state
  if ! session_id=$(find_codex_session_id "$branch_name" "$head_sha"); then
    printf 'No matching Codex task to archive.\n'
    return 2
  fi
  archive_codex_session_if_idle "$session_id"
}

archive_codex_session_if_idle() {
  local session_id=$1 state
  codex_thread_is_archived "$session_id" && return 0
  state=$(codex_session_state "$session_id")
  if [[ $state != idle ]]; then
    printf 'Codex task %s is %s; deferring archive.\n' "$session_id" "$state"
    return 3
  fi
  archive_codex_thread "$session_id"
}

resume_codex_via_daemon() {
  local session_id=$1 prompt=$2 socket_path log_path pid_file client_pid
  local started=0 attempt command watcher_pid_file
  socket_path="$codex_home/app-server-control/app-server-control.sock"
  [[ -S $socket_path ]] || return 1
  log_path="/tmp/coderabbit-review-queue-codex-$session_id.log"
  printf -v command '%q ' env TERM=xterm-256color codex \
    --remote "unix://$socket_path" --no-alt-screen resume "$session_id" "$prompt"
  pid_file="/tmp/coderabbit-review-queue-codex-$session_id.pid"
  rm -f -- "$pid_file"
  setsid -f bash -c '
    printf "%s\n" "$$" >"$1"
    tail -f /dev/null | script -qefc "$2" "$3"
  ' _ "$pid_file" "$command" "$log_path" </dev/null >/dev/null 2>&1
  for attempt in {1..20}; do
    [[ -s $pid_file ]] && break
    sleep 0.05
  done
  [[ -s $pid_file ]] || return 1
  client_pid=$(<"$pid_file")
  rm -f -- "$pid_file"
  for attempt in {1..20}; do
    if [[ $(codex_session_state "$session_id") == running ]]; then
      started=1
      break
    fi
    kill -0 "$client_pid" 2>/dev/null || break
    sleep 0.5
  done
  if (( started == 0 )); then
    kill -- -"$client_pid" 2>/dev/null || true
    return 1
  fi
  watcher_pid_file="/tmp/coderabbit-review-queue-codex-$session_id-watcher.pid"
  rm -f -- "$watcher_pid_file"
  setsid -f bash -c '
    printf "%s\n" "$$" >"$1"
    while kill -0 "$2" 2>/dev/null; do
      sleep 10
      if [[ $("$3" --repo "$4" --codex-session-state "$5") == idle ]]; then
        kill -- -"$2" 2>/dev/null || true
        break
      fi
    done
    rm -f -- "$1"
  ' _ "$watcher_pid_file" "$client_pid" "$script_path" "$repo" "$session_id" \
    </dev/null >/dev/null 2>&1
  return 0
}

codex_session_originator() {
  local session_file
  session_file=$(codex_session_file "$1") || return 1
  [[ -n $session_file ]] || return 1
  head -n 1 "$session_file" | jq -r '.payload.originator // empty'
}

resume_codex_via_t3() {
  local session_id=$1 prompt=$2
  printf '%s' "$prompt" | python3 "$script_dir/t3-delegate.py" "$session_id"
}

resume_codex_via_exec() {
  local session_id=$1 client_pid pid_file log_path attempt
  shift
  pid_file="/tmp/coderabbit-review-queue-codex-$session_id-exec.pid"
  log_path="/tmp/coderabbit-review-queue-codex-$session_id-exec.log"
  rm -f -- "$pid_file"
  setsid -f bash -c '
    printf "%s\n" "$$" >"$1"
    shift
    "$@"
  ' _ "$pid_file" codex "$@" </dev/null >"$log_path" 2>&1
  for attempt in {1..20}; do
    [[ -s $pid_file ]] && break
    sleep 0.05
  done
  [[ -s $pid_file ]] || return 1
  client_pid=$(<"$pid_file")
  rm -f -- "$pid_file"
  for attempt in {1..40}; do
    if [[ $(codex_session_state "$session_id") == running ]]; then
      return 0
    fi
    if ! kill -0 "$client_pid" 2>/dev/null || \
      [[ $(ps -o stat= -p "$client_pid" 2>/dev/null) == Z* ]]; then
      break
    fi
    sleep 0.5
  done
  [[ $(codex_session_state "$session_id") == running ]] && return 0
  kill -- -"$client_pid" 2>/dev/null || true
  printf 'Codex CLI did not start a new turn for task %s.\n' "$session_id" >&2
  return 1
}

route_unresolved_review() {
  local pr=$1
  local branch_name=$2
  local head_sha=$3
  local title=$4
  local row thread_id path line outdated
  local mode allow_codex=0 allow_claude=0
  local codex_match='' claude_match=''
  local codex_session_id='' codex_session_cwd=''
  local claude_session_id='' claude_session_cwd=''
  local codex_state='none' claude_state='none'
  local prompt agent session_id session_cwd host remote_output
  local -a new_threads=()
  local -a new_ids=()

  mode=${agent_mode_override:-$(auto_delegation_mode)}
  if (( force_delegation == 1 )); then
    [[ $mode != disabled ]] || mode=auto
  else
    [[ $mode != disabled ]] || return 0
  fi
  case $mode in
    auto)
      allow_codex=1
      allow_claude=1
      ;;
    codex) allow_codex=1 ;;
    claude) allow_claude=1 ;;
    *) return 0 ;;
  esac

  while IFS=$'\t' read -r thread_id path line outdated; do
    [[ -n ${thread_id:-} ]] || continue
    if (( force_delegation == 0 )); then
      if [[ -f $routed_threads_file ]] &&
        rg -q --fixed-strings --line-regexp "$thread_id" "$routed_threads_file"; then
        continue
      fi
      if [[ -n ${attempted_route_threads[$thread_id]+present} ]]; then
        continue
      fi
    fi
    new_threads+=("$thread_id ($path:$line, outdated=$outdated)")
    new_ids+=("$thread_id")
  done < <(unresolved_coderabbit_rows "$pr")

  (( ${#new_threads[@]} > 0 )) || return 0

  host=$(agent_host 2>/dev/null || true)
  if [[ -n $host ]]; then
    local merge_enabled=0 merge_delete=0 merge_after_delegation merge_admin=0
    auto_merge_enabled && merge_enabled=1
    delete_branch_enabled && merge_delete=1
    merge_after_delegation=$(merge_after_delegation_mode)
    merge_admin_enabled && merge_admin=1
    if remote_output=$(remote_agent_command \
      --delegate "$pr" --agent-mode "$mode" --local-agents \
      --delegation-prompt-mode "$(delegation_prompt_mode)" \
      --delegation-prompt-template "$(delegation_prompt_template)" \
      --auto-merge-setting "$merge_enabled" "$(merge_method)" "$merge_delete" \
      "$merge_after_delegation" "$merge_admin"); then
      printf '%s\n' "$remote_output"
      if [[ $remote_output == *"Routing unresolved CodeRabbit review"* ]]; then
        mark_pr_delegated "$pr"
        if archive_after_merge_enabled; then
          local archive_session_id
          archive_session_id=$(agent_codex_session_id "$branch_name" "$head_sha" || true)
          queue_pending_archive "$pr" "$branch_name" "$head_sha" "$title" \
            "$archive_session_id"
        fi
        for thread_id in "${new_ids[@]}"; do
          attempted_route_threads["$thread_id"]=1
          printf '%s\n' "$thread_id" >>"$routed_threads_file"
        done
      fi
      return 0
    fi
    printf 'Remote agent routing failed on %s for PR #%s.\n' "$host" "$pr" >&2
    return 1
  fi

  if (( allow_codex )); then
    if codex_match=$(matching_codex_session "$branch_name" "$head_sha"); then
      IFS=$'\t' read -r codex_session_id codex_session_cwd <<<"$codex_match"
      codex_state=$(codex_session_state "$codex_session_id")
    else
      codex_state='missing'
    fi
  fi
  if (( allow_claude )); then
    if claude_match=$(matching_claude_session "$branch_name" "$head_sha"); then
      IFS=$'\t' read -r claude_session_id claude_session_cwd <<<"$claude_match"
      claude_state=$(claude_session_state "$claude_session_id")
    else
      claude_state='missing'
    fi
  fi

  if [[ $codex_state == running || $claude_state == running ]]; then
    printf 'Matching agent task for PR #%s is already running; deferring review routing.\n' \
      "$pr"
    return 0
  fi

  agent=''
  session_id=''
  session_cwd=''
  if [[ $codex_state == idle ]]; then
    agent=codex
    session_id=$codex_session_id
    session_cwd=$codex_session_cwd
  elif [[ $claude_state == idle ]]; then
    agent=claude
    session_id=$claude_session_id
    session_cwd=$claude_session_cwd
  elif [[ $codex_state == unknown || $claude_state == unknown ]]; then
    printf 'Could not verify that a matching agent for PR #%s is idle; deferring review routing.\n' \
      "$pr"
    return 0
  else
    printf 'No matching idle agent task for PR #%s; not routing review.\n' "$pr"
    desktop_notify \
      'CodeRabbit review needs routing' \
      "PR #$pr — $title"$'\n'"Unresolved feedback has no matching idle agent task." \
      'critical'
    return 0
  fi

  prompt=$(render_delegation_prompt \
    "$pr" "$title" "$(IFS=', '; printf '%s' "${new_threads[*]}")")
  for thread_id in "${new_ids[@]}"; do
    if [[ $thread_id == nitpick:* ]]; then
      prompt+=$'\n\nCodeRabbit also left nitpick feedback in a review body on the current PR head. Fetch that review with gh, verify and address its findings; it is not a resolvable review thread.'
      break
    fi
  done
  prompt+=$(auto_merge_instruction "$pr")

  case $agent in
    codex)
      resume_codex_session "$pr" "$title" "$head_sha" "$session_id" \
        "$session_cwd" "$prompt" new_ids
      ;;
    claude)
      resume_claude_session "$pr" "$title" "$session_id" "$session_cwd" "$prompt" \
        new_ids
      ;;
  esac
}

# nameref-friendly: last arg is the name of an array of thread IDs to mark routed.
resume_codex_session() {
  local pr=$1
  local title=$2
  local head_sha=$3
  local session_id=$4
  local session_cwd=$5
  local prompt=$6
  local -n thread_ids=$7
  local session_state resume_status metadata task_title task_model task_effort
  local task_sandbox_policy task_approval_mode
  local originator
  local routing_lock_path routing_lock_fd
  local thread_id
  local -a codex_args

  git -C "$session_cwd" rev-parse --git-common-dir >/dev/null
  routing_lock_path="/tmp/coderabbit-review-queue-$repo_key-codex-$session_id.lock"
  exec {routing_lock_fd}>"$routing_lock_path"
  if ! flock -n "$routing_lock_fd"; then
    printf 'Codex routing for PR #%s is already being dispatched; skipping duplicate.\n' \
      "$pr"
    exec {routing_lock_fd}>&-
    return 0
  fi
  set_cloexec "$routing_lock_fd" || true

  sleep 1
  session_state=$(codex_session_state "$session_id")
  if [[ $session_state != idle ]]; then
    if [[ $session_state == running ]]; then
      printf 'Codex task %s for PR #%s started while routing; deferring review routing.\n' \
        "$session_id" "$pr"
    else
      printf 'Could not verify that Codex task %s for PR #%s remained idle; deferring review routing.\n' \
        "$session_id" "$pr"
    fi
    exec {routing_lock_fd}>&-
    return 0
  fi

  printf 'Routing unresolved CodeRabbit review on PR #%s to Codex task %s.\n' \
    "$pr" "$session_id"
  metadata=$(codex_thread_metadata "$session_id" 2>/dev/null || true)
  IFS=$'\t' read -r task_title task_model task_effort task_sandbox_policy \
    task_approval_mode <<<"$metadata"
  codex_args=(exec)
  [[ -z $task_model ]] || codex_args+=(-m "$task_model")
  [[ -z $task_effort ]] || codex_args+=(-c "model_reasoning_effort=$task_effort")
  if ! codex_resume_permission_args "$task_sandbox_policy" "$task_approval_mode" \
    codex_args; then
    desktop_notify \
      'CodeRabbit delegation blocked' \
      "PR #$pr — $title"$'\n''Could not safely reproduce the task permissions.' \
      'critical'
    exec {routing_lock_fd}>&-
    return 1
  fi
  codex_args+=(resume --all "$session_id" "$prompt")
  originator=$(codex_session_originator "$session_id" 2>/dev/null || true)
  if (
    cd "$session_cwd"
    [[ $(codex_session_state "$session_id") == idle ]] || exit 75
    if [[ $originator == 't3code_desktop' ]]; then
      resume_codex_via_t3 "$session_id" "$prompt"
      exit $?
    fi
    if [[ $originator == 'Codex Desktop' ]] && \
      resume_codex_via_daemon "$session_id" "$prompt"; then
      exit 0
    fi
    [[ $(codex_session_state "$session_id") != running ]] || exit 0
    resume_codex_via_exec "$session_id" "${codex_args[@]}"
  ); then
    mark_pr_delegated "$pr"
    for thread_id in "${thread_ids[@]}"; do
      attempted_route_threads["$thread_id"]=1
      printf '%s\n' "$thread_id" >>"$routed_threads_file"
    done
    desktop_notify \
      'CodeRabbit review routed' \
      "PR #$pr — $title"$'\n'"Sent unresolved feedback to its Codex task."
  else
    resume_status=$?
    if (( resume_status == 75 )); then
      printf 'Codex task %s for PR #%s was no longer verifiably idle; deferring review routing.\n' \
        "$session_id" "$pr"
    else
      for thread_id in "${thread_ids[@]}"; do
        attempted_route_threads["$thread_id"]=1
      done
      printf 'Codex task resume failed for PR #%s; leaving it eligible for retry.\n' "$pr" >&2
      desktop_notify \
        'CodeRabbit routing failed' \
        "PR #$pr — $title"$'\n'"Could not resume the matching Codex task." \
        'critical'
    fi
  fi
  exec {routing_lock_fd}>&-
}

resume_claude_session() {
  local pr=$1
  local title=$2
  local session_id=$3
  local session_cwd=$4
  local prompt=$5
  local -n thread_ids=$6
  local git_common_dir session_state resume_status session_file
  local routing_lock_path routing_lock_fd
  local thread_id

  session_file=$(claude_session_file "$session_id" 2>/dev/null || true)
  git_common_dir=$(
    git -C "$session_cwd" rev-parse --path-format=absolute --git-common-dir
  )
  routing_lock_path="/tmp/coderabbit-review-queue-$repo_key-claude-$session_id.lock"
  exec {routing_lock_fd}>"$routing_lock_path"
  if ! flock -n "$routing_lock_fd"; then
    printf 'Claude routing for PR #%s is already being dispatched; skipping duplicate.\n' \
      "$pr"
    exec {routing_lock_fd}>&-
    return 0
  fi
  set_cloexec "$routing_lock_fd" || true

  sleep 1
  session_state=$(claude_session_state "$session_id" "$session_file")
  if [[ $session_state != idle ]]; then
    if [[ $session_state == running ]]; then
      printf 'Claude session %s for PR #%s started while routing; deferring review routing.\n' \
        "$session_id" "$pr"
    else
      printf 'Could not verify that Claude session %s for PR #%s remained idle; deferring review routing.\n' \
        "$session_id" "$pr"
    fi
    exec {routing_lock_fd}>&-
    return 0
  fi

  printf 'Routing unresolved CodeRabbit review on PR #%s to Claude session %s.\n' \
    "$pr" "$session_id"
  if (
    cd "$session_cwd"
    [[ $(claude_session_state "$session_id" "$session_file") == idle ]] || exit 75
    claude -p \
      --resume "$session_id" \
      --permission-mode acceptEdits \
      --add-dir "$git_common_dir" \
      "$prompt"
  ); then
    for thread_id in "${thread_ids[@]}"; do
      attempted_route_threads["$thread_id"]=1
      printf '%s\n' "$thread_id" >>"$routed_threads_file"
    done
    desktop_notify \
      'CodeRabbit review routed' \
      "PR #$pr — $title"$'\n'"Sent unresolved feedback to its Claude session."
  else
    resume_status=$?
    if (( resume_status == 75 )); then
      printf 'Claude session %s for PR #%s was no longer verifiably idle; deferring review routing.\n' \
        "$session_id" "$pr"
    else
      for thread_id in "${thread_ids[@]}"; do
        attempted_route_threads["$thread_id"]=1
      done
      printf 'Claude session resume failed for PR #%s; leaving it eligible for retry.\n' \
        "$pr" >&2
      desktop_notify \
        'CodeRabbit routing failed' \
        "PR #$pr — $title"$'\n'"Could not resume the matching Claude session." \
        'critical'
    fi
  fi
  exec {routing_lock_fd}>&-
}

route_all_unresolved() {
  local state=$1
  local pr branch_name head_sha title

  while IFS=$'\t' read -r pr branch_name head_sha title; do
    [[ -n ${pr:-} ]] || continue
    if ! route_unresolved_review "$pr" "$branch_name" "$head_sha" "$title"; then
      printf 'Agent routing failed for PR #%s; review monitoring will continue.\n' \
        "$pr" >&2
      desktop_notify \
        'CodeRabbit routing failed' \
        "PR #$pr — $title"$'\n'"Review monitoring remains active." \
        'critical'
    fi
  done < <(reviewed_rows <<<"$state")
  return 0
}
