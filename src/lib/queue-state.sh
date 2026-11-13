# SPDX-License-Identifier: GPL-3.0-only

status_quota_available() {
  local row name remaining reset

  row=$(github_low_quota) || return 0
  [[ -n $row ]] || return 0
  IFS=$'\t' read -r name remaining reset <<<"$row"
  printf 'GITHUB_QUOTA_LOW\t%s\t%s\t%s\n' \
    "$reset" "$name" "$remaining" >&2
  return 75
}

read -r -d '' query <<'GRAPHQL' || true
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 50, after: $endCursor, states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        title
        isDraft
        isCrossRepository
        author {
          login
        }
        updatedAt
        headRefOid
        headRefName
        commits(last: 1) {
          nodes {
            commit {
              committedDate
              statusCheckRollup {
                contexts(first: 100) {
                  nodes {
                    ... on StatusContext {
                      context
                      state
                      description
                      createdAt
                      updatedAt
                      creator {
                        login
                      }
                    }
                    ... on CheckRun {
                      databaseId
                      name
                      status
                      conclusion
                      startedAt
                      completedAt
                      title
                      summary
                      checkSuite { app { slug } workflowRun { workflow { name } } }
                    }
                  }
                }
              }
            }
          }
        }
        comments(last: 50) {
          nodes {
            author {
              login
            }
            body
            createdAt
            updatedAt
          }
        }
        reviews(last: 50) {
          nodes {
            author {
              login
            }
            body
            state
            commit {
              oid
            }
            submittedAt
          }
        }
      }
    }
  }
}
GRAPHQL

snapshot_remote() {
  local pages
  pages=$(gh api graphql --paginate --slurp \
    -f query="$query" \
    -F owner="$owner" \
    -F name="$name") || return 1
  jq -c '{data: {repository: {pullRequests: {nodes: [.[].data.repository.pullRequests.nodes[]]}}}}' \
    <<<"$pages"
}

snapshot() {
  local max_age=${1:-10}
  local now modified temporary snapshot_lock_fd

  exec {snapshot_lock_fd}>"$snapshot_cache_lock_file"
  flock "$snapshot_lock_fd"
  if [[ -f $snapshot_cache_file ]]; then
    now=$(date +%s)
    modified=$(stat -c %Y "$snapshot_cache_file" 2>/dev/null || printf '0')
    if (( max_age > 0 && now - modified <= max_age )); then
      cat "$snapshot_cache_file"
      exec {snapshot_lock_fd}>&-
      return 0
    fi
  fi
  temporary=$(mktemp "$snapshot_cache_file.XXXXXX")
  if snapshot_remote >"$temporary"; then
    mv "$temporary" "$snapshot_cache_file"
    cat "$snapshot_cache_file"
    exec {snapshot_lock_fd}>&-
    return 0
  fi
  rm -f "$temporary"
  exec {snapshot_lock_fd}>&-
  return 1
}

# Every per-repository file name after "$repo_key-"; see configure_repo.
repo_state_suffixes='agent-host|approval-actions\.lock|archive-after-merge|archived-heads\.tsv|auto-delegate|auto-merge|check-quota-now|delegated-prs\.txt|delegation-prompt-mode|delegation-prompt-template|delete-branch|excluded-authors|excluded-branches|ignore-drafts|merge-admin|merge-after-approval|merge-after-delegation|merge-method|monitor-host|monitor\.log|monitor-state\.tsv|new-items-at-top|notifications|notify-sound|notify-sound-path|notify-sound-volume|order\.txt|pending-archives\.tsv|quota-expiry\.txt|requeued-heads\.tsv|requeued-items-at-top|resolve-merge-conflicts|review-completion\.tsv|review-history\.tsv|review-requests\.tsv|routed-threads\.txt|routing-failure-[0-9]+|snapshot\.json|snapshot\.lock|status\.txt|steer-running-agents|stop-when-empty'

configure_repo() {
  local selected=$1

  if [[ ! $selected =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    printf 'Repository must be in OWNER/NAME format: %s\n' "$selected" >&2
    return 2
  fi

  repo=$selected
  repo_key=${repo//\//__}
  mkdir -p "$state_root"
  # GitHub names are case-insensitive. Reuse the casing that already owns this
  # repository's settings so that differently cased input cannot start a second
  # monitor with its own locks and defaults. Accept only known file names: the
  # glob also matches a longer sibling repository such as NAME-pro.
  local existing
  existing=$(
    shopt -s nullglob nocaseglob
    for file in "$state_root/$repo_key"-*; do
      file=${file##*/}
      [[ ${file:${#repo_key}+1} =~ ^($repo_state_suffixes)$ ]] || continue
      printf '%s\n' "${file:0:${#repo_key}}"
    done | sort -u
  )
  if [[ -n $existing ]] && ! grep -qxF -- "$repo_key" <<<"$existing" &&
    (( $(wc -l <<<"$existing") == 1 )); then
    repo_key=$existing
    repo="${repo_key%%__*}/${repo_key#*__}"
  fi
  owner=${repo%%/*}
  name=${repo#*/}
  monitor_pid_file="$runtime_root/coderabbit-review-queue-$repo_key.pid"
  monitor_lock_file="$runtime_root/coderabbit-review-queue-$repo_key.lock"
  routed_threads_file="$state_root/$repo_key-routed-threads.txt"
  queue_order_file="$state_root/$repo_key-order.txt"
  quota_expiry_file="$state_root/$repo_key-quota-expiry.txt"
  review_requests_file="$state_root/$repo_key-review-requests.tsv"
  review_history_file="$state_root/$repo_key-review-history.tsv"
  check_quota_now_file="$state_root/$repo_key-check-quota-now"
  auto_delegate_file="$state_root/$repo_key-auto-delegate"
  stop_when_empty_file="$state_root/$repo_key-stop-when-empty"
  ignore_drafts_file="$state_root/$repo_key-ignore-drafts"
  excluded_branches_file="$state_root/$repo_key-excluded-branches"
  excluded_authors_file="$state_root/$repo_key-excluded-authors"
  notifications_file="$state_root/$repo_key-notifications"
  notify_sound_file="$state_root/$repo_key-notify-sound"
  notify_sound_path_file="$state_root/$repo_key-notify-sound-path"
  notify_sound_volume_file="$state_root/$repo_key-notify-sound-volume"
  new_items_at_top_file="$state_root/$repo_key-new-items-at-top"
  requeued_items_at_top_file="$state_root/$repo_key-requeued-items-at-top"
  agent_host_file="$state_root/$repo_key-agent-host"
  monitor_host_file="$state_root/$repo_key-monitor-host"
  delegated_prs_file="$state_root/$repo_key-delegated-prs.txt"
  requeued_heads_file="$state_root/$repo_key-requeued-heads.tsv"
  delegation_prompt_mode_file="$state_root/$repo_key-delegation-prompt-mode"
  delegation_prompt_template_file="$state_root/$repo_key-delegation-prompt-template"
  steer_running_agents_file="$state_root/$repo_key-steer-running-agents"
  auto_merge_file="$state_root/$repo_key-auto-merge"
  merge_method_file="$state_root/$repo_key-merge-method"
  delete_branch_file="$state_root/$repo_key-delete-branch"
  merge_after_delegation_file="$state_root/$repo_key-merge-after-delegation"
  merge_after_approval_file="$state_root/$repo_key-merge-after-approval"
  merge_admin_file="$state_root/$repo_key-merge-admin"
  resolve_merge_conflicts_file="$state_root/$repo_key-resolve-merge-conflicts"
  archive_after_merge_file="$state_root/$repo_key-archive-after-merge"
  archived_heads_file="$state_root/$repo_key-archived-heads.tsv"
  pending_archives_file="$state_root/$repo_key-pending-archives.tsv"
  monitor_state_file="$state_root/$repo_key-monitor-state.tsv"
  review_completion_file="$state_root/$repo_key-review-completion.tsv"
  snapshot_cache_file="$state_root/$repo_key-snapshot.json"
  snapshot_cache_lock_file="$state_root/$repo_key-snapshot.lock"
}

agent_host() {
  local host=''
  if (( force_local_agents )); then
    printf '\n'
    return 0
  fi
  if [[ -f $agent_host_file ]]; then
    host=$(head -n 1 "$agent_host_file")
  fi
  [[ -z $host || $host =~ ^[A-Za-z0-9_.@-]+$ ]] || return 1
  printf '%s\n' "$host"
}

remote_agent_command() {
  local host
  host=$(agent_host) || {
    printf 'Invalid SSH agent host configured for %s.\n' "$repo" >&2
    return 2
  }
  [[ -n $host ]] || return 2
  ssh_queue_command "$host" "$@"
}

mark_pr_delegated() {
  local pr=$1
  if [[ ! -f $delegated_prs_file ]] || \
    ! rg -q --fixed-strings --line-regexp "$pr" "$delegated_prs_file"; then
    printf '%s\n' "$pr" >>"$delegated_prs_file"
  fi
}

write_monitor_state() {
  local phase=$1 pr=${2:-} title=${3:-} expiry=${4:-0} temporary
  title=${title//$'\t'/ }
  title=${title//$'\n'/ }
  temporary=$(mktemp "$monitor_state_file.XXXXXX")
  printf '%s\t%s\t%s\t%s\n' "$phase" "$pr" "$title" "$expiry" >"$temporary"
  mv "$temporary" "$monitor_state_file"
}

write_review_completion() {
  local pr=$1 head_sha=$2 title=$3 temporary
  title=${title//$'\t'/ }
  title=${title//$'\n'/ }
  temporary=$(mktemp "$review_completion_file.XXXXXX")
  printf '%s\t%s\t%s\n' "$pr" "$head_sha" "$title" >"$temporary"
  mv "$temporary" "$review_completion_file"
}

ignore_drafts_enabled() {
  # Default on: missing file or anything other than 0.
  if [[ -f $ignore_drafts_file ]]; then
    [[ $(head -n 1 "$ignore_drafts_file") != 0 ]]
  else
    return 0
  fi
}

new_items_at_top_enabled() {
  # Default on: missing file or anything other than 0.
  if [[ -f $new_items_at_top_file ]]; then
    [[ $(head -n 1 "$new_items_at_top_file") != 0 ]]
  else
    return 0
  fi
}

requeued_items_at_top_enabled() {
  if [[ -f $requeued_items_at_top_file ]]; then
    [[ $(head -n 1 "$requeued_items_at_top_file") != 0 ]]
  else
    # Retain the existing queue placement until the new setting is saved.
    new_items_at_top_enabled
  fi
}

ignore_drafts_json() {
  if ignore_drafts_enabled; then
    printf 'true\n'
  else
    printf 'false\n'
  fi
}

# One branch name per line; blank lines and # comments are ignored.
excluded_branches_json() {
  if [[ -f $excluded_branches_file ]]; then
    jq -R -s '
      split("\n")
      | map(gsub("^\\s+|\\s+$"; ""))
      | map(select(length > 0 and (startswith("#") | not)))
    ' <"$excluded_branches_file"
  else
    printf '[]\n'
  fi
}

# Accept GraphQL logins, REST bot logins, and gh's app/name notation.
normalize_authors() {
  jq -R -s '
    split("\n") | map(gsub("^\\s+|\\s+$"; ""))
    | map(select(length > 0 and (startswith("#") | not)))
    | map(ascii_downcase | sub("^@"; "") | sub("^app/"; "") | sub("\\[bot\\]$"; ""))
    | unique
  '
}

excluded_authors_json() {
  if [[ -f $excluded_authors_file ]]; then
    normalize_authors <"$excluded_authors_file"
  else
    printf '["pull", "dependabot"]\n'
  fi
}

eligible_state() {
  jq -L "$script_dir/lib" --argjson ignoreDrafts "$(ignore_drafts_json)" \
    --argjson excludedBranches "$(excluded_branches_json)" \
    --argjson excludedAuthors "$(excluded_authors_json)" '
    include "coderabbit-checks";
    .data.repository.pullRequests.nodes |= map(
      select($ignoreDrafts == false or (.isDraft | not))
      | select(.headRefName as $b | ($excludedBranches | index($b) | not))
      | select((.author.login // "" | ascii_downcase) as $a
          | ($excludedAuthors | index($a) | not))
      | (.commits.nodes[]?.commit.statusCheckRollup.contexts.nodes[]? |= coderabbit_check_context)
    )
  '
}

# This status belongs to the current head. A new head can be queued again.
without_file_limit_skips() {
  jq '
    .data.repository.pullRequests.nodes |= map(select(
      any(.commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]?;
        (.context // "") == "CodeRabbit"
        and (.creator.login // "") == "coderabbitai"
        and ((.description // "") | test("^Review skipped: [0-9]+ files exceed the limit of [0-9]+"))
      ) | not
    ))
  '
}

stale_rows() {
  eligible_state | without_file_limit_skips | jq -r -L "$script_dir/lib" '
    include "coderabbit-limits";
    def latest_rate_limit:
      [
        .comments.nodes[]?
        | select(
            (.author.login // "") == "coderabbitai"
            and (
              (.body | rate_limit_reply)
            )
          )
      ]
      | sort_by(.updatedAt)
      | last // null;

    .data.repository.pullRequests.nodes
    | map(
        . as $pr
        | select(
            any(
              .reviews.nodes[]?;
              (.author.login // "") == "coderabbitai"
              and (.commit.oid // "") == $pr.headRefOid
              # Empty-body reviews are resolve/reply events, not a head review,
              # except an approval: `@coderabbitai approve` marks the head done.
              and (
                (.state // "") == "APPROVED"
                or ((.body // "") | gsub("^\\s+|\\s+$"; "") | length) > 0
              )
            )
            | not
          )
        | select(
            any(
              .comments.nodes[]?;
              (.author.login // "") == "coderabbitai"
              and (
                .body
                | contains(
                    "Currently processing new changes in this PR. This may take a few minutes, please wait"
                  )
              )
            )
            | not
          )
        | select(
            any(
              .commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]?;
              (.context // "") == "CodeRabbit"
              and (.creator.login // "") == "coderabbitai"
              and (
                (.state // "") == "PENDING"
                or (
                  (.description // "")
                  | test("review (in progress|processing)"; "i")
                )
              )
            )
            | not
          )
        | select(
            any(
              .commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]?;
              (.context // "") == "CodeRabbit"
              and (.creator.login // "") == "coderabbitai"
              and (.state // "") == "SUCCESS"
              and (.description // "") == "Review completed"
            )
            | not
          )
        | select(
            any(
              .comments.nodes[]?;
              (.author.login // "") == "coderabbitai"
              # The summary names the range its last review covered. That range
              # must end at the head; a skip notice elsewhere in the comment can
              # also link the head.
              and (.body | test("(No actionable comments were generated in the recent review|Reviewing files that changed from the base of the PR)"))
              and (.body | test("between [0-9a-f]+ and " + $pr.headRefOid))
              and ((.body | rate_limit_reply) | not)
            )
            | not
          )
        | (latest_rate_limit) as $rate_limit
        | . + {
            queueLimitAt: ($rate_limit.updatedAt // ""),
            queueSortAt: (.commits.nodes[0].commit.committedDate // .updatedAt)
          }
      )
    | sort_by(.queueSortAt)
    | reverse
    | .[]
    | [
        .number,
        .queueSortAt,
        .headRefName,
        .headRefOid,
        .title,
        (if .queueLimitAt == "" then "-" else .queueLimitAt end)
      ]
    | @tsv
  '
}

# requeued-heads.tsv holds "PR, head the queue last requested a review for,
# 1 once the PR has been placed after returning with a newer head". Place each
# return once, so later manual moves and further pushes keep their position.
position_requeued_pr() {
  local pr=$1 head_sha=$2 order_lock_fd reviewed_head positioned
  [[ -f $requeued_heads_file ]] || return 0
  IFS=$'\t' read -r _ reviewed_head positioned < <(
    awk -F '\t' -v pr="$pr" '$1 == pr' "$requeued_heads_file"
  ) || return 0
  [[ -n $reviewed_head && $reviewed_head != "$head_sha" ]] || return 0

  exec {order_lock_fd}>"$queue_order_file.lock"
  flock -x "$order_lock_fd"
  # If a stale GUI save omitted the placed PR, restore it.
  if [[ $positioned == 1 && -f $queue_order_file ]] &&
    rg -q -F -x "$pr" "$queue_order_file"; then
    exec {order_lock_fd}>&-
    return 0
  fi
  {
    if requeued_items_at_top_enabled; then printf '%s\n' "$pr"; fi
    if [[ -f $queue_order_file ]]; then
      awk -v pr="$pr" '$0 != pr' "$queue_order_file"
    fi
    if ! requeued_items_at_top_enabled; then printf '%s\n' "$pr"; fi
  } >"$queue_order_file.tmp.$$"
  mv "$queue_order_file.tmp.$$" "$queue_order_file"
  write_requeued_head "$pr" "$reviewed_head" 1
  exec {order_lock_fd}>&-
}

# Callers hold the queue order lock.
write_requeued_head() {
  local pr=$1 head_sha=$2 positioned=$3
  {
    if [[ -f $requeued_heads_file ]]; then
      awk -F '\t' -v pr="$pr" '$1 != pr' "$requeued_heads_file"
    fi
    printf '%s\t%s\t%s\n' "$pr" "$head_sha" "$positioned"
  } >"$requeued_heads_file.tmp.$$"
  mv "$requeued_heads_file.tmp.$$" "$requeued_heads_file"
}

load_stale_rows() {
  local state=$1
  local defer_agent_decisions=${2:-0}
  local row wanted pr index head_sha completed_key
  local -a loaded=()
  local -a ordered=()

  deferred_review_count=0
  reap_review_watchers

  while IFS= read -r row; do
    [[ -n $row ]] || continue
    IFS=$'\t' read -r pr _updated_at _branch_name head_sha _title _limit_at <<<"$row"
    completed_key="$pr:$head_sha"
    if [[ -n ${completed_review_heads[$completed_key]+present} ||
      -n ${review_watchers[$completed_key]+present} ]]; then
      continue
    fi
    if (( defer_agent_decisions )) && agent_review_decision_pending "$row"; then
      ((deferred_review_count+=1))
      continue
    fi
    position_requeued_pr "$pr" "$head_sha"
    loaded+=("$row")
  done < <(stale_rows <<<"$state")

  if [[ -f $queue_order_file ]]; then
    while IFS= read -r wanted; do
      [[ $wanted =~ ^[0-9]+$ ]] || continue
      for index in "${!loaded[@]}"; do
        IFS=$'\t' read -r pr _ <<<"${loaded[$index]}"
        if [[ $pr == "$wanted" ]]; then
          ordered+=("${loaded[$index]}")
          unset 'loaded[index]'
          break
        fi
      done
    done <"$queue_order_file"
  fi

  # Rows absent from the saved order are new; insert at top (default) or bottom.
  if new_items_at_top_enabled; then
    stale=("${loaded[@]}" "${ordered[@]}")
  else
    stale=("${ordered[@]}" "${loaded[@]}")
  fi
}

agent_review_decision_pending() {
  local row=$1 pr branch_name head_sha key progress mode
  auto_merge_enabled || return 1
  mode=$(merge_after_delegation_mode)
  [[ $mode == agent || $mode == 0 ]] || return 1
  IFS=$'\t' read -r pr _updated_at branch_name head_sha _title _limit_at <<<"$row"
  [[ -f $delegated_prs_file ]] &&
    rg -q --fixed-strings --line-regexp "$pr" "$delegated_prs_file" || return 1
  key="$pr:$head_sha"
  if [[ -z ${agent_review_decision_cache[$key]+present} ]]; then
    agent_review_decision_cache[$key]=$(agent_task_progress "$branch_name" "$head_sha")
  fi
  progress=${agent_review_decision_cache[$key]}
  [[ $progress == *Running* || $progress == 'Remote agent host unavailable'* ||
    $progress == 'Remote agent status '* ]]
}

# A watched review stays out of the queue until its watcher exits, even if
# GitHub's snapshot has not caught up with CodeRabbit's status yet.
reap_review_watchers() {
  local key status
  for key in "${!review_watchers[@]}"; do
    ! kill -0 "${review_watchers[$key]}" 2>/dev/null || continue
    status=0
    wait "${review_watchers[$key]}" || status=$?
    unset 'review_watchers[$key]'
    (( status != 0 )) || mark_head_verified "${key%%:*}" "${key#*:}"
  done
}

mark_head_verified() {
  local pr=$1
  local head_sha=$2
  local verified_key="$pr:$head_sha"

  completed_review_heads["$verified_key"]=1
}

eligible_rows() {
  eligible_state | jq -r '
    .data.repository.pullRequests.nodes[]
    | [.number, .headRefName, .headRefOid, .title]
    | @tsv
  '
}

# Pass "own" to skip fork PRs, whose branch names can collide with local tasks.
reviewed_rows() {
  eligible_state | jq -r --arg scope "${1:-all}" '
    .data.repository.pullRequests.nodes[]
    | select($scope != "own" or (.isCrossRepository | not))
    | . as $pr
    | select(
        any(
          .reviews.nodes[]?;
          (.author.login // "") == "coderabbitai"
          and (.commit.oid // "") == $pr.headRefOid
          and (
            (.state // "") == "APPROVED"
            or ((.body // "") | gsub("^\\s+|\\s+$"; "") | length) > 0
          )
        )
      )
    | [.number, .headRefName, .headRefOid, .title]
    | @tsv
  '
}

approved_review_rows() {
  eligible_state <<<"$1" | jq -r '
    .data.repository.pullRequests.nodes[]
    | . as $pr
    | select(
        any(
          .reviews.nodes[]?;
          (.author.login // "") == "coderabbitai"
          and (.commit.oid // "") == $pr.headRefOid
          and (.state // "") == "APPROVED"
        )
      )
    | [.number, .title]
    | @tsv
  '
}

approved_merge_rows() {
  eligible_state <<<"$1" | jq -r '
    .data.repository.pullRequests.nodes[]
    | select(.isCrossRepository | not)
    | . as $pr
    | select(any(.reviews.nodes[]?;
        (.author.login // "") == "coderabbitai"
        and (.commit.oid // "") == $pr.headRefOid
        and (.state // "") == "APPROVED"))
    | [.number, .headRefOid, .headRefName, .title]
    | @tsv
  '
}

approved_archive_rows() {
  eligible_state <<<"$1" | jq -r '
    .data.repository.pullRequests.nodes[]
    | select(.isCrossRepository | not)
    | . as $pr
    | select(any(.reviews.nodes[]?;
        (.author.login // "") == "coderabbitai"
        and (.commit.oid // "") == $pr.headRefOid
        and (.state // "") == "APPROVED"))
    | [.number, .headRefName, .headRefOid, .title]
    | @tsv
  '
}

# PRs CodeRabbit is currently reviewing. Rate-limited PRs are never active,
# even if we still have a local outstanding @coderabbitai review request.
active_review_rows() {
  local state
  state=$(eligible_state <<<"$1" | without_file_limit_skips)
  local pr head_sha title now completed_pr='' completed_head=''
  local -A seen=()

  if [[ -s $review_completion_file ]]; then
    IFS=$'\t' read -r completed_pr completed_head _ <"$review_completion_file"
  fi

  while IFS=$'\t' read -r pr title head_sha; do
    [[ -n ${pr:-} ]] || continue
    [[ $pr != "$completed_pr" || $head_sha != "$completed_head" ]] || continue
    seen[$pr]=1
    printf '%s\t%s\n' "$pr" "$title"
  done < <(
    jq -r -L "$script_dir/lib" '
      include "coderabbit-limits";
      def is_rate_limited:
        any(
          .commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]?;
          (.context // "") == "CodeRabbit"
          and (.creator.login // "") == "coderabbitai"
          and (.description // "") == "Review rate limited"
        )
        or any(
          .comments.nodes[]?;
          (.author.login // "") == "coderabbitai"
          and (
            (.body | rate_limit_reply)
          )
        );

      def is_reviewing:
        any(
          .comments.nodes[]?;
          (.author.login // "") == "coderabbitai"
          and (
            .body
            | contains(
                "Currently processing new changes in this PR. This may take a few minutes, please wait"
              )
          )
        )
        or any(
          .commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]?;
          (.context // "") == "CodeRabbit"
          and (.creator.login // "") == "coderabbitai"
          and (
            (.state // "") == "PENDING"
            or (
              (.description // "")
              | test("review (in progress|processing)"; "i")
            )
          )
        );

      def is_review_complete:
        . as $item
        |
        any(
          .reviews.nodes[]?;
          (.author.login // "") == "coderabbitai"
          and (.commit.oid // "") == $item.headRefOid
          and (
            (.state // "") == "APPROVED"
            or ((.body // "") | gsub("^\\s+|\\s+$"; "") | length) > 0
          )
        )
        or any(
          .commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]?;
          (.context // "") == "CodeRabbit"
          and (.creator.login // "") == "coderabbitai"
          and (.state // "") == "SUCCESS"
          and (.description // "") == "Review completed"
        );

      .data.repository.pullRequests.nodes[]
      | select(is_reviewing and (is_rate_limited | not) and (is_review_complete | not))
      | [.number, .title, .headRefOid]
      | @tsv
    ' <<<"$state"
  )

  [[ -f $review_requests_file ]] || return 0
  now=$(date -u +%s)
  while IFS=$'\t' read -r pr head_sha; do
    [[ -n ${pr:-} && -n ${head_sha:-} ]] || continue
    [[ -z ${seen[$pr]:-} ]] || continue
    title=$(
      jq -r -L "$script_dir/lib" --argjson pr "$pr" --arg head "$head_sha" '
        include "coderabbit-limits";
        def is_rate_limited:
          any(
            .commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]?;
            (.context // "") == "CodeRabbit"
            and (.creator.login // "") == "coderabbitai"
            and (.description // "") == "Review rate limited"
          )
          or any(
            .comments.nodes[]?;
            (.author.login // "") == "coderabbitai"
            and (
              (.body | rate_limit_reply)
            )
          );

        .data.repository.pullRequests.nodes[]
        | select(.number == ($pr | tonumber) and .headRefOid == $head)
        | select(is_rate_limited | not)
        | select(
            any(
              .reviews.nodes[]?;
              (.author.login // "") == "coderabbitai"
              and (.commit.oid // "") == $head
              and ((.body // "") | gsub("^\\s+|\\s+$"; "") | length) > 0
            )
            | not
          )
        | select(
            any(
              .commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]?;
              (.context // "") == "CodeRabbit"
              and (.creator.login // "") == "coderabbitai"
              and (.state // "") == "SUCCESS"
              and (.description // "") == "Review completed"
            )
            | not
          )
        | .title
      ' <<<"$state"
    )
    [[ -n $title && $title != null ]] || continue
    seen[$pr]=1
    printf '%s\t%s\n' "$pr" "$title"
  done < <(
    awk -F '\t' -v now="$now" -v max_age=900 '
      NF >= 3 {
        head[$1] = $2
        ts[$1] = $3
      }
      END {
        for (pr in ts)
          if (ts[pr] + 0 > 0 && (now - ts[pr]) <= max_age)
            printf "%s\t%s\n", pr, head[pr]
      }
    ' "$review_requests_file"
  )
}

timer_rows() {
  jq -r -L "$script_dir/lib" '
    include "coderabbit-limits";
    def latest_rate_limit:
      [
        .comments.nodes[]?
        | select(
            (.author.login // "") == "coderabbitai"
            and (
              (.body | rate_limit_reply)
            )
          )
      ]
      | sort_by(.updatedAt)
      | last // null;

    # A later "Reviews are available now" reply voids older limits, for
    # example after a plan upgrade.
    ([
      .data.repository.pullRequests.nodes[].comments.nodes[]?
      | select((.author.login // "") == "coderabbitai"
          and (.body | test("Reviews are available now"; "i")))
      | .updatedAt
    ] | max // "") as $available_at
    | .data.repository.pullRequests.nodes[]
    | (latest_rate_limit) as $rate_limit
    | (
        if $rate_limit == null then null
        else
          $rate_limit.body | rate_limit_timer
        end
      ) as $timer
    | select(
        $rate_limit != null
        and $timer != null
        and $rate_limit.updatedAt > $available_at
      )
    | [
        .number,
        .headRefOid,
        $rate_limit.updatedAt,
        $timer.amount,
        $timer.unit
      ]
    | @tsv
  '
}

review_window_safety_for() {
  case $1 in
    minute|minutes|hour|hours) printf '%s\n' "$review_window_safety_minutes" ;;
    *) printf '%s\n' "$review_window_safety_seconds" ;;
  esac
}

latest_expiry() {
  local state=$1
  local latest_update=0
  local selected_expiry=0
  local _pr _head_sha updated_at amount unit updated_epoch expiry safety

  while IFS=$'\t' read -r _pr _head_sha updated_at amount unit; do
    [[ -n ${updated_at:-} && ${amount:-} =~ ^[0-9]+$ ]] || continue
    case $unit in
      second|seconds|minute|minutes|hour|hours) ;;
      *) continue ;;
    esac
    updated_epoch=$(date -u -d "$updated_at" +%s)
    expiry=$(date -u -d "$updated_at + $amount $unit" +%s)
    if (( updated_epoch > latest_update )); then
      latest_update=$updated_epoch
      safety=$(review_window_safety_for "$unit")
      selected_expiry=$((expiry + safety))
    fi
  done < <(timer_rows <<<"$state")

  printf '%s\n' "$selected_expiry"
}

shared_expiry() {
  local expiry=0

  if [[ -f $quota_expiry_file ]]; then
    read -r expiry <"$quota_expiry_file" || true
  fi
  [[ $expiry =~ ^[0-9]+$ ]] || expiry=0
  printf '%s\n' "$expiry"
}

remember_shared_expiry() {
  local expiry=$1
  local current

  [[ $expiry =~ ^[0-9]+$ ]] || return 0
  current=$(shared_expiry)
  (( expiry > current )) || return 0
  # Replace atomically: concurrent readers must never see an empty file.
  local temporary
  temporary=$(mktemp "$quota_expiry_file.XXXXXX")
  printf '%s\n' "$expiry" >"$temporary"
  mv -- "$temporary" "$quota_expiry_file"
}

record_review_request() {
  local pr=$1
  local head_sha=$2
  local requested_at=$3
  local order_lock_file="$queue_order_file.lock" order_lock_fd

  printf '%s\t%s\t%s\n' "$pr" "$head_sha" "$requested_at" \
    >>"$review_requests_file"

  # Remember the reviewed head; the PR is placed by the requeue preference
  # when it returns with a newer one. Until then it keeps its queue position.
  if (( requested_at > 0 )); then
    exec {order_lock_fd}>"$order_lock_file"
    flock -x "$order_lock_fd"
    write_requeued_head "$pr" "$head_sha" 0
    exec {order_lock_fd}>&-
  fi
}

# Request and delegation logs only grow. At monitor start, drop PRs whose
# requests are all past every window, and other PR records of closed PRs.
prune_state_files() {
  local state=$1 temporary now
  now=$(date -u +%s)
  # Interrupted writers, such as an archive scan stopped by a monitor restart,
  # leave their mktemp files behind. No writer keeps one for a day.
  find "$state_root" -maxdepth 1 -mtime +0 \( \
    \( -type f \( -name "$repo_key-*.??????" -o -name "$repo_key-*.tmp.*" \) \) -o \
    \( -type d -name 'task-progress.??????' \) \) -exec rm -rf -- {} + 2>/dev/null || true
  # Routing-failure markers and their locks are only needed while the PR is open.
  local failure open_prs
  if open_prs=$(jq -er '.data.repository.pullRequests.nodes | map(.number) | join(" ")' \
    <<<"$state" 2>/dev/null); then
    for failure in "$state_root/$repo_key"-routing-failure-*; do
      [[ $failure =~ -routing-failure-([0-9]+)(\.lock)?$ ]] || continue
      [[ " $open_prs " == *" ${BASH_REMATCH[1]} "* ]] || rm -f -- "$failure"
    done
  fi
  if [[ -f $review_requests_file ]]; then
    temporary=$(mktemp "$review_requests_file.XXXXXX")
    awk -F '\t' -v now="$now" -v max_age="$((review_request_window > 900 ? review_request_window : 900))" '
      FNR == NR { if ($3 + 0 > 0 && now - $3 <= max_age) recent[$1] = 1; next }
      $1 in recent
    ' "$review_requests_file" "$review_requests_file" >"$temporary"
    mv "$temporary" "$review_requests_file"
  fi
  if [[ -f $review_history_file ]]; then
    temporary=$(mktemp "$review_history_file.XXXXXX")
    awk -F '\t' -v cutoff="$((now - 86400))" '$1 >= cutoff' "$review_history_file" >"$temporary"
    mv "$temporary" "$review_history_file"
  fi
  [[ -n ${open_prs:-} ]] || return 0
  local file order_lock_fd
  exec {order_lock_fd}>"$queue_order_file.lock"
  flock -x "$order_lock_fd"
  for file in "$delegated_prs_file" "$queue_order_file" "$requeued_heads_file"; do
    [[ -f $file ]] || continue
    temporary=$(mktemp "$file.XXXXXX")
    tr ' ' '\n' <<<"$open_prs" |
      awk -F '\t' 'FNR == NR { open[$1] = 1; next } $1 in open' - "$file" >"$temporary"
    mv "$temporary" "$file"
  done
  exec {order_lock_fd}>&-
}

# review-history.tsv: "epoch<TAB>accepted" when CodeRabbit accepts a queued
# review and "epoch<TAB>limited" when it reports no review available.
record_review_history() {
  printf '%s\t%s\n' "$(date -u +%s)" "$1" >>"$review_history_file"
}

# Prints the estimated start time (epoch) of the next COUNT queued reviews.
# CodeRabbit allows a number of reviews per rolling hour; each "limited" event
# shows that limit as the reviews accepted in the hour before it. Without one,
# reviews only wait for each other's quota check and trigger.
review_slot_estimates() {
  local count=$1 expiry=$2 history=/dev/null
  (( count > 0 )) || return 0
  [[ ! -f $review_history_file ]] || history=$review_history_file
  awk -F '\t' -v now="$(date -u +%s)" -v count="$count" -v expiry="$expiry" '
    function in_window(t,    i, c) {
      c = 0; oldest = 0
      for (i = 1; i <= n; i++) if (accepted[i] > t - 3600) {
        c++
        if (!oldest || accepted[i] < oldest) oldest = accepted[i]
      }
      return c
    }
    $2 == "accepted" {
      # Back-to-back triggers show how long a quota check and trigger take.
      if (n && $1 - accepted[n] < 600) { gaps += $1 - accepted[n]; gap_count++ }
      accepted[++n] = $1
      # More reviews in an hour than the limit allows: the plan changed.
      if (capacity && in_window($1) > capacity) capacity = 0
    }
    $2 == "limited" { c = in_window($1); if (c) capacity = c }
    END {
      gap = gap_count ? int(gaps / gap_count) : 60
      t = expiry > now ? expiry : now
      for (k = 1; k <= count; k++) {
        while (capacity && in_window(t) >= capacity) t = oldest + 3600
        printf "%s%d", (k > 1 ? " " : ""), t
        accepted[++n] = t
        t += gap
      }
      print ""
    }
  ' "$history"
}

clear_review_request() {
  local pr=$1
  local head_sha=$2

  record_review_request "$pr" "$head_sha" 0
}

recent_review_request_expiry() {
  local pr=$1
  local head_sha=$2
  local now cutoff cutoff_iso comments github_at github_epoch local_epoch=0
  local expiry=0

  now=$(date -u +%s)
  cutoff=$((now - review_request_window))
  cutoff_iso=$(date -u -d "@$cutoff" +%Y-%m-%dT%H:%M:%SZ)

  if [[ -f $review_requests_file ]]; then
    local_epoch=$(
      awk -F '\t' -v pr="$pr" -v head="$head_sha" '
        $1 == pr && $2 == head { requested_at = $3 }
        END { print requested_at + 0 }
      ' "$review_requests_file"
    )
  fi
  if (( local_epoch > cutoff )); then
    expiry=$((local_epoch + review_request_window))
  fi

  comments=$(issue_comments "$pr") || return 1
  github_at=$(
    jq -r -L "$script_dir/lib" --arg cutoff "$cutoff_iso" '
      include "review-feedback";
      include "coderabbit-limits";
      . as $comments
      | [
          .[]
          | select(
              .user.login != "coderabbitai[bot]"
              and .created_at >= $cutoff
              and ((.body | without_queue_footer) == "@coderabbitai review")
            )
      ]
      | sort_by(.created_at)
      | last // null
      | . as $request
      | if . == null then
          empty
        elif any(
          $comments[];
          .user.login == "coderabbitai[bot]"
          and .updated_at >= $request.created_at
          and (
            (.body | rate_limit_reply)
          )
        ) then
          empty
        else
          .created_at
        end
    ' <<<"$comments"
  )
  if [[ -n $github_at ]]; then
    github_epoch=$(date -u -d "$github_at" +%s)
    if (( github_epoch + review_request_window > expiry )); then
      expiry=$((github_epoch + review_request_window))
    fi
  fi

  printf '%s\n' "$expiry"
}
