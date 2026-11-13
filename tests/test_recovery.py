"""Recovery: damage healthy clips the ways real files get damaged, recover
them, and require outputs that actually decode, not just plausible metadata.

The clips are 4 seconds at 30 fps (120 frames) with a keyframe every second.
Frame minimums allow for what each kind of damage destroys: a recording
whose first keyframe is lost restarts at the next one, 30 frames later.
"""

import os
import unittest

from tests.fixtures import (CLIPS, HAS_X265, FrayTestCase, cut_tail, decode_errors,
                            drop_moov, make_avi, needs_ffmpeg, needs_x264, needs_x265,
                            overwrite_junk, packet_times, probe, sha256, zero_head)
from backend.recovery import _check_output_dir, _execute_recovery


class RecoveryTestCase(FrayTestCase):

    def recover(self, path, **kwargs):
        """Recover into this test's output folder; the source must not change
        and nothing may be written next to it."""
        before = sha256(path)
        listing = sorted(os.listdir(self.src_dir))
        result = _execute_recovery(path, output_dir=self.out_dir, **kwargs)
        self.assertEqual(sha256(path), before, 'the damaged original was modified')
        self.assertEqual(sorted(os.listdir(self.src_dir)), listing,
                         'recovery wrote into the source folder')
        self.assertEqual(os.path.dirname(result['output']), self.out_dir)
        self.assertEqual(sorted(os.listdir(self.out_dir)), [result['filename']],
                         'recovery left temporary files behind')
        return result

    def add_donor(self, name='donor', **kwargs):
        """A healthy clip with the same encoder settings but other content."""
        kwargs.setdefault('pattern', 'b')
        return self.place(CLIPS.get(name, **kwargs), name + '.mp4')


@needs_ffmpeg
@needs_x264
class MissingMoov(RecoveryTestCase):
    """Recordings interrupted before the camera wrote its index."""

    def test_with_donor(self):
        self.add_donor()
        path = drop_moov(CLIPS.get('base'), self.damaged('clip.mp4'))
        result = self.recover(path)
        self.assertEqual(result['method'], 'raw_nal_extraction')
        self.assertEqual(result.get('donor_used'), 'donor.mp4')
        self.assertPlaysCleanly(result['output'], min_frames=115)

    def test_without_donor_infers_parameters(self):
        path = drop_moov(CLIPS.get('base'), self.damaged('clip.mp4'))
        result = self.recover(path)
        self.assertEqual(result['method'], 'raw_nal_extraction (inferred SPS/PPS)')
        self.assertPlaysCleanly(result['output'], min_frames=115)

    def test_cut_off_recording(self):
        path = cut_tail(CLIPS.get('base'), self.damaged('clip.mp4'), 0.6)
        result = self.recover(path)
        self.assertPlaysCleanly(result['output'], min_frames=60)

    def test_b_frames_with_donor_keep_display_order(self):
        self.add_donor('donor_bf', bframes=2)
        path = drop_moov(CLIPS.get('bf', bframes=2), self.damaged('clip.mp4'))
        result = self.recover(path)
        self.assertPlaysCleanly(result['output'], min_frames=115)
        # A raw stream has no presentation times; without the ctts box the
        # frames would play in decode order and jitter. Decoding can't show
        # this (ffmpeg reorders by itself), so check the timestamps.
        times = packet_times(result['output'])
        self.assertTrue(any(pts != dts for pts, dts in times), 'no reordering')
        shown = sorted(pts for pts, _ in times)
        steps = {b - a for a, b in zip(shown, shown[1:])}
        self.assertEqual(len(steps), 1, f'uneven display times: {sorted(steps)[:5]}')

    def test_b_frames_without_donor(self):
        # Needs the right reference count: x264's B-pyramid drops references
        # explicitly, and a too-large count makes kept frames collide
        path = drop_moov(CLIPS.get('bf', bframes=2), self.damaged('clip.mp4'))
        result = self.recover(path)
        self.assertPlaysCleanly(result['output'], min_frames=115)

    def test_unknown_camera_without_donor_explains(self):
        # HEVC parameters can't be inferred; the message must point at donors
        if not HAS_X265:
            self.skipTest('ffmpeg built without libx265')
        path = drop_moov(CLIPS.get('hevc', vcodec='libx265'), self.damaged('clip.mp4'))
        with self.assertRaisesRegex(ValueError, 'donor'):
            self.recover(path)


@needs_ffmpeg
@needs_x265
class HEVC(RecoveryTestCase):

    def test_missing_moov_with_donor(self):
        self.add_donor('donor_hevc', vcodec='libx265')
        path = drop_moov(CLIPS.get('hevc', vcodec='libx265'), self.damaged('clip.mp4'))
        result = self.recover(path)
        self.assertEqual(probe(result['output'], 'codec_name')['codec_name'], 'hevc')
        self.assertPlaysCleanly(result['output'], min_frames=115)


@needs_ffmpeg
@needs_x264
class ZeroedHeader(RecoveryTestCase):
    """Card or filesystem damage that wipes the start of a file."""

    def test_moov_at_end_survives_without_donor(self):
        path = zero_head(CLIPS.get('base'), self.damaged('clip.mp4'), 4096)
        result = self.recover(path)
        self.assertTrue(result['method'].startswith('moov_recovery'), result['method'])
        self.assertPlaysCleanly(result['output'], min_frames=85)
        self.assertFalse(result['video_only'], 'audio should survive with the moov')

    def test_only_ftyp_zeroed_keeps_everything(self):
        path = zero_head(CLIPS.get('fast', faststart=True), self.damaged('clip.mp4'), 32)
        result = self.recover(path)
        self.assertEqual(result['method'], 'zero_skip')
        self.assertPlaysCleanly(result['output'], min_frames=120)
        self.assertFalse(result['video_only'])

    def test_moov_and_first_keyframe_lost(self):
        # Zeros run past the moov into the video: recovery restarts at the
        # next keyframe, with no donor needed
        path = zero_head(CLIPS.get('fast', faststart=True), self.damaged('clip.mp4'), 20000)
        result = self.recover(path)
        self.assertPlaysCleanly(result['output'], min_frames=85)

    def test_moov_lost_with_donor(self):
        self.add_donor()
        path = zero_head(CLIPS.get('fast', faststart=True), self.damaged('clip.mp4'), 60000)
        result = self.recover(path)
        self.assertEqual(result['method'], 'raw_nal_extraction')
        self.assertPlaysCleanly(result['output'], min_frames=85)


@needs_ffmpeg
@needs_x264
class CarvedFiles(RecoveryTestCase):
    """Files from recovery tools like PhotoRec: stretches of the recording
    with other files' data where fragments of it should be."""

    def foreign_fragment(self):
        """A moov-less clip with a stretch of another recording (720p,
        other content) where part of its own data should be."""
        own = drop_moov(CLIPS.get('base'), self.damaged('own.tmp'))
        other = drop_moov(CLIPS.get('big', size='1280x720', pattern='c'),
                          self.damaged('other.tmp'))
        with open(own, 'rb') as f:
            data = bytearray(f.read())
        with open(other, 'rb') as f:
            foreign = f.read()[60000:180000]
        start = len(data) // 3
        data[start:start + len(foreign)] = foreign
        os.unlink(own)
        os.unlink(other)
        path = self.damaged('clip.mp4')
        with open(path, 'wb') as f:
            f.write(bytes(data))
        return path

    def test_unrelated_data_mid_file(self):
        # The frame running into the junk may still be damaged, but
        # everything after the junk resumes at the next keyframe
        tmp = drop_moov(CLIPS.get('base'), self.damaged('tmp.mp4'))
        path = overwrite_junk(tmp, self.damaged('clip.mp4'), 0.4, 30000)
        os.unlink(tmp)
        result = self.recover(path)
        self.assertGreaterEqual(int(probe(result['output'], 'nb_frames').get('nb_frames', 0)), 100)
        from backend.decode import _check_decodes_cleanly
        ok, msg = _check_decodes_cleanly(result['output'])
        self.assertTrue(ok, msg)
        self.assertLessEqual(len(decode_errors(result['output'])), 2)

    def test_other_recording_mid_file_with_donor(self):
        # Frames from another recording decode as garbage under this one's
        # parameters; whole groups of pictures that don't decode are dropped
        self.add_donor()
        result = self.recover(self.foreign_fragment())
        self.assertPlaysCleanly(result['output'], min_frames=55)

    @unittest.expectedFailure
    def test_other_recording_mid_file_without_donor(self):
        # Known limitation: inference assumes one recording per file, and
        # the foreign frames confuse it
        result = self.recover(self.foreign_fragment())
        self.assertPlaysCleanly(result['output'], min_frames=55)


@needs_ffmpeg
class AVI(RecoveryTestCase):

    def test_truncated_avi_keeps_audio(self):
        path = cut_tail(CLIPS.get('avi', ext='.avi', maker=make_avi),
                        self.damaged('clip.avi'), 0.6)
        result = self.recover(path)
        self.assertEqual(result['method'], 'avi_salvage')
        self.assertTrue(result['output'].endswith('.mov'))
        self.assertFalse(result['video_only'])
        self.assertPlaysCleanly(result['output'], min_frames=60)


@needs_ffmpeg
@needs_x264
class Rotation(RecoveryTestCase):

    def test_auto_copies_donor_rotation(self):
        self.add_donor('donor_portrait', rotation=90)
        path = drop_moov(CLIPS.get('base'), self.damaged('clip.mp4'))
        result = self.recover(path)
        self.assertEqual(result['rotation'], 90)

    def test_explicit_rotation(self):
        self.add_donor('donor_portrait', rotation=90)
        path = drop_moov(CLIPS.get('base'), self.damaged('clip.mp4'))
        for degrees in (0, 180, 270):
            result = self.recover(path, rotation=degrees)
            self.assertEqual(result['rotation'], degrees)
            os.unlink(result['output'])


class OutputFolder(FrayTestCase):

    def test_missing_folder_rejected(self):
        folder, error = _check_output_dir(os.path.join(self.work, 'nope'))
        self.assertIsNone(folder)
        self.assertIn('not found', error)

    def test_read_only_folder_rejected(self):
        ro = os.path.join(self.work, 'ro')
        os.makedirs(ro)
        os.chmod(ro, 0o500)
        try:
            if os.access(ro, os.W_OK):
                self.skipTest('running with permissions that ignore read-only folders')
            folder, error = _check_output_dir(ro)
            self.assertIsNone(folder)
            self.assertIn('not writable', error)
        finally:
            os.chmod(ro, 0o700)

    def test_tilde_and_relative_paths_resolve(self):
        folder, error = _check_output_dir(self.out_dir)
        self.assertIsNone(error)
        self.assertEqual(folder, os.path.abspath(self.out_dir))


if __name__ == '__main__':
    unittest.main()
