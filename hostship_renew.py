#!/usr/bin/env python3
"""Check one Host-Ship server and notify once, without public account details."""

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_address
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SERVER_URL = os.getenv("SERVER_URL", "").strip()
HOSTSHIP_LOGIN = os.getenv("HOSTSHIP_LOGIN", "").strip()
# A password's leading/trailing spaces may be intentional.
HOSTSHIP_PASSWORD = os.getenv("HOSTSHIP_PASSWORD", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
IS_PROXY = os.getenv("IS_PROXY", "false").lower() == "true"
PROXY_SERVER = os.getenv("PROXY_SERVER", "socks5://127.0.0.1:1080").strip()
MANUAL_RUN = os.getenv("MANUAL_RUN", "false").lower() == "true"
SEND_TG = os.getenv("SEND_TG", "true").lower() == "true"
SCHEDULE_LABEL = os.getenv("SCHEDULE_LABEL", "以 Actions 定时配置为准")
BJ_TZ = ZoneInfo("Asia/Shanghai")

LOGIN_FIELDS = (
    (
        'input[name="email" i]',
        'input[type="email"]',
        'input[name="username" i]',
        'input[autocomplete="username"]',
    ),
    (
        'input[name="password" i]',
        'input[type="password"]',
        'input[autocomplete="current-password"]',
    ),
)
RENEW_NAME = re.compile(r"^Renew(?:\s+Now|\s+Server)?$", re.I)
CONFIRM_NAME = re.compile(r"^Renew\s+now$", re.I)
LIMIT_REACHED = re.compile(r"Renew\s+Limit\s+Reached", re.I)
SUCCESS_NOTICE = re.compile(
    r"^(?:(?:your|the)\s+)?(?:server\s+(?:has\s+been\s+)?)?"
    r"(?:renewed successfully|renewal successful|successfully renewed)[.!]?$",
    re.I,
)


NETWORK_ERRORS = (
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_TIMED_OUT",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_NETWORK_CHANGED",
)
RETRYABLE_HTTP = {500, 502, 503, 504, 520, 521, 522, 523, 524}


class PanelNavigationError(RuntimeError):
    """A read-only panel navigation failed after bounded retries."""


@dataclass(frozen=True)
class CheckResult:
    kind: str
    before: str = "未识别"
    after: str = "未识别"
    reason: str = ""

    @property
    def exit_code(self):
        return 0 if self.kind in ("renewed", "not_due") else 1


def log(message):
    # Only static states go here. The workflow additionally filters child output.
    print(message, flush=True)


def valid_server_url(url):
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "panel.host-ship.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and re.fullmatch(r"/server/[A-Za-z0-9_-]+/?", parsed.path) is not None
        )
    except ValueError:
        return False


def is_server_url(url):
    try:
        actual, expected = urlsplit(url), urlsplit(SERVER_URL)
        return (
            valid_server_url(url)
            and actual.hostname == expected.hostname
            and actual.path.rstrip("/") == expected.path.rstrip("/")
        )
    except ValueError:
        return False


def server_id():
    return (
        urlsplit(SERVER_URL).path.rstrip("/").rsplit("/", 1)[-1]
        if valid_server_url(SERVER_URL)
        else "未知"
    )


def redact(text):
    text = str(text)
    for value in (
        HOSTSHIP_LOGIN,
        HOSTSHIP_PASSWORD,
        TG_BOT_TOKEN,
        TG_CHAT_ID,
        SERVER_URL,
        os.getenv("NODE_LINK", ""),
    ):
        if value:
            text = text.replace(value, "[REDACTED]")
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)


def get_days(text):
    match = re.search(r"(?<![\d.])(\d+)\s*Days?\b", text or "", re.I)
    return int(match.group(1)) if match else None


def renewal_text(body):
    for pattern in (
        r"Renewal\s+in\s+\d+\s+Days?",
        r"Renew\s+in\s+\d+\s+Days?",
        r"\d+\s+Days?\s+until\s+renewal",
    ):
        match = re.search(pattern, body, re.I)
        if match:
            return " ".join(match.group(0).split())
    return "Renew Limit Reached" if LIMIT_REACHED.search(body) else "未识别"


def get_renewal_text(page):
    return renewal_text(page.locator("body").inner_text())


def first_visible(page, selectors):
    for selector in selectors:
        for item in page.locator(selector).all():
            if item.is_visible():
                return item
    return None


def login_fields(page):
    return tuple(first_visible(page, selectors) for selectors in LOGIN_FIELDS)


def first_action(groups, enabled=True):
    for group in groups:
        for item in group.all():
            if not item.is_visible():
                continue
            if enabled and (
                not item.is_enabled()
                or item.get_attribute("aria-disabled") == "true"
                or "disabled" in (item.get_attribute("class") or "").split()
            ):
                continue
            return item
    return None


def find_renew_button(page, enabled=True):
    return first_action(
        (page.get_by_role("button", name=RENEW_NAME), page.get_by_role("link", name=RENEW_NAME)),
        enabled=enabled,
    )


def server_page_ready(page):
    if not is_server_url(page.url) or all(login_fields(page)):
        return False
    return get_renewal_text(page) != "未识别" or find_renew_button(page, enabled=False) is not None


def security_challenge(page):
    text = page.locator("body").inner_text().lower()
    return (
        any(
            word in text
            for word in (
                "verify you are human",
                "checking your browser",
                "complete the security check",
            )
        )
        or first_visible(
            page,
            (
                'iframe[src*="challenges.cloudflare.com"]',
                ".g-recaptcha",
                ".cf-turnstile",
            ),
        )
        is not None
    )


def navigate(page, url=None, attempts=3):
    """Retry only GET/reload operations, never login or renewal submissions."""
    for attempt in range(1, attempts + 1):
        try:
            if url is None:
                response = page.reload(wait_until="domcontentloaded", timeout=60000)
            else:
                response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if response is None or response.status not in RETRYABLE_HTTP:
                return response
            error = PanelNavigationError(f"面板暂时不可用（HTTP {response.status}）。")
        except PlaywrightError as cause:
            if not isinstance(cause, PlaywrightTimeoutError) and not any(
                code in str(cause) for code in NETWORK_ERRORS
            ):
                raise
            error = PanelNavigationError(str(cause))
        if attempt == attempts:
            raise error
        log("⚠️ 面板连接暂时失败，正在重试")
        page.wait_for_timeout(attempt * 1000)


def login_if_needed(page, timeout=20000):
    navigate(page, SERVER_URL)
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        if server_page_ready(page):
            return True
        if urlsplit(page.url).hostname != "panel.host-ship.com" or security_challenge(page):
            log("⚠️ 登录页面需要人工检查")
            return False
        email, password = login_fields(page)
        if email is not None and password is not None:
            break
        page.wait_for_timeout(250)
    else:
        return False

    if not HOSTSHIP_LOGIN or not HOSTSHIP_PASSWORD:
        return False
    log("🔐 正在登录 Host-Ship")
    email.fill(HOSTSHIP_LOGIN)
    password.fill(HOSTSHIP_PASSWORD)
    submit = first_visible(
        page,
        (
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Login")',
            'button:has-text("Sign in")',
            'button:has-text("Log in")',
        ),
    )
    if submit is None:
        return False
    login_path = urlsplit(page.url).path
    submit.click()
    deadline = time.monotonic() + timeout / 1000
    opened_target = False
    while time.monotonic() < deadline:
        if server_page_ready(page):
            return True
        current = urlsplit(page.url)
        if current.hostname != "panel.host-ship.com" or security_challenge(page):
            return False
        # Wait for the login redirect before opening the server, so a pending
        # login request is not interrupted by a fixed-delay navigation.
        if not opened_target and current.path != login_path and not all(login_fields(page)):
            navigate(page, SERVER_URL)
            opened_target = True
            deadline = time.monotonic() + timeout / 1000
        page.wait_for_timeout(250)
    return False


def confirm_renewal(page, timeout=8000):
    title = (
        page.get_by_text(re.compile(r"\bConfirm\s+server\s+renewal\b", re.I))
        .filter(visible=True)
        .first
    )
    try:
        title.wait_for(state="visible", timeout=timeout)
    except Exception:
        return False

    button = first_action(
        (page.get_by_role("dialog").filter(has=title).get_by_role("button", name=CONFIRM_NAME),)
    )
    if button is None:
        # Some panels use an ordinary div instead of role=dialog. Search only
        # ancestors of the visible confirmation title, never the whole page.
        scope = title.locator("xpath=..")
        for _ in range(6):
            if not scope.count() or scope.evaluate("el => el.tagName") in ("BODY", "HTML"):
                break
            button = first_action((scope.get_by_role("button", name=CONFIRM_NAME),))
            if button is not None:
                break
            scope = scope.locator("xpath=..")
    if button is None:
        return False
    log("🔄 正在确认续期")
    # Never retry another candidate after click(): a timeout can occur after
    # the server has already accepted the request.
    button.click()
    return True


def renewal_succeeded(before, after, body_text, before_body=""):
    before_days, after_days = get_days(before), get_days(after)
    if before_days is not None and after_days is not None and after_days > before_days:
        return True

    def notices(body):
        lines = body.splitlines()
        found = set()
        for index, line in enumerate(lines):
            context = " ".join(lines[max(0, index - 1) : index + 1])
            if SUCCESS_NOTICE.fullmatch(line.strip()) and not re.search(
                r"\b(?:not|failed|failure|unable|error|cannot)\b", context, re.I
            ):
                found.add(line.strip().lower())
        return found

    old_notices = notices(before_body)
    new_notices = notices(body_text)
    return bool(new_notices - old_notices)


def wait_for_renewal_result(page, before, before_body, timeout=20000):
    deadline = time.monotonic() + timeout / 1000
    reload_at = time.monotonic() + 5
    reloaded = False
    after = before
    while True:
        if not is_server_url(page.url):
            return False, "服务器会话已失效"
        body = page.locator("body").inner_text()
        after = renewal_text(body)
        if renewal_succeeded(before, after, body, before_body):
            return True, after
        if time.monotonic() >= deadline:
            break
        if not reloaded and time.monotonic() >= reload_at:
            navigate(page)
            reloaded = True
        page.wait_for_timeout(500)
    return False, after


def private_page_details(page):
    try:
        return redact(page.locator("body").inner_text(timeout=3000))[:1200]
    except Exception:
        return "无法读取页面详情，请查看截图。"


def check_and_renew(page):
    if not login_if_needed(page):
        return CheckResult("failed", reason="无法进入服务器页面。\n" + private_page_details(page))
    log("✅ 登录成功")
    body = page.locator("body").inner_text()
    before = renewal_text(body)
    if LIMIT_REACHED.search(body):
        return CheckResult("not_due", before=before)
    button = find_renew_button(page)
    if button is None:
        if find_renew_button(page, enabled=False) is not None:
            return CheckResult("not_due", before=before)
        return CheckResult("failed", before=before, reason="未找到明确的续期按钮。")

    # The panel's enabled button determines eligibility. A positive countdown
    # does not mean renewal is forbidden: 4 -> 14 days is a valid renewal.
    log("🔄 已到续期窗口，正在打开确认弹窗")
    button.click()
    try:
        if not confirm_renewal(page):
            return CheckResult("failed", before=before, reason="未找到续期确认弹窗中的有效按钮。")
        success, after = wait_for_renewal_result(page, before, body)
    except Exception as error:
        return CheckResult("uncertain", before=before, reason=redact(error))
    if success:
        return CheckResult("renewed", before=before, after=after)
    return CheckResult(
        "uncertain", before=before, after=after, reason="已提交确认，但尚未观测到明确的续期结果。"
    )


def request_proxies():
    return {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None


def current_ip():
    try:
        response = requests.get("https://api.ipify.org", timeout=15, proxies=request_proxies())
        if response.ok:
            return str(ip_address(response.text.strip()))
    except (requests.RequestException, ValueError):
        pass
    return "获取失败"


def build_message(result, ip):
    titles = {
        "renewed": "🎉 Host-Ship 续期成功",
        "not_due": "⏳ Host-Ship 检查完成",
        "uncertain": "⚠️ Host-Ship 续期结果待确认",
        "failed": "❌ Host-Ship 检查或续期失败",
    }
    lines = [
        titles[result.kind],
        "",
        f"🖥️ 服务器：#{server_id()}",
        f"🌐 节点：{'已启用' if IS_PROXY else '直连'}",
        f"📍 出口 IP：{ip}",
        f"🕗 检查时间：{datetime.now(BJ_TZ):%Y/%m/%d %H:%M:%S}",
    ]
    if result.kind == "not_due":
        lines.extend(("", "🔒 面板当前不允许续期，本次未提交。", f"📅 面板倒计时：{result.before}"))
    elif result.kind in ("renewed", "uncertain"):
        lines.extend(("", f"📅 续期前：{result.before}", f"📅 续期后：{result.after}"))
    if result.reason:
        lines.extend(("", "⚠️ 详情：" + redact(result.reason)))
    lines.extend(("", "⏰ 自动检查：" + SCHEDULE_LABEL))
    number, attempt = os.getenv("GITHUB_RUN_NUMBER"), os.getenv("GITHUB_RUN_ATTEMPT", "1")
    repository, run_id = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_RUN_ID")
    if number:
        lines.append(f"🔎 运行 #{number} · 第 {attempt} 次尝试")
    if repository and run_id:
        lines.append(f"https://github.com/{repository}/actions/runs/{run_id}")
    return "\n".join(lines)


def telegram_text(text, limit):
    encoded = text.encode("utf-16-le")
    return (
        text
        if len(encoded) <= limit * 2
        else encoded[: (limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"
    )


def telegram_post(method, **kwargs):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}",
            timeout=25,
            proxies=request_proxies(),
            **kwargs,
        )
        try:
            result = response.json()
        except ValueError:
            result = {}
        if response.status_code == 200 and isinstance(result, dict) and result.get("ok") is True:
            return True
        log(f"⚠️ Telegram {method} 失败（HTTP {response.status_code}）")
    except requests.RequestException:
        log("⚠️ Telegram 请求异常")
    return False


def capture_screenshot(page):
    try:
        return page.screenshot(
            full_page=True, type="png", timeout=10000, mask=[page.locator("input, textarea")]
        )
    except Exception:
        log("⚠️ 页面截图失败")
        return None


def notify_result(result, page=None):
    if not SEND_TG or (result.kind == "not_due" and not MANUAL_RUN):
        log("ℹ️ 本次不发送 Telegram 通知")
        return None
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("ℹ️ Telegram 未配置，跳过通知")
        return None
    text = build_message(result, current_ip())
    photo = capture_screenshot(page) if page is not None else None
    if photo and telegram_post(
        "sendPhoto",
        data={"chat_id": TG_CHAT_ID, "caption": telegram_text(text, 1024)},
        files={"photo": ("hostship-status.png", photo, "image/png")},
    ):
        log("📸 Telegram 截图通知发送成功")
        return True
    if page is not None:
        log("ℹ️ 截图未发送成功，改发文字通知")
    sent = telegram_post(
        "sendMessage", json={"chat_id": TG_CHAT_ID, "text": telegram_text(text, 4096)}
    )
    log("📩 Telegram 文字通知发送成功" if sent else "❌ Telegram 通知发送失败")
    return sent


def main():
    log("Host-Ship 自动检查续期")
    runtime = browser = page = None
    result = CheckResult("failed", reason="初始化未完成。")
    notification = None
    try:
        if not valid_server_url(SERVER_URL):
            raise ValueError("SERVER_URL 不是有效的 Host-Ship 服务器详情页地址。")
        if not HOSTSHIP_LOGIN or not HOSTSHIP_PASSWORD:
            raise ValueError("未配置 Host-Ship 登录账号或密码。")
        runtime = sync_playwright().start()
        browser = runtime.chromium.launch(
            headless=True,
            proxy={"server": PROXY_SERVER} if IS_PROXY else None,
            args=["--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        result = check_and_renew(page)
    except Exception as error:
        if isinstance(error, PanelNavigationError):
            log("❌ 面板连接失败")
        result = CheckResult("failed", reason=redact(error))
    finally:
        log(
            {
                "renewed": "✅ 续期成功",
                "not_due": "⏳ 当前不可续期，本次未提交",
                "uncertain": "⚠️ 已提交续期，结果待确认",
                "failed": "❌ 检查或续期失败",
            }[result.kind]
        )
        # One notification decision for every path, while the browser is open.
        try:
            notification = notify_result(result, page)
        except Exception:
            log("❌ Telegram 通知发送失败")
            notification = False
        for resource, method in ((browser, "close"), (runtime, "stop")):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    log("⚠️ 浏览器资源清理未完成")
    return 1 if notification is False else result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
