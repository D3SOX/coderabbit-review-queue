import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QDialog

import queue_gui_common
import queue_gui_repo_order
from queue_gui_repo_order import RepoOrderDialog, ordered_repos, save_order, saved_order


class RepoOrderTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.directory = tempfile.TemporaryDirectory()
        self.patch = patch.object(queue_gui_common, "STATE_ROOT", Path(self.directory.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def test_saved_order_wins_and_new_repositories_follow_in_discovery_order(self):
        self.assertEqual(ordered_repos(["a/one", "a/two"]), ["a/one", "a/two"])
        save_order(["a/three", "A/TWO"])
        self.assertEqual(
            ordered_repos(["a/one", "a/two", "a/four", "a/three"]),
            ["a/three", "a/two", "a/one", "a/four"],
        )

    def test_dialog_moves_the_selected_project(self):
        dialog = RepoOrderDialog(["a/one", "a/two", "a/three"], lambda _repo: queue_gui_common.app_icon())
        self.assertFalse(dialog.up_button.isEnabled())
        dialog.down_button.click()
        dialog.down_button.click()
        self.assertEqual(dialog.repos(), ["a/two", "a/three", "a/one"])
        self.assertFalse(dialog.down_button.isEnabled())
        dialog.up_button.click()
        self.assertEqual(dialog.repos(), ["a/two", "a/one", "a/three"])
        dialog.deleteLater()
        self.app.processEvents()

    def test_window_reorders_the_selector_and_tray_menu_and_keeps_the_selection(self):
        spec = importlib.util.spec_from_file_location(
            "queue_gui_window", Path(__file__).resolve().parents[1] / "src/coderabbit-review-queue-gui.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module.QueueWindow, "load_cached_repos", return_value=True):
            window = module.QueueWindow()
        try:
            window.repo_combo.blockSignals(True)
            window.repo_combo.addItems(["a/one", "a/two", "a/three"])
            window.repo_combo.setCurrentIndex(1)
            window.repo_combo.blockSignals(False)
            save_order(["gone/repo", "a/one"])

            def accept(dialog):
                dialog.list.setCurrentRow(2)
                dialog.up_button.click()
                dialog.up_button.click()
                return QDialog.Accepted

            with patch.object(queue_gui_repo_order.RepoOrderDialog, "exec", accept), \
                 patch.object(window, "repo_changed") as changed:
                window.repo_order_button.click()
                changed.assert_not_called()
            self.assertEqual(window.listed_repos(), ["a/three", "a/one", "a/two"])
            self.assertEqual(window.selected_repo(), "a/two")
            self.assertEqual(saved_order(), ["a/three", "a/one", "a/two", "gone/repo"])
            window.update_tray_project_menu()
            self.assertEqual([a.text() for a in window.tray_project_menu.actions()],
                             ["a/three", "a/one", "a/two"])
        finally:
            window.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
