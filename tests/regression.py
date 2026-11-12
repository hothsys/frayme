#!/usr/bin/env python3
"""Regression run over a folder of real damaged videos.

The synthetic tests (python3 -m unittest discover tests) can't reproduce
everything real cameras and carving tools do, so engine changes should also
be checked against real files. Those stay on your disk; nothing here is
checked in. Usage:

    python3 tests/regression.py INPUT_DIR [--donors DONOR_DIR]
                                [--save results.json] [--compare results.json]

INPUT_DIR is scanned the way the app scans it. Every file the app would
offer to recover is recovered into a temporary folder (deleted afterwards)
and the result is decode-tested. DONOR_DIR, if given, is added as a donor
folder, like the Choose Donor button. --save writes the outcomes to a JSON
file; --compare reports every file whose outcome changed since a saved run,
and exits with status 1 if any recovery got worse.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from backend import state                                    # noqa: E402
from backend.config import VIDEO_EXTS                        # noqa: E402
from backend.decode import _check_decodes_cleanly            # noqa: E402
from backend.detect import check_video                       # noqa: E402
from backend.donors import _usable_donor                     # noqa: E402
from backend.recovery import _execute_recovery               # noqa: E402
from backend.scan import scan_directory_gen                  # noqa: E402
from tests.fixtures import ui_offers_recover                 # noqa: E402


def count_frames(path):
    import subprocess
    from backend.tools import _ffprobe_path
    r = subprocess.run([_ffprobe_path, '-v', 'error', '-select_streams', 'v:0',
                        '-count_packets', '-show_entries', 'stream=nb_read_packets',
                        '-of', 'csv=p=0', path], capture_output=True, text=True)
    digits = ''.join(c for c in r.stdout if c.isdigit())
    return int(digits) if digits else 0


def add_donor_folder(donor_dir, target_dir):
    """Make donor_dir's healthy videos donor candidates for target_dir."""
    found = []
    for dirpath, dirnames, filenames in os.walk(donor_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d != '__MACOSX']
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
                continue
            fpath = os.path.join(dirpath, name)
            try:
                status, _, details = check_video(fpath)
            except Exception:
                continue
            if _usable_donor(status, details):
                found.append(fpath)
    pool = state.donor_pool.setdefault(target_dir, [])
    pool[:0] = [f for f in found if f not in pool]
    return len(found)


def run(input_dir, donor_dir=None):
    issues = []
    state.scan_state['results'] = []
    for event in scan_directory_gen(input_dir):
        if event['type'] == 'issue':
            issues.append(event)
            state.scan_state['results'].append(event)
        elif event['type'] == 'error':
            sys.exit(event['message'])

    if donor_dir:
        found = 0
        for d in sorted({e['dir'] for e in issues}):
            found = add_donor_folder(donor_dir, d)
        print(f'Using {found} healthy donors from {donor_dir}')

    out_dir = tempfile.mkdtemp(prefix='frayme-regression-')
    results = {}
    try:
        for ev in sorted(issues, key=lambda e: e['filepath']):
            rel = os.path.relpath(ev['filepath'], input_dir)
            entry = {'status': ev['status']}
            if not ui_offers_recover(ev['status'], ev['details']):
                entry['outcome'] = 'not offered'
                results[rel] = entry
                print(f'{rel:44} {ev["status"]:10} not offered')
                continue
            t = time.time()
            try:
                r = _execute_recovery(ev['filepath'], output_dir=out_dir)
                ok, msg = _check_decodes_cleanly(r['output'])
                entry.update(outcome='recovered' if ok else 'bad output',
                             method=r['method'], frames=count_frames(r['output']),
                             check=msg)
                os.unlink(r['output'])
            except Exception as e:
                entry.update(outcome='failed', error=str(e)[:200])
            entry['seconds'] = round(time.time() - t, 1)
            results[rel] = entry
            detail = (f'{entry.get("method", "")} {entry.get("frames", "")} frames'
                      if entry['outcome'] != 'failed' else entry['error'][:70])
            print(f'{rel:44} {ev["status"]:10} {entry["outcome"]:10} '
                  f'{detail} ({entry["seconds"]}s)')
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return results


def compare(old, new):
    """Lines describing changes, and whether anything got worse."""
    rank = {'recovered': 3, 'bad output': 1, 'failed': 0, 'not offered': 0}
    lines, worse = [], False
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        if a is None or b is None:
            lines.append(f'{name}: {"new file" if a is None else "no longer found"}')
            continue
        changes = []
        for key in ('status', 'outcome', 'method'):
            if a.get(key) != b.get(key):
                changes.append(f'{key} {a.get(key)} -> {b.get(key)}')
        fa, fb = a.get('frames') or 0, b.get('frames') or 0
        if fa != fb:
            changes.append(f'frames {fa} -> {fb}')
        if changes:
            lines.append(f'{name}: ' + ', '.join(changes))
        if (rank.get(b.get('outcome'), 0) < rank.get(a.get('outcome'), 0)
                or (a.get('outcome') == b.get('outcome') == 'recovered' and fb < fa)):
            worse = True
    return lines, worse


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('input_dir')
    ap.add_argument('--donors', help='folder of healthy videos to use as donors')
    ap.add_argument('--save', help='write outcomes to this JSON file')
    ap.add_argument('--compare', help='compare with outcomes saved earlier')
    args = ap.parse_args()

    input_dir = os.path.abspath(os.path.expanduser(args.input_dir))
    if not os.path.isdir(input_dir):
        print(f'{input_dir} not found; nothing to check.')
        return 0
    donors = os.path.abspath(os.path.expanduser(args.donors)) if args.donors else None

    results = run(input_dir, donors)
    counts = {}
    for entry in results.values():
        counts[entry['outcome']] = counts.get(entry['outcome'], 0) + 1
    print('\n' + ', '.join(f'{v} {k}' for k, v in sorted(counts.items())))

    if args.save:
        with open(args.save, 'w') as f:
            json.dump(results, f, indent=1, sort_keys=True)
        print(f'Saved to {args.save}')
    if args.compare:
        with open(args.compare) as f:
            old = json.load(f)
        lines, worse = compare(old, results)
        print(f'\nChanges since {args.compare}:' if lines else '\nNo changes.')
        for line in lines:
            print('  ' + line)
        if worse:
            print('Some recoveries got worse.')
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
