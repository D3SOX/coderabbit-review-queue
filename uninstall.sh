#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail

data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
bin_dir=${XDG_BIN_HOME:-"$HOME/.local/bin"}
lib_dir="$data_home/coderabbit-review-queue"
applications_dir="$data_home/applications"
icons_dir="$data_home/icons/hicolor"
state_dir="${XDG_STATE_HOME:-"$HOME/.local/state"}/coderabbit-review-queue"

rm -f \
  "$bin_dir/coderabbit-review-queue" \
  "$bin_dir/coderabbit-review-queue-uninstall" \
  "$applications_dir/coderabbit-review-queue.desktop" \
  "$lib_dir/coderabbit-review-queue" \
  "$lib_dir/coderabbit-review-queue-gui.py" \
  "$lib_dir/t3-delegate.py" \
  "$lib_dir/coderabbit-logomark.svg" \
  "$lib_dir/coderabbit-logomark-16.png" \
  "$lib_dir/coderabbit-logomark-22.png" \
  "$lib_dir/coderabbit-logomark-24.png" \
  "$lib_dir/coderabbit-logomark-32.png" \
  "$lib_dir/coderabbit-logomark-48.png" \
  "$lib_dir/coderabbit-logomark-64.png" \
  "$lib_dir/uninstall.sh"
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
