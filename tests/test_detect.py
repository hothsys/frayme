"""Detection: each kind of damage gets the right status, and the app offers
Recover only where recovery can work."""

import os
import shutil
import unittest

from tests.fixtures import (CLIPS, FrayTestCase, cut_tail, drop_moov, make_avi,
                            make_other, needs_ffmpeg, needs_x264, overwrite_junk,
                            ui_offers_recover, zero_head)
from backend.detect import check_video


@needs_ffmpeg
@needs_x264
class DetectMP4(FrayTestCase):

    def test_healthy_file_is_ok(self):
        path = self.place(CLIPS.get('base'), 'healthy.mp4')
        status, reason, details = check_video(path)
        self.assertEqual(status, 'ok', reason)
        self.assertTrue(details['has_moov'])
        self.assertAlmostEqual(details['duration'], 4, delta=0.2)

    def test_empty_file(self):
        path = self.damaged('empty.mp4')
        open(path, 'wb').close()
        status, _, details = check_video(path)
        self.assertEqual(status, 'empty')
        self.assertFalse(ui_offers_recover(status, details))

    def test_missing_moov(self):
        path = drop_moov(CLIPS.get('base'), self.damaged('nomoov.mp4'))
        status, _, details = check_video(path)
        self.assertEqual(status, 'no_moov')
        self.assertTrue(ui_offers_recover(status, details))

    def test_cut_off_recording_without_moov(self):
        # A camera writing moov at the end loses it when recording stops
        path = cut_tail(CLIPS.get('base'), self.damaged('cut.mp4'), 0.6)
        status, _, details = check_video(path)
        self.assertEqual(status, 'no_moov')
        self.assertTrue(ui_offers_recover(status, details))

    def test_truncated_file_with_moov_first(self):
        path = cut_tail(CLIPS.get('fast', faststart=True), self.damaged('trunc.mp4'), 0.6)
        status, reason, _ = check_video(path)
        self.assertEqual(status, 'truncated', reason)

    def test_zeroed_header(self):
        path = zero_head(CLIPS.get('base'), self.damaged('zeroed.mp4'), 4096)
        status, reason, details = check_video(path)
        self.assertEqual(status, 'corrupt')
        self.assertIn('Leading zeros', reason)
        self.assertEqual(details['zero_bytes'], 4096)
        self.assertTrue(ui_offers_recover(status, details))

    def test_all_zeros_is_not_offered(self):
        path = self.damaged('zeros.mp4')
        with open(path, 'wb') as f:
            f.write(b'\x00' * 200000)
        status, _, details = check_video(path)
        self.assertEqual(status, 'corrupt')
        self.assertFalse(ui_offers_recover(status, details))

    def test_random_data_is_not_ok(self):
        path = self.damaged('random.mp4')
        with open(path, 'wb') as f:
            f.write(os.urandom(100000))
        status, _, _ = check_video(path)
        self.assertNotEqual(status, 'ok')

    def test_wrong_extension_routes_by_signature(self):
        # Carved and renamed files often carry the wrong extension
        path = self.place(CLIPS.get('base'), 'actually_mp4.avi')
        status, reason, _ = check_video(path)
        self.assertEqual(status, 'ok', reason)


@needs_ffmpeg
class DetectOtherFormats(FrayTestCase):

    def test_healthy_avi(self):
        path = self.place(CLIPS.get('avi', ext='.avi', maker=make_avi), 'ok.avi')
        status, reason, _ = check_video(path)
        self.assertEqual(status, 'ok', reason)

    def test_truncated_avi_is_salvageable(self):
        path = cut_tail(CLIPS.get('avi', ext='.avi', maker=make_avi),
                        self.damaged('cut.avi'), 0.6)
        status, _, details = check_video(path)
        self.assertNotEqual(status, 'ok')
        self.assertTrue(ui_offers_recover(status, details))

    @needs_x264
    def test_healthy_mkv_and_ts(self):
        for ext in ('.mkv', '.ts'):
            path = self.place(CLIPS.get('other' + ext, ext=ext, maker=make_other),
                              'ok' + ext)
            status, reason, _ = check_video(path)
            self.assertEqual(status, 'ok', f'{ext}: {reason}')

    @needs_x264
    def test_ts_joined_mid_stream_is_ok(self):
        # Broadcast captures and dashcam segments start between keyframes;
        # the decoder complains until the first one, but nothing is damaged
        src = CLIPS.get('ts_gop', ext='.ts', maker=make_other, duration=8, gop=30)
        with open(src, 'rb') as f:
            data = f.read()
        path = self.damaged('joined.ts')
        with open(path, 'wb') as f:
            f.write(data[(len(data) // 3) // 188 * 188 + 188 * 5:])
        status, reason, _ = check_video(path)
        self.assertEqual(status, 'ok', reason)

    @needs_x264
    def test_damaged_ts_is_reported_not_offered(self):
        src = CLIPS.get('ts_gop', ext='.ts', maker=make_other, duration=8, gop=30)
        path = overwrite_junk(src, self.damaged('junk.ts'), 1 / 6, 60000)
        status, _, details = check_video(path)
        self.assertEqual(status, 'corrupt')
        self.assertFalse(ui_offers_recover(status, details))

    @needs_x264
    def test_unreadable_mkv_is_reported_not_offered(self):
        path = self.damaged('bad.mkv')
        src = CLIPS.get('other.mkv', ext='.mkv', maker=make_other)
        shutil.copyfile(src, path)
        with open(path, 'r+b') as f:
            f.write(b'\x00' * 4096)
        status, _, details = check_video(path)
        self.assertNotEqual(status, 'ok')
        self.assertFalse(ui_offers_recover(status, details))


if __name__ == '__main__':
    unittest.main()
