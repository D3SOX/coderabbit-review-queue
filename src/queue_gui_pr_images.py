# SPDX-License-Identifier: GPL-3.0-only

"""Images from a PR's description, shown in a grid when hovering its row."""

import html
import json
import time
from html.parser import HTMLParser
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QProcess, QSize, Qt, QTemporaryDir, QUrl, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QImage
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QToolTip, QTreeWidget

GH = "gh"
MAX_IMAGES = 9
COLUMNS = 3
THUMBNAIL = QSize(240, 160)
MAX_IMAGE_BYTES = 10 << 20
# Badges and inline icons are not screenshots.
MIN_IMAGE_HEIGHT = 40
CACHE_SECONDS = 600
TIMEOUT_MS = 15000
# The rendered body links private attachments through short-lived signed URLs,
# so the images download without credentials.
QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) { pullRequest(number: $number) { bodyHTML } } }"
)


class ImageSources(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        source = values.get("src") or ""
        if tag == "img" and source.startswith("https://") and "emoji" not in (values.get("class") or ""):
            self.sources.append(source)


def image_sources(body_html: str) -> list[str]:
    parser = ImageSources()
    parser.feed(body_html)
    return list(dict.fromkeys(parser.sources))


def grid_html(files: list[tuple[Path, QSize]], more: int, text: str = "") -> str:
    cells = [
        f'<td align="center" width="{THUMBNAIL.width()}"><img src="{html.escape(str(path))}" width="{size.width()}" height="{size.height()}"></td>'
        for path, size in files
    ]
    rows = "".join(
        "<tr>" + "".join(cells[start:start + COLUMNS]) + "</tr>"
        for start in range(0, len(cells), COLUMNS)
    )
    parts = [f"<p>{html.escape(text)}</p>"] if text else []
    parts.append(f'<table cellspacing="4">{rows}</table>')
    if more:
        parts.append(f"<p>+{more} more in the description</p>")
    return "".join(parts)


class PrImages(QObject):
    """Downloads and caches thumbnails of the images in PR descriptions."""

    loaded = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.directory = QTemporaryDir()
        self.network = QNetworkAccessManager(self)
        # (repo, number) -> {"time", "files", "more", "pending", "generation"}
        self.entries: dict[tuple[str, str], dict] = {}
        self.generation = 0

    def images(self, repo: str, number: str) -> dict | None:
        """The cached thumbnails, or None while they are still loading."""
        key = (repo, number)
        entry = self.entries.get(key)
        if entry and (entry["pending"] or time.time() - entry["time"] < CACHE_SECONDS):
            return None if entry["pending"] else entry
        self.fetch(key, entry)
        return None

    def fetch(self, key: tuple[str, str], old: dict | None) -> None:
        for path, _size in (old or {}).get("files", []):
            path.unlink(missing_ok=True)
        self.generation += 1
        entry = {"time": time.time(), "files": [], "more": 0, "pending": 0, "generation": self.generation}
        self.entries[key] = entry
        owner, _, name = key[0].partition("/")
        process = QProcess(self)
        entry["pending"] = 1
        process.finished.connect(lambda code, _status: self.body_fetched(key, entry, process, code))
        process.errorOccurred.connect(
            lambda error: error == QProcess.FailedToStart and self.body_fetched(key, entry, process, -1))
        process.start(GH, [
            "api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"name={name}",
            "-F", f"number={key[1]}",
        ])

    def body_fetched(self, key: tuple[str, str], entry: dict, process: QProcess, code: int) -> None:
        output = bytes(process.readAllStandardOutput()) if code == 0 else b""
        process.deleteLater()
        try:
            body = json.loads(output)["data"]["repository"]["pullRequest"]["bodyHTML"] or ""
        except (ValueError, KeyError, TypeError):
            body = ""
        sources = image_sources(body)
        entry["more"] = max(0, len(sources) - MAX_IMAGES)
        entry["slots"] = [None] * min(len(sources), MAX_IMAGES)
        entry["pending"] = len(entry["slots"])
        for index, source in enumerate(sources[:MAX_IMAGES]):
            request = QNetworkRequest(QUrl(source))
            request.setTransferTimeout(TIMEOUT_MS)
            reply = self.network.get(request)
            reply.downloadProgress.connect(
                lambda received, _total, reply=reply: received > MAX_IMAGE_BYTES and reply.abort())
            reply.finished.connect(
                lambda reply=reply, index=index: self.image_fetched(key, entry, index, reply))
        if not entry["pending"]:
            self.finish(key, entry)

    def image_fetched(self, key: tuple[str, str], entry: dict, index: int, reply: QNetworkReply) -> None:
        data = bytes(reply.readAll()) if reply.error() == QNetworkReply.NoError else b""
        reply.deleteLater()
        image = QImage.fromData(data)
        if not image.isNull() and image.height() >= MIN_IMAGE_HEIGHT:
            size = image.size().scaled(THUMBNAIL, Qt.KeepAspectRatio).boundedTo(image.size())
            ratio = QGuiApplication.instance().devicePixelRatio()
            # Larger pixels than the cell keep the thumbnail sharp on HiDPI screens.
            thumbnail = image.scaled(size * ratio, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            path = Path(self.directory.path()) / f"{entry['generation']}-{index}.png"
            if thumbnail.save(str(path)):
                entry["slots"][index] = (path, size)
        entry["pending"] -= 1
        if not entry["pending"]:
            self.finish(key, entry)

    def finish(self, key: tuple[str, str], entry: dict) -> None:
        entry["files"] = [slot for slot in entry.pop("slots", []) if slot]
        entry["time"] = time.time()
        if self.entries.get(key) is entry:
            self.loaded.emit(*key)


class PrImageTooltips(QObject):
    """Shows the images of the PR under the pointer in the row's tooltip."""

    def __init__(self, repo, trees: list[QTreeWidget], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.repo = repo
        self.trees = {tree.viewport(): tree for tree in trees}
        self.images = PrImages(self)
        self.images.loaded.connect(self.images_loaded)
        for viewport in self.trees:
            viewport.installEventFilter(self)

    def hovered(self, viewport, position):
        tree = self.trees[viewport]
        item = tree.itemAt(position)
        number = item.data(0, Qt.UserRole) if item else None
        return (tree, item, number) if number else (tree, None, None)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.ToolTip or watched not in self.trees:
            return False
        return self.show(watched, event.pos(), event.globalPos())

    def show(self, viewport, position, global_position) -> bool:
        tree, item, number = self.hovered(viewport, position)
        repo = self.repo()
        if not item or not repo:
            return False
        entry = self.images.images(repo, str(number))
        if not entry or not entry["files"]:
            return False
        text = item.toolTip(tree.columnAt(position.x()))
        QToolTip.showText(global_position, grid_html(entry["files"], entry["more"], text),
                          viewport, tree.visualItemRect(item))
        return True

    def images_loaded(self, repo: str, number: str) -> None:
        if repo != self.repo():
            return
        for viewport in self.trees:
            if not viewport.isVisible():
                continue
            position = viewport.mapFromGlobal(QCursor.pos())
            if not viewport.rect().contains(position):
                continue
            _tree, _item, hovered = self.hovered(viewport, position)
            if str(hovered) == number:
                self.show(viewport, position, QCursor.pos())
