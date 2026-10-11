#!/bin/bash
# Build the tap helper if missing, start the server, open the UI.
cd "$(dirname "$0")" || exit 1
export MT_PORT="${MT_PORT:-8765}"
if [ ! -x tools/coreaudio-tap ]; then
  swiftc -O tools/coreaudio-tap.swift -o tools/coreaudio-tap \
    -framework CoreAudio -framework AudioToolbox -framework Foundation || exit 1
fi
tools/coreaudio-tap --cleanup >/dev/null 2>&1
[ -z "$MT_NO_OPEN" ] && (sleep 1; open "http://127.0.0.1:$MT_PORT/") &
exec python3 server.py
