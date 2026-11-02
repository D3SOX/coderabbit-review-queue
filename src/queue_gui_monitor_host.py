# SPDX-License-Identifier: GPL-3.0-only

import json
import os
import re
from pathlib import Path

from PySide6.QtCore import QProcess, QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from queue_gui_common import SCRIPT, STATE_ROOT, file_signature, ssh_config_hosts

# Mirrors monitor_setting_suffixes in lib/monitor-host.sh.
MONITOR_SETTING_SUFFIXES = (
    "agent-host", "archive-after-merge", "auto-delegate", "auto-merge",
    "delegation-prompt-mode", "delegation-prompt-template", "delete-branch",
    "excluded-authors", "excluded-branches", "ignore-drafts", "merge-admin",
    "merge-after-approval", "merge-after-delegation", "merge-method",
    "new-items-at-top", "notifications", "notify-sound", "notify-sound-path",
    "notify-sound-volume", "requeued-items-at-top", "resolve-merge-conflicts",
    "steer-running-agents", "stop-when-empty",
)
# Mirrors monitor_mirror_suffixes in lib/monitor-host.sh.
MONITOR_MIRROR_SUFFIXES = (
    "monitor-state.tsv", "order.txt", "review-completion.tsv", "review-requests.tsv",
)
REMOTE_MIRROR_ROOT = STATE_ROOT / "remote-monitors"
QUEUE_HOSTS_FILE = STATE_ROOT / "queue-hosts"
HOST_PATTERN = r"[A-Za-z0-9_.@-]+"
THIS_DEVICE = "This device"


def queue_hosts() -> list[str]:
    """Devices with the queue installed; only these are probed and offered."""
    try:
        lines = QUEUE_HOSTS_FILE.read_text().splitlines()
    except OSError:
        return []
    hosts: list[str] = []
    for line in lines:
        host = line.strip()
        if re.fullmatch(HOST_PATTERN, host) and host not in hosts:
            hosts.append(host)
    return hosts


def save_queue_hosts(hosts: list[str]) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = QUEUE_HOSTS_FILE.with_name(QUEUE_HOSTS_FILE.name + ".tmp")
    temporary.write_text("".join(f"{host}\n" for host in hosts))
    os.replace(temporary, QUEUE_HOSTS_FILE)


def move_batch(
    repos: list[str], checked: list[str], every_running: bool, target: str,
) -> list[tuple[str, list[str]]]:
    """Checked repositories move as chosen; "all running" moves only running ones."""
    if every_running:
        return [(repo, ["--move-monitor", target, "--if-running"]) for repo in repos]
    return [(repo, ["--move-monitor", target]) for repo in repos if repo in checked]


def command_output(process: QProcess) -> tuple[str, str]:
    stdout = bytes(process.readAllStandardOutput()).decode(errors="replace").strip()
    stderr = bytes(process.readAllStandardError()).decode(errors="replace").strip()
    return stdout, stderr


class MonitorHostMixin:
    """Monitors can run on another device; this window controls them over SSH."""

    def init_monitor_host(self) -> None:
        # repo -> (host, pid) from the last successful poll of that host.
        self.remote_monitors: dict[str, tuple[str, int | None]] = {}
        self.remote_monitor_errors: dict[str, str] = {}
        self.pushed_settings: dict[str, tuple] = {}
        self.monitor_info_process = QProcess(self)
        self.monitor_info_process.finished.connect(self.monitor_info_finished)
        self.monitor_info_repo = ""
        self.monitor_info_host = ""
        self.settings_push_process = QProcess(self)
        self.settings_push_process.finished.connect(self.settings_push_finished)
        self.settings_push_repo = ""
        self.settings_push_signature: tuple = ()
        self.monitor_command_process = QProcess(self)
        self.monitor_command_process.finished.connect(self.monitor_command_finished)
        self.monitor_command_repo = ""
        self.monitor_command_kind = ""
        self.move_batch: list[tuple[str, list[str]]] = []
        self.move_target_label = ""
        self.move_results: list[str] = []
        self.move_failed = False
        self.sync_process = QProcess(self)
        self.sync_process.finished.connect(self.sync_step_finished)
        self.sync_pending: list[str] = []
        self.sync_host = ""
        self.sync_direction = ""
        self.sync_results: list[str] = []
        self.sync_failed = False

    def monitor_host_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-monitor-host"

    def monitor_host(self, repo: str | None = None) -> str:
        repo = repo or self.selected_repo()
        if not repo:
            return ""
        try:
            host = self.monitor_host_file(repo).read_text().strip()
        except OSError:
            return ""
        return host if re.fullmatch(r"[A-Za-z0-9_.@-]+", host) else ""

    def repo_state_file(self, repo: str, suffix: str) -> Path:
        """A remote monitor's live files are read from their local mirror."""
        name = f"{repo.replace('/', '__')}-{suffix}"
        if suffix in MONITOR_MIRROR_SUFFIXES and self.monitor_host(repo):
            return REMOTE_MIRROR_ROOT / name
        return STATE_ROOT / name

    def remote_monitor_pid(self, repo: str) -> int | None:
        host = self.monitor_host(repo)
        known = self.remote_monitors.get(repo)
        if not host or known is None or known[0] != host:
            return None
        return known[1]

    def monitor_location_label(self, repo: str) -> str:
        host = self.monitor_host(repo)
        if not host:
            return ""
        if repo in self.remote_monitor_errors:
            return f" on {host} (unreachable)"
        if self.remote_monitors.get(repo, ("", None))[0] != host:
            return f" on {host} (checking…)"
        return f" on {host}"

    def poll_remote_monitor(self, repo: str) -> None:
        host = self.monitor_host(repo)
        if not host:
            self.remote_monitors.pop(repo, None)
            self.remote_monitor_errors.pop(repo, None)
            return
        self.push_settings_if_changed(repo)
        if self.monitor_info_process.state() != QProcess.NotRunning:
            return
        self.monitor_info_repo = repo
        self.monitor_info_host = host
        self.monitor_info_process.setProgram(SCRIPT)
        self.monitor_info_process.setArguments(["--repo", repo, "--monitor-info"])
        self.monitor_info_process.start()

    def monitor_info_finished(self, exit_code: int) -> None:
        stdout, stderr = command_output(self.monitor_info_process)
        repo, host = self.monitor_info_repo, self.monitor_info_host
        if self.monitor_host(repo) != host:
            return
        try:
            info = json.loads(stdout) if exit_code == 0 else None
        except json.JSONDecodeError:
            info = None
        if not isinstance(info, dict):
            self.remote_monitor_errors[repo] = stderr or stdout or "No response"
        else:
            self.remote_monitor_errors.pop(repo, None)
            pid = info.get("pid")
            self.remote_monitors[repo] = (host, pid if isinstance(pid, int) else None)
            files = info.get("files")
            self.write_remote_mirror(repo, files if isinstance(files, dict) else {})
        if repo == self.selected_repo():
            self.render_monitor_state()

    def write_remote_mirror(self, repo: str, files: dict) -> None:
        REMOTE_MIRROR_ROOT.mkdir(parents=True, exist_ok=True)
        for suffix in MONITOR_MIRROR_SUFFIXES:
            target = REMOTE_MIRROR_ROOT / f"{repo.replace('/', '__')}-{suffix}"
            entry = files.get(suffix)
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                target.unlink(missing_ok=True)
                continue
            try:
                current = target.read_text()
            except OSError:
                current = None
            # Rewrite only on change: the status code watches file signatures.
            if current != entry["text"]:
                temporary = target.with_name(target.name + ".tmp")
                temporary.write_text(entry["text"])
                os.replace(temporary, target)
                mtime = entry.get("mtime")
                if isinstance(mtime, int):
                    os.utime(target, (mtime, mtime))

    def settings_signature(self, repo: str) -> tuple:
        key = repo.replace("/", "__")
        return tuple(
            file_signature(STATE_ROOT / f"{key}-{suffix}") for suffix in MONITOR_SETTING_SUFFIXES
        )

    def push_settings_if_changed(self, repo: str) -> None:
        """Settings stay editable here; send every change to the monitor's host."""
        signature = self.settings_signature(repo)
        if self.pushed_settings.get(repo) == signature:
            return
        if self.settings_push_process.state() != QProcess.NotRunning:
            return
        self.settings_push_repo = repo
        self.settings_push_signature = signature
        self.settings_push_process.setProgram(SCRIPT)
        self.settings_push_process.setArguments(["--repo", repo, "--push-settings"])
        self.settings_push_process.start()

    def settings_push_finished(self, exit_code: int) -> None:
        stdout, stderr = command_output(self.settings_push_process)
        repo = self.settings_push_repo
        if exit_code == 0:
            self.pushed_settings[repo] = self.settings_push_signature
            return
        # Retried on the next monitor poll; report each failure once.
        if self.pushed_settings.get(repo) != ("failed",):
            self.pushed_settings[repo] = ("failed",)
            self.show_transient_status(
                f"Could not send settings to {self.monitor_host(repo)}: "
                f"{(stderr or stdout or 'no response').splitlines()[-1]}",
                8000,
            )

    def run_monitor_command(self, repo: str, kind: str, arguments: list[str], message: str) -> bool:
        if self.monitor_command_process.state() != QProcess.NotRunning:
            self.show_transient_status("Another monitor command is still running", 4000)
            return False
        self.monitor_command_repo = repo
        self.monitor_command_kind = kind
        self.monitor_command_process.setProgram(SCRIPT)
        self.monitor_command_process.setArguments(["--repo", repo, *arguments])
        self.monitor_command_process.start()
        self.statusBar().showMessage(message)
        self.render_monitor_state()
        return True

    def monitor_command_running(self, repo: str) -> bool:
        return (
            self.monitor_command_process.state() != QProcess.NotRunning
            and self.monitor_command_repo == repo
        )

    def monitor_command_finished(self, exit_code: int) -> None:
        stdout, stderr = command_output(self.monitor_command_process)
        repo, kind = self.monitor_command_repo, self.monitor_command_kind
        self.starting_monitors.discard(repo)
        # Poll the (possibly new) host right away instead of trusting old state.
        self.remote_monitors.pop(repo, None)
        self.remote_monitor_errors.pop(repo, None)
        self.pushed_settings.pop(repo, None)
        if kind == "move":
            # Moves run as a batch; report them together at the end.
            message = (stdout if exit_code == 0 else stderr) or stdout or stderr
            if exit_code != 0:
                self.move_failed = True
                message = f"{repo}: {message.splitlines()[-1] if message else 'failed'}"
            # Keep the "now belongs to" line rather than the start message after it.
            moved = [line for line in stdout.splitlines() if "now belongs to" in line]
            line = moved[-1] if exit_code == 0 and moved else (message.splitlines()[-1] if message else "")
            if line and "already belongs" not in line and "is not running" not in line:
                self.move_results.append(line)
            self.start_next_move()
            return
        if exit_code == 0:
            lines = stdout.splitlines()
            self.show_transient_status(lines[-1] if lines else "Done", 6000)
        else:
            titles = {
                "start": "Could not start monitor",
                "stop": "Could not stop monitor",
            }
            QMessageBox.warning(
                self, titles.get(kind, "Monitor command failed"),
                "\n".join(filter(None, [stderr, stdout])) or "The command failed.",
            )
        if repo == self.selected_repo():
            self.review_requests_signature = None
            self.update_monitor_state()
            if kind == "move":
                self.load_agent_host(repo)
                self.refresh()

    def start_monitor_via_cli(self, repo: str) -> None:
        host = self.monitor_host(repo)
        self.starting_monitors.add(repo)
        if not self.run_monitor_command(
            repo, "start", ["--start-monitor"],
            f"Starting monitor on {host}…" if host
            else "Starting monitor (checking other devices first)…",
        ):
            self.starting_monitors.discard(repo)

    def stop_remote_monitor(self, repo: str) -> None:
        host = self.monitor_host(repo)
        answer = QMessageBox.question(
            self, "Stop monitor", f"Stop the CodeRabbit queue monitor on {host}?",
        )
        if answer == QMessageBox.Yes:
            self.run_monitor_command(
                repo, "stop", ["--stop-monitor"], f"Stopping monitor on {host}…"
            )

    def move_monitor(self) -> None:
        repo = self.selected_repo()
        if not repo or self.monitor_command_process.state() != QProcess.NotRunning:
            return
        current = self.monitor_host(repo) or THIS_DEVICE
        hosts = queue_hosts()
        if not hosts and current == THIS_DEVICE:
            QMessageBox.information(
                self, "Move monitor",
                "Add the devices that have the queue installed in Devices… first.",
            )
            return
        choices = [THIS_DEVICE] + hosts
        if current not in choices:
            choices.append(current)
        repos = [self.repo_combo.itemText(index) for index in range(self.repo_combo.count())]
        if repo not in repos:
            repos.insert(0, repo)
        dialog = QDialog(self)
        dialog.setWindowTitle("Move monitor")
        intro = QLabel(
            "Running monitors are stopped, their settings and queue state are copied, "
            "and they start again on the chosen device."
        )
        intro.setWordWrap(True)
        device = QComboBox()
        device.addItems(choices)
        device.setCurrentIndex(choices.index(current))
        projects = QListWidget()
        # True or False once known; None while a remote device is asked;
        # "unreachable" when it did not answer.
        running: dict[str, bool | str | None] = {
            name: None if self.monitor_host(name) else self.monitor_pid(name) is not None
            for name in repos
        }

        def describe(name: str) -> str:
            host, state = self.monitor_host(name), running.get(name)
            if not host:
                return f"{name} (running here)" if state else f"{name} (stopped)"
            if state is None:
                return f"{name} (on {host}, checking…)"
            if state == "unreachable":
                return f"{name} (on {host}, unreachable)"
            return f"{name} (running on {host})" if state else f"{name} (stopped, on {host})"

        for name in repos:
            item = QListWidgetItem(describe(name))
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if name == repo else Qt.Unchecked)
            projects.addItem(item)

        # Open wide enough for the longest label a row can get once its
        # remote state arrives, so that no row needs a horizontal scrollbar.
        def longest_labels(name: str) -> list[str]:
            host = self.monitor_host(name)
            if not host:
                return [f"{name} (running here)"]
            return [f"{name} (on {host}, unreachable)", f"{name} (stopped, on {host})"]

        metrics = projects.fontMetrics()
        style = projects.style()
        projects.setMinimumWidth(
            max((metrics.horizontalAdvance(text) for name in repos for text in longest_labels(name)),
                default=0)
            + style.pixelMetric(style.PixelMetric.PM_IndicatorWidth)
            + style.pixelMetric(style.PixelMetric.PM_ScrollBarExtent)
            + 6 * style.pixelMetric(style.PixelMetric.PM_LayoutHorizontalSpacing, None, projects)
            + 2 * projects.frameWidth()
        )
        every_running = QCheckBox("All running monitors")
        every_running.setToolTip(
            "Move every repository whose monitor is running; stopped monitors stay where they are"
        )
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        move_button = buttons.button(QDialogButtonBox.Ok)
        move_button.setText("Move")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        chosen: list[Qt.CheckState] = []

        def update_controls() -> None:
            # Lock the rows, not the list, so that it still scrolls.
            locked = every_running.isChecked()
            with QSignalBlocker(projects):
                for index in range(projects.count()):
                    item = projects.item(index)
                    if locked:
                        item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                    else:
                        item.setFlags(item.flags() | Qt.ItemIsEnabled)
            move_button.setEnabled(every_running.isChecked() or any(
                projects.item(index).checkState() == Qt.Checked
                for index in range(projects.count())
            ))

        def toggle_every_running(enabled: bool) -> None:
            # "All running" picks the repositories itself: lock the list with
            # the running ones ticked, and restore the user's choice afterwards.
            with QSignalBlocker(projects):
                for index in range(projects.count()):
                    item = projects.item(index)
                    if enabled:
                        chosen.append(item.checkState())
                        item.setCheckState(
                            Qt.Checked if running.get(item.data(Qt.UserRole)) is True
                            else Qt.Unchecked
                        )
                    else:
                        item.setCheckState(chosen[index])
                if not enabled:
                    chosen.clear()
            update_controls()

        def remote_state_known(index: int, process: QProcess, exit_code: int) -> None:
            stdout, _stderr = command_output(process)
            name = repos[index]
            try:
                info = json.loads(stdout) if exit_code == 0 else None
            except json.JSONDecodeError:
                info = None
            running[name] = (
                isinstance(info.get("pid"), int) if isinstance(info, dict) else "unreachable"
            )
            item = projects.item(index)
            with QSignalBlocker(projects):
                item.setText(describe(name))
                if every_running.isChecked():
                    item.setCheckState(Qt.Checked if running[name] is True else Qt.Unchecked)

        probes: list[QProcess] = []
        for index, name in enumerate(repos):
            if running[name] is not None:
                continue
            process = QProcess(dialog)
            process.finished.connect(
                lambda code, _status, index=index, process=process:
                remote_state_known(index, process, code)
            )
            process.setProgram(SCRIPT)
            process.setArguments(["--repo", name, "--monitor-info"])
            process.start()
            probes.append(process)

        every_running.toggled.connect(toggle_every_running)
        projects.itemChanged.connect(update_controls)
        update_controls()
        form = QFormLayout()
        form.addRow("Device", device)
        layout = QVBoxLayout(dialog)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(QLabel("Repositories"))
        layout.addWidget(projects)
        layout.addWidget(every_running)
        layout.addWidget(buttons)
        accepted = dialog.exec() == QDialog.Accepted
        for process in probes:
            if process.state() != QProcess.NotRunning:
                process.kill()
                process.waitForFinished(1000)
        if not accepted:
            return
        target = device.currentText()
        checked = [
            projects.item(index).data(Qt.UserRole)
            for index in range(projects.count())
            if (chosen[index] if chosen else projects.item(index).checkState()) == Qt.Checked
        ]
        self.move_batch = move_batch(
            repos, checked, every_running.isChecked(),
            "local" if target == THIS_DEVICE else target,
        )
        self.move_target_label = "this device" if target == THIS_DEVICE else target
        self.move_results = []
        self.move_failed = False
        self.start_next_move()

    def start_next_move(self) -> None:
        if not self.move_batch:
            summary = "\n".join(self.move_results) or "Nothing was moved."
            if self.move_failed:
                QMessageBox.warning(self, "Could not move every monitor", summary)
            else:
                lines = summary.splitlines()
                self.show_transient_status(
                    lines[-1] if len(lines) == 1
                    else f"Moved the running monitors to {self.move_target_label}",
                    6000,
                )
            repo = self.selected_repo()
            if repo:
                self.review_requests_signature = None
                self.update_monitor_state()
                self.load_agent_host(repo)
                self.refresh()
            return
        repo, arguments = self.move_batch.pop(0)
        self.run_monitor_command(
            repo, "move", arguments, f"Moving the monitor for {repo} to {self.move_target_label}…"
        )

    def configure_devices(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Devices")
        intro = QLabel(
            "Devices with the queue installed. Only these are checked for a running "
            "monitor before one starts, and offered by Move monitor and Sync settings."
        )
        intro.setWordWrap(True)
        hosts = QListWidget()
        configured = queue_hosts()
        for host in configured + [h for h in ssh_config_hosts() if h not in configured]:
            item = QListWidgetItem(host)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if host in configured else Qt.Unchecked)
            hosts.addItem(item)
        detect = QPushButton("Detect")
        detect.setToolTip("Connect to every SSH host and check which have the queue installed")
        detected = QProcess(dialog)

        def start_detection() -> None:
            detect.setEnabled(False)
            detect.setText("Detecting…")
            detected.setProgram(SCRIPT)
            detected.setArguments(["--detect-queue-hosts"])
            detected.start()

        def detection_finished(exit_code: int) -> None:
            stdout, _stderr = command_output(detected)
            found = set(stdout.split()) if exit_code == 0 else set()
            for index in range(hosts.count()):
                item = hosts.item(index)
                item.setCheckState(Qt.Checked if item.text() in found else Qt.Unchecked)
            detect.setEnabled(True)
            detect.setText("Detect")

        detect.clicked.connect(start_detection)
        detected.finished.connect(detection_finished)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(intro)
        layout.addWidget(hosts)
        layout.addWidget(detect)
        layout.addWidget(buttons)
        accepted = dialog.exec() == QDialog.Accepted
        if detected.state() != QProcess.NotRunning:
            detected.kill()
            detected.waitForFinished(1000)
        if not accepted:
            return
        save_queue_hosts([
            hosts.item(index).text()
            for index in range(hosts.count())
            if hosts.item(index).checkState() == Qt.Checked
        ])
        self.show_transient_status(f"{len(queue_hosts())} devices configured")

    def sync_settings(self) -> None:
        repo = self.selected_repo()
        hosts = queue_hosts()
        if not repo or self.sync_process.state() != QProcess.NotRunning:
            return
        if not hosts:
            QMessageBox.information(
                self, "Sync settings",
                "Add the devices that have the queue installed in Devices… first.",
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Sync settings")
        device = QComboBox()
        device.addItems(hosts)
        direction = QComboBox()
        direction.addItem("Send this device's settings to it", "send")
        direction.addItem("Replace this device's settings with its settings", "receive")
        every_repo = QCheckBox("All repositories in the list, not just this one")
        note = QLabel(
            "Syncs the repository settings, such as delegation, approval actions, "
            "exclusions, and sounds. The queue state stays with the monitor."
        )
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Sync")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form = QFormLayout()
        form.addRow("Device", device)
        form.addRow("Direction", direction)
        layout = QVBoxLayout(dialog)
        layout.addLayout(form)
        layout.addWidget(every_repo)
        layout.addWidget(note)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        repos = [repo]
        if every_repo.isChecked():
            repos = [
                self.repo_combo.itemText(index) for index in range(self.repo_combo.count())
            ] or [repo]
        self.sync_pending = repos
        self.sync_host = device.currentText()
        self.sync_direction = direction.currentData()
        self.sync_results = []
        self.sync_failed = False
        self.sync_button.setEnabled(False)
        self.start_next_sync()

    def start_next_sync(self) -> None:
        if not self.sync_pending:
            self.sync_button.setEnabled(True)
            summary = "\n".join(self.sync_results) or "Nothing was synced."
            if self.sync_failed:
                QMessageBox.warning(self, "Sync settings", summary)
            else:
                lines = summary.splitlines()
                self.show_transient_status(
                    lines[0] if len(lines) == 1 else f"Synced settings for {len(lines)} repositories",
                    6000,
                )
            repo = self.selected_repo()
            if repo and self.sync_direction == "receive":
                # Reload the controls from the received files.
                self.repo_changed(repo)
            return
        repo = self.sync_pending.pop(0)
        self.statusBar().showMessage(f"Syncing settings for {repo} with {self.sync_host}…")
        self.sync_process.setProgram(SCRIPT)
        self.sync_process.setArguments(
            ["--repo", repo, "--sync-settings", self.sync_host, self.sync_direction]
        )
        self.sync_process.start()

    def sync_step_finished(self, exit_code: int) -> None:
        stdout, stderr = command_output(self.sync_process)
        if exit_code != 0:
            self.sync_failed = True
        message = (stderr if exit_code != 0 else stdout) or stdout or stderr
        if message:
            self.sync_results.append(message.splitlines()[-1])
        self.start_next_sync()
