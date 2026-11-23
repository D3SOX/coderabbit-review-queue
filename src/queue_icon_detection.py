# SPDX-License-Identifier: GPL-3.0-or-later

"""Finding a repository's project icon, without Qt: the GUI and the web page
share the ranking and the cache under the state directory."""

import re
from pathlib import PurePosixPath

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


def initials(repo: str) -> str:
    name = repo.rsplit("/", 1)[-1]
    words = [word for word in re.split(r"[-_.\s]+", name) if word]
    if len(words) == 1:
        words = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", words[0]) or words
    return "".join(word[0] for word in words[:2]).upper() or "?"
