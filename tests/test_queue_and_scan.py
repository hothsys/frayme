"""The scan generator and the recovery queue, as the web app drives them."""

import os
import threading
import time
import unittest
from unittest import mock

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


class HeldRecoveries:
    """Stands in for the recovery engine: each job waits until the block
    ends, so tests can see which jobs run at once."""

    def __init__(self):
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.started, self.running, self.most = [], 0, 0

    def fake(self, filepath, *args):
        with self.lock:
            self.started.append(filepath)
            self.running += 1
            self.most = max(self.most, self.running)
        self.release.wait(30)
        with self.lock:
            self.running -= 1
        return {'ok': True, 'output': filepath}

    def wait_running(self, n, timeout=10):
        deadline = time.time() + timeout
        while self.running < n:
            if time.time() > deadline:
                raise AssertionError(f'{self.running} jobs running, expected {n}')
            time.sleep(0.02)

    def __enter__(self):
        self.patch = mock.patch.object(jobs, '_execute_recovery', self.fake)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.release.set()
        # Keep the stand-in until the held jobs have finished with it
        for job in jobs.list_jobs():
            if job['status'] in ('queued', 'recovering'):
                wait_for(job['id'])
        self.patch.stop()


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
        with HeldRecoveries() as held:
            ids = [jobs.enqueue_job(self.damaged(f'clip{i}.mp4', touch=True),
                                    output_dir=self.out_dir)[1]['job_id'] for i in range(3)]
            held.wait_running(2)
            third = self.damaged('clip2.mp4')
            self.assertEqual(jobs.enqueue_job(third, output_dir=self.out_dir)[1]['error'],
                             'Already queued')
            self.assertEqual(jobs.cancel_job(ids[0])[0], 409, 'running jobs cannot be cancelled')
            self.assertEqual(jobs.cancel_job(ids[2])[0], 200)
            self.assertNotIn(ids[2], [j['id'] for j in jobs.list_jobs()])
        for job_id in ids[:2]:
            self.assertEqual(wait_for(job_id)['status'], 'done')
        self.assertNotIn(third, held.started, 'a cancelled job ran')
        self.assertEqual(jobs.cancel_job(ids[0])[0], 409, 'finished jobs cannot be cancelled')

    def test_two_recoveries_run_at_once(self):
        with HeldRecoveries() as held:
            ids = [jobs.enqueue_job(self.damaged(f'clip{i}.mp4', touch=True),
                                    output_dir=self.out_dir)[1]['job_id'] for i in range(4)]
            held.wait_running(2)
            time.sleep(0.3)
            self.assertEqual(held.running, 2, 'the pool holds two workers')
            self.assertEqual([j['status'] for j in jobs.list_jobs()],
                             ['recovering', 'recovering', 'queued', 'queued'])
        for job_id in ids:
            self.assertEqual(wait_for(job_id)['status'], 'done')
        self.assertEqual(held.most, 2)

    def test_same_names_never_run_together(self):
        # Outputs and temporary files are named after the source, so two
        # GOPR0001.MP4 files into one folder must take turns
        a, b = (os.path.join(self.src_dir, d) for d in ('card1', 'card2'))
        for d in (a, b):
            os.makedirs(d)
            open(os.path.join(d, 'GOPR0001.MP4'), 'wb').close()
        open(os.path.join(a, 'gopr0001.mov'), 'wb').close()
        other = self.damaged('other.mp4', touch=True)
        with HeldRecoveries() as held:
            ids = [jobs.enqueue_job(p, output_dir=self.out_dir)[1]['job_id']
                   for p in (os.path.join(a, 'GOPR0001.MP4'), os.path.join(b, 'GOPR0001.MP4'),
                             os.path.join(a, 'gopr0001.mov'), other)]
            held.wait_running(2)
            self.assertEqual(sorted(os.path.basename(p) for p in held.started),
                             ['GOPR0001.MP4', 'other.mp4'], 'the same name ran twice at once')
        for job_id in ids:
            self.assertEqual(wait_for(job_id)['status'], 'done')
        self.assertEqual(held.most, 2)

    def test_donor_folder_checked_once(self):
        # Two recoveries from a folder no scan has seen (a saved scan) both
        # want its donors; one checks the files, the other waits for it
        from backend import donors
        for i in range(3):
            self.place(CLIPS.get('base'), f'healthy{i}.mp4')
        real, calls = donors.check_video, []

        def counted(path, *args, **kwargs):
            calls.append(path)
            time.sleep(0.1)
            return real(path, *args, **kwargs)
        with mock.patch.object(donors, 'check_video', counted):
            threads = [threading.Thread(target=donors._ensure_donor_pool, args=(self.src_dir,))
                       for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(donors._donor_files(self.src_dir)), 3)

    def test_parallel_recoveries_both_play(self):
        # Real recoveries side by side, sharing the donor pool
        self.place(CLIPS.get('donor', pattern='b'), 'donor.mp4')
        paths = [drop_moov(CLIPS.get('base'), self.damaged('one.mp4')),
                 drop_moov(CLIPS.get('bf', bframes=2), self.damaged('two.mp4'))]
        ids = [jobs.enqueue_job(p, output_dir=self.out_dir)[1]['job_id'] for p in paths]
        for job_id in ids:
            job = wait_for(job_id)
            self.assertEqual(job['status'], 'done', job.get('error'))
            self.assertPlaysCleanly(job['result']['output'])

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
