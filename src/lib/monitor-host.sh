# SPDX-License-Identifier: GPL-3.0-or-later

# Settings that follow the monitor to its host. The GUI pushes them on change.
monitor_setting_suffixes=(
  agent-host archive-after-merge auto-delegate auto-merge delegation-prompt-mode
  delegation-prompt-template delete-branch excluded-authors excluded-branches
  excluded-titles ignore-drafts merge-admin merge-after-approval merge-after-delegation
  merge-method new-items-at-top notifications notify-sound notify-sound-path notify-sound-volume
  requeued-items-at-top resolve-merge-conflicts steer-running-agents stop-when-empty
  wait-for-other-bots
)
# Queue progress that moves with the monitor.
monitor_runtime_suffixes=(
  archived-heads.tsv delegated-prs.txt order.txt pending-archives.tsv quota-expiry.txt
  requeued-heads.tsv review-completion.tsv review-history.tsv review-requests.tsv
  routed-threads.txt
)
# Files the GUI mirrors from a remote monitor to show its live state.
monitor_mirror_suffixes=(monitor-state.tsv order.txt review-completion.tsv review-requests.tsv)

monitor_host() {
  local host=''
  if (( force_local_monitor )); then
    printf '\n'
    return 0
  fi
  [[ ! -f $monitor_host_file ]] || host=$(head -n 1 "$monitor_host_file")
  [[ -z $host || $host =~ ^[A-Za-z0-9_.@-]+$ ]] || return 1
  printf '%s\n' "$host"
}

machine_id() {
  cat /etc/machine-id 2>/dev/null || hostname
}

# Devices with the queue installed, configured in the GUI's Devices dialog.
queue_hosts() {
  [[ -f $state_root/queue-hosts ]] || return 0
  grep -E '^[A-Za-z0-9_.@-]+$' "$state_root/queue-hosts" | awk '!seen[$0]++'
}

add_queue_host() {
  local temporary
  [[ -n $1 ]] && ! queue_hosts | grep -qxF -- "$1" || return 0
  mkdir -p "$state_root"
  temporary=$(mktemp "$state_root/queue-hosts.XXXXXX")
  { queue_hosts; printf '%s\n' "$1"; } >"$temporary"
  mv -- "$temporary" "$state_root/queue-hosts"
}

# Prints the SSH config hosts that have the queue installed.
detect_queue_hosts() {
  local host probes file
  probes=$(mktemp -d "$runtime_root/coderabbit-review-queue-probes.XXXXXX")
  while IFS= read -r host; do
    (
      timeout 15 ssh -o BatchMode=yes -o ConnectTimeout=5 -- "$host" bash -s \
        <<<'[[ -x ${XDG_BIN_HOME:-$HOME/.local/bin}/coderabbit-review-queue ]]' \
        >/dev/null 2>&1 && touch "$probes/$host"
    ) &
  done < <(ssh_config_hosts)
  wait
  for file in "$probes"/*; do
    [[ -f $file ]] && printf '%s\n' "${file##*/}"
  done
  rm -rf -- "$probes"
}

ssh_config_hosts() {
  [[ -f $HOME/.ssh/config ]] || return 0
  awk 'tolower($1) == "host" {
    for (i = 2; i <= NF; i++) if ($i !~ /[*?!\[\]]/ && !seen[$i]++) print $i
  }' "$HOME/.ssh/config"
}

# Runs the installed queue on HOST for this repository. Set remote_stdin to
# pass data to its standard input.
ssh_queue_command() {
  local host=$1 command arguments
  shift
  local -a timeout_args=()
  printf -v arguments '%q ' --repo "$repo" "$@"
  command='queue="${XDG_BIN_HOME:-$HOME/.local/bin}/coderabbit-review-queue"; [[ -x $queue ]] || exit 127'$'\n'
  if [[ -n ${remote_stdin:-} ]]; then
    command+="base64 -d <<'CRQ_STDIN' | \"\$queue\" $arguments"$'\n'
    command+="$(base64 -w0 <<<"$remote_stdin")"$'\nCRQ_STDIN\n'
  else
    command+="exec \"\$queue\" $arguments"
  fi
  [[ -z ${remote_command_timeout:-} ]] || timeout_args=(timeout "$remote_command_timeout")
  # Feed the command to bash on stdin: printf %q quoting is bash syntax, and
  # the remote login shell (fish, for example) only has to run `bash -s`.
  "${timeout_args[@]}" ssh \
    -o BatchMode=yes \
    -o ConnectTimeout=5 \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=2 \
    -- "$host" bash -s <<<"$command"
}

local_monitor_pid() {
  local pid
  pid=$(head -n 1 "$monitor_pid_file" 2>/dev/null) || return 1
  [[ $pid =~ ^[0-9]+$ ]] || return 1
  # A stale pid file can name a reused pid; require the queue script itself.
  tr '\0' '\n' <"/proc/$pid/cmdline" 2>/dev/null |
    grep -qE '(^|/)coderabbit-review-queue$' || return 1
  printf '%s\n' "$pid"
}

monitor_info() {
  local pid suffix file files='{}'
  pid=$(local_monitor_pid) || pid=''
  for suffix in "${monitor_mirror_suffixes[@]}"; do
    file="$state_root/$repo_key-$suffix"
    [[ -f $file ]] || continue
    files=$(jq -c --arg name "$suffix" --rawfile text "$file" \
      --argjson mtime "$(stat -c %Y "$file")" '. + {($name): {text: $text, mtime: $mtime}}' \
      <<<"$files")
  done
  jq -cn --arg machine "$(machine_id)" --arg host "$(hostname)" --arg pid "$pid" \
    --argjson files "$files" \
    '{machineId: $machine, hostname: $host, pid: ($pid | if . == "" then null else tonumber end), files: $files}'
}

export_monitor_state() {
  local scope=$1 suffix file json='{}'
  local -a suffixes=("${monitor_setting_suffixes[@]}")
  [[ $scope == settings ]] || suffixes+=("${monitor_runtime_suffixes[@]}")
  for suffix in "${suffixes[@]}"; do
    file="$state_root/$repo_key-$suffix"
    if [[ -f $file ]]; then
      json=$(jq -c --arg name "$suffix" --rawfile text "$file" '. + {($name): $text}' <<<"$json")
    else
      json=$(jq -c --arg name "$suffix" '. + {($name): null}' <<<"$json")
    fi
  done
  printf '%s\n' "$json"
}

import_monitor_state() {
  local json name file temporary lock_fd
  json=$(cat)
  jq -e 'type == "object"' <<<"$json" >/dev/null 2>&1 || {
    printf 'Invalid monitor state for %s.\n' "$repo" >&2
    return 1
  }
  mkdir -p "$state_root"
  while IFS= read -r name; do
    if [[ " ${monitor_setting_suffixes[*]} ${monitor_runtime_suffixes[*]} " != *" $name "* ]]; then
      printf 'Skipping unknown state file %s.\n' "$name" >&2
      continue
    fi
    file="$state_root/$repo_key-$name"
    # The monitor rewrites the queue order under this lock.
    lock_fd=''
    if [[ $name == order.txt ]]; then
      exec {lock_fd}>"$queue_order_file.lock"
      flock -x "$lock_fd"
    fi
    if jq -e --arg name "$name" '.[$name] == null' <<<"$json" >/dev/null; then
      rm -f -- "$file"
    else
      temporary=$(mktemp "$file.XXXXXX")
      jq -j --arg name "$name" '.[$name]' <<<"$json" >"$temporary"
      mv -- "$temporary" "$file"
    fi
    [[ -z $lock_fd ]] || exec {lock_fd}>&-
  done < <(jq -r 'keys[]' <<<"$json")
}

# The GUI sends the PRs it shows. PRs it does not show, such as ones in review,
# keep their saved slots; the shown PRs fill theirs in the new order.
set_queue_order() {
  local temporary lock_fd saved=/dev/null
  exec {lock_fd}>"$queue_order_file.lock"
  flock -x "$lock_fd"
  [[ ! -f $queue_order_file ]] || saved=$queue_order_file
  temporary=$(mktemp "$queue_order_file.XXXXXX")
  { (( $# == 0 )) || printf '%s\n' "$@"; } | awk '
    FNR == NR { if (!($0 in shown)) { shown[$0] = 1; order[++count] = $0 }; next }
    seen[$0]++ { next }
    $0 in shown { print order[++next_index]; next }
    { print }
    END { while (next_index < count) print order[++next_index] }
  ' - "$saved" >"$temporary"
  mv -- "$temporary" "$queue_order_file"
  exec {lock_fd}>&-
}

# Settings for another device: an empty agent host means "this device" and
# would point the remote monitor at its own agents, so keep the remote value.
portable_settings() {
  export_monitor_state settings |
    jq -c 'if ((.["agent-host"] // "") | test("\\S")) then . else del(.["agent-host"]) end'
}

push_monitor_settings() {
  local host
  host=$(monitor_host) || return 2
  [[ -n $host ]] || return 0
  remote_stdin=$(portable_settings) ssh_queue_command "$host" --import-state --local-monitor
}

# Prints "HOST<TAB>MACHINE-ID<TAB>PID" for every configured device that runs a
# monitor for this repository. Unreachable devices are skipped.
monitors_elsewhere() {
  local host file probes local_id
  local_id=$(machine_id)
  probes=$(mktemp -d "$runtime_root/coderabbit-review-queue-probes.XXXXXX")
  while IFS= read -r host; do
    (
      status=0
      remote_command_timeout=15 ssh_queue_command "$host" --monitor-info --local-monitor \
        >"$probes/$host" 2>/dev/null </dev/null || status=$?
      (( status == 0 )) || rm -f -- "$probes/$host"
      # 127: no queue installed; 255: SSH failed. Usage errors mean an old queue.
      if (( status == 2 )); then
        printf 'Could not check %s: its queue is too old to report monitors.\n' "$host" >&2
      fi
    ) &
  done < <(queue_hosts)
  wait
  for file in "$probes"/*; do
    [[ -f $file ]] || continue
    host=${file##*/}
    jq -r --arg local "$local_id" --arg host "$host" '
      select(.pid != null and .machineId != $local)
      | [$host, .machineId, .pid] | @tsv' "$file" 2>/dev/null || true
  done | sort -t $'\t' -k2,2 -u
  rm -rf -- "$probes"
}

monitor_location() {
  if [[ -n $1 ]]; then printf '%s\n' "$1"; else printf 'this device\n'; fi
}

# Refuses when a device other than the allowed machine IDs runs this monitor.
refuse_monitors_elsewhere() {
  local host id pid found=0
  while IFS=$'\t' read -r host id pid; do
    [[ -n $host ]] || continue
    [[ " $* " != *" $id "* ]] || continue
    printf 'A monitor for %s already runs on %s (PID %s). Stop it or move it from there first.\n' \
      "$repo" "$host" "$pid" >&2
    found=1
  done < <(monitors_elsewhere)
  (( found == 0 ))
}

start_local_monitor() {
  local pid attempt log="$state_root/$repo_key-monitor.log"
  if pid=$(local_monitor_pid); then
    printf 'The monitor for %s is already running on %s (PID %s).\n' "$repo" "$(hostname)" "$pid"
    return 0
  fi
  mkdir -p "$state_root"
  setsid -f "$script_path" --repo "$repo" --local-monitor </dev/null >>"$log" 2>&1
  for attempt in {1..50}; do
    if pid=$(local_monitor_pid); then
      printf 'Started the monitor for %s on %s (PID %s).\n' "$repo" "$(hostname)" "$pid"
      return 0
    fi
    sleep 0.1
  done
  printf 'The monitor for %s did not start on %s; see %s.\n' "$repo" "$(hostname)" "$log" >&2
  return 1
}

stop_local_monitor() {
  local pid attempt target
  # A deliberate stop: do not start it again after a reboot.
  rm -f -- "$monitor_wanted_file"
  if ! pid=$(local_monitor_pid); then
    printf 'The monitor for %s is not running on %s.\n' "$repo" "$(hostname)"
    return 0
  fi
  # Monitors started by the queue lead their own process group; stop the
  # waiting children with them. Otherwise stop only the monitor itself.
  target=$pid
  [[ $(ps -o pgid= -p "$pid" | tr -d ' ') != "$pid" ]] || target=-$pid
  kill -TERM -- "$target" 2>/dev/null || true
  for attempt in {1..100}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      printf 'Stopped the monitor for %s on %s.\n' "$repo" "$(hostname)"
      return 0
    fi
    sleep 0.25
  done
  kill -KILL -- "$target" 2>/dev/null || true
  rm -f -- "$monitor_pid_file" "$monitor_state_file"
  printf 'Stopped the monitor for %s on %s after it ignored SIGTERM.\n' "$repo" "$(hostname)"
}

start_monitor_command() {
  local host info target_id
  host=$(monitor_host) || return 2
  if [[ -z $host ]]; then
    (( force_local_monitor )) || refuse_monitors_elsewhere "$(machine_id)" || return 1
    start_local_monitor
    return
  fi
  info=$(ssh_queue_command "$host" --monitor-info --local-monitor) || {
    printf 'Could not reach the queue on %s.\n' "$host" >&2
    return 1
  }
  target_id=$(jq -r '.machineId' <<<"$info")
  refuse_monitors_elsewhere "$target_id" || return 1
  ssh_queue_command "$host" --start-monitor --local-monitor
}

set_monitor_host() {
  local temporary
  if [[ -z $1 ]]; then
    rm -f -- "$monitor_host_file"
    return 0
  fi
  temporary=$(mktemp "$monitor_host_file.XXXXXX")
  printf '%s\n' "$1" >"$temporary"
  mv -- "$temporary" "$monitor_host_file"
}

move_monitor() {
  local target=$1 only_running=${2:-0} source info source_id target_id target_pid local_id state was_running=0
  [[ $target != local ]] || target=''
  [[ -z $target || $target =~ ^[A-Za-z0-9_.@-]+$ ]] || {
    printf 'Use an SSH config alias or a host name without spaces.\n' >&2
    return 2
  }
  source=$(monitor_host) || return 2
  if [[ $source == "$target" ]]; then
    printf 'The monitor for %s already belongs to %s.\n' "$repo" "$(monitor_location "$target")"
    return 0
  fi
  local_id=$(machine_id)
  source_id=$local_id
  if [[ -n $source ]]; then
    info=$(ssh_queue_command "$source" --monitor-info --local-monitor) || {
      printf 'Could not reach the monitor host %s.\n' "$source" >&2
      return 1
    }
    source_id=$(jq -r '.machineId' <<<"$info")
    [[ $(jq -r '.pid' <<<"$info") == null ]] || was_running=1
  elif local_monitor_pid >/dev/null; then
    was_running=1
  fi
  if (( only_running && ! was_running )); then
    printf 'The monitor for %s is not running; it stays on %s.\n' "$repo" "$(monitor_location "$source")"
    return 0
  fi
  target_id=$local_id
  if [[ -n $target ]]; then
    info=$(ssh_queue_command "$target" --monitor-info --local-monitor) || {
      printf 'Could not reach the queue on %s. Install it there first.\n' "$target" >&2
      return 1
    }
    target_id=$(jq -r '.machineId' <<<"$info")
    if [[ $target_id == "$local_id" ]]; then
      printf '%s is this device; choose this device instead.\n' "$target" >&2
      return 1
    fi
    target_pid=$(jq -r '.pid // empty' <<<"$info")
  else
    target_pid=$(local_monitor_pid) || target_pid=''
  fi
  # A monitor already running there owns newer state; adopt it instead of
  # overwriting it with this copy.
  if [[ -n $target_pid ]]; then
    refuse_monitors_elsewhere "$source_id" "$target_id" || return 1
    if (( was_running )); then
      printf 'Monitors for %s run on both %s and %s. Stop one first.\n' \
        "$repo" "$(monitor_location "$source")" "$(monitor_location "$target")" >&2
      return 1
    fi
    set_monitor_host "$target"
    add_queue_host "$target"
    printf 'The monitor for %s already runs on %s (PID %s); it now belongs there.\n' \
      "$repo" "$(monitor_location "$target")" "$target_pid"
    return 0
  fi
  if [[ -n $target ]]; then
    if [[ -z $(agent_host) ]]; then
      printf 'Agent tasks for %s run on this device. Set the agent host to this device'"'"'s SSH name first, so the monitor on %s can reach them.\n' \
        "$repo" "$target" >&2
      return 1
    fi
  fi
  refuse_monitors_elsewhere "$source_id" "$target_id" || return 1

  if [[ -n $source ]]; then
    if (( was_running )); then
      ssh_queue_command "$source" --stop-monitor --local-monitor || return 1
    fi
    state=$(ssh_queue_command "$source" --export-state --local-monitor) || return 1
  else
    (( was_running == 0 )) || stop_local_monitor || return 1
    state=$(export_monitor_state all)
  fi
  [[ -n $target ]] || state=$(jq -c 'del(.["agent-host"])' <<<"$state")
  if [[ -n $target ]]; then
    remote_stdin=$state ssh_queue_command "$target" --import-state --local-monitor
  else
    import_monitor_state <<<"$state"
  fi || {
    printf 'Could not copy the monitor state to %s.\n' "$(monitor_location "$target")" >&2
    if (( was_running )); then
      if [[ -n $source ]]; then
        ssh_queue_command "$source" --start-monitor --local-monitor || true
      else
        start_local_monitor || true
      fi
    fi
    return 1
  }
  set_monitor_host "$target"
  add_queue_host "$target"
  printf 'The monitor for %s now belongs to %s.\n' "$repo" "$(monitor_location "$target")"
  (( was_running )) || return 0
  if [[ -n $target ]]; then
    ssh_queue_command "$target" --start-monitor --local-monitor
  else
    start_local_monitor
  fi
}

# Makes one device's settings for this repository match another's. The queue
# state stays with the monitor.
sync_settings() {
  local host=$1 direction=$2 state
  [[ $host =~ ^[A-Za-z0-9_.@-]+$ ]] || {
    printf 'Use an SSH config alias or a host name without spaces.\n' >&2
    return 2
  }
  case $direction in
    send) state=$(portable_settings) ;;
    receive)
      state=$(ssh_queue_command "$host" --export-settings --local-monitor) || {
        printf 'Could not read the settings for %s on %s.\n' "$repo" "$host" >&2
        return 1
      }
      # An empty agent host there means that device; keep this device's own.
      state=$(jq -c 'if ((.["agent-host"] // "") | test("\\S")) then . else del(.["agent-host"]) end' \
        <<<"$state") || return 1
      ;;
    *)
      printf 'The sync direction must be send or receive.\n' >&2
      return 2
      ;;
  esac
  # Never let a device without settings for this repository wipe the other's.
  if jq -e 'all(.[]; . == null)' <<<"$state" >/dev/null; then
    printf 'No settings for %s on %s; nothing to sync.\n' "$repo" \
      "$([[ $direction == send ]] && printf 'this device' || printf '%s' "$host")"
    return 0
  fi
  if [[ $direction == send ]]; then
    remote_stdin=$state ssh_queue_command "$host" --import-state --local-monitor || return 1
    printf 'Sent the settings for %s to %s.\n' "$repo" "$host"
  else
    import_monitor_state <<<"$state" || return 1
    printf 'Received the settings for %s from %s.\n' "$repo" "$host"
  fi
}

# Commands about the monitor and its state run where the monitor runs.
forward_to_monitor_host() {
  local host
  host=$(monitor_host) || {
    printf 'Invalid monitor host configured for %s.\n' "$repo" >&2
    return 2
  }
  [[ -n $host ]] || return 3
  ssh_queue_command "$host" "$@" --local-monitor
}

# After a rename on GitHub, move OLD's monitor, settings, and queue state to the
# new name: on the monitor's device, then on this one. A running monitor stops
# and starts again under the new name. Running it again changes nothing.
rename_repo() {
  local new=$1 canonical host old_key file new_file was_running=0
  if ! canonical=$(gh repo view "$repo" --json nameWithOwner --jq .nameWithOwner 2>/dev/null); then
    printf 'Repository %s could not be found or accessed.\n' "$repo" >&2
    return 1
  fi
  if [[ ${canonical,,} != "${new,,}" ]]; then
    printf 'GitHub names %s as %s, not %s.\n' "$repo" "$canonical" "$new" >&2
    return 1
  fi
  new=$canonical
  host=$(monitor_host) || return 2
  if [[ -n $host ]]; then
    ssh_queue_command "$host" --rename-repo "$new" --local-monitor || {
      printf 'Could not rename the monitor state on %s.\n' "$host" >&2
      return 1
    }
  elif local_monitor_pid >/dev/null; then
    was_running=1
    stop_local_monitor >/dev/null
  fi
  old_key=$repo_key
  (
    shopt -s nullglob
    for file in "$state_root/$old_key"-*; do
      [[ ${file:${#state_root}+${#old_key}+2} =~ ^($repo_state_suffixes)$ ]] || continue
      new_file="$state_root/${new//\//__}-${file:${#state_root}+${#old_key}+2}"
      [[ $file == "$new_file" ]] || mv -f -- "$file" "$new_file"
    done
    # The old name's lock files guard nothing once its monitor stopped.
    for file in "$state_root/$old_key"-*.lock; do
      if [[ $old_key != "${new//\//__}" &&
        ${file:${#state_root}+${#old_key}+2} =~ ^($repo_state_suffixes)(\.[a-z]+)*\.lock$ ]]; then
        rm -f -- "$file"
      fi
    done
  )
  rm -f -- "$monitor_pid_file" "$monitor_lock_file" "$runtime_root/coderabbit-review-queue-$old_key-routing.lock"
  local old=$repo
  configure_repo "$new"
  if (( was_running )); then
    start_local_monitor >/dev/null || return 1
  fi
  printf 'Renamed %s to %s on %s.\n' "$old" "$repo" "$(hostname)"
}

# Start the monitors that ran here before a reboot or crash. Each monitor marks
# itself in monitor-wanted while it runs; stopping it on purpose clears that.
resume_monitors() {
  local file key host status=0
  shopt -s nullglob
  for file in "$state_root"/*__*-monitor-wanted; do
    key=${file##*/}
    key=${key%-monitor-wanted}
    [[ $key =~ ^[A-Za-z0-9-]+__[A-Za-z0-9_.-]+$ ]] || continue
    # A monitor that moved to another device belongs there.
    host=''
    [[ ! -f $state_root/$key-monitor-host ]] || host=$(head -n 1 "$state_root/$key-monitor-host")
    [[ -z $host ]] || continue
    "$script_path" --repo "${key%%__*}/${key#*__}" --start-monitor --local-monitor || status=1
  done
  return "$status"
}
