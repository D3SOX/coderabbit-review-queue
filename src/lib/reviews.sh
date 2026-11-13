# SPDX-License-Identifier: GPL-3.0-only

poll_wait_background_actions() {
  local attempt=$1
  (( attempt % 12 == 1 )) || return 0
  wait_for_github_quota
  if auto_delegation_enabled || { auto_merge_enabled && merge_after_approval_enabled; }; then
    process_approval_actions_async
  fi
}

wait_until() {
  local expiry=$1
  local notification_mode=${2:-notify}
  local now delay wake interval announced=0

  now=$(date -u +%s)
  (( expiry > now )) || return 0
  delay=$((expiry - now))
  wake=$(human_expiry "$expiry")
  printf 'Waiting until CodeRabbit expiry %s (%s seconds).\n' "$wake" "$delay"
  # A wait this short is only the floored-countdown safety margin, not a new
  # review window worth announcing immediately before the quota recheck.
  if [[ $notification_mode != quiet ]] &&
    (( delay > review_window_safety_minutes )); then
    announced=1
    desktop_notify \
      'CodeRabbit queue waiting' \
      "Next review window opens $wake."
  fi
  local actions_at=$now
  while (( delay > 0 )); do
    interval=$delay
    (( interval <= 5 )) || interval=5
    sleep "$interval"
    # The GUI's "Check now" ends the wait early.
    [[ ! -e $check_quota_now_file ]] || return 0
    now=$(date -u +%s)
    delay=$((expiry - now))
    if (( delay > 0 && now - actions_at >= 60 )) && {
      auto_delegation_enabled || { auto_merge_enabled && merge_after_approval_enabled; }
    }; then
      actions_at=$now
      process_approval_actions_async
    fi
  done
  # Ding when a real rate-limit wait ends (slot opens), not when a review finishes.
  if (( announced == 1 )) && notify_sound_enabled; then
    play_notification_sound || true
  fi
}

human_time() {
  local expiry=$1

  # Prefer the locale's 12/24-hour style (%p is empty under 24-hour locales).
  if [[ -n $(date +%p) ]]; then
    date -d "@$expiry" +'%-I:%M %p'
  else
    date -d "@$expiry" +'%H:%M'
  fi
}

human_expiry() {
  local expiry=$1
  local today tomorrow target time

  today=$(date +%F)
  tomorrow=$(date -d tomorrow +%F)
  target=$(date -d "@$expiry" +%F)
  time=$(human_time "$expiry")

  if [[ $target == "$today" ]]; then
    printf 'today at %s' "$time"
  elif [[ $target == "$tomorrow" ]]; then
    printf 'tomorrow at %s' "$time"
  else
    printf '%s at %s' "$(date -d "@$expiry" '+%a, %d %b')" "$time"
  fi
}

# A REST GET that revalidates with the last ETag. GitHub does not count a 304
# against the REST quota, so polling an unchanged resource is free. --slurp
# matches `gh api --paginate --slurp`; a resource with more pages skips the cache.
github_get() {
  local slurp=0 url cache_dir cache etag='' response status=0 status_line headers body temporary
  if [[ $1 == --slurp ]]; then
    slurp=1
    shift
  fi
  url=$1
  cache_dir="$runtime_root/coderabbit-review-queue-etags"
  cache="$cache_dir/$(sha1sum <<<"$repo $url" | cut -c1-40)"
  [[ -f $cache ]] && etag=$(head -n 1 "$cache")
  # gh exits 1 on a 304, so the status line decides.
  response=$(gh api -i ${etag:+-H "If-None-Match: $etag"} "$url" 2>/dev/null) || status=$?
  status_line=${response%%$'\n'*}
  if [[ $status_line != HTTP/* ]]; then
    # Not a response with headers (a test double, for example).
    printf '%s\n' "$response"
    return "$status"
  fi
  case ${status_line#HTTP/* } in
    304*)
      touch "$cache"
      body=$(tail -n +2 "$cache")
      ;;
    2*)
      headers=$(awk '/^\r?$/ { exit } { print }' <<<"$response")
      body=$(awk 'found { print; next } /^\r?$/ { found=1 }' <<<"$response")
      if rg -qi '^link:.*rel="next"' <<<"$headers"; then
        if (( slurp )); then
          gh api --paginate --slurp "$url"
        else
          gh api --paginate "$url"
        fi
        return
      fi
      etag=$(awk -F ': ' 'tolower($1) == "etag" { sub(/\r$/, "", $2); print $2 }' <<<"$headers")
      if [[ -n $etag ]] && mkdir -p -m 700 "$cache_dir"; then
        temporary=$(mktemp "$cache.XXXXXX")
        printf '%s\n%s\n' "$etag" "$body" >"$temporary"
        mv "$temporary" "$cache"
        # Entries are per head or PR; drop the ones no poll has used lately.
        find "$cache_dir" -type f -mmin +60 -delete 2>/dev/null || true
      fi
      ;;
    *)
      printf 'GitHub request failed: %s\n' "${status_line%$'\r'}" >&2
      return 1
      ;;
  esac
  if (( slurp )); then
    printf '[%s]\n' "$body"
  else
    printf '%s\n' "$body"
  fi
}

# All issue comments as one array. Without --slurp, gh emits one array per
# page and jq would answer once per page.
issue_comments() {
  github_get --slurp "repos/$repo/issues/$1/comments?per_page=100" |
    jq -c 'add // []'
}

quota_row_from_comments() {
  local since=$1

  jq -r --arg since "$since" '
    [
      .[]
      | select(
          .user.login == "coderabbitai[bot]"
          and .created_at >= $since
        )
      | . as $comment
      | .body as $body
      | (
          [
            $body
            | capture(
                "(?<remaining>[0-9]+)(?:\\s*/\\s*[0-9]+)?\\s+reviews? remaining";
                "i"
              )
          ]
          | first // null
        ) as $remaining_match
      | (
          if $remaining_match == null then null
          else ($remaining_match.remaining | tonumber)
          end
        ) as $remaining
      | (
          [
            $body
            | capture(
                "(?:refill(?:s)? in|next review available in:?\\*{0,2}|more reviews will be available in)\\s*\\*{0,2}(?<amount>[0-9]+)\\s+(?<unit>seconds?|minutes?|hours?)";
                "i"
              )
          ]
          | first // null
        ) as $timer
      | if ($body | test("reviews are available now"; "i")) then
          {
            remaining: -1,
            at: $comment.created_at,
            timer: null
          }
        elif $remaining != null then
          {
            remaining: $remaining,
            at: $comment.created_at,
            timer: $timer
          }
        elif (
          $timer != null
          and (
            $body
            | test(
                "review limit|no reviews? remaining|couldn.t start this review|more reviews will be available";
                "i"
              )
          )
        ) then
          {
            remaining: 0,
            at: $comment.created_at,
            timer: $timer
          }
        else
          empty
        end
    ]
    | sort_by(.at)
    | last // empty
    | [
        .remaining,
        .at,
        (.timer.amount // 0),
        (.timer.unit // "seconds")
      ]
    | @tsv
  '
}

query_quota() {
  local pr=$1
  local title=$2
  local command command_at comments row amount unit attempt

  printf 'Checking CodeRabbit quota on PR #%s: %s\n' "$pr" "$title"
  command=$(
    gh api -X POST "repos/$repo/issues/$pr/comments" \
      -f body="@coderabbitai rate limit"$'\n\n'"$queue_comment_footer"
  ) || return 1
  command_at=$(jq -r '.created_at // empty' <<<"$command")
  # An empty cutoff would accept every old quota reply on the PR.
  [[ -n $command_at ]] || return 1

  for ((attempt = 1; attempt <= quota_response_attempts; attempt++)); do
    poll_wait_background_actions "$attempt"
    comments=$(issue_comments "$pr")
    row=$(quota_row_from_comments "$command_at" <<<"$comments")
    if [[ -n $row ]]; then
      IFS=$'\t' read -r quota_remaining response_at amount unit <<<"$row"
      quota_expiry=0
      if [[ $amount =~ ^[0-9]+$ ]] && (( amount > 0 )); then
        quota_expiry=$(
          date -u -d "$response_at + $amount $unit" +%s
        )
        quota_expiry=$((quota_expiry + $(review_window_safety_for "$unit")))
      fi

      if (( quota_remaining < 0 )); then
        printf 'CodeRabbit reports reviews are available now.\n'
      elif (( quota_remaining == 0 )); then
        printf 'CodeRabbit reports no reviews currently available.\n'
      else
        printf 'CodeRabbit reports %s review(s) available.\n' "$quota_remaining"
      fi
      return 0
    fi
    sleep "$github_poll_interval"
  done

  printf 'CodeRabbit did not answer the quota check within five minutes; retrying in two minutes.\n' >&2
  desktop_notify \
    'CodeRabbit quota check delayed' \
    "PR #$pr — $title"$'\n''No response after five minutes. Retrying in two minutes; the monitor remains active.'
  return 1
}

coderabbit_commit_statuses() {
  local head_sha=$1 since=$2 statuses checks
  # CodeRabbit publishes CheckRuns now. Poll those first so a running review
  # costs one request; only legacy installations also need commit statuses.
  checks=$(github_get --slurp "repos/$repo/commits/$head_sha/check-runs?check_name=CodeRabbit&per_page=100") || return $?
  checks=$(jq -c -L "$script_dir/lib" '
    include "coderabbit-checks";
    [(if type == "array" then .[] else . end)
      | objects | .check_runs[]? | coderabbit_check_context
      | select(.context == "CodeRabbit" and .creator.login == "coderabbitai")
      | .state |= ascii_downcase]
  ' <<<"$checks") || return 1
  if jq -e --arg since "$since" 'any(.[]; .created_at >= $since)' <<<"$checks" >/dev/null; then
    jq -c '{statuses: .}' <<<"$checks"
    return 0
  fi
  statuses=$(github_get "repos/$repo/commits/$head_sha/status") || return $?
  jq -s '{statuses: ((.[0].statuses // []) + .[1])}' < <(printf '%s\n' "$statuses" "$checks")
}

wait_for_acceptance() {
  local pr=$1
  local triggered_at=$2
  local head_sha=$3
  local title=$4
  local attempt comments statuses result

  for ((attempt = 1; attempt <= acceptance_response_attempts; attempt++)); do
    poll_wait_background_actions "$attempt"
    comments=$(issue_comments "$pr")
    statuses=$(coderabbit_commit_statuses "$head_sha" "$triggered_at")
    result=$(
      jq -r \
        --arg since "$triggered_at" \
        --arg head "$head_sha" \
        --slurpfile statuses <(printf '%s\n' "$statuses") '
        if (
          any(
            .[];
            .user.login == "coderabbitai[bot]"
            and .updated_at >= $since
            and (
              (.body | contains("rate limited by coderabbit.ai"))
              or (.body | test("More reviews will be available in"; "i"))
            )
          )
          or any(
            $statuses[0].statuses[];
            .context == "CodeRabbit"
            and .created_at >= $since
            and .description == "Review rate limited"
          )
        ) then
          "limited"
        elif (
          any(
            .[];
            .user.login == "coderabbitai[bot]"
            and (
              (
                .created_at >= $since
                and (.body | contains("Review triggered"))
              )
              or (
                .updated_at >= $since
                and (.body | contains("No actionable comments were generated in the recent review"))
                and (.body | test("between [0-9a-f]+ and " + $head))
                and ((.body | contains("rate limited by coderabbit.ai")) | not)
              )
            )
          )
          or any(
            $statuses[0].statuses[];
            .context == "CodeRabbit"
            and (
              (
                .created_at >= $since
                and .description != "Review rate limited"
              )
              or (
                .description == "Review in progress"
                and .state == "pending"
                and (.created_at | fromdateiso8601)
                    >= (($since | fromdateiso8601) - 60)
              )
            )
          )
        ) then
          "accepted"
        else
          "waiting"
        end
      ' <<<"$comments"
    )
    if [[ $result == accepted ]]; then
      printf 'CodeRabbit accepted PR #%s.\n' "$pr"
      return 0
    fi
    if [[ $result == limited ]]; then
      printf 'CodeRabbit rate-limited PR #%s; waiting for its updated countdown.\n' "$pr"
      desktop_notify \
        'CodeRabbit review rate-limited' \
        "PR #$pr — $title"$'\n'"Still queued until CodeRabbit's updated countdown expires."
      return 2
    fi
    sleep "$github_poll_interval"
  done

  printf 'Timed out waiting for CodeRabbit to accept PR #%s; retrying later.\n' "$pr" >&2
  desktop_notify \
    'CodeRabbit request unconfirmed' \
    "PR #$pr — $title"$'\n'"CodeRabbit has not accepted the request yet. The duplicate guard stays active; the queue retries later."
  return 1
}

wait_for_review_completion() {
  local pr=$1
  local triggered_at=$2
  local head_sha=$3
  local title=$4
  local attempt statuses result reviews comments failure_reason approved feedback
  local skipped_polls=0 snapshot_state

  for ((attempt = 1; ; attempt++)); do
    poll_wait_background_actions "$attempt"
    if (( attempt % 12 == 1 )) && snapshot_state=$(snapshot 60) &&
      ! jq -e --argjson pr "$pr" --arg head "$head_sha" '
        any(.data.repository.pullRequests.nodes[]; .number == $pr and .headRefOid == $head)
      ' <<<"$snapshot_state" >/dev/null; then
      printf 'PR #%s was closed or received a new head; no longer watching %s.\n' \
        "$pr" "${head_sha:0:7}"
      return 5
    fi
    statuses=$(coderabbit_commit_statuses "$head_sha" "$triggered_at")
    result=$(
      jq -r --arg since "$triggered_at" '
        [
          .statuses[]
          | select(
              .context == "CodeRabbit"
              and .created_at >= $since
          )
        ]
        | sort_by(.created_at)
        | last // null
        | if .description == "Review completed" then "completed"
          elif .description == "Review rate limited" then "limited"
          elif ((.description // "") | test("^Review skipped: [0-9]+ files exceed the limit of [0-9]+")) then .description
          elif ((.description // "") | startswith("Review failed:")) then .description
          elif .description == "Review skipped" then .description
          else "waiting"
          end
      ' <<<"$statuses"
    )
    # A check run that ends without a reason (neutral, skipped) can still be
    # superseded by the real run. Treat it as final once it persists.
    if [[ $result == 'Review skipped' ]]; then
      ((skipped_polls+=1))
      if (( skipped_polls >= review_skipped_polls )); then
        printf 'CodeRabbit skipped PR #%s without a reason; rechecking the queue.\n' "$pr"
        return 4
      fi
    else
      skipped_polls=0
    fi
    if [[ $result == "Review skipped:"* ]]; then
      printf 'Skipping PR #%s: %s. Continuing with the queue.\n' "$pr" "$result"
      desktop_notify 'CodeRabbit review skipped' "PR #$pr: $title"$'\n'"$result"
      return 3
    fi
    if [[ $result == "Review failed:"* ]]; then
      printf 'CodeRabbit did not complete PR #%s: %s Rechecking the queue.\n' "$pr" "$result"
      return 4
    fi
    if [[ $result == completed ]]; then
      reviews=$(github_get --slurp "repos/$repo/pulls/$pr/reviews?per_page=100")
      comments=$(github_get --slurp "repos/$repo/issues/$pr/comments?per_page=100")
      if jq -s -e \
        --arg since "$triggered_at" \
        --arg head "$head_sha" '
          .[0] as $reviews | .[1] as $comments |
          any(
            $reviews[][];
            .user.login == "coderabbitai[bot]"
            and (.commit_id // "") == $head
            and (.submitted_at // "") >= $since
            and (
              (.state // "") == "APPROVED"
              or ((.body // "") | gsub("^\\s+|\\s+$"; "") | length) > 0
            )
          )
          or any(
            $comments[][];
            .user.login == "coderabbitai[bot]"
            and (.updated_at // "") >= $since
            and (
              (
                (.body | contains("No actionable comments were generated in the recent review"))
                and (.body | test("between [0-9a-f]+ and " + $head))
              )
              or (.body | contains("No files to review."))
            )
            and ((.body | contains("rate limited by coderabbit.ai")) | not)
          )
        ' < <(printf '%s\n' "$reviews" "$comments") >/dev/null; then
        write_review_completion "$pr" "$head_sha" "$title"
        printf 'CodeRabbit finished PR #%s.\n' "$pr"
        approved=$(jq -r --arg since "$triggered_at" --arg head "$head_sha" '
          any(.[][];
            .user.login == "coderabbitai[bot]"
            and (.commit_id // "") == $head
            and (.submitted_at // "") >= $since
            and (.state // "") == "APPROVED")
        ' <<<"$reviews")
        if [[ $approved == true ]]; then
          if ! feedback=$(unresolved_coderabbit_rows "$pr"); then
            desktop_notify 'CodeRabbit approved PR' \
              "PR #$pr — $title"$'\n'"Could not check whether feedback remains."
          elif [[ -n $feedback ]]; then
            desktop_notify 'CodeRabbit approved with feedback' \
              "PR #$pr — $title"$'\n'"Unresolved comments need attention before merging."
          else
            desktop_notify 'CodeRabbit approved PR' \
              "PR #$pr — $title"$'\n'"Approved with no unresolved feedback."
          fi
        else
          desktop_notify 'CodeRabbit review finished' \
            "PR #$pr — $title"$'\n'"Review finished and is ready to inspect."
        fi
        return 0
      fi
    fi
    if (( attempt % 3 == 1 )); then
      comments=$(github_get --slurp "repos/$repo/issues/$pr/comments?per_page=100")
      failure_reason=$(jq -r --arg since "$triggered_at" '
        [ .[][]
          | select(.user.login == "coderabbitai[bot]"
              and .created_at >= $since
              and (.body | contains("Action not completed")))
          | {at: .created_at, reason: ((.body | split("</summary>") | .[1] // "") | gsub("<[^>]*>"; "") | split("\n") | map(gsub("^\\s+|\\s+$"; "") | select(length > 0)) | first // "CodeRabbit could not complete the action.")}
        ] | sort_by(.at) | last | .reason // empty
      ' <<<"$comments")
      # Re-requesting a head without new files gets the same reply forever.
      if [[ $failure_reason == 'No files to review.' ]]; then
        write_review_completion "$pr" "$head_sha" "$title"
        printf 'CodeRabbit has no new files to review on PR #%s.\n' "$pr"
        return 0
      fi
      if [[ -n $failure_reason ]]; then
        printf 'CodeRabbit did not complete PR #%s: %s Rechecking the queue.\n' "$pr" "$failure_reason"
        return 4
      fi
    fi
    if [[ $result == limited ]]; then
      printf 'CodeRabbit rate-limited PR #%s before completion.\n' "$pr"
      return 2
    fi
    if (( attempt == completion_delay_notice_attempts )); then
      printf 'CodeRabbit has not finished PR #%s after ten minutes; continuing to wait.\n' \
        "$pr" >&2
    fi
    if (( attempt >= completion_timeout_attempts )); then
      printf 'CodeRabbit has not finished PR #%s after two hours; rechecking the queue.\n' \
        "$pr" >&2
      return 4
    fi
    sleep "$github_poll_interval"
  done
}

# Background job for one accepted review. Its exit status is that of
# wait_for_review_completion; the monitor reaps it in reap_review_watchers.
watch_review_completion() {
  local pr=$1 triggered_at=$2 head_sha=$3 title=$4 status=0 state
  trap - EXIT
  close_inherited_locks
  # Several reviews may run at once; poll less often to spare GitHub quota.
  github_poll_interval=15
  completion_delay_notice_attempts=40
  completion_timeout_attempts=480
  review_skipped_polls=8
  wait_for_review_completion "$pr" "$triggered_at" "$head_sha" "$title" || status=$?
  case $status in
    0|3|5) clear_review_request "$pr" "$head_sha" ;;
    2)
      clear_review_request "$pr" "$head_sha"
      remember_shared_expiry "$(latest_expiry "$(monitor_snapshot)")"
      ;;
  esac
  if (( status == 0 )); then
    state=$(monitor_snapshot)
    process_approval_actions_async "$state"
    process_thread_archives_async "$state"
    route_all_unresolved_async "$state"
    wait
  fi
  exit "$status"
}

unresolved_coderabbit_rows() {
  local pr=$1
  local cursor='' page has_next
  local thread_query
  local -a args

  read -r -d '' thread_query <<'GRAPHQL' || true
query(
  $owner: String!,
  $name: String!,
  $number: Int!,
  $cursor: String
) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      headRefOid
      reviews(last: 100) {
        nodes {
          id
          state
          body
          submittedAt
          author { login }
          commit { oid }
        }
      }
      comments(last: 100) {
        nodes {
          createdAt
          body
          author { __typename login }
        }
      }
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          originalLine
          comments(first: 1) {
            nodes {
              author {
                login
              }
            }
          }
        }
      }
    }
  }
}
GRAPHQL

  while true; do
    args=(
      api graphql
      -f query="$thread_query"
      -F owner="$owner"
      -F name="$name"
      -F number="$pr"
    )
    [[ -z $cursor ]] || args+=(-F cursor="$cursor")
    page=$(gh "${args[@]}") || return 1
    if [[ -z $cursor ]]; then
      jq -r -L "$script_dir/lib" '
        include "review-feedback";
        .data.repository.pullRequest
        | unanswered_body_feedback
        | (if ((.body // "") | test("Nitpick comments\\s*\\([1-9][0-9]*\\)"; "i"))
           then ["nitpick:" + .id, "CodeRabbit review-body feedback", 0, false]
           else ["outside-diff:" + .id, "CodeRabbit outside-diff feedback", 0, false]
           end)
        | @tsv
      ' <<<"$page"
    fi
    jq -r '
      .data.repository.pullRequest.reviewThreads.nodes[]
      | select(
          (.isResolved | not)
          and (.comments.nodes | length) > 0
          and ((.comments.nodes[0].author.login // "") == "coderabbitai")
        )
      | [
          .id,
          .path,
          (.line // .originalLine // 0),
          .isOutdated
        ]
      | @tsv
    ' <<<"$page"

    has_next=$(
      jq -r \
        '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage' \
        <<<"$page"
    )
    [[ $has_next == true ]] || break
    cursor=$(
      jq -r \
        '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor' \
        <<<"$page"
    )
  done
}

pr_has_unresolved_coderabbit() {
  local pr=$1
  local count

  count=$(
    unresolved_coderabbit_rows "$pr" |
      awk 'NF { count++ } END { print count + 0 }'
  )
  (( count > 0 ))
}
