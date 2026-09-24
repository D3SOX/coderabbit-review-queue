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
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 50, states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number
        title
        isDraft
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
  gh api graphql \
    -f query="$query" \
    -F owner="$owner" \
    -F name="$name"
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

configure_repo() {
  local selected=$1

  if [[ ! $selected =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    printf 'Repository must be in OWNER/NAME format: %s\n' "$selected" >&2
    return 2
  fi

  repo=$selected
  owner=${repo%%/*}
  name=${repo#*/}
  repo_key=${repo//\//__}
  monitor_pid_file="/tmp/coderabbit-review-queue-$repo_key.pid"
  monitor_lock_file="/tmp/coderabbit-review-queue-$repo_key.lock"
  dispatch_lock_file="/tmp/coderabbit-review-queue-$repo_key-dispatch.lock"
  mkdir -p "$state_root"
  routed_threads_file="$state_root/$repo_key-routed-threads.txt"
  queue_order_file="$state_root/$repo_key-order.txt"
  quota_expiry_file="$state_root/$repo_key-quota-expiry.txt"
  review_requests_file="$state_root/$repo_key-review-requests.tsv"
  auto_delegate_file="$state_root/$repo_key-auto-delegate"
  stop_when_empty_file="$state_root/$repo_key-stop-when-empty"
  ignore_drafts_file="$state_root/$repo_key-ignore-drafts"
  excluded_branches_file="$state_root/$repo_key-excluded-branches"
  excluded_authors_file="$state_root/$repo_key-excluded-authors"
  notify_sound_file="$state_root/$repo_key-notify-sound"
  notify_sound_path_file="$state_root/$repo_key-notify-sound-path"
  notify_sound_volume_file="$state_root/$repo_key-notify-sound-volume"
  new_items_at_top_file="$state_root/$repo_key-new-items-at-top"
  agent_host_file="$state_root/$repo_key-agent-host"
  delegated_prs_file="$state_root/$repo_key-delegated-prs.txt"
  delegation_prompt_mode_file="$state_root/$repo_key-delegation-prompt-mode"
  delegation_prompt_template_file="$state_root/$repo_key-delegation-prompt-template"
  auto_merge_file="$state_root/$repo_key-auto-merge"
  merge_method_file="$state_root/$repo_key-merge-method"
  delete_branch_file="$state_root/$repo_key-delete-branch"
  merge_after_delegation_file="$state_root/$repo_key-merge-after-delegation"
  merge_after_approval_file="$state_root/$repo_key-merge-after-approval"
  merge_admin_file="$state_root/$repo_key-merge-admin"
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
  local host command arguments
  local -a timeout_args=()
  host=$(agent_host) || {
    printf 'Invalid SSH agent host configured for %s.\n' "$repo" >&2
    return 2
  }
  [[ -n $host ]] || return 2
  printf -v arguments '%q ' --repo "$repo" "$@"
  command="exec ~/.local/bin/coderabbit-review-queue $arguments"
  [[ -z ${remote_command_timeout:-} ]] || timeout_args=(timeout "$remote_command_timeout")
  "${timeout_args[@]}" ssh \
    -n \
    -o BatchMode=yes \
    -o ConnectTimeout=5 \
    -- "$host" "$command"
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
  jq --argjson ignoreDrafts "$(ignore_drafts_json)" \
    --argjson excludedBranches "$(excluded_branches_json)" \
    --argjson excludedAuthors "$(excluded_authors_json)" '
    .data.repository.pullRequests.nodes |= map(
      select($ignoreDrafts == false or (.isDraft | not))
      | select(.headRefName as $b | ($excludedBranches | index($b) | not))
      | select((.author.login // "" | ascii_downcase) as $a
          | ($excludedAuthors | index($a) | not))
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
  eligible_state | without_file_limit_skips | jq -r '
    def latest_rate_limit:
      [
        .comments.nodes[]?
        | select(
            (.author.login // "") == "coderabbitai"
            and (
              (.body | contains("rate limited by coderabbit.ai"))
              or (.body | test("More reviews will be available in"; "i"))
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
              # Empty-body reviews are resolve/reply events, not a head review.
              and ((.body // "") | gsub("^\\s+|\\s+$"; "") | length) > 0
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
              and (.body | contains("No actionable comments were generated in the recent review"))
              and (.body | contains($pr.headRefOid))
              and ((.body | contains("rate limited by coderabbit.ai")) | not)
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

load_stale_rows() {
  local state=$1
  local defer_agent_decisions=${2:-0}
  local row wanted pr index head_sha completed_key
  local -a loaded=()
  local -a ordered=()

  deferred_review_count=0

  while IFS= read -r row; do
    [[ -n $row ]] || continue
    IFS=$'\t' read -r pr _updated_at _branch_name head_sha _title _limit_at <<<"$row"
    completed_key="$pr:$head_sha"
    if [[ -n ${completed_review_heads[$completed_key]+present} ]]; then
      continue
    fi
    if (( defer_agent_decisions )) && agent_review_decision_pending "$row"; then
      ((deferred_review_count+=1))
      continue
    fi
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
  [[ $progress == *Running* || $progress == 'Remote agent host unavailable'* ]]
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

reviewed_rows() {
  eligible_state | jq -r '
    .data.repository.pullRequests.nodes[]
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
    | . as $pr
    | select(any(.reviews.nodes[]?;
        (.author.login // "") == "coderabbitai"
        and (.commit.oid // "") == $pr.headRefOid
        and (.state // "") == "APPROVED"))
    | [.number, .headRefOid, .title]
    | @tsv
  '
}

approved_archive_rows() {
  eligible_state <<<"$1" | jq -r '
    .data.repository.pullRequests.nodes[]
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
    jq -r '
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
            (.body | contains("rate limited by coderabbit.ai"))
            or (.body | test("More reviews will be available in"; "i"))
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
      jq -r --argjson pr "$pr" --arg head "$head_sha" '
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
              (.body | contains("rate limited by coderabbit.ai"))
              or (.body | test("More reviews will be available in"; "i"))
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
  jq -r '
    def latest_rate_limit:
      [
        .comments.nodes[]?
        | select(
            (.author.login // "") == "coderabbitai"
            and (
              (.body | contains("rate limited by coderabbit.ai"))
              or (.body | test("More reviews will be available in"; "i"))
            )
          )
      ]
      | sort_by(.updatedAt)
      | last // null;

    .data.repository.pullRequests.nodes[]
    | select((.title | startswith("[pull] ")) | not)
    | (latest_rate_limit) as $rate_limit
    | (
        if $rate_limit == null then null
        else
          (
            $rate_limit.body
            | try capture(
                "(?:Next review available in:\\*{0,2}\\s*\\*{0,2}|More reviews will be available in\\s+)(?<amount>[0-9]+)\\s+(?<unit>seconds?|minutes?|hours?)";
                "i"
              )
              catch null
          )
        end
      ) as $timer
    | select(
        $rate_limit != null
        and $timer != null
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
  printf '%s\n' "$expiry" >"$quota_expiry_file"
}

record_review_request() {
  local pr=$1
  local head_sha=$2
  local requested_at=$3
  local order_lock_file="$queue_order_file.lock" order_lock_fd

  printf '%s\t%s\t%s\n' "$pr" "$head_sha" "$requested_at" \
    >>"$review_requests_file"

  # Keep an active review pinned visually at the top. Its saved position after
  # the review follows the same top/bottom preference as every other new row.
  if (( requested_at > 0 )); then
    exec {order_lock_fd}>"$order_lock_file"
    flock -x "$order_lock_fd"
    if new_items_at_top_enabled; then
      awk -v pr="$pr" 'BEGIN { print pr } $0 != pr { print }' \
        "$queue_order_file" 2>/dev/null >"$queue_order_file.tmp.$$" ||
        printf '%s\n' "$pr" >"$queue_order_file.tmp.$$"
    else
      awk -v pr="$pr" '$0 != pr { print } END { print pr }' \
        "$queue_order_file" 2>/dev/null >"$queue_order_file.tmp.$$" ||
        printf '%s\n' "$pr" >"$queue_order_file.tmp.$$"
    fi
    mv "$queue_order_file.tmp.$$" "$queue_order_file"
    flock -u "$order_lock_fd"
    exec {order_lock_fd}>&-
  fi
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

  if ! comments=$(
    gh api --paginate "repos/$repo/issues/$pr/comments"
  ); then
    return 1
  fi
  github_at=$(
    jq -r --arg cutoff "$cutoff_iso" '
      . as $comments
      | [
          .[]
          | select(
              .user.login != "coderabbitai[bot]"
              and .created_at >= $cutoff
              and ((.body | gsub("^\\s+|\\s+$"; "")) == "@coderabbitai review")
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
            (.body | contains("rate limited by coderabbit.ai"))
            or (.body | test("More reviews will be available in"; "i"))
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
