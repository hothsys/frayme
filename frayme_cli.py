#!/usr/bin/env python3
"""
Frayme command line: recover every damaged video in a folder, no browser needed.

    python3 frayme_cli.py --input ~/damaged --output ~/recovered [--donors ~/healthy]
    python3 frayme_cli.py --input ~/damaged --scan-only

Scans --input the way the app does, recovers every file the app would offer to
recover, and writes the results under --output, mirroring the input's subfolders.
--donors is a folder of healthy videos from the same camera, like Choose Donor in
the app. Originals are never changed. Exits 0 when nothing failed, 1 when a
recovery failed, 2 when a folder could not be used.
"""

import argparse
import os
import sys

from backend.batch import run


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Recover every damaged video in a folder.",
        epilog="Exit status: 0 nothing failed, 1 a recovery failed, 2 a folder could not be used.",
    )
    ap.add_argument("--input", required=True, help="folder to scan, including subfolders")
    ap.add_argument(
        "--output",
        help="folder for recovered videos, mirroring the input's subfolders (required unless --scan-only)",
    )
    ap.add_argument("--donors", help="folder of healthy videos from the same camera")
    ap.add_argument(
        "--scan-only", action="store_true", help="list what would be recovered, change nothing"
    )
    args = ap.parse_args(argv)
    if not args.scan_only and not args.output:
        ap.error("--output is required unless --scan-only is given")

    def path(p):
        return os.path.abspath(os.path.expanduser(p)) if p else None

    code, _ = run(path(args.input), path(args.output), path(args.donors), args.scan_only)
    return code


if __name__ == "__main__":
    sys.exit(main())
