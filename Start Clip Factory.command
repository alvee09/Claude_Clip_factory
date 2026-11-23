#!/bin/bash
# Double-click this in Finder to start Clip Factory.
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null && [ ! -x /usr/local/bin/ffmpeg ] && [ ! -x /opt/homebrew/bin/ffmpeg ]; then
  echo "ffmpeg is missing. Install it with:  brew install ffmpeg"; read -r; exit 1
fi
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
(sleep 2 && open http://127.0.0.1:5055) &
exec .venv/bin/python -m clipfactory
