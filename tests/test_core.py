import contextlib
import io
import os
import unittest
from unittest.mock import MagicMock, patch

import requests

import hostship_renew as app

SERVER = "https://panel.host-ship.com/server/test-server"
PRIVATE = "private-server 203.0.113.42 private-token"


class StateTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(app, "SERVER_URL", SERVER))

    def test_server_url_validates_origin_and_exact_path(self):
        self.assertTrue(app.valid_server_url(SERVER))
        self.assertTrue(app.is_server_url(SERVER + "?tab=overview"))
        for url in (
            "https://panel.host-ship.com/login?next=/server/test-server",
            "https://panel.host-ship.com.evil.invalid/server/test-server",
            "https://user:pass@panel.host-ship.com/server/test-server",
            "https://panel.host-ship.com/server/",
            "http://panel.host-ship.com/server/test-server",
            "https://panel.host-ship.com/server/other-server",
        ):
            with self.subTest(url=url):
                self.assertFalse(app.is_server_url(url))

    def test_server_id_does_not_include_query_or_fragment(self):
        with patch.object(app, "SERVER_URL", SERVER + "?token=private#overview"):
            self.assertEqual(app.server_id(), "test-server")

    def test_multiline_countdown_is_normalized(self):
        self.assertEqual(app.renewal_text("RENEWAL IN\n14 Days"), "RENEWAL IN 14 Days")

    def test_limit_reached_alone_never_confirms_renewal(self):
        self.assertFalse(
            app.renewal_succeeded("Renewal in 14 Days", "Renewal in 14 Days", "Renew Limit Reached")
        )

    def test_increased_countdown_confirms_renewal(self):
        self.assertTrue(
            app.renewal_succeeded("Renewal in 4 Days", "Renewal in 14 Days", "Renew Limit Reached")
        )

    def test_old_success_notice_cannot_confirm_another_renewal(self):
        body = "Your server has been renewed successfully!"
        self.assertFalse(app.renewal_succeeded("未识别", "未识别", body, body))
        self.assertTrue(app.renewal_succeeded("未识别", "未识别", body, "No previous success"))

    def test_negative_success_phrases_are_not_success(self):
        self.assertFalse(
            app.renewal_succeeded(
                "未识别", "未识别", "The server could not be successfully renewed."
            )
        )

    def test_wrapped_negative_message_is_not_success(self):
        self.assertFalse(
            app.renewal_succeeded(
                "未识别", "未识别", "The server could not be\nsuccessfully renewed"
            )
        )


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(app, "log"))
        self.page = MagicMock()

    def test_temporary_socks_failure_retries_the_same_target(self):
        success = MagicMock(status=200)
        self.page.goto.side_effect = [
            app.PlaywrightError("Page.goto: net::ERR_SOCKS_CONNECTION_FAILED"),
            success,
        ]
        self.assertIs(app.navigate(self.page, SERVER), success)
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertTrue(all(call.args == (SERVER,) for call in self.page.goto.call_args_list))
        self.page.wait_for_timeout.assert_called_once_with(1000)

    def test_origin_error_is_retried(self):
        self.page.goto.side_effect = [MagicMock(status=522), MagicMock(status=200)]
        self.assertEqual(app.navigate(self.page, SERVER).status, 200)
        self.assertEqual(self.page.goto.call_count, 2)

    def test_retries_are_bounded(self):
        self.page.goto.side_effect = app.PlaywrightError("net::ERR_SOCKS_CONNECTION_FAILED")
        with self.assertRaises(app.PanelNavigationError):
            app.navigate(self.page, SERVER)
        self.assertEqual(self.page.goto.call_count, 3)

    def test_other_browser_errors_are_not_retried(self):
        self.page.goto.side_effect = app.PlaywrightError("Unrelated browser error")
        with self.assertRaises(app.PlaywrightError):
            app.navigate(self.page, SERVER)
        self.page.goto.assert_called_once()

    def test_security_rejection_is_not_retried(self):
        self.page.goto.return_value = MagicMock(status=403)
        self.assertEqual(app.navigate(self.page, SERVER).status, 403)
        self.page.goto.assert_called_once()

    def test_slow_reload_still_checks_the_fresh_renewal_result(self):
        clock = {"time": 0, "renewed": False}
        self.page.url = SERVER
        self.page.locator.return_value.inner_text.side_effect = lambda: (
            "Renewal in 14 Days" if clock["renewed"] else "Renewal in 4 Days"
        )

        def reload(**kwargs):
            clock.update(time=20, renewed=True)
            return MagicMock(status=200)

        self.page.reload.side_effect = reload
        self.page.wait_for_timeout.side_effect = lambda milliseconds: clock.update(
            time=clock["time"] + milliseconds / 1000
        )
        with (
            patch.object(app, "SERVER_URL", SERVER),
            patch.object(app.time, "monotonic", side_effect=lambda: clock["time"]),
        ):
            success, after = app.wait_for_renewal_result(
                self.page, "Renewal in 4 Days", "Renewal in 4 Days", timeout=8000
            )
        self.assertTrue(success)
        self.assertEqual(after, "Renewal in 14 Days")


def response(ok=True, status=200, description=""):
    result = MagicMock(status_code=status)
    result.json.return_value = {"ok": ok, "description": description}
    return result


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(
            patch.multiple(
                app,
                SERVER_URL=SERVER,
                TG_BOT_TOKEN="12345:test-token",
                TG_CHAT_ID="987654321",
                MANUAL_RUN=True,
                SEND_TG=True,
                IS_PROXY=False,
            )
        )
        self.post = self.enterContext(patch.object(app.requests, "post", return_value=response()))
        self.ip = self.enterContext(patch.object(app, "current_ip", return_value="203.0.113.42"))
        self.capture = self.enterContext(
            patch.object(app, "capture_screenshot", return_value=b"png-bytes")
        )
        self.page = object()

    def test_manual_check_sends_one_photo_with_caption_and_run_id(self):
        with patch.dict(
            os.environ,
            {
                "GITHUB_RUN_NUMBER": "12",
                "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_RUN_ID": "1234",
            },
        ):
            self.assertTrue(
                app.notify_result(
                    app.CheckResult("not_due", before="Renewal in 14 Days"), self.page
                )
            )
        self.post.assert_called_once()
        self.assertTrue(self.post.call_args.args[0].endswith("/sendPhoto"))
        caption = self.post.call_args.kwargs["data"]["caption"]
        self.assertIn("运行 #12 · 第 2 次尝试", caption)
        self.assertIn("actions/runs/1234", caption)
        self.assertNotIn("预计可续期", caption)
        self.assertNotIn("每天 08:00", caption)
        self.assertNotIn("203.0.113.42", self.output.getvalue())

    def test_scheduled_not_due_is_quiet(self):
        with patch.object(app, "MANUAL_RUN", False):
            self.assertIsNone(app.notify_result(app.CheckResult("not_due"), self.page))
        self.post.assert_not_called()
        self.capture.assert_not_called()
        self.ip.assert_not_called()

    def test_disabled_notification_does_not_capture_or_lookup_ip(self):
        with patch.object(app, "SEND_TG", False):
            self.assertIsNone(app.notify_result(app.CheckResult("renewed"), self.page))
        self.post.assert_not_called()
        self.capture.assert_not_called()
        self.ip.assert_not_called()

    def test_capture_failure_falls_back_to_text(self):
        self.capture.return_value = None
        self.assertTrue(
            app.notify_result(app.CheckResult("failed", reason="test failure"), self.page)
        )
        self.post.assert_called_once()
        self.assertTrue(self.post.call_args.args[0].endswith("/sendMessage"))

    def test_photo_rejection_falls_back_to_text_without_error_details(self):
        self.post.side_effect = [response(False, 400, PRIVATE), response()]
        self.assertTrue(app.notify_result(app.CheckResult("renewed"), self.page))
        self.assertEqual(self.post.call_count, 2)
        self.assertTrue(self.post.call_args.args[0].endswith("/sendMessage"))
        self.assertNotIn(PRIVATE, self.output.getvalue())

    def test_http_200_with_api_failure_is_not_success(self):
        self.post.return_value = response(False, 200, PRIVATE)
        self.assertFalse(app.notify_result(app.CheckResult("failed")))

    def test_request_exception_does_not_print_token(self):
        self.post.side_effect = requests.ConnectionError(
            "https://api.telegram.org/bot" + app.TG_BOT_TOKEN + "/sendMessage"
        )
        self.assertFalse(app.notify_result(app.CheckResult("failed")))
        self.assertNotIn(app.TG_BOT_TOKEN, self.output.getvalue())

    def test_caption_fits_telegram_limit_with_emoji(self):
        self.assertTrue(app.notify_result(app.CheckResult("failed", reason="🖼️" * 3000), self.page))
        caption = self.post.call_args.kwargs["data"]["caption"]
        self.assertLessEqual(len(caption.encode("utf-16-le")), 2048)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(
            patch.multiple(
                app,
                SERVER_URL=SERVER,
                HOSTSHIP_LOGIN="test@example.invalid",
                HOSTSHIP_PASSWORD="test-password",
            )
        )
        self.start = self.enterContext(patch.object(app, "sync_playwright"))
        self.runtime = self.start.return_value.start.return_value
        self.browser = self.runtime.chromium.launch.return_value
        self.page = self.browser.new_context.return_value.new_page.return_value
        self.check = self.enterContext(
            patch.object(app, "check_and_renew", return_value=app.CheckResult("renewed"))
        )
        self.notify = self.enterContext(patch.object(app, "notify_result", return_value=True))

    def test_success_sends_once_before_browser_closes(self):
        def notify(result, page):
            self.browser.close.assert_not_called()
            self.assertIs(page, self.page)
            return True

        self.notify.side_effect = notify
        self.assertEqual(app.main(), 0)
        self.notify.assert_called_once()
        self.browser.close.assert_called_once()
        self.runtime.stop.assert_called_once()

    def test_browser_startup_failure_is_notified_once_without_page(self):
        self.runtime.chromium.launch.side_effect = RuntimeError(PRIVATE)
        self.assertEqual(app.main(), 1)
        self.notify.assert_called_once()
        self.assertIsNone(self.notify.call_args.args[1])
        self.assertIn(PRIVATE, self.notify.call_args.args[0].reason)
        self.assertNotIn(PRIVATE, self.output.getvalue())
        self.runtime.stop.assert_called_once()

    def test_cleanup_error_does_not_send_a_second_notification(self):
        self.browser.close.side_effect = RuntimeError(PRIVATE)
        self.assertEqual(app.main(), 0)
        self.notify.assert_called_once()
        self.runtime.stop.assert_called_once()
        self.assertNotIn(PRIVATE, self.output.getvalue())

    def test_notification_failure_does_not_skip_cleanup(self):
        self.notify.side_effect = RuntimeError(PRIVATE)
        self.assertEqual(app.main(), 1)
        self.notify.assert_called_once()
        self.browser.close.assert_called_once()
        self.runtime.stop.assert_called_once()
        self.assertNotIn(PRIVATE, self.output.getvalue())

    def test_unknown_renewal_result_fails(self):
        self.check.return_value = app.CheckResult("uncertain")
        self.assertEqual(app.main(), 1)

    def test_invalid_configuration_does_not_start_browser(self):
        with patch.object(app, "SERVER_URL", "https://elsewhere.invalid/server/test-server"):
            self.assertEqual(app.main(), 1)
        self.start.assert_not_called()
        self.notify.assert_called_once()
