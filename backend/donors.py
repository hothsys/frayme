"""Donor selection: finding healthy files whose settings match a damaged one."""

import collections
import os
import re
import subprocess

from .config import VIDEO_EXTS
from .detect import check_video
from .mp4 import _extract_avcc_params, _extract_hvcc_params, _extract_moov
from .state import donor_pool
from .tools import _ffprobe_path, has_ffprobe


def _collect_sps_candidates(fdir, primary_params, codec='h264'):
    """Collect unique SPS/PPS (and VPS for HEVC) from healthy donor pool files.

    Returns a list of (vps_list, sps_list, pps_list) tuples with the primary
    donor's params first, followed by other unique variants from the same
    directory. For H.264, vps_list is always [].
    """
    extract_fn = _extract_hvcc_params if codec == 'hevc' else _extract_avcc_params
    seen = set()
    candidates = []

    # Primary donor first
    vps = primary_params.get('vps_list', [])
    key = tuple(s.hex() for s in primary_params['sps_list'])
    seen.add(key)
    candidates.append((vps, primary_params['sps_list'],
                        primary_params['pps_list']))

    # Check other healthy files in the pool
    for pool_file in _donor_files(fdir):
        try:
            moov = _extract_moov(pool_file)
            if not moov:
                continue
            params = extract_fn(moov)
            if not params:
                continue
            key = tuple(s.hex() for s in params['sps_list'])
            if key not in seen:
                seen.add(key)
                candidates.append((params.get('vps_list', []),
                                   params['sps_list'], params['pps_list']))
        except Exception:
            continue

    return candidates


def _find_matching_donor(filepath, candidates):
    """Pick the best donor from candidates by filename prefix matching."""
    base = os.path.splitext(os.path.basename(filepath))[0]
    # Strip trailing digits/underscores to get the device/naming prefix
    prefix = re.sub(r'[_\-]?\d+$', '', base)

    best = None
    best_len = -1
    for c in candidates:
        if os.path.normpath(c) == os.path.normpath(filepath):
            continue
        cbase = os.path.splitext(os.path.basename(c))[0]
        cpfx = re.sub(r'[_\-]?\d+$', '', cbase)
        if cpfx == prefix and len(cpfx) > best_len:
            best = c
            best_len = len(cpfx)

    # Fallback: return first candidate that isn't the file itself
    if best is None:
        for c in candidates:
            if os.path.normpath(c) != os.path.normpath(filepath):
                return c
    return best


def _get_donor_rotation(donor_path):
    """Get rotation metadata from donor file via ffprobe. Returns int or None."""
    if not has_ffprobe():
        return None
    try:
        cmd = subprocess.run(
            [_ffprobe_path, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream_side_data=rotation',
             '-of', 'csv=p=0', donor_path],
            capture_output=True, timeout=10)
        val = cmd.stdout.decode().strip().rstrip(',')
        if val:
            return int(float(val))
    except Exception:
        pass
    return None


def _most_common_rate_donor(donors):
    """The donor whose framerate most of the donors share.

    The same SPS can come from different framerates (iPhone slo-mo uses the
    1080p SPS at 120 fps), and a moov-less file has no timing of its own, so
    the most common rate among matching donors is the best guess.
    """
    rates = {}
    for donor in donors:
        try:
            r = subprocess.run(
                [_ffprobe_path, '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=r_frame_rate', '-of', 'csv=p=0',
                 donor],
                capture_output=True, timeout=10)
            rates[donor] = r.stdout.decode().strip().rstrip(',')
        except Exception:
            continue
    if not rates:
        return donors[0]
    common = collections.Counter(rates.values()).most_common(1)[0][0]
    return next(d for d in donors if rates.get(d) == common)


# ─── Recovery execution ───────────────────────────────────────────────────────

def _usable_donor(status, details):
    """A donor must be a healthy MP4/MOV with a moov to borrow parameters from.

    Healthy AVIs and other formats used to enter the pool, and recovery then
    failed with "Donor file has no moov atom".
    """
    return status == 'ok' and bool(details.get('has_moov'))


def _donor_files(fdir):
    """Donor candidates for a damaged file in fdir.

    Healthy files from the same folder, or when it has none, every donor
    found anywhere in this session's scans. SPS probing picks the one that
    matches, so unrelated cameras among them are harmless. Carved or
    salvaged files often sit in a folder with no healthy video at all, while
    the same camera's healthy videos are elsewhere on the scanned drive.
    """
    own = donor_pool.get(fdir)
    if own:
        return own
    seen, files = set(), []
    for pool in list(donor_pool.values()):
        for f in pool:
            if f not in seen:
                seen.add(f)
                files.append(f)
    return files


def _ensure_donor_pool(fdir):
    """Collect healthy donors for a folder this session hasn't scanned, e.g.
    when recovering from a previous scan loaded in the browser."""
    if fdir in donor_pool:
        return
    healthy = []
    try:
        names = sorted(os.listdir(fdir))
    except OSError:
        names = []
    for name in names:
        fpath = os.path.join(fdir, name)
        if (os.path.splitext(name)[1].lower() not in VIDEO_EXTS
                or not os.path.isfile(fpath)):
            continue
        try:
            status, _, details = check_video(fpath)
            if _usable_donor(status, details):
                healthy.append(fpath)
        except Exception:
            pass
    donor_pool[fdir] = healthy
