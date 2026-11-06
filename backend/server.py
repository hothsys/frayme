"""HTTP server: serves the web app and routes /api requests."""

import csv
import io
import json
import os
import subprocess
import sys
import time
from http.server import SimpleHTTPRequestHandler
from urllib.parse import parse_qs
from urllib.parse import urlparse

from .config import DEFAULT_OUTPUT_DIR, VIDEO_EXTS
from .detect import check_video
from .donors import _ensure_donor_pool, _usable_donor
from .jobs import cancel_job, clear_finished, enqueue_job, list_jobs, rotate_output
from .recovery import _check_output_dir, _execute_recovery
from .scan import scan_directory_gen
from .state import APP_DIR, donor_map, donor_pool, scan_state
from .tools import _ffmpeg_path, _ffprobe_path, has_ffmpeg, has_ffprobe


# ─── HTTP handler ─────────────────────────────────────────────────────────────

class FrameHandler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=APP_DIR, **kwargs)

    def end_headers(self):
        # Make browsers revalidate static assets so CSS/JS edits show up on reload
        if not self._route_path().startswith('/api/'):
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def _send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            return {}

    def _query_params(self):
        return parse_qs(urlparse(self.path).query)

    def _route_path(self):
        return urlparse(self.path).path

    # ── GET routes ──

    def do_GET(self):
        path = self._route_path()
        try:
            if path == '/api/scan':
                self._handle_scan()
            elif path == '/api/export':
                self._handle_export()
            elif path == '/api/browse':
                self._handle_browse()
            elif path == '/api/status':
                self._handle_status()
            elif path == '/api/recover-status':
                self._handle_recover_status()
            elif path == '/api/mount-status':
                self._handle_mount_status()
            else:
                super().do_GET()
        except (ConnectionResetError, BrokenPipeError):
            pass

    # ── POST routes ──

    def do_POST(self):
        path = self._route_path()
        try:
            if path == '/api/cancel':
                self._handle_cancel()
            elif path == '/api/reveal':
                self._handle_reveal()
            elif path == '/api/set-donor':
                self._handle_set_donor()
            elif path == '/api/recover':
                self._handle_recover()
            elif path == '/api/recover-queue':
                self._handle_recover_queue()
            elif path == '/api/recover-queue-clear':
                self._handle_recover_queue_clear()
            elif path == '/api/recover-cancel':
                self._handle_recover_cancel()
            elif path == '/api/rotate-output':
                self._handle_rotate_output()
            elif path == '/api/load-scan':
                self._handle_load_scan()
            elif path == '/api/add-donor-folder':
                self._handle_add_donor_folder()
            else:
                self.send_error(404)
        except (ConnectionResetError, BrokenPipeError):
            pass

    # ── Handler methods ──

    def _handle_scan(self):
        params = self._query_params()
        root_dir = params.get('dir', [''])[0].strip()
        if not root_dir:
            return self._send_json({'error': 'No directory specified'}, 400)

        # A new id makes any still-running previous scan stop on its own
        scan_state['scan_id'] += 1
        scan_id = scan_state['scan_id']
        scan_state['cancelled'] = False
        scan_state['running'] = True
        scan_state['results'] = []
        donor_pool.clear()

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        try:
            for event in scan_directory_gen(root_dir, scan_id):
                if event.get('type') == 'issue':
                    scan_state['results'].append(event)
                line = f"data: {json.dumps(event, default=str)}\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if scan_state['scan_id'] == scan_id:
                scan_state['running'] = False

    def _handle_cancel(self):
        scan_state['cancelled'] = True
        self._send_json({'ok': True})

    def _handle_export(self):
        results = scan_state.get('results', [])
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'Status', 'Filename', 'Extension', 'Directory', 'Full Path',
            'File Size (bytes)', 'Issue',
        ])
        for r in results:
            writer.writerow([
                r.get('status', ''),
                r.get('filename', ''),
                r.get('ext', ''),
                r.get('dir', ''),
                r.get('filepath', ''),
                r.get('file_size', ''),
                r.get('reason', ''),
            ])

        body = output.getvalue().encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/csv')
        self.send_header('Content-Disposition',
                         'attachment; filename=video_scan_results.csv')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_browse(self):
        params = self._query_params()
        path = params.get('path', [''])[0].strip()
        if not path:
            if sys.platform == 'darwin':
                path = '/Volumes'
            else:
                path = '/'

        if not os.path.isdir(path):
            return self._send_json({'error': 'Not a directory',
                                    'path': path}, 400)

        entries = []
        try:
            for name in sorted(os.listdir(path)):
                if name.startswith('.'):
                    continue
                full = os.path.join(path, name)
                if os.path.isdir(full):
                    entries.append({'name': name, 'path': full})
        except PermissionError:
            return self._send_json({'error': 'Permission denied',
                                    'path': path}, 403)

        self._send_json({'path': path, 'entries': entries})

    def _handle_mount_status(self):
        params = self._query_params()
        path = params.get('path', [''])[0].strip()
        if not path:
            return self._send_json({'error': 'No path specified'}, 400)

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        last_exists = None
        try:
            while True:
                try:
                    exists = (os.path.isdir(path) and
                              next(os.scandir(path), None) is not None)
                except OSError:
                    exists = False
                if exists != last_exists:
                    last_exists = exists
                    event = json.dumps({'exists': exists, 'path': path})
                    self.wfile.write(f'data: {event}\n\n'.encode())
                    self.wfile.flush()
                time.sleep(3)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def _handle_status(self):
        self._send_json({
            'ffmpeg': has_ffmpeg(),
            'ffprobe': has_ffprobe(),
            'ffmpeg_path': _ffmpeg_path or '',
            'ffprobe_path': _ffprobe_path or '',
            'default_output_dir': DEFAULT_OUTPUT_DIR,
        })

    def _handle_reveal(self):
        data = self._read_json_body()
        filepath = data.get('filepath', '')
        if not filepath or not os.path.exists(filepath):
            return self._send_json({'error': 'File not found'}, 404)

        # open: show a folder's contents instead of selecting it in its parent
        if data.get('open') and os.path.isdir(filepath):
            opener = {'darwin': 'open', 'win32': 'explorer'}.get(sys.platform, 'xdg-open')
            try:
                subprocess.Popen([opener, filepath])
            except Exception as e:
                return self._send_json({'error': str(e)}, 500)
            return self._send_json({'ok': True})

        try:
            if sys.platform == 'darwin':
                subprocess.Popen(['open', '-R', filepath])
            elif sys.platform == 'win32':
                subprocess.Popen(['explorer', '/select,', filepath])
            else:
                dirpath = os.path.dirname(filepath)
                subprocess.Popen(['xdg-open', dirpath])
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)

        self._send_json({'ok': True})

    def _handle_load_scan(self):
        # A previous scan picked in the browser becomes the results Export uses
        if scan_state.get('running'):
            return self._send_json({'error': 'A scan is running'}, 409)
        results = self._read_json_body().get('results')
        if not isinstance(results, list):
            return self._send_json({'error': 'No results provided'}, 400)
        scan_state['results'] = results
        self._send_json({'ok': True, 'count': len(results)})

    def _handle_add_donor_folder(self):
        # The user points at a folder of healthy videos from the same camera;
        # they become donor candidates for the damaged file's folder, and SPS
        # probing picks the one that matches
        data = self._read_json_body()
        filepath = data.get('filepath', '')
        donor_dir = os.path.expanduser(data.get('donor_dir', '').strip())
        if not filepath or not os.path.isdir(donor_dir):
            return self._send_json({'error': 'Folder not found'}, 404)
        found, checked = [], 0
        deadline = time.time() + 60
        for dirpath, dirnames, filenames in os.walk(donor_dir):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith('.') and d != '__MACOSX']
            for name in sorted(filenames):
                fpath = os.path.join(dirpath, name)
                if (os.path.splitext(name)[1].lower() not in VIDEO_EXTS
                        or fpath == filepath):
                    continue
                checked += 1
                try:
                    status, _, details = check_video(fpath)
                except Exception:
                    continue
                if _usable_donor(status, details):
                    found.append(fpath)
            if checked >= 2000 or time.time() > deadline:
                break
        fdir = os.path.dirname(filepath)
        _ensure_donor_pool(fdir)
        pool = donor_pool.setdefault(fdir, [])
        added = [f for f in found if f not in pool]
        pool[:0] = added
        self._send_json({'ok': True, 'added': len(added),
                         'found': len(found), 'checked': checked})

    def _handle_set_donor(self):
        data = self._read_json_body()
        filepath = data.get('filepath', '')
        if not filepath or not os.path.exists(filepath):
            return self._send_json({'error': 'File not found'}, 404)

        directory = data.get('directory', os.path.dirname(filepath))
        donor_map[directory] = filepath
        self._send_json({'ok': True, 'directory': directory,
                         'donor': os.path.basename(filepath)})

    def _handle_recover(self):
        data = self._read_json_body()
        filepath = data.get('filepath', '')
        donor_override = data.get('donor', '')

        if not filepath or not os.path.exists(filepath):
            return self._send_json({'error': 'File not found'}, 404)
        output_dir, error = _check_output_dir(data.get('output_dir', ''))
        if error:
            return self._send_json({'error': error}, 400)

        try:
            resp = _execute_recovery(filepath, donor_override, output_dir)
            self._send_json(resp)
        except ValueError as e:
            self._send_json({'error': str(e)}, 422)
        except Exception as e:
            self._send_json({'error': f'Recovery failed: {e}'}, 500)

    def _handle_recover_queue(self):
        data = self._read_json_body()
        code, payload = enqueue_job(
            data.get('filepath', ''), data.get('donor', ''),
            data.get('output_dir', ''), data.get('rotation'))
        self._send_json(payload, code)

    def _handle_recover_status(self):
        self._send_json({'jobs': list_jobs()})

    def _handle_rotate_output(self):
        data = self._read_json_body()
        code, payload = rotate_output(data.get('output', ''), data.get('rotation'))
        self._send_json(payload, code)

    def _handle_recover_cancel(self):
        job_id = str(self._read_json_body().get('job_id', ''))
        code, payload = cancel_job(job_id)
        self._send_json(payload, code)

    def _handle_recover_queue_clear(self):
        self._send_json({'ok': True, 'cleared': clear_finished()})

    def log_message(self, format, *args):
        pass  # Suppress default access log noise
