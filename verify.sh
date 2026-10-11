#!/bin/bash
# Verify the stack on this machine. Usage: ./verify.sh [--no-capture]
cd "$(dirname "$0")" || exit 1
PASS=0; FAIL=0; WARN=0
ok(){ echo "  ✓ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ✗ $1"; FAIL=$((FAIL+1)); }
warn(){ echo "  ! $1"; WARN=$((WARN+1)); }
export PATH="$PWD/bin:$HOME/.local/bin:/opt/homebrew/bin:$PATH"
T=$(mktemp -d); trap 'pkill -f "[c]oreaudio-tap --capture" 2>/dev/null; kill $SRV 2>/dev/null; rm -rf "$T"' EXIT

echo "1. Prerequisites"
[ "$(sw_vers -productVersion | cut -d. -f1)" -ge 15 ] || [ "$(sw_vers -productVersion | cut -d. -f1-2)" \> "14.3" ] && ok "macOS $(sw_vers -productVersion) (tap needs 14.4+)" || bad "macOS too old"
python3 -c 'import sys;sys.exit(sys.version_info<(3,9))' && ok "python $(python3 -V | cut -d' ' -f2)" || bad "python < 3.9"
command -v whisper-cli >/dev/null && ok "whisper-cli" || bad "whisper-cli missing (see bin/)"
command -v claude >/dev/null && ok "claude CLI" || bad "claude CLI missing"
ls models/*.bin >/dev/null 2>&1 && ok "model $(ls models/*.bin | head -1)" || bad "no whisper model in models/"

echo "2. Swift tap builds"
swiftc -O tools/coreaudio-tap.swift -o tools/coreaudio-tap -framework CoreAudio -framework AudioToolbox -framework Foundation 2>"$T/sw.err" \
  && ok "swiftc build" || { bad "swiftc build: $(tail -2 "$T/sw.err")"; }
tools/coreaudio-tap --list >/dev/null 2>&1 && ok "tap --list" || bad "tap --list"

echo "3. Pipeline (synthetic speech -> chunker -> whisper -> claude)"
say -o "$T/a.aiff" "The meeting starts now. Carol will send the invoice on Tuesday. We decided to hire two engineers." \
  && afconvert -f WAVE -d LEI16@16000 -c 1 "$T/a.aiff" "$T/a.wav" && ok "generated test audio" || bad "say/afconvert"
python3 - "$T" <<'PY' && ok "transcript + summary + matching Summary/Transcript notes produced" || bad "pipeline failed (see above)"
import glob, io, os, sys, time, wave
t = sys.argv[1]; os.environ["MT_SAVE_DIR"] = t; os.environ["MT_NOTES_DIR"] = t + "/notes"
sys.path.insert(0, ".")
import server
os.makedirs(t + "/s/chunks")
server.segment(io.BytesIO(wave.open(t + "/a.wav").readframes(10**9)), t + "/s/chunks")
server.worker.submit(dict(id="s", dir=t + "/s", started=0))
while server.worker.jobs["s"]["state"] not in ("done", "error"): time.sleep(0.5)
j = server.worker.jobs["s"]
if j["state"] != "done": print(j); sys.exit(1)
tx = open(t + "/s/transcript.txt").read().lower()
print("   transcript:", tx.strip().replace("\n", " ")[:100])
notes = glob.glob(t + "/notes/*.md")
print("   notes:", [os.path.basename(n) for n in sorted(notes)])
sys.exit(0 if "carol" in tx and os.path.getsize(t + "/s/summary.md") > 50 and len(notes) == 2 and any(n.endswith(" - Summary.md") for n in notes) and any(n.endswith(" - Transcript.md") for n in notes) else 1)
PY

echo "4. Server API"
MT_PORT=8799 MT_SAVE_DIR="$T/srv" python3 server.py >"$T/srv.log" 2>&1 & SRV=$!
sleep 1.5
curl -sf localhost:8799/ | grep -q "Meeting Recorder" && ok "GET / serves UI" || bad "GET /"
curl -sf localhost:8799/api/status | python3 -c 'import sys,json;d=json.load(sys.stdin)["deps"];sys.exit(0 if all(d.values()) else 1)' \
  && ok "/api/status deps all present" || bad "/api/status reports missing deps"
[ "$(curl -s -o /dev/null -w %{http_code} localhost:8799/api/file/../../etc/passwd)" = 404 ] && ok "path traversal blocked" || bad "path traversal"

if [ "$1" != "--no-capture" ]; then
  echo "5. Live capture (mic + system audio, 6s)"
  tools/coreaudio-tap --capture >"$T/cap.pcm" 2>"$T/cap.err" & TP=$!
  for i in 1 2 3 4 5 6; do sleep 1; grep -q capturing "$T/cap.err" && break; done
  if grep -q capturing "$T/cap.err"; then
    ok "tap started"
    afplay "$T/a.aiff" & sleep 4; kill $TP 2>/dev/null; wait $TP 2>/dev/null
    PEAK=$(python3 -c 'import array;a=array.array("h");a.frombytes(open("'"$T"'/cap.pcm","rb").read());print(len(a)//16000, max(map(abs,a)) if a else 0)')
    set -- $PEAK
    [ "$1" -ge 2 ] && ok "captured ${1}s of audio" || bad "captured only ${1}s"
    [ "$2" -gt 500 ] && ok "signal present (peak $2) - system audio tap works" || warn "silent capture (peak $2): grant Audio Recording permission"
  else
    kill -9 $TP 2>/dev/null
    bad "tap blocked before capturing - grant Microphone + Audio Recording to your terminal, then rerun. $(tail -1 "$T/cap.err")"
  fi
  tools/coreaudio-tap --cleanup >/dev/null 2>&1
fi
echo; echo "passed=$PASS failed=$FAIL warnings=$WARN"; [ $FAIL -eq 0 ]
