# Local Voice-Note -> Obsidian pipeline (on-device, macOS Apple Silicon).
# Records audio in Obsidian -> WhisperKit transcribes -> Ollama/Gemma formats -> note.
# Everything runs locally; nothing leaves the machine. `make help` for targets.
#
# NB: no docker-compose — WhisperKit needs native macOS CoreML/Metal, which can't
# run in a Linux container. launchd + brew services are the right primitives here.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ---- config (override on the CLI, e.g. `make all VAULT=/path/to/vault`) ----
VAULT         ?= /Users/mjg/micro1/micro1
WHISPER_MODEL ?= large-v3_turbo
LLM_MODEL     ?= gemma4:e4b-mlx
WHISPER_PORT  ?= 50060
PROXY_PORT    ?= 50062
OLLAMA_PORT   ?= 11434

BIN          := $(HOME)/.local/bin
LA           := $(HOME)/Library/LaunchAgents
MODEL_CACHE  := $(HOME)/.cache/whisperkit-models
PLUGIN       := $(VAULT)/.obsidian/plugins/whisper
UID          := $(shell id -u)
WK_PLIST     := $(LA)/com.mjg.whisperkit.plist
PX_PLIST     := $(LA)/com.mjg.whisper-transcode-proxy.plist

.PHONY: all help install plugin models proxy plists configure start stop restart status logs check uninstall

all: install plugin models proxy plists configure restart check ## full setup from scratch (Homebrew + Obsidian must already be installed)

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

install: ## brew deps (whisperkit-cli, ollama, ffmpeg, uv) + start ollama
	@command -v brew >/dev/null || { echo 'Homebrew not found. Install it first:'; echo '  /bin/bash -c "$$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'; exit 1; }
	brew install whisperkit-cli ollama ffmpeg uv
	brew services start ollama

plugin: ## download + enable the Obsidian "Whisper" community plugin in the vault
	@test -d "$(VAULT)/.obsidian" || { echo "ERROR: $(VAULT) is not an Obsidian vault (no .obsidian/)"; exit 1; }
	@mkdir -p "$(PLUGIN)"
	@for f in manifest.json main.js styles.css; do \
	  curl -fsSL "https://github.com/nikdanilov/whisper-obsidian-plugin/releases/latest/download/$$f" -o "$(PLUGIN)/$$f" \
	    && echo "  got $$f" || echo "  WARN: could not download $$f (install the plugin manually in Obsidian)"; \
	done
	@python3 -c "import json,os; p='$(VAULT)/.obsidian/community-plugins.json'; a=json.load(open(p)) if os.path.exists(p) else []; a=a if 'whisper' in a else a+['whisper']; json.dump(a,open(p,'w'),indent=2); print('  enabled whisper in community-plugins.json')"

models: ## pull the LLM + WhisperKit model into a non-TCC cache
	ollama pull $(LLM_MODEL)
	@# WhisperKit's default cache (~/Documents/huggingface) is TCC-protected and
	@# unreadable by a launchd agent; download then copy into a plain cache dir.
	-whisperkit-cli transcribe --model $(WHISPER_MODEL) --audio-path /dev/null 2>/dev/null
	@mkdir -p "$(MODEL_CACHE)/argmaxinc/whisperkit-coreml" "$(MODEL_CACHE)/tokenizers"
	@SRC=$$(find "$(HOME)/Documents/huggingface" -type d -name 'openai_whisper-$(WHISPER_MODEL)' 2>/dev/null | head -1); \
	 if [ -n "$$SRC" ]; then cp -R "$$SRC" "$(MODEL_CACHE)/argmaxinc/whisperkit-coreml/"; echo "copied model -> $(MODEL_CACHE)"; \
	 else echo "WARN: WhisperKit model not found under ~/Documents/huggingface — run a manual transcribe once"; fi

proxy: ## install the Opus->wav + CORS transcode proxy
	@mkdir -p "$(BIN)"
	cp proxy/whisper-transcode-proxy.py "$(BIN)/"
	-uv run --with aiohttp python -c "import aiohttp" >/dev/null 2>&1

plists: ## write + load the launchd agents (auto-start at login)
	@printf '%s\n' \
	  '<?xml version="1.0" encoding="UTF-8"?>' \
	  '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
	  '<plist version="1.0"><dict>' \
	  '<key>Label</key><string>com.mjg.whisperkit</string>' \
	  '<key>ProgramArguments</key><array>' \
	  '<string>/opt/homebrew/bin/whisperkit-cli</string><string>serve</string>' \
	  '<string>--model-path</string><string>$(MODEL_CACHE)/argmaxinc/whisperkit-coreml/openai_whisper-$(WHISPER_MODEL)</string>' \
	  '<string>--download-tokenizer-path</string><string>$(MODEL_CACHE)/tokenizers</string>' \
	  '<string>--port</string><string>$(WHISPER_PORT)</string><string>--host</string><string>127.0.0.1</string>' \
	  '</array>' \
	  '<key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string></dict>' \
	  '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>' \
	  '<key>StandardOutPath</key><string>/tmp/whisperkit.out.log</string>' \
	  '<key>StandardErrorPath</key><string>/tmp/whisperkit.err.log</string>' \
	  '</dict></plist>' > "$(WK_PLIST)"
	@printf '%s\n' \
	  '<?xml version="1.0" encoding="UTF-8"?>' \
	  '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
	  '<plist version="1.0"><dict>' \
	  '<key>Label</key><string>com.mjg.whisper-transcode-proxy</string>' \
	  '<key>ProgramArguments</key><array>' \
	  '<string>$(BIN)/uv</string><string>run</string><string>--with</string><string>aiohttp</string>' \
	  '<string>python</string><string>$(BIN)/whisper-transcode-proxy.py</string>' \
	  '</array>' \
	  '<key>EnvironmentVariables</key><dict><key>PATH</key><string>$(BIN):/opt/homebrew/bin:/usr/bin:/bin</string><key>HOME</key><string>$(HOME)</string></dict>' \
	  '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>' \
	  '<key>StandardOutPath</key><string>/tmp/whisper-proxy.out.log</string>' \
	  '<key>StandardErrorPath</key><string>/tmp/whisper-proxy.err.log</string>' \
	  '</dict></plist>' > "$(PX_PLIST)"
	@echo "wrote $(WK_PLIST) and $(PX_PLIST)"

configure: ## point the Obsidian Whisper plugin at the local stack
	@test -d "$(PLUGIN)" || { echo "ERROR: Whisper plugin not found at $(PLUGIN) — install it first"; exit 1; }
	@python3 -c "import json,os; p='$(PLUGIN)/data.json'; d=json.load(open(p)) if os.path.exists(p) else {}; \
	d['apiUrl']='http://127.0.0.1:$(PROXY_PORT)/v1/audio/transcriptions'; \
	d['postProcessingUrl']='http://127.0.0.1:$(OLLAMA_PORT)/v1/chat/completions'; \
	d['postProcessingModel']='$(LLM_MODEL)'; d['postProcessing']=True; \
	json.dump(d,open(p,'w'),indent=2); print('configured',p)"

start: ## start all services
	-brew services start ollama
	-launchctl bootstrap gui/$(UID) "$(WK_PLIST)" 2>/dev/null
	-launchctl bootstrap gui/$(UID) "$(PX_PLIST)" 2>/dev/null
	@echo "started (restart Obsidian to pick up plugin config)"

stop: ## stop all services
	-launchctl bootout gui/$(UID)/com.mjg.whisperkit 2>/dev/null
	-launchctl bootout gui/$(UID)/com.mjg.whisper-transcode-proxy 2>/dev/null
	-brew services stop ollama

restart: ## reload everything
	-launchctl kickstart -k gui/$(UID)/com.mjg.whisperkit 2>/dev/null
	-launchctl kickstart -k gui/$(UID)/com.mjg.whisper-transcode-proxy 2>/dev/null
	-brew services restart ollama
	@$(MAKE) start

status: ## show service state + ports
	@launchctl print gui/$(UID)/com.mjg.whisperkit 2>/dev/null | grep -i "state =" | sed 's/^/  whisperkit /' || echo "  whisperkit: not loaded"
	@launchctl print gui/$(UID)/com.mjg.whisper-transcode-proxy 2>/dev/null | grep -i "state =" | sed 's/^/  proxy      /' || echo "  proxy: not loaded"
	@brew services list | grep -i ollama | sed 's/^/  /'
	@for p in $(WHISPER_PORT) $(PROXY_PORT) $(OLLAMA_PORT); do printf "  127.0.0.1:%s -> " $$p; curl -so/dev/null -w "%{http_code}\n" -m3 -X OPTIONS http://127.0.0.1:$$p/ 2>/dev/null; done

logs: ## tail service logs
	tail -n 30 /tmp/whisperkit.err.log /tmp/whisper-proxy.err.log 2>/dev/null

check: ## run the end-to-end sanity check
	@bash healthcheck.sh

uninstall: stop ## stop services + remove launchd agents (keeps brew pkgs + models)
	-rm -f "$(WK_PLIST)" "$(PX_PLIST)"
	@echo "removed launchd agents (brew packages + models left intact)"
