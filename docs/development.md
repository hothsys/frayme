# Development

## Project structure

```
frayme/
  app.py            # Entry point: starts the server
  frayme_cli.py     # Command line: recover a whole folder (see docs/command-line.md)
  backend/          # Server, scanner, and recovery engine (Python standard library only)
  index.html        # Web UI
  logo.svg          # App logo (source for all icons)
  favicon.*, apple-touch-icon.png   # Browser icons, generated from logo.svg
  js/               # Frontend JavaScript: core, browse, rows, scan, history, recovery, mount, main
  css/styles.css    # Stylesheet
  frayme.sh         # Start/stop/status script (macOS, Linux)
  Frayme.app        # macOS double-click launcher (no Terminal window)
  Frayme.command    # macOS double-click launcher (Terminal window)
  launcher/         # Frayme.app source and icon generator
  app.conf          # Port setting (default PORT=5901)
  tests/            # Test suite and real-file regression script
```

Zero build step. No npm, no pip, no bundler.

## Testing

The test suite generates its own damaged videos, so no video files are checked in. Each run encodes a few short clips from FFmpeg's built-in test patterns into a temporary folder, damages copies of them the way real files get damaged (a lost moov, a zeroed header, a cut-off tail, another file's data in the middle, a stretch of a different recording), and recovers them. A recovery only passes if its output actually decodes: matching metadata is not enough.

```sh
python3 -m unittest discover tests          # everything, about a minute
python3 -m unittest tests.test_recovery     # one module
```

It needs FFmpeg built with libx264 (Homebrew's is); HEVC tests also use libx265 and are skipped without it. Everything is written to the system temp folder and removed afterwards; nothing goes to Downloads, and the damaged originals are checked to be unchanged after every recovery.

| Module | Covers |
|--------|--------|
| `test_detect.py` | Status for each kind of damage, and which files get a Recover button |
| `test_recovery.py` | Every recovery engine: missing moov (with and without a donor, B-frames), zeroed headers, carved files, AVI, HEVC, rotation, output folder checks |
| `test_queue_and_scan.py` | The scan generator and the recovery queue (jobs, cancel, rotation, failures) |
| `test_server.py` | The HTTP server: static files, JSON endpoints, the scan event stream |
| `test_engine.py` | The decode check and the NAL scanner |
| `test_batch.py` | The command line (batch recovery, output layout, donors, scan-only) and a check that its recoverable rule matches `canRecover` in `js/rows.js` |

Known limitations are kept as expected failures, so they show up as an "unexpected success" once fixed.

Synthetic clips can't reproduce everything real cameras and carving tools do, so changes to the recovery engine should also be checked against real damaged files. `tests/regression.py` scans a folder the way the app does, recovers everything it would offer to recover (into a temporary folder), and decode-tests the results. Save a run before a change and compare after:

```sh
python3 tests/regression.py ~/damaged-videos --donors ~/healthy-videos --save before.json
# ...change the engine...
python3 tests/regression.py ~/damaged-videos --donors ~/healthy-videos --compare before.json
```

`--compare` lists every file whose outcome, method or frame count changed, and exits with status 1 if any recovery got worse. Add `--keep ~/Desktop/frayme-check` to save the recovered videos there for watching instead of deleting them. Your files stay where they are; only the script is part of the project.
