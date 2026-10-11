# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A fully local macOS meeting recorder → transcriber → summarizer. A Swift Core Audio Process Tap captures mic + system output, whisper.cpp transcribes locally, and the `claude` CLI writes the summary from a template. Only transcript text goes to Claude.

## Commands

```bash
./setup.sh                      # one-time: build the tap, get whisper-cli (brew or build from source into ./bin), download the model
./verify.sh [--no-capture]      # end-to-end self-check; live capture needs mic/audio permission for the launching terminal
./start.sh                      # build tap if missing, start server, open http://127.0.0.1:8765/  (MT_PORT to change)
tools/coreaudio-tap --list      # input devices
tools/coreaudio-tap --cleanup   # destroy stale "MeetingTap" aggregate devices
```

There are no unit tests; `verify.sh` is the test suite. It feeds `say`-generated speech through `segment()` → `Worker` → whisper → `claude` into a temp dir (`MT_SAVE_DIR`, `MT_NOTES_DIR`) and checks the two notes. Before restarting a running server, check `GET /api/status` (`rec.recording`) so you don't kill someone's recording.

## Architecture

- `tools/coreaudio-tap.swift`: with `--capture`, one aggregate device + one IOProc carry the mic and a system-output tap (single clock domain), mono-mixed and resampled to 16 kHz, raw s16le on stdout.
- `server.py` (stdlib-only, Python 3.9): `segment()` splits that stream into 6 s WAV chunks and updates the level `meter` per 100 ms. `Recorder` owns the tap process. `live_loop()` transcribes each 12 s window while recording (`LIVE_CHUNKS`). `Worker` is a serial queue: merge chunks → `whisper()` (`transcript.json/.txt`) → `summarize()` → `write_notes()`. `recover_orphans()` re-queues sessions with chunks but no `full.wav` on startup.
- Sessions live in `transcripts/<id>/` (`chunks/`, `full.wav`, `transcript.*`, `summary.md`, `summaries/` history, `meta.json` with template + title, `note_path.txt`, `transcript_note_path.txt`). The `<id>` is the start time `YYYY-MM-DD_HHMMSS`.
- Notes: `write_notes()` writes `<YYYY-MM-DD HH.MM> - <title> - Summary.md` and `… - Transcript.md` into the notes folder. The title is the first `# ` line of the summary, so `RULES` must keep demanding it. Re-summarizing rewrites both files and renames them if the title changed; it never overwrites a file it didn't create.
- Templates: each `*.md` in the templates folder (default `./templates`) is a prompt with optional `name:` / `description:` front matter. `list_templates()` rescans on every call, which is how new files are picked up with no restart. The prompt sent to `claude -p` is `template body + RULES + meeting context + "[m:ss] text" transcript`.
- Settings: `settings.json` (notes_dir, templates_dir, default_template; env `MT_NOTES_DIR` / `MT_TEMPLATES_DIR` win). `save_settings()` validates and rolls back a bad default template.
- `index.html`: single-page vanilla JS, no build step. Polls `/api/status`, `/api/sessions`, `/api/templates` every 1.5 s and `/api/level` every 150 ms while recording. `renderView()` re-renders only when its `key` changes, so add any new displayed field to that key. The live transcript shows newest first; the finished one is chronological. `md()` is a small markdown renderer (headings, lists, tables, quotes).
- External tools are looked up in `./bin`, Homebrew, `~/.local/bin`. See `STACK.md` for the API table and all env vars.

## Gotchas

- Mic / Audio Recording permission belongs to the app that launched the server. Started from a non-GUI parent, the tap blocks at `AudioDeviceStart` and never prints `capturing` (the UI meter shows "No signal"); run `./start.sh` from Terminal.
- Swift toolchain must match the SDK ("this SDK is not supported by the compiler" → install matching Command Line Tools).
- Whisper has no diarization; speaker counts in summaries are LLM estimates from the dialogue.
- Not committed (see `.gitignore`): `models/`, `bin/`, `tools/coreaudio-tap`, `transcripts/`, `notes/`, `settings.json`. They hold binaries or private recordings.
