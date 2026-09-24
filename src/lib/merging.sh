# SPDX-License-Identifier: GPL-3.0-only

merge_review_threads_clear() {
  local pr=$1 cursor='' page has_next query
  local -a args
  read -r -d '' query <<'GRAPHQL' || true
query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
  repository(owner:$owner,name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100,after:$cursor) {
        nodes { isResolved }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
GRAPHQL
  while true; do
    args=(api graphql -f query="$query" -F owner="$owner" -F name="$name" -F number="$pr")
    [[ -z $cursor ]] || args+=(-F cursor="$cursor")
    page=$(gh "${args[@]}") || return 1
    if ! jq -e '
      (.data.repository.pullRequest.reviewThreads.nodes | type == "array")
      and (.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage | type == "boolean")
      and (all(.data.repository.pullRequest.reviewThreads.nodes[]; .isResolved == true))
    ' <<<"$page" >/dev/null; then
      printf 'PR #%s has unresolved review threads or their status is unavailable.\n' "$pr" >&2
      return 1
    fi
    has_next=$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage' <<<"$page")
    [[ $has_next == true ]] || return 0
    cursor=$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor // empty' <<<"$page")
    [[ -n $cursor ]] || return 1
  done
}

merge_pr_now() {
  local pr=$1 expected_head=$2 method=$3 delete_branch=$4 bypass_approval=${5:-0} data
  data=$(gh pr view "$pr" --repo "$repo" \
    --json state,headRefOid,mergeable,mergeStateStatus,statusCheckRollup,reviews) || return 1
  if ! jq -e --arg head "$expected_head" '
    .state == "OPEN"
    and .headRefOid == $head
    and .mergeable == "MERGEABLE"
    and .mergeStateStatus == "CLEAN"
    and (.statusCheckRollup | type == "array")
    and (all(.statusCheckRollup[];
      if .__typename == "StatusContext" then .state == "SUCCESS"
      else .status == "COMPLETED"
        and (.conclusion | IN("SUCCESS", "NEUTRAL", "SKIPPED", "CANCELLED")) end))
    and (.reviews | type == "array")
    and ([.reviews[] | select(.commit.oid == $head and .state != "DISMISSED")]
      | group_by(.author.login)
      | all(.[]; .[-1].state != "CHANGES_REQUESTED"))
    and (all(.reviews[];
      .author.login != "coderabbitai"
      or .commit.oid != $head
      or .state == "DISMISSED"
      or ((.body // "") | test("Nitpick comments \\([1-9][0-9]*\\)"; "i") | not)))
  ' <<<"$data" >/dev/null; then
    printf 'PR #%s is not ready to merge: head, checks, or reviews are pending.\n' "$pr" >&2
    return 1
  fi
  merge_review_threads_clear "$pr" || return 1
  local -a args=(pr merge "$pr" --repo "$repo" "--$method")
  [[ $delete_branch == 1 ]] && args+=(--delete-branch)
  [[ $bypass_approval == 1 ]] && args+=(--admin)
  gh "${args[@]}"
}

route_merge_pr() {
  local pr=$1 head_sha=$2 method delete_branch=0 merge_admin=0 host
  method=$(merge_method)
  delete_branch_enabled && delete_branch=1
  merge_admin_enabled && merge_admin=1
  host=$(agent_host 2>/dev/null || true)
  if [[ -n $host ]]; then
    remote_agent_command --merge-now "$pr" "$head_sha" "$method" "$delete_branch" \
      "$merge_admin" --local-agents
  else
    merge_pr_now "$pr" "$head_sha" "$method" "$delete_branch" "$merge_admin"
  fi
}

merge_approved_reviews() {
  local state=$1 pr head_sha title
  auto_merge_enabled || return 0
  merge_after_approval_enabled || return 0
  while IFS=$'\t' read -r pr head_sha title; do
    [[ -n ${pr:-} ]] || continue
    if route_merge_pr "$pr" "$head_sha"; then
      desktop_notify 'CodeRabbit PR merged' "PR #$pr — $title"
    fi
  done < <(approved_merge_rows "$state")
}

archive_after_merge_enabled() {
  [[ -f $archive_after_merge_file ]] &&
    [[ $(head -n 1 "$archive_after_merge_file") == 1 ]]
}

route_archive_codex_thread() {
  local branch_name=$1 head_sha=$2 session_id=${3:-} host
  host=$(agent_host 2>/dev/null || true)
  if [[ -n $host ]]; then
    if [[ -n $session_id ]]; then
      remote_agent_command --archive-session "$session_id" --local-agents
    else
      remote_agent_command --archive-task "$branch_name" "$head_sha" --local-agents
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
  local temporary archive_lock_fd
  archive_after_merge_enabled || return 0
  [[ -s $pending_archives_file ]] || return 0
  exec {archive_lock_fd}>"$pending_archives_file.lock"
  flock "$archive_lock_fd"
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
    if [[ $pr_state == MERGED && $current_head == "$head_sha" ]]; then
      if route_archive_codex_thread "$branch_name" "$head_sha" "$session_id"; then
        printf '%s\n' "$marker" >>"$archived_heads_file"
        printf 'Archived the matching Codex task for merged PR #%s.\n' "$pr"
        continue
      fi
      printf '%s\t%s\t%s\t%s\t%s\n' "$pr" "$branch_name" "$head_sha" "$title" "$session_id" \
        >>"$temporary"
    fi
  done <"$pending_archives_file"
  mv "$temporary" "$pending_archives_file"
  exec {archive_lock_fd}>&-
}
