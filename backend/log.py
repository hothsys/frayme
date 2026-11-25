"""Logging for the whole app: setup_logging() once, first thing in app.py, then
each module logs through logging.getLogger(__name__).

Two destinations:
  * a persistent, rotating log file in the OS's standard place
    (~/Library/Logs/Frayme/app.log on macOS, also shown by Console.app;
    ~/.local/state/Frayme/app.log elsewhere), with a timestamp, level, and
    source on every line. `./frayme.sh logs` follows it.
  * the console (stdout) as plain lines, warnings and errors labelled, which
    is what a foreground run shows.

The level comes from FRAYME_LOG_LEVEL in the environment, or LOG_LEVEL in
app.conf when the environment does not set it (default INFO). DEBUG detail
goes to the file only; the console never shows below INFO.
Standard library only.
"""

import logging
import logging.handlers
import os
import platform
import sys
import threading

LOG_FILE_MAX_BYTES = 5 * 1024 * 1024  # rotate at 5 MB...
LOG_FILE_BACKUPS = 3  # ...keeping app.log.1 to app.log.3 (20 MB at most)

APP_NAME = "Frayme"


def _app_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log_dir():
    if platform.system() == "Darwin":
        return os.path.expanduser(f"~/Library/Logs/{APP_NAME}")
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, APP_NAME)


LOG_FILE = os.path.join(log_dir(), "app.log")


def _read_app_conf():
    """Small KEY=VALUE reader for app.conf; returns {} when it is missing."""
    values = {}
    try:
        with open(os.path.join(_app_dir(), "app.conf"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def _configured_level():
    name = os.environ.get("FRAYME_LOG_LEVEL") or _read_app_conf().get("LOG_LEVEL", "INFO")
    return getattr(logging, str(name).upper(), logging.INFO)


class _ConsoleFormatter(logging.Formatter):
    """Plain message; warnings and errors get their level in front."""

    def format(self, record):
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            message = f"{record.levelname}: {message}"
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return message


def setup_logging():
    """Configure the root logger (once per process). Returns the log file path,
    or None if it couldn't be created (console logging still works)."""
    root = logging.getLogger()
    if getattr(root, "_app_configured", False):
        if any(isinstance(h, logging.FileHandler) for h in root.handlers):
            return os.path.join(log_dir(), "app.log")
        return None
    level = _configured_level()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(max(level, logging.INFO))  # DEBUG detail goes to the file only
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)

    log_file = None
    log_file_path = os.path.join(log_dir(), "app.log")
    try:
        os.makedirs(log_dir(), exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file_path,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )
        )
        root.addHandler(file_handler)
        log_file = log_file_path
    except OSError as e:
        logging.getLogger(__name__).warning(
            f"Logging to the console only: cannot write {log_file_path} ({e})"
        )

    # Errors nothing else caught: log them with their traceback instead of losing them
    def log_uncaught(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("app").critical("Unhandled error", exc_info=(exc_type, exc, tb))

    sys.excepthook = log_uncaught
    threading.excepthook = lambda args: logging.getLogger("app").error(
        f"Unhandled error in thread {args.thread.name if args.thread else '?'}",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )

    root._app_configured = True
    return log_file
