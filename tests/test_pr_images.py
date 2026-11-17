import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QEvent, QEventLoop, QPoint, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QHelpEvent, QImage
from PySide6.QtWidgets import QApplication, QToolTip

import queue_gui_pr_images
from queue_gui_pr_images import PrImages, grid_html, image_sources


class ImageSourceTests(unittest.TestCase):
    def test_collects_unique_https_images_without_emoji(self):
        body = (
            '<p><a href="x"><img src="https://example.com/a.png" alt="A"></a>'
            '<img src="https://example.com/a.png"><img class="emoji" src="https://example.com/e.png">'
            '<img src="data:image/png;base64,AA"><img src="https://example.com/b.gif"></p>'
        )
        self.assertEqual(image_sources(body), ["https://example.com/a.png", "https://example.com/b.gif"])

    def test_grid_has_three_columns_and_mentions_the_rest(self):
        files = [(Path(f"/x/{index}.png"), QSize(240, 100)) for index in range(4)]
        grid = grid_html(files, 2, "Held <reason>")
        self.assertEqual(grid.count("<tr>"), 2)
        self.assertEqual(grid.count("<img"), 4)
        self.assertIn("Held &lt;reason&gt;", grid)
        self.assertIn("+2 more", grid)


class PrImagesTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        image = QImage(600, 400, QImage.Format_ARGB32)
        image.fill(QColor("red"))
        image.save(str(root / "shot.png"))
        image.scaled(80, 20).save(str(root / "badge.png"))
        self.calls = root / "calls"
        fake_gh = root / "gh"
        body = json.dumps({"data": {"repository": {"pullRequest": {"bodyHTML": "<p>body</p>"}}}})
        fake_gh.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >>'{self.calls}'\nprintf '%s' '{body}'\n")
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)
        # Local files stand in for the signed image URLs of the rendered body.
        sources = [(root / name).as_uri() for name in ("shot.png", "badge.png", "missing.png", "shot.png")]
        self.patches = [
            patch.object(queue_gui_pr_images, "GH", str(fake_gh)),
            patch.object(queue_gui_pr_images, "image_sources", lambda _body: sources),
            patch.object(queue_gui_pr_images, "MAX_IMAGES", 3),
        ]
        for item in self.patches:
            item.start()
        self.images = PrImages()

    def tearDown(self):
        self.images.deleteLater()
        self.app.processEvents()
        for item in self.patches:
            item.stop()
        self.directory.cleanup()

    def load(self):
        loop = QEventLoop()
        self.images.loaded.connect(loop.quit)
        QTimer.singleShot(5000, loop.quit)
        self.assertIsNone(self.images.images("owner/repo", "7"))
        loop.exec()
        self.images.loaded.disconnect(loop.quit)
        return self.images.images("owner/repo", "7")

    def test_downloads_thumbnails_and_skips_badges_and_failures(self):
        entry = self.load()
        self.assertIn("number=7", self.calls.read_text())
        self.assertEqual([size for _path, size in entry["files"]], [QSize(240, 160)])
        self.assertEqual(entry["more"], 1)
        self.assertTrue(entry["files"][0][0].is_file())
        # Fresh entries are served from the cache until they expire.
        self.images.images("owner/repo", "7")
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        old = entry["files"][0][0]
        entry["time"] = time.time() - queue_gui_pr_images.CACHE_SECONDS
        entry = self.load()
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.assertFalse(old.exists())

    def test_hovering_a_row_shows_the_grid_once_loaded(self):
        spec = importlib.util.spec_from_file_location(
            "queue_gui_window", Path(__file__).resolve().parents[1] / "src/coderabbit-review-queue-gui.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module.QueueWindow, "load_cached_repos", return_value=True):
            window = module.QueueWindow()
        try:
            window.repo_combo.blockSignals(True)
            window.repo_combo.addItem("owner/repo")
            window.repo_combo.blockSignals(False)
            window.show()
            item = module.QTreeWidgetItem(["#7", "Title", "Queued"])
            item.setData(0, Qt.UserRole, "7")
            item.setToolTip(2, "Not asking CodeRabbit yet: reason")
            window.queue.addTopLevelItem(item)
            self.images.deleteLater()
            self.images = window.pr_image_tooltips.images
            self.load()
            viewport = window.queue.viewport()
            position = window.queue.visualItemRect(item).center()
            position.setX(window.queue.columnViewportPosition(2) + 2)
            event = QHelpEvent(QEvent.ToolTip, position, viewport.mapToGlobal(position))
            QApplication.sendEvent(viewport, event)
            self.assertIn("<img", QToolTip.text())
            self.assertIn("Not asking CodeRabbit yet: reason", QToolTip.text())
            QToolTip.hideText()
        finally:
            window.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
