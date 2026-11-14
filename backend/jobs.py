"""The recovery queue: jobs, the worker thread, and queue operations."""

import os
import threading
import time

from .config import RECOVERY_WORKERS, VIDEO_EXTS, _ROTATIONS
from .hints import _source_hints
from .recovery import _apply_rotation, _check_output_dir, _execute_recovery, _read_rotation


# Every job this session, by id. Queued jobs wait here in id order; there is
# no separate queue, so cancelling a job is just removing it.
_recover_jobs = {}


_recover_lock = threading.Lock()


# Signalled when a job is queued or finishes
_recover_wakeup = threading.Condition(_recover_lock)


_recover_counter = 0


_recover_workers = []


# Output names in use by running jobs: (output folder, stem), lowercased
# since macOS folders ignore case. Engines name their outputs and temporary
# files <stem>_recovered.*, so two jobs on files with the same name (say
# GOPR0001.MP4 from two cards) must not run at once into the same folder.
_running_outputs = set()


def _output_key(job):
    stem = os.path.splitext(os.path.basename(job['filepath']))[0]
    return (job['output_dir'].lower(), stem.lower())


def _next_job():
    """The oldest queued job whose output names are free, marked as running.
    Blocks until there is one. Call without the lock held."""
    with _recover_wakeup:
        while True:
            for job_id in sorted((j for j, job in _recover_jobs.items()
                                  if job['status'] == 'queued'), key=int):
                job = _recover_jobs[job_id]
                key = _output_key(job)
                if key not in _running_outputs:
                    _running_outputs.add(key)
                    job['status'] = 'recovering'
                    job['started_at'] = time.time()
                    return job
            _recover_wakeup.wait()


def _recovery_worker():
    """Daemon thread: runs queued jobs, one at a time per worker."""
    while True:
        job = _next_job()
        update = {}
        try:
            result = _execute_recovery(
                job['filepath'], job.get('donor', ''), job.get('output_dir', ''),
                job.get('rotation'))
            update = {'status': 'done', 'result': result}
        except Exception as e:
            hints = None
            if os.path.splitext(job['filepath'])[1].lower() in VIDEO_EXTS - {'.avi'}:
                try:
                    hints = _source_hints(job['filepath'])
                except Exception:
                    pass
            update = {'status': 'failed', 'error': str(e), 'hints': hints}
        finally:
            with _recover_wakeup:
                job.update(update or {'status': 'failed', 'error': 'Recovery stopped'})
                job['finished_at'] = time.time()
                _running_outputs.discard(_output_key(job))
                _recover_wakeup.notify_all()


def _ensure_recovery_workers():
    """Start the worker pool on first use. Each recovery runs several ffmpeg
    processes (trial decodes use half the cores), so the pool stays small."""
    with _recover_lock:
        while len(_recover_workers) < RECOVERY_WORKERS:
            t = threading.Thread(target=_recovery_worker, daemon=True,
                                 name=f'recovery-{len(_recover_workers) + 1}')
            _recover_workers.append(t)
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
        _recover_wakeup.notify_all()

    _ensure_recovery_workers()
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

    Only queued jobs can be cancelled; running ones can't be stopped midway.
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
