#!/usr/bin/env python3
"""Only publish known status lines from processes that have access to secrets."""

import argparse
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

PUBLIC_STATUSES = frozenset(
    {
        "Host-Ship 自动检查续期",
        "⚠️ 登录页面需要人工检查",
        "🔐 正在登录 Host-Ship",
        "🔄 正在确认续期",
        "✅ 登录成功",
        "🔄 已到续期窗口，正在打开确认弹窗",
        "⚠️ Telegram 请求异常",
        "⚠️ 页面截图失败",
        "ℹ️ 本次不发送 Telegram 通知",
        "ℹ️ Telegram 未配置，跳过通知",
        "📸 Telegram 截图通知发送成功",
        "ℹ️ 截图未发送成功，改发文字通知",
        "📩 Telegram 文字通知发送成功",
        "❌ Telegram 通知发送失败",
        "✅ 续期成功",
        "⏳ 当前不可续期，本次未提交",
        "⚠️ 已提交续期，结果待确认",
        "❌ 检查或续期失败",
        "⚠️ 浏览器资源清理未完成",
    }
)
PRIVATE_COMMAND_FILES = (
    "GITHUB_ENV",
    "GITHUB_OUTPUT",
    "GITHUB_STATE",
    "GITHUB_STEP_SUMMARY",
    "GITHUB_PATH",
)


def public_status(line):
    line = line.strip()
    if line in PUBLIC_STATUSES or re.fullmatch(
        r"⚠️ Telegram (sendPhoto|sendMessage) 失败（HTTP [1-5][0-9]{2}）", line
    ):
        return line
    return None


def export_proxy_environment(source, destination):
    """Do not expose arbitrary environment values written by the proxy installer."""
    values = {}
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        name, separator, value = line.partition("=")
        if separator and name in ("IS_PROXY", "PROXY_SERVER"):
            values[name] = value
    proxy = urlsplit(values.get("PROXY_SERVER", ""))
    if (
        values.get("IS_PROXY") != "true"
        or proxy.scheme not in ("http", "https", "socks4", "socks5")
        or proxy.hostname not in ("127.0.0.1", "localhost", "::1")
        or proxy.username is not None
        or proxy.password is not None
        or proxy.path not in ("", "/")
        or proxy.query
        or proxy.fragment
        or proxy.port is None
        or not 1 <= proxy.port <= 65535
        or not destination
    ):
        raise ValueError("Invalid local proxy configuration")
    hostname = f"[{proxy.hostname}]" if ":" in proxy.hostname else proxy.hostname
    # The only exported URL consists of a local address and port, without credentials.
    safe_proxy = f"{proxy.scheme}://{hostname}:{proxy.port}"
    with open(destination, "a", encoding="utf-8") as output:
        output.write(f"IS_PROXY=true\nPROXY_SERVER={safe_proxy}\n")


def run_private(command, proxy_setup=False):
    with tempfile.TemporaryDirectory(prefix="hostship-private-") as folder:
        environment = os.environ.copy()
        public_environment = environment.get("GITHUB_ENV")
        for name in PRIVATE_COMMAND_FILES:
            path = Path(folder, name)
            path.touch(mode=0o600)
            environment[name] = str(path)
        try:
            # An unnamed temporary file also avoids waiting for pipe EOF from
            # a background proxy daemon that inherits the installer's stdout.
            with tempfile.TemporaryFile() as output:
                process = subprocess.run(
                    command,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env=environment,
                )
                output.seek(0)
                for line in output:
                    status = public_status(line.decode("utf-8", errors="replace"))
                    if status is not None:
                        print(status, flush=True)
                code = process.returncode
        except OSError:
            print("❌ 无法启动执行进程", flush=True)
            return 1

        if code:
            print("❌ 执行失败，原始输出未写入公开日志", flush=True)
            return code if code > 0 else 1
        if proxy_setup:
            try:
                export_proxy_environment(Path(folder, "GITHUB_ENV"), public_environment)
            except (OSError, ValueError):
                print("❌ 未获得有效的本地代理配置", flush=True)
                return 1
            print("✅ 代理初始化成功", flush=True)
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-setup", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("a command is required")
    raise SystemExit(run_private(args.command, args.proxy_setup))
