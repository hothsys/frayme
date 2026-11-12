"""Clues to a damaged file's source camera, shown to help choose a donor."""

import collections
import re

from .config import _COMMON_FRAME_SIZES
from .h264 import _find_inband_params, _parse_h264_sps_order, _rbsp, _read_ue, _scan_mdat_for_nals
from .mp4 import parse_boxes


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
