#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail

data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
bin_dir=${XDG_BIN_HOME:-"$HOME/.local/bin"}
lib_dir="$data_home/coderabbit-review-queue"
applications_dir="$data_home/applications"
state_dir="${XDG_STATE_HOME:-"$HOME/.local/state"}/coderabbit-review-queue"

rm -f \
  "$bin_dir/coderabbit-review-queue" \
  "$bin_dir/coderabbit-review-queue-uninstall" \
  "$applications_dir/coderabbit-review-queue.desktop" \
  "$lib_dir/coderabbit-review-queue" \
  "$lib_dir/coderabbit-review-queue-gui.py" \
  "$lib_dir/uninstall.sh"
rmdir "$lib_dir" 2>/dev/null || true

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi

printf 'Uninstalled CodeRabbit Review Queue.\n'
printf 'Runtime state was preserved at %s\n' "$state_dir"
