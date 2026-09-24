# SPDX-License-Identifier: GPL-3.0-only

import os
import re
from pathlib import Path

from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton,
    QSlider, QTextEdit, QVBoxLayout,
)

from queue_gui_common import (
    BABYSIT_DELEGATION_PROMPT, DEFAULT_DELEGATION_PROMPT, SCRIPT,
    SHORT_DELEGATION_PROMPT, STATE_ROOT, ssh_config_hosts,
)


class SettingsMixin:
    def agent_host_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-agent-host"

    def delegation_prompt_mode_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-delegation-prompt-mode"

    def delegation_prompt_template_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-delegation-prompt-template"

    def auto_merge_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-auto-merge"

    def merge_method_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-merge-method"

    def delete_branch_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-delete-branch"

    def merge_after_delegation_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-merge-after-delegation"

    def post_delegation_review_mode(self, repo: str) -> str:
        try:
            mode = self.merge_after_delegation_file(repo).read_text().strip()
        except OSError:
            mode = "1"
        return mode if mode in {"0", "1", "agent"} else "1"

    def merge_after_approval_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-merge-after-approval"

    def merge_admin_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-merge-admin"

    def archive_after_merge_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-archive-after-merge"

    def configure_auto_merge(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        try:
            enabled = self.auto_merge_file(repo).read_text().strip() == "1"
        except OSError:
            enabled = False
        try:
            method = self.merge_method_file(repo).read_text().strip()
        except OSError:
            method = "squash"
        if method not in {"merge", "squash", "rebase"}:
            method = "squash"
        try:
            delete_branch = self.delete_branch_file(repo).read_text().strip() != "0"
        except OSError:
            delete_branch = True
        review_mode = self.post_delegation_review_mode(repo)
        try:
            merge_after_approval = (
                self.merge_after_approval_file(repo).read_text().strip() != "0"
            )
        except OSError:
            merge_after_approval = True
        try:
            merge_admin = self.merge_admin_file(repo).read_text().strip() == "1"
        except OSError:
            merge_admin = False
        try:
            archive_after_merge = (
                self.archive_after_merge_file(repo).read_text().strip() == "1"
            )
        except OSError:
            archive_after_merge = False

        dialog = QDialog(self)
        dialog.setWindowTitle("Configure approval actions")
        archive_box = QCheckBox(
            "Archive the matching Codex task after the PR is merged"
        )
        archive_box.setChecked(archive_after_merge)
        enabled_box = QCheckBox("Automatically merge pull requests")
        enabled_box.setChecked(enabled)
        method_label = QLabel("Merge method")
        method_select = QComboBox()
        method_select.addItem("Merge commit", "merge")
        method_select.addItem("Squash and merge", "squash")
        method_select.addItem("Rebase and merge", "rebase")
        method_select.setCurrentIndex(method_select.findData(method))
        delete_box = QCheckBox("Delete the branch after merging")
        delete_box.setChecked(delete_branch)
        review_label = QLabel("After a delegated fix")
        review_select = QComboBox()
        review_select.addItem("Require another CodeRabbit review", "0")
        review_select.addItem("Merge without another review", "1")
        review_select.addItem("Let the agent decide", "agent")
        review_select.setCurrentIndex(review_select.findData(review_mode))
        approval_box = QCheckBox("Merge after CodeRabbit approval")
        approval_box.setChecked(merge_after_approval)
        admin_box = QCheckBox("Use --admin to bypass branch protection")
        admin_box.setChecked(merge_admin)
        explanation = QLabel(
            "The agent-decision option lets the agent request another review by "
            "leaving the PR open for the queue, or merge without one when it judges "
            "the fix sufficiently covered. The separate approval option lets the "
            "app merge an approved head. Merges run on the selected agent host and "
            "never enable GitHub auto-merge. Branch protection is bypassed only "
            "when your GitHub account permits it. Task archiving is independent "
            "of auto-merge and happens only after a merge is confirmed and the "
            "matching Codex task is idle."
        )
        explanation.setWordWrap(True)

        def update_controls() -> None:
            active = enabled_box.isChecked()
            method_label.setEnabled(active)
            method_select.setEnabled(active)
            delete_box.setEnabled(active)
            review_label.setEnabled(active)
            review_select.setEnabled(active)
            approval_box.setEnabled(active)
            admin_box.setEnabled(active)

        enabled_box.toggled.connect(lambda _checked: update_controls())
        update_controls()
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(archive_box)
        layout.addWidget(enabled_box)
        layout.addWidget(approval_box)
        layout.addWidget(review_label)
        layout.addWidget(review_select)
        layout.addWidget(method_label)
        layout.addWidget(method_select)
        layout.addWidget(delete_box)
        layout.addWidget(admin_box)
        layout.addWidget(explanation)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return

        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        values = (
            (self.auto_merge_file(repo), "1\n" if enabled_box.isChecked() else "0\n"),
            (self.merge_method_file(repo), f"{method_select.currentData()}\n"),
            (self.delete_branch_file(repo), "1\n" if delete_box.isChecked() else "0\n"),
            (
                self.merge_after_delegation_file(repo),
                f"{review_select.currentData()}\n",
            ),
            (
                self.merge_after_approval_file(repo),
                "1\n" if approval_box.isChecked() else "0\n",
            ),
            (
                self.merge_admin_file(repo),
                "1\n" if admin_box.isChecked() else "0\n",
            ),
            (
                self.archive_after_merge_file(repo),
                "1\n" if archive_box.isChecked() else "0\n",
            ),
        )
        for target, value in values:
            temporary = target.with_suffix(".tmp")
            temporary.write_text(value)
            os.replace(temporary, target)
        self.show_transient_status("Approval action settings saved")

    def delegation_prompt_settings(self, repo: str) -> tuple[str, str]:
        try:
            mode = self.delegation_prompt_mode_file(repo).read_text().strip()
        except OSError:
            mode = "default"
        if mode not in {"default", "short", "babysit", "custom"}:
            mode = "default"
        try:
            custom = self.delegation_prompt_template_file(repo).read_text()
        except OSError:
            custom = ""
        return mode, custom

    def configure_delegation_prompt(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        current_mode, custom_prompt = self.delegation_prompt_settings(repo)
        dialog = QDialog(self)
        dialog.setWindowTitle("Delegation prompt")
        dialog.resize(760, 620)

        mode_label = QLabel("Prompt for this repository")
        mode_select = QComboBox()
        mode_select.addItem("Default review workflow", "default")
        mode_select.addItem("Resolve review and stop", "short")
        mode_select.addItem("Continue babysitting", "babysit")
        mode_select.addItem("Custom prompt", "custom")
        mode_select.setCurrentIndex(mode_select.findData(current_mode))

        prompt_editor = QTextEdit()
        prompt_editor.setAcceptRichText(False)
        prompt_editor.setAccessibleName("Delegation prompt text")
        placeholders = QLabel(
            "Custom placeholders: {pr_number}, {pr_title}, {repo}, "
            "{pr_url}, {threads}"
        )
        placeholders.setTextInteractionFlags(Qt.TextSelectableByMouse)
        placeholders.setWordWrap(True)

        editor_state = {"mode": "", "custom": custom_prompt}

        def update_editor() -> None:
            if editor_state["mode"] == "custom":
                editor_state["custom"] = prompt_editor.toPlainText()
            mode = mode_select.currentData()
            if mode == "default":
                prompt_editor.setPlainText(DEFAULT_DELEGATION_PROMPT)
            elif mode == "short":
                prompt_editor.setPlainText(SHORT_DELEGATION_PROMPT)
            elif mode == "babysit":
                prompt_editor.setPlainText(BABYSIT_DELEGATION_PROMPT)
            else:
                prompt_editor.setPlainText(editor_state["custom"])
            prompt_editor.setEnabled(mode == "custom")
            editor_state["mode"] = mode

        mode_select.currentIndexChanged.connect(lambda _index: update_editor())
        update_editor()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(mode_label)
        layout.addWidget(mode_select)
        layout.addWidget(prompt_editor, 1)
        layout.addWidget(placeholders)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return
        mode = mode_select.currentData()
        if not isinstance(mode, str):
            return
        custom = prompt_editor.toPlainText() if mode == "custom" else custom_prompt
        if mode == "custom" and not custom.strip():
            QMessageBox.warning(
                self,
                "Empty custom prompt",
                "Enter a custom prompt or choose a preset.",
            )
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        mode_target = self.delegation_prompt_mode_file(repo)
        mode_temporary = mode_target.with_suffix(".tmp")
        mode_temporary.write_text(mode + "\n")
        os.replace(mode_temporary, mode_target)
        if mode == "custom":
            template_target = self.delegation_prompt_template_file(repo)
            template_temporary = template_target.with_suffix(".tmp")
            template_temporary.write_text(custom.rstrip() + "\n")
            os.replace(template_temporary, template_target)
        self.show_transient_status("Delegation prompt saved")

    def load_agent_host(self, repo: str) -> None:
        host = ""
        if repo:
            try:
                host = self.agent_host_file(repo).read_text().strip()
            except OSError:
                pass
        self.agent_host_button.setEnabled(bool(repo))
        self.agent_host_button.setText(
            f"Agent host: {host}" if host else "Agent host: Local"
        )

    def configure_agent_host(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        try:
            current = self.agent_host_file(repo).read_text().strip()
        except OSError:
            current = ""
        choices = ["Local"] + ssh_config_hosts()
        initial = current or "Local"
        if initial not in choices:
            choices.append(initial)
        selected, accepted = QInputDialog.getItem(
            self,
            "Configure agent host",
            "SSH host running Codex or Claude tasks:",
            choices,
            choices.index(initial),
            True,
        )
        if not accepted:
            return
        host = selected.strip()
        if host.lower() == "local":
            host = ""
        if host and not re.fullmatch(r"[A-Za-z0-9_.@-]+", host):
            QMessageBox.warning(
                self,
                "Invalid SSH host",
                "Use an SSH config alias or a host name without spaces.",
            )
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.agent_host_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(host + "\n")
        os.replace(temporary, target)
        self.load_agent_host(repo)
        self.show_transient_status(
            f"Agent tasks will run on {host}" if host else "Agent tasks will run locally"
        )
        self.refresh()

    def load_auto_delegate(self, repo: str) -> None:
        self.auto_delegate.blockSignals(True)
        enabled = bool(repo)
        self.auto_delegate_label.setEnabled(enabled)
        self.auto_delegate.setEnabled(enabled)
        mode = "disabled"
        if repo:
            try:
                raw = self.auto_delegate_file(repo).read_text().strip()
            except OSError:
                raw = "disabled"
            if raw in {"0", "disabled", ""}:
                mode = "disabled"
            elif raw in {"1", "codex"}:
                mode = "codex"
            elif raw in {"auto", "both"}:
                mode = "auto"
            elif raw == "claude":
                mode = "claude"
            else:
                mode = "disabled"
        index = self.auto_delegate.findData(mode)
        self.auto_delegate.setCurrentIndex(index if index >= 0 else 0)
        self.auto_delegate.blockSignals(False)

    def auto_delegate_changed(self, _index: int = 0) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        mode = self.auto_delegate.currentData()
        if not isinstance(mode, str):
            mode = "disabled"
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.auto_delegate_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(f"{mode}\n")
        os.replace(temporary, target)
        labels = {
            "disabled": "Automatic delegation disabled",
            "auto": "Automatic delegation set to Auto (Codex + Claude)",
            "codex": "Automatic delegation set to Codex only",
            "claude": "Automatic delegation set to Claude only",
        }
        self.show_transient_status(labels.get(mode, "Automatic delegation updated"))

    def stop_when_empty_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-stop-when-empty"

    def load_stop_when_empty(self, repo: str) -> None:
        self.stop_when_empty.blockSignals(True)
        self.stop_when_empty.setEnabled(bool(repo))
        if repo:
            try:
                enabled = self.stop_when_empty_file(repo).read_text().strip() == "1"
            except OSError:
                enabled = False
            self.stop_when_empty.setChecked(enabled)
        else:
            self.stop_when_empty.setChecked(False)
        self.stop_when_empty.blockSignals(False)

    def stop_when_empty_changed(self, enabled: bool) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.stop_when_empty_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("1\n" if enabled else "0\n")
        os.replace(temporary, target)
        self.show_transient_status(
            "Monitor will stop when the queue becomes empty"
            if enabled
            else "Monitor will remain active while the queue is empty"
        )

    def ignore_drafts_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-ignore-drafts"

    def load_ignore_drafts(self, repo: str) -> None:
        self.ignore_drafts.blockSignals(True)
        self.ignore_drafts.setEnabled(bool(repo))
        if repo:
            try:
                enabled = self.ignore_drafts_file(repo).read_text().strip() != "0"
            except OSError:
                enabled = True
            self.ignore_drafts.setChecked(enabled)
        else:
            self.ignore_drafts.setChecked(True)
        self.ignore_drafts.blockSignals(False)

    def ignore_drafts_changed(self, enabled: bool) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.ignore_drafts_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("1\n" if enabled else "0\n")
        os.replace(temporary, target)
        self.show_transient_status(
            "Draft pull requests are ignored"
            if enabled
            else "Draft pull requests are included"
        )
        self.refresh(manual=True)

    def excluded_branches_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-excluded-branches"

    def load_excluded_branches(self, repo: str) -> None:
        self.excluded_branches.blockSignals(True)
        self.excluded_branches.setEnabled(bool(repo))
        text = ""
        if repo:
            try:
                lines = []
                for line in self.excluded_branches_file(repo).read_text().splitlines():
                    branch = line.strip()
                    if branch and not branch.startswith("#"):
                        lines.append(branch)
                text = ", ".join(lines)
            except OSError:
                text = ""
        self.excluded_branches.setText(text)
        self.excluded_branches.blockSignals(False)

    def excluded_branches_changed(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        branches = []
        for part in self.excluded_branches.text().replace("\n", ",").split(","):
            branch = part.strip()
            if branch and branch not in branches:
                branches.append(branch)
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.excluded_branches_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("".join(f"{branch}\n" for branch in branches))
        os.replace(temporary, target)
        self.excluded_branches.blockSignals(True)
        self.excluded_branches.setText(", ".join(branches))
        self.excluded_branches.blockSignals(False)
        if branches:
            self.show_transient_status(
                f"Excluded branches: {', '.join(branches)}"
            )
        else:
            self.show_transient_status("No branches excluded")
        self.refresh(manual=True)

    def excluded_authors_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-excluded-authors"

    def load_excluded_authors(self, repo: str) -> None:
        self.excluded_authors.setEnabled(bool(repo))
        authors = []
        if repo:
            try:
                authors = [
                    line.strip()
                    for line in self.excluded_authors_file(repo).read_text().splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
            except FileNotFoundError:
                authors = ["pull", "dependabot"]
        self.excluded_authors.setText(", ".join(authors))

    def excluded_authors_changed(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        authors = []
        for part in self.excluded_authors.text().replace("\n", ",").split(","):
            author = part.strip().lower()
            if not author or author.startswith("#"):
                continue
            author = author.removeprefix("@").removeprefix("app/").removesuffix("[bot]")
            if author and author not in authors:
                authors.append(author)
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.excluded_authors_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("".join(f"{author}\n" for author in authors))
        os.replace(temporary, target)
        self.excluded_authors.setText(", ".join(authors))
        self.show_transient_status(
            f"Excluded authors: {', '.join(authors)}" if authors else "No authors excluded"
        )
        self.refresh(manual=True)

    def notify_sound_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-notify-sound"

    def notify_sound_path_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-notify-sound-path"

    def notify_sound_volume_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-notify-sound-volume"

    def notify_sound_settings(self, repo: str) -> tuple[str, int]:
        try:
            path = self.notify_sound_path_file(repo).read_text().strip()
        except OSError:
            path = ""
        try:
            volume = int(self.notify_sound_volume_file(repo).read_text().strip())
        except (OSError, ValueError):
            volume = 30
        return path, max(0, min(volume, 100))

    def configure_notify_sound(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        current_path, current_volume = self.notify_sound_settings(repo)
        dialog = QDialog(self)
        dialog.setWindowTitle("Notification sound")

        sound_label = QLabel("Sound")
        sound_select = QComboBox()
        sound_select.addItem("Automatic", "")
        candidates = (
            ("Freedesktop complete", "/usr/share/sounds/freedesktop/stereo/complete.oga"),
            ("Ocean information", "/usr/share/sounds/ocean/stereo/dialog-information.oga"),
            ("Oxygen information", "/usr/share/sounds/oxygen/stereo/dialog-information.ogg"),
        )
        for label, path in candidates:
            if Path(path).is_file():
                sound_select.addItem(label, path)
        if current_path and sound_select.findData(current_path) < 0:
            sound_select.addItem(Path(current_path).name, current_path)
        current_index = sound_select.findData(current_path)
        sound_select.setCurrentIndex(current_index if current_index >= 0 else 0)

        choose_button = QPushButton(QIcon.fromTheme("document-open"), "Choose file…")

        def choose_sound() -> None:
            selected, _filter = QFileDialog.getOpenFileName(
                dialog,
                "Choose notification sound",
                str(Path(current_path).parent if current_path else Path.home()),
                "Audio files (*.oga *.ogg *.wav *.flac *.mp3);;All files (*)",
            )
            if not selected:
                return
            index = sound_select.findData(selected)
            if index < 0:
                sound_select.addItem(Path(selected).name, selected)
                index = sound_select.count() - 1
            sound_select.setCurrentIndex(index)

        choose_button.clicked.connect(choose_sound)
        sound_row = QHBoxLayout()
        sound_row.addWidget(sound_select, 1)
        sound_row.addWidget(choose_button)

        volume_label = QLabel(f"Volume: {current_volume}%")
        volume_slider = QSlider(Qt.Horizontal)
        volume_slider.setRange(0, 100)
        volume_slider.setValue(current_volume)
        volume_slider.setAccessibleName("Notification sound volume")
        volume_slider.valueChanged.connect(
            lambda value: volume_label.setText(f"Volume: {value}%")
        )
        preview_button = QPushButton(QIcon.fromTheme("media-playback-start"), "Preview")

        def preview_sound() -> None:
            path = sound_select.currentData()
            QProcess.startDetached(
                SCRIPT,
                [
                    "--repo",
                    repo,
                    "--preview-notify-sound",
                    path if isinstance(path, str) else "",
                    str(volume_slider.value()),
                ],
            )

        preview_button.clicked.connect(preview_sound)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(sound_label)
        layout.addLayout(sound_row)
        layout.addWidget(volume_label)
        layout.addWidget(volume_slider)
        layout.addWidget(preview_button)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return

        path = sound_select.currentData()
        if not isinstance(path, str):
            path = ""
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        values = (
            (self.notify_sound_path_file(repo), path + "\n"),
            (self.notify_sound_volume_file(repo), f"{volume_slider.value()}\n"),
        )
        for target, value in values:
            temporary = target.with_suffix(".tmp")
            temporary.write_text(value)
            os.replace(temporary, target)
        self.show_transient_status("Notification sound settings saved")

    def load_notify_sound(self, repo: str) -> None:
        self.notify_sound.blockSignals(True)
        self.notify_sound.setEnabled(bool(repo))
        if repo:
            try:
                enabled = self.notify_sound_file(repo).read_text().strip() != "0"
            except OSError:
                enabled = True
            self.notify_sound.setChecked(enabled)
        else:
            self.notify_sound.setChecked(True)
        self.notify_sound.blockSignals(False)

    def notify_sound_changed(self, enabled: bool) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.notify_sound_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("1\n" if enabled else "0\n")
        os.replace(temporary, target)
        self.show_transient_status(
            "Review-available sound enabled"
            if enabled
            else "Review-available sound disabled"
        )

    def new_items_at_top_file(self, repo: str) -> Path:
        return STATE_ROOT / f"{repo.replace('/', '__')}-new-items-at-top"

    def load_new_items_at_top(self, repo: str) -> None:
        self.new_items_at_top.blockSignals(True)
        self.new_items_at_top.setEnabled(bool(repo))
        if repo:
            try:
                enabled = self.new_items_at_top_file(repo).read_text().strip() != "0"
            except OSError:
                enabled = True
            self.new_items_at_top.setChecked(enabled)
        else:
            self.new_items_at_top.setChecked(True)
        self.new_items_at_top.blockSignals(False)

    def new_items_at_top_changed(self, enabled: bool) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        target = self.new_items_at_top_file(repo)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("1\n" if enabled else "0\n")
        os.replace(temporary, target)
        self.show_transient_status(
            "New queue items are added at the top"
            if enabled
            else "New queue items are added at the bottom"
        )
        self.refresh(manual=True)
