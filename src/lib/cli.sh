# SPDX-License-Identifier: GPL-3.0-only

show_status() {
  local snapshot_max_age=${1:-10}
  local state expiry now row pr branch_name head_sha title count task_progress review_mode
  local completed_pr completed_head completed_title
  local finished=''
  local -a active=()
  local -a approved=()
  local -A active_prs=()
  local -A unresolved_prs=()
  local -A finished_prs=()
  local -A queued_prs=()

  status_quota_available || return $?
  state=$(snapshot "$snapshot_max_age")
  review_mode=$(merge_after_delegation_mode)
  _claude_agents_json=''
  load_stale_rows "$state" 1
  for row in "${stale[@]}"; do
    IFS=$'\t' read -r pr _ <<<"$row"
    queued_prs[$pr]=1
  done
  printf 'Repository: %s\n' "$repo"

  while IFS=$'\t' read -r pr title; do
    [[ -n ${pr:-} ]] || continue
    active+=("$pr"$'\t'"$title")
    active_prs[$pr]=1
  done < <(active_review_rows "$state")
  if (( ${#active[@]} > 0 )); then
    printf 'Active reviews:\n'
    for row in "${active[@]}"; do
      IFS=$'\t' read -r pr title <<<"$row"
      printf '  #%s %s\n' "$pr" "$title"
    done
  fi

  while IFS=$'\t' read -r pr title; do
    [[ -n ${pr:-} ]] || continue
    approved+=("$pr"$'\t'"$title")
  done < <(approved_review_rows "$state")

  while IFS=$'\t' read -r pr branch_name head_sha title; do
    [[ -n ${pr:-} ]] || continue
    count=$(
      unresolved_coderabbit_rows "$pr" |
        awk 'NF { count++ } END { print count + 0 }'
    )
    if (( count > 0 )); then
      unresolved_prs[$pr]=1
      finished_prs[$pr]=1
      finished+=$'\n'"  #$pr $title"
      finished+=$'\n'"    Result: $count unresolved"
      task_progress=$(agent_task_progress "$branch_name" "$head_sha")
      finished+=$'\n'"    Agent task: $task_progress"
    fi
  done < <(reviewed_rows <<<"$state")

  if [[ -f $delegated_prs_file ]]; then
    while IFS= read -r pr; do
      [[ $pr =~ ^[0-9]+$ && -z ${finished_prs[$pr]:-} ]] || continue
      row=$(jq -r --argjson pr "$pr" '
        .data.repository.pullRequests.nodes[]
        | select(.number == $pr)
        | [.headRefName, .headRefOid, .title]
        | @tsv
      ' <<<"$state")
      [[ -n $row ]] || continue
      [[ -z ${active_prs[$pr]:-} ]] || continue
      IFS=$'\t' read -r branch_name head_sha title <<<"$row"
      count=$(unresolved_coderabbit_rows "$pr" |
        awk 'NF { count++ } END { print count + 0 }')
      # In agent-decision mode, an idle task with a still-unreviewed head
      # means the monitor can request another review. Show the same queue
      # decision in the UI instead of pinning its old delegation below.
      if (( count == 0 )) && auto_merge_enabled && \
        [[ $review_mode == agent || $review_mode == 0 ]] && \
        [[ -n ${queued_prs[$pr]:-} ]]; then
        continue
      fi
      task_progress=$(agent_task_progress "$branch_name" "$head_sha")
      # A delegated review remains completed for this PR even after the agent
      # pushes a new head and becomes idle. Otherwise the new head looks like
      # an unreviewed PR and is incorrectly put back into the review queue.
      unresolved_prs[$pr]=1
      finished_prs[$pr]=1
      finished+=$'\n'"  #$pr $title"
      if (( count > 0 )); then
        finished+=$'\n'"    Result: $count unresolved"
      elif [[ $task_progress == *Running* ]]; then
        finished+=$'\n'"    Result: Agent handling review"
      else
        finished+=$'\n'"    Result: Agent completed review"
      fi
      finished+=$'\n'"    Agent task: $task_progress"
    done <"$delegated_prs_file"
  fi

  for row in "${approved[@]}"; do
    IFS=$'\t' read -r pr title <<<"$row"
    [[ -z ${unresolved_prs[$pr]:-} ]] || continue
    unresolved_prs[$pr]=1
    finished_prs[$pr]=1
    finished+=$'\n'"  #$pr $title"
    finished+=$'\n'"    Result: Approved"
    finished+=$'\n'"    Agent task: —"
  done

  if [[ -s $review_completion_file ]]; then
    IFS=$'\t' read -r completed_pr completed_head completed_title \
      <"$review_completion_file"
    if [[ $completed_pr =~ ^[0-9]+$ ]] && \
      [[ -z ${finished_prs[$completed_pr]:-} ]] && \
      jq -e --argjson pr "$completed_pr" --arg head "$completed_head" '
        any(.data.repository.pullRequests.nodes[];
          .number == $pr and .headRefOid == $head)
      ' <<<"$state" >/dev/null; then
      unresolved_prs[$completed_pr]=1
      finished+=$'\n'"  #$completed_pr $completed_title"
      finished+=$'\n'"    Result: Review completed; syncing details"
      finished+=$'\n'"    Agent task: —"
    fi
  fi

  # Rebuild queue without unresolved PRs for display. load_stale_rows already
  # excludes them for the monitor; keep the same rule visible in --status.
  if (( ${#stale[@]} == 0 )); then
    if (( ${#active[@]} == 0 )) && [[ -z $finished ]]; then
      printf 'No eligible open PR has an outstanding CodeRabbit review request.\n'
    fi
  else
    printf 'Queued PRs:\n'
    for row in "${stale[@]}"; do
      IFS=$'\t' read -r pr _updated_at _branch_name _head_sha title _limit_at <<<"$row"
      [[ -z ${unresolved_prs[$pr]:-} ]] || continue
      # Still listed for ordering, but mark in-flight reviews clearly.
      if [[ -n ${active_prs[$pr]:-} ]]; then
        printf '  #%s %s (in progress)\n' "$pr" "$title"
      else
        printf '  #%s %s\n' "$pr" "$title"
      fi
    done

    expiry=$(latest_expiry "$state")
    quota_expiry=$(shared_expiry)
    (( quota_expiry > expiry )) && expiry=$quota_expiry
    now=$(date -u +%s)
    if (( expiry > now )); then
      printf 'Next outstanding rate limit expires at %s UTC.\n' \
        "$(date -u -d "@$expiry" +%Y-%m-%dT%H:%M:%SZ)"
    fi
  fi

  if [[ -n $finished ]]; then
    printf '\nFinished CodeRabbit reviews:%s\n' "$finished"
  fi
}

list_repositories() {
  local org selection candidate login
  local -A seen=()
  local -a orgs=()

  # Personal repos are not covered by orgs/*/installations. Discover them the
  # same way as org installs with repository_selection=selected: via PRs that
  # CodeRabbit has already commented on.
  login=$(gh api user --jq .login)
  while IFS= read -r candidate; do
    [[ -n $candidate && -z ${seen[$candidate]:-} ]] || continue
    seen[$candidate]=1
    printf '%s\n' "$candidate"
  done < <(
    gh api --paginate -X GET search/issues \
      -f "q=user:$login is:pr commenter:coderabbitai[bot]" \
      -f per_page=100 \
      --jq '.items[].repository_url | split("/")[-2:] | join("/")'
  )

  mapfile -t orgs < <(
    gh api --paginate user/orgs --jq '.[].login'
  )

  for org in "${orgs[@]}"; do
    selection=$(
      gh api "orgs/$org/installations" --jq '
        [
          .installations[]
          | select((.app_slug | ascii_downcase) == "coderabbitai")
          | .repository_selection
        ][0] // empty
      ' 2>/dev/null
    ) || continue
    [[ -n $selection ]] || continue

    if [[ $selection == all ]]; then
      while IFS= read -r candidate; do
        [[ -n $candidate && -z ${seen[$candidate]:-} ]] || continue
        seen[$candidate]=1
        printf '%s\n' "$candidate"
      done < <(
        gh api --paginate -X GET "orgs/$org/repos" \
          -f type=all \
          -f sort=updated \
          -f per_page=100 \
          --jq '.[].full_name'
      )
    else
      while IFS= read -r candidate; do
        [[ -n $candidate && -z ${seen[$candidate]:-} ]] || continue
        seen[$candidate]=1
        printf '%s\n' "$candidate"
      done < <(
        gh api --paginate -X GET search/issues \
          -f "q=org:$org is:pr commenter:coderabbitai[bot]" \
          -f per_page=100 \
          --jq '.items[].repository_url | split("/")[-2:] | join("/")'
      )
    fi
  done
}

show_gui() {
  python3 "$gui_script"
}

delegate_pr_now() {
  local requested_pr=$1
  local state pr branch_name head_sha title

  state=$(snapshot 0)
  while IFS=$'\t' read -r pr branch_name head_sha title; do
    [[ $pr == "$requested_pr" ]] || continue
    force_delegation=1
    route_unresolved_review "$pr" "$branch_name" "$head_sha" "$title"
    return 0
  done < <(reviewed_rows <<<"$state")

  printf 'PR #%s is not an eligible open pull request.\n' "$requested_pr" >&2
  return 1
}

delegate_all_now() {
  local state pr branch_name head_sha title failed=0
  state=$(snapshot 0)
  force_delegation=1
  while IFS=$'\t' read -r pr branch_name head_sha title; do
    [[ -n ${pr:-} ]] || continue
    if ! route_unresolved_review "$pr" "$branch_name" "$head_sha" "$title"; then
      failed=1
      break
    fi
  done < <(reviewed_rows <<<"$state")
  return "$failed"
}

validate_repo() {
  local canonical
  if ! canonical=$(gh repo view "$repo" --json nameWithOwner --jq .nameWithOwner 2>/dev/null); then
    printf 'Repository %s could not be found or accessed.\n' "$repo" >&2
    return 1
  fi
  if [[ $canonical != "$repo" ]]; then
    printf 'Repository name is %s, not %s.\n' "$canonical" "$repo" >&2
    return 1
  fi
  printf '%s\n' "$canonical"
}

ensure_fdflags() {
  if type -t fdflags >/dev/null 2>&1; then
    return 0
  fi
  local candidate
  for candidate in /usr/lib/bash/fdflags /usr/lib64/bash/fdflags; do
    if enable -f "$candidate" fdflags 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

# Keep flock fds out of exec'd children (e.g. sleep). Otherwise a SIGTERM to the
# monitor shell can leave an orphan child holding the lock with no pid file.
set_cloexec() {
  local fd=$1
  ensure_fdflags && fdflags -s +cloexec "$fd"
}

monitor_pid_alive() {
  local pid
  [[ -f $monitor_pid_file ]] || return 1
  pid=$(head -n 1 "$monitor_pid_file" 2>/dev/null || true)
  [[ $pid =~ ^[0-9]+$ ]] || return 1
  [[ -e /proc/$pid ]]
}

clear_stale_monitor_lock() {
  monitor_pid_alive && return 1
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "$monitor_lock_file" >/dev/null 2>&1 || true
  fi
  rm -f "$monitor_pid_file"
  return 0
}

claim_monitor() {
  exec {monitor_lock_fd}>"$monitor_lock_file"
  if flock -n "$monitor_lock_fd"; then
    set_cloexec "$monitor_lock_fd" || true
    printf '%s\n' "$$" >"$monitor_pid_file"
    return 0
  fi
  exec {monitor_lock_fd}>&-

  if monitor_pid_alive; then
    return 1
  fi

  printf 'Clearing a stale monitor lock for %s.\n' "$repo" >&2
  clear_stale_monitor_lock || return 1

  exec {monitor_lock_fd}>"$monitor_lock_file"
  flock -n "$monitor_lock_fd" || return 1
  set_cloexec "$monitor_lock_fd" || true
  printf '%s\n' "$$" >"$monitor_pid_file"
}

claim_dispatch() {
  exec {dispatch_lock_fd}>"$dispatch_lock_file"
  if ! flock -n "$dispatch_lock_fd"; then
    write_monitor_state dispatch_wait
    flock "$dispatch_lock_fd"
  fi
  set_cloexec "$dispatch_lock_fd" || true
}

release_dispatch() {
  exec {dispatch_lock_fd}>&-
  dispatch_lock_fd=''
}

cleanup_monitor() {
  rm -f "$monitor_pid_file"
  rm -f "$monitor_state_file"
  # Pre-cloexec leftovers: stop children that may still hold inherited lock fds.
  pkill -P $$ >/dev/null 2>&1 || true
}

monitor_snapshot() {
  local data
  local warned=0

  while true; do
    wait_for_github_quota
    if data=$(snapshot 5); then
      break
    fi
    printf 'GitHub status refresh failed; retrying in 30 seconds.\n' >&2
    if (( warned == 0 )); then
      desktop_notify \
        'CodeRabbit monitor waiting' \
        "$repo status could not be refreshed. Retrying automatically."
      warned=1
    fi
    sleep 30
  done
  printf '%s\n' "$data"
}
