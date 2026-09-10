#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QDate, QDateTime, QLocale, QProcess, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTextEdit,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

SCRIPT = str(Path(__file__).with_name("coderabbit-review-queue"))
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


def proc_stat_is_running(stat: str) -> bool:
    remainder = stat.rpartition(")")[2].strip().split()
    return bool(remainder) and remainder[0] != "Z"


def process_is_running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return proc_stat_is_running(stat)


class QueueWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.base_window_title = "CodeRabbit Review Queue"
        self.setWindowTitle(self.base_window_title)
        self.resize(1100, 820)
        self.next_review_at: QDateTime | None = None
        self.has_queued_reviews = False
        self.active_reviews: list[tuple[str, str]] = []
        self.monitor_activity: tuple[str, str, str, int] | None = None
        self.status_process = QProcess(self)
        self.status_process.finished.connect(self.status_finished)
        self.status_repo = ""
        self.status_failures = 0
        self.status_manual = False
        self.review_requests_signature: (
            tuple[
                str,
                tuple[int, int, int] | None,
                tuple[int, int, int] | None,
            ]
            | None
        ) = None
        self.status_retry_timer = QTimer(self)
        self.status_retry_timer.setSingleShot(True)
        self.status_retry_timer.timeout.connect(self.retry_status)
        self.repo_process = QProcess(self)
        self.repo_process.finished.connect(self.repos_finished)
        self.repo_validation_process = QProcess(self)
        self.repo_validation_process.finished.connect(
            self.repository_validation_finished
        )
        self.pending_repository = ""
        self.delegate_process = QProcess(self)
        self.delegate_process.finished.connect(self.delegate_finished)
        self.delegate_repo = ""
        self.delegate_pr = ""
        self.starting_monitors: set[str] = set()
        # True after we have shown a future rate-limit countdown; used so the
        # availability ding fires on transition, not on startup already-open.
        self.waiting_for_review_window = False

        title_icon = QLabel()
        title_icon.setPixmap(
            QIcon.fromTheme("system-software-update").pixmap(32, 32)
        )
        title = QLabel("CodeRabbit Review Queue")
        title.setObjectName("title")
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title_row.setContentsMargins(0, 0, 0, 6)
        title_row.addWidget(title_icon, 0, Qt.AlignVCenter)
        title_row.addWidget(title, 1, Qt.AlignVCenter)

        repo_label = QLabel("Repository")
        self.repo_combo = QComboBox()
        self.repo_combo.setEditable(True)
        self.repo_combo.setInsertPolicy(QComboBox.NoInsert)
        self.repo_combo.setMinimumWidth(360)
        self.repo_combo.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self.repo_combo.completer().setFilterMode(Qt.MatchContains)
        self.repo_combo.currentIndexChanged.connect(
            lambda _index: self.repo_changed(self.selected_repo())
        )
        self.repo_combo.lineEdit().editingFinished.connect(
            lambda: self.repo_changed(self.selected_repo())
        )
        self.repo_refresh_button = QPushButton(
            QIcon.fromTheme("folder-sync"),
            "Refresh repositories",
        )
        self.repo_refresh_button.clicked.connect(self.load_repos)

        repo_row = QHBoxLayout()
        repo_row.addWidget(repo_label)
        repo_row.addWidget(self.repo_combo, 1)
        repo_row.addWidget(self.repo_refresh_button)

        self.auto_delegate_label = QLabel("Auto-delegate unresolved feedback:")
        self.auto_delegate = QComboBox()
        self.auto_delegate.addItem("Disabled", "disabled")
        self.auto_delegate.addItem("Auto (Codex + Claude)", "auto")
        self.auto_delegate.addItem("Codex only", "codex")
        self.auto_delegate.addItem("Claude only", "claude")
        self.auto_delegate.setToolTip(
            "Per repository. A running matching task is never started again. "
            "Auto prefers an idle Codex match, then Claude."
        )
        self.auto_delegate.currentIndexChanged.connect(self.auto_delegate_changed)
        self.agent_host_button = QPushButton(
            QIcon.fromTheme("network-server"),
            "Agent host: Local",
        )
        self.agent_host_button.setToolTip(
            "Choose the computer where Codex or Claude tasks are running."
        )
        self.agent_host_button.clicked.connect(self.configure_agent_host)
        self.delegation_prompt_button = QPushButton(
            QIcon.fromTheme("document-edit"),
            "Delegation prompt…",
        )
        self.delegation_prompt_button.setToolTip(
            "Choose the prompt sent to Codex for this repository."
        )
        self.delegation_prompt_button.setEnabled(False)
        self.delegation_prompt_button.clicked.connect(
            self.configure_delegation_prompt
        )
        self.auto_merge_button = QPushButton(
            QIcon.fromTheme("configure"), "Configure auto-merge…"
        )
        self.auto_merge_button.setToolTip(
            "Configure merging after CodeRabbit approval or completed delegation."
        )
        self.auto_merge_button.setEnabled(False)
        self.auto_merge_button.clicked.connect(self.configure_auto_merge)
        auto_delegate_row = QHBoxLayout()
        auto_delegate_row.addWidget(self.auto_delegate_label)
        auto_delegate_row.addWidget(self.auto_delegate, 1)
        auto_delegate_row.addWidget(self.agent_host_button)
        auto_delegate_row.addWidget(self.delegation_prompt_button)
        auto_delegate_row.addWidget(self.auto_merge_button)
        self.stop_when_empty = QCheckBox(
            "Stop monitor after the last queued review finishes"
        )
        self.stop_when_empty.setToolTip(
            "When disabled, an empty queue stays monitored for future PR updates."
        )
        self.stop_when_empty.toggled.connect(self.stop_when_empty_changed)
        self.ignore_drafts = QCheckBox("Ignore draft pull requests")
        self.ignore_drafts.setToolTip(
            "Per repository. When enabled, draft PRs are left out of the review queue."
        )
        self.ignore_drafts.setChecked(True)
        self.ignore_drafts.toggled.connect(self.ignore_drafts_changed)
        excluded_label = QLabel("Excluded branches:")
        self.excluded_branches = QLineEdit()
        self.excluded_branches.setPlaceholderText(
            "Comma-separated head branch names, e.g. weblate-translations"
        )
        self.excluded_branches.setToolTip(
            "Per repository. Open PRs whose head branch matches are left out of "
            "the review queue and unresolved-feedback list."
        )
        self.excluded_branches.editingFinished.connect(self.excluded_branches_changed)
        excluded_row = QHBoxLayout()
        excluded_row.addWidget(excluded_label)
        excluded_row.addWidget(self.excluded_branches, 1)
        authors_label = QLabel("Excluded &authors:")
        self.excluded_authors = QLineEdit()
        authors_label.setBuddy(self.excluded_authors)
        self.excluded_authors.setAccessibleName("Excluded authors")
        self.excluded_authors.setPlaceholderText("No authors excluded")
        self.excluded_authors.setToolTip(
            "Per repository. Comma-separated GitHub usernames. Defaults to pull "
            "and dependabot. Clear to include all authors. Changes apply to the "
            "review queue and unresolved-feedback list."
        )
        self.excluded_authors.editingFinished.connect(self.excluded_authors_changed)
        authors_row = QHBoxLayout()
        authors_row.addWidget(authors_label)
        authors_row.addWidget(self.excluded_authors, 1)
        self.notify_sound = QCheckBox(
            "Play a sound when a new review becomes available"
        )
        self.notify_sound.setToolTip(
            "Per repository. Plays when the CodeRabbit rate-limit window opens "
            "(a new review can start), not when a review finishes."
        )
        self.notify_sound.setChecked(True)
        self.notify_sound.toggled.connect(self.notify_sound_changed)
        self.new_items_at_top = QCheckBox("Add new queue items at the top")
        self.new_items_at_top.setToolTip(
            "Per repository. When disabled, newly discovered PRs are appended at the bottom."
        )
        self.new_items_at_top.setChecked(True)
        self.new_items_at_top.toggled.connect(self.new_items_at_top_changed)

        self.monitor_label = QLabel()
        self.timer_label = QLabel("Next review: —")

        self.monitor_button = QPushButton(
            QIcon.fromTheme("media-playback-start"),
            "Start monitor",
        )
        self.refresh_button = QPushButton(
            QIcon.fromTheme("view-refresh"),
            "Refresh",
        )
        self.monitor_button.clicked.connect(self.toggle_monitor)
        self.refresh_button.clicked.connect(lambda: self.refresh(manual=True))

        header_buttons = QHBoxLayout()
        header_buttons.addWidget(self.monitor_label)
        header_buttons.addWidget(self.timer_label)
        header_buttons.addStretch()
        header_buttons.addWidget(self.monitor_button)
        header_buttons.addWidget(self.refresh_button)

        queue_label = QLabel("CodeRabbit review queue")
        self.queue = QTreeWidget()
        self.queue.setHeaderLabels(["PR", "Title", "Status"])
        self.queue.setRootIsDecorated(False)
        self.queue.setAlternatingRowColors(True)
        self.queue.setSelectionMode(QTreeWidget.SingleSelection)
        self.queue.header().resizeSection(0, 90)
        self.queue.header().resizeSection(1, 520)
        self.queue.header().resizeSection(2, 120)
        self.queue.itemDoubleClicked.connect(
            lambda item, _column: self.open_pr_number(item.data(0, Qt.UserRole))
        )
        self.queue.itemSelectionChanged.connect(self.update_queue_buttons)

        task_label = QLabel("Finished CodeRabbit reviews")
        self.tasks = QTreeWidget()
        self.tasks.setHeaderLabels(
            ["PR", "Title", "Result", "Agent status", "Latest progress"]
        )
        self.tasks.setRootIsDecorated(False)
        self.tasks.setAlternatingRowColors(True)
        self.tasks.header().resizeSection(0, 90)
        self.tasks.header().resizeSection(1, 300)
        self.tasks.header().resizeSection(2, 120)
        self.tasks.header().resizeSection(3, 130)
        self.tasks.itemDoubleClicked.connect(
            lambda item, _column: self.open_pr_number(item.data(0, Qt.UserRole))
        )
        self.tasks.itemSelectionChanged.connect(self.update_delegate_button)

        self.up_button = QPushButton(QIcon.fromTheme("go-up"), "Move up")
        self.down_button = QPushButton(QIcon.fromTheme("go-down"), "Move down")
        self.up_button.setEnabled(False)
        self.down_button.setEnabled(False)
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button.clicked.connect(lambda: self.move_selected(1))

        queue_buttons = QHBoxLayout()
        queue_buttons.addStretch()
        queue_buttons.addWidget(self.up_button)
        queue_buttons.addWidget(self.down_button)

        self.delegate_button = QPushButton(
            QIcon.fromTheme("system-run"),
            "Delegate selected",
        )
        self.delegate_button.setEnabled(False)
        self.delegate_button.clicked.connect(self.delegate_selected)
        task_buttons = QHBoxLayout()
        task_buttons.addStretch()
        task_buttons.addWidget(self.delegate_button)

        queue_panel = QWidget()
        queue_layout = QVBoxLayout(queue_panel)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.addWidget(queue_label)
        queue_layout.addWidget(self.queue)
        queue_layout.addLayout(queue_buttons)

        task_panel = QWidget()
        task_layout = QVBoxLayout(task_panel)
        task_layout.setContentsMargins(0, 0, 0, 0)
        task_layout.addWidget(task_label)
        task_layout.addWidget(self.tasks)
        task_layout.addLayout(task_buttons)

        self.table_splitter = QSplitter(Qt.Vertical)
        self.table_splitter.addWidget(queue_panel)
        self.table_splitter.addWidget(task_panel)
        self.table_splitter.setStretchFactor(0, 1)
        self.table_splitter.setStretchFactor(1, 1)
        self.table_splitter.setChildrenCollapsible(False)
        self.restore_splitter_sizes()
        self.table_splitter.splitterMoved.connect(self.save_splitter_sizes)

        layout = QVBoxLayout()
        layout.addLayout(title_row)
        layout.addLayout(repo_row)
        layout.addLayout(auto_delegate_row)
        layout.addWidget(self.stop_when_empty)
        layout.addWidget(self.ignore_drafts)
        layout.addLayout(excluded_row)
        layout.addLayout(authors_row)
        layout.addWidget(self.notify_sound)
        layout.addWidget(self.new_items_at_top)
        layout.addLayout(header_buttons)
        layout.addWidget(self.table_splitter, 1)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.tray_icon = QSystemTrayIcon(QIcon.fromTheme("system-software-update"), self)
        tray_menu = QMenu(self)
        self.window_action = QAction("Hide CodeRabbit queue", self)
        self.window_action.triggered.connect(self.toggle_from_tray)
        refresh_action = QAction("Refresh status", self)
        refresh_action.triggered.connect(lambda: self.refresh(manual=True))
        quit_action = QAction("Quit queue window", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        self.stop_all_monitors_action = QAction(
            "Quit and stop all monitors", self
        )
        self.stop_all_monitors_action.triggered.connect(
            self.stop_all_monitors_and_quit
        )
        self.stop_all_monitors_action.setVisible(False)
        tray_menu.addAction(self.window_action)
        tray_menu.addAction(refresh_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        tray_menu.addAction(self.stop_all_monitors_action)
        tray_menu.aboutToShow.connect(self.update_tray_window_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.tray_activated)
        self.tray_icon.setToolTip(self.base_window_title)
        self.tray_icon.show()
        self.show_transient_status("Ready", 2000)
        self.refresh_spinner = QLabel()
        self.refresh_spinner.hide()
        self.statusBar().addPermanentWidget(self.refresh_spinner)
        self.spinner_frames = tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
        self.spinner_index = 0
        self.spinner_timer = QTimer(self)
        self.spinner_timer.setInterval(80)
        self.spinner_timer.timeout.connect(self.advance_spinner)
        self.setStyleSheet(
            """
            QLabel#title { font-size: 24px; font-weight: 600; }
            QTreeWidget { font-size: 14px; }
            QPushButton { min-height: 30px; padding: 0 12px; }
            """
        )

        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self.update_monitor_state)
        self.monitor_timer.start(3000)
        self.auto_refresh = QTimer(self)
        self.auto_refresh.timeout.connect(self.refresh)
        self.auto_refresh.start(60000)
        self.countdown_timer = QTimer(self)
        self.countdown_timer.timeout.connect(self.update_countdown_display)
        self.countdown_timer.start(1000)
        self.update_monitor_state()
        if not self.load_cached_repos():
            self.load_repos()

    def selected_repo(self) -> str:
        return self.repo_combo.currentText().strip()

    def show_transient_status(self, message: str, timeout: int = 4000) -> None:
        self.statusBar().showMessage(message, timeout)

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def window_is_shown(self) -> bool:
        return self.isVisible() and not self.isMinimized()

    def toggle_from_tray(self) -> None:
        if self.window_is_shown():
            self.hide()
        else:
            self.show_from_tray()

    def update_tray_window_action(self) -> None:
        self.window_action.setText(
            "Hide CodeRabbit queue"
            if self.window_is_shown()
            else "Show CodeRabbit queue"
        )
        self.stop_all_monitors_action.setVisible(self.any_monitor_running())

    def any_monitor_running(self) -> bool:
        for pid_file in Path("/tmp").glob("coderabbit-review-queue-*.pid"):
            try:
                if pid_file.stat().st_uid != os.getuid():
                    continue
                pid = int(pid_file.read_text().strip())
            except (OSError, ValueError):
                continue
            if process_is_running(pid):
                return True
        return False

    def tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_from_tray()

    def set_tray_countdown(self, text: str | None, tooltip: str) -> None:
        if text is None:
            self.tray_icon.setIcon(QIcon.fromTheme("system-software-update"))
        else:
            pixmap = QPixmap(64, 64)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#2563eb"))
            painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
            painter.setPen(QColor("white"))
            font = QFont()
            font.setBold(True)
            font.setPixelSize(25 if len(text) <= 2 else 20)
            painter.setFont(font)
            painter.drawText(pixmap.rect(), Qt.AlignCenter, text)
            painter.end()
            self.tray_icon.setIcon(QIcon(pixmap))
        self.tray_icon.setToolTip(tooltip)

    def load_cached_repos(self) -> bool:
        try:
            repos = [
                line.strip()
                for line in REPOSITORY_CACHE_FILE.read_text().splitlines()
                if line.strip()
            ]
        except OSError:
            return False
        self.set_repositories(repos)
        self.show_transient_status(
            f"Loaded {len(repos)} cached CodeRabbit repositories"
        )
        return True

    def load_repos(self) -> None:
        if self.repo_process.state() != QProcess.NotRunning:
            return
        self.repo_refresh_button.setEnabled(False)
        self.statusBar().showMessage("Detecting CodeRabbit repositories…")
        self.repo_process.setProgram(SCRIPT)
        self.repo_process.setArguments(["--list-repos"])
        self.repo_process.start()

    def repos_finished(self, exit_code: int) -> None:
        stdout = bytes(self.repo_process.readAllStandardOutput()).decode()
        stderr = bytes(self.repo_process.readAllStandardError()).decode().strip()
        self.repo_refresh_button.setEnabled(True)
        if exit_code != 0:
            self.show_transient_status(
                "Could not detect CodeRabbit repositories",
                6000,
            )
            QMessageBox.warning(
                self,
                "Repository refresh failed",
                stderr or "GitHub CodeRabbit installation lookup failed",
            )
            return

        repos = list(dict.fromkeys(line.strip() for line in stdout.splitlines() if line.strip()))
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = REPOSITORY_CACHE_FILE.with_suffix(".tmp")
        temporary.write_text("\n".join(repos) + ("\n" if repos else ""))
        os.replace(temporary, REPOSITORY_CACHE_FILE)
        self.set_repositories(repos)
        self.show_transient_status(
            f"Found and cached {len(repos)} CodeRabbit repositories"
        )

    def set_repositories(self, repos: list[str]) -> None:
        try:
            selected = SELECTED_REPO_FILE.read_text().strip()
        except OSError:
            selected = ""
        # Keep a remembered or typed OWNER/NAME even when discovery missed it
        # (e.g. brand-new CodeRabbit install with no PR comments yet).
        if not selected and repos:
            selected = repos[0]

        self.repo_combo.blockSignals(True)
        self.repo_combo.clear()
        self.repo_combo.addItems(repos)
        if selected:
            index = self.repo_combo.findText(selected)
            if index >= 0:
                self.repo_combo.setCurrentIndex(index)
            else:
                self.repo_combo.setEditText(selected)
        self.repo_combo.blockSignals(False)
        if selected:
            self.repo_changed(selected)
        else:
            self.queue.clear()
            self.tasks.clear()
            self.statusBar().showMessage(
                "No detectable CodeRabbit repositories. "
                "Enter OWNER/NAME manually or refresh repositories."
            )

    def repo_changed(self, repo: str) -> None:
        if not repo:
            return
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            QMessageBox.warning(
                self,
                "Invalid repository",
                "Enter a repository as OWNER/NAME.",
            )
            self.restore_selected_repo()
            return
        listed = any(
            self.repo_combo.itemText(index) == repo
            for index in range(self.repo_combo.count())
        )
        if listed:
            self.activate_repo(repo)
            return
        if self.repo_validation_process.state() != QProcess.NotRunning:
            return
        self.pending_repository = repo
        self.repo_combo.setEnabled(False)
        self.statusBar().showMessage(f"Validating {repo}…")
        self.repo_validation_process.setProgram(SCRIPT)
        self.repo_validation_process.setArguments(
            ["--repo", repo, "--validate-repo"]
        )
        self.repo_validation_process.start()

    def repository_validation_finished(self, exit_code: int) -> None:
        stderr = bytes(
            self.repo_validation_process.readAllStandardError()
        ).decode().strip()
        repo = self.pending_repository
        self.pending_repository = ""
        self.repo_combo.setEnabled(True)
        if exit_code != 0:
            QMessageBox.warning(
                self,
                "Repository not found",
                stderr or f"GitHub could not find {repo}.",
            )
            self.restore_selected_repo()
            return
        if self.repo_combo.findText(repo) < 0:
            self.repo_combo.blockSignals(True)
            self.repo_combo.addItem(repo)
            self.repo_combo.setCurrentText(repo)
            self.repo_combo.blockSignals(False)
        self.activate_repo(repo)

    def restore_selected_repo(self) -> None:
        try:
            selected = SELECTED_REPO_FILE.read_text().strip()
        except OSError:
            selected = ""
        self.repo_combo.blockSignals(True)
        self.repo_combo.setCurrentText(selected)
        self.repo_combo.blockSignals(False)

    def activate_repo(self, repo: str) -> None:
        self.status_retry_timer.stop()
        self.status_failures = 0
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = SELECTED_REPO_FILE.with_suffix(".tmp")
        temporary.write_text(repo + "\n")
        os.replace(temporary, SELECTED_REPO_FILE)
        self.load_auto_delegate(repo)
        self.load_agent_host(repo)
        self.delegation_prompt_button.setEnabled(bool(repo))
        self.auto_merge_button.setEnabled(bool(repo))
        self.load_stop_when_empty(repo)
        self.load_ignore_drafts(repo)
        self.load_excluded_branches(repo)
        self.load_excluded_authors(repo)
        self.load_notify_sound(repo)
        self.load_new_items_at_top(repo)
        cache_is_fresh = self.load_cached_status(repo)
        self.waiting_for_review_window = False
        self.update_monitor_state()
        if not cache_is_fresh:
            self.refresh()

    def auto_delegate_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-auto-delegate"

    def status_cache_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-status.txt"

    def load_cached_status(self, repo: str) -> bool:
        cache = self.status_cache_file(repo)
        try:
            status = cache.read_text()
            cache_mtime = cache.stat().st_mtime
            age = time.time() - cache_mtime
        except OSError:
            return False
        if not status.strip():
            return False
        self.populate_queue(status)
        self.populate_tasks(status)
        newer_state_exists = False
        for path in (
            self.review_requests_file(repo),
            self.monitor_state_file(repo),
        ):
            try:
                if path.stat().st_mtime > cache_mtime:
                    newer_state_exists = True
                    break
            except (OSError, TypeError):
                continue
        return age <= STATUS_CACHE_MAX_AGE and not newer_state_exists

    def save_status_cache(self, repo: str, status: str) -> None:
        if not status.strip():
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.status_cache_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(status.rstrip() + "\n")
        os.replace(temporary, target)

    def agent_host_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-agent-host"

    def delegation_prompt_mode_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-delegation-prompt-mode"

    def delegation_prompt_template_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-delegation-prompt-template"

    def auto_merge_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-auto-merge"

    def merge_method_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-merge-method"

    def delete_branch_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-delete-branch"

    def merge_after_delegation_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-merge-after-delegation"

    def merge_after_approval_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-merge-after-approval"

    def configure_auto_merge(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        try:
            enabled = self.auto_merge_file(repo).read_text().strip() == "1"
        except OSError:
            enabled = False
        try:
            method = self.merge_method_file(repo).read_text().strip()
        except OSError:
            method = "squash"
        if method not in {"merge", "squash", "rebase"}:
            method = "squash"
        try:
            delete_branch = self.delete_branch_file(repo).read_text().strip() != "0"
        except OSError:
            delete_branch = True
        try:
            merge_after_delegation = (
                self.merge_after_delegation_file(repo).read_text().strip() != "0"
            )
        except OSError:
            merge_after_delegation = True
        try:
            merge_after_approval = (
                self.merge_after_approval_file(repo).read_text().strip() != "0"
            )
        except OSError:
            merge_after_approval = True

        dialog = QDialog(self)
        dialog.setWindowTitle("Configure auto-merge")
        enabled_box = QCheckBox("Automatically merge pull requests")
        enabled_box.setChecked(enabled)
        method_label = QLabel("Merge method")
        method_select = QComboBox()
        method_select.addItem("Merge commit", "merge")
        method_select.addItem("Squash and merge", "squash")
        method_select.addItem("Rebase and merge", "rebase")
        method_select.setCurrentIndex(method_select.findData(method))
        delete_box = QCheckBox("Delete the branch after merging")
        delete_box.setChecked(delete_branch)
        delegated_box = QCheckBox(
            "Merge after a completed delegated fix without another CodeRabbit review"
        )
        delegated_box.setChecked(merge_after_delegation)
        approval_box = QCheckBox("Merge after CodeRabbit approval")
        approval_box.setChecked(merge_after_approval)
        explanation = QLabel(
            "The app waits for green CI and never enables GitHub auto-merge. "
            "The merge command runs on the selected agent host."
        )
        explanation.setWordWrap(True)

        def update_controls() -> None:
            active = enabled_box.isChecked()
            method_label.setEnabled(active)
            method_select.setEnabled(active)
            delete_box.setEnabled(active)
            delegated_box.setEnabled(active)
            approval_box.setEnabled(active)

        enabled_box.toggled.connect(lambda _checked: update_controls())
        update_controls()
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(enabled_box)
        layout.addWidget(approval_box)
        layout.addWidget(delegated_box)
        layout.addWidget(method_label)
        layout.addWidget(method_select)
        layout.addWidget(delete_box)
        layout.addWidget(explanation)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return

        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        values = (
            (self.auto_merge_file(repo), "1\n" if enabled_box.isChecked() else "0\n"),
            (self.merge_method_file(repo), f"{method_select.currentData()}\n"),
            (self.delete_branch_file(repo), "1\n" if delete_box.isChecked() else "0\n"),
            (
                self.merge_after_delegation_file(repo),
                "1\n" if delegated_box.isChecked() else "0\n",
            ),
            (
                self.merge_after_approval_file(repo),
                "1\n" if approval_box.isChecked() else "0\n",
            ),
        )
        for target, value in values:
            temporary = target.with_suffix(".tmp")
            temporary.write_text(value)
            os.replace(temporary, target)
        self.show_transient_status("Auto-merge settings saved")

    def delegation_prompt_settings(self, repo: str) -> tuple[str, str]:
        try:
            mode = self.delegation_prompt_mode_file(repo).read_text().strip()
        except OSError:
            mode = "default"
        if mode not in {"default", "short", "custom"}:
            mode = "default"
        try:
            custom = self.delegation_prompt_template_file(repo).read_text()
        except OSError:
            custom = ""
        return mode, custom

    def configure_delegation_prompt(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        current_mode, custom_prompt = self.delegation_prompt_settings(repo)
        dialog = QDialog(self)
        dialog.setWindowTitle("Delegation prompt")
        dialog.resize(760, 620)

        mode_label = QLabel("Prompt for this repository")
        mode_select = QComboBox()
        mode_select.addItem("Default review workflow", "default")
        mode_select.addItem("Resolve review and stop", "short")
        mode_select.addItem("Custom prompt", "custom")
        mode_select.setCurrentIndex(mode_select.findData(current_mode))

        prompt_editor = QTextEdit()
        prompt_editor.setAcceptRichText(False)
        prompt_editor.setAccessibleName("Delegation prompt text")
        placeholders = QLabel(
            "Custom placeholders: {pr_number}, {pr_title}, {repo}, "
            "{pr_url}, {threads}"
        )
        placeholders.setTextInteractionFlags(Qt.TextSelectableByMouse)
        placeholders.setWordWrap(True)

        editor_state = {"mode": "", "custom": custom_prompt}

        def update_editor() -> None:
            if editor_state["mode"] == "custom":
                editor_state["custom"] = prompt_editor.toPlainText()
            mode = mode_select.currentData()
            if mode == "default":
                prompt_editor.setPlainText(DEFAULT_DELEGATION_PROMPT)
            elif mode == "short":
                prompt_editor.setPlainText(SHORT_DELEGATION_PROMPT)
            else:
                prompt_editor.setPlainText(editor_state["custom"])
            prompt_editor.setEnabled(mode == "custom")
            editor_state["mode"] = mode

        mode_select.currentIndexChanged.connect(lambda _index: update_editor())
        update_editor()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(mode_label)
        layout.addWidget(mode_select)
        layout.addWidget(prompt_editor, 1)
        layout.addWidget(placeholders)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return
        mode = mode_select.currentData()
        if not isinstance(mode, str):
            return
        custom = prompt_editor.toPlainText() if mode == "custom" else custom_prompt
        if mode == "custom" and not custom.strip():
            QMessageBox.warning(
                self,
                "Empty custom prompt",
                "Enter a custom prompt or choose a preset.",
            )
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        mode_target = self.delegation_prompt_mode_file(repo)
        mode_temporary = mode_target.with_suffix(".tmp")
        mode_temporary.write_text(mode + "\n")
        os.replace(mode_temporary, mode_target)
        if mode == "custom":
            template_target = self.delegation_prompt_template_file(repo)
            template_temporary = template_target.with_suffix(".tmp")
            template_temporary.write_text(custom.rstrip() + "\n")
            os.replace(template_temporary, template_target)
        self.show_transient_status("Delegation prompt saved")

    def load_agent_host(self, repo: str) -> None:
        host = ""
        if repo:
            try:
                host = self.agent_host_file(repo).read_text().strip()
            except OSError:
                pass
        self.agent_host_button.setEnabled(bool(repo))
        self.agent_host_button.setText(
            f"Agent host: {host}" if host else "Agent host: Local"
        )

    def configure_agent_host(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        try:
            current = self.agent_host_file(repo).read_text().strip()
        except OSError:
            current = ""
        choices = ["Local"] + ssh_config_hosts()
        initial = current or "Local"
        if initial not in choices:
            choices.append(initial)
        selected, accepted = QInputDialog.getItem(
            self,
            "Configure agent host",
            "SSH host running Codex or Claude tasks:",
            choices,
            choices.index(initial),
            True,
        )
        if not accepted:
            return
        host = selected.strip()
        if host.lower() == "local":
            host = ""
        if host and not re.fullmatch(r"[A-Za-z0-9_.@-]+", host):
            QMessageBox.warning(
                self,
                "Invalid SSH host",
                "Use an SSH config alias or a host name without spaces.",
            )
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.agent_host_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(host + "\n")
        os.replace(temporary, target)
        self.load_agent_host(repo)
        self.show_transient_status(
            f"Agent tasks will run on {host}" if host else "Agent tasks will run locally"
        )
        self.refresh()

    def load_auto_delegate(self, repo: str) -> None:
        self.auto_delegate.blockSignals(True)
        enabled = bool(repo)
        self.auto_delegate_label.setEnabled(enabled)
        self.auto_delegate.setEnabled(enabled)
        mode = "disabled"
        if repo:
            try:
                raw = self.auto_delegate_file(repo).read_text().strip()
            except OSError:
                raw = "disabled"
            if raw in {"0", "disabled", ""}:
                mode = "disabled"
            elif raw in {"1", "codex"}:
                mode = "codex"
            elif raw in {"auto", "both"}:
                mode = "auto"
            elif raw == "claude":
                mode = "claude"
            else:
                mode = "disabled"
        index = self.auto_delegate.findData(mode)
        self.auto_delegate.setCurrentIndex(index if index >= 0 else 0)
        self.auto_delegate.blockSignals(False)

    def auto_delegate_changed(self, _index: int = 0) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        mode = self.auto_delegate.currentData()
        if not isinstance(mode, str):
            mode = "disabled"
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.auto_delegate_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(f"{mode}\n")
        os.replace(temporary, target)
        labels = {
            "disabled": "Automatic delegation disabled",
            "auto": "Automatic delegation set to Auto (Codex + Claude)",
            "codex": "Automatic delegation set to Codex only",
            "claude": "Automatic delegation set to Claude only",
        }
        self.show_transient_status(labels.get(mode, "Automatic delegation updated"))

    def stop_when_empty_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-stop-when-empty"

    def load_stop_when_empty(self, repo: str) -> None:
        self.stop_when_empty.blockSignals(True)
        self.stop_when_empty.setEnabled(bool(repo))
        if repo:
            try:
                enabled = self.stop_when_empty_file(repo).read_text().strip() == "1"
            except OSError:
                enabled = False
            self.stop_when_empty.setChecked(enabled)
        else:
            self.stop_when_empty.setChecked(False)
        self.stop_when_empty.blockSignals(False)

    def stop_when_empty_changed(self, enabled: bool) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.stop_when_empty_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("1\n" if enabled else "0\n")
        os.replace(temporary, target)
        self.show_transient_status(
            "Monitor will stop when the queue becomes empty"
            if enabled
            else "Monitor will remain active while the queue is empty"
        )

    def ignore_drafts_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-ignore-drafts"

    def load_ignore_drafts(self, repo: str) -> None:
        self.ignore_drafts.blockSignals(True)
        self.ignore_drafts.setEnabled(bool(repo))
        if repo:
            try:
                enabled = self.ignore_drafts_file(repo).read_text().strip() != "0"
            except OSError:
                enabled = True
            self.ignore_drafts.setChecked(enabled)
        else:
            self.ignore_drafts.setChecked(True)
        self.ignore_drafts.blockSignals(False)

    def ignore_drafts_changed(self, enabled: bool) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.ignore_drafts_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("1\n" if enabled else "0\n")
        os.replace(temporary, target)
        self.show_transient_status(
            "Draft pull requests are ignored"
            if enabled
            else "Draft pull requests are included"
        )
        self.refresh(manual=True)

    def excluded_branches_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-excluded-branches"

    def load_excluded_branches(self, repo: str) -> None:
        self.excluded_branches.blockSignals(True)
        self.excluded_branches.setEnabled(bool(repo))
        text = ""
        if repo:
            try:
                lines = []
                for line in self.excluded_branches_file(repo).read_text().splitlines():
                    branch = line.strip()
                    if branch and not branch.startswith("#"):
                        lines.append(branch)
                text = ", ".join(lines)
            except OSError:
                text = ""
        self.excluded_branches.setText(text)
        self.excluded_branches.blockSignals(False)

    def excluded_branches_changed(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        branches = []
        for part in self.excluded_branches.text().replace("\n", ",").split(","):
            branch = part.strip()
            if branch and branch not in branches:
                branches.append(branch)
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.excluded_branches_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("".join(f"{branch}\n" for branch in branches))
        os.replace(temporary, target)
        self.excluded_branches.blockSignals(True)
        self.excluded_branches.setText(", ".join(branches))
        self.excluded_branches.blockSignals(False)
        if branches:
            self.show_transient_status(
                f"Excluded branches: {', '.join(branches)}"
            )
        else:
            self.show_transient_status("No branches excluded")
        self.refresh(manual=True)

    def excluded_authors_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-excluded-authors"

    def load_excluded_authors(self, repo: str) -> None:
        self.excluded_authors.setEnabled(bool(repo))
        authors = []
        if repo:
            try:
                authors = [
                    line.strip()
                    for line in self.excluded_authors_file(repo).read_text().splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
            except FileNotFoundError:
                authors = ["pull", "dependabot"]
        self.excluded_authors.setText(", ".join(authors))

    def excluded_authors_changed(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        authors = []
        for part in self.excluded_authors.text().replace("\n", ",").split(","):
            author = part.strip().lower()
            if not author or author.startswith("#"):
                continue
            author = author.removeprefix("@").removeprefix("app/").removesuffix("[bot]")
            if author and author not in authors:
                authors.append(author)
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.excluded_authors_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("".join(f"{author}\n" for author in authors))
        os.replace(temporary, target)
        self.excluded_authors.setText(", ".join(authors))
        self.show_transient_status(
            f"Excluded authors: {', '.join(authors)}" if authors else "No authors excluded"
        )
        self.refresh(manual=True)

    def notify_sound_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-notify-sound"

    def load_notify_sound(self, repo: str) -> None:
        self.notify_sound.blockSignals(True)
        self.notify_sound.setEnabled(bool(repo))
        if repo:
            try:
                enabled = self.notify_sound_file(repo).read_text().strip() != "0"
            except OSError:
                enabled = True
            self.notify_sound.setChecked(enabled)
        else:
            self.notify_sound.setChecked(True)
        self.notify_sound.blockSignals(False)

    def notify_sound_changed(self, enabled: bool) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.notify_sound_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("1\n" if enabled else "0\n")
        os.replace(temporary, target)
        self.show_transient_status(
            "Review-available sound enabled"
            if enabled
            else "Review-available sound disabled"
        )

    def new_items_at_top_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-new-items-at-top"

    def load_new_items_at_top(self, repo: str) -> None:
        self.new_items_at_top.blockSignals(True)
        self.new_items_at_top.setEnabled(bool(repo))
        if repo:
            try:
                enabled = self.new_items_at_top_file(repo).read_text().strip() != "0"
            except OSError:
                enabled = True
            self.new_items_at_top.setChecked(enabled)
        else:
            self.new_items_at_top.setChecked(True)
        self.new_items_at_top.blockSignals(False)

    def new_items_at_top_changed(self, enabled: bool) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.new_items_at_top_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("1\n" if enabled else "0\n")
        os.replace(temporary, target)
        self.show_transient_status(
            "New queue items are added at the top"
            if enabled
            else "New queue items are added at the bottom"
        )
        self.refresh(manual=True)

    def monitor_pid_file(self, repo: str | None = None) -> Path | None:
        repo = repo or self.selected_repo()
        if not repo:
            return None
        return Path(f"/tmp/coderabbit-review-queue-{repo.replace('/', '__')}.pid")

    def monitor_pid(self, repo: str | None = None) -> int | None:
        pid_file = self.monitor_pid_file(repo)
        if pid_file is None:
            return None
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if process_is_running(pid) else None

    def update_monitor_state(self) -> None:
        repo = self.selected_repo()
        running = self.monitor_pid(repo) is not None
        starting = repo in self.starting_monitors
        if running:
            label = "Monitor: Running"
        elif starting:
            label = "Monitor: Starting…"
        else:
            label = "Monitor: Stopped"
        self.monitor_label.setText(label)
        if running:
            self.monitor_button.setIcon(QIcon.fromTheme("media-playback-stop"))
            self.monitor_button.setText("Stop monitor")
            self.monitor_button.setEnabled(True)
        else:
            self.monitor_button.setIcon(QIcon.fromTheme("media-playback-start"))
            self.monitor_button.setText("Start monitor")
            self.monitor_button.setEnabled(not starting)
        self.refresh_if_review_requests_changed()

    def review_requests_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-review-requests.tsv"

    def monitor_state_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-monitor-state.tsv"

    def read_monitor_activity(self, repo: str) -> None:
        if self.monitor_pid(repo) is None:
            self.monitor_activity = None
            return
        try:
            phase, number, title, expiry = (
                self.monitor_state_file(repo).read_text().strip().split("\t", 3)
            )
            parsed_expiry = int(expiry)
        except (OSError, ValueError):
            self.monitor_activity = None
            return
        self.monitor_activity = (phase, number, title, parsed_expiry)
        if phase == "waiting" and parsed_expiry > 0:
            self.next_review_at = QDateTime.fromSecsSinceEpoch(
                parsed_expiry
            ).toLocalTime()

    def refresh_if_review_requests_changed(self) -> None:
        repo = self.selected_repo()
        if not repo:
            self.review_requests_signature = None
            return
        self.read_monitor_activity(repo)
        self.apply_monitor_activity_to_queue()
        self.update_countdown_display()
        signature = (
            repo,
            file_signature(self.review_requests_file(repo)),
            file_signature(self.monitor_state_file(repo)),
        )
        if self.review_requests_signature is None:
            self.review_requests_signature = signature
            return
        if signature == self.review_requests_signature:
            return
        if self.status_process.state() != QProcess.NotRunning:
            return
        self.review_requests_signature = signature
        self.refresh()

    def refresh(self, manual: bool = False, retry: bool = False) -> None:
        if self.status_process.state() != QProcess.NotRunning:
            return
        if self.status_retry_timer.isActive():
            if not manual:
                return
            self.status_retry_timer.stop()
        repo = self.selected_repo()
        if not repo:
            return
        if not retry:
            self.status_failures = 0
            self.status_manual = manual
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Refreshing…")
        self.spinner_index = 0
        self.advance_spinner()
        self.refresh_spinner.show()
        self.spinner_timer.start()
        self.statusBar().showMessage("Refreshing GitHub status…")
        self.status_repo = repo
        self.status_process.setProgram(SCRIPT)
        status_action = "--status" if manual else "--cached-status"
        self.status_process.setArguments(["--repo", repo, status_action])
        self.status_process.start()

    def retry_status(self) -> None:
        self.refresh(manual=self.status_manual, retry=True)

    def status_finished(self, exit_code: int) -> None:
        stdout = bytes(self.status_process.readAllStandardOutput()).decode().strip()
        stderr = bytes(self.status_process.readAllStandardError()).decode().strip()
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Refresh")
        self.spinner_timer.stop()
        self.refresh_spinner.hide()
        self.update_monitor_state()
        if self.status_repo != self.selected_repo():
            self.status_failures = 0
            self.refresh()
            return
        if exit_code != 0:
            quota = re.search(
                r"GITHUB_QUOTA_LOW\t(\d+)\t([^\t\r\n]+)\t(\d+)",
                stderr,
            )
            if quota:
                reset = int(quota.group(1))
                api = quota.group(2)
                remaining = quota.group(3)
                now = QDateTime.currentSecsSinceEpoch()
                delay = max(30, reset - now + 10)
                wake = QDateTime.fromSecsSinceEpoch(reset).toLocalTime()
                self.status_failures = 0
                self.statusBar().showMessage(
                    f"GitHub {api} quota low ({remaining} remaining); "
                    f"refreshing at {self.format_time(wake)}"
                )
                self.status_retry_timer.start(delay * 1000)
                return

            self.status_failures += 1
            if self.status_failures <= 3:
                delay = (5, 15, 30)[self.status_failures - 1]
                self.statusBar().showMessage(
                    f"GitHub refresh failed; retrying in {delay} seconds…"
                )
                self.status_retry_timer.start(delay * 1000)
                return

            message = stderr or stdout or "Unknown GitHub error"
            self.status_failures = 0
            if self.status_manual:
                QMessageBox.warning(self, "Status refresh failed", message)
            else:
                self.show_transient_status(
                    "GitHub status is temporarily unavailable",
                    6000,
                )
            return

        self.status_failures = 0
        self.save_status_cache(self.status_repo, stdout)
        self.populate_queue(stdout)
        self.populate_tasks(stdout)
        self.show_transient_status("Status refreshed", 3000)

    def advance_spinner(self) -> None:
        frame = self.spinner_frames[self.spinner_index]
        self.spinner_index = (self.spinner_index + 1) % len(self.spinner_frames)
        self.refresh_spinner.setText(f"{frame} Refreshing status…")

    def parse_expiry(self, line: str) -> QDateTime | None:
        raw = line.removeprefix(
            "Next outstanding rate limit expires at "
        ).removesuffix(".").removesuffix(" UTC")
        expiry = QDateTime.fromString(raw, Qt.ISODate)
        if not expiry.isValid():
            return None
        return expiry.toLocalTime()

    def format_time(self, local: QDateTime) -> str:
        return QLocale.system().toString(local.time(), QLocale.ShortFormat)

    def format_expiry(self, local: QDateTime) -> str:
        today = QDate.currentDate()
        if local.date() == today:
            day = "Today"
        elif local.date() == today.addDays(1):
            day = "Tomorrow"
        else:
            day = local.toString("ddd, d MMM")

        seconds = QDateTime.currentDateTime().secsTo(local)
        if seconds <= 0:
            remaining = "available now"
        elif seconds < 60:
            remaining = f"in {seconds} second{'s' if seconds != 1 else ''}"
        else:
            minutes = (seconds + 59) // 60
            if minutes < 60:
                remaining = f"in {minutes} minute{'s' if minutes != 1 else ''}"
            else:
                hours, minutes = divmod(minutes, 60)
                remaining = f"in {hours}h {minutes}m"
        return f"{day} at {self.format_time(local)} ({remaining})"

    def play_availability_sound(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        QProcess.startDetached(SCRIPT, ["--repo", repo, "--play-notify-sound"])

    def maybe_play_availability_sound(self) -> None:
        if not self.waiting_for_review_window:
            return
        self.waiting_for_review_window = False
        if not self.notify_sound.isChecked():
            return
        # Monitor plays the ding after its own wait_until; avoid a double sound.
        if self.monitor_pid() is not None:
            return
        self.play_availability_sound()

    def update_countdown_display(self) -> None:
        self.setWindowTitle(self.base_window_title)
        repo = self.selected_repo() or self.base_window_title
        if self.monitor_activity is not None:
            phase, number, title, activity_expiry = self.monitor_activity
            if (
                phase == "waiting"
                and activity_expiry <= QDateTime.currentSecsSinceEpoch()
            ):
                summary = "Preparing availability check"
                self.timer_label.setText(f"Next review: {summary}")
                self.set_tray_countdown("…", f"{repo}\n{summary}")
                self.waiting_for_review_window = False
                return
            if phase in {"checking", "triggering"}:
                if phase == "checking":
                    summary = f"Checking availability for #{number}"
                else:
                    summary = f"Triggering review on #{number}"
                self.timer_label.setText(f"Next review: {summary}")
                self.set_tray_countdown("…", f"{repo}\n{summary}\n{title}")
                self.waiting_for_review_window = False
                return
            if phase == "reviewing":
                summary = f"Review in progress on #{number}"
                self.timer_label.setText(f"Next review: {summary}")
                self.set_tray_countdown("…", f"{repo}\n{summary}\n{title}")
                self.waiting_for_review_window = False
                return
        if self.active_reviews:
            labels = ", ".join(f"#{number}" for number, _title in self.active_reviews)
            detail = self.active_reviews[0][1]
            if len(self.active_reviews) == 1:
                summary = f"Review in progress on #{self.active_reviews[0][0]}"
            else:
                summary = f"Reviews in progress ({labels})"
            self.timer_label.setText(f"Next review: {summary}")
            self.set_tray_countdown(
                "…",
                f"{repo}\n{summary}\n{detail}",
            )
            self.waiting_for_review_window = False
            return

        if not self.has_queued_reviews:
            self.timer_label.setText("Next review: —")
            self.set_tray_countdown(None, f"{repo}\nNo reviews waiting")
            self.waiting_for_review_window = False
            return

        if self.next_review_at is None:
            if self.monitor_pid() is None:
                self.timer_label.setText("Next review: Availability unknown")
                self.set_tray_countdown("?", f"{repo}\nReview availability unknown")
            else:
                self.timer_label.setText("Next review: Available now")
                self.set_tray_countdown("✓", f"{repo}\nReview available now")
                self.maybe_play_availability_sound()
            return

        seconds = QDateTime.currentDateTime().secsTo(self.next_review_at)
        if seconds <= 0:
            if self.monitor_pid() is None:
                self.timer_label.setText("Next review: Availability unknown")
                self.set_tray_countdown("?", f"{repo}\nReview availability unknown")
            else:
                self.timer_label.setText("Next review: Available now")
                self.set_tray_countdown("✓", f"{repo}\nReview available now")
                self.maybe_play_availability_sound()
            return

        self.waiting_for_review_window = True
        if seconds < 60:
            compact = f"{seconds}s"
        else:
            minutes = (seconds + 59) // 60
            if minutes < 60:
                compact = f"{minutes}m"
            else:
                hours, minutes = divmod(minutes, 60)
                compact = f"{hours}h {minutes}m"
        detail = self.format_expiry(self.next_review_at)
        self.timer_label.setText("Next review: " + detail)
        self.set_tray_countdown(compact, f"{repo}\nNext review: {detail}")

    def populate_queue(self, status: str) -> None:
        current_item = self.queue.currentItem()
        selected_number = (
            current_item.data(0, Qt.UserRole) if current_item is not None else None
        )
        queued: list[tuple[str, str]] = []
        active: list[tuple[str, str]] = []
        section = ""
        expiry: QDateTime | None = None
        for line in status.splitlines():
            if line == "Active reviews:":
                section = "active"
            elif line == "Queued PRs:":
                section = "queue"
            elif line.startswith("Finished CodeRabbit reviews:"):
                section = "finished"
            elif line.startswith("Next outstanding rate limit expires at "):
                expiry = self.parse_expiry(line)
            elif section in {"active", "queue"} and line.startswith("  #"):
                number, _, title = line.strip().partition(" ")
                number = number.removeprefix("#")
                title = re.sub(r" \(in progress\)$", "", title)
                if section == "active":
                    active.append((number, title))
                else:
                    queued.append((number, title))

        repo = self.selected_repo()
        if repo:
            try:
                saved_order = self.queue_order_file(repo).read_text().splitlines()
            except OSError:
                saved_order = []
            queued_by_number = {number: title for number, title in queued}
            saved_queued = [
                (number, queued_by_number.pop(number))
                for number in saved_order
                if number in queued_by_number
            ]
            new_queued = list(queued_by_number.items())
            if self.new_items_at_top.isChecked():
                queued = new_queued + saved_queued
            else:
                queued = saved_queued + new_queued

        self.active_reviews = active
        self.has_queued_reviews = bool(queued)
        self.next_review_at = expiry
        self.update_countdown_display()

        active_numbers = {number for number, _title in active}
        display_rows: list[tuple[str, str, str]] = [
            (number, title, "In progress") for number, title in active
        ]
        for number, title in queued:
            if number in active_numbers:
                continue
            display_rows.append((number, title, "Queued"))

        self.queue.clear()
        for number, title, review_status in display_rows:
            item = QTreeWidgetItem([f"#{number}", title, review_status])
            item.setData(0, Qt.UserRole, number)
            item.setData(0, Qt.UserRole + 1, review_status)
            self.queue.addTopLevelItem(item)
            if number == selected_number:
                self.queue.setCurrentItem(item)
        if display_rows and self.queue.currentItem() is None:
            self.queue.setCurrentItem(self.queue.topLevelItem(0))
        self.apply_monitor_activity_to_queue()
        self.update_queue_buttons()

    def apply_monitor_activity_to_queue(self) -> None:
        if self.monitor_activity is None:
            return
        phase, number, title, _expiry = self.monitor_activity
        labels = {
            "checking": "Checking availability",
            "triggering": "Triggering review",
            "reviewing": "In progress",
        }
        status = labels.get(phase)
        if status is None or not number:
            return
        selected = self.queue.currentItem()
        selected_number = selected.data(0, Qt.UserRole) if selected else None
        for index in range(self.queue.topLevelItemCount()):
            item = self.queue.topLevelItem(index)
            if item.data(0, Qt.UserRole) == number:
                self.queue.takeTopLevelItem(index)
                if not title:
                    title = item.text(1)
                break
        live_item = QTreeWidgetItem([f"#{number}", title, status])
        live_item.setData(0, Qt.UserRole, number)
        live_item.setData(0, Qt.UserRole + 1, status)
        self.queue.insertTopLevelItem(0, live_item)
        if selected_number == number:
            self.queue.setCurrentItem(live_item)

    def update_queue_buttons(self) -> None:
        item = self.queue.currentItem()
        row = self.selected_row()
        movable = (
            item is not None
            and item.data(0, Qt.UserRole + 1) == "Queued"
            and self.queue.topLevelItemCount() > 1
        )
        self.up_button.setEnabled(movable and row > 0)
        self.down_button.setEnabled(
            movable and 0 <= row < self.queue.topLevelItemCount() - 1
        )

    def splitter_sizes_file(self) -> Path:
        return STATE_ROOT / "table-splitter-sizes"

    def save_splitter_sizes(self, *_args: object) -> None:
        sizes = self.table_splitter.sizes()
        if len(sizes) != 2 or any(size <= 0 for size in sizes):
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.splitter_sizes_file()
        temporary = target.with_suffix(".tmp")
        temporary.write_text(f"{sizes[0]} {sizes[1]}\n")
        os.replace(temporary, target)

    def restore_splitter_sizes(self) -> None:
        try:
            values = [
                int(value) for value in self.splitter_sizes_file().read_text().split()
            ]
        except (OSError, ValueError):
            return
        if len(values) == 2 and all(value > 0 for value in values):
            self.table_splitter.setSizes(values)

    def populate_tasks(self, status: str) -> None:
        current_item = self.tasks.currentItem()
        selected_number = (
            current_item.data(0, Qt.UserRole) if current_item is not None else None
        )
        rows: list[tuple[str, str, str, str]] = []
        current: tuple[str, str] | None = None
        result = ""
        in_finished = False

        for line in status.splitlines():
            if line.startswith("Finished CodeRabbit reviews:"):
                in_finished = True
                continue
            if not in_finished:
                continue

            review = re.fullmatch(r"  #(\d+) (.+)", line)
            if review:
                current = (review.group(1), review.group(2))
                result = ""
                continue
            if current and line.startswith("    Result: "):
                result = line.removeprefix("    Result: ")
                continue
            if current and (
                line.startswith("    Agent task: ")
                or line.startswith("    Codex task: ")
            ):
                progress = line.removeprefix("    Agent task: ").removeprefix(
                    "    Codex task: "
                )
                rows.append((*current, result, progress))
                current = None

        self.tasks.clear()
        for number, title, result, progress in rows:
            state, separator, detail = progress.partition(" — ")
            actionable = result.endswith(" unresolved")
            item = QTreeWidgetItem(
                [
                    f"#{number}",
                    title,
                    result,
                    state,
                    detail if separator else "",
                ]
            )
            item.setData(0, Qt.UserRole, number)
            item.setData(0, Qt.UserRole + 1, "Running" in state.split())
            item.setData(0, Qt.UserRole + 2, actionable)
            item.setToolTip(4, detail)
            self.tasks.addTopLevelItem(item)
            if number == selected_number:
                self.tasks.setCurrentItem(item)
        self.update_delegate_button()

    def selected_row(self) -> int:
        item = self.queue.currentItem()
        return self.queue.indexOfTopLevelItem(item) if item else -1

    def move_selected(self, offset: int) -> None:
        row = self.selected_row()
        target = row + offset
        if row < 0 or target < 0 or target >= self.queue.topLevelItemCount():
            return
        item = self.queue.topLevelItem(row)
        other = self.queue.topLevelItem(target)
        if (
            item is None
            or other is None
            or item.data(0, Qt.UserRole + 1) != "Queued"
            or other.data(0, Qt.UserRole + 1) != "Queued"
        ):
            return
        item = self.queue.takeTopLevelItem(row)
        self.queue.insertTopLevelItem(target, item)
        self.queue.setCurrentItem(item)
        self.save_order()
        self.update_queue_buttons()

    def save_order(self) -> None:
        numbers = [
            self.queue.topLevelItem(index).data(0, Qt.UserRole)
            for index in range(self.queue.topLevelItemCount())
            if self.queue.topLevelItem(index).data(0, Qt.UserRole + 1)
            != "In progress"
        ]
        repo = self.selected_repo()
        if not repo:
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        order_file = self.queue_order_file(repo)
        temporary = order_file.with_suffix(".tmp")
        temporary.write_text("\n".join(numbers) + "\n")
        os.replace(temporary, order_file)
        self.show_transient_status("Queue order saved")

    def queue_order_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-order.txt"

    def open_pr_number(self, number: str) -> None:
        repo = self.selected_repo()
        if not repo or not number:
            return
        QDesktopServices.openUrl(
            QUrl(f"https://github.com/{repo}/pull/{number}")
        )

    def update_delegate_button(self) -> None:
        item = self.tasks.currentItem()
        self.delegate_button.setEnabled(
            item is not None
            and bool(item.data(0, Qt.UserRole + 2))
            and not bool(item.data(0, Qt.UserRole + 1))
            and self.delegate_process.state() == QProcess.NotRunning
        )

    def delegate_selected(self) -> None:
        item = self.tasks.currentItem()
        repo = self.selected_repo()
        if (
            item is None
            or not repo
            or not bool(item.data(0, Qt.UserRole + 2))
            or bool(item.data(0, Qt.UserRole + 1))
            or self.delegate_process.state() != QProcess.NotRunning
        ):
            return
        self.delegate_pr = item.data(0, Qt.UserRole)
        self.delegate_repo = repo
        self.delegate_button.setEnabled(False)
        self.delegate_button.setText("Delegating…")
        self.statusBar().showMessage(
            f"Delegating PR #{self.delegate_pr} feedback…"
        )
        self.delegate_process.setProgram(SCRIPT)
        self.delegate_process.setArguments(
            [
                "--repo",
                repo,
                "--delegate",
                self.delegate_pr,
            ]
        )
        self.delegate_process.start()
        QTimer.singleShot(1500, self.refresh)

    def delegate_finished(self, exit_code: int) -> None:
        stdout = bytes(self.delegate_process.readAllStandardOutput()).decode()
        stderr = bytes(self.delegate_process.readAllStandardError()).decode().strip()
        self.delegate_button.setText("Delegate selected")
        self.update_delegate_button()
        if exit_code != 0:
            self.show_transient_status("Delegation failed", 6000)
            QMessageBox.warning(
                self,
                "Delegation failed",
                stderr or stdout.strip() or "Unknown delegation error",
            )
        elif "already running" in stdout or "deferring review routing" in stdout:
            self.show_transient_status(
                f"PR #{self.delegate_pr}: matching agent task is already running",
                6000,
            )
        else:
            self.show_transient_status(
                f"PR #{self.delegate_pr}: delegation finished"
            )
        self.refresh()

    def toggle_monitor(self) -> None:
        if self.monitor_pid() is not None:
            self.stop_monitor()
        else:
            self.start_monitor()

    def start_monitor(self) -> None:
        repo = self.selected_repo()
        if (
            not repo
            or repo in self.starting_monitors
            or self.monitor_pid(repo) is not None
        ):
            return
        self.starting_monitors.add(repo)
        self.update_monitor_state()
        self.statusBar().showMessage("Starting monitor…")
        try:
            subprocess.Popen(
                [SCRIPT, "--repo", repo],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as error:
            self.starting_monitors.discard(repo)
            self.update_monitor_state()
            self.show_transient_status(
                f"Could not start monitor: {error}",
                6000,
            )
            return
        QTimer.singleShot(200, lambda: self.check_monitor_started(repo, 0))

    def check_monitor_started(self, repo: str, attempt: int) -> None:
        if self.monitor_pid(repo) is not None:
            self.starting_monitors.discard(repo)
            if repo == self.selected_repo():
                self.show_transient_status("Monitor started")
                self.update_monitor_state()
            return
        if attempt < 24:
            QTimer.singleShot(
                200,
                lambda: self.check_monitor_started(repo, attempt + 1),
            )
            return

        self.starting_monitors.discard(repo)
        if repo == self.selected_repo():
            self.show_transient_status(
                "Monitor finished immediately or could not acquire its lock",
                6000,
            )
            self.update_monitor_state()
            self.refresh()

    def stop_monitor(self) -> None:
        pid = self.monitor_pid()
        if pid is None:
            self.update_monitor_state()
            return
        answer = QMessageBox.question(
            self,
            "Stop monitor",
            "Stop the running CodeRabbit queue monitor?",
        )
        if answer == QMessageBox.Yes:
            try:
                # Monitors are started in a new session, so kill the whole group
                # and any waiting sleep children that inherited the lock fd.
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            self.show_transient_status("Monitor stopped")
            QTimer.singleShot(500, self.update_monitor_state)

    def stop_all_monitors_and_quit(self) -> None:
        answer = QMessageBox.question(
            self,
            "Quit and stop all monitors",
            "Stop every CodeRabbit queue monitor on this computer and quit?",
        )
        if answer != QMessageBox.Yes:
            return
        failed: list[int] = []
        for pid_file in Path("/tmp").glob("coderabbit-review-queue-*.pid"):
            pid = 0
            try:
                if pid_file.stat().st_uid != os.getuid():
                    continue
                pid = int(pid_file.read_text().strip())
                if not Path(f"/proc/{pid}").exists():
                    continue
                process_group = os.getpgid(pid)
                if process_group == pid:
                    os.killpg(process_group, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while (
                    pid_file.exists()
                    and process_is_running(pid)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                if pid_file.exists() and process_is_running(pid):
                    failed.append(pid)
            except ProcessLookupError:
                continue
            except (OSError, ValueError, PermissionError):
                if pid:
                    failed.append(pid)
        if failed:
            QMessageBox.warning(
                self,
                "Could not stop every monitor",
                "The queue window is still open because these monitor processes "
                f"did not stop: {', '.join(str(pid) for pid in failed)}",
            )
            return
        QApplication.instance().quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("CodeRabbit Review Queue")
    app.setDesktopFileName("coderabbit-review-queue")
    app.setWindowIcon(QIcon.fromTheme("system-software-update"))
    window = QueueWindow()
    window.show()
    if "--smoke-test" in sys.argv:
        QTimer.singleShot(500, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
