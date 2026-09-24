# SPDX-License-Identifier: GPL-3.0-only

import os
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon

SCRIPT = str(Path(__file__).with_name("coderabbit-review-queue"))
APP_ICON_PATH = Path(__file__).with_name("coderabbit-logomark.svg")
if not APP_ICON_PATH.is_file():
    APP_ICON_PATH = Path(__file__).resolve().parent.parent / "assets/icons/coderabbit-logomark.svg"
STATE_ROOT = (
    Path(
        os.environ.get(
            "XDG_STATE_HOME",
            str(Path.home() / ".local" / "state"),
        )
    )
    / "coderabbit-review-queue"
)
SELECTED_REPO_FILE = STATE_ROOT / "selected-repo"
REPOSITORY_CACHE_FILE = STATE_ROOT / "repositories.txt"
STATUS_CACHE_MAX_AGE = 60
SHORT_DELEGATION_PROMPT = "Resolve the CodeRabbit review and stop."
BABYSIT_DELEGATION_PROMPT = """Continue babysitting this PR. Resolve the current CodeRabbit feedback, monitor CI and new review feedback, fix actionable failures and comments, and continue until the PR is merge-ready. Never trigger CodeRabbit reviews through comments."""
DEFAULT_DELEGATION_PROMPT = """Address unresolved CodeRabbit feedback on PR #{pr_number} ({pr_title}).
PR: {pr_url}
Threads that triggered this delegation: {threads}

Follow repository instructions and existing task context.

1. Use gh to fetch the live PR and unresolved CodeRabbit threads; the list above is a snapshot. Stop if the PR is closed or merged. Ignore non-CodeRabbit feedback.
2. Confirm the PR branch and inspect the working tree. Preserve unrelated work. Verify every finding against current code, including outdated findings, and make the smallest necessary fixes.
3. Validate changes and recheck feedback before pushing. Bundle related fixes, commit only this task's changes using repository signing conventions, and push to the PR branch. If signing times out, stop and ask the user to say continue.
4. Reply with the fix and validation or a concrete dismissal reason, using repository attribution rules. Resolve fixes after pushing and dismissals after explaining. Leave uncertain or blocked findings open.
5. Reply directly to existing threads without creating or submitting reviews. Verify no pending reviews remain; delete accidental pending reviews only if they contain no unrelated user comments.

Never trigger CodeRabbit reviews through comments; the queue handles requests. Finish this pass without waiting for another review. Report changes, validation, the pushed commit, and blockers."""


def app_icon() -> QIcon:
    icon = QIcon()
    has_raster_icon = False
    for size in (16, 22, 24, 32, 48, 64):
        path = APP_ICON_PATH.with_name(f"coderabbit-logomark-{size}.png")
        if path.is_file():
            icon.addFile(str(path), QSize(size, size))
            has_raster_icon = True
    if not has_raster_icon and APP_ICON_PATH.is_file():
        icon.addFile(str(APP_ICON_PATH))
    if icon.isNull():
        return QIcon.fromTheme("system-software-update")
    return icon


def ssh_config_hosts(path: Path | None = None) -> list[str]:
    config = path or Path.home() / ".ssh" / "config"
    try:
        lines = config.read_text().splitlines()
    except OSError:
        return []
    hosts: list[str] = []
    for line in lines:
        parts = line.strip().split()
        if not parts or parts[0].lower() != "host":
            continue
        for host in parts[1:]:
            if any(character in host for character in "*!?[]"):
                continue
            if host not in hosts:
                hosts.append(host)
    return hosts


def file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def status_belongs_to_repo(status: str, repo: str) -> bool:
    return status.splitlines()[0:1] == [f"Repository: {repo}"]


def live_review_number(activity: tuple[str, str, str, int] | None) -> str | None:
    if isinstance(activity, tuple) and activity[0] in {"checking", "triggering", "reviewing"}:
        return activity[1] or None
    return None


def proc_stat_is_running(stat: str) -> bool:
    remainder = stat.rpartition(")")[2].strip().split()
    return bool(remainder) and remainder[0] != "Z"


def process_is_running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return proc_stat_is_running(stat)
