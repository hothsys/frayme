"""The scan generator and the recovery queue, as the web app drives them."""

import os
import time
import unittest

from tests.fixtures import (CLIPS, FrayTestCase, drop_moov, needs_ffmpeg, needs_x264,
                            zero_head)
from backend import jobs, state
from backend.scan import scan_directory_gen


def wait_for(job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = next((j for j in jobs.list_jobs() if j['id'] == job_id), None)
        if job and job['status'] in ('done', 'failed'):
            return job
        time.sleep(0.2)
    raise AssertionError(f'job {job_id} did not finish')


@needs_ffmpeg
@needs_x264
class Scan(FrayTestCase):

    def setUp(self):
        super().setUp()
        state.scan_state['cancelled'] = False

    def run_scan(self, root=None):
        events = list(scan_directory_gen(root or self.src_dir))
        return events, [e for e in events if e['type'] == 'issue']

    def test_reports_damage_and_collects_donors(self):
        healthy = self.place(CLIPS.get('base'), 'healthy.mp4')
        drop_moov(CLIPS.get('base'), self.damaged('nomoov.mp4'))
        zero_head(CLIPS.get('base'), self.damaged('zeroed.mp4'), 4096)
        open(self.damaged('empty.mov'), 'wb').close()
        with open(os.path.join(self.src_dir, 'notes.txt'), 'w') as f:
            f.write('not a video')

        events, issues = self.run_scan()
        self.assertEqual(events[-1]['type'], 'complete')
        self.assertEqual(events[-1]['scanned'], 4)
        self.assertEqual({e['filename']: e['status'] for e in issues},
                         {'nomoov.mp4': 'no_moov', 'zeroed.mp4': 'corrupt',
                          'empty.mov': 'empty'})
        self.assertEqual(state.donor_pool[self.src_dir], [healthy])

    def test_skips_hidden_and_macosx_folders(self):
        for folder in ('.hidden', '__MACOSX'):
            os.makedirs(os.path.join(self.src_dir, folder))
            open(os.path.join(self.src_dir, folder, 'x.mp4'), 'wb').close()
        sub = os.path.join(self.src_dir, 'sub')
        os.makedirs(sub)
        open(os.path.join(sub, 'found.mp4'), 'wb').close()
        _, issues = self.run_scan()
        self.assertEqual([e['filename'] for e in issues], ['found.mp4'])

    def test_missing_folder(self):
        events = list(scan_directory_gen(os.path.join(self.work, 'nope')))
        self.assertEqual(events[0]['type'], 'error')

    def test_cancel(self):
        for i in range(6):
            self.place(CLIPS.get('base'), f'clip{i}.mp4')
        gen = scan_directory_gen(self.src_dir)
        for event in gen:
            if event['type'] == 'scanning':
                state.scan_state['cancelled'] = True
        state.scan_state['cancelled'] = False
        self.assertEqual(event['type'], 'cancelled')


@needs_ffmpeg
@needs_x264
class Queue(FrayTestCase):

    def tearDown(self):
        jobs.clear_finished()
        super().tearDown()

    def test_job_recovers_into_chosen_folder(self):
        path = drop_moov(CLIPS.get('base'), self.damaged('clip.mp4'))
        code, payload = jobs.enqueue_job(path, output_dir=self.out_dir)
        self.assertEqual(code, 200, payload)
        job = wait_for(payload['job_id'])
        self.assertEqual(job['status'], 'done', job.get('error'))
        self.assertEqual(os.path.dirname(job['result']['output']), self.out_dir)

    def test_rejects_bad_requests(self):
        path = drop_moov(CLIPS.get('base'), self.damaged('clip.mp4'))
        self.assertEqual(jobs.enqueue_job(self.damaged('missing.mp4'),
                                          output_dir=self.out_dir)[0], 404)
        code, payload = jobs.enqueue_job(path, output_dir=os.path.join(self.work, 'nope'))
        self.assertEqual((code, payload.get('error_kind')), (400, 'output_dir'))
        self.assertEqual(jobs.enqueue_job(path, output_dir=self.out_dir, rotation=45)[0], 400)

    def test_failed_job_reports_why(self):
        path = self.damaged('zeros.mp4')
        with open(path, 'wb') as f:
            f.write(b'\x00' * 50000 + os.urandom(150000))
        job = wait_for(jobs.enqueue_job(path, output_dir=self.out_dir)[1]['job_id'])
        self.assertEqual(job['status'], 'failed')
        self.assertTrue(job['error'])
        self.assertEqual(os.listdir(self.out_dir), [], 'a failed job left a file')

    def test_duplicate_and_cancel(self):
        # Hold the worker on a slow first job so the second stays queued
        slow = drop_moov(CLIPS.get('bf', bframes=2), self.damaged('slow.mp4'))
        path = drop_moov(CLIPS.get('base'), self.damaged('clip.mp4'))
        first = jobs.enqueue_job(slow, output_dir=self.out_dir)[1]['job_id']
        second = jobs.enqueue_job(path, output_dir=self.out_dir)[1]['job_id']
        self.assertEqual(jobs.enqueue_job(path, output_dir=self.out_dir)[1]['error'],
                         'Already queued')
        self.assertEqual(jobs.cancel_job(second)[0], 200)
        self.assertNotIn(second, [j['id'] for j in jobs.list_jobs()])
        wait_for(first)
        self.assertEqual(jobs.cancel_job(first)[0], 409, 'finished jobs cannot be cancelled')
        self.assertFalse(os.path.exists(os.path.join(self.out_dir, 'clip_recovered.mp4')))

    def test_rotate_output_only_for_this_sessions_outputs(self):
        path = drop_moov(CLIPS.get('base'), self.damaged('clip.mp4'))
        job = wait_for(jobs.enqueue_job(path, output_dir=self.out_dir)[1]['job_id'])
        output = job['result']['output']
        code, payload = jobs.rotate_output(output, 270)
        self.assertEqual((code, payload['rotation']), (200, 270))
        self.assertEqual(jobs.rotate_output(self.place(CLIPS.get('base'), 'other.mp4'), 90)[0], 404)
        self.assertEqual(jobs.rotate_output(output, 45)[0], 400)

    def test_clear_finished(self):
        path = drop_moov(CLIPS.get('base'), self.damaged('clip.mp4'))
        wait_for(jobs.enqueue_job(path, output_dir=self.out_dir)[1]['job_id'])
        self.assertGreaterEqual(jobs.clear_finished(), 1)
        self.assertEqual(jobs.list_jobs(), [])


if __name__ == '__main__':
    unittest.main()
