#!/usr/bin/env python3
"""
Frayme
Recursively scans directories for corrupt or damaged MP4/MOV video files.
Detects: missing moov atoms, leading-zero corruption, truncated files,
and files with no valid container structure.
Offers best-effort recovery via moov transplant and raw stream extraction.

Entry point. The application itself lives in backend/.
"""

import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer

from backend.server import FrameHandler


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5901
    print()
    print('╔══════════════════════════════════════════════╗')
    print('║              Frayme                           ║')
    print(f'║  Open http://localhost:{port} in your browser  ║')
    print('╚══════════════════════════════════════════════╝')
    print()
    threading.Timer(1.2, lambda: webbrowser.open(f'http://localhost:{port}')).start()
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(('127.0.0.1', port), FrameHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
        server.server_close()
