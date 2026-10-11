#!/usr/bin/env python3
"""meeting-agent: record (Core Audio tap) -> transcribe (whisper.cpp) -> summarize (claude).
Python 3.9+ stdlib only."""
import array, glob, json, os, re, queue, shutil, signal, subprocess, tempfile, threading, time, wave
from datetime import datetime
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("MT_PORT", "8765"))
SAVE_DIR = os.path.expanduser(os.environ.get("MT_SAVE_DIR", os.path.join(ROOT, "transcripts")))
MIC_NAME = os.environ.get("MT_MIC_NAME", "")  # substring of input device name; empty = default
MODEL = os.environ.get("MT_MODEL", os.path.join(ROOT, "models", "ggml-large-v3-turbo-q5_0.bin"))
CLAUDE_MODEL = os.environ.get("MT_CLAUDE_SUMMARY_MODEL", "sonnet")
CHUNK_SECONDS, RATE, LIVE_CHUNKS = 6, 16000, 2  # live window = LIVE_CHUNKS * CHUNK_SECONDS
SEARCH_PATH = os.pathsep.join([os.path.join(ROOT, "bin"), "/opt/homebrew/bin", "/usr/local/bin",
                               os.path.expanduser("~/.local/bin"), os.environ.get("PATH", "")])
RULES = ("\n\n---\nFormat rules: the FIRST line of your reply must be '# <specific title, max 8 words>'. "
         "Use markdown. Write everything, including the title, in the same language as the transcript "
         "(English transcript -> English). Use only facts from the transcript; write 'Not stated' "
         "instead of guessing.\n\nThe meeting context and transcript follow.\n\n")
SETTINGS_FILE = os.path.join(ROOT, "settings.json")
DEFAULT_NOTES_DIR = os.path.join(ROOT, "notes")
DEFAULT_TEMPLATES_DIR = os.path.join(ROOT, "templates")
FALLBACK_TEMPLATE = "meeting-summary"
TAP = os.path.join(ROOT, "tools", "coreaudio-tap")


def find(name, env=None):
    return os.environ.get(env) if env and os.environ.get(env) else shutil.which(name, path=SEARCH_PATH)


def deps():
    return {"tap": TAP if os.access(TAP, os.X_OK) else None, "whisper-cli": find("whisper-cli"),
            "claude": find("claude", "MT_CLAUDE_BIN"), "model": MODEL if os.path.exists(MODEL) else None}


SID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
MAX_BODY = 1 << 20  # 1 MiB; API bodies are tiny JSON


def valid_sid(sid):
    return isinstance(sid, str) and bool(SID_RE.match(sid))


def inside(path, folder):
    """True if `path` really lives inside `folder` (after resolving symlinks and '..')."""
    try:
        path, folder = os.path.realpath(path), os.path.realpath(folder)
        return os.path.commonpath([path, folder]) == folder
    except ValueError:
        return False


def sid_time(sid):
    try:
        return datetime.strptime(sid, "%Y-%m-%d_%H%M%S")
    except ValueError:
        return datetime.now()


def load_settings():
    try:
        s = json.load(open(SETTINGS_FILE))
    except (OSError, ValueError):
        s = {}
    return dict(notes_dir=os.path.expanduser(os.environ.get("MT_NOTES_DIR") or s.get("notes_dir") or DEFAULT_NOTES_DIR),
                templates_dir=os.path.expanduser(os.environ.get("MT_TEMPLATES_DIR") or s.get("templates_dir") or DEFAULT_TEMPLATES_DIR),
                default_template=s.get("default_template") or FALLBACK_TEMPLATE)


def check_dir(path):
    path = os.path.abspath(os.path.expanduser(path.strip()))
    probe = os.path.join(path, ".mt_write_test")
    try:
        os.makedirs(path, exist_ok=True)
        open(probe, "w").close(); os.remove(probe)
    except OSError as e:
        raise RuntimeError("folder is not writable: %s" % e)
    return path


def save_settings(body):
    try:
        cur = json.load(open(SETTINGS_FILE))
    except (OSError, ValueError):
        cur = {}
    if "notes_dir" in body:
        cur["notes_dir"] = check_dir(body["notes_dir"] or DEFAULT_NOTES_DIR)
    if "templates_dir" in body:
        d = check_dir(body["templates_dir"] or DEFAULT_TEMPLATES_DIR)
        if not glob.glob(os.path.join(d, "*.md")) and d != DEFAULT_TEMPLATES_DIR:  # seed an empty folder with the built-ins
            for f in glob.glob(os.path.join(DEFAULT_TEMPLATES_DIR, "*.md")):
                shutil.copy(f, d)
        cur["templates_dir"] = d
    if body.get("default_template"):
        cur["default_template"] = body["default_template"]
    old = open(SETTINGS_FILE).read() if os.path.exists(SETTINGS_FILE) else None
    json.dump(cur, open(SETTINGS_FILE, "w"), indent=2)
    st = load_settings()
    if st["default_template"] not in [t["id"] for t in list_templates()]:  # roll back instead of persisting a bad choice
        if old is None:
            os.remove(SETTINGS_FILE)
        else:
            open(SETTINGS_FILE, "w").write(old)
        raise RuntimeError("default template not found in the templates folder: " + cur["default_template"])
    return st


def pick_folder(prompt="Choose a folder"):
    script = 'on run argv\nPOSIX path of (choose folder with prompt (item 1 of argv))\nend run'
    r = subprocess.run(["osascript", "-e", script, str(prompt)[:120]], capture_output=True, text=True, timeout=300)
    return r.stdout.strip().rstrip("/") if r.returncode == 0 else None


def parse_template(path):
    """A template is a .md file; optional '---' front matter gives name/description, the rest is the prompt."""
    text, meta = open(path, encoding="utf-8").read(), {}
    m = re.match(r"---\s*\n(.*?)\n---\s*\n?(.*)", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            meta[k.strip().lower()] = v.strip()
        text = m.group(2)
    tid = os.path.splitext(os.path.basename(path))[0]
    return dict(id=tid, name=meta.get("name") or tid.replace("-", " ").replace("_", " ").title(),
                description=meta.get("description", ""), body=text.strip())


def list_templates():
    """Scanned on every call, so templates dropped into the folder show up without a restart."""
    out = []
    for f in sorted(glob.glob(os.path.join(load_settings()["templates_dir"], "*.md"))):
        if os.path.basename(f).startswith((".", "_")) or os.path.basename(f).lower() == "readme.md":
            continue
        try:
            t = parse_template(f)
        except (OSError, UnicodeDecodeError):
            continue
        if t["body"]:
            out.append(t)
    dflt = load_settings()["default_template"]
    return sorted(out, key=lambda t: (t["id"] != dflt, t["name"].lower()))


def get_template(tid):
    ts = {t["id"]: t for t in list_templates()}
    for k in (tid, load_settings()["default_template"], FALLBACK_TEMPLATE):
        if k in ts:
            return ts[k]
    if ts:
        return next(iter(ts.values()))
    raise RuntimeError("no templates found in " + load_settings()["templates_dir"])


def summary_title(summary):
    for line in summary.splitlines():
        m = re.match(r"#\s+(.+)", line.strip())
        if m:
            return m.group(1).strip().strip("*# ")
    return ""


def note_base(sid, title):
    """'2026-10-11 15.00 - Roadmap Review' (date, time, summary title); files add ' - Summary' / ' - Transcript'."""
    when = sid_time(sid).strftime("%Y-%m-%d %H.%M")
    clean = re.sub(r"\s*[/\\:]\s*", " - ", title)
    clean = re.sub(r'[*?"<>|\x00-\x1f]', "", clean).strip(" .-")[:80]
    return "%s - %s" % (when, clean or "Meeting")


def fmt_clock(t):
    t = int(t)
    return "%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60) if t >= 3600 else "%d:%02d" % (t // 60, t % 60)


def note_paths(sdir):
    def rd(n):
        f = os.path.join(sdir, n)
        return open(f).read() if os.path.exists(f) else None
    return rd("note_path.txt"), rd("transcript_note_path.txt")


def write_notes(sid, sdir, title, summary, segs, duration, template_name=""):
    """Write '<base> - Summary.md' (if summary) and '<base> - Transcript.md' into the notes folder.
    Replaces this session's previous notes (renaming them when the title changed)."""
    old_s, old_t = note_paths(sdir)
    folder = load_settings()["notes_dir"]
    os.makedirs(folder, exist_ok=True)
    mine = {x for x in (old_s, old_t) if x}
    base, n = note_base(sid, title), 2
    paths = lambda b: (os.path.join(folder, b + " - Summary.md"), os.path.join(folder, b + " - Transcript.md"))
    while any(os.path.exists(x) and x not in mine for x in paths(base)):  # never overwrite someone else's note
        base, n = note_base(sid, title) + " (%d)" % n, n + 1
    sp, tp = paths(base)
    when = sid_time(sid).strftime("%Y-%m-%d %H:%M")
    info = "%s · %s" % (when, fmt_clock(duration))
    link = lambda label, path: "%s: [%s](<%s>)" % (label, os.path.basename(path)[:-3], os.path.basename(path))
    if summary:
        sl = summary.splitlines()
        for i, l in enumerate(sl):  # the title becomes the note heading; drop its first occurrence
            if re.match(r"#\s+", l.strip()):
                del sl[i]; break
        head = ["# " + title, "", "*%s%s*" % (info, " · " + template_name if template_name else ""), "", link("Transcript", tp), ""]
        open(sp, "w").write("\n".join(head + ["\n".join(sl).strip(), ""]))
    thead = ["# %s (Transcript)" % title, "", "*%s*" % info, ""] + ([link("Summary", sp), ""] if summary else [])
    open(tp, "w").write("\n".join(thead + ["[%s] %s" % (fmt_clock(g["start"]), g["text"]) for g in segs]) + "\n")
    open(os.path.join(sdir, "transcript_note_path.txt"), "w").write(tp)
    if summary:
        open(os.path.join(sdir, "note_path.txt"), "w").write(sp)
    elif os.path.exists(os.path.join(sdir, "note_path.txt")):
        os.remove(os.path.join(sdir, "note_path.txt"))
    for old in mine - {sp, tp}:  # title changed (or old combined note): the notes move to their new names
        if os.path.exists(old):
            os.remove(old)
    return (sp if summary else None), tp


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode:
        raise RuntimeError("%s failed: %s" % (os.path.basename(cmd[0]), p.stderr.strip()[-400:]))
    return p.stdout


def write_wav(path, frames):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(frames)


def segment(stream, outdir, meter=None):
    """Split the tap's raw s16le mono PCM stream into CHUNK_SECONDS WAV files.
    Reads in 100 ms blocks so `meter` (a dict) tracks the live input level."""
    block, size, i, buf = RATE // 10 * 2, CHUNK_SECONDS * RATE * 2, 0, bytearray()
    while True:
        data = stream.read(block)
        if not data:
            break
        if meter is not None:
            a = array.array("h"); a.frombytes(data[:len(data) // 2 * 2])
            meter.update(rms=(sum(x * x for x in a) / max(len(a), 1)) ** 0.5 / 32768,
                         peak=max(map(abs, a), default=0) / 32768, at=time.time())
        buf += data
        if len(buf) >= size:
            write_wav(os.path.join(outdir, "chunk_%05d.wav" % i), bytes(buf)); buf.clear(); i += 1
    if buf:
        write_wav(os.path.join(outdir, "chunk_%05d.wav" % i), bytes(buf))


def whisper(wav, base, d):
    """Run whisper-cli; returns [{start, end, text}] in seconds and leaves base.txt/base.json."""
    run([d["whisper-cli"], "-m", d["model"], "-f", wav, "-l", "auto", "-otxt", "-oj", "-of", base, "-np"])
    return read_segments(base + ".json")


def read_segments(path):
    out = []
    for t in json.load(open(path)).get("transcription", []):
        text = t["text"].strip()
        if text and not (text[0] in "[(" and text[-1] in "])"):  # drop [BLANK_AUDIO], (music) ...
            out.append(dict(start=t["offsets"]["from"] / 1000, end=t["offsets"]["to"] / 1000, text=text))
    return out


def live_loop(s):
    """While recording, transcribe each new LIVE_CHUNKS-chunk window into s['live']."""
    d, nxt, tmp = deps(), 0, os.path.join(s["dir"], "live_tmp")
    while not s["stop"].wait(1):
        files = sorted(glob.glob(os.path.join(s["dir"], "chunks", "*.wav")))
        if len(files) - nxt < LIVE_CHUNKS:
            continue
        group = files[nxt:nxt + LIVE_CHUNKS]
        try:
            write_wav(tmp + ".wav", b"".join(wave.open(c, "rb").readframes(10**9) for c in group))
            off = nxt * CHUNK_SECONDS
            for seg in whisper(tmp + ".wav", tmp, d):
                s["live"].append(dict(seg, start=seg["start"] + off, end=seg["end"] + off))
            json.dump(s["live"], open(os.path.join(s["dir"], "live.json"), "w"))
        except Exception as e:
            s["live_error"] = str(e)
        nxt += LIVE_CHUNKS


class Recorder:
    def __init__(self):
        self.lock, self.tap, self.reader, self.session = threading.Lock(), None, None, None
        self.meter = {}

    def active(self):
        return self.tap is not None and self.tap.poll() is None

    def start(self, live=False, template=None):
        with self.lock:
            if self.active():
                raise RuntimeError("already recording")
            if not deps()["tap"]:
                raise RuntimeError("tools/coreaudio-tap not built (run ./start.sh)")
            sid = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            sdir = os.path.join(SAVE_DIR, sid)
            os.makedirs(os.path.join(sdir, "chunks"))
            self.meter = {}
            cmd = [TAP, "--capture", "--rate", str(RATE)] + (["--mic", MIC_NAME] if MIC_NAME else [])
            self.tap = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=open(os.path.join(sdir, "recorder.log"), "wb"))
            self.reader = threading.Thread(target=segment, args=(self.tap.stdout, os.path.join(sdir, "chunks"), self.meter), daemon=True)
            self.reader.start()
            self.session = dict(id=sid, dir=sdir, started=time.time(), live=[], stop=threading.Event(), is_live=live,
                                template=template or load_settings()["default_template"])
            if live:
                d = deps()
                if not (d["whisper-cli"] and d["model"]):
                    self.session["live_error"] = "whisper-cli/model missing"
                else:
                    threading.Thread(target=live_loop, args=(self.session,), daemon=True).start()
            return sid

    def stop(self):
        with self.lock:
            if not self.session:
                raise RuntimeError("not recording")
            s, self.session = self.session, None
            s["stop"].set()
            if self.active():
                self.tap.send_signal(signal.SIGTERM)
            try:
                self.tap.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.tap.kill()
            self.reader.join(timeout=15)
            self.tap = None
            return s

    def level(self):
        m = self.meter
        age = time.time() - m["at"] if "at" in m else None
        return dict(rms=m.get("rms", 0), peak=m.get("peak", 0), age=age)

    def status(self):
        s = self.session
        if not s:
            return dict(recording=False)
        return dict(recording=self.active(), id=s["id"], elapsed=int(time.time() - s["started"]), live=s["is_live"],
                    chunks=len(glob.glob(os.path.join(s["dir"], "chunks", "*.wav"))))


class Worker:
    """Serial job queue: merge -> transcribe -> summarize."""

    def __init__(self):
        self.q, self.jobs = queue.Queue(), {}
        threading.Thread(target=self.loop, daemon=True).start()

    def submit(self, s):
        self.jobs[s["id"]] = dict(state="queued", detail="")
        self.q.put(s)

    def loop(self):
        while True:
            s = self.q.get()
            try:
                self.process(s)
            except Exception as e:
                self.jobs[s["id"]] = dict(state="error", detail=str(e))

    def process(self, s):
        if s.get("kind") == "resummarize":
            return self.resummarize(s)
        sid, sdir, d = s["id"], s["dir"], deps()
        chunks = sorted(glob.glob(os.path.join(sdir, "chunks", "*.wav")))
        if not chunks:
            raise RuntimeError("no audio captured (check permissions; see recorder.log)")
        for k in ("whisper-cli", "model", "claude"):
            if not d[k]:
                raise RuntimeError("missing: " + k)
        self.jobs[sid] = dict(state="transcribing", detail="")
        frames = b""
        for c in chunks:
            with wave.open(c, "rb") as r:
                frames += r.readframes(r.getnframes())
        write_wav(os.path.join(sdir, "full.wav"), frames)
        whisper(os.path.join(sdir, "full.wav"), os.path.join(sdir, "transcript"), d)
        self.summarize(sid, sdir, s.get("template"), d)
        self.jobs[sid] = dict(state="done", detail="")

    def resummarize(self, s):
        sid, sdir, d = s["id"], s["dir"], deps()
        if not d["claude"]:
            raise RuntimeError("missing: claude")
        if not os.path.exists(os.path.join(sdir, "transcript.txt")):
            raise RuntimeError("no transcript to summarize yet")
        self.summarize(sid, sdir, s.get("template"), d)
        self.jobs[sid] = dict(state="done", detail="")

    def summarize(self, sid, sdir, tid, d):
        """Summarize the transcript with a template, keep older summaries, (re)write both notes."""
        segs = read_segments(os.path.join(sdir, "transcript.json"))
        dur = int(wav_duration(os.path.join(sdir, "full.wav")))
        if not segs:
            return
        if not note_paths(sdir)[1]:  # the transcript note exists even if summarizing fails
            write_notes(sid, sdir, "Meeting", None, segs, dur)
        t = get_template(tid)
        self.jobs[sid] = dict(state="summarizing", detail=t["name"])
        context = "Meeting context: recorded %s, duration %s.\nThe transcript has [m:ss] timestamps." % (
            sid_time(sid).strftime("%A %Y-%m-%d %H:%M"), fmt_clock(dur))
        text = "\n".join("[%s] %s" % (fmt_clock(g["start"]), g["text"]) for g in segs)
        summary = run([d["claude"], "-p", "--model", CLAUDE_MODEL, "--tools", "", "--no-session-persistence"],
                      cwd=tempfile.gettempdir(), input=t["body"] + RULES + context + "\n\n" + text)
        hist = os.path.join(sdir, "summaries")
        os.makedirs(hist, exist_ok=True)
        open(os.path.join(hist, "%s-%s.md" % (datetime.now().strftime("%Y%m%d-%H%M%S"), t["id"])), "w").write(summary)
        open(os.path.join(sdir, "summary.md"), "w").write(summary)
        title = summary_title(summary) or "Meeting"
        json.dump(dict(template=t["id"], name=t["name"], title=title), open(os.path.join(sdir, "meta.json"), "w"))
        self.jobs[sid] = dict(state="saving", detail="")
        write_notes(sid, sdir, title, summary, segs, dur, t["name"])


rec, worker = Recorder(), Worker()


def list_sessions():
    out = []
    for p in sorted(glob.glob(os.path.join(SAVE_DIR, "*", "")), reverse=True):
        sid = os.path.basename(p.rstrip("/"))
        out.append(dict(id=sid, title=session_title(p), transcript=os.path.exists(p + "transcript.txt"),
                        summary=os.path.exists(p + "summary.md"), duration=wav_duration(p + "full.wav"),
                        live=bool(rec.session and rec.session["id"] == sid)))
    return out


def session_title(sdir):
    """Generated meeting title (once summarized): meta.json, else the summary's first heading."""
    try:
        t = json.load(open(os.path.join(sdir, "meta.json"))).get("title")
        if t:
            return t
    except (OSError, ValueError):
        pass
    try:
        return summary_title(open(os.path.join(sdir, "summary.md")).read()) or None
    except OSError:
        return None


def wav_duration(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / w.getframerate()
    except (OSError, EOFError, wave.Error):
        return 0


def session_detail(sid):
    sdir = os.path.join(SAVE_DIR, sid)
    if not valid_sid(sid) or not os.path.isdir(sdir):
        return None
    cur = rec.session if rec.session and rec.session["id"] == sid else None
    final = os.path.exists(os.path.join(sdir, "transcript.json"))
    if final:
        segs = read_segments(os.path.join(sdir, "transcript.json"))
    elif cur:
        segs = list(cur["live"])
    elif os.path.exists(os.path.join(sdir, "live.json")):
        segs = json.load(open(os.path.join(sdir, "live.json")))
    else:
        segs = []
    sp = os.path.join(sdir, "summary.md")
    full = os.path.join(sdir, "full.wav")
    try:
        meta = json.load(open(os.path.join(sdir, "meta.json")))
    except (OSError, ValueError):
        meta = {}
    return dict(id=sid, template=meta.get("template"), template_name=meta.get("name"), segments=segs, final=final, recording=bool(cur),
                live_error=cur.get("live_error") if cur else None, job=worker.jobs.get(sid),
                summary=open(sp).read() if os.path.exists(sp) else None,
                title=session_title(sdir), note=note_paths(sdir)[0], transcript_note=note_paths(sdir)[1],
                audio=os.path.exists(full), duration=wav_duration(full))


def peaks(sid, n=400):
    import array
    path = os.path.join(SAVE_DIR, sid, "full.wav")
    with wave.open(path, "rb") as w:
        a = array.array("h"); a.frombytes(w.readframes(w.getnframes()))
    step = max(1, len(a) // n)
    return [round(max(map(abs, a[i:i + step])) / 32768, 3) for i in range(0, len(a) - step + 1, step)][:n]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def send_audio(self, path):
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a or 0)
            end = min(int(b), size - 1) if b else size - 1
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read(end - start + 1))

    def guard(self, post):
        """The API can start the mic and write files, so only the local UI may call it.
        Host check stops DNS rebinding; Origin check stops other sites; POSTs also need a custom header,
        which browsers only allow cross-site after a CORS preflight that this server never grants."""
        ok_hosts = ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in ok_hosts or (origin and origin not in ("http://" + h for h in ok_hosts)) \
                or (post and self.headers.get("X-MT") != "1"):
            self.send(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if not self.guard(False):
            return
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self.send(200, open(os.path.join(ROOT, "index.html"), "rb").read(), "text/html")
        if p == "/api/status":
            return self.send(200, dict(rec=rec.status(), jobs=worker.jobs, deps=deps()))
        if p == "/api/settings":
            return self.send(200, dict(load_settings(), default=DEFAULT_NOTES_DIR))
        if p == "/api/templates":
            ts = [{k: t[k] for k in ("id", "name", "description")} for t in list_templates()]
            return self.send(200, dict(templates=ts, default=load_settings()["default_template"]))
        if p == "/api/level":
            return self.send(200, rec.level())
        if p == "/api/sessions":
            return self.send(200, list_sessions())
        parts = p.split("/")
        if len(parts) == 4 and parts[:2] == ["", "api"] and parts[2] in ("session", "audio", "peaks") \
                and valid_sid(parts[3]):
            sid = parts[3]
            if parts[2] == "session":
                d = session_detail(sid)
                return self.send(200 if d else 404, d or {"error": "not found"})
            if os.path.exists(os.path.join(SAVE_DIR, sid, "full.wav")):
                if parts[2] == "audio":
                    return self.send_audio(os.path.join(SAVE_DIR, sid, "full.wav"))
                return self.send(200, peaks(sid))
            return self.send(404, {"error": "no audio yet"})
        # /api/file/<sid>/<name>
        if len(parts) == 5 and parts[:3] == ["", "api", "file"] and parts[4] in ("transcript.txt", "summary.md") \
                and valid_sid(parts[3]):
            fp = os.path.join(SAVE_DIR, parts[3], parts[4])
            if os.path.exists(fp):
                return self.send(200, open(fp, "rb").read(), "text/plain")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.guard(True):
            return
        route, _, q = self.path.partition("?")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self.send(413, {"error": "body too large"})
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        try:
            if route == "/api/settings":
                return self.send(200, save_settings(body))
            if route == "/api/pick-folder":
                try:
                    return self.send(200, {"path": pick_folder(body.get("prompt") or "Choose a folder")})
                except subprocess.TimeoutExpired:
                    return self.send(200, {"path": None})
            if route == "/api/reveal":
                path = body.get("path", "")
                if os.path.isfile(path) and inside(path, load_settings()["notes_dir"]):
                    subprocess.run(["open", "-R", path])
                    return self.send(200, {"ok": True})
                return self.send(404, {"error": "not found"})
            if route == "/api/open-folder":
                folder = load_settings().get({"notes": "notes_dir", "templates": "templates_dir"}.get(body.get("which"), ""))
                if folder and os.path.isdir(folder):
                    subprocess.run(["open", folder])
                    return self.send(200, {"ok": True})
                return self.send(404, {"error": "folder not found"})
            if route == "/api/resummarize":
                sid, sdir = body.get("id", ""), os.path.join(SAVE_DIR, body.get("id", ""))
                if not valid_sid(sid) or not os.path.isdir(sdir):
                    return self.send(404, {"error": "session not found"})
                j = worker.jobs.get(sid)
                if (j and j["state"] not in ("done", "error")) or (rec.session and rec.session["id"] == sid):
                    raise RuntimeError("this session is still being processed")
                tid = body.get("template") or ""
                if tid not in [t["id"] for t in list_templates()]:
                    raise RuntimeError("unknown template: " + tid)
                worker.submit(dict(id=sid, dir=sdir, kind="resummarize", template=tid))
                return self.send(200, {"id": sid})
            if route == "/api/start":
                args = parse_qs(q)
                tid = (args.get("template") or [""])[0]
                if tid and tid not in [t["id"] for t in list_templates()]:
                    raise RuntimeError("unknown template: " + tid)
                return self.send(200, {"id": rec.start(live="live=1" in q, template=tid or None)})
            if route == "/api/stop":
                s = rec.stop()
                worker.submit(s)
                return self.send(200, {"id": s["id"]})
        except RuntimeError as e:
            return self.send(409, {"error": str(e)})
        self.send(404, {"error": "not found"})


def recover_orphans():
    """Sessions with audio chunks but no merged full.wav were cut off (crash/restart): process them."""
    for sdir in sorted(glob.glob(os.path.join(SAVE_DIR, "*", ""))):
        sdir = sdir.rstrip("/")
        if glob.glob(os.path.join(sdir, "chunks", "*.wav")) and not os.path.exists(os.path.join(sdir, "full.wav")):
            worker.submit(dict(id=os.path.basename(sdir), dir=sdir, started=0))


if __name__ == "__main__":
    os.makedirs(SAVE_DIR, exist_ok=True)
    recover_orphans()
    print("meeting-agent on http://127.0.0.1:%d/ (save: %s)" % (PORT, SAVE_DIR), flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    finally:
        if rec.session:
            rec.stop()
