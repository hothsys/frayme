"""Recovery engines and the single-file recovery entry point."""

import json
import os
import struct
import subprocess
from pathlib import Path

from .config import DEFAULT_OUTPUT_DIR
from .decode import _check_decodes_cleanly, _decode_stats
from .detect import _walk_avi
from .findings import describe_recovery
from .donors import _collect_sps_candidates, _donor_files, _ensure_donor_pool, _find_matching_donor, _get_donor_rotation, _most_common_rate_donor
from .h264 import _apply_presentation_order, _decoding_signature, _find_inband_params, _parse_h264_pps, _parse_h264_sps_order, _probe_sps_candidates, _scan_mdat_for_nals, _write_annex_b_stream
from .inference import _infer_h264_params, _trial_map
from .mp4 import _adjust_stco, _extract_avcc_params, _extract_ftyp, _extract_hvcc_params, _extract_moov, _scan_for_moov, _truncate_stsz, parse_boxes
from .state import donor_map, scan_state
from .tools import _ffmpeg_path, _ffprobe_path, has_ffmpeg, has_ffprobe


def _read_rotation(path):
    """Clockwise display rotation of a video file (0 when it has none)."""
    rotation = _get_donor_rotation(path)
    return (-rotation) % 360 if rotation else 0


def _apply_rotation(path, clockwise):
    """Set a video file's display rotation in place, without re-encoding.

    Rotation lives in the container (the track's display matrix), so this is
    a quick remux. Damaged files lose it along with the moov, and a donor's
    is only a guess, so the user can correct it.
    """
    stem, ext = os.path.splitext(path)
    tmp_path = stem + '.rotating' + ext
    try:
        r = subprocess.run(
            [_ffmpeg_path, '-y', '-v', 'error',
             '-display_rotation:v:0', str((-clockwise) % 360),
             '-i', path, '-map', '0', '-c', 'copy', '-movflags', '+faststart',
             tmp_path], capture_output=True, timeout=600)
        if r.returncode != 0:
            raise ValueError('Could not set rotation: '
                             + r.stderr.decode(errors='replace')[-200:])
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _clean_gops(filepath, nal_entries, sps_list, pps_list, vps_list, codec):
    """The NAL entries of the groups of pictures that decode cleanly.

    For recoveries that fail the decode check as a whole. Carved files can
    hold stretches of other recordings (another camera mode decodes as
    garbage under these parameters), and a damaged keyframe breaks every
    frame up to the next one. Each group from one IDR to the next is decoded
    on its own, and only the clean ones are kept; every kept group starts
    with a keyframe, so nothing references a dropped frame. Returns None if
    no group is clean.
    """
    import tempfile
    idr_types = (19, 20) if codec == 'hevc' else (5,)
    groups, current = [], []
    with open(filepath, 'rb') as f:
        for offset, length in nal_entries:
            f.seek(offset)
            head = f.read(3)
            if len(head) < 3:
                continue
            nal_type = (head[0] >> 1) & 0x3F if codec == 'hevc' else head[0] & 0x1F
            first = head[2] & 0x80 if codec == 'hevc' else head[1] & 0x80
            if nal_type in idr_types and first and current:
                groups.append(current)
                current = []
            current.append((offset, length))
    if current:
        groups.append(current)
    if len(groups) < 2:
        return None

    def decode_group(group):
        with tempfile.NamedTemporaryFile(suffix='.' + codec, delete=False) as tmp:
            tmp_path = tmp.name
        try:
            pictures = []
            _write_annex_b_stream(filepath, group, sps_list, pps_list, tmp_path,
                                  vps_list=vps_list, codec=codec, pictures=pictures)
            problems, frames = _decode_stats(
                ['-threads', '1', '-f', codec, '-i', tmp_path], 120)
            return problems, frames, len(pictures)
        finally:
            os.unlink(tmp_path)

    kept = []
    for group, (problems, frames, pictures) in zip(groups, _trial_map(decode_group, groups)):
        # Same bar as _check_decodes_cleanly. H.264 pictures are counted by
        # the writer, so a group whose frames mostly fail to decode is out
        if frames and problems <= frames * 0.1 and frames >= pictures * 0.8:
            kept.extend(group)
    return kept or None


def _count_pictures(filepath, nal_entries, codec):
    """Pictures among the scanned NALs (slices that start a picture)."""
    count = 0
    with open(filepath, 'rb') as f:
        for offset, _ in nal_entries:
            f.seek(offset)
            head = f.read(3)
            if len(head) < 3:
                continue
            if codec == 'hevc':
                count += ((head[0] >> 1) & 0x3F) < 32 and bool(head[2] & 0x80)
            else:
                count += 1 <= (head[0] & 0x1F) <= 5 and bool(head[1] & 0x80)
    return count


def recover_no_moov(filepath, donor_path, output_dir=None, data_range=None, info=None):
    """Recover a video file missing its moov atom.

    Strategy 1: Raw NAL extraction — scan mdat for H.264 or HEVC NALs,
    get parameter sets from the file itself or the donor's avcC/hvcC, write
    Annex-B stream, wrap with ffmpeg.
    Strategy 2 (fallback): Moov transplant from donor.
    donor_path may be empty when the file carries its own SPS/PPS.
    data_range: (offset, size) of media data with no mdat box header, for
    files whose header was zeroed; by default the file's mdat box is used.
    info: optional dict, filled with where rotation and frame rate came from,
    frames found, and the decode check's counts (see findings.describe_recovery).
    Returns (output_path, method_used) on success, raises ValueError on failure.
    """
    if info is None:
        info = {}
    donor_moov = _extract_moov(donor_path) if donor_path else None
    if donor_path and not donor_moov:
        raise ValueError('Donor file has no moov atom')

    # Get damaged file's mdat info
    raw_data = data_range is not None
    damaged_mdat_offset = 0
    damaged_mdat_size = 0
    if raw_data:
        damaged_mdat_offset, damaged_mdat_size = data_range
    else:
        for bt, off, sz in parse_boxes(filepath):
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
                params['nal_length_size'], raw_data=raw_data, codec=codec)

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
                # The file's own parameter sets are exact; errors under them
                # come from damaged data, which inference can't fix
                own_params = bool(inband) and best_score is not None and best_sps == inband[1]
                if codec == 'h264' and not own_params and (best_score is None or best_score > 0):
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

                def build(entries):
                    """Write entries as out_path; True if the remux worked."""
                    pictures = []
                    _write_annex_b_stream(
                        filepath, entries, best_sps, best_pps,
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
                        info['fps_source'] = info['rotation_source'] = (
                            'donor:' + os.path.basename(fps_donor))
                    elif used_inferred:
                        # Inferred SPS carry no timing, and no donor shares
                        # the camera's settings: 30 fps is the likeliest
                        fps = '30'
                        info['fps_source'], info['rotation_source'] = 'assumed', 'unknown'
                    else:
                        info['fps_source'], info['rotation_source'] = 'stream', 'unknown'

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
                    if result.returncode != 0 or not os.path.exists(out_path):
                        return False
                    # B-frames need their display order restored
                    _apply_presentation_order(out_path, pictures)
                    return True

                try:
                    if build(nal_entries):
                        out_size = os.path.getsize(out_path)
                        if out_size > damaged_mdat_size * 0.01:
                            # Decode-test: reject unless the frames decode.
                            # If the whole fails, keep the groups of pictures
                            # that decode cleanly on their own.
                            try:
                                stats = {}
                                info['salvaged'] = False
                                ok, msg = _check_decodes_cleanly(out_path, stats=stats)
                                if not ok:
                                    kept = _clean_gops(filepath, nal_entries, best_sps,
                                                       best_pps, best_vps, codec)
                                    if kept and build(kept):
                                        stats = {}
                                        ok, _ = _check_decodes_cleanly(out_path, stats=stats)
                                        info['salvaged'] = ok
                                if not ok:
                                    raw_failure = msg
                                    if os.path.exists(out_path):
                                        os.unlink(out_path)
                                    continue  # try next codec
                            except subprocess.TimeoutExpired:
                                pass
                            info['decode'] = stats
                            info['inferred'] = used_inferred
                            info['frames_found'] = _count_pictures(
                                filepath, nal_entries, codec)
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
    # The donor's whole index is used: its rotation and timing too
    info.update(fps_source='donor:' + os.path.basename(donor_path),
                rotation_source='donor:' + os.path.basename(donor_path),
                inferred=False, salvaged=False)

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
    if raw_data:
        mdat_data = struct.pack('>I', len(mdat_data) + 8) + b'mdat' + mdat_data

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


def _keep_if_decodes(path, info=None):
    """(ok, message) from the decode check; deletes path if it fails.

    Without ffmpeg nothing can be checked, so the file is kept. info, if
    given, gets the check's counts as info['decode'].
    """
    if not has_ffmpeg():
        return True, 'not checked (no ffmpeg)'
    stats = {}
    try:
        ok, msg = _check_decodes_cleanly(path, stats=stats)
        if info is not None:
            info['decode'] = stats
    except subprocess.TimeoutExpired:
        return True, 'decode check timed out'
    if not ok and os.path.exists(path):
        os.unlink(path)
    return ok, msg


def recover_leading_zeros(filepath, donor_path=None, output_dir=None, info=None):
    """Recover a video file with leading zeros.

    Skips zero region, looks for surviving MP4 boxes or raw NAL stream.
    info: optional dict, as for recover_no_moov.
    Returns (output_path, method_used), raises ValueError on failure.
    """
    if info is None:
        info = {}
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

    failure = None   # why the last strategy's output was rejected

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

            ok, failure = _keep_if_decodes(out_path, info)
            if ok:
                info.update(fps_source='original', rotation_source='original')
                return out_path, method
            # The moov is there but its frames don't decode (e.g. it belongs
            # to another recording); the strategies below may still work

    # Read surviving data. A box size field starts with zero bytes, so when
    # only whole boxes were zeroed the zeros run a byte or more into the next
    # box's header; back up to it.
    box_start = zero_end
    with open(filepath, 'rb') as f:
        for back in range(4):
            if zero_end - back < 0:
                break
            f.seek(zero_end - back)
            hdr = f.read(8)
            if (len(hdr) == 8 and hdr[4:8] in (b'ftyp', b'moov', b'mdat', b'free', b'wide', b'skip')
                    and struct.unpack('>I', hdr[:4])[0] >= 8):
                box_start = zero_end - back
                break
        f.seek(box_start)
        surviving_data = f.read(min(file_size - box_start, 32))

    # Check if surviving data starts with valid MP4 boxes
    if len(surviving_data) >= 8:
        box_type = surviving_data[4:8]
        try:
            bt = box_type.decode('ascii')
            if bt in ('ftyp', 'moov', 'mdat', 'free', 'wide', 'skip') and box_start >= 8:
                # Only whole boxes were zeroed (typically the ftyp). Chunk
                # offsets in the moov count from the start of the file, so
                # the zeroed bytes are replaced in place (an ftyp plus a
                # free box, or just a free box) rather than cut out.
                if box_start >= 32:
                    filler = (struct.pack('>I', 24) + b'ftypisom' + struct.pack('>I', 0x200)
                              + b'isomiso2' + struct.pack('>I', box_start - 24) + b'free')
                else:
                    filler = struct.pack('>I', box_start) + b'free'
                with open(filepath, 'rb') as f, open(out_path, 'wb') as out:
                    out.write(filler.ljust(box_start, b'\x00'))
                    f.seek(box_start)
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                boxes = [b for b, _, _ in parse_boxes(out_path)]
                if 'moov' in boxes and 'mdat' in boxes:
                    ok, failure = _keep_if_decodes(out_path, info)
                    if ok:
                        info.update(fps_source='original', rotation_source='original')
                        return out_path, 'zero_skip'
                elif os.path.exists(out_path):
                    # No moov survived: the raw extraction below handles it
                    os.unlink(out_path)
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
                ok, failure = _keep_if_decodes(out_path, info)
                if ok:
                    info.update(fps_source='stream', rotation_source='unknown')
                    return out_path, 'raw_' + ffmpeg_fmt + '_wrap'
        except Exception:
            pass
        finally:
            if os.path.exists(raw_path):
                try:
                    os.unlink(raw_path)
                except OSError:
                    pass

    # Length-prefixed NALs (MP4-style), the usual case: the same pipeline as
    # a file missing its moov (the file's own parameter sets, donors, then
    # inference), over the data after the zeros
    try:
        return recover_no_moov(filepath, donor_path or '', output_dir,
                               data_range=(zero_end, surviving), info=info)
    except ValueError as e:
        failure = str(e)

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
    if stripped_boxes and any(bt == 'moov' for bt, _, _ in stripped_boxes):
        ok, msg = _keep_if_decodes(out_path, info)
        if ok:
            info.update(fps_source='original', rotation_source='original')
            return out_path, 'zero_strip'
        failure = failure or msg

    # Not a valid container: clean up and fail
    if os.path.exists(out_path):
        os.unlink(out_path)
    raise ValueError(failure or 'No recoverable video data found; the file is too damaged')


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


def _execute_recovery(filepath, donor_override='', output_dir='', rotation=None):
    """Run recovery on a single file. Returns result dict on success.

    rotation: degrees clockwise to set on the output, or None to keep what
    the recovery chose (a matching donor's rotation, if any).
    """
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

    # A donor is optional: a zeroed file may keep its moov at the end, carry
    # its own SPS/PPS, or have them inferred. The engines explain failures.
    info = {}
    if is_avi:
        out_path, method = recover_avi(filepath, output_dir)
        donor_auto = ''
        info.update(fps_source='original', rotation_source='original')
    elif all_zero:
        out_path, method = recover_leading_zeros(
            filepath, donor_path, output_dir, info=info)
    else:
        out_path, method = recover_no_moov(
            filepath, donor_path, output_dir, info=info)

    if rotation is not None:
        _apply_rotation(out_path, rotation)

    out_size = os.path.getsize(out_path)

    probed = _probe_output(out_path)
    duration = probed.get('duration')

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

    resp = {
        'ok': True,
        'output': out_path,
        'filename': os.path.basename(out_path),
        'size': out_size,
        'method': method,
        'duration': duration,
        'recovery_pct': recovery_pct,
        'original_size': original_size,
        'video_only': not probed.get('has_audio'),
        'rotation': _read_rotation(out_path),
        'codec': probed.get('codec'),
        'resolution': probed.get('resolution'),
        'fps': probed.get('fps'),
        'frames': probed.get('frames'),
    }
    # A file decoded with its own or inferred SPS/PPS didn't use the donor
    if donor_auto and 'embedded' not in method and 'inferred' not in method:
        resp['donor_used'] = donor_auto
    try:
        resp.update(describe_recovery(info, resp, rotation))
    except Exception:
        pass    # the row just shows less
    return resp


def _probe_output(path):
    """Duration, sound, codec, size, frame rate and frame count of a
    recovered file, from one ffprobe call (container fields, no decoding)."""
    out = {}
    if not has_ffprobe():
        return out
    try:
        r = subprocess.run(
            [_ffprobe_path, '-v', 'error', '-show_entries',
             'format=duration:stream=codec_type,codec_name,width,height,'
             'avg_frame_rate,nb_frames', '-of', 'json', path],
            capture_output=True, timeout=30)
        data = json.loads(r.stdout or b'{}')
    except Exception:
        return out
    try:
        out['duration'] = float(data.get('format', {}).get('duration'))
    except (TypeError, ValueError):
        pass
    streams = data.get('streams', [])
    out['has_audio'] = any(st.get('codec_type') == 'audio' for st in streams)
    video = next((st for st in streams if st.get('codec_type') == 'video'), None)
    if video:
        out['codec'] = {'h264': 'H.264', 'hevc': 'HEVC', 'mjpeg': 'Motion JPEG'}.get(
            video.get('codec_name'), (video.get('codec_name') or '').upper() or None)
        if video.get('width') and video.get('height'):
            out['resolution'] = f"{video['width']}x{video['height']}"
        try:
            num, den = (int(x) for x in video.get('avg_frame_rate', '0/0').split('/'))
            if num and den:
                out['fps'] = round(num / den, 3)
        except ValueError:
            pass
        if str(video.get('nb_frames', '')).isdigit():
            out['frames'] = int(video['nb_frames'])
    return out


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
