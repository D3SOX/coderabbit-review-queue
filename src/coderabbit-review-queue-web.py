#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A small web page to start, stop and reorder this device's monitors.

It listens on 127.0.0.1 only; expose it with `tailscale serve`. Every action
runs the queue's CLI, so monitors on other devices are controlled over SSH as
in the GUI.
"""

import calendar
import html
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

SCRIPT = str(Path(__file__).with_name("coderabbit-review-queue"))
STATE_ROOT = (
    Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    / "coderabbit-review-queue"
)
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# Files every configured repository has; OWNER__NAME-suffix.
REPO_FILE = re.compile(r"([A-Za-z0-9-]+)__([A-Za-z0-9_.-]+?)-(?:monitor\.log|order\.txt|wait-for-other-bots|new-items-at-top)")
WAITING_FOR_BOTS = "Waiting for bots"
DEFAULT_PORT = 8765


def run(repo: str, *args: str, timeout: int = 120) -> tuple[int, str]:
    try:
        result = subprocess.run(
            [SCRIPT, "--repo", repo, *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "The command timed out."
    return result.returncode, (result.stdout + result.stderr).strip()


def known_repos() -> list[str]:
    try:
        names = os.listdir(STATE_ROOT)
    except OSError:
        return []
    repos = {f"{m[1]}/{m[2]}" for m in map(REPO_FILE.fullmatch, names) if m}
    return sorted(repos, key=str.lower)


def monitor_running(repo: str) -> bool | None:
    """True or False, or None when the monitor's device did not answer."""
    code, output = run(repo, "--monitor-info", timeout=30)
    if code != 0:
        return None
    try:
        info = json.loads(output.splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return isinstance(info, dict) and isinstance(info.get("pid"), int)


def parse_status(status: str) -> dict:
    """The parts of --status the page shows, as the GUI reads them."""
    queue: list[dict] = []
    finished: list[dict] = []
    active: set[str] = set()
    opened: dict[str, int] = {}
    starts: list[int] = []
    expiry = None
    section = ""
    for line in status.splitlines():
        if line.startswith("PRs opened: "):
            for value in line.removeprefix("PRs opened: ").split():
                number, _, epoch = value.partition("=")
                if epoch.isdigit():
                    opened[number] = int(epoch)
        elif line == "Active reviews:":
            section = "active"
        elif line == "Queued PRs:":
            section = "queue"
        elif line.startswith("Finished CodeRabbit reviews:"):
            section = "finished"
        elif line.startswith("Next outstanding rate limit expires at "):
            expiry = line.removeprefix("Next outstanding rate limit expires at ").removesuffix(" UTC.")
        elif line.startswith("Estimated review starts: "):
            starts = [int(v) for v in line.split(": ", 1)[1].split() if v.isdigit()]
        elif match := re.fullmatch(r"  #(\d+) (.*)", line):
            number, title = match.groups()
            if section == "active":
                active.add(number)
                queue.append({"number": number, "title": title, "status": "In progress"})
            elif section == "queue":
                held = re.search(r" \(waiting for other bots: (.*)\)$", title)
                if title.endswith(" (in progress)") or number in active:
                    continue
                queue.append({
                    "number": number,
                    "title": title[: held.start()] if held else title,
                    "status": WAITING_FOR_BOTS if held else "Queued",
                    "reason": held.group(1) if held else "",
                })
            elif section == "finished":
                finished.append({"number": number, "title": title, "result": ""})
        elif section == "finished" and finished and line.startswith("    Result: "):
            finished[-1]["result"] = line.removeprefix("    Result: ")
    pending = iter(starts)
    for row in queue:
        row["opened"] = opened.get(row["number"])
        row["start"] = next(pending, None) if row["status"] == "Queued" else None
    return {"queue": queue, "finished": finished, "expiry": expiry}


def move(order: list[str], number: str, direction: str) -> list[str]:
    """ORDER with NUMBER moved up, down, to the top or to the bottom."""
    if number not in order:
        return order
    position = order.index(number)
    rest = order[:position] + order[position + 1:]
    target = {
        "top": 0,
        "up": max(position - 1, 0),
        "down": min(position + 1, len(rest)),
        "bottom": len(rest),
    }.get(direction, position)
    return rest[:target] + [number] + rest[target:]


def allowed_host(host: str) -> bool:
    """Refuse other names, so a page elsewhere cannot rebind its DNS to us."""
    name = urlsplit(f"//{host}").hostname or ""
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    name = name.lower().rstrip(".")
    short = socket.gethostname().lower().split(".")[0]
    return name in {"localhost", short} or name.endswith(".ts.net")


STYLE = """
:root {
  color-scheme: light dark;
  --bg: #ecebf4; --surface: #f7f6fb; --ink: #23213b; --muted: #6e6a8c; --line: #d3d0e3;
  --accent: #ff570a; --accent-ink: #fff; --run: #1f9d55; --held: #b7790f; --track: #c4c0da;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1b1a2a; --surface: #242338; --ink: #ecebf7; --muted: #9c98b8; --line: #34324c;
    --accent: #ff7a3d; --accent-ink: #1b1a2a; --run: #43c07a; --held: #e0a63a; --track: #3d3b58;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 17px/1.45 var(--sans);
  -webkit-text-size-adjust: 100%; }
main { max-width: 40rem; margin: 0 auto; padding: 1.25rem 1rem 4rem; }
a { color: inherit; text-underline-offset: .18em; text-decoration-thickness: 1px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.muted { color: var(--muted); }
.back { display: inline-block; color: var(--muted); text-decoration: none; margin-bottom: 1rem; }
.flash { display: flex; gap: .6rem; align-items: flex-start; margin: 0 0 1.25rem; padding: .75rem 1rem;
  border-radius: 12px; background: var(--surface); box-shadow: inset 4px 0 0 var(--accent); white-space: pre-wrap; }
.flash .i { color: var(--accent); margin-top: .15em; }
.sprite { position: absolute; width: 0; height: 0; overflow: hidden; }
.i { width: 1.05em; height: 1.05em; flex: none; vertical-align: -.17em; }
button .i { margin-right: .4rem; }
.note .i, .when small .i, .host .i, .next .label .i { width: .95em; height: .95em; margin-right: .3rem; }
.back .i { margin: 0 .15rem 0 -.3rem; }
.badge { display: inline-grid; place-items: center; flex: none; width: 2.4rem; height: 2.4rem; border-radius: 22%;
  background: hsl(var(--hue) 55% 41%); color: #fff; font-weight: 700; font-size: .95rem; letter-spacing: .01em; }
.heading { display: flex; align-items: center; gap: .8rem; }
.heading .badge { width: 3rem; height: 3rem; font-size: 1.15rem; }
h1 { font-size: 1.6rem; line-height: 1.15; letter-spacing: -.015em; margin: 0; font-weight: 700;
  overflow-wrap: anywhere; }
h1 .owner { display: block; font-size: 1rem; font-weight: 500; color: var(--muted); letter-spacing: 0; }
h2 { font-size: 1.05rem; margin: 2.25rem 0 .75rem; font-weight: 650; }
h2 .count { color: var(--muted); font-weight: 500; }
form { display: inline; margin: 0; }
button { font: inherit; font-size: .95rem; font-weight: 600; color: inherit; cursor: pointer;
  border: 1.5px solid var(--line); background: var(--surface); border-radius: 999px; padding: .5rem 1.05rem; }
button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
button:disabled { opacity: .3; cursor: default; }
.state { display: inline-flex; align-items: center; gap: .45rem; font-weight: 600; }
.state::before { content: ""; width: .7rem; height: .7rem; border-radius: 50%;
  box-shadow: inset 0 0 0 2px var(--muted); }
.state.on::before { background: var(--run); box-shadow: 0 0 0 4px color-mix(in srgb, var(--run) 22%, transparent); }
.state.unknown::before { box-shadow: inset 0 0 0 2px var(--held); }
.controls { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem 1rem; margin-top: 1rem; }
.controls .state { flex-basis: 100%; }

/* Home: the repositories and whether their monitors run. */
.site { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; margin-bottom: 1.5rem; }
.site h1 { font-size: 1.25rem; }
.repos { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--line); }
.repos li { display: flex; align-items: center; gap: .75rem; padding: .9rem 0; border-bottom: 1px solid var(--line); }
.repos .name { flex: 1; min-width: 0; text-decoration: none; font-weight: 650; overflow-wrap: anywhere; }
.repos .name .owner { display: block; font-weight: 500; font-size: .85rem; color: var(--muted); }
.repos .state { font-size: .9rem; font-weight: 500; color: var(--muted); }
.other { display: flex; gap: .6rem; margin-top: .75rem; }
.other input { flex: 1; min-width: 0; font: inherit; color: inherit; background: var(--surface);
  border: 1.5px solid var(--line); border-radius: 999px; padding: .5rem 1rem; }

/* Repository: the next window, then the queue as a timeline. */
.next { margin: 2rem 0 0; display: flex; align-items: baseline; gap: .75rem; flex-wrap: wrap; }
.next .time { font-size: 3.4rem; line-height: 1; font-weight: 700; letter-spacing: -.03em;
  font-variant-numeric: tabular-nums; }
.next .label { color: var(--muted); }
.queue { list-style: none; margin: 0; padding: 0; }
.stop { display: grid; grid-template-columns: 4.3rem 1.4rem 1fr; column-gap: .5rem; }
.stop .when { padding-top: .1rem; text-align: right; font-weight: 650; font-variant-numeric: tabular-nums;
  white-space: nowrap; }
.stop .when small { display: block; font-weight: 500; font-size: .8rem; color: var(--muted); }
.stop .rail { position: relative; }
.stop .rail::before { content: ""; position: absolute; left: 50%; top: 0; bottom: 0; width: 2px;
  margin-left: -1px; background: var(--track); }
.stop:first-child .rail::before { top: .55rem; }
.stop:last-child .rail::before { bottom: auto; height: .55rem; }
.stop .rail::after { content: ""; position: absolute; left: 50%; top: .35rem; width: .8rem; height: .8rem;
  margin-left: -.4rem; border-radius: 50%; background: var(--bg); box-shadow: inset 0 0 0 2.5px var(--ink); }
.stop.first .rail::after { background: var(--accent); box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 25%, transparent); }
.stop.held .rail::after { box-shadow: inset 0 0 0 2.5px var(--held); }
.stop.active .rail::after { background: var(--run); box-shadow: 0 0 0 4px color-mix(in srgb, var(--run) 25%, transparent); }
.stop .body { padding-bottom: 1.4rem; min-width: 0; }
.stop .title { overflow-wrap: anywhere; }
.stop .title a { font-weight: 650; text-decoration: none; }
.stop .note { font-size: .88rem; color: var(--muted); margin-top: .15rem; }
.stop.held .note .why { color: var(--held); }
.moves { display: inline-flex; margin-top: .55rem; border: 1.5px solid var(--line); border-radius: 10px;
  overflow: hidden; background: var(--surface); }
.moves form + form button { border-left: 1.5px solid var(--line); }
.moves button { border: 0; border-radius: 0; background: none; padding: .45rem .8rem; line-height: 1; display: flex; }
.moves button .i { margin: 0; width: 1.1rem; height: 1.1rem; }
.empty { color: var(--muted); }
.done { list-style: none; margin: 0; padding: 0; }
.done li { padding: .55rem 0; border-bottom: 1px solid var(--line); }
.done li:last-child { border-bottom: 0; }
.done .title { overflow-wrap: anywhere; }
.done .title a { font-weight: 650; text-decoration: none; }
.done .note { font-size: .88rem; color: var(--muted); }
.done .note.good { color: var(--run); } .done .note.open { color: var(--held); }
"""

SCRIPT_JS = """
const now = Date.now();
const rel = ms => {
  const m = Math.round(Math.abs(ms) / 60000);
  return m < 1 ? 'now' : m < 60 ? `${m} min` : m < 2880 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} days`;
};
for (const el of document.querySelectorAll('[data-epoch]')) {
  const date = new Date(el.dataset.epoch * 1000);
  const time = date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  const today = date.toDateString() === new Date().toDateString();
  el.title = date.toLocaleString();
  if (el.dataset.kind === 'ago') el.textContent = `${rel(now - date)} ago`;
  else if (el.dataset.kind === 'in') el.textContent = date <= now ? 'available now' : `in ${rel(date - now)}`;
  else if (el.dataset.kind === 'day') el.textContent = today ? '' : date.toLocaleDateString([], {weekday: 'short'});
  else el.textContent = date <= now ? 'now' : time;
}
"""


# Paths from Lucide (https://lucide.dev), ISC License, Copyright (c) Lucide Contributors.
ICONS = {
    "play": '<path d="M6 3l14 9-14 9z"/>',
    "stop": '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>',
    "back": '<path d="m15 18-6-6 6-6"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "top": '<path d="M5 3h14"/><path d="m18 13-6-6-6 6"/><path d="M12 7v14"/>',
    "up": '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
    "down": '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
    "bottom": '<path d="M12 17V3"/><path d="m6 11 6 6 6-6"/><path d="M19 21H5"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "hourglass": '<path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.17a2 2 0 0 0-.59-1.42L12 12l-4.41 4.41A2 2 0 0 0 7 17.83V22"/><path d="M7 2v4.17a2 2 0 0 0 .59 1.42L12 12l4.41-4.41A2 2 0 0 0 17 6.17V2"/>',
    "pr": '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7"/><path d="M6 9v12"/>',
    "check": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "comment": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "server": '<rect width="20" height="8" x="2" y="2" rx="2"/><rect width="20" height="8" x="2" y="14" rx="2"/><path d="M6 6h.01"/><path d="M6 18h.01"/>',
    "window": '<path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/>',
}
SPRITE = (
    '<svg class=sprite aria-hidden=true><defs>' + "".join(
        f'<symbol id="i-{name}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">{paths}</symbol>'
        for name, paths in ICONS.items()
    ) + "</defs></svg>"
)


def icon(name: str) -> str:
    return f'<svg class=i aria-hidden=true><use href="#i-{name}"/></svg>'


def initials(repo: str) -> str:
    """The GUI's fallback project icon letters."""
    name = repo.rsplit("/", 1)[-1]
    words = [word for word in re.split(r"[-_.\s]+", name) if word]
    if len(words) == 1:
        words = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", words[0]) or words
    return "".join(word[0] for word in words[:2]).upper() or "?"


def badge(repo: str) -> str:
    hue = zlib.crc32(repo.lower().encode()) % 360
    return f'<span class=badge style="--hue: {hue}" aria-hidden=true>{html.escape(initials(repo))}</span>'


def page(title: str, body: str, flash: str = "") -> bytes:
    flash_html = f'<p class="flash" role="status">{icon("info")}<span>{html.escape(flash)}</span></p>' if flash else ""
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        '<meta name=theme-color content="#ecebf4" media="(prefers-color-scheme: light)">'
        '<meta name=theme-color content="#1b1a2a" media="(prefers-color-scheme: dark)">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style>"
        f"<body>{SPRITE}<main>{flash_html}{body}</main><script>{SCRIPT_JS}</script></body></html>"
    ).encode()


def button(
    action: str, label: str, fields: dict | None = None, css: str = "", tip: str = "",
    disabled: bool = False, glyph: str = "",
) -> str:
    hidden = "".join(
        f'<input type=hidden name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in (fields or {}).items()
    )
    title = f' title="{html.escape(tip)}" aria-label="{html.escape(tip)}"' if tip else ""
    css_attr = f' class="{css}"' if css else ""
    return (
        f'<form method=post action="{html.escape(action)}">{hidden}'
        f'<button{css_attr}{title}{" disabled" if disabled else ""}>'
        f'{icon(glyph) if glyph else ""}{html.escape(label)}</button></form>'
    )


def state_label(running: bool | None) -> str:
    if running is None:
        return '<span class="state unknown">Unreachable</span>'
    return '<span class="state on">Running</span>' if running else '<span class="state">Stopped</span>'


def repo_name(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f'<span class=owner>{html.escape(owner)}/</span>{html.escape(name)}'


def home_page(flash: str) -> bytes:
    repos = known_repos()
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(monitor_running, repos))
    rows = "".join(
        f'<li>{badge(repo)}<a class=name href="/repo/{quote(repo)}" title="{html.escape(repo)}">{repo_name(repo)}</a>'
        f"{state_label(running)}"
        + (button(f"/repo/{quote(repo)}/stop", "Stop", {"back": "/"}, glyph="stop") if running else
           button(f"/repo/{quote(repo)}/start", "Start", {"back": "/"}, "primary", glyph="play")
           if running is False else "")
        + "</li>"
        for repo, running in zip(repos, states)
    )
    body = (
        f'<header class=site><h1>Review queue</h1><span class="muted host">{icon("server")}{html.escape(socket.gethostname())}</span></header>'
        + (f"<ul class=repos>{rows}</ul>" if rows else
           "<p class=empty>No repository has a queue on this device yet. Start one below.</p>")
        + '<h2>Start another repository</h2><form method=post action="/start" class=other>'
        '<input type=text name=repo placeholder="OWNER/NAME" aria-label="Repository" '
        'autocapitalize=off autocorrect=off spellcheck=false required>'
        f'<button class=primary>{icon("plus")}Start</button></form>'
    )
    return page("Review queue", body, flash)


def result_kind(result: str) -> str:
    return "good" if result == "Approved" else "open" if result.endswith(" unresolved") else ""


def result_icon(result: str) -> str:
    return {"good": "check", "open": "comment"}.get(result_kind(result), "info")


def repo_page(repo: str, flash: str) -> bytes:
    with ThreadPoolExecutor(max_workers=2) as pool:
        running_future = pool.submit(monitor_running, repo)
        code, status = run(repo, "--status")
        running = running_future.result()
    base = f"/repo/{quote(repo)}"
    controls = state_label(running)
    if running:
        controls += (button(f"{base}/check", "Check CodeRabbit now", glyph="refresh")
                     + button(f"{base}/stop", "Stop", glyph="stop"))
    elif running is False:
        controls += button(f"{base}/start", "Start monitor", css="primary", glyph="play")
    parts = [
        f'<a class=back href="/">{icon("back")}All repositories</a>',
        f"<div class=heading>{badge(repo)}<h1>{repo_name(repo)}</h1></div>",
        f"<div class=controls>{controls}</div>",
    ]
    if code != 0:
        parts.append(f"<p class=flash>{html.escape(status or 'Could not load the queue.')}</p>")
        return page(repo, "".join(parts), flash)
    data = parse_status(status)
    if data["expiry"] and (when := epoch(data["expiry"])):
        parts.append(
            f'<p class=next><span class=time data-epoch="{when}"></span>'
            f'<span class=label>{icon("window")}next review window, <span data-kind=in data-epoch="{when}"></span></span></p>'
        )
    queue = data["queue"]
    movable = [row["number"] for row in queue if row["status"] != "In progress"]
    order = " ".join(movable)
    parts.append(f"<h2>Queue <span class=count>{len(queue)}</span></h2>")
    if not queue:
        parts.append("<p class=empty>No PR is waiting for a CodeRabbit review.</p>")
    stops = []
    first_waiting = next((row["number"] for row in queue if row["status"] == "Queued"), None)
    for row in queue:
        number = row["number"]
        kind = {"In progress": "active", WAITING_FOR_BOTS: "held"}.get(row["status"], "")
        if number == first_waiting:
            kind = "first"
        if row["status"] == "In progress":
            when = f"now<small>{icon('pr')}reviewing</small>"
        elif row["status"] == WAITING_FOR_BOTS:
            when = f"<small>{icon('hourglass')}waiting</small>"
        elif row["start"]:
            when = (f'<span data-epoch="{row["start"]}"></span>'
                    f'<small data-kind=day data-epoch="{row["start"]}"></small>')
        else:
            when = "<small>queued</small>"
        notes = []
        if row.get("reason"):
            notes.append(f'<span class=why>{icon("hourglass")}{html.escape(row["reason"])}</span>')
        if row["opened"]:
            notes.append(f'{icon("clock")}Opened <span data-kind=ago data-epoch="{row["opened"]}"></span>')
        moves = ""
        if number in movable:
            moves = "<div class=moves>" + "".join(
                button(f"{base}/move", "", {"pr": number, "to": direction, "order": order}, tip=tip,
                       disabled=number == (movable[0] if direction in {"top", "up"} else movable[-1]),
                       glyph=direction)
                for direction, tip in (
                    ("top", f"Move #{number} to the top"), ("up", f"Move #{number} up"),
                    ("down", f"Move #{number} down"), ("bottom", f"Move #{number} to the bottom"),
                )
            ) + "</div>"
        link = f"https://github.com/{repo}/pull/{number}"
        stops.append(
            f'<li class="stop {kind}"><div class=when>{when}</div><div class=rail></div><div class=body>'
            f'<div class=title><a href="{html.escape(link)}">#{number}</a> {html.escape(row["title"])}</div>'
            + "".join(f"<div class=note>{note}</div>" for note in notes)
            + f"{moves}</div></li>"
        )
    if stops:
        parts.append(f"<ol class=queue>{''.join(stops)}</ol>")
    if data["finished"]:
        parts.append(f"<h2>Finished reviews <span class=count>{len(data['finished'])}</span></h2><ul class=done>")
        for row in data["finished"]:
            link = f"https://github.com/{repo}/pull/{row['number']}"
            parts.append(
                f'<li><div class=title><a href="{html.escape(link)}">#{row["number"]}</a> '
                f"{html.escape(row['title'])}</div>"
                + (f"<div class=\"note {result_kind(row['result'])}\">{icon(result_icon(row['result']))}"
                   f"{html.escape(row['result'])}</div>" if row["result"] else "")
                + "</li>"
            )
        parts.append("</ul>")
    return page(repo, "".join(parts), flash)


def epoch(utc: str) -> int:
    try:
        return calendar.timegm(time.strptime(utc, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return 0


class Handler(BaseHTTPRequestHandler):
    server_version = "coderabbit-review-queue"

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write(f"{self.address_string()} {format % args}\n")

    def send(self, status: int, body: bytes = b"", location: str = "") -> None:
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                         "form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def checked_repo(self, path: str) -> tuple[str, str] | None:
        match = re.fullmatch(r"/repo/([^/]+)/([^/]+)(?:/(\w+))?", path)
        if not match:
            return None
        repo = f"{match[1]}/{match[2]}"
        return (repo, match[3] or "") if REPO.fullmatch(repo) else None

    def do_GET(self) -> None:
        if not allowed_host(self.headers.get("Host", "")):
            self.send(421, b"Unknown host name")
            return
        url = urlsplit(self.path)
        flash = parse_qs(url.query).get("msg", [""])[0]
        if url.path == "/":
            self.send(200, home_page(flash))
            return
        target = self.checked_repo(url.path)
        if target and not target[1]:
            self.send(200, repo_page(target[0], flash))
            return
        self.send(404, page("Not found", "<p>Not found. <a href=/>Back</a></p>"))

    def do_POST(self) -> None:
        host = self.headers.get("Host", "")
        origin = urlsplit(self.headers.get("Origin", ""))
        # Forms from other sites carry their own Origin.
        if not allowed_host(host) or origin.netloc != host:
            self.send(403, b"Cross-site request refused")
            return
        length = min(int(self.headers.get("Content-Length") or 0), 65536)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        path = urlsplit(self.path).path
        if path == "/start":
            repo = form.get("repo", "").strip()
            if not REPO.fullmatch(repo):
                self.redirect("/", "Enter a repository as OWNER/NAME.")
                return
            target = (repo, "start")
        else:
            target = self.checked_repo(path)
        if not target or target[1] not in {"start", "stop", "check", "move"}:
            self.send(404, b"Not found")
            return
        repo, action = target
        # The home page's buttons return there; everything else to the repository.
        back = "/" if form.get("back") == "/" else f"/repo/{quote(repo)}"
        if action == "start":
            _code, output = run(repo, "--start-monitor")
        elif action == "stop":
            _code, output = run(repo, "--stop-monitor")
        elif action == "check":
            code, output = run(repo, "--check-quota-now")
            output = output or ("The monitor checks CodeRabbit now." if code == 0 else "Could not ask the monitor.")
        else:
            order = [n for n in form.get("order", "").split() if n.isdigit()]
            new_order = move(order, form.get("pr", ""), form.get("to", ""))
            code, output = run(repo, "--set-order", *new_order)
            output = output if code != 0 else ""
        self.redirect(back, output)

    def redirect(self, location: str, message: str = "") -> None:
        if message:
            location += "?" + urlencode({"msg": message[-2000:]})
        self.send(303, location=location)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Serving on http://127.0.0.1:{port}/", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
