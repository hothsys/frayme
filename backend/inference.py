"""Parameter inference: rebuild a moov-less H.264 stream's SPS/PPS by trial decoding."""

import collections
import os
import re
import statistics
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from .config import _COMMON_FRAME_SIZES
from .decode import _decode_stats
from .h264 import _build_h264_pps, _build_h264_sps, _rbsp, _read_bits, _read_se, _read_ue, _write_annex_b_stream
from .tools import _ffmpeg_path, has_ffmpeg


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
        if problems:
            # The default reference count (the most the level allows) breaks
            # streams that drop references explicitly. The smallest count
            # that decodes cleanly is the encoder's; fewer drop references
            # it still uses.
            counts = [dict(group_best[1], num_ref_frames=n) for n in range(1, 16)]
            results = _trial_map(
                lambda c: _trial_decode(filepath, test_nals, c), counts,
                stop=lambda r: r[0] == 0)
            tried = [((r[0], -r[1]), i) for i, r in enumerate(results) if r]
            if tried:
                best = min(tried)
                if best[0] < group_best[0]:
                    group_best = (best[0], counts[best[1]])
                    problems, frames = best[0][0], -best[0][1]
        # Damage in the data shows up here too (a damaged first keyframe
        # breaks every frame up to the next one, whatever the settings); the
        # full recovery is checked strictly afterwards, so only reject
        # clearly wrong settings, which fail on nearly every frame
        if frames and problems <= frames * 0.5 + 2:
            p = group_best[1]
            return [], [_build_h264_sps(p)], [_build_h264_pps(p)]
    return None
