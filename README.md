# CodeRabbit Review Queue

A Linux desktop queue for CodeRabbit reviews. It tracks each repository's review
window, starts the next eligible review, and shows the queue in a Qt window and
system tray.

## Why this exists

CodeRabbit's rolling limits make several active PRs awkward to manage by hand:
the next window, the reviewed head, and the order of pending PRs can all change.
The queue keeps that state per repository and guards against duplicate review
requests.

## Features

- Queues unreviewed PR heads per repository, with manual ordering and an option
  to insert new items at the top (default) or bottom. Drafts, branches, and authors can be
  excluded; `pull` and `dependabot` are excluded by default.
- Uses CodeRabbit's quota response to time the next request, rechecks before
  posting, and guards against duplicate `@coderabbitai review` comments.
  Oversized PR heads are skipped rather than blocking the queue.
- Shows pending and in-progress reviews separately from finished reviews,
  approvals, unresolved feedback, and matching agent-task progress. Queue
  order, the last status, and unexpired review windows survive an app restart.
- Runs separate monitors for separate repositories. The GUI can start and stop
  them; the tray shows the next window and can show or hide the window.
- Can delegate unresolved feedback to an idle Codex or Claude task, including
  tasks on an SSH host selected from your SSH config. Codex sessions started by
  the CLI or T3 Code are supported. You can delegate one PR or all reachable,
  idle tasks from the finished-reviews table.
- Lets each repository choose a delegation prompt: the full review workflow,
  “resolve review and stop,” continued babysitting, or a custom template with
  PR and review-thread placeholders.
- Offers optional approval actions: guarded merges after CodeRabbit approval,
  or an agent-led merge after a delegated fix. Choose whether that fix needs
  another CodeRabbit review, needs none, or lets the agent decide. Merge commit,
  squash, and rebase are supported; branch deletion and `--admin` are optional.
  GitHub auto-merge is never enabled. Codex tasks can be archived after merge.
- Retries transient GitHub failures and pauses near GitHub API quota limits.
  Desktop notifications are grouped by outcome; the review-available sound can
  be changed, previewed, or disabled, with volume defaulting to 30%.

## Requirements

Required:

- Linux with Bash 4 or newer
- Python 3 and PySide6
- [GitHub CLI](https://cli.github.com/) authenticated with `gh auth login`
- `jq`, `git`, `ripgrep`, and `flock`

Recommended:

- `notify-send` from libnotify for desktop notifications
- Codex CLI for Codex delegation; Codex Desktop or T3 Code if they own your tasks
- Claude Code for Claude delegation; Claude Desktop if it owns your tasks
- Passwordless SSH and an installed queue on the chosen agent host only when
  using remote task detection, delegation, or merging

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
The repository keeps the CLI entry point and GUI in `src/`, Bash functions in
`src/lib/`, and GUI helpers in `src/queue_gui_*.py`. Icons and the desktop entry
are in `assets/`, and checks are in `tests/`. Installation copies the runtime
files into the same locations as earlier versions; existing settings need no
migration.

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

Set author exclusions in the GUI's **Excluded authors** field, or with the CLI:

```bash
coderabbit-review-queue --repo OWNER/REPOSITORY --set-excluded-authors 'pull,dependabot,another-user'
# Include all authors:
coderabbit-review-queue --repo OWNER/REPOSITORY --set-excluded-authors ''
```

The command saves the setting and exits. Running monitors pick it up on their
next refresh. Usernames are case-insensitive; `dependabot[bot]` and
`app/dependabot` also work. Exclusions apply to the review queue, active-review
list, and unresolved-feedback delegation. They do not cancel an ongoing review.
An empty saved list overrides the defaults. The old `[pull]` title filter is
replaced by these author settings.

If CodeRabbit reports that a PR exceeds its file limit, the monitor logs the
reason and continues with the next PR. That head stays out of the queue while
its CodeRabbit status reports the rejection. Pushing a new head allows another
attempt. The tool uses CodeRabbit's reported limit rather than imposing a
limit on every PR's total changed files.

Repository discovery uses GitHub App installation information visible to the
authenticated account. An `OWNER/REPOSITORY` value can also be entered
manually.

Automatic delegation is disabled by default. For each repository, choose Auto
(Codex + Claude), Codex only, or Claude only. A matching task must be idle;
Auto prefers Codex when both are available. The **Agent host** dialog lists
hosts from `~/.ssh/config`, or you can leave it on Local. The queue uses that
host for task lookup, delegation, and `gh pr merge`.

T3 Code delegations are sent through its running local server so turns also
appear in the T3 app. If its CLI is not on `PATH`, set `T3CODE_CLI` on the
agent host to the executable or its `bin.mjs` bundle.

Automatic merging and task archiving are off by default. Configure them per
repository in **Configure approval actions**. Before merging, the queue's
merge guard checks the live PR head, CI, review states, and unresolved review
threads, including feedback from other bots. If those checks fail, it does not
merge. Archiving waits until a merge is confirmed and the matching Codex task
is idle.

## Privacy

The source contains no account identifiers, credentials, analytics, or
telemetry.

GitHub access goes through the authenticated `gh` CLI. CodeRabbit is controlled
through comments and statuses on GitHub. Optional agent integration uses the
selected host over SSH, the agent CLIs, or T3 Code's loopback server.

For optional Codex and Claude integration, the tool scans session metadata to
match a PR branch or head commit to a task. Agent session files stay where their
apps store them, but the queue's local status cache can include task titles and
latest-progress snippets. Automatic delegation resumes sessions according to
the selected mode.

Runtime state can include:

- repository names and queue order
- PR metadata, commit hashes, review-thread IDs, and cached status text
- quota reset timestamps and per-repository settings

That state stays under the XDG state directory and should not be committed or
shared.

## Safety notes

- Review comments are posted only after an authenticated quota check.
- A per-repository lock prevents duplicate monitor processes.
- Review dispatch and quota waits are independent per repository.
- GitHub REST and GraphQL work pauses when either quota approaches its reserve.
- Closing the GUI does not stop an already-running monitor.

## License

GNU General Public License v3.0 (GPLv3)
