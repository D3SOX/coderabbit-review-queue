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
    def test_request_file_change_triggers_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            request_file = Path(directory) / "requests.tsv"
            request_file.write_text("1\thead\t100\n")
            window = Mock()
            window.selected_repo.return_value = "example/repo"
            window.review_requests_file.return_value = request_file
            window.review_requests_signature = (
                "example/repo",
                queue_gui.file_signature(request_file),
            )
            window.status_process.state.return_value = QProcess.NotRunning

            request_file.write_text("1\thead\t100\n2\thead\t200\n")
            queue_gui.QueueWindow.refresh_if_review_requests_changed(window)

            window.refresh.assert_called_once_with()
            self.assertEqual(
                window.review_requests_signature,
                ("example/repo", queue_gui.file_signature(request_file)),
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


if __name__ == "__main__":
    unittest.main()
