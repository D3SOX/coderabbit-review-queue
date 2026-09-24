# SPDX-License-Identifier: GPL-3.0-only

import fcntl
import os
import re
from pathlib import Path

from PySide6.QtCore import QDate, QDateTime, QLocale, QProcess, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMessageBox, QTreeWidget, QTreeWidgetItem

from queue_gui_common import (
    SCRIPT, STATE_ROOT, file_signature, live_review_number,
    process_is_running, status_belongs_to_repo,
)


class StatusMixin:
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

    def review_completion_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-review-completion.tsv"

    def apply_recent_completion(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        marker = self.review_completion_file(repo)
        try:
            marker_time = marker.stat().st_mtime_ns
            cache_time = self.status_cache_file(repo).stat().st_mtime_ns
            number, _head, title = marker.read_text().strip().split("\t", 2)
        except (OSError, ValueError):
            return
        if marker_time <= cache_time or not number.isdecimal():
            return
        if live_review_number(self.monitor_activity) == number:
            return
        self.active_reviews = [
            row for row in self.active_reviews if row[0] != number
        ]
        for index in range(self.queue.topLevelItemCount() - 1, -1, -1):
            item = self.queue.topLevelItem(index)
            if item.data(0, Qt.UserRole) == number:
                self.queue.takeTopLevelItem(index)
        if not any(
            self.tasks.topLevelItem(index).data(0, Qt.UserRole) == number
            for index in range(self.tasks.topLevelItemCount())
        ):
            item = QTreeWidgetItem(
                [f"#{number}", title, "Review completed; syncing details", "—", "—", "—"]
            )
            item.setData(0, Qt.UserRole, number)
            item.setData(0, Qt.UserRole + 1, False)
            item.setData(0, Qt.UserRole + 2, False)
            self.tasks.addTopLevelItem(item)
        self.update_queue_buttons()
        self.update_delegate_button()

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
        self.apply_recent_completion()
        self.update_countdown_display()
        signature = (
            repo,
            file_signature(self.review_requests_file(repo)),
            file_signature(self.monitor_state_file(repo)),
            file_signature(self.review_completion_file(repo)),
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
        if not repo or repo != self.displayed_repo:
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
        self.status_monitor_signature = file_signature(self.monitor_state_file(repo))
        self.status_completion_signature = file_signature(self.review_completion_file(repo))
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
        if self.status_repo != self.selected_repo():
            self.status_failures = 0
            self.refresh()
            return
        if self.status_repo != self.displayed_repo:
            return
        if self.status_monitor_signature != file_signature(
            self.monitor_state_file(self.status_repo)
        ) or self.status_completion_signature != file_signature(
            self.review_completion_file(self.status_repo)
        ):
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
        if not status_belongs_to_repo(stdout, self.status_repo):
            self.statusBar().showMessage(
                "Status response belongs to another repository; retrying…"
            )
            self.status_retry_timer.start(5000)
            return
        self.save_status_cache(self.status_repo, stdout)
        self.status_loading_repo = ""
        self.populate_queue(stdout)
        self.populate_tasks(stdout)
        self.show_transient_status("Status refreshed", 3000)
        self.update_monitor_state()

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
            if phase == "dispatch_wait":
                summary = "Waiting for this repository's review slot"
                self.timer_label.setText(f"Next review: {summary}")
                self.set_tray_countdown("…", f"{repo}\n{summary}")
                self.waiting_for_review_window = False
                return
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
        if self.status_loading_repo and self.status_loading_repo == self.selected_repo():
            self.timer_label.setText("Next review: Loading status…")
            self.set_tray_countdown("…", f"{repo}\nLoading status…")
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
        scroll_position = self.queue.verticalScrollBar().value()
        current_item = self.queue.currentItem()
        selected_number = (
            current_item.data(0, Qt.UserRole) if current_item is not None else None
        )
        queued: list[tuple[str, str]] = []
        active: list[tuple[str, str]] = []
        finished_numbers: set[str] = set()
        approved_numbers: set[str] = set()
        current_finished_number = ""
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
            elif section == "finished" and line.startswith("  #"):
                number, _, _title = line.strip().partition(" ")
                current_finished_number = number.removeprefix("#")
                finished_numbers.add(current_finished_number)
            elif section == "finished" and line == "    Result: Approved":
                approved_numbers.add(current_finished_number)
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
        self.approved_review_numbers = approved_numbers
        live_number = live_review_number(self.monitor_activity)
        if live_number and live_number not in approved_numbers:
            finished_numbers.discard(live_number)
        self.finished_review_numbers = finished_numbers
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
        self.queue.verticalScrollBar().setValue(scroll_position)
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
        transient_statuses = set(labels.values())
        if status is None:
            for index in range(self.queue.topLevelItemCount()):
                item = self.queue.topLevelItem(index)
                if item.text(2) not in transient_statuses:
                    continue
                base_status = item.data(0, Qt.UserRole + 2) or "Queued"
                item.setText(2, base_status)
                item.setData(0, Qt.UserRole + 1, base_status)
            self.update_queue_buttons()
            return
        if not number:
            return
        approved_numbers = getattr(self, "approved_review_numbers", None)
        if isinstance(approved_numbers, set) and number in approved_numbers:
            return
        self.finished_review_numbers.discard(number)
        if isinstance(self.tasks, QTreeWidget):
            for index in range(self.tasks.topLevelItemCount() - 1, -1, -1):
                if self.tasks.topLevelItem(index).data(0, Qt.UserRole) == number:
                    self.tasks.takeTopLevelItem(index)
                    self.update_delegate_button()
        selected = self.queue.currentItem()
        selected_number = selected.data(0, Qt.UserRole) if selected else None
        base_status = "Queued"
        for index in range(self.queue.topLevelItemCount()):
            item = self.queue.topLevelItem(index)
            if item.data(0, Qt.UserRole) == number:
                self.queue.takeTopLevelItem(index)
                if not title:
                    title = item.text(1)
                base_status = item.data(0, Qt.UserRole + 2) or item.data(
                    0, Qt.UserRole + 1
                )
                break
        live_item = QTreeWidgetItem([f"#{number}", title, status])
        live_item.setData(0, Qt.UserRole, number)
        live_item.setData(0, Qt.UserRole + 1, status)
        live_item.setData(0, Qt.UserRole + 2, base_status)
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
            if number == live_review_number(self.monitor_activity) and result != "Approved":
                continue
            fields = progress.split("\t", 2)
            if len(fields) == 3:
                state, task_name, detail = fields
            else:
                state, separator, detail = progress.partition(" — ")
                state = re.sub(r" \([0-9a-f]{8}\)$", "", state)
                task_name = "—"
            actionable = result.endswith(" unresolved")
            item = QTreeWidgetItem(
                [
                    f"#{number}",
                    title,
                    result,
                    state,
                    task_name,
                    detail,
                ]
            )
            item.setData(0, Qt.UserRole, number)
            item.setData(0, Qt.UserRole + 1, "Running" in state.split())
            item.setData(0, Qt.UserRole + 2, actionable)
            item.setToolTip(5, detail)
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
        lock_file = order_file.with_suffix(order_file.suffix + ".lock")
        with lock_file.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            temporary = order_file.with_suffix(".tmp")
            temporary.write_text("\n".join(numbers) + "\n")
            os.replace(temporary, order_file)
        self.show_transient_status("Queue order saved")

    def queue_order_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-order.txt"
