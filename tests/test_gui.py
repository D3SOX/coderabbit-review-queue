import os
from pathlib import Path
import tempfile
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
    Agent task: Codex Idle (12345678)
""",
        )
        self.assertEqual(window.tasks.topLevelItemCount(), 2)
        approved = window.tasks.topLevelItem(0)
        feedback = window.tasks.topLevelItem(1)
        self.assertEqual(approved.text(2), "Approved")
        self.assertFalse(approved.data(0, Qt.UserRole + 2))
        self.assertEqual(feedback.text(2), "2 unresolved")
        self.assertTrue(feedback.data(0, Qt.UserRole + 2))
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
