"""H.264/HEVC stream handling: bit reading, NAL scanning, parameter sets, and the Annex-B writer."""

import mmap
import os
import re
import struct
import subprocess
import tempfile

from .decode import _decode_stats
from .mp4 import parse_boxes
from .tools import _ffmpeg_path, has_ffmpeg


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


def _h264_slice_frame_num(nal, sps_by_id, pps_by_id):
    """(frame_num, max_frame_num) from an H.264 slice header, or None when the
    parameter sets are unknown or the slice is a field."""
    d = _rbsp(nal, 32)
    bit = 8
    _, bit = _read_ue(d, bit)                  # first_mb_in_slice
    _, bit = _read_ue(d, bit)                  # slice_type
    pps_id, bit = _read_ue(d, bit)
    sps = sps_by_id.get(pps_by_id.get(pps_id))
    if sps is None:
        return None
    if sps['separate_colour_plane']:
        _, bit = _read_bits(d, bit, 2)
    frame_num, bit = _read_bits(d, bit, sps['log2_max_frame_num'])
    if frame_num is None:
        return None
    if not sps['frame_mbs_only']:
        field_pic, _ = _read_bits(d, bit, 1)
        if field_pic is None or field_pic:
            return None
    return frame_num, 1 << sps['log2_max_frame_num']


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
        # length < 8 MB (top byte 0) followed by a NAL header of a valid type.
        # The pattern is a lookahead so matches can overlap: a false match
        # in audio data that starts a byte or two before a real frame would
        # otherwise hide that frame, and losing a reference frame breaks
        # every frame up to the next keyframe.
        candidate_re = None
        if nal_length_size == 4:
            if codec == 'hevc':
                first = bytes(sorted({t << 1 for t in max_sizes}))
                candidate_re = re.compile(
                    rb'(?=\x00[\x00-\x7f][\s\S]{2}[' + re.escape(first) + rb'][\x01-\x07])')
            else:
                first = bytes(sorted({(ref << 5) | t
                                      for ref in range(4) for t in max_sizes}))
                candidate_re = re.compile(
                    rb'(?=\x00[\x00-\x7f][\s\S]{2}[' + re.escape(first) + rb'])')

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

        idr_types = (19, 20) if codec == 'hevc' else (5,)

        def next_keyframe(start):
            # Last resort when no chain lies ahead: in files with audio after
            # every frame, chains only occur where a frame happens to carry
            # an SEI or parameter sets, often just at the very start. If that
            # stretch is lost (zeroed header, foreign data in a carved file),
            # anchor on a keyframe slice instead: the first slice of an IDR
            # picture with a clean payload, followed by three more frames that
            # fit the same stream. The decode check still judges the result.
            if candidate_re is None:
                return None
            for match in candidate_re.finditer(m, start, data_end):
                cand = match.start()
                nal_len = nal_length(cand)
                if nal_len is None:
                    continue
                hdr_pos = cand + nal_length_size
                nal_type = ((m[hdr_pos] >> 1) & 0x3F if codec == 'hevc'
                            else m[hdr_pos] & 0x1F)
                if nal_type not in idr_types or not clean_payload(cand, nal_len):
                    continue
                head = m[hdr_pos:hdr_pos + 16]
                if codec == 'hevc':
                    first = head[2] & 0x80
                    pps_id, _ = _read_ue(head, 18)
                else:
                    first_mb, bit = _read_ue(head, 8)
                    _, bit = _read_ue(head, bit)
                    pps_id, _ = _read_ue(head, bit)
                    first = first_mb == 0
                if not first or pps_id is None:
                    continue
                known = set(profile['pps_ids'])
                if known and pps_id not in known:
                    continue
                profile['pps_ids'].add(pps_id)
                if next_lone(cand + nal_length_size + nal_len, hops=2) is not None:
                    return cand
                profile['pps_ids'] = known
            return None

        nal_entries = []
        pos = next_chain(data_start)
        trusted = True                 # pos came from a full chain
        if pos is None:
            pos, trusted = next_keyframe(data_start), False
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
                    resume = next_chain(pos + 1)
                    if resume is None:
                        pos, trusted = next_keyframe(pos + 1), False
                    else:
                        pos, trusted = resume, True
        return nal_entries
    finally:
        m.close()


def _find_param_nal(block, start, end, nal_type, codec):
    """First length-prefixed parameter-set NAL of nal_type in block[start:end].

    Returns (nal_bytes, end_offset) or None.
    """
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
    H.264 frames whose frame_num shows that reference frames are missing
    (a stretch lost to damage, which can be shorter than an audio chunk, so
    the distance between NALs can't tell) are skipped up to the next IDR.
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
        # Parameter sets by id, for reading frame_num and picture order from
        # slice headers
        sps_by_id, pps_by_id = {}, {}
        if codec != 'hevc':
            for sps in sps_list:
                info = _parse_h264_sps_order(sps)
                if info:
                    sps_by_id[info['sps_id']] = info
            for pps in pps_list:
                ids = _parse_h264_pps_ids(pps)
                if ids:
                    pps_by_id[ids[0]] = ids[1]
        # frame_num continuity: each picture's frame_num is one more than the
        # last reference picture's (IDR: 0). Checked only if the stream's
        # first pictures follow that rule, in case a camera's headers read
        # differently than expected.
        def picture_frame_num(data):
            if codec == 'hevc' or not (len(data) > 1 and data[1] & 0x80):
                return None
            return _h264_slice_frame_num(data, sps_by_id, pps_by_id)

        check_frame_num = False
        picture_fn = None      # frame_num of the picture being written
        if sps_by_id:
            prev_ref, follows, total = None, 0, 0
            for nal_offset, nal_len in nal_entries[first_idr:]:
                src.seek(nal_offset)
                data = src.read(min(nal_len, 32))
                if not data or _get_nal_type(data[0]) not in slice_types | idr_types:
                    continue
                fn = picture_frame_num(data)
                if fn is None:
                    continue
                if _get_nal_type(data[0]) in idr_types:
                    prev_ref = fn[0]
                    continue
                if prev_ref is not None:
                    total += 1
                    follows += fn[0] == (prev_ref + 1) % fn[1]
                if data[0] & 0x60:
                    prev_ref = fn[0]
                if total >= 200:
                    break
            check_frame_num = total >= 10 and follows >= total * 0.95
        prev_ref = None

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

            if check_frame_num and first_in_pic:
                fn = picture_frame_num(data)
                picture_fn = fn[0] if fn else None
                if fn is not None:
                    if nal_type in idr_types:
                        prev_ref = fn[0]
                    elif not need_keyframe:
                        if prev_ref is not None and fn[0] != (prev_ref + 1) % fn[1]:
                            need_keyframe = True   # references were lost
                        elif data[0] & 0x60:
                            prev_ref = fn[0]
            elif check_frame_num and not first_in_pic:
                # Further slices of a picture share its frame_num; one that
                # doesn't (or a few bytes too short to hold a slice header)
                # is other data that happened to look like a slice
                fn = _h264_slice_frame_num(data, sps_by_id, pps_by_id)
                if nal_len < 4 or (fn is not None and picture_fn is not None
                                   and fn[0] != picture_fn):
                    continue

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

    No VUI: it carries no decoding information. max_num_ref_frames is
    p['num_ref_frames'] when inference found it, otherwise as large as level
    5.1 allows for the frame size: a larger sliding window than the
    encoder's usually decodes the same, but not for streams that also drop
    references explicitly (x264's B-pyramid), where kept references then
    outlive frame_num wrapping.
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
    w.ue(p.get('num_ref_frames') or max(1, min(16, 184320 // (mbw * mbh))))
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
