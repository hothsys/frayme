"""Constants: file types, worker counts, and default paths."""

import os


# Recovery queue infrastructure
# Where recovered files go unless the user picks another folder
DEFAULT_OUTPUT_DIR = os.path.expanduser('~/Downloads')


VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.m4a', '.3gp', '.3g2', '.avi'}


# Other video formats: scanned with a basic readability check so damaged ones
# are reported rather than skipped; recovery isn't supported for them
OTHER_VIDEO_EXTS = {'.mkv', '.webm', '.mpg', '.mpeg', '.m1v', '.m2v', '.vob',
                    '.ts', '.mts', '.m2ts', '.wmv', '.asf', '.flv', '.ogv'}


VALID_BOX_TYPES = {
    b'ftyp', b'moov', b'mdat', b'free', b'skip', b'wide', b'pnot',
    b'uuid', b'moof', b'mfra', b'meta', b'styp', b'sidx', b'ssix',
    b'pdin', b'bloc', b'meco',
}


WORKER_THREADS = 4


# Rotations the user can pick, in degrees clockwise. ffmpeg's display
# rotation counts counter-clockwise: an iPhone portrait clip reads -90.
_ROTATIONS = (0, 90, 180, 270)


# ─── Parameter inference (trial decoding) ────────────────────────────────────
#
# When no donor's SPS/PPS decodes a moov-less H.264 file, build them from
# scratch. Slice headers can only be parsed consistently under the settings
# the encoder used, so a search over header-level settings (frame_num and
# POC sizes, entropy coding, deblocking control) yields a shortlist; trial
# decoding of keyframes then fixes the settings that only affect decoding
# (base quantizer, 8x8 transform, frame size), and a short decode of P/B
# frames fixes the default reference count and direct-mode inference.

_COMMON_FRAME_SIZES = [
    (1920, 1080), (1280, 720), (3840, 2160), (1920, 1440), (1440, 1080),
    (960, 720), (2704, 1520), (2704, 2028), (2560, 1440), (4096, 2160),
    (1080, 1920), (720, 1280), (640, 480), (720, 480), (720, 576),
    (848, 480), (640, 360), (1280, 960), (3840, 2880)]
