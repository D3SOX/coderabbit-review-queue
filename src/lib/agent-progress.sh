# SPDX-License-Identifier: GPL-3.0-only

route_all_unresolved_async() {
  local state=$1
  (
    trap - EXIT
    if [[ -n ${monitor_lock_fd:-} ]]; then
      exec {monitor_lock_fd}>&-
    fi
    exec {routing_scan_fd}>"/tmp/coderabbit-review-queue-$repo_key-routing.lock"
    flock -n "$routing_scan_fd" || exit 0
    route_all_unresolved "$state"
  ) &
}

codex_task_progress() {
  local branch_name=$1
  local head_sha=$2
  local match session_id session_cwd session_file state message metadata task_title
  local task_model task_effort

  if ! match=$(matching_codex_session "$branch_name" "$head_sha"); then
    printf 'No matching task\n'
    return 0
  fi
  IFS=$'\t' read -r session_id session_cwd <<<"$match"
  session_file=$(codex_session_file "$session_id")
  if [[ -z $session_file ]]; then
    printf 'Matched %s\n' "${session_id:0:8}"
    return 0
  fi

  case $(codex_session_state "$session_id") in
    running) state='Running' ;;
    idle) state='Idle' ;;
    *) state='Unknown' ;;
  esac
  message=$(
    jq -R -s -r '
      [
        split("\n")[]
        | fromjson?
        | if .type == "event_msg" and .payload.type == "agent_message" then
            .payload.message
          elif .type == "event_msg"
            and .payload.type == "item_completed"
            and .payload.item.type == "AgentMessage" then
            [
              .payload.item.content[]?
              | select(.type == "Text")
              | .text
            ]
            | join("\n")
          else
            empty
          end
      ]
      | last // ""
      | gsub("[\\r\\n\\t]+"; " ")
      | if length > 180 then .[0:177] + "..." else . end
    ' "$session_file"
  )
  metadata=$(codex_thread_metadata "$session_id" 2>/dev/null || true)
  IFS=$'\t' read -r task_title task_model task_effort <<<"$metadata"
  [[ -n $task_title ]] || task_title='—'
  printf '%s\t%s\t%s\n' "$state" "$task_title" "$message"
}

claude_task_progress() {
  local branch_name=$1
  local head_sha=$2
  local match session_id session_cwd session_file state message

  if ! match=$(matching_claude_session "$branch_name" "$head_sha"); then
    printf 'No matching task\n'
    return 0
  fi
  IFS=$'\t' read -r session_id session_cwd <<<"$match"
  if ! session_file=$(claude_session_file "$session_id"); then
    printf 'Matched %s\n' "${session_id:0:8}"
    return 0
  fi

  case $(claude_session_state "$session_id" "$session_file") in
    running) state='Running' ;;
    idle) state='Idle' ;;
    *) state='Unknown' ;;
  esac
  message=$(
    jq -r '
      select(.type == "assistant")
      | [
          .message.content[]?
          | select(.type == "text")
          | .text
        ]
      | join(" ")
      | select(length > 0)
    ' "$session_file" 2>/dev/null |
      tail -n 1 |
      tr '\r\n\t' '   ' |
      awk '{
        line=$0
        if (length(line) > 180) {
          print substr(line, 1, 177) "..."
        } else {
          print line
        }
      }'
  )

  if [[ -n $message ]]; then
    printf '%s (%s) — %s\n' "$state" "${session_id:0:8}" "$message"
  else
    printf '%s (%s)\n' "$state" "${session_id:0:8}"
  fi
}

local_agent_task_progress() {
  local branch_name=$1
  local head_sha=$2
  local codex_out claude_out

  codex_out=$(codex_task_progress "$branch_name" "$head_sha")
  claude_out=$(claude_task_progress "$branch_name" "$head_sha")

  if [[ $codex_out == Running* ]]; then
    printf 'Codex %s\n' "$codex_out"
    return 0
  fi
  if [[ $claude_out == Running* ]]; then
    printf 'Claude %s\n' "$claude_out"
    return 0
  fi
  if [[ $codex_out != 'No matching task' ]]; then
    printf 'Codex %s\n' "$codex_out"
    return 0
  fi
  if [[ $claude_out != 'No matching task' ]]; then
    printf 'Claude %s\n' "$claude_out"
    return 0
  fi
  printf 'No matching task\n'
}

agent_task_progress() {
  local branch_name=$1 head_sha=$2 host output
  host=$(agent_host 2>/dev/null || true)
  if [[ -z $host ]]; then
    local_agent_task_progress "$branch_name" "$head_sha"
    return
  fi
  if output=$(remote_command_timeout=20 remote_agent_command \
    --task-progress "$branch_name" "$head_sha" 2>/dev/null); then
    printf '%s\n' "$output"
  else
    printf 'Remote agent host unavailable (%s)\n' "$host"
  fi
}
