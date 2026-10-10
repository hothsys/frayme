#!/usr/bin/env bash
# Double-click in Finder to start Frayme. Press Return (or close the window) to stop it.

cd "$(dirname "$0")" || exit 1

# Finder launches may not load your shell profile; make sure Homebrew's ffmpeg is found
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

./frayme.sh start || { echo; read -r -p "Press Return to close."; exit 1; }

trap './frayme.sh stop; exit' HUP INT TERM

echo
read -r -p "Frayme is running. Press Return to stop it. "
./frayme.sh stop
