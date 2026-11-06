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
    'scan_id': 0,
}

# Per-directory donor map: { dir_path: filepath } (manual override)
donor_map = {}

# Auto-collected healthy files: { dir_path: [filepath, ...] }
donor_pool = {}

# Recovery queue infrastructure
# Where recovered files go unless the user picks another folder
DEFAULT_OUTPUT_DIR = os.path.expanduser('~/Downloads')

_recover_queue = queue_mod.Queue()
_recover_jobs = {}
_recover_lock = threading.Lock()
_recover_counter = 0
_recover_worker_started = False

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


# Largest plausible size per NAL type; anything bigger is junk read as a length
_H264_NAL_MAX = {1: 8 << 20, 5: 8 << 20,      # slices
                 6: 256 << 10,                 # SEI
                 7: 1024, 8: 1024,             # SPS, PPS
                 9: 8, 12: 1 << 20}            # AUD, filler
_HEVC_NAL_MAX = {t: 8 << 20 for t in list(range(10)) + list(range(16, 22))}
_HEVC_NAL_MAX.update({32: 1024, 33: 1024, 34: 1024, 35: 8,   # VPS, SPS, PPS, AUD
                      39: 256 << 10, 40: 256 << 10})          # SEI

# A position counts as video only if this many valid NALs chain from it. Two
# was not enough: random data (audio, other files in carved recoveries)
# regularly chained twice and yielded multi-megabyte junk "NALs".
_CHAIN_MIN = 3


def _read_ue(data, bit):
    """Read an Exp-Golomb ue(v) from data at bit offset. Returns (value, bit) or (None, bit)."""
    zeros = 0
    while bit < len(data) * 8 and not (data[bit >> 3] >> (7 - (bit & 7))) & 1:
        zeros += 1
        bit += 1
        if zeros > 20:
            return None, bit
    bit += 1
    value = 0
    for _ in range(zeros):
        if bit >= len(data) * 8:
            return None, bit
        value = (value << 1) | ((data[bit >> 3] >> (7 - (bit & 7))) & 1)
        bit += 1
    return (1 << zeros) - 1 + value, bit


def _read_bits(data, bit, count):
    """Read count bits from data at bit offset. Returns (value, bit) or (None, bit)."""
    if bit + count > len(data) * 8:
        return None, bit
    value = 0
    for _ in range(count):
        value = (value << 1) | ((data[bit >> 3] >> (7 - (bit & 7))) & 1)
        bit += 1
    return value, bit


def _read_se(data, bit):
    """Read a signed Exp-Golomb se(v). Returns (value, bit) or (None, bit)."""
    k, bit = _read_ue(data, bit)
    if k is None:
        return None, bit
    return ((k + 1) // 2) * (1 if k % 2 else -1), bit


def _rbsp(nal, limit=None):
    """NAL payload with emulation prevention bytes (00 00 03) removed."""
    data = nal[:limit] if limit else nal
    out = bytearray()
    zeros = 0
    for b in data:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def _parse_h264_sps_order(sps):
    """SPS fields needed to read picture order from slice headers, or None.

    Returns dict with sps_id, log2_max_frame_num, poc_type, log2_max_poc_lsb,
    frame_mbs_only, separate_colour_plane.
    """
    d = _rbsp(sps)
    profile = d[1] if len(d) > 3 else None
    if profile is None:
        return None
    bit = 32                                   # NAL header, profile, constraints, level
    sps_id, bit = _read_ue(d, bit)
    separate_colour_plane = 0
    scaling = 0                                # 0 flat, 1 spec defaults, 2 custom lists
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma_format, bit = _read_ue(d, bit)
        if chroma_format is None:
            return None
        if chroma_format == 3:
            separate_colour_plane, bit = _read_bits(d, bit, 1)
        _, bit = _read_ue(d, bit)              # bit_depth_luma_minus8
        _, bit = _read_ue(d, bit)              # bit_depth_chroma_minus8
        _, bit = _read_bits(d, bit, 1)         # qpprime_y_zero_transform_bypass
        scaling, bit = _read_bits(d, bit, 1)
        if scaling:
            for i in range(12 if chroma_format == 3 else 8):
                present, bit = _read_bits(d, bit, 1)
                if present:
                    scaling = 2
                    last = nxt = 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            delta, bit = _read_se(d, bit)
                            if delta is None:
                                return None
                            nxt = (last + delta + 256) % 256
                        last = nxt or last
    log2_max_frame_num, bit = _read_ue(d, bit)
    poc_type, bit = _read_ue(d, bit)
    if log2_max_frame_num is None or poc_type is None:
        return None
    log2_max_poc_lsb = None
    if poc_type == 0:
        log2_max_poc_lsb, bit = _read_ue(d, bit)
        if log2_max_poc_lsb is None:
            return None
        log2_max_poc_lsb += 4
    elif poc_type == 1:
        return None                            # rare; not needed so far
    _, bit = _read_ue(d, bit)                  # max_num_ref_frames
    _, bit = _read_bits(d, bit, 1)             # gaps_in_frame_num_allowed
    mbw, bit = _read_ue(d, bit)                # pic_width_in_mbs_minus1
    mbh, bit = _read_ue(d, bit)                # pic_height_in_map_units_minus1
    frame_mbs_only, bit = _read_bits(d, bit, 1)
    if frame_mbs_only is None or mbw is None or mbh is None:
        return None
    width, height = (mbw + 1) * 16, (mbh + 1) * 16 * (2 - frame_mbs_only)
    if not frame_mbs_only:
        _, bit = _read_bits(d, bit, 1)         # mb_adaptive_frame_field
    _, bit = _read_bits(d, bit, 1)             # direct_8x8_inference
    cropping, bit = _read_bits(d, bit, 1)
    if cropping:                               # 4:2:0 crop units are 2 pixels
        crop = []
        for _ in range(4):
            value, bit = _read_ue(d, bit)
            crop.append(value or 0)
        width -= 2 * (crop[0] + crop[1])
        height -= 2 * (crop[2] + crop[3]) * (2 - frame_mbs_only)
    return {'sps_id': sps_id, 'log2_max_frame_num': log2_max_frame_num + 4,
            'poc_type': poc_type, 'log2_max_poc_lsb': log2_max_poc_lsb,
            'frame_mbs_only': frame_mbs_only,
            'separate_colour_plane': separate_colour_plane,
            'width': width, 'height': height, 'scaling': scaling}


def _parse_h264_pps_ids(pps):
    """(pps_id, sps_id) of an H.264 PPS, or None."""
    d = _rbsp(pps, 16)
    pps_id, bit = _read_ue(d, 8)
    sps_id, bit = _read_ue(d, bit)
    if pps_id is None or sps_id is None:
        return None
    return pps_id, sps_id


def _h264_slice_poc_lsb(nal, sps_by_id, pps_by_id):
    """(pic_order_cnt_lsb, max_poc_lsb) from an H.264 slice header, or None.

    Returns None for streams whose order can't be read this way (POC type
    1/2, field pictures, unknown parameter sets).
    """
    d = _rbsp(nal, 48)
    bit = 8
    _, bit = _read_ue(d, bit)                  # first_mb_in_slice
    _, bit = _read_ue(d, bit)                  # slice_type
    pps_id, bit = _read_ue(d, bit)
    sps = sps_by_id.get(pps_by_id.get(pps_id))
    if sps is None or sps['poc_type'] != 0:
        return None
    if sps['separate_colour_plane']:
        _, bit = _read_bits(d, bit, 2)
    _, bit = _read_bits(d, bit, sps['log2_max_frame_num'])
    if not sps['frame_mbs_only']:
        field_pic, bit = _read_bits(d, bit, 1)
        if field_pic:
            return None
    if nal[0] & 0x1F == 5:
        _, bit = _read_ue(d, bit)              # idr_pic_id
    lsb, bit = _read_bits(d, bit, sps['log2_max_poc_lsb'])
    if lsb is None:
        return None
    return lsb, 1 << sps['log2_max_poc_lsb']


def _plausible_slice_header(header, codec):
    """Check the first slice header fields of a slice NAL (header includes the NAL header)."""
    if codec == 'hevc':
        nal_type = (header[0] >> 1) & 0x3F
        bit = 16 + 1                       # NAL header, first_slice_segment_in_pic_flag
        if 16 <= nal_type <= 23:
            bit += 1                       # no_output_of_prior_pics_flag
        pps_id, _ = _read_ue(header, bit)
        return pps_id is not None and pps_id <= 63
    first_mb, bit = _read_ue(header, 8)
    if first_mb is None or first_mb > 139264:     # macroblocks in an 8K frame
        return False
    slice_type, bit = _read_ue(header, bit)
    if slice_type is None or slice_type > 9:
        return False
    if header[0] & 0x1F == 5:
        # IDR slices are always I or SI and always referenced
        if slice_type % 5 not in (2, 4) or not header[0] & 0x60:
            return False
    pps_id, _ = _read_ue(header, bit)
    return pps_id is not None and pps_id <= 255


def _scan_mdat_for_nals(filepath, mdat_offset, mdat_size,
                        nal_length_size=4, raw_data=False, codec='h264'):
    """Scan mdat for length-prefixed NAL units, skipping audio chunks.

    Supports both H.264 and HEVC codecs. Reads through mmap.
    Returns list of (file_offset, nal_length) tuples where file_offset
    points to the NAL data (after the length prefix).

    Scanning only starts, and after a break only resumes, at a position where
    _CHAIN_MIN valid NALs chain together. Slice NALs must also have a
    plausible slice header. Carved files (e.g. PhotoRec output of fragmented
    camera recordings) mix stretches of video with unrelated data, and
    weaker checks lock onto that data and jump over real video.

    If raw_data=True, mdat_offset points directly at NAL data (no mdat box
    header to skip). Used for leading-zero recovery where the box header
    was destroyed.
    """
    import mmap
    import re

    max_sizes = _HEVC_NAL_MAX if codec == 'hevc' else _H264_NAL_MAX
    header_size = 2 if codec == 'hevc' else 1
    slice_types = (set(range(10)) | set(range(16, 22)) if codec == 'hevc'
                   else {1, 5})

    with open(filepath, 'rb') as f:
        if os.fstat(f.fileno()).st_size == 0:
            return []
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        # Determine mdat data range
        if raw_data:
            data_start = mdat_offset
        else:
            box_sz = struct.unpack_from('>I', m, mdat_offset)[0]
            data_start = mdat_offset + (16 if box_sz == 1 else 8)
        data_end = min(mdat_offset + mdat_size, len(m))

        def nal_length(pos):
            """Length of a valid NAL at pos (pointing at its length prefix), else None."""
            hdr_pos = pos + nal_length_size
            if hdr_pos + header_size > data_end:
                return None
            nal_len = int.from_bytes(m[pos:hdr_pos], 'big')
            nal_byte = m[hdr_pos]
            if nal_byte & 0x80:            # forbidden_zero_bit
                return None
            if codec == 'hevc':
                nal_type = (nal_byte >> 1) & 0x3F
                # layer_id must be 0, nuh_temporal_id_plus1 must be >= 1
                nal_byte2 = m[hdr_pos + 1]
                if (nal_byte & 1) or (nal_byte2 >> 3) or not nal_byte2 & 7:
                    return None
            else:
                nal_type = nal_byte & 0x1F
            limit = max_sizes.get(nal_type)
            if (limit is None or nal_len < header_size or nal_len > limit
                    or hdr_pos + nal_len > data_end):
                return None
            if nal_type in slice_types and not _plausible_slice_header(
                    m[hdr_pos:hdr_pos + min(nal_len, 16)], codec):
                return None
            return nal_len

        def is_aud(pos, nal_len):
            # Access unit delimiter: fixed length, and the byte after the NAL
            # header is 3 bits of picture type, a stop bit, then zeros
            hdr_pos = pos + nal_length_size
            if codec == 'hevc':
                return (nal_len == 3 and (m[hdr_pos] >> 1) & 0x3F == 35
                        and m[hdr_pos + 2] & 0x1F == 0x10)
            return nal_len == 2 and m[hdr_pos] & 0x1F == 9 and m[hdr_pos + 1] & 0x1F == 0x10

        def clean_payload(pos, nal_len):
            # The spec forbids 00 00 00/01/02 inside a NAL (trailing zero
            # padding aside); a random stretch of data rarely avoids them
            start = pos + nal_length_size
            body = m[start:start + nal_len].rstrip(b'\x00')
            return (b'\x00\x00\x00' not in body and b'\x00\x00\x01' not in body
                    and b'\x00\x00\x02' not in body)

        def chain_at(pos):
            first_is_aud = False
            found = []
            for i in range(_CHAIN_MIN):
                nal_len = nal_length(pos)
                if nal_len is None:
                    # A delimiter plus one valid NAL is as specific as three
                    # NALs. Some cameras (Nikon) write delimiter + slice per
                    # frame with audio after every frame, so three never chain.
                    if first_is_aud and i == 2:
                        return True
                    # Two NALs with clean payloads: high-bitrate files often
                    # hold just two frames between audio chunks
                    return i == 2 and all(clean_payload(p, n) for p, n in found)
                if i == 0:
                    first_is_aud = is_aud(pos, nal_len)
                found.append((pos, nal_len))
                pos += nal_length_size + nal_len
                if pos >= data_end:
                    return True
            return True

        # With 4-byte lengths, a regex finds candidate positions at C speed:
        # length < 8 MB (top byte 0) followed by a NAL header of a valid type
        candidate_re = None
        if nal_length_size == 4:
            if codec == 'hevc':
                first = bytes(sorted({t << 1 for t in max_sizes}))
                candidate_re = re.compile(
                    rb'\x00[\x00-\x7f][\s\S]{2}[' + re.escape(first) + rb'][\x01-\x07]')
            else:
                first = bytes(sorted({(ref << 5) | t
                                      for ref in range(4) for t in max_sizes}))
                candidate_re = re.compile(
                    rb'\x00[\x00-\x7f][\s\S]{2}[' + re.escape(first) + rb']')

        def next_chain(start):
            if candidate_re is not None:
                for match in candidate_re.finditer(m, start, data_end):
                    if chain_at(match.start()):
                        return match.start()
                return None
            for pos in range(start, data_end - nal_length_size - header_size):
                if chain_at(pos):
                    return pos
            return None

        # Lone frames between audio chunks: ffmpeg-muxed files interleave a
        # little audio after every frame, so slices never chain. Once the
        # stream is known from trusted chains, accept a lone slice that fits
        # it (same PPS, picture start if pictures are single-slice, clean
        # payload) when the next two frames follow the same way: random
        # data rarely passes several times in a row.
        profile = {'pps_ids': set(), 'multi_slice': False}
        GAP = 256 * 1024

        def learn(pos, nal_len):
            hdr_pos = pos + nal_length_size
            nal_type = ((m[hdr_pos] >> 1) & 0x3F if codec == 'hevc'
                        else m[hdr_pos] & 0x1F)
            if nal_type not in slice_types:
                return
            head = m[hdr_pos:hdr_pos + 16]
            if codec == 'hevc':
                first = head[2] & 0x80
                pps_id, _ = _read_ue(head, 17 + (1 if 16 <= nal_type <= 23 else 0))
            else:
                first_mb, bit = _read_ue(head, 8)
                _, bit = _read_ue(head, bit)
                pps_id, _ = _read_ue(head, bit)
                first = first_mb == 0
            profile['pps_ids'].add(pps_id)
            if not first:
                profile['multi_slice'] = True

        def lone_slice(pos):
            nal_len = nal_length(pos)
            if nal_len is None:
                return None
            hdr_pos = pos + nal_length_size
            nal_type = ((m[hdr_pos] >> 1) & 0x3F if codec == 'hevc'
                        else m[hdr_pos] & 0x1F)
            if nal_type not in slice_types:
                return None
            head = m[hdr_pos:hdr_pos + 16]
            if codec == 'hevc':
                first = head[2] & 0x80
                pps_id, _ = _read_ue(head, 17 + (1 if 16 <= nal_type <= 23 else 0))
            else:
                first_mb, bit = _read_ue(head, 8)
                _, bit = _read_ue(head, bit)
                pps_id, _ = _read_ue(head, bit)
                first = first_mb == 0
            if pps_id not in profile['pps_ids']:
                return None
            if not first and not profile['multi_slice']:
                return None
            if not clean_payload(pos, nal_len):
                return None
            return nal_len

        def next_lone(start, hops=2):
            if not profile['pps_ids'] or candidate_re is None:
                return None
            for match in candidate_re.finditer(m, start, min(start + GAP, data_end)):
                cand = match.start()
                nal_len = lone_slice(cand)
                if nal_len is None:
                    continue
                end = cand + nal_length_size + nal_len
                if (hops == 0 or end >= data_end - nal_length_size
                        or nal_length(end) is not None
                        or next_lone(end, hops - 1) is not None):
                    return cand
            return None

        nal_entries = []
        pos = next_chain(data_start)
        trusted = True                 # pos came from a full chain
        while pos is not None and pos < data_end - nal_length_size:
            nal_len = nal_length(pos)
            if nal_len is not None:
                nal_entries.append((pos + nal_length_size, nal_len))
                if trusted:
                    learn(pos, nal_len)
                pos += nal_length_size + nal_len
            else:
                lone = next_lone(pos)
                if lone is not None:
                    pos, trusted = lone, False
                else:
                    pos, trusted = next_chain(pos + 1), True
        return nal_entries
    finally:
        m.close()


def _find_param_nal(block, start, end, nal_type, codec):
    """First length-prefixed parameter-set NAL of nal_type in block[start:end].

    Returns (nal_bytes, end_offset) or None.
    """
    import re
    if codec == 'hevc':
        pattern = rb'\x00\x00([\x00-\x03][\s\S])(' + re.escape(bytes([nal_type << 1])) + rb')\x01'
    else:
        pattern = rb'\x00\x00([\x00-\x03][\s\S])([' + re.escape(
            bytes([(ref << 5) | nal_type for ref in (1, 2, 3)])) + rb'])'
    for match in re.compile(pattern).finditer(block, start, end):
        length = int.from_bytes(match.group(1), 'big')
        nal_start = match.start() + 4
        nal = block[nal_start:nal_start + length]
        if not 2 <= length <= 1024 or len(nal) < length:
            continue
        # H.264 SPS starts with a known profile_idc
        if codec != 'hevc' and nal_type == 7 and nal[1] not in (66, 77, 88, 100, 110, 122, 244):
            continue
        # Same rule as _write_annex_b_stream: real NALs never contain these
        body = nal.rstrip(b'\x00')
        if b'\x00\x00\x00' in body or b'\x00\x00\x01' in body or b'\x00\x00\x02' in body:
            continue
        return nal, nal_start + length
    return None


def _find_inband_params(filepath, mdat_offset, nal_entries, codec='h264'):
    """Parameter sets the damaged file carries itself, as (vps_list, sps_list, pps_list).

    Looks in the scanned NALs (cameras that repeat SPS/PPS in the stream) and
    in the first 1 MB of mdat, where some cameras keep them in a private
    header (GoPro's GPRO block holds a length-prefixed SPS and PPS). Returns
    None when no SPS and PPS are found. These match the recording exactly,
    unlike a donor's, so a file can be recovered even without a donor.
    """
    import re
    if codec == 'hevc':
        kinds = {32: 'vps', 33: 'sps', 34: 'pps'}
        get_type = lambda b: (b >> 1) & 0x3F
    else:
        kinds = {7: 'sps', 8: 'pps'}
        get_type = lambda b: b & 0x1F
    found = {'vps': [], 'sps': [], 'pps': []}

    def add(kind, nal):
        if nal not in found[kind] and len(found[kind]) < 4:
            found[kind].append(nal)

    with open(filepath, 'rb') as f:
        for offset, length in nal_entries:
            f.seek(offset)
            head = f.read(1)
            if head and get_type(head[0]) in kinds:
                f.seek(offset)
                add(kinds[get_type(head[0])], f.read(length))

        if not found['sps'] or not found['pps']:
            f.seek(mdat_offset)
            block = f.read(1 << 20)
            # Search in the order cameras write them (VPS, SPS, PPS), each
            # within 1 KB of the previous: a lone match elsewhere in 1 MB of
            # header and compressed data is usually a chance byte pattern
            if codec == 'hevc':
                order = [('vps', 32), ('sps', 33), ('pps', 34)]
            else:
                order = [('sps', 7), ('pps', 8)]
            header_set = {}
            pos, limit = 0, len(block)
            for kind, nal_type in order:
                nal = _find_param_nal(block, pos, limit, nal_type, codec)
                if nal is None:
                    header_set = None
                    break
                header_set[kind], end = nal
                pos, limit = end, min(len(block), end + 1024)
            if header_set:
                for kind, nal in header_set.items():
                    if not found[kind]:
                        found[kind] = [nal]

    if not found['sps'] or not found['pps'] or (codec == 'hevc' and not found['vps']):
        return None
    return found['vps'], found['sps'], found['pps']


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


def _decode_stats(input_args, timeout):
    """Decode with ffmpeg; return (problems, frames_decoded).

    problems counts error lines plus concealment notices. Concealment
    matters as much as errors: an SPS declaring a taller frame than the
    recording (1440 rows for 1080p video) decodes with no errors, but ffmpeg
    fills the missing rows and logs "concealing ... errors" at info level,
    and the picture gets a green band. frames_decoded matters as much as
    problems: with a wrong SPS most frames can fail to decode at all, and a
    file that decodes nothing has no errors.
    """
    import re
    result = subprocess.run(
        [_ffmpeg_path, '-hide_banner', '-loglevel', 'level+info']
        + input_args + ['-f', 'null', '-'],
        capture_output=True, timeout=timeout)
    lines = result.stderr.decode(errors='replace').splitlines()
    problems = sum(1 for line in lines
                   if line.startswith(('[error]', '[fatal]')) or 'concealing' in line)
    frames = 0
    for line in lines:
        match = re.search(r'frame=\s*(\d+)', line)
        if match:
            frames = int(match.group(1))
    return problems, frames


def _check_decodes_cleanly(path, timeout=600):
    """Decode-test a recovered file. Returns (ok, message).

    At least 80% of the video packets must decode, with problems in under
    10% of them. A size-based error allowance used to pass files where a
    mismatched SPS left nearly every frame undecodable (a blank video).
    """
    pk = subprocess.run(
        [_ffprobe_path, '-v', 'quiet', '-select_streams', 'v:0',
         '-count_packets', '-show_entries', 'stream=nb_read_packets',
         '-of', 'csv=p=0', path],
        capture_output=True, timeout=timeout)
    digits = ''.join(ch for ch in pk.stdout.decode() if ch.isdigit())
    packets = int(digits) if digits else 0
    problems, frames = _decode_stats(['-i', path], timeout)
    if packets == 0:
        return False, 'no video frames'
    if frames < packets * 0.8:
        return False, (f'only {frames} of {packets} frames decode'
                       + (f', {problems} decode errors' if problems else ''))
    if problems > packets * 0.1 + 2:
        return False, f'{problems} decode errors in {packets} frames'
    return True, f'{frames} frames decode'


def _probe_sps_candidates(filepath, nal_entries, candidates, codec='h264',
                          return_score=False):
    """Try each (vps_list, sps_list, pps_list) candidate against the NAL data.

    Writes a short test stream (first IDR + a few frames) with each
    candidate, decodes it with ffmpeg, and returns the candidate
    with the fewest decode errors.
    Returns (best_vps_list, best_sps_list, best_pps_list), or with
    return_score, (that, score) where score counts decode problems plus
    frames that failed to decode (0 = clean; None = not measured).
    """
    if not candidates or not has_ffmpeg():
        best = candidates[0] if candidates else ([], [], [])
        return (best, None) if return_score else best

    if len(candidates) == 1 and not return_score:
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

    # Frames in the test stream: slices that start a picture
    # (first_mb_in_slice / first_slice_segment_in_pic_flag)
    expected_frames = 0
    with open(filepath, 'rb') as f:
        for offset, length in test_nals:
            f.seek(offset)
            head = f.read(3)
            if codec == 'hevc':
                if len(head) == 3 and ((head[0] >> 1) & 0x3F) < 32 and head[2] & 0x80:
                    expected_frames += 1
            elif len(head) >= 2 and 1 <= (head[0] & 0x1F) <= 5 and head[1] & 0x80:
                expected_frames += 1

    if not test_nals:
        return (candidates[0], None) if return_score else candidates[0]

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

            problems, frames = _decode_stats(
                ['-f', ffmpeg_fmt, '-i', tmp_path], timeout=10)
            err_count = problems + max(0, expected_frames - frames)

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

    if return_score:
        return best, (best_errors if best_errors != float('inf') else None)
    return best


def _decoding_signature(sps_list, pps_list):
    """The H.264 settings that decide how a stream decodes, or None.

    Lets inferred parameters be matched to donors (for framerate and
    rotation) even though the bytes differ: inferred SPS/PPS carry no VUI.
    """
    if not sps_list or not pps_list:
        return None
    order = _parse_h264_sps_order(sps_list[0])
    pps = _parse_h264_pps(pps_list[0])
    if not order or not pps:
        return None
    return (order['log2_max_frame_num'], order['poc_type'],
            order['log2_max_poc_lsb'], order['width'], order['height'],
            order['scaling'], pps['entropy'], pps['init_qp'], pps['transform_8x8'])


def _write_annex_b_stream(filepath, nal_entries, sps_list, pps_list,
                          output_path, vps_list=None, codec='h264',
                          pictures=None):
    """Write NAL units as an Annex-B stream (.h264 or .hevc).

    Skips NALs before the first IDR keyframe (P/B-frames without a reference
    produce decode errors that break playback). Prepends VPS/SPS/PPS, re-emits
    them before each IDR for decoder robustness.
    Returns the number of NALs written. If pictures is a list, H.264
    pictures are appended to it in decode order as (is_idr, is_reference,
    poc_lsb, max_poc_lsb) for _apply_presentation_order; poc_lsb is None
    when the order can't be read.
    """
    START_CODE = b'\x00\x00\x00\x01'
    count = 0
    if vps_list is None:
        vps_list = []

    if codec == 'hevc':
        idr_types = {19, 20}
        slice_types = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9}
        vcl_limit, aud_type = 32, 35
    else:
        idr_types = {5}
        slice_types = {1, 2, 3, 4}
        vcl_limit, aud_type = 6, 9

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
        prev_end = None
        # Parameter sets by id, for reading picture order from slice headers
        sps_by_id, pps_by_id = {}, {}
        if pictures is not None and codec != 'hevc':
            for sps in sps_list:
                info = _parse_h264_sps_order(sps)
                if info:
                    sps_by_id[info['sps_id']] = info
            for pps in pps_list:
                ids = _parse_h264_pps_ids(pps)
                if ids:
                    pps_by_id[ids[0]] = ids[1]
        # AUD/SEI wait here until their frame is written, so a skipped or
        # missing frame doesn't leave an access unit without a picture
        pending = []
        for nal_offset, nal_len in nal_entries[first_idr:]:
            src.seek(nal_offset)
            data = src.read(nal_len)
            if not data:
                continue
            nal_type = _get_nal_type(data[0])

            # A jump of over 1 MB means the scanner resynced past non-video
            # data (in carved files, another file's data); the frames after it
            # don't reference the frames before it
            if prev_end is not None and nal_offset - prev_end > (1 << 20):
                need_keyframe = True
            prev_end = nal_offset + nal_len

            # Damaged NAL: the spec forbids 00 00 00/01/02 inside a NAL
            # (trailing zero padding aside), and zeroed blocks show up as
            # 16-byte zero runs. Both appear when a NAL's data was overwritten
            # or, in carved files, when a frame runs into another file's data.
            # Trailing zeros are padding (constant-bitrate cameras pad every
            # frame), not damage, so both checks ignore them
            body = data.rstrip(b'\x00')
            if (b'\x00\x00\x01' in body or b'\x00\x00\x02' in body
                    or b'\x00\x00\x00' in body
                    or (nal_len > 64 and b'\x00' * 16 in body)):
                if nal_type < vcl_limit:
                    need_keyframe = True
                    pending = []
                continue

            if nal_type >= vcl_limit:
                pending.append((nal_type, data))
                continue

            # After a gap, skip slice frames until next IDR
            # Cameras may split a picture into several slices (iPhone 5:
            # two per frame); only the first starts a new picture
            # (first_mb_in_slice == 0 / first_slice_segment_in_pic_flag)
            if codec == 'hevc':
                first_in_pic = len(data) > 2 and bool(data[2] & 0x80)
            else:
                first_in_pic = len(data) > 1 and bool(data[1] & 0x80)

            if need_keyframe:
                # HEVC CRA/BLA frames are random access points too
                if (nal_type not in idr_types and not (
                        codec == 'hevc' and 16 <= nal_type <= 21)) \
                        or not first_in_pic:
                    pending = []
                    continue
                need_keyframe = False

            # Access unit order: AUD, then VPS/SPS/PPS (re-emitted before
            # each IDR keyframe), then SEI and the rest, then the frame.
            # Parameters go only before a picture's first slice: between
            # two slices of one keyframe they end the picture halfway, and
            # the decoder conceals the rest.
            for t, nal in pending:
                if t == aud_type:
                    out.write(START_CODE)
                    out.write(nal)
            if nal_type in idr_types and first_in_pic:
                _write_params(out)
            for t, nal in pending:
                if t != aud_type:
                    out.write(START_CODE)
                    out.write(nal)
                    count += 1
            pending = []

            out.write(START_CODE)
            out.write(data)
            count += 1

            if pictures is not None and codec != 'hevc' and first_in_pic:
                order = _h264_slice_poc_lsb(data, sps_by_id, pps_by_id)
                pictures.append((nal_type in idr_types, bool(data[0] & 0x60))
                                + (order or (None, None)))

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


class _BitWriter:
    """Builds an H.264 RBSP: fixed-width fields and Exp-Golomb codes."""

    def __init__(self):
        self.bits = []

    def u(self, count, value):
        self.bits += [(value >> (count - 1 - i)) & 1 for i in range(count)]

    def ue(self, value):
        value += 1
        self.u(value.bit_length() - 1, 0)
        self.u(value.bit_length(), value)

    def se(self, value):
        self.ue(2 * value - 1 if value > 0 else -2 * value)

    def nal(self):
        """The NAL bytes: stop bit, byte alignment, emulation prevention."""
        bits = self.bits + [1]
        bits += [0] * (-len(bits) % 8)
        raw = bytes(int(''.join(map(str, bits[i:i + 8])), 2)
                    for i in range(0, len(bits), 8))
        out, zeros = bytearray(), 0
        for b in raw:
            if zeros >= 2 and b <= 3:
                out.append(3)
                zeros = 0
            out.append(b)
            zeros = zeros + 1 if b == 0 else 0
        return bytes(out)


def _build_h264_sps(p):
    """SPS NAL for inferred settings: High profile, 4:2:0 8-bit, progressive.

    No VUI: it carries no decoding information. max_num_ref_frames is as
    large as level 5.1 allows for the frame size, since a larger sliding
    window than the encoder's decodes the same.
    """
    mbw, mbh = (p['width'] + 15) // 16, (p['height'] + 15) // 16
    w = _BitWriter()
    w.u(8, 0x67)
    w.u(8, 100)
    w.u(8, 0)
    w.u(8, 51)
    w.ue(0)                                    # sps_id
    w.ue(1)                                    # chroma_format_idc 4:2:0
    w.ue(0)
    w.ue(0)                                    # 8-bit luma and chroma
    w.u(1, 0)
    # Scaling matrices: flat (iPhone, GoPro, x264) or the spec's defaults,
    # declared without sending any lists (Nikon, Fujifilm)
    w.u(1, 1 if p.get('default_scaling') else 0)
    if p.get('default_scaling'):
        w.u(8, 0)
    w.ue(p['log2_max_frame_num'] - 4)
    w.ue(p['poc_type'])
    if p['poc_type'] == 0:
        w.ue(p['log2_max_poc_lsb'] - 4)
    w.ue(max(1, min(16, 184320 // (mbw * mbh))))
    w.u(1, 0)                                  # gaps_in_frame_num
    w.ue(mbw - 1)
    w.ue(mbh - 1)
    w.u(1, 1)                                  # frame_mbs_only
    w.u(1, p.get('direct_8x8', 1))
    crop_r, crop_b = (mbw * 16 - p['width']) // 2, (mbh * 16 - p['height']) // 2
    w.u(1, 1 if crop_r or crop_b else 0)
    if crop_r or crop_b:
        w.ue(0)
        w.ue(crop_r)
        w.ue(0)
        w.ue(crop_b)
    w.u(1, 0)                                  # no VUI
    return w.nal()


def _build_h264_pps(p):
    """PPS NAL for inferred settings (High profile syntax)."""
    w = _BitWriter()
    w.u(8, 0x68)
    w.ue(p.get('pps_id', 0))
    w.ue(0)
    w.u(1, p['entropy'])
    w.u(1, 0)                                  # bottom_field_pic_order
    w.ue(0)                                    # one slice group
    w.ue(p.get('ref_l0', 0))
    w.ue(p.get('ref_l1', 0))
    w.u(1, p.get('weighted_pred', 0))
    w.u(2, p.get('weighted_bipred', 0))
    w.se(p.get('init_qp', 0))
    w.se(0)
    w.se(p.get('chroma_qp', 0))
    w.u(1, p['deblock_ctrl'])
    w.u(1, 0)                                  # constrained_intra_pred
    w.u(1, 0)                                  # redundant_pic_cnt
    w.u(1, p.get('transform_8x8', 0))
    w.u(1, 0)
    w.se(p.get('chroma_qp', 0))
    return w.nal()


def _parse_h264_pps(pps):
    """Fields of an H.264 PPS that slice-header parsing depends on, or None."""
    d = _rbsp(pps)
    fields = {}
    bit = 8
    for name, kind in (('pps_id', 'ue'), ('sps_id', 'ue'), ('entropy', 1),
                       ('bottom_field_poc', 1), ('slice_groups', 'ue'),
                       ('ref_l0', 'ue'), ('ref_l1', 'ue'), ('weighted_pred', 1),
                       ('weighted_bipred', 2), ('init_qp', 'se'), ('init_qs', 'se'),
                       ('chroma_qp', 'se'), ('deblock_ctrl', 1),
                       ('constrained_intra', 1), ('redundant', 1)):
        if kind == 'ue':
            value, bit = _read_ue(d, bit)
        elif kind == 'se':
            value, bit = _read_se(d, bit)
        else:
            value, bit = _read_bits(d, bit, kind)
        if value is None:
            return None
        fields[name] = value
    if fields['slice_groups']:
        return None
    fields['transform_8x8'] = 0
    rest = len(d) * 8 - bit
    if rest > 8 or (rest > 0 and d[-1] & ((1 << rest) - 1) != 1 << (rest - 1)):
        fields['transform_8x8'], bit = _read_bits(d, bit, 1)
    return fields


def _parse_h264_slice_header(nal, sps, pps):
    """Parse a whole H.264 slice header under assumed SPS/PPS settings.

    Returns a dict (first_mb, slice_type, frame_num, poc_lsb, qp) or None
    when the header can't be read plausibly: a value out of range, or with
    CABAC, alignment bits that aren't all ones.
    """
    d = _rbsp(nal, 96)
    pos = [8]

    def ue():
        value, pos[0] = _read_ue(d, pos[0])
        if value is None:
            raise ValueError
        return value

    def se():
        value, pos[0] = _read_se(d, pos[0])
        if value is None:
            raise ValueError
        return value

    def u(count):
        value, pos[0] = _read_bits(d, pos[0], count)
        if value is None:
            raise ValueError
        return value

    try:
        nal_type, ref_idc = nal[0] & 0x1F, (nal[0] >> 5) & 3
        h = {'first_mb': ue()}
        slice_type = ue()
        if slice_type > 9:
            return None
        h['slice_type'] = slice_type = slice_type % 5   # 0 P, 1 B, 2 I, 3 SP, 4 SI
        if ue() != pps.get('pps_id', 0):
            return None
        h['frame_num'] = u(sps['log2_max_frame_num'])
        if nal_type == 5:
            ue()                                       # idr_pic_id
        h['poc_lsb'] = None
        if sps['poc_type'] == 0:
            h['poc_lsb'] = u(sps['log2_max_poc_lsb'])
            if pps.get('bottom_field_poc'):
                se()
        if pps.get('redundant'):
            ue()
        if slice_type == 1:
            u(1)                                       # direct_spatial_mv_pred
        n0, n1 = pps.get('ref_l0', 0), pps.get('ref_l1', 0)
        if slice_type in (0, 1, 3) and u(1):           # num_ref_idx_override
            n0 = ue()
            if slice_type == 1:
                n1 = ue()
        if n0 > 31 or n1 > 31:
            return None
        if slice_type not in (2, 4):                   # ref_pic_list_modification
            for _ in range(2 if slice_type == 1 else 1):
                if u(1):
                    for _ in range(33):
                        op = ue()
                        if op == 3:
                            break
                        if op > 5:
                            return None
                        ue()
                    else:
                        return None
        if ((pps.get('weighted_pred') and slice_type in (0, 3))
                or (pps.get('weighted_bipred') == 1 and slice_type == 1)):
            if ue() > 7 or ue() > 7:                   # luma/chroma log2 denominators
                return None
            for count in ((n0, n1) if slice_type == 1 else (n0,)):
                for _ in range(count + 1):
                    for _ in range(1 if u(1) else 0):
                        if not (-128 <= se() <= 127 and -128 <= se() <= 127):
                            return None
                    for _ in range(2 if u(1) else 0):
                        if not (-128 <= se() <= 127 and -128 <= se() <= 127):
                            return None
        if ref_idc:                                    # dec_ref_pic_marking
            if nal_type == 5:
                u(2)
            elif u(1):
                for _ in range(66):
                    op = ue()
                    if op == 0:
                        break
                    if op > 6:
                        return None
                    for _ in range({1: 1, 2: 1, 3: 2, 4: 1, 6: 1}.get(op, 0)):
                        ue()
                else:
                    return None
        if pps['entropy'] and slice_type not in (2, 4) and ue() > 2:
            return None                                # cabac_init_idc
        h['qp'] = 26 + pps.get('init_qp', 0) + se()
        if not 0 <= h['qp'] <= 51:
            return None
        if slice_type in (3, 4):
            if slice_type == 3:
                u(1)
            se()
        if pps['deblock_ctrl']:
            idc = ue()
            if idc > 2:
                return None
            if idc != 1 and not (-6 <= se() <= 6 and -6 <= se() <= 6):
                return None
        if pps['entropy']:
            while pos[0] % 8:                          # cabac_alignment_one_bit
                if u(1) != 1:
                    return None
        return h
    except (ValueError, IndexError):
        return None


def _slice_order_prefix(nal, log2_fn, poc_type, log2_poc):
    """(first_mb, frame_num, poc_lsb) from the start of a slice header, or None.

    These fields come before anything that depends on the PPS (other than
    pps_id), so the frame_num and POC sizes can be searched on their own.
    """
    d = _rbsp(nal, 32)
    first_mb, bit = _read_ue(d, 8)
    slice_type, bit = _read_ue(d, bit)
    pps_id, bit = _read_ue(d, bit)
    if first_mb is None or slice_type is None or slice_type > 9 or pps_id is None:
        return None
    frame_num, bit = _read_bits(d, bit, log2_fn)
    if frame_num is None:
        return None
    if nal[0] & 0x1F == 5:
        _, bit = _read_ue(d, bit)              # idr_pic_id
    lsb = None
    if poc_type == 0:
        lsb, bit = _read_bits(d, bit, log2_poc)
        if lsb is None:
            return None
    return first_mb, frame_num, lsb


def _order_regularity(slices, log2_fn, poc_type, log2_poc):
    """How regularly frame_num and POC progress under assumed sizes (0..1)."""
    pics = []
    for n in slices:
        fields = _slice_order_prefix(n, log2_fn, poc_type, log2_poc)
        if fields is None:
            return 0.0
        if fields[0] == 0:
            pics.append((n[0] & 0x1F == 5, bool(n[0] & 0x60), fields[1], fields[2]))
    if len(pics) < 3:
        return 0.0
    max_fn = 1 << log2_fn
    good, prev = 0, None
    for is_idr, is_ref, frame_num, _ in pics:
        if is_idr:
            good += frame_num == 0
        elif prev is not None:
            good += frame_num == (prev[0] + (1 if prev[1] else 0)) % max_fn
        prev = (frame_num, is_ref)
    fn_reg = good / len(pics)
    if poc_type != 0:
        return fn_reg
    # Rebuild POCs (with wraps) and check that within each IDR run the
    # sorted counts are evenly spaced, B-frames or not
    import collections
    max_lsb = 1 << log2_poc
    runs, run, prev_msb, prev_lsb = [], [], 0, 0
    for is_idr, is_ref, _, lsb in pics:
        if is_idr:
            if run:
                runs.append(run)
            run, prev_msb, prev_lsb = [], 0, 0
        if lsb < prev_lsb and prev_lsb - lsb >= max_lsb // 2:
            msb = prev_msb + max_lsb
        elif lsb > prev_lsb and lsb - prev_lsb > max_lsb // 2:
            msb = prev_msb - max_lsb
        else:
            msb = prev_msb
        run.append(msb + lsb)
        if is_ref:
            prev_msb, prev_lsb = msb, lsb
    runs.append(run)
    diffs = []
    for r in runs:
        ordered = sorted(r)
        diffs += [b - a for a, b in zip(ordered, ordered[1:])]
    if not diffs:
        return fn_reg
    step, count = collections.Counter(diffs).most_common(1)[0]
    return (fn_reg + (count / len(diffs) if step > 0 else 0.0)) / 2


def _header_shortlist(slices, pps_id):
    """Header-level settings that read the slices consistently, best first.

    Stage 1 sizes frame_num and the POC from the start of each header;
    stage 2 searches the PPS options that change what follows (entropy
    coding, deblocking control, weighted prediction) by full parses.
    """
    orders = []
    for log2_fn in range(4, 17):
        for poc_type, log2_poc in [(2, None)] + [(0, k) for k in range(4, 17)]:
            reg = _order_regularity(slices, log2_fn, poc_type, log2_poc)
            if reg:
                orders.append((reg, poc_type == 0, log2_fn, poc_type, log2_poc))
    if not orders:
        return []
    top = max(o[0] for o in orders)
    orders = sorted((o for o in orders if o[0] >= top - 0.1), reverse=True)[:6]
    has_b = any(_read_ue(_rbsp(n, 12), _read_ue(_rbsp(n, 12), 8)[1])[0] in (1, 6)
                for n in slices)
    results = []
    for reg, _, log2_fn, poc_type, log2_poc in orders:
        sps = {'log2_max_frame_num': log2_fn, 'poc_type': poc_type,
               'log2_max_poc_lsb': log2_poc}
        # With weighted prediction, each P/B header carries one weight
        # entry per reference, so the default reference count matters too
        weightings = [(0, 0, None)] + [(w, b, r) for w in (0, 1) for b in
                                       ((0, 1) if has_b else (0,))
                                       if w or b for r in list(range(8)) + [15]]
        for entropy in (0, 1):
            for deblock in (0, 1):
                for weighted, bipred, ref_l0 in weightings:
                        pps = {'pps_id': pps_id, 'entropy': entropy,
                               'deblock_ctrl': deblock, 'weighted_pred': weighted,
                               'weighted_bipred': bipred}
                        # Reference counts that weight tables depend on:
                        # P-frames with weighted prediction, B-frames with
                        # explicit weights
                        if ref_l0 is not None and weighted:
                            pps['ref_l0'] = ref_l0
                        if ref_l0 is not None and bipred == 1:
                            pps['ref_l1'] = ref_l0
                        parsed = [_parse_h264_slice_header(n, sps, pps) for n in slices]
                        rate = sum(1 for h in parsed if h) / len(slices)
                        if rate < 0.9:
                            continue
                        # Hypotheses that survive more checks are stronger
                        # evidence: CABAC alignment bits, deblocking fields,
                        # weight tables, POC regularity
                        strength = entropy + deblock + weighted + bipred + (poc_type == 0)
                        results.append((round(rate + reg, 3), strength,
                                        dict(sps, **pps)))
    if not results:
        return []
    top = max(r[0] for r in results)
    if top < 1.8:
        return []
    # Damaged slices fail under every hypothesis and lower the true one's
    # score too, so take everything close to the top, best-supported first
    close = [r for r in results if r[0] >= top - 0.1]
    close.sort(key=lambda r: (r[0], r[1], -r[2].get('ref_l0', 0)), reverse=True)
    return [r[2] for r in close[:40]]


def _keyframe_mbs_decoded(filepath, keyframe, params, check_bottom=False):
    """(macroblocks decoded cleanly, total) for one keyframe under params.

    check_bottom: also treat a bottom macroblock row that is all zeros (Y,
    U and V; real black is Y 16 with neutral chroma) as undecoded. A frame
    taller than the recording sometimes decodes "cleanly" with its extra
    rows left at zero instead of being reported as concealed.
    """
    import re
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.h264', delete=False) as out:
        tmp_path = out.name
        out.write(b'\x00\x00\x00\x01' + _build_h264_sps(params))
        out.write(b'\x00\x00\x00\x01' + _build_h264_pps(params))
        with open(filepath, 'rb') as src:
            for offset, length in keyframe:
                src.seek(offset)
                out.write(b'\x00\x00\x00\x01' + src.read(length))
    try:
        output = (['-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-']
                  if check_bottom else ['-f', 'null', '-'])
        r = subprocess.run(
            [_ffmpeg_path, '-hide_banner', '-loglevel', 'level+info', '-threads', '1',
             '-f', 'h264', '-i', tmp_path] + output,
            capture_output=True, timeout=30)
    finally:
        os.unlink(tmp_path)
    log = r.stderr.decode(errors='replace')
    mbw = (params['width'] + 15) // 16
    total = mbw * ((params['height'] + 15) // 16)
    if not re.search(r'frame=\s*1\b', log):
        return 0, total
    concealed = re.search(r'concealing (\d+) DC', log)
    if concealed:
        return max(0, total - int(concealed.group(1))), total
    if any(line.startswith('[error]') for line in log.splitlines()):
        return 0, total
    if check_bottom:
        w, h = params['width'], params['height']
        y_plane, chroma = r.stdout[:w * h], r.stdout[w * h:w * h * 3 // 2]
        last_rows = y_plane[w * max(0, h - 16):]
        last_chroma = chroma[(w // 2) * max(0, h // 2 - 8):]
        if last_rows and not any(last_rows) and not any(last_chroma[:len(last_chroma) // 2]):
            return total - mbw, total
    return total, total


def _keyframe_jump(filepath, nal_entries, params):
    """Picture change into keyframes relative to the usual frame-to-frame change.

    With the wrong base quantizer, P-frames apply mis-scaled corrections and
    the picture drifts until the next keyframe snaps it back. CAVLC parses
    the same with any quantizer, so this is how it is found there. Returns
    None with fewer than two keyframes.
    """
    import statistics
    import tempfile
    w, h = 160, 90
    pictures = []
    with tempfile.NamedTemporaryFile(suffix='.h264', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        _write_annex_b_stream(filepath, nal_entries, [_build_h264_sps(params)],
                              [_build_h264_pps(params)], tmp_path,
                              pictures=pictures)
        r = subprocess.run(
            [_ffmpeg_path, '-v', 'quiet', '-f', 'h264', '-i', tmp_path,
             '-vf', f'scale={w}:{h}', '-pix_fmt', 'gray', '-f', 'rawvideo', '-'],
            capture_output=True, timeout=120)
    finally:
        os.unlink(tmp_path)
    size = w * h
    frames = [r.stdout[i:i + size] for i in range(0, len(r.stdout) - size + 1, size)]
    keyframes = {i for i, p in enumerate(pictures) if p[0] and i}
    diffs = [sum(abs(a - b) for a, b in zip(frames[i], frames[i + 1])) / size
             for i in range(len(frames) - 1)]
    jumps = [diffs[i - 1] for i in keyframes if i <= len(diffs)]
    normal = [d for i, d in enumerate(diffs) if i + 1 not in keyframes]
    if len(jumps) < 2 or not normal or not statistics.median(normal):
        return None
    return statistics.mean(jumps) / statistics.median(normal)


# Trial decodes are separate ffmpeg runs, mostly process start-up, so they
# run side by side. The short ones are single-threaded (-threads 1) to keep
# the pool from oversubscribing the CPU.
_TRIAL_WORKERS = max(2, min(8, (os.cpu_count() or 4) // 2))


def _trial_map(fn, items, stop=None):
    """fn over items on parallel workers; results in item order.

    Runs in chunks of _TRIAL_WORKERS and stops after a chunk once
    stop(result) holds for any result in it (later items stay None). Picking
    the first or best result in item order then matches a sequential loop.
    """
    from concurrent.futures import ThreadPoolExecutor
    results = [None] * len(items)
    with ThreadPoolExecutor(_TRIAL_WORKERS) as pool:
        for start in range(0, len(items), _TRIAL_WORKERS):
            chunk = range(start, min(len(items), start + _TRIAL_WORKERS))
            for i, result in zip(chunk, pool.map(lambda k: fn(items[k]), chunk)):
                results[i] = result
            if stop and any(results[i] is not None and stop(results[i]) for i in chunk):
                break
    return results


def _trial_decode(filepath, nal_entries, params):
    """(problems, frames) for a short stream decoded under inferred params."""
    with tempfile.NamedTemporaryFile(suffix='.h264', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        _write_annex_b_stream(filepath, nal_entries, [_build_h264_sps(params)],
                              [_build_h264_pps(params)], tmp_path)
        return _decode_stats(['-threads', '1', '-f', 'h264', '-i', tmp_path], 60)
    finally:
        os.unlink(tmp_path)


def _infer_h264_params(filepath, nal_entries, hints=()):
    """Infer SPS/PPS for a moov-less H.264 stream. Returns ([], [sps], [pps]) or None.

    hints: parsed parameters of the available donors (likely base
    quantizers and frame sizes, tried first).
    """
    if not has_ffmpeg():
        return None
    # Slices from the first IDR on, and the first keyframe pictures
    slices, keyframes, current = [], [], None
    with open(filepath, 'rb') as f:
        for offset, length in nal_entries:
            f.seek(offset)
            head = f.read(min(length, 256))
            nal_type = head[0] & 0x1F
            if not 1 <= nal_type <= 5:
                continue
            if nal_type == 5 or slices:
                if len(slices) < 160:
                    slices.append(head)
            starts_picture = len(head) > 1 and head[1] & 0x80
            if starts_picture:
                if current:
                    keyframes.append(current)
                current = [(offset, length)] if nal_type == 5 else None
            elif current is not None:
                current.append((offset, length))
            if len(slices) >= 160 and len(keyframes) >= 3:
                break
    if current and len(keyframes) < 3:
        keyframes.append(current)
    if len(slices) < 10 or not keyframes:
        return None
    d = _rbsp(slices[0], 16)
    _, bit = _read_ue(d, 8)
    _, bit = _read_ue(d, bit)
    pps_id, _ = _read_ue(d, bit)
    has_b = any(_read_ue(_rbsp(n, 12), _read_ue(_rbsp(n, 12), 8)[1])[0] in (1, 6)
                for n in slices)
    shortlist = _header_shortlist(slices, pps_id or 0)
    if not shortlist:
        return None

    max_first = max(_read_ue(_rbsp(n, 8), 8)[0] or 0 for n in slices)
    mbs = lambda s: ((s[0] + 15) // 16) * ((s[1] + 15) // 16)
    sizes = [s for s in dict.fromkeys(
        [(h['width'], h['height']) for h in hints if 'width' in h]
        + _COMMON_FRAME_SIZES) if mbs(s) > max_first]
    # A frame that is too short decodes without complaint; one that is too
    # tall leaves rows concealed or blank. So the right size is the largest
    # that decodes completely.
    sizes_by_mbs = sorted(sizes, key=mbs, reverse=True)
    hint_qps = list(dict.fromkeys([h['init_qp'] for h in hints if 'init_qp' in h] + [0]))
    qps = list(dict.fromkeys(hint_qps + sorted(range(-16, 16), key=abs)))
    test_nals = nal_entries[:240]

    # Keyframes don't depend on weighted prediction or reference counts, so
    # the expensive keyframe stage runs once per group of variants
    groups = {}
    for hyp in shortlist:
        key = (hyp['log2_max_frame_num'], hyp['poc_type'], hyp['log2_max_poc_lsb'],
               hyp['entropy'], hyp['deblock_ctrl'])
        groups.setdefault(key, []).append(hyp)

    for variants in groups.values():
        hyp = variants[0]
        # Base quantizer and 8x8 transform: CABAC seeds its state from the
        # slice quantizer, so only the right pair decodes a keyframe. Any
        # plausible frame size works for this; the first rows decode.
        w0, h0 = sizes[0]
        grid = [(key, transform, qp) for key in keyframes for transform in (1, 0)
                for qp in (qps if hyp['entropy'] else hint_qps[:1])]
        scores = _trial_map(
            lambda c: _keyframe_mbs_decoded(filepath, c[0], dict(
                hyp, width=w0, height=h0, transform_8x8=c[1], init_qp=c[2])),
            grid, stop=lambda r: r[0] == r[1])
        found, found_frac, found_key = None, 0.0, None
        for (key, transform, qp), score in zip(grid, scores):
            if score and score[0] / score[1] > found_frac:
                found, found_frac, found_key = (transform, qp), score[0] / score[1], key
        if not found:
            continue
        complete = _trial_map(
            lambda s: _keyframe_mbs_decoded(filepath, found_key, dict(
                hyp, width=s[0], height=s[1], transform_8x8=found[0],
                init_qp=found[1]), check_bottom=True)[0] == mbs(s),
            sizes_by_mbs, stop=bool)
        size = next((s for s, ok in zip(sizes_by_mbs, complete) if ok), None)
        if not size:
            continue
        frame = dict(width=size[0], height=size[1], transform_8x8=found[0],
                     init_qp=found[1])

        # Settings that never cause decode errors but change the picture:
        # scaling matrices (flat, or the spec's defaults that Nikon and
        # Fujifilm declare) and, for CAVLC, the base quantizer. Wrong values
        # mis-scale P-frame corrections, so the picture drifts until the
        # next keyframe snaps it back; the right ones leave no jump.
        hint_scaling = next((h['default_scaling'] for h in hints
                             if 'default_scaling' in h), False)
        cavlc_qps = qps[:24] if not hyp['entropy'] else [frame['init_qp']]

        def jumps_for(options):
            measured = _trial_map(
                lambda o: _keyframe_jump(filepath, nal_entries[:900], dict(
                    hyp, **dict(frame, init_qp=o[1], default_scaling=o[0]))),
                options)
            return [(j, qp, sc) for (sc, qp), j in zip(options, measured) if j]

        # The jump has a sharp minimum, so search one setting at a time: the
        # quantizer with the likelier matrices, then the other matrices at
        # that quantizer (25 decodes for CAVLC instead of 48)
        jumps = jumps_for([(hint_scaling, qp) for qp in cavlc_qps])
        if jumps:
            jumps += jumps_for([(not hint_scaling, min(jumps)[1])])
        if jumps:
            _, frame['init_qp'], frame['default_scaling'] = min(jumps)
        else:
            frame['default_scaling'] = hint_scaling
            if not hyp['entropy'] and not any('init_qp' in h for h in hints):
                # Under two keyframes nothing reveals a CAVLC quantizer, and
                # a wrong one decodes "cleanly" into a wrong picture. Only a
                # donor's value is worth trying.
                continue

        group_best = None
        for variant in variants:
            params = dict(variant, **frame)
            # Settings only P/B frames use. Implicit weighted bi-prediction
            # (weighted_bipred_idc 2) changes decoding but not the headers.
            bipreds = ((params['weighted_bipred'],) if params.get('weighted_bipred')
                       else (0, 2))
            ref_counts = ((params['ref_l0'],) if 'ref_l0' in params else (0, 1, 2, 3))
            l1_counts = ((params['ref_l1'],) if 'ref_l1' in params
                         else (0, 1) if has_b else (0,))
            cands = [dict(params, ref_l0=r0, ref_l1=r1, direct_8x8=direct,
                          weighted_bipred=bipred)
                     for bipred in bipreds for r0 in ref_counts
                     for r1 in l1_counts for direct in (1, 0)]
            results = _trial_map(
                lambda c: _trial_decode(filepath, test_nals, c), cands,
                stop=lambda r: r[0] == 0)
            best = min(((r[0], -r[1]), i) for i, r in enumerate(results) if r)
            best = (best[0], cands[best[1]])
            if group_best is None or best[0] < group_best[0]:
                group_best = best
            if best[0][0] == 0:
                break
        problems, frames = group_best[0][0], -group_best[0][1]
        # Damage in the data shows up here too; the full recovery is
        # checked strictly afterwards, so only reject clearly wrong settings
        if frames and problems <= frames * 0.25 + 2:
            p = group_best[1]
            return [], [_build_h264_sps(p)], [_build_h264_pps(p)]
    return None


def _display_ranks(pictures):
    """Display position of each picture (given in decode order), or None.

    pictures: (is_idr, is_reference, poc_lsb, max_poc_lsb) tuples from
    _write_annex_b_stream. Derives the picture order count (H.264 8.2.1.1,
    POC type 0) and ranks pictures by it between IDRs, where it restarts.
    """
    if not pictures or any(p[2] is None for p in pictures):
        return None
    pocs = []
    prev_msb = prev_lsb = 0
    for is_idr, is_ref, lsb, max_lsb in pictures:
        if is_idr:
            prev_msb = prev_lsb = 0
        if lsb < prev_lsb and prev_lsb - lsb >= max_lsb // 2:
            msb = prev_msb + max_lsb
        elif lsb > prev_lsb and lsb - prev_lsb > max_lsb // 2:
            msb = prev_msb - max_lsb
        else:
            msb = prev_msb
        pocs.append(msb + lsb)
        if is_ref:
            prev_msb, prev_lsb = msb, lsb
    ranks = [0] * len(pictures)
    start = 0
    for i in range(1, len(pictures) + 1):
        if i == len(pictures) or pictures[i][0]:
            order = sorted(range(start, i), key=lambda k: (pocs[k], k))
            # B-frame reordering moves a frame a few places. Larger moves
            # mean the POC couldn't be reconstructed (it is stored modulo
            # max_poc_lsb, and damaged frames dropped from the run hide
            # the wraps, or the run came from data whose slice headers
            # don't fit these parameter sets), so keep decode order.
            run = pocs[start:i]
            if (any(abs(pos - (k - start)) > 16 for pos, k in enumerate(order))
                    or (pictures[start][0] and pictures[start][2] != 0)
                    or len(set(run)) != len(run)):
                # Also distrust runs that don't look like a real sequence:
                # an IDR's count starts at 0 and counts never repeat
                order = list(range(start, i))
            for pos, k in enumerate(order):
                ranks[k] = start + pos
            start = i
    return ranks


def _apply_presentation_order(mp4_path, pictures):
    """Give a file muxed from a raw H.264 stream its display order.

    A raw stream has no timestamps, and ffmpeg stamps each frame in decode
    order, so with B-frames (stored after the frame they precede on screen)
    the video jitters. This adds composition offsets (ctts) to the video
    track from the picture order counts in the slice headers.
    Returns True when the file was changed; leaves it alone when the order
    can't be determined or needs no change.
    """
    ranks = _display_ranks(pictures)
    if not ranks or ranks == list(range(len(ranks))):
        return False
    stem, ext = os.path.splitext(mp4_path)
    tmp_path = stem + '.order' + ext
    try:
        # Remux with the moov last, so growing it doesn't move the media data
        r = subprocess.run([_ffmpeg_path, '-y', '-v', 'error', '-i', mp4_path,
                            '-map', '0', '-c', 'copy', tmp_path],
                           capture_output=True, timeout=600)
        if r.returncode != 0:
            return False
        top = parse_boxes(tmp_path)
        if not top or top[-1][0] != 'moov':
            return False
        _, moov_off, moov_size = top[-1]
        with open(tmp_path, 'rb') as f:
            f.seek(moov_off)
            moov = bytearray(f.read(moov_size))

        def children(start, end):
            pos = start
            while pos + 8 <= end:
                size = struct.unpack_from('>I', moov, pos)[0]
                if size < 8 or pos + size > end:
                    return
                yield moov[pos + 4:pos + 8].decode('latin1'), pos, size
                pos += size

        def child(start, end, name):
            return next(((p, sz) for t, p, sz in children(start, end) if t == name),
                        (None, None))

        target = None
        for t, trak, trak_size in children(8, len(moov)):
            if t != 'trak':
                continue
            mdia, mdia_size = child(trak + 8, trak + trak_size, 'mdia')
            if mdia is None:
                continue
            hdlr, _ = child(mdia + 8, mdia + mdia_size, 'hdlr')
            if hdlr is None or moov[hdlr + 16:hdlr + 20] != b'vide':
                continue
            minf, minf_size = child(mdia + 8, mdia + mdia_size, 'minf')
            stbl, stbl_size = child(minf + 8, minf + minf_size, 'stbl') if minf else (None, None)
            if stbl is None:
                continue
            target = (trak, mdia, minf, stbl, stbl_size)
            break
        if target is None:
            return False
        trak, mdia, minf, stbl, stbl_size = target
        stts, stts_size = child(stbl + 8, stbl + stbl_size, 'stts')
        stsz, _ = child(stbl + 8, stbl + stbl_size, 'stsz')
        if stts is None or stsz is None or child(stbl + 8, stbl + stbl_size, 'ctts')[0]:
            return False
        if struct.unpack_from('>I', moov, stsz + 16)[0] != len(ranks):
            return False                       # frames don't line up
        deltas = {struct.unpack_from('>I', moov, stts + 16 + 8 * i + 4)[0]
                  for i in range(struct.unpack_from('>I', moov, stts + 12)[0])}
        if len(deltas) != 1:
            return False
        delta = deltas.pop()

        # Offsets must be non-negative (ctts version 0): delay every frame
        # by the largest reorder distance; the final remux adds an edit list
        shift = max(i - r for i, r in enumerate(ranks))
        runs = []
        for i, r in enumerate(ranks):
            offset = (r - i + shift) * delta
            if runs and runs[-1][1] == offset:
                runs[-1][0] += 1
            else:
                runs.append([1, offset])
        ctts = struct.pack('>II', 16 + 8 * len(runs), 0x63747473) + struct.pack(
            '>II', 0, len(runs)) + b''.join(struct.pack('>II', n, o) for n, o in runs)

        insert_at = stts + stts_size
        moov[insert_at:insert_at] = ctts
        for box in (0, trak, mdia, minf, stbl):
            struct.pack_into('>I', moov, box,
                             struct.unpack_from('>I', moov, box)[0] + len(ctts))
        with open(tmp_path, 'r+b') as f:
            f.truncate(moov_off)
            f.seek(moov_off)
            f.write(moov)

        ordered_path = stem + '.ordered' + ext
        r = subprocess.run([_ffmpeg_path, '-y', '-v', 'error', '-i', tmp_path,
                            '-map', '0', '-c', 'copy', '-movflags', '+faststart',
                            ordered_path], capture_output=True, timeout=600)
        if r.returncode != 0:
            return False
        os.replace(ordered_path, mp4_path)
        return True
    finally:
        for path in (tmp_path, stem + '.ordered' + ext):
            if os.path.exists(path):
                os.unlink(path)


def _most_common_rate_donor(donors):
    """The donor whose framerate most of the donors share.

    The same SPS can come from different framerates (iPhone slo-mo uses the
    1080p SPS at 120 fps), and a moov-less file has no timing of its own, so
    the most common rate among matching donors is the best guess.
    """
    import collections
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


def recover_no_moov(filepath, donor_path, output_dir=None):
    """Recover a video file missing its moov atom.

    Strategy 1: Raw NAL extraction — scan mdat for H.264 or HEVC NALs,
    get parameter sets from the file itself or the donor's avcC/hvcC, write
    Annex-B stream, wrap with ffmpeg.
    Strategy 2 (fallback): Moov transplant from donor.
    donor_path may be empty when the file carries its own SPS/PPS.
    Returns (output_path, method_used) on success, raises ValueError on failure.
    """
    donor_moov = _extract_moov(donor_path) if donor_path else None
    if donor_path and not donor_moov:
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
    h264_params = _extract_avcc_params(donor_moov) if donor_moov else None
    hevc_params = _extract_hvcc_params(donor_moov) if donor_moov else None
    if h264_params:
        codec_attempts.append(('h264', h264_params))
    if hevc_params:
        codec_attempts.append(('hevc', hevc_params))
    # If donor only has one codec, still try the other with default nal_length_size
    if not h264_params and hevc_params:
        codec_attempts.insert(0, ('h264', {'nal_length_size': 4, 'sps_list': [], 'pps_list': []}))
    if not hevc_params and h264_params:
        codec_attempts.append(('hevc', {'nal_length_size': 4, 'vps_list': [], 'sps_list': [], 'pps_list': []}))
    if not donor_moov:
        codec_attempts = [
            ('h264', {'nal_length_size': 4, 'sps_list': [], 'pps_list': []}),
            ('hevc', {'nal_length_size': 4, 'vps_list': [], 'sps_list': [], 'pps_list': []})]

    raw_failure = None   # why the best raw extraction was rejected
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
                # Candidate SPS/PPS: the file's own first (exact match for the
                # recording), then the donor pool's
                fdir = os.path.dirname(filepath)
                inband = _find_inband_params(
                    filepath, damaged_mdat_offset, nal_entries, codec=codec)
                sps_candidates = (_collect_sps_candidates(fdir, params, codec=codec)
                                  if donor_moov else [])
                if inband:
                    sps_candidates = [inband] + [
                        c for c in sps_candidates if c[1] != inband[1]]
                sps_candidates = [c for c in sps_candidates if c[1] and c[2]]
                best_score = None
                if sps_candidates:
                    (best_vps, best_sps, best_pps), best_score = _probe_sps_candidates(
                        filepath, nal_entries, sps_candidates, codec=codec,
                        return_score=True)
                # No donor decodes this stream cleanly, or there is none:
                # infer the parameters from the stream itself, with the
                # donors' quantizers and frame sizes as hints
                inferred = None
                if codec == 'h264' and (best_score is None or best_score > 0):
                    hints = []
                    for _, c_sps, c_pps in sps_candidates:
                        order = _parse_h264_sps_order(c_sps[0])
                        c_params = _parse_h264_pps(c_pps[0])
                        if order and c_params:
                            hints.append(dict(c_params, width=order['width'],
                                              height=order['height'],
                                              default_scaling=order['scaling'] == 1))
                    inferred = _infer_h264_params(filepath, nal_entries, hints)
                    if inferred:
                        # Inference ran because no donor decoded cleanly, and
                        # its result was checked across several keyframes and
                        # P/B frames, so it wins. (A short probe from the
                        # first keyframe can't judge: that keyframe may be
                        # the damaged part.)
                        best_vps, best_sps, best_pps = inferred
                if not sps_candidates and not inferred:
                    continue
                used_inferred = bool(inferred) and best_sps == inferred[1]

                # Find the donor that matches the best SPS for framerate.
                # With the file's own SPS and no matching donor, ffmpeg takes
                # the framerate from the stream's timing info instead.
                fps_donor = None
                matching = []
                for pool_file in _donor_files(fdir):
                    pm = _extract_moov(pool_file)
                    if pm:
                        pp = extract_fn(pm)
                        if pp and (pp['sps_list'] == best_sps or (
                                used_inferred and _decoding_signature(
                                    pp['sps_list'], pp['pps_list'])
                                == _decoding_signature(best_sps, best_pps))):
                            matching.append(pool_file)
                            if len(matching) >= 15:
                                break
                if matching:
                    fps_donor = _most_common_rate_donor(matching)
                if not fps_donor and not (inband and best_sps == inband[1]) \
                        and not used_inferred:
                    fps_donor = donor_path

                raw_path = out_path + '.' + ffmpeg_fmt
                pictures = []
                try:
                    _write_annex_b_stream(
                        filepath, nal_entries, best_sps, best_pps,
                        raw_path, vps_list=best_vps, codec=codec,
                        pictures=pictures)

                    fps = rotation = None
                    if fps_donor:
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
                    elif used_inferred:
                        # Inferred SPS carry no timing, and no donor shares
                        # the camera's settings: 30 fps is the likeliest
                        fps = '30'

                    ffmpeg_cmd = [_ffmpeg_path, '-y']
                    if fps:
                        ffmpeg_cmd += ['-framerate', fps]
                    if rotation:
                        ffmpeg_cmd += [
                            '-display_rotation:v:0',
                            str(rotation)]
                    ffmpeg_cmd += ['-f', ffmpeg_fmt, '-i', raw_path, '-c:v', 'copy']
                    if fps:
                        ffmpeg_cmd += ['-video_track_timescale', fps.split('/')[0]]
                    ffmpeg_cmd += ['-movflags', '+faststart']
                    if codec == 'hevc':
                        ffmpeg_cmd += ['-tag:v', 'hvc1']
                    ffmpeg_cmd.append(out_path)

                    result = subprocess.run(
                        ffmpeg_cmd, capture_output=True, timeout=300)

                    if result.returncode == 0 and os.path.exists(out_path):
                        # B-frames need their display order restored
                        _apply_presentation_order(out_path, pictures)
                        out_size = os.path.getsize(out_path)
                        if out_size > damaged_mdat_size * 0.01:
                            # Decode-test: reject unless the frames decode
                            try:
                                ok, msg = _check_decodes_cleanly(out_path)
                                if not ok:
                                    raw_failure = msg
                                    os.unlink(out_path)
                                    continue  # try next codec
                            except subprocess.TimeoutExpired:
                                pass
                            except Exception:
                                pass
                            if inband and best_sps == inband[1]:
                                return out_path, 'raw_nal_extraction (embedded SPS/PPS)'
                            if used_inferred:
                                return out_path, 'raw_nal_extraction (inferred SPS/PPS)'
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
    if not donor_moov:
        raise ValueError('No compatible donor found, and the decoder settings '
                         'could not be read from the file or inferred from '
                         'its frames. Choose a donor folder with healthy '
                         'videos from the same camera.')
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
                        ok, msg = _check_decodes_cleanly(out_path)
                        if not ok:
                            os.unlink(out_path)
                            # The transplant is a long shot; the raw
                            # extraction result says more about why
                            raise ValueError(
                                'None of the available donors decode this '
                                'video (' + (raw_failure or msg) + '). '
                                'Choose a donor folder with healthy videos '
                                'from the same camera and recording mode.')
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

    # The remux failed (or there is no ffmpeg). Without ffmpeg nothing can be
    # checked; otherwise the raw transplant must decode like any other result.
    if has_ffmpeg():
        ok, msg = _check_decodes_cleanly(out_path)
        if not ok:
            os.unlink(out_path)
            raise ValueError(
                'None of the available donors decode this video ('
                + (raw_failure or msg) + '). Choose a donor folder with '
                'healthy videos from the same camera and recording mode.')
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
                        for pool_file in _donor_files(fdir):
                            pm = _extract_moov(pool_file)
                            if pm:
                                pp = extract_fn(pm)
                                if pp and pp['sps_list'] == best_sps:
                                    fps_donor = pool_file
                                    break

                        raw_path = out_path + '.' + ffmpeg_fmt
                        try:
                            pictures = []
                            _write_annex_b_stream(
                                filepath, nal_entries,
                                best_sps, best_pps, raw_path,
                                vps_list=best_vps, codec=codec,
                                pictures=pictures)

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
                                # B-frames need their display order restored
                                _apply_presentation_order(out_path, pictures)
                                # Decode-test: reject unless the frames decode
                                try:
                                    ok, _ = _check_decodes_cleanly(out_path)
                                    if not ok:
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

def scan_directory_gen(root_dir, scan_id=None):
    """Generator that scans a directory for video files and yields SSE events.

    Stops when the scan is cancelled or a newer scan (different scan_id) starts.
    """

    def cancelled():
        return (scan_state['cancelled'] or
                (scan_id is not None and scan_state.get('scan_id') != scan_id))

    if not os.path.exists(root_dir):
        yield {'type': 'error', 'message': f'Path does not exist: {root_dir}'}
        return

    if not os.path.isdir(root_dir):
        yield {'type': 'error', 'message': f'Not a directory: {root_dir}'}
        return

    yield {'type': 'counting', 'found': 0, 'dirs': 0, 'current': root_dir}

    # Walking a large drive can take a while; report progress so the UI
    # doesn't look stuck before the first file is checked
    all_files = []
    dirs_walked = 0
    last_report = time.time()
    for dirpath, dirnames, filenames in os.walk(root_dir):
        if cancelled():
            yield {'type': 'cancelled', 'scanned': 0, 'issues': 0}
            return
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d != '__MACOSX']
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in VIDEO_EXTS or ext in OTHER_VIDEO_EXTS:
                all_files.append(os.path.join(dirpath, fname))
        dirs_walked += 1
        if time.time() - last_report >= 0.25:
            last_report = time.time()
            yield {'type': 'counting', 'found': len(all_files),
                   'dirs': dirs_walked, 'current': dirpath}

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
        if cancelled():
            yield {'type': 'cancelled', 'scanned': scanned, 'issues': issue_count}
            return

        batch = all_files[batch_start:batch_start + batch_size]
        futures = {}

        # Not a `with` block: its exit waits for every in-flight file, which
        # made Cancel appear to hang on large files
        executor = ThreadPoolExecutor(max_workers=WORKER_THREADS)
        try:
            for fpath in batch:
                fut = executor.submit(check_video, fpath, cancelled)
                futures[fut] = fpath

            for fut in as_completed(futures):
                if cancelled():
                    break

                fpath = futures[fut]

                try:
                    status, reason, details = fut.result()
                except ScanCancelled:
                    break
                except Exception as e:
                    status, reason, details = 'error', str(e), {}
                scanned += 1

                fname = os.path.basename(fpath)
                fdir = os.path.dirname(fpath)
                ext = os.path.splitext(fname)[1].lower()
                fsize = details.get('file_size', 0)
                # Mark the folder as scanned even if it has no healthy files,
                # so _ensure_donor_pool doesn't recheck it during recovery
                donor_pool.setdefault(fdir, [])

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

                if status == 'not_video':
                    # Neither an issue nor usable as a recovery donor
                    continue
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
                elif _usable_donor(status, details):
                    donor_pool.setdefault(fdir, []).append(fpath)
        finally:
            # In-flight checks stop on their own via the cancelled() hook
            for fut in futures:
                fut.cancel()
            executor.shutdown(wait=False)

        if cancelled():
            yield {'type': 'cancelled', 'scanned': scanned, 'issues': issue_count}
            return

    elapsed = round(time.time() - start_time, 1)
    yield {'type': 'complete', 'scanned': scanned, 'issues': issue_count,
           'elapsed': elapsed}


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


def recover_avi(filepath, output_dir=None):
    """Salvage the intact frames of a damaged or cut-short AVI.

    Keeps every chunk up to where valid chunks stop (_walk_avi), fixes the
    RIFF and movi sizes, and remuxes with ffmpeg so the index is rebuilt.
    Writes MOV, which QuickTime plays (Motion JPEG and PCM included), and
    falls back to AVI for codecs MOV can't hold.
    Returns (output_path, method_used); raises ValueError on failure.
    """
    if not has_ffmpeg():
        raise ValueError('AVI recovery requires ffmpeg')
    walk = _walk_avi(filepath)
    if not walk or walk['video_chunks'] <= 0:
        raise ValueError('No intact AVI video frames found')

    good_end = walk['good_end']
    movi = walk['movi_list']
    stem = Path(filepath).stem
    out_dir = output_dir or os.path.dirname(filepath)
    tmp_path = os.path.join(out_dir, f'.{stem}_salvage.avi')
    try:
        with open(filepath, 'rb') as src, open(tmp_path, 'wb') as out:
            remaining = good_end
            while remaining > 0:
                chunk = src.read(min(1 << 20, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
            out.seek(4)
            out.write(struct.pack('<I', good_end - 8))
            out.seek(movi + 4)
            out.write(struct.pack('<I', good_end - movi - 8))

        for ext in ('.mov', '.avi'):
            out_path = os.path.join(out_dir, f'{stem}_recovered{ext}')
            result = subprocess.run(
                [_ffmpeg_path, '-y', '-v', 'error', '-i', tmp_path,
                 '-map', '0', '-c', 'copy', out_path],
                capture_output=True, timeout=600)
            if result.returncode == 0 and os.path.exists(out_path) \
                    and os.path.getsize(out_path) > 0:
                break
            if os.path.exists(out_path):
                os.unlink(out_path)
        else:
            raise ValueError('ffmpeg could not rebuild the AVI: '
                             + result.stderr.decode(errors='replace')[-200:])
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    ok, msg = _check_decodes_cleanly(out_path)
    if not ok:
        os.unlink(out_path)
        raise ValueError(f'Salvaged AVI does not decode cleanly ({msg})')
    return out_path, 'avi_salvage'


def _execute_recovery(filepath, donor_override='', output_dir=''):
    """Run recovery on a single file. Returns result dict on success."""
    fdir = os.path.dirname(filepath)
    _ensure_donor_pool(fdir)

    donor_path = donor_override
    if not donor_path:
        donor_path = donor_map.get(fdir, '')

    donor_auto = ''
    if not donor_path or not os.path.exists(donor_path):
        pool = _donor_files(fdir)
        if pool:
            donor_path = _find_matching_donor(filepath, pool)
            if donor_path:
                donor_auto = os.path.basename(donor_path)

    if not donor_path or not os.path.exists(donor_path):
        donor_path = ''

    if not output_dir:
        output_dir = DEFAULT_OUTPUT_DIR

    with open(filepath, 'rb') as f:
        head = f.read(12)
    all_zero = all(b == 0 for b in head[:8])
    is_avi = head[:4] == b'RIFF' and head[8:12] == b'AVI '

    # Without a donor, only a file carrying its own SPS/PPS can be decoded;
    # recover_no_moov checks for that and explains when it can't
    if not donor_path and all_zero and not is_avi:
        raise ValueError('No compatible donor found. '
                         'Scan a directory with healthy files from '
                         'the same camera first.')

    if is_avi:
        out_path, method = recover_avi(filepath, output_dir)
        donor_auto = ''
    elif all_zero:
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
    # A file decoded with its own or inferred SPS/PPS didn't use the donor
    if donor_auto and 'embedded' not in method and 'inferred' not in method:
        resp['donor_used'] = donor_auto
    return resp


# Clues to a damaged file's source camera, for choosing a donor. Strong
# clues come from where the camera itself writes them; text found loose in
# the data may belong to another file (carved files mix files' data).
_BRAND_MAKERS = {'niko': 'Nikon', 'CAEP': 'Canon', 'XAVC': 'Sony',
                 'MSNV': 'Sony', 'pana': 'Panasonic', 'FUJI': 'Fujifilm'}
# user_data_unregistered SEI that Apple's video encoder puts in every stream
_APPLE_SEI_UUID = bytes.fromhex('47564adc5c4c433f94efc5113cd143a8')


def _source_hints(filepath):
    """Clues to the camera and date of a damaged MP4/MOV, for choosing a donor.

    Returns {'camera': [(name, strong)], 'dates': [YYYY-MM-DD, ...],
    'resolution': 'WxH' or None}.
    """
    import re
    import collections
    hints = {'camera': [], 'dates': [], 'resolution': None}
    try:
        with open(filepath, 'rb') as f:
            head = f.read(8 << 20)
    except OSError:
        return hints

    def add_camera(name, strong):
        if all(name.lower() not in c[0].lower() and c[0].lower() not in name.lower()
               for c in hints['camera']):
            hints['camera'].append((name, strong))

    # Strong: brand tags in ftyp, GoPro's header, Apple's encoder marker
    if head[4:8] == b'ftyp':
        size = int.from_bytes(head[:4], 'big')
        for i in range(8, min(size, 64), 4):
            maker = _BRAND_MAKERS.get(head[i:i + 4].decode('latin1'))
            if maker:
                add_camera(maker, True)
    gpro = head.find(b'GPRO', 0, 4096)
    if gpro != -1:
        model = re.search(rb'HERO\d+[A-Za-z ]{0,12}', head[gpro:gpro + 2048])
        add_camera('GoPro ' + model.group().decode().strip() if model else 'GoPro', True)
    if _APPLE_SEI_UUID in head:
        add_camera('Apple iPhone or iPad', True)

    # Weak: maker and model names, and dates, found as text in the data
    text_models = re.findall(
        rb'NIKON (?:D\d{1,4}[A-Z]*|Z ?\d+|COOLPIX ?[A-Z]?\d+)|Canon (?:EOS|PowerShot|IXUS)[A-Za-z0-9 ]{1,16}'
        rb'|FUJIFILM [A-Z]{1,3}-?\d{1,3}[A-Z]?|SONY (?:ILCE|DSC|FDR|HDR)-[A-Z0-9]+'
        rb'|DMC-[A-Z0-9]+|DC-[A-Z0-9]+|iPhone(?: \d+[A-Za-z ]{0,8})?|DJI [A-Za-z0-9]{2,12}', head)
    for model, _ in collections.Counter(m.decode('latin1').strip() for m in text_models).most_common(3):
        add_camera(model, False)
    dates = collections.Counter(
        m.group(1).decode() + '-' + m.group(2).decode() + '-' + m.group(3).decode()
        for m in re.finditer(rb'((?:19|20)\d\d)[:-]([01]\d)[:-]([0-3]\d)[T ][0-2]\d:[0-5]\d', head))
    hints['dates'] = [d for d, _ in dates.most_common(3) if not d.startswith(('1970', '2000-01-01'))]

    # Resolution: from the file's own SPS, or from where the second slice of
    # each picture starts in cameras that split pictures into slices
    try:
        boxes = parse_boxes(filepath)
        mdat = next(((off, size) for bt, off, size in boxes if bt == 'mdat'), None)
        if mdat:
            nal_entries = _scan_mdat_for_nals(filepath, mdat[0], min(mdat[1], 32 << 20), 4)
            inband = _find_inband_params(filepath, mdat[0], nal_entries)
            order = inband and _parse_h264_sps_order(inband[1][0])
            if order:
                hints['resolution'] = f"{order['width']}x{order['height']}"
            else:
                starts, pictures = collections.Counter(), 0
                with open(filepath, 'rb') as f:
                    for offset, length in nal_entries[:400]:
                        f.seek(offset)
                        nal = f.read(16)
                        if 1 <= nal[0] & 0x1F <= 5:
                            first_mb = _read_ue(_rbsp(nal), 8)[0] or 0
                            starts[first_mb] += 1
                            pictures += first_mb == 0
                # Damaged slices report odd starts; use those most pictures share
                later = [mb for mb, count in starts.items()
                         if mb and count >= pictures * 0.5]
                if pictures and len(later) == 1:
                    per_picture = round((pictures + starts[later[0]]) / pictures)
                    if per_picture == 2:
                        # The second slice starts on a macroblock row, roughly
                        # halfway down: a multiple of the width, about half
                        # the total
                        start = later[0]
                        fits = [w_h for w_h in _COMMON_FRAME_SIZES
                                if start % ((w_h[0] + 15) // 16) == 0
                                and 1.8 * start <= ((w_h[0] + 15) // 16)
                                * ((w_h[1] + 15) // 16) <= 2.2 * start]
                        if fits:
                            size = min(fits, key=lambda w_h: abs(
                                ((w_h[0] + 15) // 16) * ((w_h[1] + 15) // 16) - 2 * start))
                            hints['resolution'] = f'{size[0]}x{size[1]}'
    except Exception:
        pass
    return hints


def _check_output_dir(path):
    """(absolute folder, error message) for a requested output folder.

    Empty means the default. Checked when a job is queued, so a missing or
    read-only folder (an unmounted drive, say) fails right away instead of
    after the recovery has run.
    """
    if not path:
        return DEFAULT_OUTPUT_DIR, None
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return None, f'Output folder not found: {path}'
    if not os.access(path, os.W_OK):
        return None, f'Output folder is not writable: {path}'
    return path, None


def _recovery_worker():
    """Daemon thread processing recovery jobs sequentially."""
    while True:
        job = _recover_queue.get()
        job_id = job['id']
        with _recover_lock:
            # Skip jobs cancelled while waiting (they were removed from the list)
            if job_id not in _recover_jobs:
                _recover_queue.task_done()
                continue
            _recover_jobs[job_id]['status'] = 'recovering'
            _recover_jobs[job_id]['started_at'] = time.time()
        try:
            result = _execute_recovery(
                job['filepath'], job.get('donor', ''), job.get('output_dir', ''))
            with _recover_lock:
                _recover_jobs[job_id]['status'] = 'done'
                _recover_jobs[job_id]['result'] = result
                _recover_jobs[job_id]['finished_at'] = time.time()
        except Exception as e:
            hints = None
            if os.path.splitext(job['filepath'])[1].lower() in VIDEO_EXTS - {'.avi'}:
                try:
                    hints = _source_hints(job['filepath'])
                except Exception:
                    pass
            with _recover_lock:
                _recover_jobs[job_id]['status'] = 'failed'
                _recover_jobs[job_id]['error'] = str(e)
                _recover_jobs[job_id]['hints'] = hints
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

    def end_headers(self):
        # Make browsers revalidate static assets so CSS/JS edits show up on reload
        if not self._route_path().startswith('/api/'):
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

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
            elif path == '/api/recover-cancel':
                self._handle_recover_cancel()
            elif path == '/api/load-scan':
                self._handle_load_scan()
            elif path == '/api/add-donor-folder':
                self._handle_add_donor_folder()
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

        # A new id makes any still-running previous scan stop on its own
        scan_state['scan_id'] += 1
        scan_id = scan_state['scan_id']
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
            for event in scan_directory_gen(root_dir, scan_id):
                if event.get('type') == 'issue':
                    scan_state['results'].append(event)
                line = f"data: {json.dumps(event, default=str)}\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if scan_state['scan_id'] == scan_id:
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
            'default_output_dir': DEFAULT_OUTPUT_DIR,
        })

    def _handle_reveal(self):
        data = self._read_json_body()
        filepath = data.get('filepath', '')
        if not filepath or not os.path.exists(filepath):
            return self._send_json({'error': 'File not found'}, 404)

        # open: show a folder's contents instead of selecting it in its parent
        if data.get('open') and os.path.isdir(filepath):
            opener = {'darwin': 'open', 'win32': 'explorer'}.get(sys.platform, 'xdg-open')
            try:
                subprocess.Popen([opener, filepath])
            except Exception as e:
                return self._send_json({'error': str(e)}, 500)
            return self._send_json({'ok': True})

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

    def _handle_load_scan(self):
        # A previous scan picked in the browser becomes the results Export uses
        if scan_state.get('running'):
            return self._send_json({'error': 'A scan is running'}, 409)
        results = self._read_json_body().get('results')
        if not isinstance(results, list):
            return self._send_json({'error': 'No results provided'}, 400)
        scan_state['results'] = results
        self._send_json({'ok': True, 'count': len(results)})

    def _handle_add_donor_folder(self):
        # The user points at a folder of healthy videos from the same camera;
        # they become donor candidates for the damaged file's folder, and SPS
        # probing picks the one that matches
        data = self._read_json_body()
        filepath = data.get('filepath', '')
        donor_dir = os.path.expanduser(data.get('donor_dir', '').strip())
        if not filepath or not os.path.isdir(donor_dir):
            return self._send_json({'error': 'Folder not found'}, 404)
        found, checked = [], 0
        deadline = time.time() + 60
        for dirpath, dirnames, filenames in os.walk(donor_dir):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith('.') and d != '__MACOSX']
            for name in sorted(filenames):
                fpath = os.path.join(dirpath, name)
                if (os.path.splitext(name)[1].lower() not in VIDEO_EXTS
                        or fpath == filepath):
                    continue
                checked += 1
                try:
                    status, _, details = check_video(fpath)
                except Exception:
                    continue
                if _usable_donor(status, details):
                    found.append(fpath)
            if checked >= 2000 or time.time() > deadline:
                break
        fdir = os.path.dirname(filepath)
        _ensure_donor_pool(fdir)
        pool = donor_pool.setdefault(fdir, [])
        added = [f for f in found if f not in pool]
        pool[:0] = added
        self._send_json({'ok': True, 'added': len(added),
                         'found': len(found), 'checked': checked})

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

        if not filepath or not os.path.exists(filepath):
            return self._send_json({'error': 'File not found'}, 404)
        output_dir, error = _check_output_dir(data.get('output_dir', ''))
        if error:
            return self._send_json({'error': error}, 400)

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
        output_dir, error = _check_output_dir(data.get('output_dir', ''))
        if error:
            return self._send_json({'ok': False, 'error': error,
                                    'error_kind': 'output_dir'}, 400)

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
                'output_dir': output_dir,
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

    def _handle_recover_cancel(self):
        # Only jobs still waiting can be cancelled; the worker skips them
        job_id = str(self._read_json_body().get('job_id', ''))
        with _recover_lock:
            job = _recover_jobs.get(job_id)
            if not job or job['status'] != 'queued':
                return self._send_json(
                    {'ok': False, 'error': 'Only queued jobs can be cancelled'}, 409)
            del _recover_jobs[job_id]
        self._send_json({'ok': True, 'filepath': job['filepath']})

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
