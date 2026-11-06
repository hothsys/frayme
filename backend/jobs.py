"""The recovery queue: jobs, the worker thread, and queue operations."""

import os
import queue as queue_mod
import threading
import time

from .config import VIDEO_EXTS, _ROTATIONS
from .hints import _source_hints
from .recovery import _apply_rotation, _check_output_dir, _execute_recovery, _read_rotation


_recover_queue = queue_mod.Queue()


_recover_jobs = {}


_recover_lock = threading.Lock()


_recover_counter = 0


_recover_worker_started = False


def _recovery_worker():
    """Daemon thread processing recovery jobs sequentially."""
    while True:
        job = _recover_queue.get()
        job_id = job['id']
        with _recover_lock:
            # Skip jobs cancelled while waiting (they were removed from the list)
            if job_id not in _recover_jobs:
                _recover_queue.task_done()
                continue
            _recover_jobs[job_id]['status'] = 'recovering'
            _recover_jobs[job_id]['started_at'] = time.time()
        try:
            result = _execute_recovery(
                job['filepath'], job.get('donor', ''), job.get('output_dir', ''),
                job.get('rotation'))
            with _recover_lock:
                _recover_jobs[job_id]['status'] = 'done'
                _recover_jobs[job_id]['result'] = result
                _recover_jobs[job_id]['finished_at'] = time.time()
        except Exception as e:
            hints = None
            if os.path.splitext(job['filepath'])[1].lower() in VIDEO_EXTS - {'.avi'}:
                try:
                    hints = _source_hints(job['filepath'])
                except Exception:
                    pass
            with _recover_lock:
                _recover_jobs[job_id]['status'] = 'failed'
                _recover_jobs[job_id]['error'] = str(e)
                _recover_jobs[job_id]['hints'] = hints
                _recover_jobs[job_id]['finished_at'] = time.time()
        finally:
            _recover_queue.task_done()


def _ensure_recovery_worker():
    global _recover_worker_started
    if not _recover_worker_started:
        _recover_worker_started = True
        t = threading.Thread(target=_recovery_worker, daemon=True)
        t.start()


def enqueue_job(filepath, donor='', output_dir='', rotation=None):
    """Add a recovery to the queue. Returns (status_code, payload)."""
    global _recover_counter
    if not filepath or not os.path.exists(filepath):
        return 404, {'error': 'File not found'}
    output_dir, error = _check_output_dir(output_dir)
    if error:
        return 400, {'ok': False, 'error': error, 'error_kind': 'output_dir'}
    if rotation is not None and rotation not in _ROTATIONS:
        return 400, {'ok': False, 'error': 'Invalid rotation'}

    with _recover_lock:
        for j in _recover_jobs.values():
            if j['filepath'] == filepath and j['status'] in ('queued', 'recovering'):
                return 200, {'ok': False, 'error': 'Already queued'}
        _recover_counter += 1
        job_id = str(_recover_counter)
        job = {
            'id': job_id,
            'filepath': filepath,
            'filename': os.path.basename(filepath),
            'donor': donor,
            'output_dir': output_dir,
            'rotation': rotation,
            'status': 'queued',
            'queued_at': time.time(),
            'started_at': None,
            'finished_at': None,
            'result': None,
            'error': None,
        }
        _recover_jobs[job_id] = job

    _recover_queue.put(job)
    _ensure_recovery_worker()
    return 200, {'ok': True, 'job_id': job_id, 'status': 'queued'}


def list_jobs():
    """All jobs, oldest first."""
    with _recover_lock:
        return sorted(_recover_jobs.values(), key=lambda j: int(j['id']))


def rotate_output(output, rotation):
    """Change a recovered file's rotation in place. Returns (status_code, payload).

    Only files this session's recoveries wrote can be changed.
    """
    if rotation not in _ROTATIONS:
        return 400, {'error': 'Invalid rotation'}
    with _recover_lock:
        job = next((j for j in _recover_jobs.values()
                    if j['status'] == 'done' and j.get('result')
                    and j['result'].get('output') == output), None)
    if not job or not os.path.isfile(output):
        return 404, {'error': 'Recovered file not found'}
    try:
        _apply_rotation(output, rotation)
    except Exception as e:
        return 500, {'error': str(e)}
    with _recover_lock:
        job['result']['rotation'] = _read_rotation(output)
        job['result']['size'] = os.path.getsize(output)
    return 200, {'ok': True, 'rotation': job['result']['rotation']}


def cancel_job(job_id):
    """Remove a job that is still waiting. Returns (status_code, payload).

    Only queued jobs can be cancelled; the worker skips them.
    """
    with _recover_lock:
        job = _recover_jobs.get(job_id)
        if not job or job['status'] != 'queued':
            return 409, {'ok': False, 'error': 'Only queued jobs can be cancelled'}
        del _recover_jobs[job_id]
    return 200, {'ok': True, 'filepath': job['filepath']}


def clear_finished():
    """Remove finished and failed jobs. Returns the number removed."""
    with _recover_lock:
        done_ids = [jid for jid, j in _recover_jobs.items()
                    if j['status'] in ('done', 'failed')]
        for jid in done_ids:
            del _recover_jobs[jid]
    return len(done_ids)
