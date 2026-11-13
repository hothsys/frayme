"""Directory scanning: walks a tree and streams results as events."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed

from .config import OTHER_VIDEO_EXTS, VIDEO_EXTS, WORKER_THREADS
from .detect import ScanCancelled, check_video
from .donors import _usable_donor
from .findings import describe_damaged
from .state import donor_pool, scan_state


# ─── Directory scanner ───────────────────────────────────────────────────────

def _check_and_describe(fpath, should_stop):
    """check_video, plus what the rows show about a damaged file. Runs on
    the scan's worker threads, since describing takes about half a second."""
    status, reason, details = check_video(fpath, should_stop)
    if status not in ('ok', 'not_video'):
        try:
            details['findings'] = describe_damaged(fpath, status, details)
        except Exception:
            pass    # the row just shows less
    return status, reason, details


def scan_directory_gen(root_dir, scan_id=None):
    """Generator that scans a directory for video files and yields SSE events.

    Stops when the scan is cancelled or a newer scan (different scan_id) starts.
    """

    def cancelled():
        return (scan_state['cancelled'] or
                (scan_id is not None and scan_state.get('scan_id') != scan_id))

    if not os.path.exists(root_dir):
        yield {'type': 'error', 'message': f'Path does not exist: {root_dir}'}
        return

    if not os.path.isdir(root_dir):
        yield {'type': 'error', 'message': f'Not a directory: {root_dir}'}
        return

    yield {'type': 'counting', 'found': 0, 'dirs': 0, 'current': root_dir}

    # Walking a large drive can take a while; report progress so the UI
    # doesn't look stuck before the first file is checked
    all_files = []
    dirs_walked = 0
    last_report = time.time()
    for dirpath, dirnames, filenames in os.walk(root_dir):
        if cancelled():
            yield {'type': 'cancelled', 'scanned': 0, 'issues': 0}
            return
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d != '__MACOSX']
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in VIDEO_EXTS or ext in OTHER_VIDEO_EXTS:
                all_files.append(os.path.join(dirpath, fname))
        dirs_walked += 1
        if time.time() - last_report >= 0.25:
            last_report = time.time()
            yield {'type': 'counting', 'found': len(all_files),
                   'dirs': dirs_walked, 'current': dirpath}

    total = len(all_files)
    yield {'type': 'total', 'total': total}

    if total == 0:
        yield {'type': 'complete', 'scanned': 0, 'issues': 0, 'elapsed': 0}
        return

    scanned = 0
    issue_count = 0
    start_time = time.time()
    batch_size = WORKER_THREADS * 4

    for batch_start in range(0, total, batch_size):
        if cancelled():
            yield {'type': 'cancelled', 'scanned': scanned, 'issues': issue_count}
            return

        batch = all_files[batch_start:batch_start + batch_size]
        futures = {}

        # Not a `with` block: its exit waits for every in-flight file, which
        # made Cancel appear to hang on large files
        executor = ThreadPoolExecutor(max_workers=WORKER_THREADS)
        try:
            for fpath in batch:
                fut = executor.submit(_check_and_describe, fpath, cancelled)
                futures[fut] = fpath

            for fut in as_completed(futures):
                if cancelled():
                    break

                fpath = futures[fut]

                try:
                    status, reason, details = fut.result()
                except ScanCancelled:
                    break
                except Exception as e:
                    status, reason, details = 'error', str(e), {}
                scanned += 1

                fname = os.path.basename(fpath)
                fdir = os.path.dirname(fpath)
                ext = os.path.splitext(fname)[1].lower()
                fsize = details.get('file_size', 0)
                # Mark the folder as scanned even if it has no healthy files,
                # so _ensure_donor_pool doesn't recheck it during recovery
                donor_pool.setdefault(fdir, [])

                elapsed = time.time() - start_time
                rate = scanned / elapsed if elapsed > 0 else 0
                remaining = (total - scanned) / rate if rate > 0 else 0
                eta_str = ''
                if remaining > 0:
                    m, s = divmod(int(remaining), 60)
                    eta_str = f'{m}m {s}s' if m else f'{s}s'

                file_duration = details.get('duration')

                yield {
                    'type': 'scanning',
                    'file': fname,
                    'dir': fdir,
                    'scanned': scanned,
                    'total': total,
                    'issues': issue_count,
                    'eta': eta_str,
                    'duration': file_duration,
                }

                if status == 'not_video':
                    # Neither an issue nor usable as a recovery donor
                    continue
                if status != 'ok':
                    issue_count += 1
                    event = {
                        'type': 'issue',
                        'filepath': fpath,
                        'filename': fname,
                        'dir': fdir,
                        'ext': ext,
                        'file_size': fsize,
                        'status': status,
                        'reason': reason or '',
                        'details': details,
                        'duration': file_duration,
                    }
                    yield event
                elif _usable_donor(status, details):
                    donor_pool.setdefault(fdir, []).append(fpath)
        finally:
            # In-flight checks stop on their own via the cancelled() hook
            for fut in futures:
                fut.cancel()
            executor.shutdown(wait=False)

        if cancelled():
            yield {'type': 'cancelled', 'scanned': scanned, 'issues': issue_count}
            return

    elapsed = round(time.time() - start_time, 1)
    yield {'type': 'complete', 'scanned': scanned, 'issues': issue_count,
           'elapsed': elapsed}
