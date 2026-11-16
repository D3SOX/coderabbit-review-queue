# SPDX-License-Identifier: GPL-3.0-only

"""Project icons for the repository selector and the tray's project menu."""

import json
import os
import re
import shutil
import time
import zlib
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from PySide6.QtCore import QObject, QProcess, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox, QPushButton

import queue_gui_common

GH = "gh"
ICON_EXTENSIONS = ("png", "svg", "ico", "webp")
MAX_ICON_BYTES = 1 << 20
MAX_CANDIDATES = 15
# Re-detect automatic icons this often, and on "Refresh repositories".
DETECTION_MAX_AGE = 7 * 86400
SKIPPED_DIRECTORIES = {
    ".idea", "debug", "dev", "example", "examples", "fixtures", "node_modules",
    "sample", "samples", "spec", "test", "tests", "third_party", "vendor",
}
NAME_SCORES = {
    "icon": 40, "appicon": 40, "app-icon": 40, "app_icon": 40, "ic_launcher": 40,
    "logo": 35, "apple-touch-icon": 35, "favicon": 30, "ic_launcher_round": 30,
}
NAME_PREFIXES = ("icon", "logo", "appicon", "app-icon", "app_icon", "ic_launcher", "favicon")
# Android launcher layers and icon sprites are not the project's icon.
SKIPPED_NAME_PARTS = ("foreground", "background", "monochrome")
EXTENSION_SCORES = {"svg": 8, "png": 6, "webp": 4, "ico": 2}
DENSITY_SCORES = {"xxxhdpi": 5, "xxhdpi": 4, "xhdpi": 3, "hdpi": 2, "mdpi": 1}
# Only the tree's small images, so large repositories stay cheap to scan.
TREE_FILTER = (
    '[.tree[] | select(.type == "blob" and (.size // 0) <= %d'
    ' and (.path | test("\\\\.(png|svg|ico|webp)$"; "i"))) | .path]' % MAX_ICON_BYTES
)
# An image named after the project, such as a desktop app's nevumi.png.
PROJECT_NAME_SCORE = 38


def icon_root() -> Path:
    return queue_gui_common.STATE_ROOT / "repo-icons"


def repo_key(repo: str) -> str:
    return repo.replace("/", "__")


def name_key(name: str) -> str:
    return re.sub(r"[-_.\s]+", "", name.lower())


def candidate_score(path: str, project: str = "") -> int | None:
    parts = PurePosixPath(path)
    if any(part.lower() in SKIPPED_DIRECTORIES for part in parts.parts[:-1]):
        return None
    extension = parts.suffix.lower().lstrip(".")
    stem = parts.stem.lower()
    if extension not in EXTENSION_SCORES or stem.startswith("icons"):
        return None
    if any(part in stem for part in SKIPPED_NAME_PARTS):
        return None
    score = NAME_SCORES.get(stem)
    if score is None and project and name_key(stem) == name_key(project):
        score = PROJECT_NAME_SCORE
    if score is None:
        if stem.startswith(NAME_PREFIXES):
            score = 20
        elif "logo" in stem:
            score = 15
        else:
            return None
    score += EXTENSION_SCORES[extension]
    density = re.search(r"(?:mipmap|drawable)-(x*hdpi|mdpi)", path)
    if density:
        score += DENSITY_SCORES[density.group(1)]
    return score - 2 * (len(parts.parts) - 1)


def icon_candidates(paths: list[str], project: str = "") -> list[str]:
    """Rank a repository's icon-like files, the likeliest project icon first."""
    scored = [(score, path) for path in paths if (score := candidate_score(path, project)) is not None]
    scored.sort(key=lambda item: (-item[0], item[1]))
    # Of an Android icon's density variants, offer only the sharpest.
    variants = {}
    for _score, path in scored:
        variants.setdefault(re.sub(r"\b(mipmap|drawable)-[a-z]*dpi\b", r"\1", path), path)
    return list(variants.values())[:MAX_CANDIDATES]


def image_is_visible(path: Path) -> bool:
    """Whether Qt draws the image; it ignores SVG styles such as dark-mode fills."""
    image = QIcon(str(path)).pixmap(32, 32).toImage()
    if image.isNull():
        return False
    visible = sum(
        image.pixelColor(x, y).alpha() > 0
        for x in range(image.width()) for y in range(image.height())
    )
    return visible >= image.width() * image.height() // 50


def initials(repo: str) -> str:
    name = repo.rsplit("/", 1)[-1]
    words = [word for word in re.split(r"[-_.\s]+", name) if word]
    if len(words) == 1:
        words = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", words[0]) or words
    return "".join(word[0] for word in words[:2]).upper() or "?"


def initials_icon(repo: str) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor.fromHsl(zlib.crc32(repo.lower().encode()) % 360, 140, 105))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    letters = initials(repo)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(32 if len(letters) == 1 else 26)
    painter.setFont(font)
    painter.setPen(Qt.white)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, letters)
    painter.end()
    return QIcon(pixmap)


class RepoIcons(QObject):
    """Detects, caches and loads each repository's icon.

    A repository's choice is "auto" (the best-ranked image in its default
    branch), "repo" (an image the user picked from that list), "file" (a copied
    local image) or "initials".
    """

    changed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.icons: dict[str, QIcon] = {}
        self.pending: list[tuple[str, bool]] = []
        self.process = QProcess(self)
        self.process.finished.connect(self.step_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.current = ""
        self.step = ""
        self.download_path: Path | None = None
        self.download_origin = ""

    def settings_file(self, repo: str) -> Path:
        return icon_root() / f"{repo_key(repo)}.json"

    def settings(self, repo: str) -> dict:
        try:
            data = json.loads(self.settings_file(repo).read_text())
        except (OSError, ValueError):
            return {"choice": "auto"}
        return data if isinstance(data, dict) else {"choice": "auto"}

    def save_settings(self, repo: str, data: dict) -> None:
        icon_root().mkdir(parents=True, exist_ok=True)
        path = self.settings_file(repo)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data))
        os.replace(temporary, path)

    def image_file(self, repo: str) -> Path | None:
        image = self.settings(repo).get("image")
        if not isinstance(image, str) or "/" in image:
            return None
        path = icon_root() / image
        return path if path.is_file() else None

    def icon(self, repo: str) -> QIcon:
        if repo not in self.icons:
            path = self.image_file(repo)
            self.icons[repo] = QIcon(str(path)) if path else initials_icon(repo)
        return self.icons[repo]

    def candidates(self, repo: str) -> list[str] | None:
        found = self.settings(repo).get("candidates")
        return found if isinstance(found, list) else None

    def ensure(self, repos: list[str], force: bool = False) -> None:
        """Detect or fetch the icons REPOS still need; FORCE re-detects."""
        for repo in repos:
            entry = next((item for item in self.pending if item[0] == repo), None)
            if entry is None:
                self.pending.append((repo, force))
            elif force and not entry[1]:
                self.pending[self.pending.index(entry)] = (repo, True)
        self.start_next()

    def needs_tree(self, data: dict, force: bool) -> bool:
        if data.get("candidates") is None:
            return True
        stale = time.time() - float(data.get("checked", 0) or 0) > DETECTION_MAX_AGE
        return data.get("choice", "auto") == "auto" and (force or stale)

    def needs_download(self, repo: str, data: dict) -> bool:
        return (
            data.get("choice") == "repo"
            and isinstance(data.get("path"), str)
            and (data.get("image_path") != data["path"] or self.image_file(repo) is None)
        )

    def start_next(self) -> None:
        while self.process.state() == QProcess.NotRunning and self.pending:
            repo, force = self.pending.pop(0)
            data = self.settings(repo)
            if self.needs_tree(data, force):
                self.current, self.step = repo, "tree"
                self.process.setStandardOutputFile("")
                self.process.start(GH, [
                    "api", f"repos/{repo}/git/trees/HEAD?recursive=1", "--jq", TREE_FILTER,
                ])
            elif self.needs_download(repo, data):
                self.download(repo, data["path"])

    def process_error(self, error: QProcess.ProcessError) -> None:
        # Without gh, finished never fires; give up on this repository.
        if error == QProcess.FailedToStart:
            self.step_finished(-1, QProcess.CrashExit)

    def step_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        repo, step = self.current, self.step
        self.current = self.step = ""
        data = self.settings(repo)
        if step == "tree":
            output = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
            try:
                paths = json.loads(output) if exit_code == 0 else None
            except ValueError:
                paths = None
            if isinstance(paths, list):
                data["candidates"] = icon_candidates(
                    [path for path in paths if isinstance(path, str)], repo.rsplit("/", 1)[-1])
                data["checked"] = time.time()
                if data.get("choice", "auto") == "auto":
                    data["path"] = next(iter(data["candidates"]), None)
                    if data["path"] is None:
                        self.set_image(repo, data, None, None)
                self.save_settings(repo, data)
                if data.get("choice", "auto") == "auto" and data["path"]:
                    self.download(repo, data["path"])
                    return
                if self.needs_download(repo, data):
                    self.download(repo, data["path"])
                    return
        elif step == "download" and self.download_path:
            path, self.download_path = self.download_path, None
            if exit_code == 0 and QImageReader(str(path)).canRead() and image_is_visible(path):
                self.set_image(repo, data, path, self.download_origin)
                self.save_settings(repo, data)
            else:
                path.unlink(missing_ok=True)
                # Try the next detected image instead.
                candidates = data.get("candidates") or []
                if data.get("choice", "auto") == "auto" and self.download_origin in candidates:
                    following = candidates[candidates.index(self.download_origin) + 1:]
                    if following:
                        data["path"] = following[0]
                        self.save_settings(repo, data)
                        self.download(repo, following[0])
                        return
                    self.set_image(repo, data, None, None)
                    self.save_settings(repo, data)
            # The user may have picked another image during the download.
            if self.needs_download(repo, data):
                self.pending.append((repo, False))
        self.changed.emit(repo)
        self.start_next()

    def download(self, repo: str, path: str) -> None:
        extension = PurePosixPath(path).suffix.lower()
        icon_root().mkdir(parents=True, exist_ok=True)
        self.download_path = icon_root() / f"{repo_key(repo)}.download{extension}"
        self.download_origin = path
        self.current, self.step = repo, "download"
        self.process.setStandardOutputFile(str(self.download_path))
        self.process.start(GH, [
            "api", "-H", "Accept: application/vnd.github.raw",
            f"repos/{repo}/contents/{quote(path)}",
        ])

    def set_image(self, repo: str, data: dict, source: Path | None, origin: str | None) -> None:
        """Store SOURCE (from repository path ORIGIN) as the repository's image."""
        old = data.get("image")
        if isinstance(old, str) and "/" not in old:
            (icon_root() / old).unlink(missing_ok=True)
        data.pop("image", None)
        data["image_path"] = origin
        if source is not None:
            # A new name per image, so Qt cannot reuse a cached older one.
            image = f"{repo_key(repo)}-{int(time.time() * 1000)}{source.suffix.lower()}"
            os.replace(source, icon_root() / image)
            data["image"] = image
        self.icons.pop(repo, None)

    def choose_auto(self, repo: str) -> None:
        self.save_settings(repo, {**self.settings(repo), "choice": "auto"})
        self.ensure([repo], force=True)

    def choose_repo_path(self, repo: str, path: str) -> None:
        self.save_settings(repo, {**self.settings(repo), "choice": "repo", "path": path})
        self.ensure([repo])

    def choose_file(self, repo: str, source: str) -> bool:
        if not QImageReader(source).canRead() or not image_is_visible(Path(source)):
            return False
        icon_root().mkdir(parents=True, exist_ok=True)
        temporary = icon_root() / f"{repo_key(repo)}.copy{Path(source).suffix.lower()}"
        shutil.copyfile(source, temporary)
        data = {**self.settings(repo), "choice": "file"}
        self.set_image(repo, data, temporary, None)
        self.save_settings(repo, data)
        self.changed.emit(repo)
        return True

    def choose_initials(self, repo: str) -> None:
        data = {**self.settings(repo), "choice": "initials"}
        self.set_image(repo, data, None, None)
        self.save_settings(repo, data)
        self.changed.emit(repo)


class RepoIconsMixin:
    """The icon button beside the repository selector, and icons in its lists."""

    def init_repo_icons(self) -> None:
        self.repo_icons = RepoIcons(self)
        self.repo_icons.changed.connect(self.repo_icon_changed)
        self.repo_icon_button = QPushButton(QIcon.fromTheme("insert-image"), "Project icon")
        self.repo_icon_button.setToolTip("Choose the icon shown for this repository")
        self.repo_icon_menu = QMenu(self.repo_icon_button)
        self.repo_icon_menu.aboutToShow.connect(self.build_repo_icon_menu)
        # A button with setMenu() reserves room for an arrow some styles leave empty.
        self.repo_icon_button.clicked.connect(
            lambda: self.repo_icon_menu.popup(
                self.repo_icon_button.mapToGlobal(self.repo_icon_button.rect().bottomLeft())
            )
        )
        self.repo_icon_button.setEnabled(False)

    def apply_repo_icons(self, repos: list[str], force: bool = False) -> None:
        for index in range(self.repo_combo.count()):
            repo = self.repo_combo.itemText(index)
            self.repo_combo.setItemIcon(index, self.repo_icons.icon(repo))
        self.update_repo_icon_button()
        self.repo_icons.ensure(repos, force)

    def update_repo_icon_button(self) -> None:
        self.repo_icon_button.setEnabled(bool(self.selected_repo()))

    def repo_icon_changed(self, repo: str) -> None:
        index = self.repo_combo.findText(repo)
        if index >= 0:
            self.repo_combo.setItemIcon(index, self.repo_icons.icon(repo))

    def build_repo_icon_menu(self) -> None:
        menu = self.repo_icon_menu
        menu.clear()
        repo = self.selected_repo()
        if not repo:
            return
        data = self.repo_icons.settings(repo)
        choice = data.get("choice", "auto")
        group = QActionGroup(menu)

        def add(text: str, checked: bool, handler) -> QAction:
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(checked)
            group.addAction(action)
            action.triggered.connect(lambda _checked=False: handler())
            return action

        add("Detect automatically", choice == "auto",
            lambda: self.repo_icons.choose_auto(repo))
        menu.addSection("From the repository")
        candidates = self.repo_icons.candidates(repo)
        if candidates is None:
            menu.addAction("Looking for images…").setEnabled(False)
            self.repo_icons.ensure([repo])
        elif not candidates:
            menu.addAction("No icon or logo images found").setEnabled(False)
        for path in candidates or []:
            add(path, choice == "repo" and data.get("path") == path,
                lambda path=path: self.repo_icons.choose_repo_path(repo, path))
        menu.addSeparator()
        add("Choose image file…", choice == "file",
            lambda: self.choose_repo_icon_file(repo))
        add("Use initials", choice == "initials",
            lambda: self.repo_icons.choose_initials(repo))

    def choose_repo_icon_file(self, repo: str) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            f"Icon for {repo}",
            str(Path.home()),
            "Images (*.png *.svg *.ico *.webp *.jpg *.jpeg)",
        )
        if path and not self.repo_icons.choose_file(repo, path):
            QMessageBox.warning(self, "Unsupported image", f"Could not read {path} as an image.")
