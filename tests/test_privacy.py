import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.private_run import PRIVATE_COMMAND_FILES

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "private_run.py"
PRIVATE_TEXT = "private-server-identifier 203.0.113.42 unknown-session-token"


class ProcessPrivacyTests(unittest.TestCase):
    def invoke(self, script, proxy=False, environment=None):
        command = [sys.executable, str(RUNNER)]
        if proxy:
            command.append("--proxy-setup")
        command.extend([sys.executable, "-c", script])
        env = os.environ.copy()
        env.update(environment or {})
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)

    def command_files(self, folder):
        environment = {}
        for name in PRIVATE_COMMAND_FILES:
            path = Path(folder, name)
            path.write_text("", encoding="utf-8")
            environment[name] = str(path)
        return environment

    def test_only_exact_statuses_are_published_from_stdout_and_stderr(self):
        script = f"""
import sys
print('✅ 续期成功', flush=True)
print({PRIVATE_TEXT!r}, flush=True)
print('✅ 续期成功 ' + {PRIVATE_TEXT!r}, flush=True)
print('::notice::' + {PRIVATE_TEXT!r}, file=sys.stderr, flush=True)
print('dW5rbm93bi1lbmNvZGVkLXNlY3JldA==', file=sys.stderr, flush=True)
print('⚠️ Telegram sendPhoto 失败（HTTP 401）', flush=True)
print('⚠️ Telegram sendPhoto 失败（HTTP 401）: ' + {PRIVATE_TEXT!r}, flush=True)
sys.stderr.buffer.write(b'\\xffunknown-byte-secret\\n')
"""
        result = self.invoke(script)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "✅ 续期成功",
                "⚠️ Telegram sendPhoto 失败（HTTP 401）",
            ],
        )
        self.assertEqual(result.stderr, "")

    def test_child_failure_remains_failure_without_traceback_leak(self):
        result = self.invoke(f"import sys; print({PRIVATE_TEXT!r}, file=sys.stderr); sys.exit(7)")
        self.assertEqual(result.returncode, 7)
        self.assertNotIn(PRIVATE_TEXT, result.stdout + result.stderr)
        self.assertIn("执行失败", result.stdout)

    def test_unhandled_exception_is_not_published(self):
        result = self.invoke(f"raise RuntimeError({PRIVATE_TEXT!r})")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(PRIVATE_TEXT, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_proxy_only_exports_a_local_address_and_enabled_flag(self):
        script = f"""
import os
from pathlib import Path
for name in {PRIVATE_COMMAND_FILES!r}:
    Path(os.environ[name]).write_text({PRIVATE_TEXT!r})
Path(os.environ['GITHUB_ENV']).write_text(
    'IS_PROXY=true\\nPROXY_SERVER=socks5://127.0.0.1:1080\\n'
    + 'UNTRUSTED_VALUE=' + {PRIVATE_TEXT!r} + '\\n')
print({PRIVATE_TEXT!r})
"""
        with tempfile.TemporaryDirectory() as folder:
            environment = self.command_files(folder)
            result = self.invoke(script, proxy=True, environment=environment)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                Path(environment["GITHUB_ENV"]).read_text(),
                "IS_PROXY=true\nPROXY_SERVER=socks5://127.0.0.1:1080\n",
            )
            for name in PRIVATE_COMMAND_FILES:
                if name != "GITHUB_ENV":
                    self.assertEqual(Path(environment[name]).read_text(), "")
        self.assertNotIn(PRIVATE_TEXT, result.stdout + result.stderr)

    def test_proxy_rejects_external_addresses_credentials_and_url_data(self):
        for proxy in (
            "socks5://203.0.113.42:1080",
            "socks5://user:private-password@127.0.0.1:1080",
            "http://127.0.0.1:1081/?token=private-token",
        ):
            with self.subTest(proxy=proxy), tempfile.TemporaryDirectory() as folder:
                environment = self.command_files(folder)
                content = "IS_PROXY=true\nPROXY_SERVER=" + proxy + "\n"
                script = f"import os; from pathlib import Path; Path(os.environ['GITHUB_ENV']).write_text({content!r})"
                result = self.invoke(script, proxy=True, environment=environment)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(Path(environment["GITHUB_ENV"]).read_text(), "")
                self.assertNotIn(proxy, result.stdout + result.stderr)

    def test_failed_installer_does_not_export_partial_configuration(self):
        content = "IS_PROXY=true\nPROXY_SERVER=socks5://127.0.0.1:1080\n"
        script = f"import os, sys; from pathlib import Path; Path(os.environ['GITHUB_ENV']).write_text({content!r}); sys.exit(8)"
        with tempfile.TemporaryDirectory() as folder:
            environment = self.command_files(folder)
            result = self.invoke(script, proxy=True, environment=environment)
            self.assertEqual(result.returncode, 8)
            self.assertEqual(Path(environment["GITHUB_ENV"]).read_text(), "")

    def test_app_cannot_publish_command_files_or_step_summary(self):
        script = f"""
import os
from pathlib import Path
for name in {PRIVATE_COMMAND_FILES!r}:
    Path(os.environ[name]).write_text({PRIVATE_TEXT!r})
"""
        with tempfile.TemporaryDirectory() as folder:
            environment = self.command_files(folder)
            result = self.invoke(script, environment=environment)
            self.assertEqual(result.returncode, 0)
            for path in environment.values():
                self.assertEqual(Path(path).read_text(), "")

    def test_process_start_error_does_not_print_command_details(self):
        result = subprocess.run(
            [sys.executable, str(RUNNER), "/nonexistent/private-command-secret"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("private-command-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
