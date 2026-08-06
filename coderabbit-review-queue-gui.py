#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only

import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QDate, QDateTime, QLocale, QProcess, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
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


class QueueWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.base_window_title = "CodeRabbit Review Queue"
        self.setWindowTitle(self.base_window_title)
        self.resize(920, 620)
        self.next_review_at: QDateTime | None = None
        self.has_queued_reviews = False
        self.active_reviews: list[tuple[str, str]] = []
        self.status_process = QProcess(self)
        self.status_process.finished.connect(self.status_finished)
        self.status_repo = ""
        self.status_failures = 0
        self.status_manual = False
        self.status_retry_timer = QTimer(self)
        self.status_retry_timer.setSingleShot(True)
        self.status_retry_timer.timeout.connect(self.retry_status)
        self.repo_process = QProcess(self)
        self.repo_process.finished.connect(self.repos_finished)
        self.delegate_process = QProcess(self)
        self.delegate_process.finished.connect(self.delegate_finished)
        self.delegate_repo = ""
        self.delegate_pr = ""
        self.starting_monitors: set[str] = set()

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

        self.auto_delegate = QCheckBox(
            "Automatically delegate unresolved CodeRabbit feedback to Codex"
        )
        self.auto_delegate.setToolTip(
            "Per repository. A running matching Codex task is never started again."
        )
        self.auto_delegate.toggled.connect(self.auto_delegate_changed)
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
        self.notify_sound = QCheckBox(
            "Play a sound when a new review becomes available"
        )
        self.notify_sound.setToolTip(
            "Per repository. Uses the desktop notification sound when CodeRabbit finishes a review."
        )
        self.notify_sound.setChecked(True)
        self.notify_sound.toggled.connect(self.notify_sound_changed)

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

        task_label = QLabel("PRs with unresolved feedback / Codex task progress")
        self.tasks = QTreeWidget()
        self.tasks.setHeaderLabels(
            ["PR", "Title", "Feedback", "Codex status", "Latest progress"]
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
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button.clicked.connect(lambda: self.move_selected(1))

        queue_buttons = QHBoxLayout()
        queue_buttons.addStretch()
        queue_buttons.addWidget(self.up_button)
        queue_buttons.addWidget(self.down_button)

        self.delegate_button = QPushButton(
            QIcon.fromTheme("system-run"),
            "Delegate selected to Codex",
        )
        self.delegate_button.setEnabled(False)
        self.delegate_button.clicked.connect(self.delegate_selected)
        task_buttons = QHBoxLayout()
        task_buttons.addStretch()
        task_buttons.addWidget(self.delegate_button)

        layout = QVBoxLayout()
        layout.addLayout(title_row)
        layout.addLayout(repo_row)
        layout.addWidget(self.auto_delegate)
        layout.addWidget(self.stop_when_empty)
        layout.addWidget(self.ignore_drafts)
        layout.addLayout(excluded_row)
        layout.addWidget(self.notify_sound)
        layout.addLayout(header_buttons)
        layout.addWidget(queue_label)
        layout.addWidget(self.queue, 1)
        layout.addLayout(queue_buttons)
        layout.addWidget(task_label)
        layout.addWidget(self.tasks, 1)
        layout.addLayout(task_buttons)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.tray_icon = QSystemTrayIcon(QIcon.fromTheme("system-software-update"), self)
        tray_menu = QMenu(self)
        show_action = QAction("Open CodeRabbit queue", self)
        show_action.triggered.connect(self.show_from_tray)
        refresh_action = QAction("Refresh status", self)
        refresh_action.triggered.connect(lambda: self.refresh(manual=True))
        quit_action = QAction("Quit queue window", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        tray_menu.addAction(show_action)
        tray_menu.addAction(refresh_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
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

    def tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_from_tray()

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
        self.load_auto_delegate(selected)
        self.load_stop_when_empty(selected)
        self.load_ignore_drafts(selected)
        self.load_excluded_branches(selected)
        self.load_notify_sound(selected)
        self.update_monitor_state()
        if selected:
            self.refresh()
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
        self.status_retry_timer.stop()
        self.status_failures = 0
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = SELECTED_REPO_FILE.with_suffix(".tmp")
        temporary.write_text(repo + "\n")
        os.replace(temporary, SELECTED_REPO_FILE)
        self.load_auto_delegate(repo)
        self.load_stop_when_empty(repo)
        self.load_ignore_drafts(repo)
        self.load_excluded_branches(repo)
        self.load_notify_sound(repo)
        self.update_monitor_state()
        self.refresh()

    def auto_delegate_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-auto-delegate"

    def load_auto_delegate(self, repo: str) -> None:
        self.auto_delegate.blockSignals(True)
        self.auto_delegate.setEnabled(bool(repo))
        if repo:
            try:
                enabled = self.auto_delegate_file(repo).read_text().strip() != "0"
            except OSError:
                enabled = False
            self.auto_delegate.setChecked(enabled)
        else:
            self.auto_delegate.setChecked(False)
        self.auto_delegate.blockSignals(False)

    def auto_delegate_changed(self, enabled: bool) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.auto_delegate_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("1\n" if enabled else "0\n")
        os.replace(temporary, target)
        self.show_transient_status(
            "Automatic Codex delegation enabled"
            if enabled
            else "Automatic Codex delegation disabled"
        )

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
        return pid if Path(f"/proc/{pid}").exists() else None

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
        self.status_process.setArguments(["--repo", repo, "--status"])
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

    def update_countdown_display(self) -> None:
        self.setWindowTitle(self.base_window_title)
        repo = self.selected_repo() or self.base_window_title
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
            return

        if not self.has_queued_reviews:
            self.timer_label.setText("Next review: —")
            self.set_tray_countdown(None, f"{repo}\nNo reviews waiting")
            return

        if self.next_review_at is None:
            self.timer_label.setText("Next review: Available now")
            self.set_tray_countdown("✓", f"{repo}\nReview available now")
            return

        seconds = QDateTime.currentDateTime().secsTo(self.next_review_at)
        if seconds <= 0:
            self.timer_label.setText("Next review: Available now")
            self.set_tray_countdown("✓", f"{repo}\nReview available now")
            return

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
            elif line.startswith("Unresolved CodeRabbit feedback:"):
                section = "feedback"
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
            queued = list(queued_by_number.items()) + saved_queued

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
        self.update_queue_buttons()

    def update_queue_buttons(self) -> None:
        item = self.queue.currentItem()
        row = self.selected_row()
        movable = (
            item is not None
            and item.data(0, Qt.UserRole + 1) != "In progress"
            and self.queue.topLevelItemCount() > 1
        )
        self.up_button.setEnabled(movable and row > 0)
        self.down_button.setEnabled(
            movable and 0 <= row < self.queue.topLevelItemCount() - 1
        )

    def populate_tasks(self, status: str) -> None:
        current_item = self.tasks.currentItem()
        selected_number = (
            current_item.data(0, Qt.UserRole) if current_item is not None else None
        )
        rows: list[tuple[str, str, str]] = []
        current: tuple[str, str, str] | None = None
        in_feedback = False

        for line in status.splitlines():
            if line.startswith("Unresolved CodeRabbit feedback:"):
                in_feedback = True
                continue
            if not in_feedback:
                continue

            feedback = re.fullmatch(
                r"  #(\d+) (.+) \((\d+) unresolved\)",
                line,
            )
            if feedback:
                current = (
                    feedback.group(1),
                    feedback.group(2),
                    feedback.group(3),
                )
                continue
            if current and line.startswith("    Codex task: "):
                rows.append((*current, line.removeprefix("    Codex task: ")))
                current = None

        self.tasks.clear()
        for number, title, unresolved, progress in rows:
            state, separator, detail = progress.partition(" — ")
            item = QTreeWidgetItem(
                [
                    f"#{number}",
                    title,
                    f"{unresolved} unresolved",
                    state,
                    detail if separator else "",
                ]
            )
            item.setData(0, Qt.UserRole, number)
            item.setData(0, Qt.UserRole + 1, state.startswith("Running"))
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
            or item.data(0, Qt.UserRole + 1) == "In progress"
            or other.data(0, Qt.UserRole + 1) == "In progress"
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
            and not bool(item.data(0, Qt.UserRole + 1))
            and self.delegate_process.state() == QProcess.NotRunning
        )

    def delegate_selected(self) -> None:
        item = self.tasks.currentItem()
        repo = self.selected_repo()
        if (
            item is None
            or not repo
            or bool(item.data(0, Qt.UserRole + 1))
            or self.delegate_process.state() != QProcess.NotRunning
        ):
            return
        self.delegate_pr = item.data(0, Qt.UserRole)
        self.delegate_repo = repo
        self.delegate_button.setEnabled(False)
        self.delegate_button.setText("Delegating…")
        self.statusBar().showMessage(
            f"Delegating PR #{self.delegate_pr} feedback to Codex…"
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
        self.delegate_button.setText("Delegate selected to Codex")
        self.update_delegate_button()
        if exit_code != 0:
            self.show_transient_status("Codex delegation failed", 6000)
            QMessageBox.warning(
                self,
                "Delegation failed",
                stderr or stdout.strip() or "Unknown delegation error",
            )
        elif "already running; deferring" in stdout:
            self.show_transient_status(
                f"PR #{self.delegate_pr}: matching Codex task is already running",
                6000,
            )
        else:
            self.show_transient_status(
                f"PR #{self.delegate_pr}: Codex delegation finished"
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
