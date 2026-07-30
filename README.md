# CodeRabbit Review Queue

A small Linux desktop tool that serializes CodeRabbit reviews across open
GitHub pull requests, waits for rate-limit windows, and keeps the queue visible
from a Qt GUI and system tray.

## Why this exists

This was built after managing several active pull requests made CodeRabbit's
rolling review limits difficult to track manually. Repeatedly checking timers,
remembering which commit had already been reviewed, and deciding which PR
should go next was error-prone. The tool turns that workflow into one
persistent, reorderable queue. Duplicate-comment protection was added later as
a safeguard for automated operation.

## Features

- Finds open PR heads that do not have a current CodeRabbit review.
- Uses CodeRabbit's own quota comment to determine the next review window.
- Adds a safety margin and checks quota again before triggering a review.
- Prevents recent duplicate `@coderabbitai review` comments.
- Handles review requests sequentially and waits for completion.
- Supports separate monitors and settings for multiple repositories.
- Shows unresolved CodeRabbit threads and optional Codex task progress.
- Can optionally delegate unresolved feedback to a matching idle Codex task.
- Retries transient GitHub failures and pauses near GitHub API quota limits.
- Provides desktop notifications and a Plasma-compatible system-tray timer.

## Requirements

Required:

- Linux with Bash 4 or newer
- Python 3 and PySide6
- [GitHub CLI](https://cli.github.com/) authenticated with `gh auth login`
- `jq`, `git`, `ripgrep`, and `flock`

Recommended:

- `notify-send` from libnotify for desktop notifications
- Codex CLI and Codex Desktop only when using Codex task integration

The GitHub token remains managed by `gh`; the tool does not read or store it.

## Install

```bash
git clone <repository-url>
cd coderabbit-review-queue
./install.sh
```

The installer uses only user-owned locations:

- executable: `~/.local/bin/coderabbit-review-queue`
- application files: `${XDG_DATA_HOME:-~/.local/share}`
- runtime state: `${XDG_STATE_HOME:-~/.local/state}/coderabbit-review-queue`

Make sure `~/.local/bin` is in the desktop session's `PATH`.

## Uninstall

From the cloned repository:

```bash
./uninstall.sh
```

Or, after installation:

```bash
coderabbit-review-queue-uninstall
```

Uninstalling preserves runtime state and settings under the XDG state
directory.

## Usage

Open **CodeRabbit Review Queue** from the application launcher, or run:

```bash
coderabbit-review-queue --gui
```

The CLI can also target one repository directly:

```bash
coderabbit-review-queue --repo OWNER/REPOSITORY
```

Useful read-only commands:

```bash
coderabbit-review-queue --repo OWNER/REPOSITORY --status
coderabbit-review-queue --list-repos
```

Repository discovery uses GitHub App installation information visible to the
authenticated account. An `OWNER/REPOSITORY` value can also be entered
manually.

Automatic Codex delegation is disabled by default. When enabled for a
repository, the tool only resumes a matching task when it appears idle and
checks its state again immediately before dispatch.

## Privacy

The source contains no account identifiers, credentials, analytics, or
telemetry.

Network access goes through the authenticated `gh` CLI to GitHub. CodeRabbit is
controlled through comments and statuses on GitHub. If Codex delegation is
enabled, the Codex CLI performs its normal network activity.

For optional Codex integration, the tool scans local Codex session metadata to
match a PR branch or head commit to a task. It displays the latest task progress
locally. Session contents are not copied into this repository or into the
tool's state directory.

Runtime state can include:

- repository names and queue order
- PR numbers, commit hashes, and CodeRabbit review-thread IDs
- quota reset timestamps and per-repository settings

That state stays under the XDG state directory and should not be committed or
shared.

## Safety notes

- Review comments are posted only after an authenticated quota check.
- A per-repository lock prevents duplicate monitor processes.
- A shared organization lock serializes review dispatch across repositories.
- GitHub REST and GraphQL work pauses when either quota approaches its reserve.
- Closing the GUI does not stop an already-running monitor.

## License

GNU General Public License v3.0 (GPLv3)
