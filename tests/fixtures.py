"""Test fixtures: generate small videos with ffmpeg, then damage copies of them.

Nothing here is checked in as a binary. Each test run encodes a few short
clips from ffmpeg's built-in test patterns into a temporary folder, and the
damage helpers below imitate what real failures leave behind: a lost moov,
a zeroed header, a cut-off tail, foreign data spliced into the media.
"""

import atexit
import hashlib
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

# Make the project importable when tests are run from any folder
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')


def _has_encoder(name):
    if not FFMPEG:
        return False
    r = subprocess.run([FFMPEG, '-hide_banner', '-encoders'],
                       capture_output=True, text=True)
    return any(line.split()[1:2] == [name] for line in r.stdout.splitlines()
               if line.startswith(' '))


HAS_X264 = _has_encoder('libx264')
HAS_X265 = _has_encoder('libx265')

needs_ffmpeg = unittest.skipUnless(FFMPEG and FFPROBE, 'ffmpeg/ffprobe not on PATH')
needs_x264 = unittest.skipUnless(HAS_X264, 'ffmpeg built without libx264')
needs_x265 = unittest.skipUnless(HAS_X265, 'ffmpeg built without libx265')


# ─── Generating clips ─────────────────────────────────────────────────────────

# Test patterns that encode differently, so a donor and a damaged file never
# share content (only encoder settings)
PATTERNS = {
    'a': 'testsrc2=size={size}:rate={rate}:duration={duration}',
    'b': 'smptehdbars=size={size}:rate={rate}:duration={duration},'
         'drawbox=x=t*40:y=40:w=80:h=80:color=red@0.8:t=fill',
    'c': 'mandelbrot=size={size}:rate={rate},trim=duration={duration}',
}


def make_clip(path, pattern='a', size='640x360', rate=30, duration=4,
              vcodec='libx264', gop=30, bframes=0, audio=True,
              faststart=False, rotation=None, extra=()):
    """Encode a short test clip. Returns path.

    The defaults resemble a phone or action camera: H.264 High profile,
    CABAC, a keyframe every second, no B-frames, mono AAC interleaved with
    the video, moov written at the end.
    """
    src = PATTERNS[pattern].format(size=size, rate=rate, duration=duration)
    cmd = [FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', src]
    if audio:
        cmd += ['-f', 'lavfi', '-i',
                f'sine=frequency=440:sample_rate=44100:duration={duration}']
    cmd += ['-c:v', vcodec, '-pix_fmt', 'yuv420p', '-g', str(gop)]
    if vcodec == 'libx264':
        cmd += ['-preset', 'veryfast', '-bf', str(bframes)]
    elif vcodec == 'libx265':
        cmd += ['-preset', 'fast', '-tag:v', 'hvc1', '-x265-params',
                f'log-level=error:bframes={bframes}:keyint={gop}:min-keyint={gop}']
    if audio:
        cmd += ['-c:a', 'aac', '-ac', '1', '-b:a', '64k']
    if faststart:
        cmd += ['-movflags', '+faststart']
    cmd += list(extra) + [path]
    _run(cmd)
    if rotation is not None:
        # Display rotation is set on an input, so it takes a remux. ffmpeg
        # counts it counter-clockwise; rotation here is clockwise.
        tmp = path + '.rot' + os.path.splitext(path)[1]
        _run([FFMPEG, '-y', '-v', 'error', '-display_rotation:v:0',
              str((-rotation) % 360), '-i', path, '-map', '0', '-c', 'copy', tmp])
        os.replace(tmp, path)
    return path


def make_avi(path, duration=4):
    """An MJPEG + PCM AVI, the format older cameras record."""
    _run([FFMPEG, '-y', '-v', 'error',
          '-f', 'lavfi', '-i', f'testsrc2=size=640x480:rate=30:duration={duration}',
          '-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=22050:duration={duration}',
          '-c:v', 'mjpeg', '-q:v', '5', '-c:a', 'pcm_s16le', path])
    return path


def make_other(path, duration=3, gop=250):
    """A Matroska or MPEG-TS file (container picked from the extension)."""
    _run([FFMPEG, '-y', '-v', 'error',
          '-f', 'lavfi', '-i', f'testsrc2=size=640x360:rate=30:duration={duration}',
          '-c:v', 'libx264', '-preset', 'veryfast', '-g', str(gop),
          '-pix_fmt', 'yuv420p', path])
    return path


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f'{" ".join(cmd)}\n{r.stderr.decode(errors="replace")}')


# ─── Damaging copies ──────────────────────────────────────────────────────────

def top_boxes(path):
    """[(type, offset, size)] for the file's top-level MP4 boxes."""
    boxes = []
    size_total = os.path.getsize(path)
    with open(path, 'rb') as f:
        off = 0
        while off + 8 <= size_total:
            f.seek(off)
            size, kind = struct.unpack('>I4s', f.read(8))
            if size == 1:
                size = struct.unpack('>Q', f.read(8))[0]
            elif size == 0:
                size = size_total - off
            if size < 8:
                break
            boxes.append((kind.decode('latin-1'), off, size))
            off += size
    return boxes


def drop_moov(src, dst):
    """Copy src without its moov box: what a recording interrupted before
    the camera wrote its index looks like."""
    with open(src, 'rb') as f:
        data = f.read()
    out = b''.join(data[off:off + size] for kind, off, size in top_boxes(src)
                   if kind != 'moov')
    with open(dst, 'wb') as f:
        f.write(out)
    return dst


def zero_head(src, dst, nbytes):
    """Copy src with its first nbytes zeroed, as some card failures do."""
    shutil.copyfile(src, dst)
    with open(dst, 'r+b') as f:
        f.write(b'\x00' * nbytes)
    return dst


def cut_tail(src, dst, keep_fraction):
    """Copy only the first part of src: a transfer or recording cut short."""
    keep = int(os.path.getsize(src) * keep_fraction)
    with open(src, 'rb') as f, open(dst, 'wb') as out:
        out.write(f.read(keep))
    return dst


def overwrite_junk(src, dst, at_fraction, nbytes, seed=1):
    """Copy src with nbytes partway through replaced by random data, like a
    carved file holding another file's blocks where part of the recording
    should be. Sizes stay the same, as they do in carved files."""
    with open(src, 'rb') as f:
        data = f.read()
    at = int(len(data) * at_fraction)
    rng = random.Random(seed)
    junk = bytes(rng.getrandbits(8) for _ in range(nbytes))
    with open(dst, 'wb') as f:
        f.write(data[:at] + junk + data[at + nbytes:])
    return dst


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def ui_offers_recover(status, details):
    """Whether the app shows a Recover button for a scan result. Mirrors
    canRecover in js/rows.js; keep the two in step."""
    details = details or {}
    return ((status in ('no_moov', 'corrupt') and not details.get('unrecoverable'))
            or bool(details.get('salvageable')))


# ─── Inspecting results ───────────────────────────────────────────────────────

def probe(path, entries, stream='v:0'):
    """ffprobe stream fields as a dict of strings."""
    r = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', stream,
                        '-show_entries', f'stream={entries}',
                        '-of', 'default=nw=1', path],
                       capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out.setdefault(k, v)
    return out


def count_packets(path, stream='v:0'):
    r = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', stream,
                        '-count_packets', '-show_entries', 'stream=nb_read_packets',
                        '-of', 'csv=p=0', path], capture_output=True, text=True)
    digits = ''.join(c for c in r.stdout if c.isdigit())
    return int(digits) if digits else 0


def decode_errors(path):
    """Error lines from a full decode. Empty means it plays cleanly."""
    r = subprocess.run([FFMPEG, '-v', 'error', '-i', path, '-f', 'null', '-'],
                       capture_output=True, text=True)
    return [line for line in r.stderr.splitlines() if line.strip()]


def packet_times(path):
    """[(pts, dts)] for the video packets, in file order."""
    r = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'packet=pts,dts', '-of', 'csv=p=0', path],
                       capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        parts = line.strip().split(',')
        if len(parts) >= 2 and parts[0].lstrip('-').isdigit() and parts[1].lstrip('-').isdigit():
            out.append((int(parts[0]), int(parts[1])))
    return out


# ─── Shared clip cache ────────────────────────────────────────────────────────

class ClipCache:
    """Encodes each named clip once per test run, in one temporary folder.

    Tests copy from here into their own folders before damaging anything, so
    the cached originals stay intact.
    """

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix='frayme-clips-')
        self._made = {}

    def get(self, name, ext='.mp4', maker=make_clip, **kwargs):
        if name not in self._made:
            path = os.path.join(self.root, name + ext)
            maker(path, **kwargs)
            self._made[name] = path
        return self._made[name]

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


CLIPS = ClipCache()
atexit.register(CLIPS.cleanup)


class FrayTestCase(unittest.TestCase):
    """Gives each test an empty work folder and an output folder, and resets
    Frayme's session state so donors from one test can't help another."""

    def setUp(self):
        from backend import state
        self.work = tempfile.mkdtemp(prefix='frayme-test-')
        self.src_dir = os.path.join(self.work, 'videos')
        self.out_dir = os.path.join(self.work, 'recovered')
        os.makedirs(self.src_dir)
        os.makedirs(self.out_dir)
        state.donor_pool.clear()
        state.donor_map.clear()
        state.scan_state['results'] = []

    def tearDown(self):
        from backend import state
        state.donor_pool.clear()
        state.donor_map.clear()
        shutil.rmtree(self.work, ignore_errors=True)

    def place(self, cached_path, name):
        """Copy a cached clip into this test's source folder."""
        dst = os.path.join(self.src_dir, name)
        shutil.copyfile(cached_path, dst)
        return dst

    def damaged(self, name, touch=False):
        """A path in the source folder for a damaged copy; touch creates an
        empty file there (enough for queue tests that never decode it)."""
        path = os.path.join(self.src_dir, name)
        if touch:
            open(path, 'wb').close()
        return path

    def assertPlaysCleanly(self, path, min_frames=1):
        """The recovered file decodes with no errors and has real frames.

        Metadata alone proves nothing: a file can report the right frame
        count and duration while every frame fails to decode.
        """
        self.assertTrue(os.path.isfile(path), f'missing output {path}')
        errors = decode_errors(path)
        self.assertEqual(errors, [], f'{os.path.basename(path)} has decode errors')
        frames = count_packets(path)
        self.assertGreaterEqual(frames, min_frames,
                                f'{os.path.basename(path)}: {frames} frames')
        from backend.decode import _check_decodes_cleanly
        ok, msg = _check_decodes_cleanly(path)
        self.assertTrue(ok, msg)
