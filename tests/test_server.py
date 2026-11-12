"""The HTTP layer: static files, JSON endpoints, and the scan event stream.

Starts the real server on a free port. The mount-status stream never ends by
design, so it isn't requested here.
"""

import json
import os
import re
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests.fixtures import (CLIPS, PROJECT_DIR, FrayTestCase, drop_moov, needs_ffmpeg,
                            needs_x264)
from backend import jobs
from backend.server import FrameHandler


class ServerTestCase(FrayTestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FrameHandler)
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=120) as r:
            return r.status, r.headers, r.read()

    def post(self, path, data):
        req = urllib.request.Request(self.base + path, data=json.dumps(data).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, {}       # an HTML error page

    def events(self, path):
        """Server-sent events up to the final one. The stream's connection
        stays open afterwards (the page's EventSource closes it)."""
        events = []
        with urllib.request.urlopen(self.base + path, timeout=120) as r:
            self.assertEqual(r.headers['Content-Type'], 'text/event-stream')
            for line in r:
                if line.startswith(b'data: '):
                    events.append(json.loads(line[6:]))
                    if events[-1]['type'] in ('complete', 'cancelled', 'error'):
                        break
        return events


class StaticFiles(ServerTestCase):

    def test_page_and_every_asset_it_links(self):
        status, headers, body = self.get('/')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-cache')
        html = body.decode()
        assets = re.findall(r'(?:src|href)="(/[^"]+)"', html)
        self.assertTrue(any(a.startswith('/js/') for a in assets))
        for asset in assets:
            with self.subTest(asset=asset):
                status, _, data = self.get(asset)
                self.assertEqual(status, 200)
                self.assertTrue(data)

    def test_scripts_listed_in_index_exist_and_main_is_last(self):
        with open(os.path.join(PROJECT_DIR, 'index.html')) as f:
            scripts = re.findall(r'<script src="/js/([^"]+)"', f.read())
        self.assertEqual(scripts[-1], 'main.js')
        on_disk = sorted(n for n in os.listdir(os.path.join(PROJECT_DIR, 'js'))
                         if n.endswith('.js'))
        self.assertEqual(sorted(scripts), on_disk, 'index.html and js/ disagree')


class Endpoints(ServerTestCase):

    def tearDown(self):
        jobs.clear_finished()
        super().tearDown()

    def test_status(self):
        status, _, body = self.get('/api/status')
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn('ffmpeg', data)
        self.assertIn('default_output_dir', data)

    def test_browse(self):
        _, _, body = self.get('/api/browse?path=' + urllib.request.quote(self.work))
        names = [e['name'] for e in json.loads(body)['entries']]
        self.assertIn('videos', names)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get('/api/browse?path=' + urllib.request.quote(self.work + '/nope'))
        self.assertEqual(ctx.exception.code, 400)

    def test_queue_rejects_bad_requests(self):
        self.assertEqual(self.post('/api/recover-queue', {'filepath': '/nope.mp4'})[0], 404)
        path = self.damaged('x.mp4')
        open(path, 'wb').close()
        code, data = self.post('/api/recover-queue', {
            'filepath': path, 'output_dir': os.path.join(self.work, 'nope')})
        self.assertEqual((code, data.get('error_kind')), (400, 'output_dir'))
        self.assertEqual(self.post('/api/recover-cancel', {'job_id': 'nope'})[0], 409)
        self.assertEqual(self.post('/api/unknown', {})[0], 404)

    @needs_ffmpeg
    @needs_x264
    def test_scan_stream_then_recover(self):
        self.place(CLIPS.get('donor', pattern='b'), 'donor.mp4')
        damaged = drop_moov(CLIPS.get('base'), self.damaged('clip.mp4'))
        events = self.events('/api/scan?dir=' + urllib.request.quote(self.src_dir))
        self.assertEqual(events[-1]['type'], 'complete')
        issues = [e for e in events if e['type'] == 'issue']
        self.assertEqual([e['filepath'] for e in issues], [damaged])

        code, data = self.post('/api/recover-queue', {
            'filepath': damaged, 'output_dir': self.out_dir})
        self.assertEqual(code, 200, data)
        from tests.test_queue_and_scan import wait_for
        job = wait_for(data['job_id'])
        self.assertEqual(job['status'], 'done', job.get('error'))
        self.assertEqual(job['result']['donor_used'], 'donor.mp4')
        # The size comparison uses the scan's record of the damaged file
        self.assertIsNotNone(job['result']['recovery_pct'])

        _, _, body = self.get('/api/recover-status')
        self.assertIn(data['job_id'], [j['id'] for j in json.loads(body)['jobs']])
        _, headers, body = self.get('/api/export')
        self.assertIn('clip.mp4', body.decode())


if __name__ == '__main__':
    unittest.main()
