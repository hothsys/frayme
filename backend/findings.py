"""What Frayme found about a file, in words for the result rows.

Each description is {'facts': [...], 'notes': [{'level', 'text'}, ...]}.
Facts are short labels shown on the row (codec, resolution, camera); notes
are the bulleted details behind the row's info button. 'warn' notes are
things the user should know before trusting a recovery (no orientation,
an assumed frame rate); 'info' notes are explanations.
"""

import os
from pathlib import Path

from .config import OTHER_VIDEO_EXTS
from .hints import _source_hints
from .mp4 import _scan_for_moov


def _note(notes, level, text):
    notes.append({'level': level, 'text': text})


def _size(n):
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:,.0f} {unit}' if unit == 'bytes' else f'{n:.1f} {unit}'
        n /= 1024


def _codec(details):
    """'h264' or 'hevc' from the detection details (which say 'h265'), or None."""
    codec = ((details or {}).get('mdat_analysis') or {}).get('codec')
    if codec in ('h265', 'hevc'):
        return 'hevc'
    return codec if codec == 'h264' else None


def _resolution(text):
    return text.replace('x', '×') if text else None


# Said for every file whose moov is gone: the moov held all of this
_LOST_WITH_MOOV = [
    ('warn', 'No orientation: phones and most cameras record it only in the missing index '
             '(moov). If the recovered video plays sideways, pick a rotation, before '
             'recovering or afterwards.'),
    ('warn', 'No frame rate or timestamps: recovery takes the frame rate from a donor with '
             'the same camera settings, or assumes 30 fps.'),
    ('warn', 'No sound: Frayme rebuilds only the video of files missing their index.'),
]


def describe_damaged(filepath, status, details):
    """Facts and notes for a damaged file, from a quick look (about half a
    second): its detection details plus the camera clues in its first MBs."""
    facts, notes = [], []
    details = details or {}
    ext = Path(filepath).suffix.lower()

    if status == 'empty':
        _note(notes, 'info', 'The file has no data, so there is nothing to recover. '
                             'Look for another copy.')
        return {'facts': facts, 'notes': notes}
    if status == 'error':
        return {'facts': facts, 'notes': notes}

    walk = details.get('avi_walk')
    if walk:
        facts.append('AVI')
        total = walk.get('total_frames') or 0
        video = walk.get('video_chunks') or 0
        if total:
            _note(notes, 'info', f'{video:,} of the {total:,} frames the header lists are intact.')
        if walk.get('audio_chunks'):
            _note(notes, 'info', 'Has sound, and recovery keeps it.')
        if details.get('salvageable'):
            _note(notes, 'info', 'Recovery keeps the intact part and saves it as a MOV, '
                                 'which QuickTime plays.')
        return {'facts': facts, 'notes': notes}

    if ext in OTHER_VIDEO_EXTS:
        _note(notes, 'info', f'Frayme reports damage in {ext[1:].upper()} files but can\'t '
                             'recover them. Try a player or a repair tool for this format.')
        return {'facts': facts, 'notes': notes}

    if details.get('unrecoverable'):
        if details.get('zero_bytes') or status == 'corrupt':
            _note(notes, 'info', 'Almost nothing but zeros is left, so there is no video to '
                                 'recover.')
        return {'facts': facts, 'notes': notes}

    codec = _codec(details)
    try:
        hints = _source_hints(filepath)
    except Exception:
        hints = {'camera': [], 'dates': [], 'resolution': None, 'own_params': False}

    if codec:
        facts.append({'h264': 'H.264', 'hevc': 'HEVC'}[codec])
    if hints.get('resolution'):
        facts.append(_resolution(hints['resolution']))
    strong = [name for name, sure in hints.get('camera', []) if sure]
    weak = [name for name, sure in hints.get('camera', []) if not sure]
    facts.extend(strong)

    zeroed = details.get('zero_bytes')
    if zeroed:
        pct = details.get('zero_pct')
        _note(notes, 'info', f'The first {_size(zeroed)} are zeroed'
                             + (f' ({pct:g}% of the file)' if pct else '') + '.')
    if status == 'truncated' and details.get('mdat_expected'):
        expected, size = details['mdat_expected'], details.get('file_size') or 0
        if expected > size:
            _note(notes, 'warn', f'The file stops at {_size(size)}, but its media data '
                                 f'should run to {_size(expected)}: about '
                                 f'{(expected - size) / expected * 100:.0f}% is missing.')

    moov_survives = details.get('has_moov')
    if zeroed and not moov_survives:
        # Cameras that write the moov last keep it at the end, out of reach
        # of zeroing at the start; it is rarely more than a few MB
        try:
            size = os.path.getsize(filepath)
            moov_survives = bool(_scan_for_moov(filepath, max(zeroed, size - (8 << 20))))
        except OSError:
            pass
    moov_lost = status == 'no_moov' or (zeroed and not moov_survives)
    if moov_lost:
        for level, text in _LOST_WITH_MOOV:
            _note(notes, level, text)
    elif moov_survives:
        _note(notes, 'info', 'The index (moov) survives, so rotation, frame rate and sound '
                             'can be kept.')

    if moov_lost and codec:
        if hints.get('own_params'):
            _note(notes, 'info', 'Carries its own decoder settings, so no donor is needed.')
        elif codec == 'hevc':
            _note(notes, 'warn', 'HEVC without its decoder settings: recovery needs a donor '
                                 'from the same camera and recording mode.')
        else:
            _note(notes, 'info', 'No decoder settings in the file: recovery takes them from '
                                 'a donor with the same camera settings, or works them out '
                                 'from the frames.')

    unverified = []
    if weak:
        unverified.append('mentions ' + ', '.join(weak))
    if hints.get('dates'):
        unverified.append('has the dates ' + ', '.join(hints['dates']))
    if unverified:
        _note(notes, 'info', 'The data also ' + ' and '.join(unverified) + '. Unverified: '
                             'damaged and carved files can hold other files\' data.')
    return {'facts': facts, 'notes': notes}


_contents_cache = {}


def describe_contents_cached(filepath, details=None):
    """describe_contents, remembered per file (path, size, modified time)."""
    st = os.stat(filepath)
    key = (filepath, st.st_size, st.st_mtime)
    if key not in _contents_cache:
        if len(_contents_cache) > 500:
            _contents_cache.clear()
        _contents_cache[key] = describe_contents(filepath, details)
    return _contents_cache[key]


def describe_contents(filepath, details):
    """Notes from a full scan of the media data (seconds for large files, so
    run on request): where the video is, and how much of the file it fills."""
    from .h264 import _scan_mdat_for_nals
    from .detect import check_video
    notes = []
    details = details or {}
    if 'mdat_offset' not in details and 'zero_bytes' not in details:
        details = check_video(filepath)[2]
    if 'mdat_offset' in details:
        start, size, raw = details['mdat_offset'], details.get('mdat_size') or 0, False
    elif details.get('zero_bytes'):
        start = details['zero_bytes']
        size, raw = os.path.getsize(filepath) - start, True
    else:
        return notes
    if size <= 0:
        return notes

    codec = _codec(details) or 'h264'
    entries = _scan_mdat_for_nals(filepath, start, size, 4, raw_data=raw, codec=codec)
    if not entries:
        _note(notes, 'warn', 'No video frames were found in the media data.')
        return notes

    covered = sum(length + 4 for _, length in entries)
    stretches, begin, end = [], None, None
    for offset, length in entries:
        if end is not None and offset - end > (1 << 20):
            stretches.append((begin, end))
            begin = None
        if begin is None:
            begin = offset
        end = offset + length
    stretches.append((begin, end))

    pct = covered / size * 100
    where = (f'{len(stretches)} separate stretches' if len(stretches) > 1
             else 'one stretch')
    text = (f'Video fills {_size(covered)} of the {_size(size)} of media data '
            f'({pct:.0f}%), in {where}.')
    if pct < 60:
        text += (' The rest is other data, as in files pieced back together from a wiped '
                 'or formatted card; only the video stretches can be recovered.')
        _note(notes, 'warn', text)
    else:
        _note(notes, 'info', text)
    if len(stretches) > 1:
        sizes = ', '.join(_size(b - a) for a, b in stretches[:8])
        _note(notes, 'info', f'Stretch sizes: {sizes}' + (', ...' if len(stretches) > 8 else '')
                             + '. Stretches can come from different recordings; recovery '
                               'keeps the ones that decode.')
    return notes


def describe_recovery(info, result, rotation_choice):
    """Facts and notes for a recovered file, from what the recovery engine
    recorded (info) and the finished result."""
    facts, notes = [], []
    info = info or {}
    if result.get('codec'):
        facts.append(result['codec'])
    if result.get('resolution'):
        facts.append(_resolution(result['resolution']))
    if result.get('fps'):
        facts.append(f"{result['fps']:g} fps")

    # Orientation
    if rotation_choice is not None:
        _note(notes, 'info', f'Rotation set to {rotation_choice}° as chosen.')
    elif info.get('rotation_source') == 'original':
        pass
    elif info.get('rotation_source', '').startswith('donor:'):
        donor = info['rotation_source'][6:]
        _note(notes, 'info', f'Rotation ({result.get("rotation") or 0}°) copied from the '
                             f'donor {donor}, a guess: change it with the picker if the video '
                             'is sideways.')
    elif info.get('rotation_source') == 'unknown':
        _note(notes, 'warn', 'Orientation unknown: it was stored in the lost index. If the '
                             'video plays sideways, change the rotation with the picker.')

    # Frame rate
    source = info.get('fps_source', '')
    if source.startswith('donor:'):
        _note(notes, 'info', f'Frame rate from the donor {source[6:]}.')
    elif source == 'stream':
        _note(notes, 'info', "Frame rate from the video stream's own timing information.")
    elif source == 'assumed':
        _note(notes, 'warn', 'Frame rate unknown, so 30 fps is assumed: playback may be too '
                             'fast or too slow. A donor from the same camera fixes this.')

    if info.get('inferred'):
        _note(notes, 'info', 'Decoder settings were worked out from the frames: no '
                             "donor's settings decoded this file cleanly.")

    found, kept = info.get('frames_found'), result.get('frames')
    fewer = bool(found and kept and kept < found * 0.97)
    counted = f'Kept {kept:,} of the {found:,} frames found. ' if fewer else ''
    if info.get('salvaged'):
        _note(notes, 'warn', counted + 'Parts that didn\'t decode were dropped (stretches '
                             'of other recordings or damaged data), so the video skips at '
                             'those points.')
    elif fewer:
        _note(notes, 'info', counted + 'Frames whose reference frames were lost are skipped '
                             'up to the next keyframe.')

    problems = (info.get('decode') or {}).get('problems')
    if problems:
        _note(notes, 'info', f'{problems} small decode error'
                             + ('s remain' if problems != 1 else ' remains')
                             + ', which players patch over.')

    if result.get('video_only'):
        _note(notes, 'warn', 'No sound: audio isn\'t recovered from files missing their index.')
    return {'facts': facts, 'notes': notes}
