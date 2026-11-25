# Getting started

## Requirements

- **Python 3.7+** ([download](https://www.python.org/downloads/))
- **FFmpeg**: required for recovery and decode verification

| OS | Install FFmpeg |
|----|----------------|
| macOS | `brew install ffmpeg` |
| Linux (Debian/Ubuntu) | `sudo apt install ffmpeg` |
| Windows | `winget install ffmpeg`, plus Python from [python.org](https://www.python.org/downloads/) if you don't have it |

Run `ffmpeg -version` to confirm it's on your `PATH`; Frayme finds it automatically. No pip install or other dependencies are needed.

## Starting and stopping

Frayme runs as a local web server at `http://localhost:5901` and opens your browser when it starts.

### macOS

Double-click **Frayme.app** in the project folder. Frayme starts in the background (no Terminal window) and your browser opens.

- **First launch:** if macOS asks whether Frayme can access your Downloads folder or a removable volume, click **Allow**. Frayme needs this to scan your videos. If macOS blocks the app outright, right-click it, choose **Open**, and confirm.
- **To stop it or reopen the browser:** double-click Frayme.app again. A dialog says Frayme is already running and offers **Open in Browser** or **Stop Frayme**. If you clone to another directory and launch from there, the app detects the running instance globally (via `app.conf` port setting) and shows the same dialog.
- **Dock shortcut:** drag Frayme.app onto the Dock. Keep the app itself in the project folder, because it uses `frayme.sh` next to it.

Prefer a visible log? Double-click **Frayme.command** instead; it runs Frayme in a Terminal window, and pressing Return there stops it. You can also use the `frayme.sh` commands below.

### Linux

From the project folder:

```sh
./frayme.sh start         # start in the background and open the browser
./frayme.sh stop          # stop it
./frayme.sh status        # check whether it's running
./frayme.sh logs          # follow the server log
./frayme.sh restart
```

Add a port number to `start` or `restart` (e.g. `./frayme.sh start 8080`) to use a different port. Alternatively, edit `app.conf` (`PORT=5901`) so Frayme.app and `frayme.sh` share the same setting.

### Windows

From the project folder in Command Prompt or PowerShell:

```sh
python app.py             # start (use "py app.py" if "python" isn't found)
python app.py 8080        # start on a different port
```

Leave the window open while you use Frayme, and press **Ctrl+C** in it to stop. (Under WSL, use the Linux instructions.)
