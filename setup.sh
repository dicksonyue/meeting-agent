#!/bin/bash
# One-time setup: build the Core Audio tap, get whisper-cli, download the Whisper model.
# Idempotent; re-run any time. No Homebrew or sudo required (uses it only if already installed).
set -e
cd "$(dirname "$0")"
MODEL_FILE="${MT_MODEL_FILE:-ggml-large-v3-turbo-q5_0.bin}"   # multilingual (EN / Cantonese / Mandarin), ~570 MB
export PATH="$PWD/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"

echo "1/4 Core Audio tap"
if [ ! -x tools/coreaudio-tap ] || [ tools/coreaudio-tap.swift -nt tools/coreaudio-tap ]; then
  swiftc -O tools/coreaudio-tap.swift -o tools/coreaudio-tap -framework CoreAudio -framework AudioToolbox -framework Foundation \
    || { echo "swiftc failed. If it says the SDK is not supported by the compiler, install matching Command Line Tools (softwareupdate) or Xcode."; exit 1; }
fi
echo "   ok"

echo "2/4 whisper-cli"
if command -v whisper-cli >/dev/null; then
  echo "   found $(command -v whisper-cli)"
elif command -v brew >/dev/null; then
  brew install whisper-cpp
else
  command -v git >/dev/null && command -v clang >/dev/null || { echo "need git + Xcode Command Line Tools (xcode-select --install)"; exit 1; }
  W=$(mktemp -d); python3 -m venv "$W/v" && "$W/v/bin/pip" -q install cmake
  git clone -q --depth 1 https://github.com/ggml-org/whisper.cpp "$W/src"
  "$W/v/bin/cmake" -S "$W/src" -B "$W/src/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF >/dev/null
  "$W/v/bin/cmake" --build "$W/src/build" -j8 --target whisper-cli >/dev/null
  mkdir -p bin && cp "$W/src/build/bin/whisper-cli" bin/ && rm -rf "$W"
  echo "   built bin/whisper-cli"
fi

echo "3/4 Whisper model"
mkdir -p models
if [ -s "models/$MODEL_FILE" ]; then echo "   found models/$MODEL_FILE"; else
  curl -L --fail -o "models/$MODEL_FILE.part" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$MODEL_FILE" \
    && mv "models/$MODEL_FILE.part" "models/$MODEL_FILE"
fi

echo "4/4 claude CLI"
command -v claude >/dev/null && echo "   found $(command -v claude)" || echo "   NOT FOUND: install Claude Code (https://claude.com/claude-code); summaries need it"

echo; echo "Done. Run ./verify.sh to check everything, then ./start.sh"
