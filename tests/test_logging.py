"""Logging setup: OS-standard location, level from app.conf or the
FRAYME_LOG_LEVEL environment variable, rotation at 5 MB keeping 3 backups."""

import logging
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

# Make the project importable when tests are run from any folder
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from backend import log as frayme_log
from backend.server import FrameHandler


class LogDirTest(unittest.TestCase):
    def test_macos_location(self):
        with mock.patch.object(frayme_log.platform, "system", return_value="Darwin"):
            with mock.patch.dict(os.environ, {"HOME": "/Users/someone"}, clear=False):
                self.assertEqual(
                    frayme_log.log_dir(), "/Users/someone/Library/Logs/Frayme"
                )

    def test_other_unix_uses_xdg_state_home(self):
        with mock.patch.object(frayme_log.platform, "system", return_value="Linux"):
            with mock.patch.dict(
                os.environ, {"XDG_STATE_HOME": "/tmp/state", "HOME": "/home/someone"}
            ):
                self.assertEqual(frayme_log.log_dir(), "/tmp/state/Frayme")

    def test_other_unix_falls_back_to_local_state(self):
        with mock.patch.object(frayme_log.platform, "system", return_value="Linux"):
            env = {"HOME": "/home/someone"}
            env.pop("XDG_STATE_HOME", None)
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(
                    frayme_log.log_dir(), "/home/someone/.local/state/Frayme"
                )


class ConfiguredLevelTest(unittest.TestCase):
    def test_environment_wins_over_app_conf(self):
        with mock.patch.dict(os.environ, {"FRAYME_LOG_LEVEL": "DEBUG"}):
            self.assertEqual(frayme_log._configured_level(), logging.DEBUG)

    def test_app_conf_level_is_used_without_environment(self):
        env = dict(os.environ)
        env.pop("FRAYME_LOG_LEVEL", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(frayme_log._configured_level(), logging.INFO)

    def test_unknown_level_falls_back_to_info(self):
        with mock.patch.dict(os.environ, {"FRAYME_LOG_LEVEL": "BOGUS"}):
            self.assertEqual(frayme_log._configured_level(), logging.INFO)


class SetupLoggingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = logging.getLogger()
        self.saved_handlers = list(self.root.handlers)
        self.saved_level = self.root.level
        self.saved_excepthook = sys.excepthook
        import threading

        self.saved_thread_excepthook = threading.excepthook

    def tearDown(self):
        for handler in list(self.root.handlers):
            self.root.removeHandler(handler)
            handler.close()
        for handler in self.saved_handlers:
            self.root.addHandler(handler)
        self.root.setLevel(self.saved_level)
        if hasattr(self.root, "_app_configured"):
            delattr(self.root, "_app_configured")
        sys.excepthook = self.saved_excepthook
        import threading

        threading.excepthook = self.saved_thread_excepthook

    def test_writes_to_the_os_location_with_rotation_settings(self):
        env = {"HOME": self.tmp.name, "FRAYME_LOG_LEVEL": "DEBUG"}
        env.pop("XDG_STATE_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(frayme_log.platform, "system", return_value="Linux"):
                log_file = frayme_log.setup_logging()
                logging.getLogger("frayme.test").debug("hello from the test")
                logging.shutdown()
        expected = os.path.join(self.tmp.name, ".local", "state", "Frayme", "app.log")
        self.assertEqual(log_file, expected)
        with open(expected, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("hello from the test", content)
        self.assertIn("DEBUG", content)
        self.assertEqual(frayme_log.LOG_FILE_MAX_BYTES, 5 * 1024 * 1024)
        self.assertEqual(frayme_log.LOG_FILE_BACKUPS, 3)


class HttpLogTest(SetupLoggingTest):
    """Request lines are logged once, with the query string stripped, and
    error responses log the error instead of repeating the request line."""

    def test_request_and_error_lines(self):
        env = {"HOME": self.tmp.name, "FRAYME_LOG_LEVEL": "DEBUG"}
        env.pop("XDG_STATE_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(frayme_log.platform, "system", return_value="Linux"):
                frayme_log.setup_logging()
                server = ThreadingHTTPServer(("127.0.0.1", 0), FrameHandler)
                port = server.server_address[1]
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status?x=1")
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{port}/nope")
                    except urllib.error.HTTPError as e:
                        self.assertEqual(e.code, 404)
                finally:
                    server.shutdown()
                    server.server_close()
                logging.shutdown()
        log_file = os.path.join(self.tmp.name, ".local", "state", "Frayme", "app.log")
        with open(log_file, encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content.count("GET /api/status"), 1)
        self.assertNotIn("x=1", content)
        self.assertEqual(content.count("GET /nope"), 1)
        self.assertEqual(content.count("code 404"), 1)


if __name__ == "__main__":
    unittest.main()
