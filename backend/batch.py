"""Batch recovery for the command line: scan a folder, recover what the app would offer.

Uses the same scan, donor and recovery code as the web app, without the server.
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import RECOVERY_WORKERS, VIDEO_EXTS
from .decode import _check_decodes_cleanly
from .detect import check_video
from .donors import _usable_donor
from .recovery import _execute_recovery
from .scan import scan_directory_gen
from .state import donor_lock, donor_pool, scan_state


def can_recover(status, details):
    """Whether the app offers Recover for a scan result.

    Mirrors canRecover in js/rows.js. tests/test_batch.py compares the two, so a
    change to one fails until the other follows.
    """
    details = details or {}
    return (status in ("no_moov", "corrupt") and not details.get("unrecoverable")) or bool(
        details.get("salvageable")
    )


def find_donors(donor_dir):
    """Healthy MP4/MOV files with a moov under donor_dir, as donor candidates."""
    found = []
    for dirpath, dirnames, filenames in os.walk(donor_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__MACOSX"]
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
    return found


def add_donors(donors, folders):
    """Make donors candidates for every folder in folders (like Choose Donor in the UI)."""
    with donor_lock:
        for folder in folders:
            pool = donor_pool.setdefault(folder, [])
            pool[:0] = [f for f in donors if f not in pool]


def scan(input_dir):
    """Scan input_dir like the app does. Returns (issue events, error message or None)."""
    scan_state["cancelled"] = False
    scan_state["results"] = []
    with donor_lock:
        donor_pool.clear()
    issues = []
    for event in scan_directory_gen(input_dir):
        if event["type"] == "issue":
            issues.append(event)
            scan_state["results"].append(event)
        elif event["type"] == "error":
            return [], event["message"]
    return sorted(issues, key=lambda e: e["filepath"]), None


def output_dir_for(filepath, input_dir, output_dir):
    """Where a recovered file goes: the output folder, mirroring the input's subfolders."""
    rel = os.path.relpath(os.path.dirname(filepath), input_dir)
    return output_dir if rel == "." else os.path.join(output_dir, rel)


def recover_one(event, input_dir, output_dir):
    """Recover one scanned file. Returns a result dict; never raises."""
    result = {"filepath": event["filepath"], "status": event["status"]}
    out_dir = output_dir_for(event["filepath"], input_dir, output_dir)
    try:
        os.makedirs(out_dir, exist_ok=True)
        r = _execute_recovery(event["filepath"], output_dir=out_dir)
        ok, msg = _check_decodes_cleanly(r["output"])
        result.update(
            outcome="recovered" if ok else "bad output",
            method=r["method"],
            output=r["output"],
            check=msg,
        )
    except Exception as e:
        result.update(outcome="failed", error=str(e)[:200])
    return result


def run(input_dir, output_dir, donor_dir=None, scan_only=False, report=print):
    """Scan, then recover. Returns (exit code, results).

    Exit code 0: nothing failed. 1: a recovery failed or its output did not decode.
    2: the input, donor or output folder could not be used.
    """
    if not os.path.isdir(input_dir):
        report(f"Input folder not found: {input_dir}")
        return 2, []
    if donor_dir and not os.path.isdir(donor_dir):
        report(f"Donor folder not found: {donor_dir}")
        return 2, []

    issues, error = scan(input_dir)
    if error:
        report(error)
        return 2, []

    todo, skipped = [], []
    for e in issues:
        (todo if can_recover(e["status"], e["details"]) else skipped).append(e)
    report(f"Found {len(issues)} damaged files, {len(todo)} can be recovered.")
    for e in skipped:
        report(f"  skip     {os.path.relpath(e['filepath'], input_dir)} ({e['status']})")

    if scan_only:
        for e in todo:
            report(f"  would recover  {os.path.relpath(e['filepath'], input_dir)} ({e['status']})")
        return 0, []

    if not todo:
        return 0, []

    if output_dir:
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as e:
            report(f"Output folder cannot be created: {e}")
            return 2, []
        if not os.access(output_dir, os.W_OK):
            report(f"Output folder is not writable: {output_dir}")
            return 2, []

    if donor_dir:
        donors = find_donors(donor_dir)
        add_donors(donors, {e["dir"] for e in todo})
        report(f"Using {len(donors)} healthy donors from {donor_dir}")

    results = []
    with ThreadPoolExecutor(max_workers=RECOVERY_WORKERS) as pool:
        futures = [pool.submit(recover_one, e, input_dir, output_dir) for e in todo]
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            name = os.path.relpath(r["filepath"], input_dir)
            if r["outcome"] == "recovered":
                report(f"  recovered  {name} -> {r['output']} ({r['method']})")
            else:
                report(f"  {r['outcome']}  {name}: {r.get('error') or r.get('check', '')}")

    counts = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    report(", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    return (0 if counts.get("recovered", 0) == len(results) else 1), results
