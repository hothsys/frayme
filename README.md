<p align="center">
  <img src="logo.svg" alt="Frayme logo" width="280">
</p>

<h1 align="center">Frayme</h1>

<p align="center">
  A web-based tool that recursively scans directories for corrupted video files and offers best-effort recovery.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.7%2B-blue" alt="Python 3">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>


## What It Does

- Scans directories recursively for damaged video files (`.mp4`, `.mov`, `.m4v`, `.m4a`, `.3gp`, `.3g2`, `.avi`)
- Detects missing moov atoms, leading-zero corruption, truncated files, empty files, and invalid container structures
- Recovers files using raw H.264 NAL extraction with SPS/PPS probing, or moov transplant from a healthy donor file
- Auto-selects donors by matching filename prefixes, so directories with mixed devices (GoPro, iPhone, Nikon) are handled
- Explains each damaged file: codec, resolution, camera, what the damage cost (orientation, frame rate, sound) and how much of the file is still video
- Exports scan results to CSV

## Requirements

- **Python 3.7+** (ships with macOS and most Linux distributions)
- **FFmpeg**: required for recovery and decode verification

| OS | Install FFmpeg |
|----|----------------|
| macOS | `brew install ffmpeg` |
| Linux (Debian/Ubuntu) | `sudo apt install ffmpeg` |
| Windows | `winget install ffmpeg`, plus Python from [python.org](https://www.python.org/downloads/) if you don't have it |

Run `ffmpeg -version` to confirm it's on your `PATH`; Frayme finds it automatically. No pip install or other dependencies are needed.

## Starting and Stopping

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

## Usage

1. Enter a directory path in the scan bar (or browse to one)
2. Click **Scan Directory**. Frayme first searches the folder tree for video files, showing how many it has found so far and which folder it's in; on a large drive this step can take a while. It then checks the files in parallel, and results stream in real-time. While a scan runs, only **Cancel** is available; the path box, Browse, Scan, and Recover buttons are disabled until it finishes. Cancel stops the scan within a moment, even mid-way through large files, and keeps the partial results with a note of how far it got.
3. Problematic files are grouped by directory with a status:

   | Status | Meaning |
   |--------|---------|
   | **no_moov** | Missing moov atom; video data exists, but the file is unplayable |
   | **corrupt** | Invalid structure, leading zeros, or no valid container |
   | **truncated** | File cut short (mdat claims more bytes than the file contains) |
   | **empty** | 0-byte file |
   | **error** | Permission or OS-level read error |

   Each row also shows what Frayme found: the codec, resolution and camera where it can tell, and a **Details** button that expands a list of notes. A caution icon on the button means there's something to know before trusting a recovery, such as a file whose orientation, frame rate and sound were stored in the missing index. Opening the notes of a damaged MP4/MOV also looks through the whole file and reports how much of it is still video (carved files are often mostly other data). Once recovered, the notes say where the rotation and frame rate came from, how many frames were kept, and anything left to check. Sort the files in each folder by name, size or status with the picker next to the filters.

   HEIC/HEIF/AVIF photos saved with a video extension (common inside Photos libraries) are recognized as still images and skipped, not reported as damaged videos.

4. Once the scan finishes, click **Recover** on any file, and Frayme auto-selects a compatible donor from healthy files found during the scan. Recovered videos are saved to the folder shown next to **Save recovered to** (your Downloads folder unless you pick another with **Browse**; **Show** opens it). Clicking a finished job in the Recovery Queue shows its file in Finder. Damaged files lose their rotation, so each row has a rotation picker: **Auto** copies it from a matching donor, or pick one before recovering; on a recovered row, changing it rewrites the recovered file's rotation right away. Recoveries run two at a time in the **Recovery Queue** panel (bottom right), which shows each job's progress; **Clear Done** removes finished and failed jobs, and is greyed out until something has finished. If a recovery fails, the row shows the reason and the button changes to **Retry**
5. Export results to CSV at any time; it includes what Frayme found about each file

## How Recovery Works

### Raw NAL Extraction (Primary Strategy)

When a video has raw media data (`mdat`) but is missing its metadata index (`moov`):

1. Scans the mdat for length-prefixed H.264 or HEVC NAL units, skipping the audio interleaved between them and any unrelated data
2. Uses the file's own SPS/PPS parameter sets if it carries them, and collects all unique ones from healthy donor files
3. Probes each candidate by test-decoding against the actual video data to find the correct match; for H.264 with no matching donor, infers the parameters from the frames themselves (trial decoding)
4. Trims to the first IDR keyframe (skips orphaned P/B-frames that cause playback failures)
5. Filters out damaged NALs, and skips frames until the next keyframe wherever reference frames are missing
6. Wraps the clean Annex-B stream with FFmpeg at the correct framerate
7. Decode-tests the result. If it fails as a whole (for example, a carved file holding stretches of another recording), keeps only the groups of pictures that decode cleanly on their own

### Moov Transplant (Fallback)

If raw NAL extraction isn't possible, grafts the moov atom from a healthy donor file and adjusts chunk offset tables to match.

### Leading Zeros Recovery

For files with zeroed-out headers, Frayme tries multiple strategies in order, and no donor is needed unless the parameter sets can't be found or inferred:

1. **Moov-at-end recovery**: if the moov survived at the end of the file, trims damaged chunks and rebuilds (keeps audio)
2. **MP4 structure survival**: if only whole boxes were zeroed (typically the ftyp), replaces them in place so the moov's offsets stay valid (keeps everything)
3. **Raw NAL extraction**: same pipeline as no-moov recovery, over the data after the zeros
4. **Zero strip**: last resort

Every result, from any strategy, must pass the same decode test, so a file that merely looks valid is never reported as recovered.

## Project Structure

```
frayme/
  app.py            # Entry point: starts the server
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

Known limitations are kept as expected failures, so they show up as an "unexpected success" once fixed.

Synthetic clips can't reproduce everything real cameras and carving tools do, so changes to the recovery engine should also be checked against real damaged files. `tests/regression.py` scans a folder the way the app does, recovers everything it would offer to recover (into a temporary folder), and decode-tests the results. Save a run before a change and compare after:

```sh
python3 tests/regression.py ~/damaged-videos --donors ~/healthy-videos --save before.json
# ...change the engine...
python3 tests/regression.py ~/damaged-videos --donors ~/healthy-videos --compare before.json
```

`--compare` lists every file whose outcome, method or frame count changed, and exits with status 1 if any recovery got worse. Add `--keep ~/Desktop/frayme-check` to save the recovered videos there for watching instead of deleting them. Your files stay where they are; only the script is part of the project.

## Limitations

- Recovery is best-effort. Without a donor, H.264 parameters are inferred from the frames, but HEVC needs a donor from the same camera and mode
- Recovered files from raw extraction are video-only (audio is kept for AVI salvage and when the moov survives)
- Damaged stretches are dropped up to the next keyframe, so recovered videos can have gaps
- A file mixing two recordings (common in carved files) keeps only the parts its parameters decode; without a donor, inference may fail on such files

## License

MIT
