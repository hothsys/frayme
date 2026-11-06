"""Locating and checking the ffmpeg and ffprobe binaries."""

import shutil


# ─── ffmpeg / ffprobe detection ───────────────────────────────────────────────

def _find_tool(name):
    return shutil.which(name)


_ffmpeg_path = _find_tool('ffmpeg')


_ffprobe_path = _find_tool('ffprobe')


def has_ffmpeg():
    return _ffmpeg_path is not None


def has_ffprobe():
    return _ffprobe_path is not None
