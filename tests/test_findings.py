"""What the result rows tell the user: facts and notes for damaged and
recovered files, and the on-request look through a whole file."""

import os
import random
import struct
import unittest

from tests.fixtures import (CLIPS, FrayTestCase, cut_tail, drop_moov, make_avi,
                            needs_ffmpeg, needs_x264, needs_x265, top_boxes,
                            zero_head)
from backend.findings import describe_contents, describe_damaged
from backend.detect import check_video
from backend.recovery import _execute_recovery
from backend.scan import scan_directory_gen


def texts(findings, level=None):
    return [n['text'] for n in findings['notes'] if level in (None, n['level'])]


def mentions(notes, phrase):
    return any(phrase.lower() in n.lower() for n in notes)


def describe(path):
    status, _, details = check_video(path)
    return describe_damaged(path, status, details)


@needs_ffmpeg
@needs_x264
class DamagedFiles(FrayTestCase):

    def test_missing_moov_explains_what_is_lost(self):
        found = describe(drop_moov(CLIPS.get('base'), self.damaged('clip.mp4')))
        self.assertEqual(found['facts'][0], 'H.264')
        warnings = texts(found, 'warn')
        for lost in ('orientation', 'frame rate', 'sound'):
            self.assertTrue(mentions(warnings, lost), f'no warning about {lost}')
        # x264 in MP4 keeps its parameter sets in the moov, now gone
        self.assertTrue(mentions(texts(found), 'No decoder settings in the file'))

    def test_zeroed_header_with_moov_at_end(self):
        found = describe(zero_head(CLIPS.get('base'), self.damaged('clip.mp4'), 4096))
        notes = texts(found)
        self.assertTrue(mentions(notes, 'first 4.0 KB are zeroed'))
        self.assertTrue(mentions(notes, 'index (moov) survives'))
        self.assertFalse(mentions(notes, 'No orientation'))

    def test_zeroed_moov(self):
        found = describe(zero_head(CLIPS.get('fast', faststart=True),
                                   self.damaged('clip.mp4'), 20000))
        self.assertTrue(mentions(texts(found, 'warn'), 'No orientation'))

    def test_truncated_says_how_much_is_missing(self):
        found = describe(cut_tail(CLIPS.get('fast', faststart=True),
                                  self.damaged('clip.mp4'), 0.6))
        self.assertTrue(mentions(texts(found, 'warn'), '% is missing'))

    def test_empty_file(self):
        path = self.damaged('empty.mov')
        open(path, 'wb').close()
        self.assertTrue(mentions(texts(describe(path)), 'nothing to recover'))

    def test_contents_find_the_video_stretches(self):
        # A carved-style file: two stretches of video with 2 MB of other
        # data between them, all inside one mdat
        tmp = drop_moov(CLIPS.get('base'), self.damaged('tmp.mp4'))
        boxes = {kind: (off, size) for kind, off, size in top_boxes(tmp)}
        with open(tmp, 'rb') as f:
            data = f.read()
        os.unlink(tmp)
        payload = data[boxes['mdat'][0] + 8:boxes['mdat'][0] + boxes['mdat'][1]]
        junk = random.Random(2).getrandbits(8 * (2 << 20)).to_bytes(2 << 20, 'big')
        body = payload + junk + payload
        path = self.damaged('clip.mp4')
        with open(path, 'wb') as f:
            f.write(data[:boxes['mdat'][0]] + struct.pack('>I', len(body) + 8) + b'mdat' + body)
        notes = describe_contents(path, check_video(path)[2])
        self.assertTrue(mentions([n['text'] for n in notes], 'separate stretches'))
        whole = describe_contents(drop_moov(CLIPS.get('base'), self.damaged('whole.mp4')), None)
        self.assertTrue(mentions([n['text'] for n in whole], 'in one stretch'))

    def test_scan_events_carry_findings(self):
        drop_moov(CLIPS.get('base'), self.damaged('clip.mp4'))
        issues = [e for e in scan_directory_gen(self.src_dir) if e['type'] == 'issue']
        self.assertIn('findings', issues[0]['details'])
        self.assertTrue(issues[0]['details']['findings']['notes'])


@needs_ffmpeg
@needs_x265
class HEVCFiles(FrayTestCase):

    def test_hevc_is_named_and_needs_a_donor(self):
        found = describe(drop_moov(CLIPS.get('hevc', vcodec='libx265'), self.damaged('c.mp4')))
        self.assertEqual(found['facts'][0], 'HEVC')
        self.assertTrue(mentions(texts(found, 'warn'), 'needs a donor'))


@needs_ffmpeg
class AVIFiles(FrayTestCase):

    def test_avi_notes(self):
        found = describe(cut_tail(CLIPS.get('avi', ext='.avi', maker=make_avi),
                                  self.damaged('clip.avi'), 0.6))
        self.assertEqual(found['facts'], ['AVI'])
        self.assertTrue(mentions(texts(found), 'Has sound'))


@needs_ffmpeg
@needs_x264
class RecoveredFiles(FrayTestCase):

    def recover(self, path, **kwargs):
        return _execute_recovery(path, output_dir=self.out_dir, **kwargs)

    def test_no_donor_warns_about_orientation_and_frame_rate(self):
        result = self.recover(drop_moov(CLIPS.get('base'), self.damaged('clip.mp4')))
        self.assertEqual(result['facts'], ['H.264', '640×360', '30 fps'])
        warnings = [n['text'] for n in result['notes'] if n['level'] == 'warn']
        self.assertTrue(mentions(warnings, 'Orientation unknown'))
        self.assertTrue(mentions(warnings, '30 fps is assumed'))
        self.assertTrue(mentions(warnings, 'No sound'))

    def test_donor_supplies_rotation_and_frame_rate(self):
        self.place(CLIPS.get('donor_portrait', pattern='b', rotation=90), 'donor.mp4')
        result = self.recover(drop_moov(CLIPS.get('base'), self.damaged('clip.mp4')))
        notes = [n['text'] for n in result['notes']]
        self.assertTrue(mentions(notes, 'Rotation (90°) copied from the donor donor.mp4'))
        self.assertTrue(mentions(notes, 'Frame rate from the donor donor.mp4'))
        self.assertFalse(mentions(notes, 'Orientation unknown'))

    def test_chosen_rotation(self):
        result = self.recover(drop_moov(CLIPS.get('base'), self.damaged('clip.mp4')),
                              rotation=270)
        self.assertTrue(mentions([n['text'] for n in result['notes']],
                                 'Rotation set to 270° as chosen'))

    def test_surviving_moov_keeps_everything(self):
        result = self.recover(zero_head(CLIPS.get('base'), self.damaged('clip.mp4'), 4096))
        notes = [n['text'] for n in result['notes']]
        self.assertFalse(mentions(notes, 'Orientation unknown'))
        self.assertFalse(mentions(notes, 'No sound'))


if __name__ == '__main__':
    unittest.main()
