import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess, Qt
from PySide6.QtWidgets import QApplication

import importlib.util


GUI_PATH = Path(__file__).resolve().parents[1] / "src/coderabbit-review-queue-gui.py"
SPEC = importlib.util.spec_from_file_location("queue_gui", GUI_PATH)
assert SPEC and SPEC.loader
queue_gui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue_gui)


class ReviewRequestRefreshTests(unittest.TestCase):
    def test_requeue_setting_inherits_old_choice_once_then_stays_independent(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            setting = Path(directory) / "requeued-items-at-top"
            window = Mock()
            window.new_items_at_top = queue_gui.QCheckBox()
            window.new_items_at_top.setChecked(False)
            window.requeued_items_at_top = queue_gui.QCheckBox()
            window.requeued_items_at_top_file.return_value = setting

            queue_gui.QueueWindow.load_requeued_items_at_top(window, "example/repo")
            self.assertFalse(window.requeued_items_at_top.isChecked())
            self.assertEqual(setting.read_text(), "0\n")

            window.new_items_at_top.setChecked(True)
            queue_gui.QueueWindow.load_requeued_items_at_top(window, "example/repo")
            self.assertFalse(window.requeued_items_at_top.isChecked())
            app.processEvents()

    def test_source_checkout_finds_moved_app_icon(self):
        self.assertTrue(queue_gui.APP_ICON_PATH.is_file())
        self.assertFalse(queue_gui.app_icon().isNull())

    def test_delegated_review_mode_preserves_saved_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            setting = Path(directory) / "merge-after-delegation"
            window = Mock()
            window.merge_after_delegation_file.return_value = setting
            self.assertEqual(
                queue_gui.QueueWindow.post_delegation_review_mode(window, "example/repo"),
                "1",
            )
            setting.write_text("0\n")
            self.assertEqual(
                queue_gui.QueueWindow.post_delegation_review_mode(window, "example/repo"),
                "0",
            )
            setting.write_text("agent\n")
            self.assertEqual(
                queue_gui.QueueWindow.post_delegation_review_mode(window, "example/repo"),
                "agent",
            )

    def test_approval_dialog_saves_agent_review_choice(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            window = queue_gui.QMainWindow()
            window.selected_repo = lambda: "example/repo"
            window.show_transient_status = Mock()
            for name in (
                "auto_merge_file", "merge_method_file", "delete_branch_file",
                "merge_after_delegation_file", "merge_after_approval_file",
                "merge_admin_file", "archive_after_merge_file",
            ):
                setattr(window, name, lambda repo, name=name: Path(directory) / name)
            window.post_delegation_review_mode = lambda repo: (
                queue_gui.QueueWindow.post_delegation_review_mode(window, repo)
            )

            def choose_agent(dialog):
                review_select = next(
                    combo for combo in dialog.findChildren(queue_gui.QComboBox)
                    if combo.findData("agent") >= 0
                )
                self.assertEqual(review_select.count(), 3)
                review_select.setCurrentIndex(review_select.findData("agent"))
                return queue_gui.QDialog.Accepted

            with patch.object(queue_gui.QDialog, "exec", choose_agent):
                queue_gui.QueueWindow.configure_auto_merge(window)

            self.assertEqual(
                (Path(directory) / "merge_after_delegation_file").read_text(),
                "agent\n",
            )
            app.processEvents()

    def test_switch_to_uncached_repository_clears_both_tables_before_refresh(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            window = Mock()
            window.queue = queue_gui.QTreeWidget()
            window.tasks = queue_gui.QTreeWidget()
            window.queue.addTopLevelItem(queue_gui.QTreeWidgetItem(["#1", "old queue"]))
            window.tasks.addTopLevelItem(queue_gui.QTreeWidgetItem(["#2", "old review"]))
            window.status_cache_file.return_value = Path(directory) / "uncached-status.txt"
            window.load_cached_status = lambda repo: queue_gui.QueueWindow.load_cached_status(window, repo)
            window.active_reviews = [("1", "old queue")]
            window.finished_review_numbers = {"2"}
            window.has_queued_reviews = True

            with patch.object(queue_gui, "STATE_ROOT", Path(directory)), patch.object(
                queue_gui, "SELECTED_REPO_FILE", Path(directory) / "selected-repo"
            ):
                queue_gui.QueueWindow.activate_repo(window, "new/repo")

            self.assertEqual(window.queue.topLevelItemCount(), 0)
            self.assertEqual(window.tasks.topLevelItemCount(), 0)
            window.refresh.assert_called_once_with()
            app.processEvents()

    def test_old_repository_refresh_cannot_fill_new_repository_tables(self):
        window = Mock()
        window.status_repo = "old/repo"
        window.selected_repo.return_value = "new/repo"
        window.status_process.readAllStandardOutput.return_value = (
            b"Repository: old/repo\nQueued PRs:\n  #1 old PR\n"
        )
        window.status_process.readAllStandardError.return_value = b""

        queue_gui.QueueWindow.status_finished(window, 0)

        window.populate_queue.assert_not_called()
        window.populate_tasks.assert_not_called()
        window.refresh.assert_called_once_with()

    def test_refresh_during_repository_switch_cannot_fill_previous_tables(self):
        window = Mock()
        window.status_repo = "new/repo"
        window.displayed_repo = "old/repo"
        window.status_monitor_signature = None
        window.status_completion_signature = None
        window.monitor_state_file.return_value = Path("/nonexistent-monitor-state")
        window.review_completion_file.return_value = Path("/nonexistent-completion")
        window.selected_repo.return_value = "new/repo"
        window.status_process.readAllStandardOutput.return_value = (
            b"Repository: new/repo\nQueued PRs:\n  #2 new PR\n"
        )
        window.status_process.readAllStandardError.return_value = b""

        queue_gui.QueueWindow.status_finished(window, 0)

        window.populate_queue.assert_not_called()
        window.populate_tasks.assert_not_called()

    def test_status_result_for_other_repository_is_never_cached_or_displayed(self):
        window = Mock()
        window.status_repo = "new/repo"
        window.displayed_repo = "new/repo"
        window.selected_repo.return_value = "new/repo"
        window.status_monitor_signature = None
        window.monitor_state_file.return_value = Path("/nonexistent-monitor-state")
        window.status_process.readAllStandardOutput.return_value = (
            b"Repository: old/repo\nQueued PRs:\n  #42 old PR\n"
        )
        window.status_process.readAllStandardError.return_value = b""

        queue_gui.QueueWindow.status_finished(window, 0)

        window.save_status_cache.assert_not_called()
        window.populate_queue.assert_not_called()

    def test_finished_status_applies_before_monitor_update_can_start_new_refresh(self):
        window = Mock()
        window.status_repo = "old/repo"
        window.displayed_repo = "old/repo"
        window.selected_repo.return_value = "old/repo"
        window.status_monitor_signature = None
        window.status_completion_signature = None
        window.monitor_state_file.return_value = Path("/nonexistent-monitor-state")
        window.review_completion_file.return_value = Path("/nonexistent-completion")
        window.status_process.readAllStandardOutput.return_value = (
            b"Repository: old/repo\nQueued PRs:\n  #42 old PR\n"
        )
        window.status_process.readAllStandardError.return_value = b""
        window.update_monitor_state.side_effect = lambda: setattr(
            window, "status_repo", "new/repo"
        )

        queue_gui.QueueWindow.status_finished(window, 0)

        window.save_status_cache.assert_called_once_with(
            "old/repo", "Repository: old/repo\nQueued PRs:\n  #42 old PR"
        )

    def test_changing_repository_text_hides_previous_tables_immediately(self):
        app = QApplication.instance() or QApplication([])
        with patch.object(queue_gui.QueueWindow, "load_cached_repos", return_value=True):
            window = queue_gui.QueueWindow()
        window.repo_combo.setEditText("old/repo")
        window.displayed_repo = "old/repo"
        window.queue.addTopLevelItem(queue_gui.QTreeWidgetItem(["#42", "old PR"]))
        window.tasks.addTopLevelItem(queue_gui.QTreeWidgetItem(["#43", "old review"]))

        window.repo_combo.setEditText("new/repo")

        self.assertEqual(window.queue.topLevelItemCount(), 0)
        self.assertEqual(window.tasks.topLevelItemCount(), 0)
        window.close()
        app.processEvents()

    def test_uncached_repository_shows_loading_instead_of_old_availability(self):
        window = Mock()
        window.base_window_title = "CodeRabbit Review Queue"
        window.status_loading_repo = "new/repo"
        window.monitor_activity = None
        window.selected_repo.return_value = "new/repo"

        queue_gui.QueueWindow.update_countdown_display(window)

        window.timer_label.setText.assert_called_once_with(
            "Next review: Loading status…"
        )
        window.set_tray_countdown.assert_called_once_with(
            "…", "new/repo\nLoading status…"
        )

    def test_notification_sound_settings_default_to_30_percent(self):
        with tempfile.TemporaryDirectory() as directory:
            window = Mock()
            window.notify_sound_path_file.return_value = Path(directory) / "missing-path"
            window.notify_sound_volume_file.return_value = Path(directory) / "missing-volume"

            self.assertEqual(
                queue_gui.QueueWindow.notify_sound_settings(window, "example/repo"),
                ("", 30),
            )

    def test_notification_sound_settings_clamp_saved_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "sound-path"
            volume_file = Path(directory) / "sound-volume"
            path_file.write_text("/tmp/complete.oga\n")
            volume_file.write_text("140\n")
            window = Mock()
            window.notify_sound_path_file.return_value = path_file
            window.notify_sound_volume_file.return_value = volume_file

            settings = queue_gui.QueueWindow.notify_sound_settings(
                window, "example/repo"
            )

            self.assertEqual(settings, ("/tmp/complete.oga", 100))

    def test_babysit_delegation_prompt_mode_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            mode_file = Path(directory) / "prompt-mode"
            custom_file = Path(directory) / "prompt-custom"
            mode_file.write_text("babysit\n")
            window = Mock()
            window.delegation_prompt_mode_file.return_value = mode_file
            window.delegation_prompt_template_file.return_value = custom_file

            mode, custom = queue_gui.QueueWindow.delegation_prompt_settings(
                window, "example/repo"
            )

            self.assertEqual(mode, "babysit")
            self.assertEqual(custom, "")

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

    def test_cache_with_other_repository_header_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "status.txt"
            cache.write_text("Repository: old/repo\nQueued PRs:\n  #42 old PR\n")
            window = Mock()
            window.status_cache_file.return_value = cache

            fresh = queue_gui.QueueWindow.load_cached_status(window, "new/repo")

            self.assertFalse(fresh)
            window.populate_queue.assert_not_called()
            window.populate_tasks.assert_not_called()

    def test_status_cache_write_rejects_other_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "status.txt"
            window = Mock()
            window.status_cache_file.return_value = cache

            queue_gui.QueueWindow.save_status_cache(
                window, "new/repo", "Repository: old/repo\nQueued PRs:\n  #42 old PR"
            )

            self.assertFalse(cache.exists())

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
        window.displayed_repo = "example/repo"

        queue_gui.QueueWindow.refresh(window)

        window.status_process.setArguments.assert_called_once_with(
            ["--repo", "example/repo", "--cached-status"]
        )

    def test_manual_refresh_requests_recent_status(self):
        window = Mock()
        window.status_process.state.return_value = QProcess.NotRunning
        window.status_retry_timer.isActive.return_value = False
        window.selected_repo.return_value = "example/repo"
        window.displayed_repo = "example/repo"

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

    def test_dispatch_wait_is_scoped_to_current_repository(self):
        window = Mock()
        window.monitor_activity = ("dispatch_wait", "", "", 0)
        window.active_reviews = []
        window.has_queued_reviews = True
        window.next_review_at = None
        window.status_loading_repo = ""
        window.selected_repo.return_value = "example/repo"

        queue_gui.QueueWindow.update_countdown_display(window)

        window.timer_label.setText.assert_called_once_with(
            "Next review: Waiting for this repository's review slot"
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
            window.review_completion_file.return_value = Path(directory) / "completion.tsv"
            window.monitor_pid.return_value = None
            window.review_requests_signature = (
                "example/repo",
                queue_gui.file_signature(request_file),
                None,
                None,
            )
            window.status_process.state.return_value = QProcess.NotRunning

            request_file.write_text("1\thead\t100\n2\thead\t200\n")
            queue_gui.QueueWindow.refresh_if_review_requests_changed(window)

            window.refresh.assert_called_once_with()
            self.assertEqual(
                window.review_requests_signature,
                ("example/repo", queue_gui.file_signature(request_file), None, None),
            )

    def test_completion_marker_moves_stale_active_row_immediately(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "status.txt"
            cache.write_text("stale status")
            marker = root / "completion.tsv"
            marker.write_text("1535\thead\tFinished PR\n")
            window = Mock()
            window.queue = queue_gui.QTreeWidget()
            window.tasks = queue_gui.QTreeWidget()
            window.queue.addTopLevelItem(
                queue_gui.QTreeWidgetItem(["#1535", "Finished PR", "In progress"])
            )
            window.queue.topLevelItem(0).setData(0, Qt.UserRole, "1535")
            window.active_reviews = [("1535", "Finished PR")]
            window.selected_repo.return_value = "example/repo"
            window.review_completion_file.return_value = marker
            window.status_cache_file.return_value = cache

            queue_gui.QueueWindow.apply_recent_completion(window)

            self.assertEqual(window.queue.topLevelItemCount(), 0)
            self.assertEqual(window.tasks.topLevelItemCount(), 1)
            self.assertEqual(window.tasks.topLevelItem(0).text(2), "Review completed; syncing details")
            self.assertEqual(window.active_reviews, [])

    def test_status_refresh_records_monitor_state_version(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = Path(directory) / "monitor.tsv"
            monitor.write_text("reviewing\t42\ttitle\t0\n")
            window = Mock()
            window.status_process.state.return_value = QProcess.NotRunning
            window.status_retry_timer.isActive.return_value = False
            window.selected_repo.return_value = "example/repo"
            window.displayed_repo = "example/repo"
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

    def test_queue_placement_settings_are_side_by_side(self):
        app = QApplication.instance() or QApplication([])
        with patch.object(queue_gui.QueueWindow, "load_cached_repos", return_value=True):
            window = queue_gui.QueueWindow()
        layout = window.centralWidget().layout()
        rows = [layout.itemAt(i).layout() for i in range(layout.count())]
        placement_row = next(
            row for row in rows
            if row is not None and row.indexOf(window.new_items_at_top) >= 0
        )
        self.assertEqual(placement_row.indexOf(window.new_items_at_top), 0)
        self.assertEqual(placement_row.indexOf(window.requeued_items_at_top), 1)
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

    def test_live_review_displaces_stale_finished_row(self):
        app = QApplication.instance() or QApplication([])
        window = Mock()
        window.queue = queue_gui.QTreeWidget()
        window.tasks = queue_gui.QTreeWidget()
        window.monitor_activity = ("reviewing", "42", "live review", 0)
        window.selected_repo.return_value = ""
        window.update_countdown_display = Mock()
        window.update_queue_buttons = Mock()
        window.update_delegate_button = Mock()
        window.apply_monitor_activity_to_queue = lambda: (
            queue_gui.QueueWindow.apply_monitor_activity_to_queue(window)
        )
        status = """Repository: example/repo
Finished CodeRabbit reviews:
  #42 live review
    Result: Agent completed review
    Agent task: Codex Idle\tReview task\tdone
"""

        queue_gui.QueueWindow.populate_queue(window, status)
        queue_gui.QueueWindow.populate_tasks(window, status)

        self.assertEqual(window.queue.topLevelItemCount(), 1)
        self.assertEqual(window.queue.topLevelItem(0).text(2), "In progress")
        self.assertEqual(window.tasks.topLevelItemCount(), 0)
        app.processEvents()

    def test_monitor_transition_moves_existing_finished_row_immediately(self):
        app = QApplication.instance() or QApplication([])
        window = Mock()
        window.queue = queue_gui.QTreeWidget()
        window.tasks = queue_gui.QTreeWidget()
        window.monitor_activity = ("reviewing", "42", "live review", 0)
        window.finished_review_numbers = {"42"}
        window.approved_review_numbers = set()
        task = queue_gui.QTreeWidgetItem(["#42", "live review"])
        task.setData(0, Qt.UserRole, "42")
        window.tasks.addTopLevelItem(task)

        queue_gui.QueueWindow.apply_monitor_activity_to_queue(window)

        self.assertEqual(window.queue.topLevelItemCount(), 1)
        self.assertEqual(window.tasks.topLevelItemCount(), 0)
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
