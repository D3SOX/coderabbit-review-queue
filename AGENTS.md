# Agent notes

- After changing `coderabbit-review-queue` or `coderabbit-review-queue-gui.py`, always reinstall with `./install.sh`. The desktop launcher and `~/.local/bin/coderabbit-review-queue` run the copy under `~/.local/share/coderabbit-review-queue/`, not the git working tree. Skipping reinstall leaves the user on the old binary.
