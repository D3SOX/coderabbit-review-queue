# SPDX-License-Identifier: GPL-3.0-only

merge_review_threads_clear() {
  local pr=$1 cursor='' page has_next query unresolved author path thread_id
  local -a args
  read -r -d '' query <<'GRAPHQL' || true
query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
  repository(owner:$owner,name:$name) {
    pullRequest(number:$number) {
      headRefOid
      reviews(last:100) {
        nodes { id state body submittedAt author { login } commit { oid } }
      }
      comments(last:100) {
        nodes { createdAt body author { __typename login } }
      }
      reviewThreads(first:100,after:$cursor) {
        nodes {
          id
          isResolved
          path
          comments(first:1) { nodes { author { login } } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
GRAPHQL
  while true; do
    args=(api graphql -f query="$query" -F owner="$owner" -F name="$name" -F number="$pr")
    [[ -z $cursor ]] || args+=(-F cursor="$cursor")
    if ! page=$(gh "${args[@]}"); then
      printf 'PR #%s review thread status is unavailable.\n' "$pr" >&2
      return 1
    fi
    if ! jq -e '
      (.data.repository.pullRequest.reviewThreads.nodes | type == "array")
      and (.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage | type == "boolean")
      and (all(.data.repository.pullRequest.reviewThreads.nodes[];
        (.isResolved | type) == "boolean"))
    ' <<<"$page" >/dev/null; then
      printf 'PR #%s review thread status is unavailable.\n' "$pr" >&2
      return 1
    fi
    if [[ -z $cursor ]] && jq -L "$script_dir/lib" -e '
      include "review-feedback";
      first(.data.repository.pullRequest | unanswered_body_feedback)
    ' <<<"$page" >/dev/null; then
      printf 'PR #%s has unanswered CodeRabbit review-body feedback on its head.\n' "$pr" >&2
      return 1
    fi
    unresolved=$(jq -r '
      .data.repository.pullRequest.reviewThreads.nodes[]
      | select(.isResolved == false)
      | [(.comments.nodes[0].author.login // "unknown author"),
         (.path // "unknown path"), (.id // "unknown thread")]
      | @tsv
    ' <<<"$page")
    if [[ -n $unresolved ]]; then
      while IFS=$'\t' read -r author path thread_id; do
        printf 'PR #%s has an unresolved review thread from %s at %s (%s).\n' \
          "$pr" "$author" "$path" "$thread_id" >&2
      done <<<"$unresolved"
      return 1
    fi
    has_next=$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage' <<<"$page")
    [[ $has_next == true ]] || return 0
    cursor=$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor // empty' <<<"$page")
    [[ -n $cursor ]] || return 1
  done
}

# A queued workflow run has no jobs yet, so its checks are missing from the
# PR's status rollup. Refuse to merge until every run for the head finished.
merge_workflow_runs_finished() {
  local pr=$1 head_sha=$2 runs pending
  if ! runs=$(gh api --paginate --slurp \
    "repos/$repo/actions/runs?head_sha=$head_sha&per_page=100") ||
    ! pending=$(jq -r '
      if all(.[]; .workflow_runs | type == "array") then
        [.[].workflow_runs[] | select(.status != "completed") | .name] | unique | join(", ")
      else error("unexpected response") end
    ' <<<"$runs" 2>/dev/null); then
    printf 'PR #%s workflow run status is unavailable.\n' "$pr" >&2
    return 1
  fi
  if [[ -n $pending ]]; then
    printf 'PR #%s is not ready to merge: workflow runs are pending: %s.\n' "$pr" "$pending" >&2
    return 1
  fi
}

# Exit status of merge_pr_now when the PR entered a merge queue instead.
merge_queued_status=43

merge_queue_status() {
  local pr=$1 data query
  read -r -d '' query <<'GRAPHQL' || true
query($owner:String!,$name:String!,$number:Int!) {
  repository(owner:$owner,name:$name) {
    pullRequest(number:$number) {
      state title headRefOid headRefName baseRefName isCrossRepository
      isMergeQueueEnabled isInMergeQueue
      autoMergeRequest { enabledAt }
      headRef { id target { oid } }
      timelineItems(last:1,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]) {
        nodes { ... on RemovedFromMergeQueueEvent { createdAt reason } }
      }
    }
  }
}
GRAPHQL
  data=$(gh api graphql -f query="$query" -F owner="$owner" -F name="$name" -F number="$pr") &&
    jq -ce '.data.repository.pullRequest | objects' <<<"$data"
}

merge_pr_now() {
  local pr=$1 expected_head=$2 method=$3 delete_branch=$4 bypass_approval=${5:-0} data attempt
  local queue_data queued=0 admin_guard=$bypass_approval
  if ! queue_data=$(merge_queue_status "$pr"); then
    printf 'PR #%s merge queue status is unavailable.\n' "$pr" >&2
    return 1
  fi
  if jq -e '.isInMergeQueue == true' <<<"$queue_data" >/dev/null; then
    printf 'PR #%s is already in the merge queue.\n' "$pr"
    start_merge_queue_watch "$pr" "$expected_head" "$method" "$delete_branch" "$bypass_approval"
    return "$merge_queued_status"
  fi
  # The queue runs its own checks before merging; `--admin` would skip them,
  # and the queue cannot bypass branch protection either.
  if jq -e '.isMergeQueueEnabled == true' <<<"$queue_data" >/dev/null; then
    queued=1
    admin_guard=0
  fi
  for attempt in 1 2 3; do
    data=$(gh pr view "$pr" --repo "$repo" \
      --json state,isCrossRepository,headRefOid,mergeable,mergeStateStatus,statusCheckRollup,reviews) || return 1
    if jq -e '.isCrossRepository == true' <<<"$data" >/dev/null; then
      printf 'PR #%s comes from a fork; merge guard refused.\n' "$pr" >&2
      return 1
    fi
    if jq -e --arg head "$expected_head" '
      .state == "OPEN" and .headRefOid == $head
      and (.mergeable == "CONFLICTING" or .mergeStateStatus == "DIRTY")
    ' <<<"$data" >/dev/null; then
      printf 'PR #%s has merge conflicts; merge guard refused.\n' "$pr" >&2
      return 42
    fi
    if jq -e -L "$script_dir/lib" --arg head "$expected_head" --argjson admin "$admin_guard" '
    include "latest-checks";
    .state == "OPEN"
    and .headRefOid == $head
    and .mergeable == "MERGEABLE"
    and (.mergeStateStatus == "CLEAN" or ($admin == 1 and .mergeStateStatus == "BLOCKED"))
    and (.statusCheckRollup | type == "array")
    and (.statusCheckRollup
      | latest_checks
      | all(.[];
          if .__typename == "StatusContext" then .state == "SUCCESS"
          else .status == "COMPLETED"
            and (.conclusion | IN("SUCCESS", "NEUTRAL", "SKIPPED")) end))
    and (.reviews | type == "array")
    # A later COMMENTED review must not hide an earlier change request.
    and ([.reviews[] | select(.commit.oid == $head
        and (.state | IN("APPROVED", "CHANGES_REQUESTED")))]
      | group_by(.author.login)
      | all(.[]; $admin == 1 or .[-1].state != "CHANGES_REQUESTED"))
  ' <<<"$data" >/dev/null; then
      break
    fi
    # The PR rollup can lag behind `gh pr checks` just after CI finishes.
    # Retry only pending states; never bypass a failed check or review guard.
    if (( attempt < 3 )) && jq -e -L "$script_dir/lib" '
      include "latest-checks";
      .mergeable == "UNKNOWN" or (.mergeStateStatus | IN("UNKNOWN", "UNSTABLE"))
      or ((.statusCheckRollup // []) | latest_checks
        | any(.[]; if .__typename == "StatusContext" then .state == "PENDING"
                   else .status != "COMPLETED" end))
    ' <<<"$data" >/dev/null; then
      sleep 3
      continue
    fi
    printf 'PR #%s is not ready to merge: head, checks, or reviews are pending.\n' "$pr" >&2
    (( queued == 0 || bypass_approval == 0 )) ||
      printf 'Its base branch has a merge queue, so the admin bypass does not apply.\n' >&2
    jq -c -L "$script_dir/lib" --arg head "$expected_head" '
      include "latest-checks";
      ((.statusCheckRollup // []) | latest_checks) as $checks
      | {headMatches: (.headRefOid == $head), state, mergeable, mergeStateStatus,
       pendingChecks: [$checks[] | select(
         if .__typename == "StatusContext" then .state == "PENDING"
         else .status != "COMPLETED" end) | (.name // .context // "unknown")],
       failedChecks: [$checks[] | select(
         if .__typename == "StatusContext" then .state == "FAILURE"
         else .status == "COMPLETED" and
           ((.conclusion | IN("SUCCESS", "NEUTRAL", "SKIPPED")) | not) end)
         | (.name // .context // "unknown")],
       currentHeadChangeRequests: [.reviews[]? | select(
         .commit.oid == $head and .state == "CHANGES_REQUESTED")
         | .author.login]}
    ' <<<"$data" >&2
    return 1
  done
  merge_workflow_runs_finished "$pr" "$expected_head" || return 1
  merge_review_threads_clear "$pr" || return 1
  if (( queued )); then
    queue_pr_for_merge "$pr" "$expected_head" "$method" "$delete_branch" "$bypass_approval"
    return
  fi
  # Merge only the head that passed the guard, even if a push lands meanwhile.
  local -a args=(pr merge "$pr" --repo "$repo" "--$method" --match-head-commit "$expected_head")
  [[ $delete_branch == 1 ]] && args+=(--delete-branch)
  [[ $bypass_approval == 1 ]] && args+=(--admin)
  gh "${args[@]}"
}

# The queue's merge method applies. Branch deletion and other post-merge work
# wait for the watcher to see GitHub report the PR merged.
queue_pr_for_merge() {
  local pr=$1 expected_head=$2 method=$3 delete_branch=$4 admin=$5 data
  gh pr merge "$pr" --repo "$repo" --match-head-commit "$expected_head" || return 1
  if ! data=$(merge_queue_status "$pr"); then
    printf 'PR #%s merge queue status is unavailable after queuing.\n' "$pr" >&2
    return 1
  fi
  if jq -e '.isInMergeQueue == true' <<<"$data" >/dev/null; then
    printf 'PR #%s was added to the merge queue. GitHub merges it after the queue checks pass; this is not a failure, so stop here. The branch is deleted after the merge if enabled, and the agent task gets a message if the PR leaves the queue without merging.\n' "$pr"
    start_merge_queue_watch "$pr" "$expected_head" "$method" "$delete_branch" "$admin"
    return "$merge_queued_status"
  fi
  if jq -e '.state == "MERGED"' <<<"$data" >/dev/null; then
    return 0
  fi
  # gh enables auto-merge when the PR is not ready for the queue.
  if jq -e '.autoMergeRequest != null' <<<"$data" >/dev/null; then
    gh pr merge "$pr" --repo "$repo" --disable-auto >/dev/null || true
    printf 'PR #%s was not ready for the merge queue; GitHub auto-merge was disabled again.\n' "$pr" >&2
    return 1
  fi
  printf 'PR #%s did not enter the merge queue.\n' "$pr" >&2
  return 1
}

# Watch from the host that queued the PR: its agents can fix a failed queue run.
start_merge_queue_watch() {
  local -a launcher=()
  local log="$state_root/$repo_key-merge-queue.log"
  # Leave the calling agent's cgroup so that its exit does not stop the watch.
  if command -v systemd-run >/dev/null 2>&1 &&
    systemctl --user show-environment >/dev/null 2>&1; then
    launcher=(systemd-run --user --scope --quiet --collect --)
  fi
  setsid -f "${launcher[@]}" "$script_path" --repo "$repo" --watch-merge-queue "$@" \
    </dev/null >>"$log" 2>&1 || true
}

merge_queue_poll_seconds=60
merge_queue_watch_seconds=86400
# Someone may remove a PR on purpose, e.g. to requeue a stuck entry.
merge_queue_manual_grace_seconds=600

watch_merge_queue() {
  local pr=$1 head=$2 method=$3 delete_branch=$4 admin=$5
  local lock_fd data now deadline out_since=0 reason status
  exec {lock_fd}>"$runtime_root/coderabbit-review-queue-$repo_key-merge-queue-$pr.lock"
  flock -n "$lock_fd" || return 0
  set_cloexec "$lock_fd" || true
  printf '%s Watching PR #%s in the merge queue.\n' "$(date -Is)" "$pr"
  deadline=$(( $(date +%s) + merge_queue_watch_seconds ))
  while (( $(date +%s) < deadline )); do
    if data=$(merge_queue_status "$pr"); then
      case $(jq -r '.state' <<<"$data") in
        MERGED)
          finish_queued_merge "$pr" "$data" "$delete_branch"
          return 0
          ;;
        CLOSED)
          printf '%s PR #%s was closed without merging.\n' "$(date -Is)" "$pr"
          return 0
          ;;
      esac
      if jq -e '.isInMergeQueue == true' <<<"$data" >/dev/null; then
        out_since=0
      else
        now=$(date +%s)
        reason=$(jq -r '.timelineItems.nodes[0].reason // "unknown" | ascii_downcase' <<<"$data")
        # GitHub removes the entry just before it reports the merge.
        if [[ $reason != merged ]]; then
          (( out_since > 0 )) || out_since=$now
          if [[ $reason == manual ]]; then
            if (( now - out_since >= merge_queue_manual_grace_seconds )); then
              printf '%s PR #%s was removed from the merge queue manually.\n' "$(date -Is)" "$pr"
              return 0
            fi
          # Confirm on the next poll that the PR did not merge meanwhile.
          elif (( now > out_since )); then
            status=0
            steer_merge_queue_failure "$pr" "$head" "$method" "$delete_branch" "$admin" "$data" ||
              status=$?
            (( status == 75 )) || return "$status"
          fi
        fi
      fi
    fi
    sleep "$merge_queue_poll_seconds"
  done
  printf '%s Stopped watching PR #%s in the merge queue.\n' "$(date -Is)" "$pr"
}

finish_queued_merge() {
  local pr=$1 data=$2 delete_branch=$3 title ref_id
  title=$(jq -r '.title' <<<"$data")
  printf '%s The merge queue merged PR #%s.\n' "$(date -Is)" "$pr"
  # Keep a branch that gained commits after the merged head.
  ref_id=$(jq -r 'select(.isCrossRepository != true and .headRef.target.oid == .headRefOid)
    | .headRef.id // empty' <<<"$data")
  if [[ $delete_branch == 1 && -n $ref_id ]]; then
    gh api graphql -f query='mutation($id:ID!){deleteRef(input:{refId:$id}){clientMutationId}}' \
      -F id="$ref_id" >/dev/null ||
      printf 'Could not delete the branch of merged PR #%s.\n' "$pr" >&2
  fi
  desktop_notify 'CodeRabbit PR merged' "PR #$pr — $title"
}

steer_merge_queue_failure() {
  local pr=$1 head=$2 method=$3 delete_branch=$4 admin=$5 data=$6
  local title branch base reason removed_at runs token prompt status=0
  IFS=$'\t' read -r title branch base reason removed_at < <(jq -r '[.title, .headRefName,
    .baseRefName, (.timelineItems.nodes[0].reason // "unknown"),
    (.timelineItems.nodes[0].createdAt // "")] | @tsv' <<<"$data")
  token="merge-queue:$pr:$head:$removed_at"
  if [[ -f $routed_threads_file ]] && rg -qxF "$token" "$routed_threads_file"; then
    return 0
  fi
  runs=$(gh run list --repo "$repo" --event merge_group --limit 100 \
    --json databaseId,headBranch,conclusion,workflowName,url 2>/dev/null |
    jq -r --arg prefix "gh-readonly-queue/$base/pr-$pr-" '
      [.[] | select((.headBranch | startswith($prefix))
        and (.conclusion | IN("failure", "timed_out", "startup_failure")))][:3][]
      | "\(.workflowName) (\(.conclusion)): \(.url), run ID \(.databaseId)"' 2>/dev/null || true)
  prompt="PR #$pr ($title), https://github.com/$repo/pull/$pr, left the merge queue without merging (GitHub reason: $reason). The queue runs its checks on a temporary merge_group branch that combines the PR with $base, so they can fail even though the PR's own checks passed."
  if [[ -n $runs ]]; then
    prompt+=" Failed queue runs:"$'\n'"$runs"$'\n'"Read their failed logs with \`gh run view <run ID> --repo $repo --log-failed\`."
  else
    prompt+=" Find the failed run with \`gh run list --repo $repo --event merge_group\`; its branch starts with gh-readonly-queue/$base/pr-$pr- or with the branch of a PR queued in the same group."
  fi
  prompt+=" Fetch the current PR and stop if it is closed or merged. Find the cause and fix it in the PR branch, preserving unrelated work. Prove the fix by running the failing checks locally, or explain why they cannot run here. Commit and push the fix, wait for CI and all review bots, and resolve actionable review threads. Do not trigger CodeRabbit yourself. Then run the merge guard on this host again with the new live head SHA:"
  prompt+=" \`$(installed_command) --repo $repo --merge-now $pr \"\$(gh pr view $pr --repo $repo --json headRefOid --jq .headRefOid)\" $method $delete_branch $admin --local-agents\`. If the failure is unrelated to this PR, report that instead of changing code. Never bypass the guard; report any remaining blocker."
  local -a ids=("$token")
  steer_pr_agent "$pr" "$title" "$branch" "$head" "$prompt" ids 'merge queue failure' || status=$?
  if (( status == 1 )); then
    desktop_notify 'CodeRabbit merge queue failed' \
      "PR #$pr — $title"$'\n'"It left the merge queue without merging, and no agent task could take the fix." \
      'critical'
  fi
  return "$status"
}

# Returns 0 once the prompt reached the PR's agent, 75 to retry later, and 1
# when no matching agent task exists.
steer_pr_agent() {
  local pr=$1 title=$2 branch=$3 head=$4 prompt=$5 ids_name=$6 reason=$7
  local -n token_ids=$6
  local match session_id session_cwd found=0 agent
  local -a agents=(codex claude)
  ! agent_prefers_claude "$branch" || agents=(claude codex)
  for agent in "${agents[@]}"; do
    if [[ $agent == codex ]]; then
      match=$(matching_codex_session "$branch" "$head") || continue
      found=1
      IFS=$'\t' read -r session_id session_cwd <<<"$match"
      codex_can_receive_review "$session_id" "$(codex_session_state "$session_id")" || continue
      resume_codex_session "$pr" "$title" "$head" "$session_id" "$session_cwd" "$prompt" \
        "$ids_name" "$reason" || continue
    else
      match=$(matching_claude_session "$branch" "$head") || continue
      found=1
      IFS=$'\t' read -r session_id session_cwd <<<"$match"
      claude_can_receive_review "$session_id" "$(claude_session_state "$session_id")" || continue
      resume_claude_session "$pr" "$title" "$session_id" "$session_cwd" "$prompt" \
        "$ids_name" "$head" "$reason" || continue
    fi
    # Resuming can also defer when the task started a turn meanwhile.
    if [[ -f $routed_threads_file ]] && rg -qxF "${token_ids[0]}" "$routed_threads_file"; then
      return 0
    fi
  done
  (( found )) && return 75
  return 1
}

route_merge_pr() {
  local pr=$1 head_sha=$2 method delete_branch=0 merge_admin=0 host status=0
  method=$(merge_method)
  delete_branch_enabled && delete_branch=1
  merge_admin_enabled && merge_admin=1
  host=$(agent_host 2>/dev/null || true)
  if [[ -n $host ]]; then
    remote_agent_command --merge-now "$pr" "$head_sha" "$method" "$delete_branch" \
      "$merge_admin" --local-agents || status=$?
  else
    merge_pr_now "$pr" "$head_sha" "$method" "$delete_branch" "$merge_admin" || status=$?
  fi
  if (( status == 42 )) && resolve_merge_conflicts_enabled; then
    if [[ -n $host ]]; then
      remote_agent_command --resolve-merge-conflicts "$pr" "$head_sha" "$method" \
        "$delete_branch" "$merge_admin" --local-agents || true
    else
      resolve_merge_conflicts "$pr" "$head_sha" "$method" "$delete_branch" "$merge_admin" || true
    fi
  fi
  return "$status"
}

resolve_merge_conflicts() {
  local pr=$1 expected_head=$2 method=$3 delete_branch=$4 admin=$5
  local data branch title head base match session_id session_cwd token prompt
  data=$(gh pr view "$pr" --repo "$repo" \
    --json state,headRefOid,baseRefOid,headRefName,title,mergeable,mergeStateStatus) || return 1
  jq -e --arg head "$expected_head" '
    .state == "OPEN" and .headRefOid == $head
    and (.mergeable == "CONFLICTING" or .mergeStateStatus == "DIRTY")
  ' <<<"$data" >/dev/null || return 1
  IFS=$'\t' read -r branch title head base < <(
    jq -r '[.headRefName,.title,.headRefOid,.baseRefOid] | @tsv' <<<"$data")
  [[ -n $base && $base != null ]] || return 1
  token="merge-conflict:$pr:$head:$base"
  if [[ -f $routed_threads_file ]] && rg -qxF "$token" "$routed_threads_file"; then
    return 0
  fi
  match=$(matching_codex_session "$branch" "$head") || {
    printf 'No matching Codex task for conflict repair on PR #%s; retrying later.\n' "$pr" >&2
    return 1
  }
  IFS=$'\t' read -r session_id session_cwd <<<"$match"
  if [[ $(codex_session_state "$session_id") != idle ]]; then
    printf 'Matching Codex task for PR #%s is not verifiably idle; deferring conflict repair.\n' "$pr"
    return 1
  fi
  prompt="PR #$pr ($title), https://github.com/$repo/pull/$pr, could not merge because it has merge conflicts. Fetch the current PR and base branch. Stop if closed or merged. Resolve the conflicts in the PR branch, preserve unrelated work, validate and push the resolution. Wait for CI and all review bots to finish and resolve all actionable review threads. Do not trigger CodeRabbit yourself. Then rerun the merge guard on this host with the new live head SHA:"
  prompt+=" \`$(installed_command) --repo $repo --merge-now $pr \"\$(gh pr view $pr --repo $repo --json headRefOid --jq .headRefOid)\" $method $delete_branch $admin --local-agents\`. Never bypass the guard; report any remaining blocker."
  local -a conflict_ids=("$token")
  resume_codex_session "$pr" "$title" "$head" "$session_id" "$session_cwd" "$prompt" conflict_ids 'merge conflict resolution'
}

merge_approved_reviews() {
  local state=$1 pr head_sha branch_name title
  auto_merge_enabled || return 0
  merge_after_approval_enabled || return 0
  while IFS=$'\t' read -r pr head_sha branch_name title; do
    [[ -n ${pr:-} ]] || continue
    # A running agent may still push or answer reviews; merge once it stops.
    if [[ $(agent_task_progress "$branch_name" "$head_sha") =~ ^(Claude|Codex)\ Running ]]; then
      printf 'PR #%s is approved, but its agent task is still running; merging later.\n' "$pr"
      continue
    fi
    if route_merge_pr "$pr" "$head_sha"; then
      desktop_notify 'CodeRabbit PR merged' "PR #$pr — $title"
    fi
  done < <(approved_merge_rows "$state")
}

archive_after_merge_enabled() {
  [[ -f $archive_after_merge_file ]] &&
    [[ $(head -n 1 "$archive_after_merge_file") == 1 ]]
}

process_thread_archives_async() {
  local state=$1
  archive_after_merge_enabled || return 0
  (
    trap - EXIT
    close_inherited_locks
    exec {archive_scan_fd}>"$pending_archives_file.scan.lock"
    flock -n "$archive_scan_fd" || exit 0
    set_cloexec "$archive_scan_fd" || true
    process_pending_thread_archives "$state"
  ) &
}

process_approval_actions_async() {
  local state=${1:-}
  (
    trap - EXIT
    close_inherited_locks
    exec {approval_scan_fd}>"$state_root/$repo_key-approval-actions.lock"
    flock -n "$approval_scan_fd" || exit 0
    set_cloexec "$approval_scan_fd" || true
    # Quota waits have no current snapshot. Fetch inside the locked worker so
    # slow GitHub/agent lookups cannot block the review timer or overlap scans.
    [[ -n $state ]] || state=$(monitor_snapshot) || exit 1
    # Reviews triggered outside this queue can finish while quota is exhausted.
    # Route their feedback from this same snapshot, not only at dispatch time.
    if auto_delegation_enabled; then
      route_all_unresolved_async "$state"
    fi
    # Capture the task before a merge can delete its branch. Lookup and merge
    # run independently of both the review queue and older archive attempts.
    queue_approved_thread_archives "$state"
    merge_approved_reviews "$state"
  ) &
}

route_archive_codex_thread() {
  local branch_name=$1 head_sha=$2 session_id=${3:-} host
  host=$(agent_host 2>/dev/null || true)
  if [[ -n $host ]]; then
    if [[ -n $session_id ]]; then
      remote_command_timeout=60 remote_agent_command --archive-session "$session_id" --local-agents
    else
      remote_command_timeout=60 remote_agent_command --archive-task "$branch_name" "$head_sha" --local-agents
    fi
  else
    if [[ -n $session_id ]]; then
      archive_codex_session_if_idle "$session_id"
    else
      archive_matching_codex_thread "$branch_name" "$head_sha"
    fi
  fi
}

agent_codex_session_id() {
  local branch_name=$1 head_sha=$2 host
  host=$(agent_host 2>/dev/null || true)
  if [[ -n $host ]]; then
    remote_command_timeout=30 remote_agent_command \
      --task-id "$branch_name" "$head_sha" --local-agents 2>/dev/null
  else
    find_codex_session_id "$branch_name" "$head_sha"
  fi
}

queue_pending_archive() {
  local pr=$1 branch_name=$2 head_sha=$3 title=$4 session_id=${5:-} marker archive_lock_fd temporary
  archive_after_merge_enabled || return 0
  marker="$pr"$'\t'"$head_sha"
  if [[ -f $archived_heads_file ]] &&
    rg -q --fixed-strings --line-regexp "$marker" "$archived_heads_file"; then
    return 0
  fi
  exec {archive_lock_fd}>"$pending_archives_file.lock"
  flock "$archive_lock_fd"
  if [[ -f $pending_archives_file ]] &&
    awk -F '\t' -v pr="$pr" -v head="$head_sha" \
      '$1 == pr && $3 == head { found=1 } END { exit !found }' \
      "$pending_archives_file"; then
    if [[ -n $session_id ]]; then
      temporary=$(mktemp "$pending_archives_file.XXXXXX")
      awk -F '\t' -v OFS='\t' -v pr="$pr" -v head="$head_sha" -v id="$session_id" \
        '$1 == pr && $3 == head && $5 == "" { $5=id } { print }' \
        "$pending_archives_file" >"$temporary"
      mv "$temporary" "$pending_archives_file"
    fi
  else
    title=${title//$'\t'/ }
    title=${title//$'\n'/ }
    printf '%s\t%s\t%s\t%s\t%s\n' \
      "$pr" "$branch_name" "$head_sha" "$title" "$session_id" \
      >>"$pending_archives_file"
  fi
  exec {archive_lock_fd}>&-
}

queue_approved_thread_archives() {
  local state=$1 pr branch_name head_sha title session_id
  archive_after_merge_enabled || return 0
  while IFS=$'\t' read -r pr branch_name head_sha title; do
    [[ -n ${pr:-} ]] || continue
    if [[ -f $pending_archives_file ]] &&
      awk -F '\t' -v pr="$pr" -v head="$head_sha" \
        '$1 == pr && $3 == head { found=1 } END { exit !found }' \
        "$pending_archives_file"; then
      continue
    fi
    session_id=$(agent_codex_session_id "$branch_name" "$head_sha" || true)
    queue_pending_archive "$pr" "$branch_name" "$head_sha" "$title" "$session_id"
  done < <(approved_archive_rows "$state")
}

process_pending_thread_archives() {
  local state=$1 pr branch_name head_sha title session_id marker data pr_state current_head
  local temporary archive_lock_fd scanned reconciled
  archive_after_merge_enabled || return 0
  [[ -s $pending_archives_file ]] || return 0
  exec {archive_lock_fd}>"$pending_archives_file.lock"
  flock "$archive_lock_fd"
  scanned=$(mktemp "$pending_archives_file.XXXXXX")
  cp "$pending_archives_file" "$scanned"
  exec {archive_lock_fd}>&-
  temporary=$(mktemp "$pending_archives_file.XXXXXX")
  while IFS=$'\t' read -r pr branch_name head_sha title session_id; do
    [[ -n ${pr:-} ]] || continue
    marker="$pr"$'\t'"$head_sha"
    if [[ -f $archived_heads_file ]] &&
      rg -q --fixed-strings --line-regexp "$marker" "$archived_heads_file"; then
      continue
    fi
    if jq -e --argjson pr "$pr" --arg head "$head_sha" '
      any(.data.repository.pullRequests.nodes[]?;
        .number == $pr and .headRefOid == $head)
    ' <<<"$state" >/dev/null; then
      printf '%s\t%s\t%s\t%s\t%s\n' "$pr" "$branch_name" "$head_sha" "$title" "$session_id" \
        >>"$temporary"
      continue
    fi
    if ! data=$(gh pr view "$pr" --repo "$repo" --json state,headRefOid 2>/dev/null); then
      printf '%s\t%s\t%s\t%s\t%s\n' "$pr" "$branch_name" "$head_sha" "$title" "$session_id" \
        >>"$temporary"
      continue
    fi
    pr_state=$(jq -r '.state' <<<"$data")
    current_head=$(jq -r '.headRefOid' <<<"$data")
    if [[ $pr_state == OPEN ]]; then
      printf '%s\t%s\t%s\t%s\t%s\n' "$pr" "$branch_name" "$head_sha" "$title" "$session_id" \
        >>"$temporary"
      continue
    fi
    # An agent may push fixes and merge without another review, so the merged
    # head can differ from the one recorded at delegation.
    if [[ $pr_state == MERGED ]]; then
      if route_archive_codex_thread "$branch_name" "$current_head" "$session_id"; then
        printf '%s\n' "$marker" >>"$archived_heads_file"
        printf 'Archived the matching Codex task for merged PR #%s.\n' "$pr"
        continue
      fi
      printf '%s\t%s\t%s\t%s\t%s\n' "$pr" "$branch_name" "$head_sha" "$title" "$session_id" \
        >>"$temporary"
    fi
  done <"$scanned"
  # Remote lookup must not hold the file lock: delegation can enqueue another
  # archive while this scan runs. Reconcile with the latest file under lock.
  exec {archive_lock_fd}>"$pending_archives_file.lock"
  flock "$archive_lock_fd"
  reconciled=$(mktemp "$pending_archives_file.XXXXXX")
  awk -F '\t' '
    FILENAME == ARGV[1] { scanned[$1 FS $3]=1; next }
    FILENAME == ARGV[2] { retained[$1 FS $3]=1; next }
    !(($1 FS $3) in scanned) || (($1 FS $3) in retained)
  ' "$scanned" "$temporary" "$pending_archives_file" >"$reconciled"
  mv "$reconciled" "$pending_archives_file"
  exec {archive_lock_fd}>&-
  rm -f "$scanned" "$temporary"
}
