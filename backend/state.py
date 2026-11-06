"""Shared state: scan results, donor folders, and the donor pool."""

import os


# The project folder (this file lives in backend/), where the web app's files are
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─── Global state ─────────────────────────────────────────────────────────────

scan_state = {
    'running': False,
    'cancelled': False,
    'results': [],
    'scan_id': 0,
}


# Per-directory donor map: { dir_path: filepath } (manual override)
donor_map = {}


# Auto-collected healthy files: { dir_path: [filepath, ...] }
donor_pool = {}
