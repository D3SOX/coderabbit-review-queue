#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only

import os
import fcntl
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QDate, QDateTime, QLocale, QProcess, QSize, QTimer, Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSystemTrayIcon,
    QTextEdit,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from queue_gui_common import (
    APP_ICON_PATH, BABYSIT_DELEGATION_PROMPT, DEFAULT_DELEGATION_PROMPT,
    REPOSITORY_CACHE_FILE, SCRIPT, SELECTED_REPO_FILE, SHORT_DELEGATION_PROMPT,
    STATE_ROOT, STATUS_CACHE_MAX_AGE, app_icon, file_signature,
    live_review_number, proc_stat_is_running, process_is_running,
    ssh_config_hosts, status_belongs_to_repo,
)
from queue_gui_settings import SettingsMixin
from queue_gui_status import StatusMixin


class QueueWindow(SettingsMixin, StatusMixin, QMainWindow):
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
        self.displayed_repo = ""
        self.status_loading_repo = ""
        self.status_monitor_signature: tuple[int, int] | None = None
        self.status_completion_signature: tuple[int, int, int] | None = None
        self.status_failures = 0
        self.status_manual = False
        self.review_requests_signature: (
            tuple[
                str,
                tuple[int, int, int] | None,
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
        title_icon.setPixmap(app_icon().pixmap(32, 32))
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
        self.repo_combo.editTextChanged.connect(self.repository_text_changed)
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
            QIcon.fromTheme("configure"), "Configure approval actions…"
        )
        self.auto_merge_button.setToolTip(
            "Configure merging and Codex task archiving after a merge."
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
        excluded_label = QLabel("Excluded &branches:")
        self.excluded_branches = QLineEdit()
        excluded_label.setBuddy(self.excluded_branches)
        self.excluded_branches.setPlaceholderText(
            "Comma-separated head branch names, e.g. weblate-translations"
        )
        self.excluded_branches.setToolTip(
            "Per repository. Open PRs whose head branch matches are left out of "
            "the review queue and unresolved-feedback list."
        )
        self.excluded_branches.editingFinished.connect(self.excluded_branches_changed)
        excluded_row = QHBoxLayout()
        excluded_row.setSpacing(12)
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
        excluded_row.addWidget(authors_label)
        excluded_row.addWidget(self.excluded_authors, 1)
        self.notify_sound = QCheckBox(
            "Play a sound when a new review becomes available"
        )
        self.notify_sound.setToolTip(
            "Per repository. Plays when the CodeRabbit rate-limit window opens "
            "(a new review can start), not when a review finishes."
        )
        self.notify_sound.setChecked(True)
        self.notify_sound.toggled.connect(self.notify_sound_changed)
        self.notify_sound_button = QPushButton(
            QIcon.fromTheme("audio-volume-high"), "Configure sound…"
        )
        self.notify_sound_button.setEnabled(False)
        self.notify_sound_button.clicked.connect(self.configure_notify_sound)
        monitor_options_row = QHBoxLayout()
        monitor_options_row.setSpacing(12)
        monitor_options_row.addWidget(self.stop_when_empty)
        monitor_options_row.addWidget(self.ignore_drafts)
        monitor_options_row.addWidget(self.notify_sound)
        monitor_options_row.addStretch()
        monitor_options_row.addWidget(self.notify_sound_button)
        self.new_items_at_top = QCheckBox("Add new queue items at the top")
        self.new_items_at_top.setToolTip(
            "Per repository. When disabled, newly discovered PRs are appended at the bottom."
        )
        self.new_items_at_top.setChecked(True)
        self.new_items_at_top.toggled.connect(self.new_items_at_top_changed)
        self.requeued_items_at_top = QCheckBox("Requeue reviewed PRs at the top")
        self.requeued_items_at_top.setToolTip(
            "Per repository. Controls where a PR returns after its review; "
            "newly discovered PRs use the setting beside it."
        )
        self.requeued_items_at_top.setChecked(True)
        self.requeued_items_at_top.toggled.connect(
            self.requeued_items_at_top_changed
        )
        queue_placement_row = QHBoxLayout()
        queue_placement_row.setSpacing(12)
        queue_placement_row.addWidget(self.new_items_at_top)
        queue_placement_row.addWidget(self.requeued_items_at_top)
        queue_placement_row.addStretch()

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
            ["PR", "Title", "Result", "Agent status", "Task", "Latest progress"]
        )
        self.tasks.setRootIsDecorated(False)
        self.tasks.setAlternatingRowColors(True)
        self.tasks.header().resizeSection(0, 90)
        self.tasks.header().resizeSection(1, 300)
        self.tasks.header().resizeSection(2, 120)
        self.tasks.header().resizeSection(3, 130)
        self.tasks.header().resizeSection(4, 260)
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
        self.delegate_all_button = QPushButton(
            QIcon.fromTheme("system-run"),
            "Delegate all idle",
        )
        self.delegate_all_button.setEnabled(False)
        self.delegate_all_button.clicked.connect(self.delegate_all_idle)
        task_buttons = QHBoxLayout()
        task_buttons.addStretch()
        task_buttons.addWidget(self.delegate_all_button)
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
        layout.addLayout(monitor_options_row)
        layout.addLayout(excluded_row)
        layout.addLayout(queue_placement_row)
        layout.addLayout(header_buttons)
        layout.addWidget(self.table_splitter, 1)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.tray_icon = QSystemTrayIcon(app_icon(), self)
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

    def repository_text_changed(self, _text: str) -> None:
        if self.selected_repo() == self.displayed_repo:
            return
        self.displayed_repo = ""
        self.status_loading_repo = self.selected_repo()
        self.queue.clear()
        self.tasks.clear()
        self.active_reviews = []
        self.finished_review_numbers = set()
        self.approved_review_numbers = set()
        self.has_queued_reviews = False
        self.next_review_at = None
        self.monitor_activity = None
        self.update_queue_buttons()
        self.update_delegate_button()
        self.update_countdown_display()

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
            self.tray_icon.setIcon(app_icon())
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
        if selected:
            self.activate_repo(selected)

    def activate_repo(self, repo: str) -> None:
        if self.displayed_repo != repo:
            self.displayed_repo = repo
            self.status_loading_repo = repo
            self.queue.clear()
            self.tasks.clear()
            self.active_reviews = []
            self.finished_review_numbers = set()
            self.approved_review_numbers = set()
            self.has_queued_reviews = False
            self.next_review_at = None
            self.monitor_activity = None
            self.update_queue_buttons()
            self.update_delegate_button()
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
        self.load_requeued_items_at_top(repo)
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
        if not status_belongs_to_repo(status, repo):
            return False
        self.status_loading_repo = ""
        self.populate_queue(status)
        self.populate_tasks(status)
        newer_state_exists = False
        for path in (
            self.review_requests_file(repo),
            self.monitor_state_file(repo),
            self.review_completion_file(repo),
        ):
            try:
                if path.stat().st_mtime > cache_mtime:
                    newer_state_exists = True
                    break
            except (OSError, TypeError):
                continue
        return age <= STATUS_CACHE_MAX_AGE and not newer_state_exists

    def save_status_cache(self, repo: str, status: str) -> None:
        if not status_belongs_to_repo(status, repo):
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.status_cache_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(status.rstrip() + "\n")
        os.replace(temporary, target)

    def open_pr_number(self, number: str) -> None:
        repo = self.selected_repo()
        if not repo or not number:
            return
        QDesktopServices.openUrl(
            QUrl(f"https://github.com/{repo}/pull/{number}")
        )

    def update_delegate_button(self) -> None:
        item = self.tasks.currentItem()
        process_idle = self.delegate_process.state() == QProcess.NotRunning
        self.delegate_button.setEnabled(
            item is not None
            and bool(self.selected_repo())
            and bool(item.data(0, Qt.UserRole + 2))
            and not bool(item.data(0, Qt.UserRole + 1))
            and process_idle
        )
        has_idle = any(
            bool(self.tasks.topLevelItem(index).data(0, Qt.UserRole + 2))
            and not bool(self.tasks.topLevelItem(index).data(0, Qt.UserRole + 1))
            for index in range(self.tasks.topLevelItemCount())
        )
        self.delegate_all_button.setEnabled(
            bool(self.selected_repo()) and has_idle and process_idle
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

    def delegate_all_idle(self) -> None:
        repo = self.selected_repo()
        if not repo or self.delegate_process.state() != QProcess.NotRunning:
            return
        self.delegate_pr = ""
        self.delegate_repo = repo
        self.delegate_button.setEnabled(False)
        self.delegate_all_button.setEnabled(False)
        self.delegate_all_button.setText("Delegating all…")
        self.statusBar().showMessage("Delegating all reachable idle tasks…")
        self.delegate_process.setProgram(SCRIPT)
        self.delegate_process.setArguments(["--repo", repo, "--delegate-all"])
        self.delegate_process.start()

    def mark_delegation_running(self, number: str) -> None:
        for index in range(self.tasks.topLevelItemCount()):
            item = self.tasks.topLevelItem(index)
            if item.data(0, Qt.UserRole) != number:
                continue
            agent = item.text(3).partition(" ")[0] or "Codex"
            item.setText(3, f"{agent} Running")
            item.setData(0, Qt.UserRole + 1, True)
            break
        self.update_delegate_button()

    def delegate_finished(self, exit_code: int) -> None:
        stdout = bytes(self.delegate_process.readAllStandardOutput()).decode()
        stderr = bytes(self.delegate_process.readAllStandardError()).decode().strip()
        self.delegate_button.setText("Delegate selected")
        self.delegate_all_button.setText("Delegate all idle")
        for number in set(re.findall(r"PR #(\d+)", stdout)):
            if (
                "Routing unresolved CodeRabbit review on PR #" + number in stdout
                or "for PR #" + number + " is already running" in stdout
            ):
                self.mark_delegation_running(number)
        if exit_code != 0:
            self.update_delegate_button()
            self.show_transient_status("Delegation failed", 6000)
            QMessageBox.warning(
                self,
                "Delegation failed",
                stderr or stdout.strip() or "Unknown delegation error",
            )
        elif not self.delegate_pr:
            self.update_delegate_button()
            started = len(
                set(re.findall(r"Routing unresolved CodeRabbit review on PR #(\d+)", stdout))
            )
            self.show_transient_status(
                f"Started {started} idle agent task{'' if started == 1 else 's'}"
            )
        elif (
            "already running" in stdout
            or "started while routing" in stdout
            or "Routing unresolved CodeRabbit review" in stdout
        ):
            if self.delegate_pr:
                self.mark_delegation_running(self.delegate_pr)
            if "Routing unresolved CodeRabbit review" in stdout:
                self.show_transient_status(
                    f"PR #{self.delegate_pr}: agent task started"
                )
            else:
                self.show_transient_status(
                    f"PR #{self.delegate_pr}: matching agent task is already running",
                    6000,
                )
        else:
            self.update_delegate_button()
            self.show_transient_status(
                f"PR #{self.delegate_pr}: delegation was not started",
                6000,
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
    window = QueueWindow()
    window.show()
    if "--smoke-test" in sys.argv:
        QTimer.singleShot(500, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
