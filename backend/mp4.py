"""MP4/MOV box parsing and the moov atom helpers."""

import os
import struct


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
