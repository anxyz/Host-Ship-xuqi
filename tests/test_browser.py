import json
import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

import hostship_renew as app

SERVER = "https://panel.host-ship.com/server/test-server"
LOGIN = "https://panel.host-ship.com/login?next=/server/test-server"
PANEL = """
<!doctype html><title>Server</title>
<p>The server password is managed in Settings.</p>
<div id="timer">RENEWAL IN 4 Days</div>
<button disabled>Renew</button>
<button id="open" onclick="document.querySelector('#modal').hidden=false">Renew</button>
<button onclick="window.wrong=(window.wrong||0)+1">Auto Renew</button>
<div id="modal" role="dialog" hidden>
  <h2>Confirm server renewal</h2>
  <button id="confirm" onclick="window.submitted=(window.submitted||0)+1;
      document.querySelector('#timer').textContent='RENEWAL IN 14 Days';
      document.querySelector('#modal').hidden=true;">Renew now</button>
</div>
"""
LOGIN_PAGE = """
<!doctype html><title>Sign in</title><form onsubmit="event.preventDefault();
  fetch('/session', {method:'POST',body:JSON.stringify({
    email:document.querySelector('[name=email]').value,
    password:document.querySelector('[name=password]').value
  })}).then(r=>r.json()).then(r=>{if(r.ok) location.href='/server/test-server';});">
  <input name="email" type="email"><input name="password" type="password">
  <button type="submit">Login</button>
</form><footer>Protected by Cloudflare</footer>
"""


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(1500)
        self.enterContext(
            patch.multiple(
                app,
                SERVER_URL=SERVER,
                HOSTSHIP_LOGIN="test@example.invalid",
                HOSTSHIP_PASSWORD="  test-password  ",
            )
        )
        self.enterContext(patch.object(app, "log"))

    def panel(self, html=PANEL):
        self.page.route(
            "**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
        )
        self.page.goto(SERVER)

    def login_routes(self, accept=True, client_redirect=False):
        state = {"logged_in": False, "credentials": None}

        def handle(route):
            if route.request.url.endswith("/session"):
                state["credentials"] = json.loads(route.request.post_data)
                state["logged_in"] = accept
                route.fulfill(content_type="application/json", body=json.dumps({"ok": accept}))
            elif route.request.url == SERVER:
                if state["logged_in"]:
                    route.fulfill(content_type="text/html", body=PANEL)
                elif client_redirect:
                    route.fulfill(
                        content_type="text/html",
                        body=(
                            "<script>setTimeout(() => location.href="
                            + json.dumps(LOGIN)
                            + ", 10)</script><body>Loading</body>"
                        ),
                    )
                else:
                    route.fulfill(
                        content_type="text/html",
                        body=(
                            '<script>history.replaceState(null,"",'
                            + json.dumps(LOGIN)
                            + ")</script>"
                            + LOGIN_PAGE
                        ),
                    )
            else:
                route.fulfill(content_type="text/html", body=LOGIN_PAGE)

        self.page.route("**/*", handle)
        return state

    def test_login_waits_for_redirect_and_preserves_password_spaces(self):
        state = self.login_routes()
        self.assertTrue(app.login_if_needed(self.page, timeout=2000))
        self.assertEqual(state["credentials"]["password"], "  test-password  ")
        self.assertEqual(self.page.url, SERVER)

    def test_server_path_in_login_query_does_not_count_as_logged_in(self):
        self.login_routes(accept=False)
        self.assertFalse(app.login_if_needed(self.page, timeout=400))
        self.assertEqual(self.page.url, LOGIN)

    def test_waits_for_client_side_login_redirect(self):
        self.login_routes(client_redirect=True)
        self.assertTrue(app.login_if_needed(self.page, timeout=2000))

    def test_server_description_containing_password_does_not_trigger_login(self):
        self.panel()
        self.assertTrue(app.login_if_needed(self.page, timeout=500))

    def test_disabled_candidate_does_not_hide_enabled_renew_button(self):
        self.panel()
        button = app.find_renew_button(self.page)
        self.assertEqual(button.get_attribute("id"), "open")

    def test_unrelated_renew_controls_are_not_selected(self):
        self.panel(
            "<button>Auto Renew</button><button>Renew Limit Reached</button><a>Renew History</a>"
        )
        self.assertIsNone(app.find_renew_button(self.page))

    def test_confirmation_is_scoped_to_dialog(self):
        self.panel(
            PANEL + '<button id="background" onclick="window.wrongClicks=1">Renew now</button>'
        )
        self.page.locator("#open").click()
        self.assertTrue(app.confirm_renewal(self.page))
        self.assertEqual(self.page.evaluate("window.submitted"), 1)
        self.assertIsNone(self.page.evaluate("window.wrongClicks"))

    def test_confirmation_supports_dialog_without_role(self):
        self.panel(
            PANEL.replace('role="dialog"', 'class="modal"').replace(
                "Confirm server renewal</h2>", "Confirm server renewal?</h2>"
            )
        )
        self.page.locator("#open").click()
        self.assertTrue(app.confirm_renewal(self.page))
        self.assertEqual(self.page.evaluate("window.submitted"), 1)

    def test_confirmation_never_falls_back_to_background_button(self):
        self.panel("<h2>Confirm server renewal</h2><div><button>Renew now</button></div>")
        self.assertFalse(app.confirm_renewal(self.page, timeout=500))

    def test_failed_confirmation_click_is_not_retried(self):
        self.panel()
        self.page.locator("#open").click()
        button = MagicMock()
        button.click.side_effect = RuntimeError("response lost after submission")
        with patch.object(app, "first_action", return_value=button) as choose:
            with self.assertRaises(RuntimeError):
                app.confirm_renewal(self.page)
        button.click.assert_called_once()
        choose.assert_called_once()

    def test_real_browser_renews_from_four_to_fourteen_days(self):
        self.panel()
        result = app.check_and_renew(self.page)
        self.assertEqual(result.kind, "renewed")
        self.assertEqual(app.get_days(result.before), 4)
        self.assertEqual(app.get_days(result.after), 14)
        self.assertEqual(self.page.evaluate("window.submitted"), 1)

    def test_not_due_does_not_submit_a_request(self):
        self.panel("<p>RENEWAL IN 14 Days</p><button disabled>Renew Limit Reached</button>")
        result = app.check_and_renew(self.page)
        self.assertEqual(result.kind, "not_due")
        self.assertEqual(result.exit_code, 0)

    def test_redirect_after_submission_cannot_report_success(self):
        self.panel()
        self.page.goto(LOGIN)
        success, _ = app.wait_for_renewal_result(self.page, "Renewal in 4 Days", "", timeout=500)
        self.assertFalse(success)

    def test_screenshot_is_returned_as_png_without_a_file(self):
        self.panel('<input value="test-secret"><h1>Fixture</h1>')
        screenshot = app.capture_screenshot(self.page)
        self.assertTrue(screenshot.startswith(b"\x89PNG\r\n\x1a\n"))
