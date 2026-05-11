# <img src="logo.svg" width="75" valign="bottom" hspace="0" /> &nbsp;&nbsp;Frayme

A web-based tool that recursively scans directories for corrupted video files and offers best-effort recovery.

![License](https://img.shields.io/badge/license-MIT-green)

## What It Does

- **Scans** directories recursively for damaged video files (`.mp4`, `.mov`, `.m4v`, `.m4a`, `.3gp`, `.3g2`, `.avi`)
- **Detects** missing moov atoms, leading-zero corruption, truncated files, empty files, and invalid container structures
- **Recovers** files using raw H.264 NAL extraction with SPS/PPS probing, or moov transplant from a healthy donor file
- **Auto-selects donors** by matching filename prefixes — handles directories with mixed devices (GoPro, iPhone, Nikon)
- **Exports** scan results to CSV

## Requirements

- **Python 3.7+** (ships with macOS)
- **FFmpeg** — required for recovery and decode verification

### Installing FFmpeg

```sh
brew install ffmpeg
```

Verify the install:

```sh
ffmpeg -version
```

Frayme detects FFmpeg automatically if it's on your `PATH`.

## Quick Start

```sh
python3 app.py
```

That's it — no pip install, no dependencies beyond FFmpeg. This starts a local web server and opens your browser to `http://localhost:5901`.

To use a different port:

```sh
python3 app.py 8080
```

## Usage

1. Enter a directory path in the scan bar (or browse to one)
2. Click **Scan** — files are checked in parallel and results stream in real-time
3. Problematic files are grouped by directory with a status:

   | Status | Meaning |
   |--------|---------|
   | **no_moov** | Missing moov atom — video data exists but the file is unplayable |
   | **corrupt** | Invalid structure, leading zeros, or no valid container |
   | **truncated** | File cut short — mdat claims more bytes than the file contains |
   | **empty** | 0-byte file |
   | **error** | Permission or OS-level read error |

4. Click **Recover** on any file — Frayme auto-selects a compatible donor from healthy files found during the scan
5. Export results to CSV at any time

## How Recovery Works

### Raw NAL Extraction (Primary Strategy)

When a video has raw media data (`mdat`) but is missing its metadata index (`moov`):

1. Scans the mdat for length-prefixed H.264 NAL units
2. Collects all unique SPS/PPS parameter sets from healthy files in the same directory
3. **Probes each candidate** by test-decoding against the actual video data to find the correct resolution match
4. Trims to the first IDR keyframe (skips orphaned P/B-frames that cause playback failures)
5. Filters out zero-corrupted NALs and skips subsequent frames until the next keyframe
6. Wraps the clean Annex-B stream with FFmpeg at the correct framerate

### Moov Transplant (Fallback)

If raw NAL extraction isn't possible, grafts the moov atom from a healthy donor file and adjusts chunk offset tables to match.

### Leading Zeros Recovery

For files with zeroed-out headers, Frayme tries multiple strategies in order:

1. **Moov-at-end recovery** — if the moov survived at the end of the file, trims damaged chunks and rebuilds
2. **MP4 structure survival** — extracts valid boxes after the zero region
3. **Raw NAL extraction** — same pipeline as no-moov recovery, using donor SPS/PPS
4. **Zero strip** — last resort

## Project Structure

```
frayme/
  app.py        # Server, scanner, and recovery engine
  index.html    # Web UI
  logo.svg      # App logo
  js/
    app.js      # Frontend JavaScript
  css/
    styles.css  # Stylesheet
```

Zero build step. No npm, no pip, no bundler.

## Limitations

- Recovery is best-effort and depends on having a compatible donor file
- Recovered files are video-only (audio tracks are not recovered)
- Files with corrupted mdat (actual video data damage) will have gaps at keyframe boundaries
- Multi-mode cameras (e.g., GoPro 4K and 1080p) require SPS probing to match the correct resolution

## License

MIT

---

Built with [Claude Code](https://claude.ai/claude-code) using Claude Opus 4.6
