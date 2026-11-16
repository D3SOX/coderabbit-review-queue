# SPDX-License-Identifier: GPL-3.0-only

"""The user's order of projects in the repository selector and tray menu."""

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QListWidget,
    QPushButton, QVBoxLayout,
)

import queue_gui_common


def order_file() -> Path:
    return queue_gui_common.STATE_ROOT / "repository-order.txt"


def saved_order() -> list[str]:
    try:
        return [line.strip() for line in order_file().read_text().splitlines() if line.strip()]
    except OSError:
        return []


def save_order(repos: list[str]) -> None:
    order_file().parent.mkdir(parents=True, exist_ok=True)
    temporary = order_file().with_name(order_file().name + ".tmp")
    temporary.write_text("".join(f"{repo}\n" for repo in repos))
    os.replace(temporary, order_file())


def ordered_repos(repos: list[str]) -> list[str]:
    """REPOS in the saved order; repositories it does not list keep theirs at the end."""
    rank = {repo.lower(): index for index, repo in enumerate(saved_order())}
    return sorted(repos, key=lambda repo: rank.get(repo.lower(), len(rank)))


class RepoOrderDialog(QDialog):
    def __init__(self, repos: list[str], icon_for, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Reorder projects")
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        for repo in repos:
            self.list.addItem(repo)
            self.list.item(self.list.count() - 1).setIcon(icon_for(repo))
        self.list.setCurrentRow(0)
        # Styles such as Breeze pad the list a little beyond its frame.
        rows = self.list.sizeHintForRow(0) * len(repos) + 2 * self.list.frameWidth() + 8
        self.list.setMinimumSize(360, min(480, rows))

        self.up_button = QPushButton(QIcon.fromTheme("go-up"), "Move up")
        self.down_button = QPushButton(QIcon.fromTheme("go-down"), "Move down")
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.list.currentRowChanged.connect(self.update_buttons)
        moves = QVBoxLayout()
        moves.addWidget(self.up_button)
        moves.addWidget(self.down_button)
        moves.addStretch(1)
        row = QHBoxLayout()
        row.addWidget(self.list, 1)
        row.addLayout(moves)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(buttons)
        self.update_buttons()

    def repos(self) -> list[str]:
        return [self.list.item(index).text() for index in range(self.list.count())]

    def move_selected(self, offset: int) -> None:
        row = self.list.currentRow()
        target = row + offset
        if row < 0 or not 0 <= target < self.list.count():
            return
        self.list.insertItem(target, self.list.takeItem(row))
        self.list.setCurrentRow(target)

    def update_buttons(self) -> None:
        row = self.list.currentRow()
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < self.list.count() - 1)


class RepoOrderMixin:
    def init_repo_order(self) -> None:
        self.repo_order_button = QPushButton(QIcon.fromTheme("view-sort"), "Reorder projects…")
        self.repo_order_button.setToolTip("Change the order of projects in this list and the tray menu")
        self.repo_order_button.clicked.connect(self.reorder_repos)

    def listed_repos(self) -> list[str]:
        return [self.repo_combo.itemText(index) for index in range(self.repo_combo.count())]

    def reorder_repos(self) -> None:
        dialog = RepoOrderDialog(self.listed_repos(), self.repo_icons.icon, self)
        if dialog.exec() != QDialog.Accepted:
            return
        repos = dialog.repos()
        # Keep repositories that the saved order lists but discovery missed now.
        listed = {repo.lower() for repo in repos}
        save_order(repos + [repo for repo in saved_order() if repo.lower() not in listed])
        self.apply_repo_order()

    def apply_repo_order(self) -> None:
        """Reorder the selector in place, keeping the selection and its state."""
        selected = self.selected_repo()
        repos = ordered_repos(self.listed_repos())
        if repos == self.listed_repos():
            return
        self.repo_combo.blockSignals(True)
        self.repo_combo.clear()
        for repo in repos:
            self.repo_combo.addItem(self.repo_icons.icon(repo), repo)
        index = self.repo_combo.findText(selected)
        if index >= 0:
            self.repo_combo.setCurrentIndex(index)
        else:
            self.repo_combo.setEditText(selected)
        self.repo_combo.blockSignals(False)
