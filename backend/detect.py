"""Damage detection for each container format (check_video and friends)."""

import mmap
import os
import re
import struct
import subprocess
from pathlib import Path

from .config import OTHER_VIDEO_EXTS, VIDEO_EXTS
from .decode import _decode_stats
from .h264 import _find_inband_params, _scan_mdat_for_nals
from .mp4 import _get_mvhd_duration, parse_boxes
from .tools import _ffprobe_path, has_ffmpeg, has_ffprobe


def analyze_mdat(filepath, mdat_offset, mdat_size):
    """Scan first portion of mdat for codec information.

    Returns dict with:
      codec: 'h264' | 'h265' | 'unknown'
      nal_count: number of NAL start codes found
      has_sps: bool
      has_pps: bool
      has_idr: bool
      resolution: (w, h) or None (from SPS if found)
    """
    result = {
        'codec': 'unknown',
        'nal_count': 0,
        'has_sps': False,
        'has_pps': False,
        'has_idr': False,
        'resolution': None,
    }

    scan_len = min(mdat_size, 2 * 1024 * 1024)
    data_offset = mdat_offset + 8  # skip mdat header

    with open(filepath, 'rb') as f:
        f.seek(data_offset)
        data = f.read(scan_len)

    if len(data) < 8:
        return result

    # Check if mdat uses length-prefixed NALs (MP4 style) vs start-code (Annex B)
    # MP4 style: 4-byte big-endian length + NAL data
    # Annex B: 00 00 00 01 + NAL data
    is_annex_b = False
    nal_count = 0
    has_sps = False
    has_pps = False
    has_idr = False
    codec = 'unknown'

    best = None
    if len(data) >= 8:
        # MP4-style length-prefixed NALs first, using the recovery scanner so the
        # codec shown matches what recovery will find. A looser two-NAL check
        # here labeled H.264 GoPro footage as H.265.
        window = min(mdat_size, scan_len + 16)
        for scan_codec in ('h264', 'hevc'):
            nals = _scan_mdat_for_nals(filepath, mdat_offset, window, 4,
                                       codec=scan_codec)
            if len(nals) >= 3 and (best is None or len(nals) > len(best[1])):
                best = (scan_codec, nals)
        if best:
            scan_codec, nals = best
            codec = 'h265' if scan_codec == 'hevc' else 'h264'
            nal_count = len(nals)
            idr_types = {19, 20} if scan_codec == 'hevc' else {5}
            with open(filepath, 'rb') as f:
                for offset, _ in nals:
                    f.seek(offset)
                    b = f.read(1)[0]
                    if ((b >> 1) & 0x3F if scan_codec == 'hevc' else b & 0x1F) in idr_types:
                        has_idr = True
                        break
            # SPS/PPS the file carries itself (in the stream or a camera header)
            has_sps = has_pps = _find_inband_params(
                filepath, mdat_offset, nals, codec=scan_codec) is not None

    # Annex B start codes only if that found nothing: 00 00 00 01 also
    # occurs by chance in length-prefixed data (GoPro's header has it)
    i = 0
    while best is None and i < len(data) - 4:
        if data[i:i + 4] == b'\x00\x00\x00\x01':
            nal_byte = data[i + 4]
            # H.264: nal_unit_type is lower 5 bits
            h264_type = nal_byte & 0x1F
            # H.265: nal_unit_type is bits 1-6 of first byte
            h265_type = (nal_byte >> 1) & 0x3F

            if h264_type in (1, 5, 6, 7, 8, 9):
                codec = 'h264'
                if h264_type == 7:
                    has_sps = True
                elif h264_type == 8:
                    has_pps = True
                elif h264_type == 5:
                    has_idr = True
            elif h265_type in (0, 1, 19, 20, 32, 33, 34, 35):
                codec = 'h265'
                if h265_type == 33:
                    has_sps = True
                elif h265_type == 34:
                    has_pps = True
                elif h265_type in (19, 20):
                    has_idr = True

            nal_count += 1
            is_annex_b = True
            i += 4
        else:
            i += 1

    result['codec'] = codec
    result['nal_count'] = nal_count
    result['has_sps'] = has_sps
    result['has_pps'] = has_pps
    result['has_idr'] = has_idr

    return result


# ─── Video file checker ──────────────────────────────────────────────────────

def _walk_avi(filepath):
    """Walk an AVI's movi list and report where valid chunks stop.

    Returns a dict (total_frames from avih, movi_list offset, movi_end,
    good_end, video_chunks, audio_chunks, ends_with: 'complete' | 'eof' |
    'invalid') or None if there is no movi list. Every movi chunk is a stream
    tag like 00dc (video) or 01wb (audio) plus a size, so the first chunk with
    an invalid tag or a size past the end of the file is where the data stops
    being this video.
    """
    import mmap
    import re
    valid_id = re.compile(rb'(\d\d(dc|db|wb|pc|tx)|ix\d\d|JUNK)$')
    with open(filepath, 'rb') as f:
        if os.fstat(f.fileno()).st_size < 12:
            return None
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        size = len(m)
        total_frames = None
        avih = m.find(b'avih', 12, min(size, 65536))
        if avih != -1 and avih + 32 <= size:
            total_frames = struct.unpack_from('<I', m, avih + 24)[0]
        # Find the top-level LIST movi
        pos, movi = 12, None
        while pos + 12 <= size:
            cid = m[pos:pos + 4]
            csize = struct.unpack_from('<I', m, pos + 4)[0]
            if cid == b'LIST' and m[pos + 8:pos + 12] == b'movi':
                movi = pos
                break
            if csize == 0 and cid != b'LIST':
                return None
            pos += 8 + csize + (csize & 1)
        if movi is None:
            return None
        movi_end = movi + 8 + struct.unpack_from('<I', m, movi + 4)[0]
        pos = movi + 12
        good_end, last_start = pos, None
        video = audio = 0
        ends_with = 'complete'
        while pos + 8 <= min(movi_end, size):
            cid = m[pos:pos + 4]
            csize = struct.unpack_from('<I', m, pos + 4)[0]
            if cid == b'LIST':
                pos += 12          # descend into 'rec ' lists
                continue
            if not valid_id.match(cid):
                ends_with = 'invalid'
                break
            if pos + 8 + csize > size:
                ends_with = 'eof'
                break
            if cid[2:] in (b'dc', b'db'):
                video += 1
            elif cid[2:] == b'wb':
                audio += 1
            last_start = pos
            pos += 8 + csize + (csize & 1)
            good_end = min(pos, size)
        else:
            if pos < movi_end:
                ends_with = 'eof'
        if ends_with == 'invalid' and last_start is not None:
            # The chunk before non-video data may itself run into that data
            good_end = last_start
            last_tag = m[last_start + 2:last_start + 4]
            if last_tag in (b'dc', b'db'):
                video -= 1
            elif last_tag == b'wb':
                audio -= 1
        return {'total_frames': total_frames, 'movi_list': movi,
                'movi_end': movi_end, 'good_end': good_end,
                'video_chunks': video, 'audio_chunks': audio,
                'ends_with': ends_with}
    finally:
        m.close()


def _check_other_format(filepath, details):
    """Basic check for formats without a dedicated parser (MKV, MPEG, TS...).

    Reports files ffmpeg can't open or decode; recovery isn't supported.
    """
    import re
    details['unrecoverable'] = True
    if not has_ffprobe():
        return 'ok', None, details
    try:
        r = subprocess.run(
            [_ffprobe_path, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', filepath],
            capture_output=True, timeout=30)
        err = r.stderr.decode(errors='replace').strip().splitlines()
        if r.returncode != 0:
            return ('corrupt',
                    'Unreadable: ' + (err[-1].split(': ', 1)[-1][:120] if err
                                      else 'ffprobe could not open the file')
                    + '. Recovery is not supported for this format.',
                    details)
        try:
            details['duration'] = round(float(r.stdout.decode().strip()), 2)
        except ValueError:
            pass
        # A readable header with no frames behind it decodes without errors
        pk = subprocess.run(
            [_ffprobe_path, '-v', 'quiet', '-select_streams', 'v:0',
             '-read_intervals', '%+#30', '-count_packets', '-show_entries',
             'stream=nb_read_packets', '-of', 'csv=p=0', filepath],
            capture_output=True, timeout=30)
        counts = [int(t) for t in re.findall(rb'\d+', pk.stdout)]
        if not counts or max(counts) == 0:
            return ('corrupt',
                    'No readable video frames. '
                    'Recovery is not supported for this format.',
                    details)
        if has_ffmpeg():
            problems, _ = _decode_stats(['-t', '3', '-i', filepath], timeout=60)
            if problems:
                return ('corrupt',
                        f'Decode errors in the first seconds ({problems}). '
                        'Recovery is not supported for this format.',
                        details)
    except subprocess.TimeoutExpired:
        pass
    return 'ok', None, details


def _check_avi(filepath, file_size, head, details):
    """Integrity check for AVI (RIFF) files. Recovery not supported."""
    details['unrecoverable'] = True

    # Validate RIFF/AVI signature
    if len(head) < 12 or head[:4] != b'RIFF' or head[8:12] != b'AVI ':
        if len(head) >= 4 and head[:4] == b'RIFF':
            return ('corrupt', 'RIFF file but not a valid AVI container', details)
        return ('corrupt', 'Not a valid AVI file — missing RIFF/AVI header', details)

    # Walk the frame chunks: the header and a quick ffprobe read look fine
    # when only the first part of the frames survived (carved files, or a
    # recording that stopped), so count what is really there
    try:
        walk = _walk_avi(filepath)
    except Exception:
        walk = None
    if walk and walk['ends_with'] != 'complete':
        frames = walk['video_chunks']
        total = walk['total_frames']
        details['avi_walk'] = walk
        of_total = f' of {total:,}' if total and total >= frames else ''
        if frames > 0:
            details['unrecoverable'] = False
            details['salvageable'] = True
        if walk['ends_with'] == 'eof':
            return ('truncated',
                    f'AVI file is cut short: {frames:,}{of_total} video frames '
                    f'are intact before the file ends',
                    details)
        return ('corrupt',
                f'AVI frame data breaks off after {frames:,}{of_total} video '
                f'frames; the rest of the file is not video data',
                details)

    # Check for truncation via RIFF chunk size (little-endian at bytes 4-7)
    riff_data_size = struct.unpack('<I', head[4:8])[0]
    declared_size = riff_data_size + 8
    if declared_size > file_size + 8:
        details['declared_size'] = declared_size
        return ('truncated',
                f'AVI file is truncated — declares {declared_size:,} bytes but '
                f'file is only {file_size:,} bytes',
                details)

    # Use ffprobe to detect decode errors if available
    if has_ffprobe():
        try:
            r = subprocess.run(
                [_ffprobe_path, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', filepath],
                capture_output=True, timeout=15)
            err = r.stderr.decode(errors='replace').strip()
            if r.returncode != 0 or err:
                return ('corrupt',
                        f'AVI file is unreadable or corrupt: {err[:120]}' if err
                        else 'AVI file could not be read by ffprobe',
                        details)
            # Try to extract duration
            try:
                dur = float(r.stdout.decode().strip())
                if dur > 0:
                    details['duration'] = round(dur, 2)
            except (ValueError, AttributeError):
                pass
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

    return 'ok', None, details


HEIF_IMAGE_BRANDS = {'heic', 'heix', 'heim', 'heis', 'mif1', 'mif2', 'avif'}


def _is_heif_image(filepath, boxes, ftyp_brand):
    """True if a moov-less ISO BMFF file is a HEIF/HEIC/AVIF still image.

    Photos libraries can store still images under a .mov name. These share
    the MP4 box layout (ftyp + meta + mdat) but have no moov by design.
    """
    if ftyp_brand in HEIF_IMAGE_BRANDS:
        return True
    # Fall back to the meta box's handler: 'pict' marks a still image
    for bt, off, sz in boxes:
        if bt == 'meta':
            with open(filepath, 'rb') as f:
                f.seek(off + 12)  # skip box header + FullBox version/flags
                hdlr = f.read(20)
            return hdlr[4:8] == b'hdlr' and hdlr[16:20] == b'pict'
    return False


class ScanCancelled(Exception):
    pass


def _find_intact_twin(filepath, file_size):
    """Path of another video in the same folder with the same size and an ftyp header."""
    fdir = os.path.dirname(filepath)
    try:
        names = os.listdir(fdir)
    except OSError:
        return None
    for name in names:
        other = os.path.join(fdir, name)
        if (other == filepath or
                os.path.splitext(name)[1].lower() not in VIDEO_EXTS):
            continue
        try:
            if os.path.getsize(other) != file_size:
                continue
            with open(other, 'rb') as f:
                if f.read(8)[4:8] == b'ftyp':
                    return other
        except OSError:
            continue
    return None


def _no_structure_result(filepath, file_size, details):
    """check_video result for a file with no MP4 boxes and a non-zeroed header.

    Neither recovery engine has an mdat to work from, so mark it unrecoverable.
    """
    details['unrecoverable'] = True
    twin = _find_intact_twin(filepath, file_size)
    if twin:
        # Messages keeps an encrypted copy next to each received video
        details['intact_copy'] = twin
        return ('corrupt',
                'No video structure; appears to be an encrypted copy. An intact '
                f'copy of the same size is in this folder: {os.path.basename(twin)}',
                details)
    return ('corrupt',
            'No valid MP4/MOV container structure found; the file may be '
            'encrypted or not a video',
            details)


def check_video(filepath, should_stop=None):
    """Check a video file for integrity issues.

    Returns (status, reason, details_dict).
    Statuses: ok, no_moov, corrupt, truncated, empty, error, not_video
    should_stop: optional callable; when it returns True, the full-file read
    loops raise ScanCancelled so a cancelled scan doesn't wait on large files.
    """
    details = {}
    try:
        file_size = os.path.getsize(filepath)
        details['file_size'] = file_size

        if file_size == 0:
            return 'empty', 'File is empty (0 bytes)', details

        if file_size < 8:
            return 'corrupt', f'File too small ({file_size} bytes)', details

        # Check for leading zeros
        with open(filepath, 'rb') as f:
            head = f.read(min(64, file_size))

        all_zero = all(b == 0 for b in head[:8])
        if all_zero:
            # Find where zeros end
            zero_end = 0
            with open(filepath, 'rb') as f:
                chunk_size = 65536
                offset = 0
                while offset < file_size:
                    if should_stop and should_stop():
                        raise ScanCancelled()
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    rest = chunk.lstrip(b'\x00')
                    if rest:
                        zero_end = offset + len(chunk) - len(rest)
                        break
                    offset += len(chunk)

            if zero_end == 0:
                details['unrecoverable'] = True
                return 'corrupt', 'File is entirely zeros', details

            # Check how much actual data survives in the rest of the file.
            # Sample blocks after zero_end to detect scattered-zero files
            # where almost all data is gone.
            nonzero_bytes = 0
            with open(filepath, 'rb') as f:
                sample_pos = zero_end
                while sample_pos < file_size:
                    if should_stop and should_stop():
                        raise ScanCancelled()
                    f.seek(sample_pos)
                    blk = f.read(65536)
                    if not blk:
                        break
                    nonzero_bytes += len(blk) - blk.count(0)
                    sample_pos += len(blk)

            zero_pct = zero_end / file_size * 100
            details['zero_bytes'] = zero_end
            details['zero_pct'] = round(zero_pct, 1)
            surviving = file_size - zero_end
            details['surviving_bytes'] = surviving
            details['nonzero_bytes'] = nonzero_bytes

            if nonzero_bytes < file_size * 0.05 or nonzero_bytes < 65536:
                details['unrecoverable'] = True
                return ('corrupt',
                        f'File is effectively all zeros — only '
                        f'{nonzero_bytes:,} bytes of data remain '
                        f'in a {file_size:,} byte file.',
                        details)

            nz_pct = round(nonzero_bytes / file_size * 100, 1)
            return ('corrupt',
                    f'Leading zeros — first {zero_end:,} bytes ({zero_pct:.0f}%) '
                    f'are zeroed. {nonzero_bytes:,} bytes ({nz_pct}%) of '
                    f'actual data remain.',
                    details)

        # Route by signature first: carved and renamed files often carry
        # the wrong extension
        ext = Path(filepath).suffix.lower()
        if head[:4] == b'RIFF' and head[8:12] == b'AVI ':
            return _check_avi(filepath, file_size, head, details)
        if head[4:8] != b'ftyp':
            if ext == '.avi':
                return _check_avi(filepath, file_size, head, details)
            if ext in OTHER_VIDEO_EXTS:
                return _check_other_format(filepath, details)

        # Parse MP4 boxes
        boxes = parse_boxes(filepath)
        if not boxes:
            return _no_structure_result(filepath, file_size, details)

        details['boxes'] = [(bt, off, sz) for bt, off, sz in boxes]

        has_ftyp = False
        has_moov = False
        has_mdat = False
        mdat_offset = 0
        mdat_size = 0
        ftyp_brand = ''

        for bt, off, sz in boxes:
            if bt == 'ftyp':
                has_ftyp = True
                with open(filepath, 'rb') as f:
                    f.seek(off + 8)
                    brand_bytes = f.read(4)
                    ftyp_brand = brand_bytes.decode('ascii', errors='replace').strip()
                details['ftyp_brand'] = ftyp_brand
            elif bt == 'moov':
                has_moov = True
            elif bt == 'mdat':
                has_mdat = True
                mdat_offset = off
                mdat_size = sz
                details['mdat_offset'] = mdat_offset
                details['mdat_size'] = mdat_size

        details['has_ftyp'] = has_ftyp
        details['has_moov'] = has_moov
        details['has_mdat'] = has_mdat

        if has_moov:
            dur = _get_mvhd_duration(filepath)
            if dur is not None:
                details['duration'] = dur

        if has_moov and has_mdat:
            # Check if mdat extends beyond file
            if mdat_offset + mdat_size > file_size + 8:
                details['mdat_expected'] = mdat_offset + mdat_size
                return ('truncated',
                        f'File is truncated — mdat claims {mdat_size:,} bytes '
                        f'but file ends at {file_size:,}',
                        details)
            return 'ok', None, details

        if not has_moov and _is_heif_image(filepath, boxes, ftyp_brand):
            return ('not_video',
                    'HEIF/HEIC still image with a video file extension',
                    details)

        if has_mdat and not has_moov:
            # Check if mdat content is mostly zeros (valid header but wiped data)
            mdat_data_start = mdat_offset + 8
            nonzero_bytes = 0
            with open(filepath, 'rb') as f:
                sample_pos = mdat_data_start
                mdat_end = mdat_offset + mdat_size
                while sample_pos < mdat_end:
                    if should_stop and should_stop():
                        raise ScanCancelled()
                    f.seek(sample_pos)
                    blk = f.read(65536)
                    if not blk:
                        break
                    nonzero_bytes += len(blk) - blk.count(0)
                    sample_pos += len(blk)

            actual_mdat_data = mdat_size - 8
            if actual_mdat_data > 0 and (
                    nonzero_bytes < actual_mdat_data * 0.10
                    or nonzero_bytes < 65536):
                details['unrecoverable'] = True
                details['nonzero_bytes'] = nonzero_bytes
                return ('corrupt',
                        f'File has valid header but mdat is effectively all zeros '
                        f'— only {nonzero_bytes:,} bytes of data remain '
                        f'in {actual_mdat_data:,} bytes of mdat.',
                        details)

            mdat_info = analyze_mdat(filepath, mdat_offset, mdat_size)
            details['mdat_analysis'] = mdat_info
            codec_str = mdat_info['codec'].upper()
            if codec_str == 'UNKNOWN':
                codec_str = 'unknown codec'
            reason = (f'No moov atom (metadata/index) — file is unplayable. '
                      f'Video data ({codec_str}) is present in mdat '
                      f'({mdat_size:,} bytes).')
            if mdat_info['has_sps']:
                reason += ' Embedded SPS/PPS found.'
            return 'no_moov', reason, details

        if has_ftyp and not has_mdat and not has_moov:
            return ('corrupt',
                    'Has ftyp but no moov or mdat — file is just a header stub',
                    details)

        if not has_ftyp and not has_moov and not has_mdat:
            if all(bt in ('free', 'skip', 'wide') for bt, _, _ in boxes):
                details['unrecoverable'] = True
                return ('empty',
                        'Contains only padding (free/skip boxes); no video data',
                        details)
            return _no_structure_result(filepath, file_size, details)

        return 'ok', None, details

    except ScanCancelled:
        raise
    except PermissionError:
        return 'error', 'Permission denied reading file', details
    except OSError as e:
        return 'error', f'OS error: {e}', details
    except Exception as e:
        return 'error', f'Unexpected error: {e}', details
