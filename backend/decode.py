"""Decode checks: run ffmpeg on a stream and count errors and frames."""

import re
import subprocess

from .tools import _ffmpeg_path, _ffprobe_path


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
