#!/usr/bin/env python3
"""
Frayme
Recursively scans directories for corrupt or damaged MP4/MOV video files.
Detects: missing moov atoms, leading-zero corruption, truncated files,
and files with no valid container structure.
Offers best-effort recovery via moov transplant and raw stream extraction.
"""

import os
import sys
import json
import time
import struct
import threading
import queue as queue_mod
import csv
import io
import subprocess
import webbrowser
import tempfile
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── Global state ─────────────────────────────────────────────────────────────

scan_state = {
    'running': False,
    'cancelled': False,
    'results': [],
}

# Per-directory donor map: { dir_path: filepath } (manual override)
donor_map = {}

# Auto-collected healthy files: { dir_path: [filepath, ...] }
donor_pool = {}

# Recovery queue infrastructure
_recover_queue = queue_mod.Queue()
_recover_jobs = {}
_recover_lock = threading.Lock()
_recover_counter = 0
_recover_worker_started = False

VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.m4a', '.3gp', '.3g2', '.avi'}

VALID_BOX_TYPES = {
    b'ftyp', b'moov', b'mdat', b'free', b'skip', b'wide', b'pnot',
    b'uuid', b'moof', b'mfra', b'meta', b'styp', b'sidx', b'ssix',
    b'pdin', b'bloc', b'meco',
}

WORKER_THREADS = 4

# ─── ffmpeg / ffprobe detection ───────────────────────────────────────────────

def _find_tool(name):
    return shutil.which(name)

_ffmpeg_path = _find_tool('ffmpeg')
_ffprobe_path = _find_tool('ffprobe')

def has_ffmpeg():
    return _ffmpeg_path is not None

def has_ffprobe():
    return _ffprobe_path is not None


# ─── MP4/MOV box parser (pure Python) ────────────────────────────────────────

def parse_boxes(filepath, max_depth=0):
    """Parse top-level MP4/MOV boxes.

    Returns [(type_str, offset, size), ...] for each box found.
    Stops at first invalid box or EOF.
    """
    file_size = os.path.getsize(filepath)
    boxes = []
    with open(filepath, 'rb') as f:
        pos = 0
        while pos < file_size:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            box_size = struct.unpack('>I', hdr[:4])[0]
            box_type = hdr[4:8]

            if box_size == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    break
                box_size = struct.unpack('>Q', ext)[0]
                if box_size < 16:
                    break
            elif box_size == 0:
                box_size = file_size - pos
            elif box_size < 8:
                break

            try:
                bt = box_type.decode('ascii')
            except (UnicodeDecodeError, ValueError):
                break

            if not all(c.isalnum() or c in ' _-' for c in bt):
                break

            boxes.append((bt, pos, box_size))
            pos += box_size
    return boxes


def _get_mvhd_duration(filepath):
    """Extract video duration in seconds from the moov/mvhd box.

    Parses the binary mvhd directly — no ffprobe needed.
    Returns float seconds or None.
    """
    boxes = parse_boxes(filepath)
    moov_offset = None
    moov_size = None
    for bt, off, sz in boxes:
        if bt == 'moov':
            moov_offset = off
            moov_size = sz
            break
    if moov_offset is None:
        return None
    try:
        with open(filepath, 'rb') as f:
            f.seek(moov_offset + 8)
            moov_data = f.read(min(moov_size - 8, 1024))
        # Find mvhd child box
        pos = 0
        while pos < len(moov_data) - 8:
            sz = struct.unpack('>I', moov_data[pos:pos + 4])[0]
            bt = moov_data[pos + 4:pos + 8]
            if sz < 8 or pos + sz > len(moov_data):
                break
            if bt == b'mvhd':
                version = moov_data[pos + 8]
                if version == 0:
                    timescale = struct.unpack('>I',
                        moov_data[pos + 20:pos + 24])[0]
                    duration = struct.unpack('>I',
                        moov_data[pos + 24:pos + 28])[0]
                else:
                    timescale = struct.unpack('>I',
                        moov_data[pos + 28:pos + 32])[0]
                    duration = struct.unpack('>Q',
                        moov_data[pos + 32:pos + 40])[0]
                if timescale > 0:
                    return round(duration / timescale, 2)
                return None
            pos += sz
    except Exception:
        pass
    return None


def parse_moov_children(data, offset=0):
    """Parse child boxes inside a moov atom. Returns dict of type -> bytes."""
    children = {}
    pos = offset
    while pos < len(data) - 8:
        box_size = struct.unpack('>I', data[pos:pos + 4])[0]
        box_type = data[pos + 4:pos + 8]
        if box_size < 8 or pos + box_size > len(data):
            break
        try:
            bt = box_type.decode('ascii')
        except (UnicodeDecodeError, ValueError):
            break
        children[bt] = data[pos:pos + box_size]
        pos += box_size
    return children


def find_box_in_data(data, box_type_str):
    """Find a box by type in binary data. Returns (offset, size) or None."""
    target = box_type_str.encode('ascii')
    pos = 0
    while pos < len(data) - 8:
        box_size = struct.unpack('>I', data[pos:pos + 4])[0]
        box_type = data[pos + 4:pos + 8]
        if box_size < 8 or pos + box_size > len(data):
            break
        if box_type == target:
            return pos, box_size
        pos += box_size
    return None


def _extract_moov(filepath):
    """Extract raw moov atom bytes from a video file. Returns bytes or None."""
    boxes = parse_boxes(filepath)
    for bt, offset, size in boxes:
        if bt == 'moov':
            with open(filepath, 'rb') as f:
                f.seek(offset)
                return f.read(size)
    return None


def _extract_ftyp(filepath):
    """Extract raw ftyp atom bytes from a video file. Returns bytes or None."""
    boxes = parse_boxes(filepath)
    for bt, offset, size in boxes:
        if bt == 'ftyp':
            with open(filepath, 'rb') as f:
                f.seek(offset)
                return f.read(size)
    return None


def _walk_box_children(data):
    """Iterate over child boxes, yielding (type_bytes, offset, size)."""
    pos = 0
    while pos < len(data) - 8:
        sz = struct.unpack('>I', data[pos:pos + 4])[0]
        bt = data[pos + 4:pos + 8]
        if sz < 8 or pos + sz > len(data):
            break
        yield bt, pos, sz
        pos += sz


def _navigate_to_stsd(moov_data, sample_entry_types):
    """Navigate moov > trak > mdia > minf > stbl > stsd and find a sample entry.

    sample_entry_types: set of 4-byte box types to look for (e.g. {b'avc1', b'avc3'}).
    Returns (entry_data_after_header, entry_box_size) or (None, 0).
    """
    moov_body = moov_data[8:]
    for bt, tpos, tsz in _walk_box_children(moov_body):
        if bt != b'trak':
            continue
        trak_body = moov_body[tpos + 8:tpos + tsz]
        path = [b'mdia', b'minf', b'stbl', b'stsd']
        cur = trak_body
        found = True
        for target in path:
            hit = False
            for cbt, cpos, csz in _walk_box_children(cur):
                if cbt == target:
                    cur = cur[cpos + 8:cpos + csz]
                    hit = True
                    break
            if not hit:
                found = False
                break
        if not found:
            continue
        if len(cur) < 16:
            continue
        stsd_body = cur[8:]
        for etype, epos, esz in _walk_box_children(stsd_body):
            if etype in sample_entry_types:
                entry_data = stsd_body[epos + 8:epos + esz]
                if len(entry_data) >= 78:
                    return entry_data[78:], esz
    return None, 0


def _extract_avcc_params(moov_data):
    """Extract SPS/PPS NAL units from the avcC box inside a moov atom.

    Navigates: moov > trak(video) > mdia > minf > stbl > stsd > avc1 > avcC.
    Returns dict with 'sps_list', 'pps_list', 'nal_length_size' or None.
    """
    ext_data, _ = _navigate_to_stsd(moov_data, {b'avc1', b'avc3'})
    if ext_data is None:
        return None

    for xbt, xpos, xsz in _walk_box_children(ext_data):
        if xbt != b'avcC':
            continue
        avcc = ext_data[xpos + 8:xpos + xsz]
        if len(avcc) < 7:
            return None
        profile = avcc[1]
        level = avcc[3]
        nal_length_size = (avcc[4] & 0x03) + 1
        num_sps = avcc[5] & 0x1F
        off = 6
        sps_list = []
        for _ in range(num_sps):
            if off + 2 > len(avcc):
                break
            sps_len = struct.unpack('>H', avcc[off:off + 2])[0]
            off += 2
            if off + sps_len > len(avcc):
                break
            sps_list.append(bytes(avcc[off:off + sps_len]))
            off += sps_len
        if off >= len(avcc):
            return None
        num_pps = avcc[off]
        off += 1
        pps_list = []
        for _ in range(num_pps):
            if off + 2 > len(avcc):
                break
            pps_len = struct.unpack('>H', avcc[off:off + 2])[0]
            off += 2
            if off + pps_len > len(avcc):
                break
            pps_list.append(bytes(avcc[off:off + pps_len]))
            off += pps_len
        return {
            'sps_list': sps_list,
            'pps_list': pps_list,
            'nal_length_size': nal_length_size,
            'profile': profile,
            'level': level,
        }
    return None


def _extract_hvcc_params(moov_data):
    """Extract VPS/SPS/PPS NAL units from the hvcC box inside a moov atom.

    Navigates: moov > trak(video) > mdia > minf > stbl > stsd > hvc1/hev1 > hvcC.
    Returns dict with 'vps_list', 'sps_list', 'pps_list', 'nal_length_size' or None.
    """
    ext_data, _ = _navigate_to_stsd(moov_data, {b'hvc1', b'hev1'})
    if ext_data is None:
        return None

    for xbt, xpos, xsz in _walk_box_children(ext_data):
        if xbt != b'hvcC':
            continue
        hvcc = ext_data[xpos + 8:xpos + xsz]
        if len(hvcc) < 23:
            return None
        nal_length_size = (hvcc[21] & 0x03) + 1
        num_arrays = hvcc[22]
        off = 23
        vps_list = []
        sps_list = []
        pps_list = []
        for _ in range(num_arrays):
            if off + 3 > len(hvcc):
                break
            nal_type = hvcc[off] & 0x3F
            num_nalus = struct.unpack('>H', hvcc[off + 1:off + 3])[0]
            off += 3
            for _ in range(num_nalus):
                if off + 2 > len(hvcc):
                    break
                nal_len = struct.unpack('>H', hvcc[off:off + 2])[0]
                off += 2
                if off + nal_len > len(hvcc):
                    break
                nal_data = bytes(hvcc[off:off + nal_len])
                off += nal_len
                if nal_type == 32:
                    vps_list.append(nal_data)
                elif nal_type == 33:
                    sps_list.append(nal_data)
                elif nal_type == 34:
                    pps_list.append(nal_data)
        if sps_list:
            return {
                'vps_list': vps_list,
                'sps_list': sps_list,
                'pps_list': pps_list,
                'nal_length_size': nal_length_size,
            }
    return None


def _scan_for_moov(filepath, start_offset):
    """Scan a file for a valid moov atom starting from start_offset.

    Searches for the 'moov' signature and validates the box has
    recognizable children (mvhd, trak).
    Returns (moov_offset, moov_size) or None.
    """
    file_size = os.path.getsize(filepath)
    search_size = file_size - start_offset
    if search_size < 16:
        return None

    target = b'moov'
    chunk_size = 256 * 1024
    with open(filepath, 'rb') as f:
        offset = start_offset
        while offset < file_size - 8:
            f.seek(offset)
            chunk = f.read(min(chunk_size, file_size - offset))
            if len(chunk) < 8:
                break
            # Search for 'moov' in this chunk
            pos = 0
            while pos < len(chunk) - 8:
                idx = chunk.find(target, pos + 4)
                if idx == -1 or idx < 4:
                    break
                # The 4 bytes before 'moov' should be the box size
                box_offset = offset + idx - 4
                size_bytes = chunk[idx - 4:idx]
                box_size = struct.unpack('>I', size_bytes)[0]

                if box_size < 16 or box_offset + box_size > file_size:
                    pos = idx + 1
                    continue

                # Read and validate moov children
                f.seek(box_offset)
                moov_data = f.read(min(box_size, 4096))
                if len(moov_data) < 16:
                    pos = idx + 1
                    continue

                # Check for mvhd or trak as first child
                child_pos = 8
                has_valid_child = False
                while child_pos < len(moov_data) - 8:
                    csz = struct.unpack('>I', moov_data[child_pos:child_pos + 4])[0]
                    ctype = moov_data[child_pos + 4:child_pos + 8]
                    if csz < 8:
                        break
                    if ctype in (b'mvhd', b'trak', b'udta', b'meta'):
                        has_valid_child = True
                        break
                    child_pos += csz

                if has_valid_child:
                    return (box_offset, box_size)
                pos = idx + 1
            offset += len(chunk) - 8  # overlap to avoid missing split signatures

    return None


def _scan_mdat_for_nals(filepath, mdat_offset, mdat_size,
                        nal_length_size=4, raw_data=False, codec='h264'):
    """Scan mdat for length-prefixed NAL units, skipping audio chunks.

    Supports both H.264 and HEVC codecs. Uses buffered file I/O.
    Returns list of (file_offset, nal_length) tuples where file_offset
    points to the NAL data (after the length prefix).

    If raw_data=True, mdat_offset points directly at NAL data (no mdat box
    header to skip). Used for leading-zero recovery where the box header
    was destroyed.
    """
    if codec == 'hevc':
        VALID_TYPES = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9,
                       16, 17, 18, 19, 20, 21}
    else:
        VALID_TYPES = {1, 2, 3, 4, 5, 6}  # slices + SEI
    MAX_NAL = 20 * 1024 * 1024  # 20MB sanity limit

    # Determine mdat data range
    if raw_data:
        data_start = mdat_offset
    else:
        # Skip box header
        with open(filepath, 'rb') as f:
            f.seek(mdat_offset)
            hdr = f.read(8)
            box_sz = struct.unpack('>I', hdr[:4])[0]
            if box_sz == 1:
                data_start = mdat_offset + 16
            else:
                data_start = mdat_offset + 8
    data_end = mdat_offset + mdat_size

    nal_entries = []

    def _is_valid_nal(fh, pos):
        """Read length prefix + NAL header and check validity."""
        header_size = 2 if codec == 'hevc' else 1
        if pos + nal_length_size + header_size > data_end:
            return None
        fh.seek(pos)
        lb = fh.read(nal_length_size + header_size)
        if len(lb) < nal_length_size + header_size:
            return None
        if nal_length_size == 4:
            nal_len = struct.unpack('>I', lb[:4])[0]
        elif nal_length_size == 2:
            nal_len = struct.unpack('>H', lb[:2])[0]
        else:
            nal_len = lb[0]
        if nal_len < header_size or nal_len > MAX_NAL:
            return None
        if pos + nal_length_size + nal_len > data_end:
            return None
        nal_byte = lb[nal_length_size]
        if (nal_byte >> 7) & 1:  # forbidden_zero_bit
            return None
        if codec == 'hevc':
            nal_type = (nal_byte >> 1) & 0x3F
            # Validate HEVC 2-byte header: layer_id must be 0,
            # nuh_temporal_id_plus1 must be >= 1
            nal_byte2 = lb[nal_length_size + 1]
            layer_id = ((nal_byte & 1) << 5) | ((nal_byte2 >> 3) & 0x1F)
            tid = nal_byte2 & 0x07
            if layer_id != 0 or tid < 1:
                return None
        else:
            nal_type = nal_byte & 0x1F
        if nal_type not in VALID_TYPES:
            return None
        return nal_len

    def _find_next_chain(fh, start):
        """Search forward for a position with 2+ consecutive valid NALs."""
        CHUNK = 65536
        pos = start
        while pos < data_end - 8:
            fh.seek(pos)
            buf = fh.read(min(CHUNK, data_end - pos))
            if len(buf) < nal_length_size + 1:
                break
            for i in range(len(buf) - nal_length_size - 1):
                cand = pos + i
                nl = _is_valid_nal(fh, cand)
                if nl is None:
                    continue
                # Validate second NAL in chain
                nl2 = _is_valid_nal(fh, cand + nal_length_size + nl)
                if nl2 is not None:
                    return cand
            pos += len(buf) - 8  # overlap
        return None

    with open(filepath, 'rb') as fh:
        pos = data_start
        while pos < data_end - nal_length_size:
            nl = _is_valid_nal(fh, pos)
            if nl is not None:
                nal_entries.append((pos + nal_length_size, nl))
                pos += nal_length_size + nl
            else:
                nxt = _find_next_chain(fh, pos + 1)
                if nxt is None:
                    break
                pos = nxt

    return nal_entries


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
    for pool_file in donor_pool.get(fdir, []):
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


def _probe_sps_candidates(filepath, nal_entries, candidates, codec='h264'):
    """Try each (vps_list, sps_list, pps_list) candidate against the NAL data.

    Writes a short test stream (first IDR + a few frames) with each
    candidate, decodes it with ffmpeg, and returns the candidate
    with the fewest decode errors.
    Returns (best_vps_list, best_sps_list, best_pps_list).
    """
    if not candidates or not has_ffmpeg():
        return candidates[0] if candidates else ([], [], [])

    if len(candidates) == 1:
        return candidates[0]

    idr_types = {19, 20} if codec == 'hevc' else {5}
    ffmpeg_fmt = 'hevc' if codec == 'hevc' else 'h264'

    # Find first IDR and collect a short segment (IDR + up to 10 frames)
    test_nals = []
    found_idr = False
    with open(filepath, 'rb') as f:
        for offset, length in nal_entries:
            f.seek(offset)
            b = f.read(1)
            if not b:
                continue
            if codec == 'hevc':
                nal_type = (b[0] >> 1) & 0x3F
            else:
                nal_type = b[0] & 0x1F
            if not found_idr:
                if nal_type in idr_types:
                    found_idr = True
                else:
                    continue
            test_nals.append((offset, length))
            if len(test_nals) >= 15:
                break

    if not test_nals:
        return candidates[0]

    import tempfile
    START_CODE = b'\x00\x00\x00\x01'
    best = candidates[0]
    best_errors = float('inf')

    for vps_list, sps_list, pps_list in candidates:
        try:
            with tempfile.NamedTemporaryFile(suffix='.' + ffmpeg_fmt,
                                             delete=False) as tmp:
                tmp_path = tmp.name
                for vps in vps_list:
                    tmp.write(START_CODE + vps)
                for sps in sps_list:
                    tmp.write(START_CODE + sps)
                for pps in pps_list:
                    tmp.write(START_CODE + pps)
                with open(filepath, 'rb') as src:
                    for nal_off, nal_len in test_nals:
                        tmp.write(START_CODE)
                        src.seek(nal_off)
                        remaining = nal_len
                        while remaining > 0:
                            chunk = src.read(min(65536, remaining))
                            if not chunk:
                                break
                            tmp.write(chunk)
                            remaining -= len(chunk)

            result = subprocess.run(
                [_ffmpeg_path, '-v', 'error', '-f', ffmpeg_fmt,
                 '-i', tmp_path, '-f', 'null', '-'],
                capture_output=True, timeout=10)
            err_count = result.stderr.decode(errors='replace').count(
                'error while decoding')

            if err_count < best_errors:
                best_errors = err_count
                best = (vps_list, sps_list, pps_list)
            if err_count == 0:
                break
        except Exception:
            continue
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return best


def _write_annex_b_stream(filepath, nal_entries, sps_list, pps_list,
                          output_path, vps_list=None, codec='h264'):
    """Write NAL units as an Annex-B stream (.h264 or .hevc).

    Skips NALs before the first IDR keyframe (P/B-frames without a reference
    produce decode errors that break playback). Prepends VPS/SPS/PPS, re-emits
    them before each IDR for decoder robustness.
    Returns the number of NALs written.
    """
    START_CODE = b'\x00\x00\x00\x01'
    count = 0
    if vps_list is None:
        vps_list = []

    if codec == 'hevc':
        idr_types = {19, 20}
        slice_types = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9}
    else:
        idr_types = {5}
        slice_types = {1, 2, 3, 4}

    def _get_nal_type(b):
        if codec == 'hevc':
            return (b >> 1) & 0x3F
        return b & 0x1F

    def _write_params(out):
        for vps in vps_list:
            out.write(START_CODE)
            out.write(vps)
        for sps in sps_list:
            out.write(START_CODE)
            out.write(sps)
        for pps in pps_list:
            out.write(START_CODE)
            out.write(pps)

    # Find first IDR keyframe — skip orphaned P/B-frames before it
    first_idr = 0
    with open(filepath, 'rb') as f:
        for i, (offset, length) in enumerate(nal_entries):
            f.seek(offset)
            b = f.read(1)
            if b and _get_nal_type(b[0]) in idr_types:
                first_idr = i
                break

    with open(output_path, 'wb') as out, open(filepath, 'rb') as src:
        _write_params(out)

        need_keyframe = False
        for nal_offset, nal_len in nal_entries[first_idr:]:
            # Peek at NAL type
            src.seek(nal_offset)
            nal_byte = src.read(1)
            if not nal_byte:
                continue
            nal_type = _get_nal_type(nal_byte[0])

            # Probe for zero corruption: sample a few positions in the NAL
            corrupted = False
            if nal_len > 64:
                for probe_pos in (nal_len // 4, nal_len // 2,
                                  nal_len * 3 // 4):
                    src.seek(nal_offset + probe_pos)
                    sample = src.read(16)
                    if len(sample) == 16 and all(b == 0 for b in sample):
                        corrupted = True
                        break
            if corrupted:
                need_keyframe = True
                continue

            # After a gap, skip slice frames until next IDR
            if need_keyframe:
                if nal_type not in idr_types:
                    if nal_type in slice_types:
                        continue
                else:
                    need_keyframe = False

            # Re-emit VPS/SPS/PPS before each IDR keyframe
            if nal_type in idr_types:
                _write_params(out)

            out.write(START_CODE)
            # Copy NAL data in chunks
            src.seek(nal_offset)
            remaining = nal_len
            while remaining > 0:
                chunk = src.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
            count += 1

    return count


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

    # Try Annex B start codes first
    i = 0
    while i < len(data) - 4:
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

    if not is_annex_b and len(data) >= 8:
        # Try MP4-style length-prefixed NALs.
        # iPhone mdat may start with audio data — if the first NAL chain
        # fails immediately, skip forward up to 64KB to find video data.
        def _try_nal_chain(start):
            """Try to parse a chain of NALs from 'start'. Returns
            (codec, nal_count, has_sps, has_pps, has_idr) or None."""
            c_codec = 'unknown'
            c_count = 0
            c_sps = c_pps = c_idr = False
            pos = start
            while pos < len(data) - 5:
                nl = struct.unpack('>I', data[pos:pos + 4])[0]
                if nl < 1 or nl > mdat_size:
                    break
                nb = data[pos + 4]
                if (nb >> 7) & 1:  # forbidden_zero_bit must be 0
                    break
                h264t = nb & 0x1F
                # H264 types are more specific — check first
                if h264t in (1, 5, 6, 7, 8, 9):
                    c_codec = 'h264'
                    if h264t == 7: c_sps = True
                    elif h264t == 8: c_pps = True
                    elif h264t == 5: c_idr = True
                    c_count += 1
                else:
                    h265t = (nb >> 1) & 0x3F
                    if h265t in (0, 1, 19, 20, 32, 33, 34, 35):
                        if c_codec == 'unknown':
                            c_codec = 'h265'
                        if h265t == 33: c_sps = True
                        elif h265t == 34: c_pps = True
                        elif h265t in (19, 20): c_idr = True
                        c_count += 1
                    else:
                        break  # unrecognized NAL type
                nxt = pos + 4 + nl
                if nxt > len(data):
                    break
                pos = nxt
            if c_count >= 2:
                return c_codec, c_count, c_sps, c_pps, c_idr
            return None

        # Try from the start first, then skip forward if it fails
        # (iPhone mdat starts with audio data at offset 0 — video begins later)
        chain = _try_nal_chain(0)
        if not chain:
            for skip in range(1, min(65536, len(data) - 8)):
                chain = _try_nal_chain(skip)
                if chain:
                    break
        if chain:
            codec, nal_count, has_sps, has_pps, has_idr = chain

    result['codec'] = codec
    result['nal_count'] = nal_count
    result['has_sps'] = has_sps
    result['has_pps'] = has_pps
    result['has_idr'] = has_idr

    return result


# ─── Video file checker ──────────────────────────────────────────────────────

def _check_avi(filepath, file_size, head, details):
    """Integrity check for AVI (RIFF) files. Recovery not supported."""
    details['unrecoverable'] = True

    # Validate RIFF/AVI signature
    if len(head) < 12 or head[:4] != b'RIFF' or head[8:12] != b'AVI ':
        if len(head) >= 4 and head[:4] == b'RIFF':
            return ('corrupt', 'RIFF file but not a valid AVI container', details)
        return ('corrupt', 'Not a valid AVI file — missing RIFF/AVI header', details)

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


def check_video(filepath):
    """Check a video file for integrity issues.

    Returns (status, reason, details_dict).
    Statuses: ok, no_moov, corrupt, truncated, empty, error
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
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    for i, b in enumerate(chunk):
                        if b != 0:
                            zero_end = offset + i
                            break
                    if zero_end > 0:
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
                    f.seek(sample_pos)
                    blk = f.read(65536)
                    if not blk:
                        break
                    for b in blk:
                        if b != 0:
                            nonzero_bytes += 1
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

        # AVI files use RIFF container — handled separately from MP4/MOV
        if Path(filepath).suffix.lower() == '.avi':
            return _check_avi(filepath, file_size, head, details)

        # Parse MP4 boxes
        boxes = parse_boxes(filepath)
        if not boxes:
            return ('corrupt',
                    'No valid MP4/MOV container structure found',
                    details)

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

        if has_mdat and not has_moov:
            # Check if mdat content is mostly zeros (valid header but wiped data)
            mdat_data_start = mdat_offset + 8
            nonzero_bytes = 0
            with open(filepath, 'rb') as f:
                sample_pos = mdat_data_start
                mdat_end = mdat_offset + mdat_size
                while sample_pos < mdat_end:
                    f.seek(sample_pos)
                    blk = f.read(65536)
                    if not blk:
                        break
                    for b in blk:
                        if b != 0:
                            nonzero_bytes += 1
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
                reason += ' SPS found.'
            return 'no_moov', reason, details

        if has_ftyp and not has_mdat and not has_moov:
            return ('corrupt',
                    'Has ftyp but no moov or mdat — file is just a header stub',
                    details)

        if not has_ftyp and not has_moov and not has_mdat:
            return ('corrupt',
                    'No valid MP4/MOV container structure found',
                    details)

        return 'ok', None, details

    except PermissionError:
        return 'error', 'Permission denied reading file', details
    except OSError as e:
        return 'error', f'OS error: {e}', details
    except Exception as e:
        return 'error', f'Unexpected error: {e}', details


# ─── Recovery functions ──────────────────────────────────────────────────────

def _adjust_stco(moov_data, delta):
    """Adjust all stco (chunk offset) entries in moov by delta bytes.

    Walks the moov atom recursively finding stco and co64 boxes,
    and adjusts every offset entry.
    Returns new moov_data bytes.
    """
    result = bytearray(moov_data)

    def walk(data, base_offset):
        pos = 0
        while pos < len(data) - 8:
            box_size = struct.unpack('>I', data[pos:pos + 4])[0]
            box_type = data[pos + 4:pos + 8]
            if box_size < 8 or pos + box_size > len(data):
                break

            if box_type == b'stco':
                # stco: version(1) + flags(3) + entry_count(4) + entries(4 each)
                content_start = base_offset + pos + 8
                entry_count = struct.unpack('>I',
                    result[content_start + 4:content_start + 8])[0]
                for i in range(entry_count):
                    off = content_start + 8 + i * 4
                    if off + 4 <= len(result):
                        old_val = struct.unpack('>I', result[off:off + 4])[0]
                        new_val = max(0, old_val + delta)
                        struct.pack_into('>I', result, off, new_val)

            elif box_type == b'co64':
                content_start = base_offset + pos + 8
                entry_count = struct.unpack('>I',
                    result[content_start + 4:content_start + 8])[0]
                for i in range(entry_count):
                    off = content_start + 8 + i * 8
                    if off + 8 <= len(result):
                        old_val = struct.unpack('>Q', result[off:off + 8])[0]
                        new_val = max(0, old_val + delta)
                        struct.pack_into('>Q', result, off, new_val)

            elif box_type in (b'moov', b'trak', b'mdia', b'minf', b'stbl',
                              b'edts', b'udta', b'dinf'):
                # Container box — recurse into children
                child_data = data[pos + 8:pos + box_size]
                walk(child_data, base_offset + pos + 8)

            pos += box_size

    # Skip the moov box header (8 bytes) and process children
    walk(moov_data[8:], 8)
    return bytes(result)


def _truncate_stsz(moov_data, target_count):
    """Truncate the stsz (sample size) table to target_count entries.

    This adjusts the moov so it only references as many samples as we expect
    to find in the damaged mdat. Also adjusts the stts, stsc, and stss tables.
    Returns adjusted moov bytes.
    """
    result = bytearray(moov_data)

    def adjust_table(data, base_offset, box_type_bytes, entry_size):
        pos = 0
        while pos < len(data) - 8:
            box_size = struct.unpack('>I', data[pos:pos + 4])[0]
            box_type = data[pos + 4:pos + 8]
            if box_size < 8 or pos + box_size > len(data):
                break

            if box_type == box_type_bytes:
                content_start = base_offset + pos + 8
                if box_type_bytes == b'stsz':
                    # stsz: version(1) + flags(3) + sample_size(4) + count(4)
                    old_count = struct.unpack('>I',
                        result[content_start + 8:content_start + 12])[0]
                    if target_count < old_count:
                        struct.pack_into('>I', result, content_start + 8,
                                         target_count)
                elif box_type_bytes == b'stss':
                    # stss: version(1) + flags(3) + count(4) + entries
                    old_count = struct.unpack('>I',
                        result[content_start + 4:content_start + 8])[0]
                    # Remove entries beyond target_count
                    new_count = 0
                    for i in range(old_count):
                        off = content_start + 8 + i * 4
                        if off + 4 <= len(result):
                            val = struct.unpack('>I', result[off:off + 4])[0]
                            if val <= target_count:
                                new_count += 1
                            else:
                                break
                    struct.pack_into('>I', result, content_start + 4, new_count)

            elif box_type in (b'moov', b'trak', b'mdia', b'minf', b'stbl',
                              b'edts', b'udta', b'dinf'):
                child_data = data[pos + 8:pos + box_size]
                adjust_table(child_data, base_offset + pos + 8,
                             box_type_bytes, entry_size)
            pos += box_size

    walk_data = moov_data[8:]
    adjust_table(walk_data, 8, b'stsz', 4)
    adjust_table(walk_data, 8, b'stss', 4)

    return bytes(result)


def _find_matching_donor(filepath, candidates):
    """Pick the best donor from candidates by filename prefix matching."""
    import re
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


def recover_no_moov(filepath, donor_path, output_dir=None):
    """Recover a video file missing its moov atom.

    Strategy 1: Raw NAL extraction — scan mdat for H.264 or HEVC NALs,
    get parameter sets from donor's avcC/hvcC, write Annex-B stream, wrap
    with ffmpeg.
    Strategy 2 (fallback): Moov transplant from donor.
    Returns (output_path, method_used) on success, raises ValueError on failure.
    """
    donor_moov = _extract_moov(donor_path)
    if not donor_moov:
        raise ValueError('Donor file has no moov atom')

    # Get damaged file's mdat info
    damaged_boxes = parse_boxes(filepath)
    damaged_mdat_offset = 0
    damaged_mdat_size = 0
    for bt, off, sz in damaged_boxes:
        if bt == 'mdat':
            damaged_mdat_offset = off
            damaged_mdat_size = sz
            break

    if damaged_mdat_size == 0:
        raise ValueError('No mdat found in damaged file')

    # Build output path
    stem = Path(filepath).stem
    suffix = Path(filepath).suffix
    out_name = f'{stem}_recovered{suffix}'
    if output_dir:
        out_path = os.path.join(output_dir, out_name)
    else:
        out_path = os.path.join(os.path.dirname(filepath), out_name)

    # ═══ Strategy 1: Raw NAL extraction (H.264 or HEVC) ═══
    # Try both codecs — donor codec may differ from damaged file's codec
    codec_attempts = []
    h264_params = _extract_avcc_params(donor_moov)
    hevc_params = _extract_hvcc_params(donor_moov)
    if h264_params:
        codec_attempts.append(('h264', h264_params))
    if hevc_params:
        codec_attempts.append(('hevc', hevc_params))
    # If donor only has one codec, still try the other with default nal_length_size
    if not h264_params and hevc_params:
        codec_attempts.insert(0, ('h264', {'nal_length_size': 4, 'sps_list': [], 'pps_list': []}))
    if not hevc_params and h264_params:
        codec_attempts.append(('hevc', {'nal_length_size': 4, 'vps_list': [], 'sps_list': [], 'pps_list': []}))

    for codec, params in codec_attempts:
        if not has_ffmpeg():
            break
        extract_fn = (_extract_hvcc_params if codec == 'hevc'
                      else _extract_avcc_params)
        ffmpeg_fmt = 'hevc' if codec == 'hevc' else 'h264'
        try:
            nal_entries = _scan_mdat_for_nals(
                filepath, damaged_mdat_offset, damaged_mdat_size,
                params['nal_length_size'], codec=codec)

            if len(nal_entries) >= 10:
                # Collect candidate SPS/PPS from donor pool for probing
                fdir = os.path.dirname(filepath)
                sps_candidates = _collect_sps_candidates(
                    fdir, params, codec=codec)
                best_vps, best_sps, best_pps = _probe_sps_candidates(
                    filepath, nal_entries, sps_candidates, codec=codec)

                # Find the donor that matches the best SPS for framerate
                fps_donor = donor_path
                for pool_file in donor_pool.get(fdir, []):
                    pm = _extract_moov(pool_file)
                    if pm:
                        pp = extract_fn(pm)
                        if pp and pp['sps_list'] == best_sps:
                            fps_donor = pool_file
                            break

                raw_path = out_path + '.' + ffmpeg_fmt
                try:
                    _write_annex_b_stream(
                        filepath, nal_entries, best_sps, best_pps,
                        raw_path, vps_list=best_vps, codec=codec)

                    # Get framerate from matching donor via ffprobe
                    ffprobe_path = _ffmpeg_path.replace('ffmpeg', 'ffprobe')
                    fps_cmd = subprocess.run(
                        [ffprobe_path, '-v', 'error', '-select_streams',
                         'v:0', '-show_entries', 'stream=r_frame_rate',
                         '-of', 'csv=p=0', fps_donor],
                        capture_output=True, timeout=10)
                    fps = fps_cmd.stdout.decode().strip().rstrip(',') or '30'

                    # Carry rotation metadata from donor
                    rotation = _get_donor_rotation(fps_donor)

                    ffmpeg_cmd = [_ffmpeg_path, '-y', '-framerate', fps]
                    if rotation:
                        ffmpeg_cmd += [
                            '-display_rotation:v:0',
                            str(rotation)]
                    ffmpeg_cmd += [
                        '-f', ffmpeg_fmt, '-i', raw_path,
                        '-c:v', 'copy', '-video_track_timescale',
                        fps.split('/')[0],
                        '-movflags', '+faststart']
                    if codec == 'hevc':
                        ffmpeg_cmd += ['-tag:v', 'hvc1']
                    ffmpeg_cmd.append(out_path)

                    result = subprocess.run(
                        ffmpeg_cmd, capture_output=True, timeout=300)

                    if result.returncode == 0 and os.path.exists(out_path):
                        out_size = os.path.getsize(out_path)
                        if out_size > damaged_mdat_size * 0.01:
                            # Decode-test: reject if error density is too high
                            try:
                                dec = subprocess.run(
                                    [_ffmpeg_path, '-v', 'error',
                                     '-i', out_path, '-f', 'null', '-'],
                                    capture_output=True, timeout=120)
                                err_lines = [l for l in
                                    dec.stderr.decode(errors='replace')
                                    .splitlines() if l.strip()]
                                # Scale threshold by output size: ~1 allowed
                                # error per 100KB. Small garbled outputs
                                # (< 1MB) stay strict; large valid
                                # recoveries tolerate more.
                                max_errors = max(10,
                                                 out_size // (100 * 1024))
                                if len(err_lines) > max_errors:
                                    os.unlink(out_path)
                                    continue  # try next codec
                            except subprocess.TimeoutExpired:
                                pass
                            except Exception:
                                pass
                            return out_path, 'raw_nal_extraction'
                finally:
                    if os.path.exists(raw_path):
                        try:
                            os.unlink(raw_path)
                        except OSError:
                            pass
        except Exception:
            pass

    # ═══ Strategy 2: Moov transplant (fallback) ═══
    donor_ftyp = _extract_ftyp(donor_path)
    if not donor_ftyp:
        raise ValueError('Donor file has no ftyp atom')

    donor_boxes = parse_boxes(donor_path)
    donor_mdat_offset = 0
    donor_mdat_size = 0
    for bt, off, sz in donor_boxes:
        if bt == 'mdat':
            donor_mdat_offset = off
            donor_mdat_size = sz
            break

    donor_mdat_data_start = donor_mdat_offset + 8
    new_mdat_data_start = len(donor_ftyp) + len(donor_moov) + 8
    delta = new_mdat_data_start - donor_mdat_data_start
    adjusted_moov = _adjust_stco(donor_moov, delta)

    if donor_mdat_size > 0 and damaged_mdat_size < donor_mdat_size:
        ratio = damaged_mdat_size / donor_mdat_size

        def find_stsz_count(data):
            pos = 0
            while pos < len(data) - 8:
                box_size = struct.unpack('>I', data[pos:pos + 4])[0]
                box_type = data[pos + 4:pos + 8]
                if box_size < 8 or pos + box_size > len(data):
                    break
                if box_type == b'stsz':
                    count = struct.unpack('>I', data[pos + 16:pos + 20])[0]
                    return count
                if box_type in (b'moov', b'trak', b'mdia', b'minf', b'stbl'):
                    child_result = find_stsz_count(data[pos + 8:pos + box_size])
                    if child_result:
                        return child_result
                pos += box_size
            return None

        donor_samples = find_stsz_count(adjusted_moov)
        if donor_samples and ratio < 0.95:
            target_samples = max(1, int(donor_samples * ratio))
            adjusted_moov = _truncate_stsz(adjusted_moov, target_samples)

    with open(filepath, 'rb') as f:
        f.seek(damaged_mdat_offset)
        mdat_data = f.read(damaged_mdat_size)

    with open(out_path, 'wb') as f:
        f.write(donor_ftyp)
        f.write(adjusted_moov)
        f.write(mdat_data)

    if has_ffmpeg():
        fixed_path = out_path.rsplit('.', 1)[0] + '_tmp.' + out_path.rsplit('.', 1)[1] if '.' in out_path else out_path + '.tmp'
        try:
            result = subprocess.run(
                [_ffmpeg_path, '-y', '-i', out_path, '-c', 'copy',
                 '-movflags', '+faststart', fixed_path],
                capture_output=True, timeout=120,
            )
            if result.returncode == 0 and os.path.exists(fixed_path):
                file_size = os.path.getsize(fixed_path)
                if file_size > 1000:
                    os.replace(fixed_path, out_path)
                    # Decode-test the transplanted result
                    try:
                        dec = subprocess.run(
                            [_ffmpeg_path, '-v', 'error',
                             '-i', out_path, '-f', 'null', '-'],
                            capture_output=True, timeout=120)
                        err_lines = [l for l in
                            dec.stderr.decode(errors='replace')
                            .splitlines() if l.strip()]
                        max_errors = max(10,
                                         file_size // (100 * 1024))
                        if len(err_lines) > max_errors:
                            os.unlink(out_path)
                            raise ValueError(
                                'Moov transplant produced unplayable output '
                                f'({len(err_lines)} decode errors) — '
                                'donor file is not compatible')
                    except subprocess.TimeoutExpired:
                        pass
                    except ValueError:
                        raise
                    except Exception:
                        pass
                    return out_path, 'moov_transplant+ffmpeg'
        except ValueError:
            raise
        except Exception:
            pass
        finally:
            if os.path.exists(fixed_path):
                try:
                    os.unlink(fixed_path)
                except OSError:
                    pass

    # No ffmpeg — return raw transplant without validation
    return out_path, 'moov_transplant'


def _trim_damaged_chunks(moov_data, zero_end, filepath=None):
    """Remove references to ALL damaged chunks from moov tables.

    Probes the actual file data at each chunk offset to determine if it
    is zeroed/corrupted, then removes all bad chunks (not just from the
    front). Handles multiple tracks (video, audio, metadata).
    Returns (adjusted_moov_bytes, total_samples_trimmed).
    """
    result = bytearray(moov_data)

    def _find_box_in(data, target, base):
        """Find absolute offset and size of a box, recursing into containers."""
        pos = 0
        while pos < len(data) - 8:
            sz = struct.unpack('>I', data[pos:pos + 4])[0]
            bt = data[pos + 4:pos + 8]
            if sz < 8 or pos + sz > len(data):
                break
            if bt == target:
                return (base + pos, sz)
            if bt in (b'mdia', b'minf', b'stbl', b'edts', b'udta', b'dinf'):
                found = _find_box_in(data[pos + 8:pos + sz], target,
                                     base + pos + 8)
                if found:
                    return found
            pos += sz
        return None

    def _find_traks(data, base):
        """Find all trak boxes in moov. Returns list of (abs_offset, size)."""
        traks = []
        pos = 0
        while pos < len(data) - 8:
            sz = struct.unpack('>I', data[pos:pos + 4])[0]
            bt = data[pos + 4:pos + 8]
            if sz < 8 or pos + sz > len(data):
                break
            if bt == b'trak':
                traks.append((base + pos, sz))
            pos += sz
        return traks

    def _analyze_trak(trak_off, trak_sz):
        """Analyze a trak, determine which chunks are damaged.

        Returns a dict with analysis results, or None if nothing to do.
        """
        trak_data = result[trak_off + 8:trak_off + trak_sz]
        trak_base = trak_off + 8

        stco_loc = _find_box_in(trak_data, b'stco', trak_base)
        if not stco_loc:
            return None
        stco_off, _ = stco_loc

        chunk_count = struct.unpack('>I',
            result[stco_off + 12:stco_off + 16])[0]
        if chunk_count == 0:
            return None
        chunk_offsets = [struct.unpack('>I',
            result[stco_off + 16 + i * 4:stco_off + 20 + i * 4])[0]
            for i in range(chunk_count)]

        # Probe every chunk — build a good/bad bitmap
        good_mask = [True] * chunk_count
        if filepath and os.path.exists(filepath):
            fh = open(filepath, 'rb')
            for i, off in enumerate(chunk_offsets):
                fh.seek(off)
                probe = fh.read(8)
                if len(probe) < 8 or all(b == 0 for b in probe):
                    good_mask[i] = False
            fh.close()
        else:
            for i, off in enumerate(chunk_offsets):
                if off < zero_end:
                    good_mask[i] = False

        # --- Parse stsc to build per-chunk sample counts ---
        stsc_loc = _find_box_in(trak_data, b'stsc', trak_base)
        if not stsc_loc:
            return None
        stsc_off, _ = stsc_loc
        stsc_count = struct.unpack('>I',
            result[stsc_off + 12:stsc_off + 16])[0]
        stsc_entries = []
        for i in range(stsc_count):
            base = stsc_off + 16 + i * 12
            fc = struct.unpack('>I', result[base:base + 4])[0]
            spc = struct.unpack('>I', result[base + 4:base + 8])[0]
            sdi = struct.unpack('>I', result[base + 8:base + 12])[0]
            stsc_entries.append((fc, spc, sdi))

        # Build per-chunk sample count array
        samples_per_chunk = [0] * chunk_count
        spc_idx = 0
        for c in range(chunk_count):
            chunk_1idx = c + 1
            while (spc_idx + 1 < len(stsc_entries) and
                   stsc_entries[spc_idx + 1][0] <= chunk_1idx):
                spc_idx += 1
            samples_per_chunk[c] = stsc_entries[spc_idx][1]

        # Build sample index ranges per chunk
        sample_starts = []
        s = 0
        for c in range(chunk_count):
            sample_starts.append(s)
            s += samples_per_chunk[c]
        total_samples = s

        # Refine good_mask with sample-level probing
        if filepath and os.path.exists(filepath):
            stsz_loc = _find_box_in(trak_data, b'stsz', trak_base)
            if stsz_loc:
                stsz_off_tmp, _ = stsz_loc
                uniform_tmp = struct.unpack('>I',
                    result[stsz_off_tmp + 12:stsz_off_tmp + 16])[0]
                sc_tmp = struct.unpack('>I',
                    result[stsz_off_tmp + 16:stsz_off_tmp + 20])[0]
                if uniform_tmp == 0:
                    ssizes = [struct.unpack('>I',
                        result[stsz_off_tmp + 20 + i * 4:
                               stsz_off_tmp + 24 + i * 4])[0]
                        for i in range(sc_tmp)]
                else:
                    ssizes = [uniform_tmp] * sc_tmp

                fh = open(filepath, 'rb')
                for c in range(chunk_count):
                    if not good_mask[c]:
                        continue
                    off = chunk_offsets[c]
                    for j in range(samples_per_chunk[c]):
                        sidx = sample_starts[c] + j
                        if sidx >= len(ssizes):
                            break
                        fh.seek(off)
                        probe = fh.read(4)
                        if len(probe) < 4 or all(b == 0 for b in probe):
                            good_mask[c] = False
                            break
                        off += ssizes[sidx]
                fh.close()

        # Keyframe-aware filtering for video tracks
        stss_loc = _find_box_in(trak_data, b'stss', trak_base)
        if stss_loc:
            stss_off_tmp, _ = stss_loc
            kf_count_tmp = struct.unpack('>I',
                result[stss_off_tmp + 12:stss_off_tmp + 16])[0]
            keyframe_set = set()
            for i in range(kf_count_tmp):
                kf = struct.unpack('>I',
                    result[stss_off_tmp + 16 + i * 4:
                           stss_off_tmp + 20 + i * 4])[0]
                keyframe_set.add(kf)  # 1-indexed

            need_keyframe = True
            for c in range(chunk_count):
                if not good_mask[c]:
                    need_keyframe = True
                    continue
                if need_keyframe:
                    has_kf = False
                    for j in range(samples_per_chunk[c]):
                        if (sample_starts[c] + j + 1) in keyframe_set:
                            has_kf = True
                            break
                    if not has_kf:
                        good_mask[c] = False
                    else:
                        need_keyframe = False

        # --- Get stts to calculate trim time ---
        stts_loc = _find_box_in(trak_data, b'stts', trak_base)
        stts_per_sample = []
        if stts_loc:
            stts_off_tmp, _ = stts_loc
            stts_count_tmp = struct.unpack('>I',
                result[stts_off_tmp + 12:stts_off_tmp + 16])[0]
            for i in range(stts_count_tmp):
                b = stts_off_tmp + 16 + i * 8
                cnt = struct.unpack('>I', result[b:b + 4])[0]
                val = struct.unpack('>I', result[b + 4:b + 8])[0]
                stts_per_sample.extend([val] * cnt)

        # --- Get timescale from mdhd ---
        mdhd_loc = _find_box_in(trak_data, b'mdhd', trak_base)
        timescale = 600  # fallback
        if mdhd_loc:
            mdhd_off, _ = mdhd_loc
            version = result[mdhd_off + 8]
            if version == 0:
                timescale = struct.unpack('>I',
                    result[mdhd_off + 20:mdhd_off + 24])[0]
            else:
                timescale = struct.unpack('>I',
                    result[mdhd_off + 28:mdhd_off + 32])[0]

        # Calculate the original time of trimmed leading samples
        # (time from track start to the first surviving sample)
        first_good = next((i for i in range(chunk_count)
                           if good_mask[i]), chunk_count)
        trimmed_leading_samples = 0
        if first_good < chunk_count:
            trimmed_leading_samples = sample_starts[first_good]
        trim_time_ticks = sum(stts_per_sample[:trimmed_leading_samples])
        trim_time_seconds = trim_time_ticks / timescale if timescale else 0

        return {
            'trak_off': trak_off, 'trak_sz': trak_sz,
            'trak_base': trak_base, 'trak_data': trak_data,
            'stco_off': stco_off, 'stsc_off': stsc_off,
            'chunk_count': chunk_count, 'chunk_offsets': chunk_offsets,
            'good_mask': good_mask,
            'stsc_entries': stsc_entries,
            'samples_per_chunk': samples_per_chunk,
            'sample_starts': sample_starts,
            'total_samples': total_samples,
            'stts_per_sample': stts_per_sample,
            'timescale': timescale,
            'trim_time_ticks': trim_time_ticks,
            'trim_time_seconds': trim_time_seconds,
        }

    def _apply_trim(info, extra_leading_samples=0):
        """Apply the trim to a trak's sample tables.

        extra_leading_samples: additional good samples to trim from the
        front for cross-track alignment.
        Returns number of samples trimmed.
        """
        trak_data = info['trak_data']
        trak_base = info['trak_base']
        good_mask = list(info['good_mask'])  # copy
        chunk_count = info['chunk_count']
        chunk_offsets = info['chunk_offsets']
        stco_off = info['stco_off']
        stsc_off = info['stsc_off']
        stsc_entries = info['stsc_entries']
        samples_per_chunk = info['samples_per_chunk']
        sample_starts = info['sample_starts']
        total_samples = info['total_samples']

        # If we need to trim extra leading samples for alignment,
        # mark additional leading good chunks as bad
        if extra_leading_samples > 0:
            extra_remaining = extra_leading_samples
            for c in range(chunk_count):
                if not good_mask[c]:
                    continue
                if extra_remaining <= 0:
                    break
                spc = samples_per_chunk[c]
                if spc <= extra_remaining:
                    good_mask[c] = False
                    extra_remaining -= spc
                else:
                    # Partial chunk — mark entire chunk as bad since
                    # we can't split a chunk in the stco table
                    good_mask[c] = False
                    extra_remaining = 0

        good_indices = [i for i in range(chunk_count) if good_mask[i]]
        if len(good_indices) == chunk_count:
            return 0  # nothing to trim

        # Samples to keep: those in good chunks
        keep_samples = []
        for c in good_indices:
            keep_samples.append((sample_starts[c], samples_per_chunk[c]))

        total_kept = sum(cnt for _, cnt in keep_samples)
        samples_trimmed = total_samples - total_kept

        if samples_trimmed == 0:
            return 0

        # --- stco: keep only good chunk offsets ---
        new_chunk_count = len(good_indices)
        struct.pack_into('>I', result, stco_off + 12, new_chunk_count)
        for i, gi in enumerate(good_indices):
            struct.pack_into('>I', result, stco_off + 16 + i * 4,
                             chunk_offsets[gi])

        # --- stsc: rebuild for new chunk numbering ---
        new_stsc = []
        for i, gi in enumerate(good_indices):
            new_chunk_1idx = i + 1
            spc = samples_per_chunk[gi]
            sdi = 1
            for fc_o, spc_o, sdi_o in stsc_entries:
                if fc_o <= gi + 1:
                    sdi = sdi_o
            if (not new_stsc or new_stsc[-1][1] != spc or
                    new_stsc[-1][2] != sdi):
                new_stsc.append([new_chunk_1idx, spc, sdi])

        struct.pack_into('>I', result, stsc_off + 12, len(new_stsc))
        for i, (fc, spc, sdi) in enumerate(new_stsc):
            base = stsc_off + 16 + i * 12
            struct.pack_into('>I', result, base, fc)
            struct.pack_into('>I', result, base + 4, spc)
            struct.pack_into('>I', result, base + 8, sdi)

        # --- stsz: keep only samples from good chunks ---
        stsz_loc = _find_box_in(trak_data, b'stsz', trak_base)
        if stsz_loc:
            stsz_off, _ = stsz_loc
            uniform = struct.unpack('>I',
                result[stsz_off + 12:stsz_off + 16])[0]
            struct.pack_into('>I', result, stsz_off + 16, total_kept)
            if uniform == 0:
                dst = stsz_off + 20
                for start, count in keep_samples:
                    src = stsz_off + 20 + start * 4
                    for j in range(count):
                        if src + j * 4 + 4 <= len(result):
                            v = struct.unpack('>I',
                                result[src + j * 4:src + j * 4 + 4])[0]
                            struct.pack_into('>I', result, dst, v)
                            dst += 4

        # --- Helper to rebuild run-length tables (stts, ctts) ---
        def _rebuild_rle_table(box_type):
            loc = _find_box_in(trak_data, box_type, trak_base)
            if not loc:
                return
            box_off, _ = loc
            entry_count = struct.unpack('>I',
                result[box_off + 12:box_off + 16])[0]

            per_sample = []
            for i in range(entry_count):
                base = box_off + 16 + i * 8
                count = struct.unpack('>I', result[base:base + 4])[0]
                val = struct.unpack('>I', result[base + 4:base + 8])[0]
                per_sample.extend([val] * count)

            kept_values = []
            for start, count in keep_samples:
                kept_values.extend(per_sample[start:start + count])

            new_entries = []
            if kept_values:
                cur_val = kept_values[0]
                cur_count = 1
                for v in kept_values[1:]:
                    if v == cur_val:
                        cur_count += 1
                    else:
                        new_entries.append((cur_count, cur_val))
                        cur_val = v
                        cur_count = 1
                new_entries.append((cur_count, cur_val))

            struct.pack_into('>I', result, box_off + 12, len(new_entries))
            for i, (count, val) in enumerate(new_entries):
                base = box_off + 16 + i * 8
                struct.pack_into('>I', result, base, count)
                struct.pack_into('>I', result, base + 4, val)

        _rebuild_rle_table(b'stts')
        _rebuild_rle_table(b'ctts')

        # --- stss: keep only keyframes that are in good chunks ---
        stss_loc = _find_box_in(trak_data, b'stss', trak_base)
        if stss_loc:
            stss_off, _ = stss_loc
            kf_count = struct.unpack('>I',
                result[stss_off + 12:stss_off + 16])[0]

            old_to_new = {}
            new_idx = 1
            for start, count in keep_samples:
                for j in range(count):
                    old_to_new[start + j + 1] = new_idx
                    new_idx += 1

            new_kfs = []
            for i in range(kf_count):
                kf = struct.unpack('>I', result[stss_off + 16 + i * 4:
                                                stss_off + 20 + i * 4])[0]
                if kf in old_to_new:
                    new_kfs.append(old_to_new[kf])

            struct.pack_into('>I', result, stss_off + 12, len(new_kfs))
            for i, kf in enumerate(new_kfs):
                struct.pack_into('>I', result, stss_off + 16 + i * 4, kf)

        return samples_trimmed

    # --- Phase 1: Analyze all traks ---
    traks = _find_traks(moov_data[8:], 8)
    analyses = []
    for trak_off, trak_sz in traks:
        info = _analyze_trak(trak_off, trak_sz)
        analyses.append(info)

    # --- Phase 2: Calculate cross-track alignment ---
    # Find the maximum trim time across all tracks. Tracks that lose
    # less time from the front need extra leading samples trimmed so
    # all tracks start at the same original presentation time.
    max_trim_seconds = 0
    for info in analyses:
        if info and info['trim_time_seconds'] > max_trim_seconds:
            max_trim_seconds = info['trim_time_seconds']

    # --- Phase 3: Apply trims with alignment ---
    total_trimmed = 0
    for info in analyses:
        if not info:
            continue
        extra_leading = 0
        if max_trim_seconds > 0 and info['trim_time_seconds'] < max_trim_seconds:
            # This track starts earlier — trim extra samples to align
            gap_seconds = max_trim_seconds - info['trim_time_seconds']
            gap_ticks = gap_seconds * info['timescale']
            # Count how many leading good samples to skip
            stts = info['stts_per_sample']
            good_mask = info['good_mask']
            sample_starts_arr = info['sample_starts']
            samples_per_chunk_arr = info['samples_per_chunk']
            accumulated = 0
            extra_leading = 0
            for c in range(info['chunk_count']):
                if not good_mask[c]:
                    continue
                for j in range(samples_per_chunk_arr[c]):
                    sidx = sample_starts_arr[c] + j
                    if sidx < len(stts):
                        accumulated += stts[sidx]
                    if accumulated >= gap_ticks:
                        break
                extra_leading = sample_starts_arr[c] + \
                    samples_per_chunk_arr[c] - \
                    info.get('_first_good_sample', 0)
                if accumulated >= gap_ticks:
                    # Count full chunks needed
                    break
            # Simpler: count good samples from front until time >= gap
            extra_leading = 0
            accumulated = 0
            for c in range(info['chunk_count']):
                if not good_mask[c]:
                    continue
                chunk_time = 0
                for j in range(samples_per_chunk_arr[c]):
                    sidx = sample_starts_arr[c] + j
                    if sidx < len(stts):
                        chunk_time += stts[sidx]
                if accumulated + chunk_time <= gap_ticks:
                    extra_leading += samples_per_chunk_arr[c]
                    accumulated += chunk_time
                else:
                    break

        trimmed = _apply_trim(info, extra_leading)
        total_trimmed += trimmed

    return bytes(result), total_trimmed



def recover_leading_zeros(filepath, donor_path=None, output_dir=None):
    """Recover a video file with leading zeros.

    Skips zero region, looks for surviving MP4 boxes or raw NAL stream.
    Returns (output_path, method_used), raises ValueError on failure.
    """
    file_size = os.path.getsize(filepath)

    # Find first non-zero byte
    zero_end = 0
    with open(filepath, 'rb') as f:
        chunk_size = 65536
        offset = 0
        while offset < file_size:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            for i, b in enumerate(chunk):
                if b != 0:
                    zero_end = offset + i
                    break
            if zero_end > 0:
                break
            offset += len(chunk)

    if zero_end == 0 or zero_end >= file_size:
        raise ValueError('File is entirely zeros')

    surviving = file_size - zero_end

    stem = Path(filepath).stem
    suffix = Path(filepath).suffix
    out_name = f'{stem}_recovered{suffix}'
    if output_dir:
        out_path = os.path.join(output_dir, out_name)
    else:
        out_path = os.path.join(os.path.dirname(filepath), out_name)

    # Check if moov survived at the end of the file (common for moov-at-end
    # layouts like Nikon MOV: ftyp + mdat + moov). Zeros wipe ftyp and the
    # start of mdat, but the moov at the end stays intact.
    moov_info = _scan_for_moov(filepath, zero_end)
    if moov_info:
        moov_offset, moov_size = moov_info
        # Verify moov is near end of file (allow trailing free/skip boxes)
        moov_end = moov_offset + moov_size
        tail_ok = moov_end >= file_size - 8
        if not tail_ok and moov_end < file_size:
            # Check if only free/skip/wide boxes follow the moov
            with open(filepath, 'rb') as f:
                pos = moov_end
                while pos < file_size - 8:
                    f.seek(pos)
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        break
                    tsz = struct.unpack('>I', hdr[:4])[0]
                    tbt = hdr[4:8]
                    if tsz < 8 or tbt not in (b'free', b'skip', b'wide'):
                        break
                    pos += tsz
                tail_ok = pos >= file_size - 8
        if tail_ok:
            # Get ftyp from donor, or build a minimal one
            ftyp_data = None
            if donor_path and os.path.exists(donor_path):
                ftyp_data = _extract_ftyp(donor_path)
            if not ftyp_data:
                # Minimal ftyp box: brand 'isom'
                ftyp_data = struct.pack('>I', 24) + b'ftyp' + b'isom' + \
                    struct.pack('>I', 0x200) + b'isomiso2'

            # Read moov from damaged file
            with open(filepath, 'rb') as f:
                f.seek(moov_offset)
                moov_data = f.read(moov_size)

            # Trim damaged chunks from the moov by probing actual data
            # at each chunk offset to detect scattered zero corruption.
            moov_data, samples_trimmed = _trim_damaged_chunks(
                moov_data, zero_end, filepath)

            # Find the earliest remaining stco offset across all tracks —
            # that's where valid mdat data begins in the original file.
            first_valid_offset = zero_end  # fallback
            def _collect_all_stco(data, base):
                """Find all stco boxes and return list of abs offsets."""
                results = []
                pos = 0
                while pos < len(data) - 8:
                    sz = struct.unpack('>I', data[pos:pos + 4])[0]
                    bt = data[pos + 4:pos + 8]
                    if sz < 8 or pos + sz > len(data):
                        break
                    if bt == b'stco':
                        results.append(base + pos)
                    elif bt in (b'moov', b'trak', b'mdia', b'minf',
                                b'stbl', b'edts', b'udta', b'dinf'):
                        results.extend(_collect_all_stco(
                            data[pos + 8:pos + sz], base + pos + 8))
                    pos += sz
                return results

            min_offset = None
            for stco_abs in _collect_all_stco(moov_data[8:], 8):
                cnt = struct.unpack('>I',
                    moov_data[stco_abs + 12:stco_abs + 16])[0]
                if cnt > 0:
                    off = struct.unpack('>I',
                        moov_data[stco_abs + 16:stco_abs + 20])[0]
                    if min_offset is None or off < min_offset:
                        min_offset = off
            if min_offset is not None:
                first_valid_offset = min_offset

            # Adjust stco offsets for the new file layout:
            # ftyp + mdat_header(8) + valid_data + moov
            ftyp_len = len(ftyp_data)
            new_mdat_data_start = ftyp_len + 8
            stco_delta = new_mdat_data_start - first_valid_offset
            moov_data = _adjust_stco(moov_data, stco_delta)

            valid_data_size = moov_offset - first_valid_offset
            mdat_box_size = valid_data_size + 8

            # Build mdat box header
            if mdat_box_size < 2**32:
                mdat_header = struct.pack('>I', mdat_box_size) + b'mdat'
            else:
                mdat_header = struct.pack('>I', 1) + b'mdat' + \
                    struct.pack('>Q', mdat_box_size + 8)

            # Write reconstructed file: ftyp + mdat(valid data only) + moov
            with open(out_path, 'wb') as out:
                out.write(ftyp_data)
                out.write(mdat_header)
                # Copy only valid mdat data from original file
                with open(filepath, 'rb') as f:
                    f.seek(first_valid_offset)
                    remaining = valid_data_size
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
                out.write(moov_data)

            method = 'moov_recovery'

            # Run ffmpeg to repackage cleanly if available
            if has_ffmpeg():
                fixed_path = out_path.rsplit('.', 1)[0] + '_tmp.' + out_path.rsplit('.', 1)[1] if '.' in out_path else out_path + '.tmp'
                try:
                    result = subprocess.run(
                        [_ffmpeg_path, '-y', '-err_detect',
                         'ignore_err', '-i', out_path,
                         '-c', 'copy', '-movflags', '+faststart',
                         '-shortest', fixed_path],
                        capture_output=True, timeout=120,
                    )
                    if result.returncode == 0 and os.path.exists(fixed_path):
                        if os.path.getsize(fixed_path) > 1000:
                            os.replace(fixed_path, out_path)
                            method = 'moov_recovery+ffmpeg'
                except Exception:
                    pass
                finally:
                    if os.path.exists(fixed_path):
                        try:
                            os.unlink(fixed_path)
                        except OSError:
                            pass

            return out_path, method

    # Read surviving data
    with open(filepath, 'rb') as f:
        f.seek(zero_end)
        surviving_data = f.read(min(surviving, 32))

    # Check if surviving data starts with valid MP4 boxes
    if len(surviving_data) >= 8:
        box_size = struct.unpack('>I', surviving_data[:4])[0]
        box_type = surviving_data[4:8]
        try:
            bt = box_type.decode('ascii')
            if bt in ('ftyp', 'moov', 'mdat', 'free', 'wide', 'skip'):
                # Surviving data has MP4 structure — extract it
                with open(filepath, 'rb') as f:
                    f.seek(zero_end)
                    with open(out_path, 'wb') as out:
                        while True:
                            chunk = f.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)

                # Check if the extracted file has a moov
                extracted_boxes = parse_boxes(out_path)
                has_moov = any(bt == 'moov' for bt, _, _ in extracted_boxes)
                has_mdat = any(bt == 'mdat' for bt, _, _ in extracted_boxes)

                if has_moov and has_mdat:
                    return out_path, 'zero_skip'
                elif has_mdat and not has_moov and donor_path:
                    # Try moov transplant on the extracted file
                    try:
                        return recover_no_moov(out_path, donor_path, output_dir)
                    except ValueError:
                        return out_path, 'zero_skip_no_moov'
                return out_path, 'zero_skip'
        except (UnicodeDecodeError, ValueError):
            pass

    # Check for raw NAL units (Annex-B start codes) — H.264 or HEVC
    with open(filepath, 'rb') as f:
        f.seek(zero_end)
        sample = f.read(min(surviving, 1024 * 1024))

    nal_count = 0
    for i in range(len(sample) - 4):
        if sample[i:i + 4] == b'\x00\x00\x00\x01':
            nal_count += 1

    if nal_count >= 5 and has_ffmpeg():
        # Detect codec from first NAL after start code
        annexb_codec = 'h264'
        for i in range(len(sample) - 5):
            if sample[i:i + 4] == b'\x00\x00\x00\x01':
                nb = sample[i + 4]
                h265t = (nb >> 1) & 0x3F
                if h265t in (32, 33, 34, 0, 1, 19, 20):
                    annexb_codec = 'hevc'
                break

        ffmpeg_fmt = 'hevc' if annexb_codec == 'hevc' else 'h264'
        raw_path = out_path + '.' + ffmpeg_fmt
        with open(filepath, 'rb') as f:
            f.seek(zero_end)
            with open(raw_path, 'wb') as out:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        try:
            wrap_cmd = [_ffmpeg_path, '-y', '-f', ffmpeg_fmt, '-i',
                        raw_path, '-c:v', 'copy']
            if annexb_codec == 'hevc':
                wrap_cmd += ['-tag:v', 'hvc1']
            wrap_cmd.append(out_path)
            result = subprocess.run(
                wrap_cmd, capture_output=True, timeout=300,
            )
            if result.returncode == 0 and os.path.getsize(out_path) > 1000:
                return out_path, 'raw_' + ffmpeg_fmt + '_wrap'
        except Exception:
            pass
        finally:
            if os.path.exists(raw_path):
                try:
                    os.unlink(raw_path)
                except OSError:
                    pass

    # Check for length-prefixed NALs (MP4-style) using donor params
    # Try both codecs since donor may differ from damaged file
    if donor_path and has_ffmpeg():
        try:
            donor_moov = _extract_moov(donor_path)
            if donor_moov:
                codec_attempts = []
                h264_p = _extract_avcc_params(donor_moov)
                hevc_p = _extract_hvcc_params(donor_moov)
                if h264_p:
                    codec_attempts.append(('h264', h264_p))
                if hevc_p:
                    codec_attempts.append(('hevc', hevc_p))
                if not h264_p and hevc_p:
                    codec_attempts.insert(0, ('h264', {'nal_length_size': 4,
                        'sps_list': [], 'pps_list': []}))
                if not hevc_p and h264_p:
                    codec_attempts.append(('hevc', {'nal_length_size': 4,
                        'vps_list': [], 'sps_list': [], 'pps_list': []}))

                for codec, params in codec_attempts:
                    extract_fn = (_extract_hvcc_params if codec == 'hevc'
                                  else _extract_avcc_params)
                    ffmpeg_fmt = 'hevc' if codec == 'hevc' else 'h264'

                    nal_entries = _scan_mdat_for_nals(
                        filepath, zero_end, surviving,
                        params['nal_length_size'],
                        raw_data=True, codec=codec)

                    if len(nal_entries) >= 3:
                        # Probe SPS candidates to find correct resolution
                        fdir = os.path.dirname(filepath)
                        sps_candidates = _collect_sps_candidates(
                            fdir, params, codec=codec)
                        best_vps, best_sps, best_pps = (
                            _probe_sps_candidates(
                                filepath, nal_entries,
                                sps_candidates, codec=codec))

                        # Find donor matching best SPS for framerate
                        fps_donor = donor_path
                        for pool_file in donor_pool.get(fdir, []):
                            pm = _extract_moov(pool_file)
                            if pm:
                                pp = extract_fn(pm)
                                if pp and pp['sps_list'] == best_sps:
                                    fps_donor = pool_file
                                    break

                        raw_path = out_path + '.' + ffmpeg_fmt
                        try:
                            _write_annex_b_stream(
                                filepath, nal_entries,
                                best_sps, best_pps, raw_path,
                                vps_list=best_vps, codec=codec)

                            # Get framerate from matching donor
                            ffprobe_path = _ffmpeg_path.replace(
                                'ffmpeg', 'ffprobe')
                            fps_cmd = subprocess.run(
                                [ffprobe_path, '-v', 'error',
                                 '-select_streams', 'v:0',
                                 '-show_entries', 'stream=r_frame_rate',
                                 '-of', 'csv=p=0', fps_donor],
                                capture_output=True, timeout=10)
                            fps = fps_cmd.stdout.decode().strip().rstrip(
                                ',') or '30'

                            # Carry rotation metadata from donor
                            rotation = _get_donor_rotation(fps_donor)

                            ffmpeg_cmd = [
                                _ffmpeg_path, '-y', '-framerate', fps]
                            if rotation:
                                ffmpeg_cmd += [
                                    '-display_rotation:v:0',
                                    str(rotation)]
                            ffmpeg_cmd += [
                                '-f', ffmpeg_fmt, '-i', raw_path,
                                '-c:v', 'copy',
                                '-video_track_timescale',
                                fps.split('/')[0],
                                '-movflags', '+faststart']
                            if codec == 'hevc':
                                ffmpeg_cmd += ['-tag:v', 'hvc1']
                            ffmpeg_cmd.append(out_path)

                            result = subprocess.run(
                                ffmpeg_cmd,
                                capture_output=True, timeout=600)

                            if (result.returncode == 0 and
                                    os.path.exists(out_path) and
                                    os.path.getsize(out_path) >
                                    surviving * 0.01):
                                # Decode-test: reject if error density too high
                                try:
                                    out_sz = os.path.getsize(out_path)
                                    dec = subprocess.run(
                                        [_ffmpeg_path, '-v', 'error',
                                         '-i', out_path, '-f', 'null',
                                         '-'],
                                        capture_output=True, timeout=120)
                                    err_lines = [l for l in
                                        dec.stderr.decode(errors='replace')
                                        .splitlines() if l.strip()]
                                    max_errors = max(10,
                                                     out_sz // (100 * 1024))
                                    if len(err_lines) > max_errors:
                                        os.unlink(out_path)
                                        continue
                                except subprocess.TimeoutExpired:
                                    pass
                                except Exception:
                                    pass
                                return out_path, 'raw_nal_extraction'
                        finally:
                            if os.path.exists(raw_path):
                                try:
                                    os.unlink(raw_path)
                                except OSError:
                                    pass
        except Exception:
            pass

    # Try moov transplant before zero strip — the surviving mdat data
    # may be usable with a donor moov even if NAL scanning failed
    if donor_path and os.path.exists(donor_path):
        try:
            result_path, method = recover_no_moov(filepath, donor_path,
                                                   output_dir)
            if os.path.exists(result_path) and os.path.getsize(result_path) > 1024:
                return result_path, method
        except (ValueError, Exception):
            pass

    # Last resort: just strip the zeros — only if the result has valid structure
    with open(filepath, 'rb') as f:
        f.seek(zero_end)
        with open(out_path, 'wb') as out:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

    # Check if stripped file has any valid MP4 structure
    stripped_boxes = parse_boxes(out_path)
    if stripped_boxes and any(bt in ('moov', 'mdat') for bt, _, _ in stripped_boxes):
        return out_path, 'zero_strip'

    # Not a valid container — clean up and fail
    try:
        os.unlink(out_path)
    except OSError:
        pass
    raise ValueError('No recoverable video data found — file is too damaged')


# ─── Directory scanner ───────────────────────────────────────────────────────

def scan_directory_gen(root_dir):
    """Generator that scans a directory for video files and yields SSE events."""

    if not os.path.exists(root_dir):
        yield {'type': 'error', 'message': f'Path does not exist: {root_dir}'}
        return

    if not os.path.isdir(root_dir):
        yield {'type': 'error', 'message': f'Not a directory: {root_dir}'}
        return

    yield {'type': 'status', 'message': 'Counting video files…'}

    all_files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        if scan_state['cancelled']:
            return
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d != '__MACOSX']
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in VIDEO_EXTS:
                all_files.append(os.path.join(dirpath, fname))

    total = len(all_files)
    yield {'type': 'total', 'total': total}

    if total == 0:
        yield {'type': 'complete', 'scanned': 0, 'issues': 0, 'elapsed': 0}
        return

    scanned = 0
    issue_count = 0
    start_time = time.time()
    batch_size = WORKER_THREADS * 4

    for batch_start in range(0, total, batch_size):
        if scan_state['cancelled']:
            yield {'type': 'cancelled', 'scanned': scanned, 'issues': issue_count}
            return

        batch = all_files[batch_start:batch_start + batch_size]
        futures = {}

        with ThreadPoolExecutor(max_workers=WORKER_THREADS) as executor:
            for fpath in batch:
                if scan_state['cancelled']:
                    break
                fut = executor.submit(check_video, fpath)
                futures[fut] = fpath

            for fut in as_completed(futures):
                if scan_state['cancelled']:
                    break

                fpath = futures[fut]
                scanned += 1

                try:
                    status, reason, details = fut.result()
                except Exception as e:
                    status, reason, details = 'error', str(e), {}

                fname = os.path.basename(fpath)
                fdir = os.path.dirname(fpath)
                ext = os.path.splitext(fname)[1].lower()
                fsize = details.get('file_size', 0)

                elapsed = time.time() - start_time
                rate = scanned / elapsed if elapsed > 0 else 0
                remaining = (total - scanned) / rate if rate > 0 else 0
                eta_str = ''
                if remaining > 0:
                    m, s = divmod(int(remaining), 60)
                    eta_str = f'{m}m {s}s' if m else f'{s}s'

                file_duration = details.get('duration')

                yield {
                    'type': 'scanning',
                    'file': fname,
                    'dir': fdir,
                    'scanned': scanned,
                    'total': total,
                    'issues': issue_count,
                    'eta': eta_str,
                    'duration': file_duration,
                }

                if status != 'ok':
                    issue_count += 1
                    event = {
                        'type': 'issue',
                        'filepath': fpath,
                        'filename': fname,
                        'dir': fdir,
                        'ext': ext,
                        'file_size': fsize,
                        'status': status,
                        'reason': reason or '',
                        'details': details,
                        'duration': file_duration,
                    }
                    yield event
                else:
                    donor_pool.setdefault(fdir, []).append(fpath)

    elapsed = round(time.time() - start_time, 1)
    yield {'type': 'complete', 'scanned': scanned, 'issues': issue_count,
           'elapsed': elapsed}


# ─── Recovery execution ───────────────────────────────────────────────────────

def _execute_recovery(filepath, donor_override='', output_dir=''):
    """Run recovery on a single file. Returns result dict on success."""
    fdir = os.path.dirname(filepath)

    donor_path = donor_override
    if not donor_path:
        donor_path = donor_map.get(fdir, '')

    donor_auto = ''
    if not donor_path or not os.path.exists(donor_path):
        pool = donor_pool.get(fdir, [])
        if pool:
            donor_path = _find_matching_donor(filepath, pool)
            if donor_path:
                donor_auto = os.path.basename(donor_path)

    if not donor_path or not os.path.exists(donor_path):
        raise ValueError('No compatible donor found. '
                         'Scan a directory with healthy files from '
                         'the same camera first.')

    if not output_dir:
        output_dir = os.path.expanduser('~/Downloads')

    with open(filepath, 'rb') as f:
        head = f.read(8)
    all_zero = all(b == 0 for b in head[:8])

    if all_zero:
        out_path, method = recover_leading_zeros(
            filepath, donor_path, output_dir)
    else:
        out_path, method = recover_no_moov(
            filepath, donor_path, output_dir)

    out_size = os.path.getsize(out_path)

    duration = None
    if has_ffprobe():
        try:
            dur_cmd = subprocess.run(
                [_ffprobe_path, '-v', 'error',
                 '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', out_path],
                capture_output=True, timeout=10)
            dur_str = dur_cmd.stdout.decode().strip()
            if dur_str:
                duration = float(dur_str)
        except Exception:
            pass

    original_size = 0
    for r in scan_state.get('results', []):
        if r.get('filepath') == filepath:
            details = r.get('details', {})
            original_size = (details.get('mdat_size', 0) or
                             details.get('surviving_bytes', 0) or
                             details.get('file_size', 0))
            break
    recovery_pct = None
    if original_size > 0:
        recovery_pct = round(out_size / original_size * 100, 1)

    has_audio = False
    if has_ffprobe():
        try:
            ac = subprocess.run(
                [_ffprobe_path, '-v', 'error',
                 '-select_streams', 'a',
                 '-show_entries', 'stream=codec_type',
                 '-of', 'csv=p=0', out_path],
                capture_output=True, timeout=10)
            has_audio = bool(ac.stdout.decode().strip())
        except Exception:
            pass

    resp = {
        'ok': True,
        'output': out_path,
        'filename': os.path.basename(out_path),
        'size': out_size,
        'method': method,
        'duration': duration,
        'recovery_pct': recovery_pct,
        'original_size': original_size,
        'video_only': not has_audio,
    }
    if donor_auto:
        resp['donor_used'] = donor_auto
    return resp


def _recovery_worker():
    """Daemon thread processing recovery jobs sequentially."""
    while True:
        job = _recover_queue.get()
        job_id = job['id']
        with _recover_lock:
            _recover_jobs[job_id]['status'] = 'recovering'
            _recover_jobs[job_id]['started_at'] = time.time()
        try:
            result = _execute_recovery(
                job['filepath'], job.get('donor', ''))
            with _recover_lock:
                _recover_jobs[job_id]['status'] = 'done'
                _recover_jobs[job_id]['result'] = result
                _recover_jobs[job_id]['finished_at'] = time.time()
        except Exception as e:
            with _recover_lock:
                _recover_jobs[job_id]['status'] = 'failed'
                _recover_jobs[job_id]['error'] = str(e)
                _recover_jobs[job_id]['finished_at'] = time.time()
        finally:
            _recover_queue.task_done()


def _ensure_recovery_worker():
    global _recover_worker_started
    if not _recover_worker_started:
        _recover_worker_started = True
        t = threading.Thread(target=_recovery_worker, daemon=True)
        t.start()


# ─── HTTP handler ─────────────────────────────────────────────────────────────

class FrameHandler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=APP_DIR, **kwargs)

    def _send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            return {}

    def _query_params(self):
        return parse_qs(urlparse(self.path).query)

    def _route_path(self):
        return urlparse(self.path).path

    # ── GET routes ──

    def do_GET(self):
        path = self._route_path()
        try:
            if path == '/api/scan':
                self._handle_scan()
            elif path == '/api/export':
                self._handle_export()
            elif path == '/api/browse':
                self._handle_browse()
            elif path == '/api/status':
                self._handle_status()
            elif path == '/api/recover-status':
                self._handle_recover_status()
            elif path == '/api/mount-status':
                self._handle_mount_status()
            else:
                super().do_GET()
        except (ConnectionResetError, BrokenPipeError):
            pass

    # ── POST routes ──

    def do_POST(self):
        path = self._route_path()
        try:
            if path == '/api/cancel':
                self._handle_cancel()
            elif path == '/api/reveal':
                self._handle_reveal()
            elif path == '/api/set-donor':
                self._handle_set_donor()
            elif path == '/api/recover':
                self._handle_recover()
            elif path == '/api/recover-queue':
                self._handle_recover_queue()
            elif path == '/api/recover-queue-clear':
                self._handle_recover_queue_clear()
            else:
                self.send_error(404)
        except (ConnectionResetError, BrokenPipeError):
            pass

    # ── Handler methods ──

    def _handle_scan(self):
        params = self._query_params()
        root_dir = params.get('dir', [''])[0].strip()
        if not root_dir:
            return self._send_json({'error': 'No directory specified'}, 400)

        scan_state['cancelled'] = False
        scan_state['running'] = True
        scan_state['results'] = []
        donor_pool.clear()

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        try:
            for event in scan_directory_gen(root_dir):
                if event.get('type') == 'issue':
                    scan_state['results'].append(event)
                line = f"data: {json.dumps(event, default=str)}\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            scan_state['running'] = False

    def _handle_cancel(self):
        scan_state['cancelled'] = True
        self._send_json({'ok': True})

    def _handle_export(self):
        results = scan_state.get('results', [])
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'Status', 'Filename', 'Extension', 'Directory', 'Full Path',
            'File Size (bytes)', 'Issue',
        ])
        for r in results:
            writer.writerow([
                r.get('status', ''),
                r.get('filename', ''),
                r.get('ext', ''),
                r.get('dir', ''),
                r.get('filepath', ''),
                r.get('file_size', ''),
                r.get('reason', ''),
            ])

        body = output.getvalue().encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/csv')
        self.send_header('Content-Disposition',
                         'attachment; filename=video_scan_results.csv')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_browse(self):
        params = self._query_params()
        path = params.get('path', [''])[0].strip()
        if not path:
            if sys.platform == 'darwin':
                path = '/Volumes'
            else:
                path = '/'

        if not os.path.isdir(path):
            return self._send_json({'error': 'Not a directory',
                                    'path': path}, 400)

        entries = []
        try:
            for name in sorted(os.listdir(path)):
                if name.startswith('.'):
                    continue
                full = os.path.join(path, name)
                if os.path.isdir(full):
                    entries.append({'name': name, 'path': full})
        except PermissionError:
            return self._send_json({'error': 'Permission denied',
                                    'path': path}, 403)

        self._send_json({'path': path, 'entries': entries})

    def _handle_mount_status(self):
        params = self._query_params()
        path = params.get('path', [''])[0].strip()
        if not path:
            return self._send_json({'error': 'No path specified'}, 400)

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        last_exists = None
        try:
            while True:
                try:
                    exists = (os.path.isdir(path) and
                              next(os.scandir(path), None) is not None)
                except OSError:
                    exists = False
                if exists != last_exists:
                    last_exists = exists
                    event = json.dumps({'exists': exists, 'path': path})
                    self.wfile.write(f'data: {event}\n\n'.encode())
                    self.wfile.flush()
                time.sleep(3)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def _handle_status(self):
        self._send_json({
            'ffmpeg': has_ffmpeg(),
            'ffprobe': has_ffprobe(),
            'ffmpeg_path': _ffmpeg_path or '',
            'ffprobe_path': _ffprobe_path or '',
        })

    def _handle_reveal(self):
        data = self._read_json_body()
        filepath = data.get('filepath', '')
        if not filepath or not os.path.exists(filepath):
            return self._send_json({'error': 'File not found'}, 404)

        try:
            if sys.platform == 'darwin':
                subprocess.Popen(['open', '-R', filepath])
            elif sys.platform == 'win32':
                subprocess.Popen(['explorer', '/select,', filepath])
            else:
                dirpath = os.path.dirname(filepath)
                subprocess.Popen(['xdg-open', dirpath])
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)

        self._send_json({'ok': True})

    def _handle_set_donor(self):
        data = self._read_json_body()
        filepath = data.get('filepath', '')
        if not filepath or not os.path.exists(filepath):
            return self._send_json({'error': 'File not found'}, 404)

        directory = data.get('directory', os.path.dirname(filepath))
        donor_map[directory] = filepath
        self._send_json({'ok': True, 'directory': directory,
                         'donor': os.path.basename(filepath)})

    def _handle_recover(self):
        data = self._read_json_body()
        filepath = data.get('filepath', '')
        donor_override = data.get('donor', '')
        output_dir = data.get('output_dir', '')

        if not filepath or not os.path.exists(filepath):
            return self._send_json({'error': 'File not found'}, 404)

        try:
            resp = _execute_recovery(filepath, donor_override, output_dir)
            self._send_json(resp)
        except ValueError as e:
            self._send_json({'error': str(e)}, 422)
        except Exception as e:
            self._send_json({'error': f'Recovery failed: {e}'}, 500)

    def _handle_recover_queue(self):
        global _recover_counter
        data = self._read_json_body()
        filepath = data.get('filepath', '')
        donor = data.get('donor', '')

        if not filepath or not os.path.exists(filepath):
            return self._send_json({'error': 'File not found'}, 404)

        with _recover_lock:
            for j in _recover_jobs.values():
                if (j['filepath'] == filepath
                        and j['status'] in ('queued', 'recovering')):
                    return self._send_json(
                        {'ok': False, 'error': 'Already queued'})
            _recover_counter += 1
            job_id = str(_recover_counter)
            job = {
                'id': job_id,
                'filepath': filepath,
                'filename': os.path.basename(filepath),
                'donor': donor,
                'status': 'queued',
                'queued_at': time.time(),
                'started_at': None,
                'finished_at': None,
                'result': None,
                'error': None,
            }
            _recover_jobs[job_id] = job

        _recover_queue.put(job)
        _ensure_recovery_worker()
        self._send_json({'ok': True, 'job_id': job_id, 'status': 'queued'})

    def _handle_recover_status(self):
        with _recover_lock:
            jobs = sorted(_recover_jobs.values(),
                          key=lambda j: int(j['id']))
        self._send_json({'jobs': jobs})

    def _handle_recover_queue_clear(self):
        with _recover_lock:
            done_ids = [jid for jid, j in _recover_jobs.items()
                        if j['status'] in ('done', 'failed')]
            for jid in done_ids:
                del _recover_jobs[jid]
        self._send_json({'ok': True, 'cleared': len(done_ids)})

    def log_message(self, format, *args):
        pass  # Suppress default access log noise



# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5901
    print()
    print('╔══════════════════════════════════════════════╗')
    print('║              Frayme                           ║')
    print(f'║  Open http://localhost:{port} in your browser  ║')
    print('╚══════════════════════════════════════════════╝')
    print()
    threading.Timer(1.2, lambda: webbrowser.open(f'http://localhost:{port}')).start()
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(('127.0.0.1', port), FrameHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
        server.server_close()
