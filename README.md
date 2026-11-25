# <img src="logo.svg" width="100" valign="middle" hspace="10" vspace="4"/>Frayme

A web-based tool that recursively scans directories for corrupted video files and offers best-effort recovery.

## Quick Start

Requires Python 3.7+ ([download](https://www.python.org/downloads/)) and FFmpeg (`brew install ffmpeg` on macOS). Frayme runs as a local web server at `http://localhost:5901` and opens your browser when it starts. No pip install or other dependencies are needed.

- **macOS:** double-click **Frayme.app** in the project folder. Double-click it again to stop it or reopen the browser.
- **Linux:** from the project folder, run `./frayme.sh start` (stop with `./frayme.sh stop`).
- **Windows:** from the project folder, run `python app.py` (use `py app.py` if `python` isn't found). Press **Ctrl+C** to stop.

FFmpeg install commands for each OS, changing the port and the rest are in [Getting started](docs/getting-started.md).

## Documentation

Full documentation lives in the [`docs/`](docs/) folder:

| Page | What it covers |
| --- | --- |
| [Getting started](docs/getting-started.md) | Requirements, FFmpeg install, starting and stopping on macOS, Linux and Windows |
| [Features](docs/features.md) | Feature overview |
| [Usage](docs/usage.md) | Scanning, statuses, recovering, the Recovery Queue, CSV export |
| [Command line](docs/command-line.md) | Recovering a whole folder without the browser |
| [Recovery](docs/recovery.md) | How recovery works and its limitations |
| [Development](docs/development.md) | Project structure and the test suite |

## License

This project is licensed under the [MIT](LICENSE.md) License. See the [LICENSE.md](LICENSE.md) file for details.
