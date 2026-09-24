# SPDX-License-Identifier: GPL-3.0-only

matching_codex_session() {
  local branch_name=$1
  local head_sha=$2
  local file meta parsed timestamp session_id cwd metadata_branch metadata_head
  local event_cwd
  local live_branch live_head remote key candidate_path candidate_session
  local -A branch_sessions=()
  local -A branch_timestamps=()
  local -A head_sessions=()
  local -A head_timestamps=()
  local -A metadata_branch_sessions=()
  local -A metadata_branch_timestamps=()
  local -A metadata_head_sessions=()
  local -A metadata_head_timestamps=()
  local -A unique_sessions=()

  while IFS= read -r file; do
    [[ -n ${file:-} ]] || continue
    meta=$(head -n 1 "$file")
    parsed=$(
      jq -r \
        --arg repo "$repo" '
        (.payload.git.repository_url // "") as $url
        | select(
          .type == "session_meta"
          and (.payload.originator | IN("Codex Desktop", "t3code_desktop", "codex-tui", "codex_exec"))
          and (.payload.thread_source // "") != "subagent"
          and (
            ($url | endswith("github.com:" + $repo + ".git"))
            or ($url | endswith("github.com/" + $repo + ".git"))
            or ($url | endswith("github.com/" + $repo))
          )
        )
        | [
            (.timestamp // ""),
            (.payload.session_id // .payload.id // ""),
            (.payload.cwd // ""),
            (.payload.git.branch // ""),
            (.payload.git.commit_hash // "")
          ]
        | @tsv
      ' <<<"$meta"
    )
    [[ -n $parsed ]] || continue
    IFS=$'\t' read -r timestamp session_id cwd metadata_branch metadata_head <<<"$parsed"
    [[ -n $timestamp && -n $session_id && -n $cwd ]] || continue

    if [[ $metadata_branch == "$branch_name" ]]; then
      if [[ $timestamp > ${metadata_branch_timestamps[$cwd]:-} ]]; then
        metadata_branch_timestamps["$cwd"]=$timestamp
        metadata_branch_sessions["$cwd"]=$session_id
      fi
    fi
    if [[ $metadata_head == "$head_sha" ]]; then
      if [[ $timestamp > ${metadata_head_timestamps[$cwd]:-} ]]; then
        metadata_head_timestamps["$cwd"]=$timestamp
        metadata_head_sessions["$cwd"]=$session_id
      fi
    fi

    if ! git -C "$cwd" rev-parse --git-dir >/dev/null 2>&1; then
      continue
    fi
    remote=$(git -C "$cwd" remote get-url origin 2>/dev/null || true)
    if [[ $remote != *"github.com:$repo.git" &&
      $remote != *"github.com/$repo.git" &&
      $remote != *"github.com/$repo" ]]; then
      continue
    fi
    live_branch=$(git -C "$cwd" branch --show-current 2>/dev/null || true)
    live_head=$(git -C "$cwd" rev-parse HEAD 2>/dev/null || true)
    if [[ $live_branch == "$branch_name" ]]; then
      if [[ $timestamp > ${branch_timestamps[$cwd]:-} ]]; then
        branch_timestamps["$cwd"]=$timestamp
        branch_sessions["$cwd"]=$session_id
      fi
    fi
    if [[ $live_head == "$head_sha" ]]; then
      if [[ $timestamp > ${head_timestamps[$cwd]:-} ]]; then
        head_timestamps["$cwd"]=$timestamp
        head_sessions["$cwd"]=$session_id
      fi
    fi

    while IFS= read -r event_cwd; do
      [[ -n $event_cwd && $event_cwd != "$cwd" ]] || continue
      if ! git -C "$event_cwd" rev-parse --git-dir >/dev/null 2>&1; then
        continue
      fi
      remote=$(git -C "$event_cwd" remote get-url origin 2>/dev/null || true)
      if [[ $remote != *"github.com:$repo.git" &&
        $remote != *"github.com/$repo.git" &&
        $remote != *"github.com/$repo" ]]; then
        continue
      fi
      live_branch=$(git -C "$event_cwd" branch --show-current 2>/dev/null || true)
      live_head=$(git -C "$event_cwd" rev-parse HEAD 2>/dev/null || true)
      if [[ $live_branch == "$branch_name" ]]; then
        branch_timestamps["$event_cwd"]=$timestamp
        branch_sessions["$event_cwd"]=$session_id
      fi
      if [[ $live_head == "$head_sha" ]]; then
        head_timestamps["$event_cwd"]=$timestamp
        head_sessions["$event_cwd"]=$session_id
      fi
    done < <(
      jq -R -r '
        fromjson?
        | .payload.item.cwd? // empty
        | sub("^file://"; "")
      ' "$file" 2>/dev/null | sort -u
    )
  done < <(
    rg -l --fixed-strings \
      "$repo" \
      "$codex_sessions_root" 2>/dev/null || true
  )

  for key in branch_sessions head_sessions metadata_branch_sessions metadata_head_sessions; do
    local -n candidates=$key
    unique_sessions=()
    for candidate_path in "${!candidates[@]}"; do
      candidate_session=${candidates[$candidate_path]}
      unique_sessions["$candidate_session"]=$candidate_path
    done
    if (( ${#unique_sessions[@]} == 1 )); then
      printf '%s\t%s\n' "${!unique_sessions[@]}" "${unique_sessions[@]}"
      return 0
    fi
    if (( ${#unique_sessions[@]} > 1 )); then
      return 1
    fi
  done
  return 1
}

claude_agents_json() {
  if [[ -z $_claude_agents_json ]]; then
    if ! _claude_agents_json=$(claude agents --json 2>/dev/null); then
      _claude_agents_json='[]'
    fi
    [[ -n $_claude_agents_json ]] || _claude_agents_json='[]'
  fi
  printf '%s\n' "$_claude_agents_json"
}

matching_claude_session() {
  local branch_name=$1
  local head_sha=$2
  local project_dir file line parsed timestamp session_id cwd metadata_branch
  local live_branch live_head remote key
  local -A branch_sessions=()
  local -A branch_timestamps=()
  local -A head_sessions=()
  local -A head_timestamps=()
  local -A metadata_branch_sessions=()
  local -A metadata_branch_timestamps=()

  [[ -d $claude_projects_root ]] || return 1

  while IFS= read -r -d '' file; do
    [[ -n ${file:-} ]] || continue
    line=$(
      rg -N -m 1 \
        '"cwd":"/' \
        "$file" 2>/dev/null || true
    )
    [[ -n $line ]] || continue
    parsed=$(
      jq -r '
        select(
          .cwd != null
          and .cwd != ""
          and .sessionId != null
          and .sessionId != ""
          and ((.isSidechain // false) | not)
        )
        | [
            (.timestamp // ""),
            .sessionId,
            .cwd,
            (.gitBranch // "")
          ]
        | @tsv
      ' <<<"$line" 2>/dev/null || true
    )
    [[ -n $parsed ]] || continue
    IFS=$'\t' read -r timestamp session_id cwd metadata_branch <<<"$parsed"
    [[ -n $session_id && -n $cwd ]] || continue
    [[ -n $timestamp ]] || timestamp='1970-01-01T00:00:00.000Z'

    if [[ $metadata_branch == "$branch_name" ]]; then
      if [[ $timestamp > ${metadata_branch_timestamps[$cwd]:-} ]]; then
        metadata_branch_timestamps["$cwd"]=$timestamp
        metadata_branch_sessions["$cwd"]=$session_id
      fi
    fi

    if ! git -C "$cwd" rev-parse --git-dir >/dev/null 2>&1; then
      continue
    fi
    remote=$(git -C "$cwd" remote get-url origin 2>/dev/null || true)
    if [[ $remote != *"github.com:$repo.git" &&
      $remote != *"github.com/$repo.git" &&
      $remote != *"github.com/$repo" ]]; then
      continue
    fi
    live_branch=$(git -C "$cwd" branch --show-current 2>/dev/null || true)
    live_head=$(git -C "$cwd" rev-parse HEAD 2>/dev/null || true)
    if [[ $live_branch == "$branch_name" ]]; then
      if [[ $timestamp > ${branch_timestamps[$cwd]:-} ]]; then
        branch_timestamps["$cwd"]=$timestamp
        branch_sessions["$cwd"]=$session_id
      fi
    fi
    if [[ $live_head == "$head_sha" ]]; then
      if [[ $timestamp > ${head_timestamps[$cwd]:-} ]]; then
        head_timestamps["$cwd"]=$timestamp
        head_sessions["$cwd"]=$session_id
      fi
    fi
  done < <(
    find "$claude_projects_root" -mindepth 1 -maxdepth 1 -type d \
      \( -name "*-${name}" -o -name "*-${name}--*" \) \
      -print0 2>/dev/null |
      while IFS= read -r -d '' project_dir; do
        find "$project_dir" -maxdepth 1 -type f -name '*.jsonl' -print0 2>/dev/null
      done
  )

  for key in branch_sessions head_sessions metadata_branch_sessions; do
    local -n candidates=$key
    if (( ${#candidates[@]} == 1 )); then
      printf '%s\t%s\n' "${candidates[@]}" "${!candidates[@]}"
      return 0
    fi
    if (( ${#candidates[@]} > 1 )); then
      return 1
    fi
  done
  return 1
}

claude_session_file() {
  local session_id=$1
  local session_file

  session_file=$(
    find "$claude_projects_root" -mindepth 1 -maxdepth 1 -type d \
      \( -name "*-${name}" -o -name "*-${name}--*" \) \
      -print0 2>/dev/null |
      while IFS= read -r -d '' project_dir; do
        find "$project_dir" -maxdepth 1 -type f -name "${session_id}.jsonl" -print 2>/dev/null
      done |
      head -n 1
  )
  [[ -n $session_file ]] || return 1
  printf '%s\n' "$session_file"
}

claude_session_state() {
  local session_id=$1
  local session_file=${2:-}
  local last_op last_stop mtime now age

  if jq -e --arg id "$session_id" '
      any(.[]; .sessionId == $id and ((.state // "") != "done"))
    ' <<<"$(claude_agents_json)" >/dev/null 2>&1; then
    printf 'running\n'
    return 0
  fi

  if [[ -z $session_file ]]; then
    session_file=$(claude_session_file "$session_id" 2>/dev/null || true)
  fi
  if [[ -z $session_file || ! -f $session_file ]]; then
    printf 'unknown\n'
    return 0
  fi

  last_op=$(
    jq -r 'select(.type == "queue-operation") | .operation' "$session_file" 2>/dev/null |
      tail -n 1
  )
  if [[ $last_op == enqueue ]]; then
    printf 'running\n'
    return 0
  fi

  last_stop=$(
    jq -r 'select(.type == "assistant") | .message.stop_reason // empty' \
      "$session_file" 2>/dev/null |
      tail -n 1
  )
  mtime=$(stat -c %Y "$session_file" 2>/dev/null || printf '0')
  now=$(date +%s)
  age=$((now - mtime))
  if [[ $last_stop == tool_use ]] && (( age < 120 )); then
    printf 'running\n'
    return 0
  fi
  printf 'idle\n'
}
