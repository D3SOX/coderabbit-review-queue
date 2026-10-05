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
  local task_model task_effort t3_title

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
    python3 - "$session_file" <<'PY'
import json
import re
import sys

message = ''
with open(sys.argv[1]) as events:
    for line in events:
        if 'agent_message' not in line and 'item_completed' not in line:
            continue
        try:
            event = json.loads(line)
            payload = event.get('payload', {})
            if event.get('type') != 'event_msg':
                continue
            if payload.get('type') == 'agent_message':
                text = payload.get('message', '')
            elif payload.get('type') == 'item_completed' and payload.get('item', {}).get('type') == 'AgentMessage':
                text = '\n'.join(item.get('text', '') for item in payload['item'].get('content', [])
                                 if item.get('type') == 'Text')
            else:
                continue
            if isinstance(text, str):
                message = text
        except (ValueError, AttributeError, TypeError):
            continue
message = re.sub(r'[\r\n\t]+', ' ', message)
print(message[:177] + '...' if len(message) > 180 else message)
PY
  )
  metadata=$(codex_thread_metadata "$session_id" 2>/dev/null || true)
  IFS=$'\t' read -r task_title task_model task_effort <<<"$metadata"
  t3_title=$(python3 "$script_dir/t3-delegate.py" --title "$session_id" 2>/dev/null || true)
  [[ -z $t3_title ]] || task_title=$t3_title
  if [[ $state == Running ]] &&
    codex_can_steer "$session_id"; then
    state='Running (steerable)'
  fi
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
  local codex_out claude_out mode
  mode=${agent_mode_override:-$(auto_delegation_mode)}

  codex_out=$(codex_task_progress "$branch_name" "$head_sha")
  if [[ $codex_out == Running* ]]; then
    printf 'Codex %s\n' "$codex_out"
    return 0
  fi
  if [[ $mode == codex ]]; then
    if [[ $codex_out == 'No matching task' ]]; then
      printf '%s\n' "$codex_out"
    else
      printf 'Codex %s\n' "$codex_out"
    fi
    return 0
  fi
  claude_out=$(claude_task_progress "$branch_name" "$head_sha")
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
  local branch_name=$1 head_sha=$2 host output mode
  host=$(agent_host 2>/dev/null || true)
  if [[ -z $host ]]; then
    local_agent_task_progress "$branch_name" "$head_sha"
    return
  fi
  mode=${agent_mode_override:-$(auto_delegation_mode)}
  [[ $mode != disabled ]] || mode=auto
  if output=$(remote_command_timeout=20 remote_agent_command \
    --task-progress "$branch_name" "$head_sha" --agent-mode "$mode" 2>/dev/null); then
    printf '%s\n' "$output"
  else
    case $? in
      124) printf 'Remote agent status timed out (%s)\n' "$host" ;;
      255) printf 'Remote agent host unavailable (%s)\n' "$host" ;;
      *) printf 'Remote agent status query failed (%s)\n' "$host" ;;
    esac
  fi
}
