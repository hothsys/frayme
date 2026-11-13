"""Lower-level checks of pieces every recovery relies on."""

import os
import subprocess
import unittest

from tests.fixtures import (CLIPS, FFMPEG, FrayTestCase, drop_moov, needs_ffmpeg,
                            needs_x264, top_boxes)
from backend.decode import _check_decodes_cleanly, _decode_stats
from backend.h264 import _scan_mdat_for_nals


def video_packets(path):
    """[(offset, size)] of a healthy file's video samples, from ffprobe."""
    import json
    from tests.fixtures import FFPROBE
    r = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'packet=pos,size', '-of', 'json', path],
                       capture_output=True, text=True)
    return [(int(p['pos']), int(p['size'])) for p in json.loads(r.stdout)['packets']]


@needs_ffmpeg
@needs_x264
class DecodeCheck(FrayTestCase):

    def test_counts_decoder_errors(self):
        # ffmpeg prefixes decoder messages ("[h264 @ 0x...] [error] ..."); a
        # check that only matched "[error]" at the line start missed them
        src = CLIPS.get('base')
        stream = os.path.join(self.work, 'damaged.h264')
        subprocess.run([FFMPEG, '-v', 'error', '-i', src, '-c:v', 'copy', '-an',
                        '-bsf:v', 'h264_mp4toannexb', stream], check=True)
        with open(stream, 'r+b') as f:
            data = bytearray(f.read())
            for at in range(len(data) // 3, len(data) // 3 + 4000, 97):
                data[at] ^= 0x5A
            f.seek(0)
            f.write(data)
        problems, frames = _decode_stats(['-f', 'h264', '-i', stream], 60)
        self.assertGreater(problems, 0)
        self.assertGreater(frames, 0)

    def test_clean_file_passes(self):
        ok, msg = _check_decodes_cleanly(CLIPS.get('base'))
        self.assertTrue(ok, msg)

    def test_blank_output_fails(self):
        # Zero decoded frames means zero errors; it must still fail
        path = os.path.join(self.work, 'audio_only.mp4')
        subprocess.run([FFMPEG, '-v', 'error', '-i', CLIPS.get('base'), '-vn',
                        '-c', 'copy', path], check=True)
        ok, _ = _check_decodes_cleanly(path)
        self.assertFalse(ok)


@needs_ffmpeg
@needs_x264
class NALScanner(FrayTestCase):

    def scanned_frames(self, clip):
        path = drop_moov(clip, self.damaged('clip.mp4'))
        mdat = next(b for b in top_boxes(path) if b[0] == 'mdat')
        starts = [o - 4 for o, _ in _scan_mdat_for_nals(path, mdat[1], mdat[2], 4)]
        # The moov was at the end, so offsets match the original file
        packets = video_packets(clip)
        return [any(pos <= s < pos + size for s in starts) for pos, size in packets]

    def test_finds_every_frame_between_audio_chunks(self):
        # Audio after every frame, as ffmpeg and some cameras write. A false
        # match starting inside audio a byte or two before a frame used to
        # hide that frame; a lost reference frame breaks the rest of its GOP.
        for name, kwargs in (('base', {}), ('bf', {'bframes': 2})):
            found = self.scanned_frames(CLIPS.get(name, **kwargs))
            missing = [i for i, ok in enumerate(found) if not ok]
            # The last frames can't be confirmed by the frames after them
            self.assertTrue(all(i >= len(found) - 2 for i in missing),
                            f'{name}: frames {missing} not found')

    def test_video_only_recording(self):
        found = self.scanned_frames(CLIPS.get('noaudio', audio=False))
        self.assertGreaterEqual(sum(found), len(found) - 1)


if __name__ == '__main__':
    unittest.main()
