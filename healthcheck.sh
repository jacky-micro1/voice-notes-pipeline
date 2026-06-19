#!/usr/bin/env bash
# End-to-end sanity check for the local voice-note pipeline.
# Verifies services, CORS (single header), Opus transcription, post-processing, and an edge case.
set -uo pipefail

WHISPER_PORT="${WHISPER_PORT:-50060}"
PROXY_PORT="${PROXY_PORT:-50062}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
LLM_MODEL="${LLM_MODEL:-gemma4:e4b-mlx}"
VAULT="${VAULT:-/Users/mjg/micro1/micro1}"
DATA="$VAULT/.obsidian/plugins/whisper/data.json"
UID_="$(id -u)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
no(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "===== Voice-Note Pipeline — Sanity Check ====="

echo "--- services up ---"
launchctl print "gui/$UID_/com.mjg.whisperkit" 2>/dev/null | grep -q "state = running" && ok "WhisperKit running" || no "WhisperKit running"
launchctl print "gui/$UID_/com.mjg.whisper-transcode-proxy" 2>/dev/null | grep -q "state = running" && ok "transcode-proxy running" || no "transcode-proxy running"
brew services list 2>/dev/null | grep -q "ollama.*started" && ok "Ollama started" || no "Ollama started"

echo "--- CORS single-header (the bug that breaks processing) ---"
n=$(curl -s -i -X OPTIONS "http://127.0.0.1:$PROXY_PORT/v1/audio/transcriptions" -H "Origin: app://obsidian.md" 2>/dev/null | grep -ic "access-control-allow-origin")
[ "$n" = 1 ] && ok "transcribe: exactly 1 Allow-Origin" || no "transcribe Allow-Origin count=$n (want 1)"
n=$(curl -s -i -m20 "http://127.0.0.1:$OLLAMA_PORT/v1/chat/completions" -H "Origin: app://obsidian.md" -d "{\"model\":\"$LLM_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"stream\":false}" 2>/dev/null | grep -ic "access-control-allow-origin")
[ "$n" = 1 ] && ok "chat: exactly 1 Allow-Origin" || no "chat Allow-Origin count=$n (want 1)"

echo "--- E2E transcription (real Opus webm, like the plugin) ---"
say -o "$TMP/a.aiff" "remind me to email the landlord tomorrow" 2>/dev/null
afconvert "$TMP/a.aiff" "$TMP/a.wav" -d LEI16 -f WAVE 2>/dev/null
ffmpeg -y -i "$TMP/a.wav" -c:a libopus "$TMP/a.webm" 2>/dev/null
TX=$(curl -s -m30 "http://127.0.0.1:$PROXY_PORT/v1/audio/transcriptions" -H "Origin: app://obsidian.md" -F file=@"$TMP/a.webm" -F model=whisper-1 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print((''.join(s['text'] for s in d.get('segments',[])) or d.get('text','')).strip())" 2>/dev/null)
[ -n "$TX" ] && ok "webm transcribed: '$TX'" || no "webm transcription"

echo "--- E2E post-processing (uses the plugin's real system prompt) ---"
# Build the payload in python: read the actual postProcessingPrompt from data.json
# (the plugin's real instruction), pass the transcript via env to avoid shell quoting.
PAYLOAD=$(TX="$TX" DATA="$DATA" LLM_MODEL="$LLM_MODEL" python3 -c '
import json, os
sysprompt = "You format raw voice-note transcripts into clean Obsidian markdown. Output only valid GFM. Produce, in order: ## Summary (1-2 sentences). ## Tasks (action items as \"- [ ] \" checkboxes; omit if none). ## Notes (bullets). ## Transcript (verbatim)."
try:
    d = json.load(open(os.environ["DATA"]))
    if d.get("postProcessingPrompt"): sysprompt = d["postProcessingPrompt"]
except Exception: pass
print(json.dumps({"model": os.environ["LLM_MODEL"], "stream": False,
    "messages": [{"role": "system", "content": sysprompt}, {"role": "user", "content": os.environ["TX"]}]}))')
OUT=$(curl -s -m90 "http://127.0.0.1:$OLLAMA_PORT/v1/chat/completions" -H "Origin: app://obsidian.md" -H "Content-Type: application/json" -d "$PAYLOAD" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)['choices'][0]['message']['content'])" 2>/dev/null)
echo "$OUT" | grep -qiE "^#+ *summary" && ok "emits Summary section" || no "Summary section"
echo "$OUT" | grep -qE "## Tasks|- \[ \]" && ok "emits Tasks / checkbox" || no "Tasks section"
echo "$OUT" | grep -qiE "^#+ *transcript" && ok "emits Transcript section" || no "Transcript section"

echo "--- edge: silent audio is graceful ---"
ffmpeg -y -f lavfi -i anullsrc=r=16000:cl=mono -t 1 -c:a libopus "$TMP/s.webm" 2>/dev/null
sc=$(curl -so/dev/null -w "%{http_code}" -m20 "http://127.0.0.1:$PROXY_PORT/v1/audio/transcriptions" -F file=@"$TMP/s.webm" -F model=whisper-1 2>/dev/null)
[ "$sc" = 200 ] && ok "silent audio -> HTTP 200" || no "silent audio HTTP $sc"

echo "--- plugin config ---"
if [ -f "$DATA" ]; then
  python3 -c "import json;d=json.load(open('$DATA')); import sys; sys.exit(0 if d.get('apiUrl')=='http://127.0.0.1:$PROXY_PORT/v1/audio/transcriptions' and d.get('postProcessingUrl')=='http://127.0.0.1:$OLLAMA_PORT/v1/chat/completions' else 1)" && ok "plugin URLs correct" || no "plugin URLs (check Obsidian → Whisper settings)"
else no "plugin data.json missing at $DATA"; fi

echo "===== RESULT: $PASS passed / $FAIL failed ====="
[ "$FAIL" = 0 ]
