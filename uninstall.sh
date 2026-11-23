#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail

data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
bin_dir=${XDG_BIN_HOME:-"$HOME/.local/bin"}
lib_dir="$data_home/coderabbit-review-queue"
applications_dir="$data_home/applications"
icons_dir="$data_home/icons/hicolor"
state_dir="${XDG_STATE_HOME:-"$HOME/.local/state"}/coderabbit-review-queue"

systemd_user_dir="${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user"
# Disable a unit only if systemd runs the file this removes. Disabling the
# monitors unit does not stop running monitors (KillMode=process).
for unit in coderabbit-review-queue-web.service coderabbit-review-queue-monitors.service; do
  if command -v systemctl >/dev/null 2>&1 &&
    [[ $(systemctl --user show -p FragmentPath --value "$unit" 2>/dev/null) == "$systemd_user_dir/$unit" ]]; then
    systemctl --user disable --now "$unit" >/dev/null 2>&1 || true
  fi
done
rm -f \
  "$bin_dir/coderabbit-review-queue" \
  "$bin_dir/coderabbit-review-queue-uninstall" \
  "$applications_dir/coderabbit-review-queue.desktop" \
  "$lib_dir/coderabbit-review-queue" \
  "$lib_dir/coderabbit-review-queue-gui.py" \
  "$lib_dir/coderabbit-review-queue-web.py" \
  "$systemd_user_dir/coderabbit-review-queue-web.service" \
  "$systemd_user_dir/coderabbit-review-queue-monitors.service" \
  "$lib_dir/t3-delegate.py" \
  "$lib_dir/codex-steer.py" \
  "$lib_dir/claude-steer.py" \
  "$lib_dir/coderabbit-logomark.svg" \
  "$lib_dir/coderabbit-logomark-16.png" \
  "$lib_dir/coderabbit-logomark-22.png" \
  "$lib_dir/coderabbit-logomark-24.png" \
  "$lib_dir/coderabbit-logomark-32.png" \
  "$lib_dir/coderabbit-logomark-48.png" \
  "$lib_dir/coderabbit-logomark-64.png" \
  "$lib_dir/uninstall.sh"
rm -f "$lib_dir"/queue_gui_*.py "$lib_dir/queue_icon_detection.py"
rm -f "$lib_dir"/lib/*.sh
rm -f "$lib_dir"/lib/*.jq
rmdir "$lib_dir/lib" 2>/dev/null || true
rm -f "$lib_dir"/__pycache__/queue_gui_*.pyc "$lib_dir"/__pycache__/queue_icon_detection.*.pyc
rmdir "$lib_dir/__pycache__" 2>/dev/null || true
for size in 16 22 24 32 48 64; do
  rm -f "$icons_dir/${size}x${size}/apps/coderabbit-review-queue.png"
done
rm -f "$icons_dir/scalable/apps/coderabbit-review-queue.svg"
rmdir "$lib_dir" 2>/dev/null || true

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t "$icons_dir" >/dev/null 2>&1 || true
fi

printf 'Uninstalled CodeRabbit Review Queue.\n'
printf 'Runtime state was preserved at %s\n' "$state_dir"
