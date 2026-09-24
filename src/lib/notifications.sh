# SPDX-License-Identifier: GPL-3.0-only

notify_sound_enabled() {
  # Default on: missing file or anything other than 0.
  if [[ -f $notify_sound_file ]]; then
    [[ $(head -n 1 "$notify_sound_file") != 0 ]]
  else
    return 0
  fi
}

play_notification_sound() {
  local sound_file volume percent volume_scalar

  volume=${sound_volume_override:-}
  if [[ -z $volume && -f $notify_sound_volume_file ]]; then
    volume=$(head -n 1 "$notify_sound_volume_file")
  fi
  [[ $volume =~ ^[0-9]+$ ]] || volume=30
  (( volume > 100 )) && volume=100
  percent=$((volume * 65536 / 100))
  volume_scalar=$(awk -v volume="$volume" 'BEGIN { printf "%.2f", volume / 100 }')

  if [[ -n $sound_path_override ]]; then
    sound_file=$sound_path_override
  elif [[ -s $notify_sound_path_file ]]; then
    sound_file=$(head -n 1 "$notify_sound_path_file")
  else
    sound_file=''
  fi

  if [[ -n $sound_file && -f $sound_file ]]; then
    if command -v pw-play >/dev/null 2>&1; then
      pw-play --volume "$volume_scalar" "$sound_file" \
        >/dev/null 2>&1 && return
    fi
    if command -v paplay >/dev/null 2>&1; then
      paplay --volume="$percent" "$sound_file" >/dev/null 2>&1 && return
    fi
  fi

  for sound_file in \
    /usr/share/sounds/freedesktop/stereo/complete.oga \
    /usr/share/sounds/ocean/stereo/dialog-information.oga \
    /usr/share/sounds/oxygen/stereo/dialog-information.ogg; do
    [[ -f $sound_file ]] || continue
    if command -v pw-play >/dev/null 2>&1; then
      pw-play --volume "$volume_scalar" "$sound_file" \
        >/dev/null 2>&1 && return
    fi
    if command -v paplay >/dev/null 2>&1; then
      paplay --volume="$percent" "$sound_file" >/dev/null 2>&1 && return
    fi
  done

  # Event sounds can be disabled or unmapped by the active desktop theme, so
  # use libcanberra only when no sound file/player combination is available.
  if command -v canberra-gtk-play >/dev/null 2>&1; then
    canberra-gtk-play --id=complete \
      --description='CodeRabbit review available' >/dev/null 2>&1
  fi
}

desktop_notify() {
  local title=$1
  local message=$2
  local urgency=${3:-normal}
  local with_sound=${4:-0}
  local app_icon
  app_icon="$script_dir/coderabbit-logomark.svg"
  [[ -f $app_icon ]] || app_icon="$script_dir/../assets/icons/coderabbit-logomark.svg"
  [[ -f $app_icon ]] || app_icon='system-software-update'
  local -a args=(
    --app-name='CodeRabbit Review Queue'
    --app-icon="$app_icon"
    --hint='string:desktop-entry:coderabbit-review-queue'
    # Keep Plasma/libnotify from playing their own sound; we only ding
    # intentionally via play_notification_sound when with_sound=1.
    --hint='boolean:suppress-sound:true'
    --urgency="$urgency"
  )

  notify-send \
    "${args[@]}" \
    "$title" \
    "$message" >/dev/null 2>&1 || true

  if [[ $with_sound == 1 ]] && notify_sound_enabled; then
    play_notification_sound || true
  fi
}

github_low_quota() {
  gh api rate_limit 2>/dev/null |
    jq -r --argjson threshold "$github_quota_threshold" '
      [
        {
          name: "REST",
          remaining: .resources.core.remaining,
          reset: .resources.core.reset
        },
        {
          name: "GraphQL",
          remaining: .resources.graphql.remaining,
          reset: .resources.graphql.reset
        }
      ]
      | map(select(.remaining <= $threshold))
      | if length == 0 then
          empty
        else
          max_by(.reset)
          | [.name, .remaining, .reset]
          | @tsv
        end
    '
}

wait_for_github_quota() {
  local row name remaining reset now delay wake

  while true; do
    row=$(github_low_quota) || return 0
    [[ -n $row ]] || return 0
    IFS=$'\t' read -r name remaining reset <<<"$row"
    now=$(date -u +%s)
    delay=$((reset - now + 10))
    (( delay >= 30 )) || delay=30
    wake=$(human_expiry "$((now + delay))")
    printf 'GitHub %s quota is low (%s remaining); waiting until %s.\n' \
      "$name" "$remaining" "$wake" >&2
    desktop_notify \
      'GitHub API quota low' \
      "$name has $remaining requests remaining. The $repo monitor will resume $wake."
    sleep "$delay"
  done
}
