import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import queue_gui_common
import queue_gui_icons
from queue_gui_icons import RepoIcons, icon_candidates, initials


class IconCandidateTests(unittest.TestCase):
    def test_project_icons_rank_before_other_images(self):
        paths = [
            "_icons/iconColor.png",
            "_icons/icon.ico",
            "_icons/icon.svg",
            "website/assets/icons.svg",
            "spec/fixtures/assets/logo.png",
            "node_modules/pkg/logo.svg",
            "app/src/debug/res/mipmap-xxxhdpi/ic_launcher.png",
            "app/src/main/res/mipmap-hdpi/ic_launcher.png",
            "app/src/main/res/mipmap-xxxhdpi/ic_launcher.png",
            "app/src/main/res/mipmap-xxxhdpi/ic_launcher_foreground.png",
            "app/src/dev/res/mipmap-xxxhdpi/ic_launcher.png",
            "src/renderer/assets/img/invidious-logo-dark.svg",
            "README.md",
        ]
        self.assertEqual(icon_candidates(paths), [
            "_icons/icon.svg",
            "app/src/main/res/mipmap-xxxhdpi/ic_launcher.png",
            "_icons/icon.ico",
            "_icons/iconColor.png",
            "src/renderer/assets/img/invidious-logo-dark.svg",
        ])

    def test_initials_split_words_and_camel_case(self):
        self.assertEqual(initials("D3SOX/Nevumi"), "N")
        self.assertEqual(initials("OpenTubeX/sync-server"), "SS")
        self.assertEqual(initials("OpenTubeX/LibreTube-Enhanced"), "LE")
        self.assertEqual(initials("OpenTubeX/OpenTubeX"), "OT")


class RepoIconsTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.state = root / "state"
        image = QImage(8, 8, QImage.Format_ARGB32)
        image.fill(QColor("red"))
        self.png = root / "icon.png"
        image.save(str(self.png))
        image.fill(QColor(0, 0, 0, 0))
        self.blank = root / "blank.png"
        image.save(str(self.blank))
        self.calls = root / "calls"
        fake_gh = root / "gh"
        fake_gh.write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >>'{self.calls}'
case "$*" in
  *git/trees*) printf '%s\\n' '["docs/logo.png", "assets/icon.png", "tests/icon.png"]' ;;
  *contents/$(cat '{root}/blank-paths' 2>/dev/null || echo none)) cat '{self.blank}' ;;
  *contents/*) cat '{self.png}' ;;
esac
""")
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)
        self.patches = [
            patch.object(queue_gui_common, "STATE_ROOT", self.state),
            patch.object(queue_gui_icons, "GH", str(fake_gh)),
        ]
        for item in self.patches:
            item.start()
        self.icons = RepoIcons()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.directory.cleanup()

    def wait_for_change(self):
        loop = QEventLoop()
        self.icons.changed.connect(loop.quit)
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        self.icons.changed.disconnect(loop.quit)

    def gh_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_detects_downloads_and_caches_the_best_image(self):
        self.icons.ensure(["owner/repo"])
        self.wait_for_change()
        settings = self.icons.settings("owner/repo")
        self.assertEqual(settings["candidates"], ["assets/icon.png", "docs/logo.png"])
        self.assertEqual(settings["image_path"], "assets/icon.png")
        self.assertEqual(self.icons.image_file("owner/repo").read_bytes(), self.png.read_bytes())
        self.assertEqual(len(self.gh_calls()), 2)
        self.assertIn("contents/assets/icon.png", self.gh_calls()[1])
        # A fresh detection is not repeated until a refresh forces it.
        self.icons.ensure(["owner/repo"])
        self.assertEqual(len(self.gh_calls()), 2)
        self.icons.ensure(["owner/repo"], force=True)
        self.wait_for_change()
        self.assertEqual(len(self.gh_calls()), 4)
        self.assertEqual(len(list((self.state / "repo-icons").glob("owner__repo-*"))), 1)

    def test_an_image_qt_draws_blank_falls_through_to_the_next_one(self):
        (Path(self.directory.name) / "blank-paths").write_text("assets/icon.png")
        self.icons.ensure(["owner/repo"])
        self.wait_for_change()
        self.assertEqual(self.icons.settings("owner/repo")["image_path"], "docs/logo.png")
        self.assertEqual(len(self.gh_calls()), 3)
        self.assertFalse(self.icons.choose_file("owner/repo", str(self.blank)))

    def test_a_picked_repository_image_survives_refreshes(self):
        self.icons.ensure(["owner/repo"])
        self.wait_for_change()
        self.icons.choose_repo_path("owner/repo", "docs/logo.png")
        self.wait_for_change()
        self.assertIn("contents/docs/logo.png", self.gh_calls()[-1])
        self.assertEqual(self.icons.settings("owner/repo")["image_path"], "docs/logo.png")
        calls = len(self.gh_calls())
        self.icons.ensure(["owner/repo"], force=True)
        self.assertEqual(len(self.gh_calls()), calls)

    def test_local_file_and_initials_replace_the_image(self):
        self.assertTrue(self.icons.choose_file("owner/repo", str(self.png)))
        self.assertTrue(self.png.exists())
        self.assertEqual(self.icons.settings("owner/repo")["choice"], "file")
        self.assertIsNotNone(self.icons.image_file("owner/repo"))
        self.assertFalse(self.icons.choose_file("owner/repo", str(self.calls)))
        self.icons.choose_initials("owner/repo")
        self.assertIsNone(self.icons.image_file("owner/repo"))
        self.assertEqual(list((self.state / "repo-icons").glob("owner__repo-*")), [])
        self.assertFalse(self.icons.icon("owner/repo").isNull())

    def test_failed_detection_falls_back_to_initials(self):
        with patch.object(queue_gui_icons, "GH", str(Path(self.directory.name) / "missing-gh")):
            self.icons.ensure(["owner/repo"])
            self.wait_for_change()
        self.assertIsNone(self.icons.image_file("owner/repo"))
        self.assertFalse(self.icons.icon("owner/repo").isNull())
        self.assertEqual(self.icons.process.state(), self.icons.process.state().NotRunning)

    def test_selector_menu_offers_detected_images_and_marks_the_choice(self):
        spec = __import__("importlib.util").util.spec_from_file_location(
            "queue_gui_window", Path(__file__).resolve().parents[1] / "src/coderabbit-review-queue-gui.py")
        module = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.icons.save_settings("owner/repo", {
            "choice": "repo", "path": "docs/logo.png", "image_path": "docs/logo.png",
            "candidates": ["assets/icon.png", "docs/logo.png"], "checked": 9e12,
        })
        with patch.object(module.QueueWindow, "load_cached_repos", return_value=True):
            window = module.QueueWindow()
        try:
            window.repo_combo.addItems(["owner/repo", "other/repo"])
            window.repo_combo.setCurrentIndex(0)
            window.apply_repo_icons([])
            self.assertFalse(window.repo_combo.itemIcon(1).isNull())
            self.assertTrue(window.repo_icon_button.isEnabled())
            window.build_repo_icon_menu()
            actions = [a for a in window.repo_icon_menu.actions() if a.text() and not a.isSeparator()]
            self.assertEqual([a.text() for a in actions], [
                "Detect automatically", "assets/icon.png",
                "docs/logo.png", "Choose image file…", "Use initials",
            ])
            self.assertEqual([a.text() for a in actions if a.isChecked()], ["docs/logo.png"])
            actions[-1].trigger()
            self.assertEqual(window.repo_icons.settings("owner/repo")["choice"], "initials")
            window.update_tray_project_menu()
            self.assertTrue(all(not a.icon().isNull() for a in window.tray_project_menu.actions()))
        finally:
            window.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
