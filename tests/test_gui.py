import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess, Qt
from PySide6.QtWidgets import QApplication

import importlib.util


GUI_PATH = Path(__file__).resolve().parents[1] / "coderabbit-review-queue-gui.py"
SPEC = importlib.util.spec_from_file_location("queue_gui", GUI_PATH)
assert SPEC and SPEC.loader
queue_gui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue_gui)


class ReviewRequestRefreshTests(unittest.TestCase):
    def test_zombie_monitor_is_not_considered_running(self):
        self.assertFalse(queue_gui.proc_stat_is_running("1040817 (bash) Z 1 2 3"))
        self.assertTrue(queue_gui.proc_stat_is_running("1040817 (bash) S 1 2 3"))

    def test_tray_click_hides_visible_window(self):
        window = Mock()
        window.window_is_shown.return_value = True

        queue_gui.QueueWindow.toggle_from_tray(window)

        window.hide.assert_called_once_with()
        window.show_from_tray.assert_not_called()

    def test_tray_click_restores_minimized_window(self):
        window = Mock()
        window.window_is_shown.return_value = False

        queue_gui.QueueWindow.toggle_from_tray(window)

        window.show_from_tray.assert_called_once_with()
        window.hide.assert_not_called()

    def test_tray_menu_action_matches_window_visibility(self):
        window = Mock()
        window.isVisible.return_value = True
        window.isMinimized.return_value = False
        window.any_monitor_running.return_value = False

        queue_gui.QueueWindow.update_tray_window_action(window)

        window.window_action.setText.assert_called_once_with("Hide CodeRabbit queue")
        window.stop_all_monitors_action.setVisible.assert_called_once_with(False)

    def test_tray_stop_all_action_is_visible_with_running_monitor(self):
        window = Mock()
        window.isVisible.return_value = False
        window.isMinimized.return_value = False
        window.any_monitor_running.return_value = True

        queue_gui.QueueWindow.update_tray_window_action(window)

        window.stop_all_monitors_action.setVisible.assert_called_once_with(True)

    def test_recent_complete_status_cache_skips_startup_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "status.txt"
            cache.write_text("Repository: example/repo\nQueued PRs:\n  #42 cached\n")
            window = Mock()
            window.status_cache_file.return_value = cache

            fresh = queue_gui.QueueWindow.load_cached_status(
                window, "example/repo"
            )

            self.assertTrue(fresh)
            window.populate_queue.assert_called_once_with(cache.read_text())
            window.populate_tasks.assert_called_once_with(cache.read_text())

    def test_old_complete_status_cache_displays_before_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "status.txt"
            cache.write_text("Repository: example/repo\n")
            old = time.time() - 120
            os.utime(cache, (old, old))
            window = Mock()
            window.status_cache_file.return_value = cache

            fresh = queue_gui.QueueWindow.load_cached_status(
                window, "example/repo"
            )

            self.assertFalse(fresh)
            window.populate_queue.assert_called_once_with(cache.read_text())
            window.populate_tasks.assert_called_once_with(cache.read_text())

    def test_review_request_change_invalidates_complete_status_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "status.txt"
            request = Path(directory) / "requests.tsv"
            cache.write_text("Repository: example/repo\n")
            request.write_text("42\thead\t123\n")
            newer = time.time() + 1
            os.utime(request, (newer, newer))
            window = Mock()
            window.status_cache_file.return_value = cache
            window.review_requests_file.return_value = request
            window.monitor_state_file.return_value = Path(directory) / "missing"

            fresh = queue_gui.QueueWindow.load_cached_status(
                window, "example/repo"
            )

            self.assertFalse(fresh)
            window.populate_queue.assert_called_once_with(cache.read_text())

    def test_automatic_refresh_uses_shared_snapshot_cache(self):
        window = Mock()
        window.status_process.state.return_value = QProcess.NotRunning
        window.status_retry_timer.isActive.return_value = False
        window.selected_repo.return_value = "example/repo"

        queue_gui.QueueWindow.refresh(window)

        window.status_process.setArguments.assert_called_once_with(
            ["--repo", "example/repo", "--cached-status"]
        )

    def test_manual_refresh_requests_recent_status(self):
        window = Mock()
        window.status_process.state.return_value = QProcess.NotRunning
        window.status_retry_timer.isActive.return_value = False
        window.selected_repo.return_value = "example/repo"

        queue_gui.QueueWindow.refresh(window, manual=True)

        window.status_process.setArguments.assert_called_once_with(
            ["--repo", "example/repo", "--status"]
        )

    def test_ssh_hosts_exclude_patterns_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            config.write_text(
                "Host desktop *.example !blocked\n"
                "Host laptop desktop\n"
            )
            self.assertEqual(
                queue_gui.ssh_config_hosts(config),
                ["desktop", "laptop"],
            )

    def test_unlisted_repository_is_validated_before_activation(self):
        window = Mock()
        window.repo_combo.count.return_value = 1
        window.repo_combo.itemText.return_value = "example/repo"
        window.repo_validation_process.state.return_value = QProcess.NotRunning

        queue_gui.QueueWindow.repo_changed(window, "example/missing")

        window.activate_repo.assert_not_called()
        window.repo_validation_process.start.assert_called_once_with()

    def test_monitor_state_overrides_expired_countdown(self):
        window = Mock()
        window.monitor_activity = ("checking", "42", "fix the queue", 0)
        window.active_reviews = []
        window.has_queued_reviews = True
        window.selected_repo.return_value = "example/repo"

        queue_gui.QueueWindow.update_countdown_display(window)

        window.timer_label.setText.assert_called_with(
            "Next review: Checking availability for #42"
        )

    def test_expired_monitor_wait_does_not_claim_availability(self):
        window = Mock()
        window.monitor_activity = ("waiting", "", "", 0)
        window.active_reviews = []
        window.has_queued_reviews = True
        window.selected_repo.return_value = "example/repo"

        queue_gui.QueueWindow.update_countdown_display(window)

        window.timer_label.setText.assert_called_once_with(
            "Next review: Preparing availability check"
        )

    def test_stopped_monitor_does_not_claim_review_is_available(self):
        window = Mock()
        window.monitor_activity = None
        window.active_reviews = []
        window.has_queued_reviews = True
        window.next_review_at = None
        window.selected_repo.return_value = "example/repo"
        window.monitor_pid.return_value = None

        queue_gui.QueueWindow.update_countdown_display(window)

        window.timer_label.setText.assert_called_once_with(
            "Next review: Availability unknown"
        )

    def test_request_file_change_triggers_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            request_file = Path(directory) / "requests.tsv"
            request_file.write_text("1\thead\t100\n")
            window = Mock()
            window.selected_repo.return_value = "example/repo"
            window.review_requests_file.return_value = request_file
            window.monitor_state_file.return_value = Path(directory) / "monitor.tsv"
            window.monitor_pid.return_value = None
            window.review_requests_signature = (
                "example/repo",
                queue_gui.file_signature(request_file),
                None,
            )
            window.status_process.state.return_value = QProcess.NotRunning

            request_file.write_text("1\thead\t100\n2\thead\t200\n")
            queue_gui.QueueWindow.refresh_if_review_requests_changed(window)

            window.refresh.assert_called_once_with()
            self.assertEqual(
                window.review_requests_signature,
                ("example/repo", queue_gui.file_signature(request_file), None),
            )

    def test_status_refresh_records_monitor_state_version(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = Path(directory) / "monitor.tsv"
            monitor.write_text("reviewing\t42\ttitle\t0\n")
            window = Mock()
            window.status_process.state.return_value = QProcess.NotRunning
            window.status_retry_timer.isActive.return_value = False
            window.selected_repo.return_value = "example/repo"
            window.monitor_state_file.return_value = monitor

            queue_gui.QueueWindow.refresh(window)

            self.assertEqual(
                window.status_monitor_signature,
                queue_gui.file_signature(monitor),
            )

    def test_tables_are_separated_by_vertical_splitter(self):
        app = QApplication.instance() or QApplication([])
        with unittest.mock.patch.object(
            queue_gui.QueueWindow, "load_cached_repos", return_value=True
        ):
            window = queue_gui.QueueWindow()
        self.assertEqual(window.table_splitter.orientation(), Qt.Vertical)
        self.assertEqual(window.table_splitter.count(), 2)
        window.close()
        app.processEvents()

    def test_move_buttons_start_disabled_without_selection(self):
        app = QApplication.instance() or QApplication([])
        with unittest.mock.patch.object(
            queue_gui.QueueWindow, "load_cached_repos", return_value=True
        ):
            window = queue_gui.QueueWindow()
        self.assertFalse(window.up_button.isEnabled())
        self.assertFalse(window.down_button.isEnabled())
        window.close()
        app.processEvents()

    def test_splitter_sizes_are_saved_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "splitter-sizes"
            window = Mock()
            window.splitter_sizes_file.return_value = state_file
            window.table_splitter.sizes.return_value = [420, 180]

            queue_gui.QueueWindow.save_splitter_sizes(window)
            self.assertEqual(state_file.read_text(), "420 180\n")

            restored = Mock()
            restored.splitter_sizes_file.return_value = state_file
            queue_gui.QueueWindow.restore_splitter_sizes(restored)
            restored.table_splitter.setSizes.assert_called_once_with([420, 180])

    def test_finished_reviews_include_approved_and_feedback_rows(self):
        app = QApplication.instance() or QApplication([])
        window = Mock()
        window.tasks = queue_gui.QTreeWidget()
        window.update_delegate_button = Mock()
        queue_gui.QueueWindow.populate_tasks(
            window,
            """Finished CodeRabbit reviews:
  #10 approved change
    Result: Approved
    Agent task: —
  #11 needs fixes
    Result: 2 unresolved
    Agent task: Codex Idle\tResolve review feedback\tLatest task update
""",
        )
        self.assertEqual(window.tasks.topLevelItemCount(), 2)
        approved = window.tasks.topLevelItem(0)
        feedback = window.tasks.topLevelItem(1)
        self.assertEqual(approved.text(2), "Approved")
        self.assertFalse(approved.data(0, Qt.UserRole + 2))
        self.assertEqual(feedback.text(2), "2 unresolved")
        self.assertEqual(feedback.text(3), "Codex Idle")
        self.assertEqual(feedback.text(4), "Resolve review feedback")
        self.assertEqual(feedback.text(5), "Latest task update")
        self.assertTrue(feedback.data(0, Qt.UserRole + 2))
        app.processEvents()

    def test_successful_delegation_immediately_marks_task_running(self):
        app = QApplication.instance() or QApplication([])
        window = Mock()
        window.tasks = queue_gui.QTreeWidget()
        item = queue_gui.QTreeWidgetItem(
            ["#1292", "fix menus", "1 unresolved", "Codex Idle", "task", ""]
        )
        item.setData(0, Qt.UserRole, "1292")
        item.setData(0, Qt.UserRole + 1, False)
        item.setData(0, Qt.UserRole + 2, True)
        window.tasks.addTopLevelItem(item)
        window.update_delegate_button = Mock()

        queue_gui.QueueWindow.mark_delegation_running(window, "1292")

        self.assertEqual(item.text(3), "Codex Running")
        self.assertTrue(item.data(0, Qt.UserRole + 1))
        window.update_delegate_button.assert_called_once_with()
        app.processEvents()

    def test_live_review_state_keeps_pr_at_top_of_queue(self):
        app = QApplication.instance() or QApplication([])
        window = Mock()
        window.queue = queue_gui.QTreeWidget()
        window.monitor_activity = ("reviewing", "42", "live review", 0)
        window.selected_repo.return_value = ""
        window.update_countdown_display = Mock()
        window.update_queue_buttons = Mock()
        window.apply_monitor_activity_to_queue = lambda: (
            queue_gui.QueueWindow.apply_monitor_activity_to_queue(window)
        )

        queue_gui.QueueWindow.populate_queue(window, "Repository: example/repo\n")

        self.assertEqual(window.queue.topLevelItemCount(), 1)
        item = window.queue.topLevelItem(0)
        self.assertEqual(item.text(0), "#42")
        self.assertEqual(item.text(2), "In progress")

    def test_checking_state_replaces_queued_row_without_duplicate(self):
        app = QApplication.instance() or QApplication([])
        window = Mock()
        window.queue = queue_gui.QTreeWidget()
        window.monitor_activity = ("checking", "42", "live review", 0)
        window.selected_repo.return_value = ""
        window.update_countdown_display = Mock()
        window.update_queue_buttons = Mock()
        window.apply_monitor_activity_to_queue = lambda: (
            queue_gui.QueueWindow.apply_monitor_activity_to_queue(window)
        )

        queue_gui.QueueWindow.populate_queue(
            window,
            "Repository: example/repo\nQueued PRs:\n  #7 other\n  #42 live review\n",
        )

        self.assertEqual(window.queue.topLevelItemCount(), 2)
        self.assertEqual(window.queue.topLevelItem(0).text(0), "#42")
        self.assertEqual(window.queue.topLevelItem(0).text(2), "Checking availability")

    def test_waiting_state_clears_previous_checking_status(self):
        app = QApplication.instance() or QApplication([])
        window = Mock()
        window.queue = queue_gui.QTreeWidget()
        window.monitor_activity = ("waiting", "", "", 123)
        window.update_queue_buttons = Mock()
        item = queue_gui.QTreeWidgetItem(
            ["#42", "live review", "Checking availability"]
        )
        item.setData(0, Qt.UserRole, "42")
        item.setData(0, Qt.UserRole + 1, "Checking availability")
        item.setData(0, Qt.UserRole + 2, "Queued")
        window.queue.addTopLevelItem(item)

        queue_gui.QueueWindow.apply_monitor_activity_to_queue(window)

        self.assertEqual(item.text(2), "Queued")
        self.assertEqual(item.data(0, Qt.UserRole + 1), "Queued")
        window.update_queue_buttons.assert_called_once_with()
        app.processEvents()

    def test_refresh_preserves_scroll_position_for_offscreen_selection(self):
        app = QApplication.instance() or QApplication([])
        window = Mock()
        window.queue = queue_gui.QTreeWidget()
        window.queue.setFixedHeight(100)
        window.queue.show()
        window.monitor_activity = None
        window.selected_repo.return_value = ""
        window.update_countdown_display = Mock()
        window.update_queue_buttons = Mock()
        window.apply_monitor_activity_to_queue = lambda: None
        status = "Repository: example/repo\nQueued PRs:\n" + "".join(
            f"  #{number} PR {number}\n" for number in range(30)
        )
        queue_gui.QueueWindow.populate_queue(window, status)
        window.queue.setCurrentItem(window.queue.topLevelItem(25))
        window.queue.verticalScrollBar().setValue(0)
        app.processEvents()

        queue_gui.QueueWindow.populate_queue(window, status)
        app.processEvents()

        self.assertEqual(window.queue.currentItem().text(0), "#25")
        self.assertEqual(window.queue.verticalScrollBar().value(), 0)
        window.queue.close()

    def test_finished_review_suppresses_stale_live_overlay(self):
        app = QApplication.instance() or QApplication([])
        window = Mock()
        window.queue = queue_gui.QTreeWidget()
        window.monitor_activity = ("reviewing", "42", "live review", 0)
        window.selected_repo.return_value = ""
        window.update_countdown_display = Mock()
        window.update_queue_buttons = Mock()
        window.apply_monitor_activity_to_queue = lambda: (
            queue_gui.QueueWindow.apply_monitor_activity_to_queue(window)
        )

        queue_gui.QueueWindow.populate_queue(
            window,
            """Repository: example/repo
Finished CodeRabbit reviews:
  #42 live review
    Result: Approved
    Agent task: —
""",
        )

        self.assertEqual(window.queue.topLevelItemCount(), 0)


if __name__ == "__main__":
    unittest.main()
