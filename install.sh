#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
icons_source_dir="$source_dir/assets/icons"
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
bin_dir=${XDG_BIN_HOME:-"$HOME/.local/bin"}
lib_dir="$data_home/coderabbit-review-queue"
applications_dir="$data_home/applications"
icons_dir="$data_home/icons/hicolor"
desktop_tmp=$(mktemp)

cleanup() {
  rm -f "$desktop_tmp"
}
trap cleanup EXIT

required=(gh jq git rg flock python3)
missing=()
for command_name in "${required[@]}"; do
  command -v "$command_name" >/dev/null 2>&1 ||
    missing+=("$command_name")
done
if (( ${#missing[@]} > 0 )); then
  printf 'Missing required commands: %s\n' "${missing[*]}" >&2
  exit 1
fi
if ! python3 -c 'import PySide6' >/dev/null 2>&1; then
  printf '%s\n' 'PySide6 is required for the GUI.' >&2
  exit 1
fi

install -d "$bin_dir" "$lib_dir" "$applications_dir"
install -m 755 \
  "$source_dir/src/coderabbit-review-queue" \
  "$lib_dir/coderabbit-review-queue"
install -m 755 \
  "$source_dir/src/coderabbit-review-queue-gui.py" \
  "$lib_dir/coderabbit-review-queue-gui.py"
install -m 755 \
  "$source_dir/src/t3-delegate.py" \
  "$lib_dir/t3-delegate.py"
install -m 644 \
  "$icons_source_dir/coderabbit-logomark.svg" \
  "$lib_dir/coderabbit-logomark.svg"
install -m 644 "$icons_source_dir"/coderabbit-logomark-*.png "$lib_dir/"
for size in 16 22 24 32 48 64; do
  icon_dir="$icons_dir/${size}x${size}/apps"
  install -d "$icon_dir"
  install -m 644 \
    "$icons_source_dir/coderabbit-logomark-$size.png" \
    "$icon_dir/coderabbit-review-queue.png"
done
install -d "$icons_dir/scalable/apps"
install -m 644 \
  "$icons_source_dir/coderabbit-logomark.svg" \
  "$icons_dir/scalable/apps/coderabbit-review-queue.svg"
install -m 755 \
  "$source_dir/uninstall.sh" \
  "$lib_dir/uninstall.sh"
ln -sfn "$lib_dir/coderabbit-review-queue" \
  "$bin_dir/coderabbit-review-queue"
ln -sfn "$lib_dir/uninstall.sh" \
  "$bin_dir/coderabbit-review-queue-uninstall"

escaped_exec="$bin_dir/coderabbit-review-queue"
escaped_exec=${escaped_exec//\\/\\\\}
escaped_exec=${escaped_exec//&/\\&}
escaped_exec=${escaped_exec//|/\\|}
sed "s|@EXEC@|$escaped_exec|" \
  "$source_dir/assets/coderabbit-review-queue.desktop.in" >"$desktop_tmp"
install -m 644 "$desktop_tmp" \
  "$applications_dir/coderabbit-review-queue.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t "$icons_dir" >/dev/null 2>&1 || true
fi

printf 'Installed CodeRabbit Review Queue.\n'
printf 'Command: %s/coderabbit-review-queue\n' "$bin_dir"
printf 'Uninstall command: %s/coderabbit-review-queue-uninstall\n' "$bin_dir"
printf 'Application entry: %s/coderabbit-review-queue.desktop\n' \
  "$applications_dir"
