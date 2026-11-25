# Logging

Frayme follows the shared convention in
[hothsys/.github docs/logging.md](https://github.com/hothsys/.github/blob/main/docs/logging.md).

## Where the log lives

- macOS: `~/Library/Logs/Frayme/app.log` (visible in Console.app)
- Other Unix: `$XDG_STATE_HOME/Frayme/app.log`, or
  `~/.local/state/Frayme/app.log` when `XDG_STATE_HOME` is unset

The file rotates as it is written, at 5 MB, keeping 3 backups
(`app.log.1` to `app.log.3`, 20 MB at most).

An old `.frayme.log` from before this change is left where it was, in the
app folder. It is no longer written to.

## Level

Levels are `DEBUG`, `INFO`, `WARNING`, `ERROR`. The default is `INFO`.
Set it in `app.conf` (`LOG_LEVEL`), or override it with the
`FRAYME_LOG_LEVEL` environment variable. `DEBUG` detail goes to the file
only; the console never shows below `INFO`.

## What is logged

Startup and shutdown, HTTP request lines (method and path only, never the
query string), and uncaught errors with their tracebacks. File contents,
edited text, and PII values are never logged.

## Reading the log

```sh
./frayme.sh logs          # follow the log file
```

The command line tool (`frayme_cli.py`) does not write to the log file.
It prints to the console instead.
