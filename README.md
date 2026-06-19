# Local Voice-Note → Obsidian Pipeline

Push-to-record voice notes in Obsidian, transcribed and formatted **100% on-device** on Apple Silicon. No audio or text leaves the machine.

**Flow:** record in Obsidian → WhisperKit transcribes → Ollama/Gemma formats into `## Summary / ## Tasks / ## Notes / ## Transcript` → note saved to the vault with the audio embedded.

## Architecture (3 local services)

```
Obsidian "Whisper" plugin (records webm/Opus)
   │  transcription POST                         post-processing POST
   ▼                                             ▼
transcode proxy :50062  ──ffmpeg Opus→wav──▶  WhisperKit serve :50060    Ollama :11434 (Gemma)
(adds CORS, on-the-fly transcode)             (CoreML, IPv4 127.0.0.1)    (native CORS)
```

Three subtleties this setup solves (all caught during a real bring-up):

1. **IPv4** — WhisperKit defaults to IPv6 (`::1`); Obsidian/Electron `fetch` uses IPv4. The server is pinned to `127.0.0.1`.
2. **CORS** — the plugin makes browser-style (axios) calls that require CORS headers. WhisperKit can't send them, so a tiny proxy adds them; Ollama does CORS itself (so post-processing goes **direct** to Ollama — routing it through a second CORS proxy produces a *duplicate* `Access-Control-Allow-Origin` and Electron rejects it).
3. **Audio format** — Obsidian records Opus (webm/ogg); WhisperKit's AVFoundation loader only decodes wav/mp3/m4a. The proxy transcodes Opus→16 kHz mono wav with `ffmpeg` before forwarding.

> **Why no docker-compose?** WhisperKit needs native macOS **CoreML/Metal** — it can't run in a Linux container with GPU access. launchd + `brew services` are the correct primitives on macOS, driven here by the `Makefile`.

## Cold start (fresh Mac, step by step)

Prereqs you supply: **macOS 14+ on Apple Silicon**, and **Obsidian** with a vault.

```bash
# 1. Homebrew (skip if you have it)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. Obsidian (skip if installed) + open/create your vault once
brew install --cask obsidian

# 3. Clone this repo and run the whole setup against your vault
git clone https://github.com/jacky-micro1/voice-notes-pipeline.git ~/voice-notes-pipeline
cd ~/voice-notes-pipeline
make all VAULT=/absolute/path/to/your/vault
```

`make all` then does everything: installs `whisperkit-cli` / `ollama` / `ffmpeg` / `uv`, **downloads the WhisperKit model + pulls Gemma (`gemma4:e4b-mlx`, ~9 GB — first run is slow)**, installs + enables the Obsidian Whisper plugin, sets up the transcode proxy, writes the launchd auto-start agents, points the plugin at the local stack, starts everything, and runs the sanity check.

**Then in Obsidian (the only manual bits):** Settings → Community plugins → enable **Whisper** if it shows disabled, then record once and **Allow** the macOS Microphone prompt.

## Quick start (already have Homebrew + Obsidian + the plugin)

```bash
make all                      # install + models + proxy + launchd + configure + start + check
# or step by step:
make install                  # brew: whisperkit-cli, ollama, ffmpeg, uv
make models                   # pull Gemma + the WhisperKit model (into a non-TCC cache)
make proxy plists configure   # install proxy, write launchd agents, point the Obsidian plugin
make start && make check      # start services + run the sanity check
```

Then **restart Obsidian**, grant the one-time **Microphone** permission, and record.

Override defaults on the CLI, e.g. `make all VAULT=/path/to/vault LLM_MODEL=gemma4:e4b`.

## Day-to-day

| Command | Does |
|---|---|
| `make status` | service states + port reachability |
| `make restart` | reload all three services |
| `make check` | full end-to-end sanity check (transcribe + format + CORS + edge cases) |
| `make logs` | tail the WhisperKit + proxy logs |
| `make stop` / `make uninstall` | stop / remove launchd agents (keeps brew pkgs + models) |

## "network error" or "request failed" — triage

- **network error** → a service is down or a `localhost`/`127.0.0.1` mismatch. Run `make status`; restart with `make restart`.
- **request failed with 500** → audio-format issue (transcode proxy down). Check `make logs`; `launchctl kickstart -k gui/$(id -u)/com.mjg.whisper-transcode-proxy`.
- **transcription works, processing doesn't** → CORS on the chat path. Post-processing URL must be **`http://127.0.0.1:11434/...`** (Ollama direct), not behind another CORS proxy.

The plugin's two URLs (Obsidian → Settings → Whisper):
- Transcription: `http://127.0.0.1:50062/v1/audio/transcriptions`
- Post-processing: `http://127.0.0.1:11434/v1/chat/completions`

## Layout

```
Makefile                          # install / setup / run / check — the source of truth
proxy/whisper-transcode-proxy.py  # Opus→wav + CORS proxy in front of WhisperKit
obsidian/whisper-data.json.example# reference plugin config (only "local" placeholders, no secrets)
healthcheck.sh                    # the end-to-end sanity check (`make check`)
```

## Launch from your terminal (`.zshrc`)

The three services already auto-start at login. For manual control, add this function to `~/.zshrc`:

```zsh
voicenotes() { make -C ~/voice-notes-pipeline "${@:-start}"; }
```

Then from anywhere:

| Command | Does |
|---|---|
| `voicenotes` | start everything |
| `voicenotes status` | service states + ports |
| `voicenotes check` | full end-to-end sanity check |
| `voicenotes restart` | reload all services |
| `voicenotes logs` | tail logs |
| `voicenotes stop` | stop services |
