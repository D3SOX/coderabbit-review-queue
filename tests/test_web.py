import http.client
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
WEB_PATH = Path(__file__).resolve().parents[1] / "src/coderabbit-review-queue-web.py"
SPEC = importlib.util.spec_from_file_location("queue_web", WEB_PATH)
assert SPEC and SPEC.loader
web = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(web)

STATUS = """Repository: me/repo
PRs opened: 7=1795000000 8=1795003600 9=1795007200
Active reviews:
  #7 Reviewing now
Queued PRs:
  #7 Reviewing now (in progress)
  #9 Held one (waiting for other bots: Codex still reviewing)
  #8 Waiting one
Next outstanding rate limit expires at 2026-11-23T10:35:04Z UTC.
Estimated review starts: 1795010000

Finished CodeRabbit reviews:
  #5 Done one
    Result: Approved
"""


class WebTests(unittest.TestCase):
    def test_status_lists_the_queue_in_order_with_estimates_for_waiting_rows(self):
        data = web.parse_status(STATUS)
        self.assertEqual(
            [(row["number"], row["status"], row["start"]) for row in data["queue"]],
            [("7", "In progress", None), ("9", "Waiting for bots", None), ("8", "Queued", 1795010000)],
        )
        self.assertEqual(data["queue"][1]["reason"], "Codex still reviewing")
        self.assertEqual(data["queue"][2]["opened"], 1795003600)
        self.assertEqual(data["finished"], [{"number": "5", "title": "Done one", "result": "Approved"}])
        self.assertEqual(web.epoch(data["expiry"]), 1795430104)

    def test_moves(self):
        order = ["1", "2", "3"]
        self.assertEqual(web.move(order, "3", "top"), ["3", "1", "2"])
        self.assertEqual(web.move(order, "2", "up"), ["2", "1", "3"])
        self.assertEqual(web.move(order, "1", "up"), order)
        self.assertEqual(web.move(order, "2", "down"), ["1", "3", "2"])
        self.assertEqual(web.move(order, "1", "bottom"), ["2", "3", "1"])
        self.assertEqual(web.move(order, "4", "top"), order)

    def test_only_tailnet_and_local_host_names_are_served(self):
        with patch.object(web.socket, "gethostname", return_value="HomeLAN"):
            for host in ("homelan.tail20f5d.ts.net:8765", "homelan:8765", "100.104.162.31:8765",
                         "[fd7a:115c::1]:8765", "localhost:8765", "127.0.0.1"):
                with self.subTest(host=host):
                    self.assertTrue(web.allowed_host(host))
            for host in ("evil.example:8765", "", "homelan.evil.example"):
                with self.subTest(host=host):
                    self.assertFalse(web.allowed_host(host))

    def test_server_runs_actions_through_the_cli_and_refuses_cross_site_posts(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        calls = Path(directory) / "calls"
        script = Path(directory) / "coderabbit-review-queue"
        script.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$*" >>{calls}\n'
            'case "$*" in\n'
            '  *--monitor-info*) echo \'{"pid": 42}\' ;;\n'
            f"  *--status*) cat {Path(directory) / 'status'} ;;\n"
            "  *--start-monitor*) echo 'Started the monitor.' ;;\n"
            "esac\n"
        )
        script.chmod(0o755)
        (Path(directory) / "status").write_text(STATUS)
        state = Path(directory) / "state"
        state.mkdir()
        (state / "me__repo-order.txt").write_text("8\n")
        (state / "my-org__web.site-monitor.log").write_text("")
        server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        def request(method, path, form=None, origin=True, host=f"localhost:{port}"):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"Host": host}
            body = None
            if form is not None:
                body = urlencode(form)
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                if origin:
                    headers["Origin"] = f"http://{host}"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheader("Location"), response.read().decode()

        with patch.object(web, "SCRIPT", str(script)), patch.object(web, "STATE_ROOT", state), \
                patch.object(web.PROJECT_ICONS, "ensure"):
            status, _, body = request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("me/repo", body)
            self.assertIn("my-org/web.site", body)
            self.assertIn("Running", body)
            status, _, body = request("GET", "/repo/me/repo")
            self.assertEqual(status, 200)
            self.assertIn("Held one", body)
            self.assertIn("Codex still reviewing", body)
            self.assertEqual(request("GET", "/", host="evil.example")[0], 421)

            calls.write_text("")
            status, location, _ = request("POST", "/repo/me/repo/move",
                                          {"pr": "8", "to": "top", "order": "9 8"})
            self.assertEqual((status, location), (303, "/repo/me/repo"))
            # The move shows at once, without asking for the whole status again.
            body = request("GET", "/repo/me/repo")[2]
            self.assertLess(body.index("Waiting one"), body.index("Held one"))
            loaded = web.STATUS.loaded_at("me/repo")
            self.assertEqual(request("GET", f"/repo/me/repo/updated?since={loaded}")[0], 204)
            self.assertEqual(request("GET", f"/repo/me/repo/updated?since={loaded - 1}")[0], 200)
            status, location, _ = request("POST", "/start", {"repo": "other/repo"})
            self.assertEqual(status, 303)
            self.assertEqual(location, "/repo/other/repo?msg=Started+the+monitor.")
            self.assertEqual(request("POST", "/repo/me/repo/stop", {}, origin=False)[0], 403)
            self.assertEqual(request("POST", "/start", {"repo": "bad repo"})[1],
                             "/?msg=Enter+a+repository+as+OWNER%2FNAME.")
            self.assertEqual([call for call in calls.read_text().splitlines()
                              if not call.endswith(("--status", "--monitor-info"))], [
                "--repo me/repo --set-order 8 9",
                "--repo other/repo --start-monitor",
            ])

    def test_project_icons_are_found_like_the_gui_finds_them_and_served(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        fake_gh = directory / "gh"
        fake_gh.write_text(
            "#!/bin/bash\n"
            'case "$*" in\n'
            "  *git/trees*) echo '[\"docs/logo.png\", \"assets/icon.svg\", \"tests/icon.svg\"]' ;;\n"
            "  *contents/assets/icon.svg*) echo 'not an image' ;;\n"
            "  *contents/docs/logo.png*) printf '\\x89PNG\\r\\n\\x1a\\nrest' ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        fake_gh.chmod(0o755)
        state = directory / "state"
        (state / "repo-icons").mkdir(parents=True)
        # A choice made in the GUI stays.
        (state / "repo-icons/me__chosen.json").write_text(
            '{"choice": "initials", "checked": 1, "candidates": []}')
        with patch.object(web, "STATE_ROOT", state), patch.object(web, "GH", str(fake_gh)):
            web.PROJECT_ICONS.find("me/repo")
            data = web.icon_settings("me/repo")
            self.assertEqual(data["candidates"], ["assets/icon.svg", "docs/logo.png"])
            # The SVG was not an image, so the next candidate is used.
            self.assertEqual(data["path"], "docs/logo.png")
            self.assertTrue(web.icon_file("me/repo").read_bytes().startswith(b"\x89PNG"))
            self.assertIn('<img class=badge src="/icon/me/repo?v=', web.badge("me/repo"))
            finder = web.IconFinder()
            finder.ensure(["me/chosen"])
            self.assertEqual(finder.pending, [])
            self.assertIn("<span class=badge", web.badge("me/chosen"))

    def test_a_saved_move_reorders_the_kept_status(self):
        reordered = web.reorder_status(STATUS, ["8", "9"])
        self.assertIn("  #7 Reviewing now (in progress)\n  #8 Waiting one\n"
                      "  #9 Held one (waiting for other bots: Codex still reviewing)\n", reordered)
        self.assertEqual(web.reorder_status("Queued PRs:\n", ["1"]), "Queued PRs:\n")


if __name__ == "__main__":
    unittest.main()
