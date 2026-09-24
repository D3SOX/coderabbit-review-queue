# SPDX-License-Identifier: GPL-3.0-only

wait_until() {
  local expiry=$1
  local notification_mode=${2:-notify}
  local now delay wake announced=0

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
  sleep "$delay"
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
      -f body='@coderabbitai rate limit'
  )
  command_at=$(jq -r .created_at <<<"$command")

  for ((attempt = 1; attempt <= quota_response_attempts; attempt++)); do
    (( attempt % 12 != 1 )) || wait_for_github_quota
    comments=$(gh api --paginate "repos/$repo/issues/$pr/comments")
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

wait_for_acceptance() {
  local pr=$1
  local triggered_at=$2
  local head_sha=$3
  local title=$4
  local attempt comments statuses result

  for ((attempt = 1; attempt <= acceptance_response_attempts; attempt++)); do
    (( attempt % 12 != 1 )) || wait_for_github_quota
    comments=$(gh api --paginate "repos/$repo/issues/$pr/comments")
    statuses=$(gh api "repos/$repo/commits/$head_sha/status")
    result=$(
      jq -r \
        --arg since "$triggered_at" \
        --arg head "$head_sha" \
        --argjson statuses "$statuses" '
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
            $statuses.statuses[];
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
                and (.body | contains($head))
                and ((.body | contains("rate limited by coderabbit.ai")) | not)
              )
            )
          )
          or any(
            $statuses.statuses[];
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

  printf 'Timed out waiting for CodeRabbit to accept PR #%s; stopping safely.\n' "$pr" >&2
  desktop_notify \
    'CodeRabbit queue stopped' \
    "PR #$pr — $title"$'\n'"Timed out waiting for CodeRabbit to accept the request." \
    'critical'
  return 1
}

wait_for_review_completion() {
  local pr=$1
  local triggered_at=$2
  local head_sha=$3
  local title=$4
  local attempt statuses result reviews comments failure_reason approved feedback

  for ((attempt = 1; ; attempt++)); do
    (( attempt % 12 != 1 )) || wait_for_github_quota
    statuses=$(gh api "repos/$repo/commits/$head_sha/status")
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
          else "waiting"
          end
      ' <<<"$statuses"
    )
    if [[ $result == "Review skipped:"* ]]; then
      printf 'Skipping PR #%s: %s. Continuing with the queue.\n' "$pr" "$result"
      desktop_notify 'CodeRabbit review skipped' "PR #$pr: $title"$'\n'"$result"
      return 3
    fi
    if [[ $result == completed ]]; then
      reviews=$(gh api --paginate --slurp "repos/$repo/pulls/$pr/reviews?per_page=100")
      comments=$(gh api --paginate --slurp "repos/$repo/issues/$pr/comments?per_page=100")
      if jq -e \
        --arg since "$triggered_at" \
        --arg head "$head_sha" \
        --argjson comments "$comments" '
          any(
            .[][];
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
                and (.body | contains($head))
              )
              or (.body | contains("No files to review."))
            )
            and ((.body | contains("rate limited by coderabbit.ai")) | not)
          )
        ' <<<"$reviews" >/dev/null; then
        write_monitor_state waiting '' '' 0
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
      comments=$(gh api --paginate --slurp "repos/$repo/issues/$pr/comments?per_page=100")
      failure_reason=$(jq -r --arg since "$triggered_at" '
        [ .[][]
          | select(.user.login == "coderabbitai[bot]"
              and .created_at >= $since
              and (.body | contains("Action not completed")))
          | {at: .created_at, reason: ((.body | split("</summary>") | .[1] // "") | split("\n") | map(select(length > 0)) | first // "CodeRabbit could not complete the action.")}
        ] | sort_by(.at) | last | .reason // empty
      ' <<<"$comments")
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
    sleep "$github_poll_interval"
  done
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
          author { login }
          commit { oid }
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
      jq -r '
        .data.repository.pullRequest as $pr
        | $pr.reviews.nodes[]?
        | select(
            .author.login == "coderabbitai"
            and .commit.oid == $pr.headRefOid
            and .state != "DISMISSED"
            and ((.body // "") | test("Nitpick comments \\([1-9][0-9]*\\)"; "i"))
          )
        | ["nitpick:" + .id, "CodeRabbit review-body nitpick", 0, false]
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
