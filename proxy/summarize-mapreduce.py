#!/usr/bin/env python3
"""Map-reduce summarizer for transcripts too long for one LLM call.

The Obsidian Whisper plugin posts the *entire* transcript as one user message to
Ollama (Gemma) for post-processing. A multi-hour meeting overruns the model's
context window, so the tail of the transcript is silently truncated and the
Summary/Tasks/Notes degrade. This helper does the post-processing instead, by
map-reduce:

  map    split the transcript into token-bounded chunks and ask the model for
         per-chunk partial notes (Summary / Tasks / Notes for that slice);
  reduce feed the concatenated partials back to the model to fold them into one
         final ## Summary / ## Tasks / ## Notes, then append the full verbatim
         ## Transcript.

It reuses the model, endpoint and prompt already configured for the pipeline —
no new provider, no plugin change. Point it at the same Ollama URL the plugin
uses. For short transcripts it makes a single call (identical to the plugin),
so it is safe to run on everything.

Usage:
  python summarize-mapreduce.py transcript.txt          # prints markdown
  cat transcript.txt | python summarize-mapreduce.py    # stdin

Env knobs:
  OLLAMA_URL        chat endpoint (default http://127.0.0.1:11434/v1/chat/completions)
  LLM_MODEL         model id      (default gemma4:e4b-mlx)
  SUMMARY_CHUNK_CHARS  map-chunk size in characters (default 8000, ~2k tokens)
"""
import json
import os
import sys
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemma4:e4b-mlx")
CHUNK_CHARS = int(os.environ.get("SUMMARY_CHUNK_CHARS", "8000"))

# The plugin's real post-processing instruction (kept in sync with
# obsidian/whisper-data.json.example -> postProcessingPrompt).
FINAL_PROMPT = (
    "You format raw voice-note transcripts into clean Obsidian markdown. Output "
    "only valid GitHub-flavored Markdown - no preamble, no commentary, no code "
    "fences around the whole response. Produce, in order: ## Summary (1-2 "
    "sentences). ## Tasks (action items as '- [ ] ' checkboxes; omit the section "
    "entirely if none; do not invent tasks). ## Notes (key points as bullets). "
    "Do not add facts not present in the transcript."
)
MAP_PROMPT = (
    "You are summarizing one part of a longer meeting transcript. Produce concise "
    "markdown with three sections for THIS part only: ## Summary (1-2 sentences), "
    "## Tasks (action items as '- [ ] ' checkboxes; omit if none; do not invent "
    "tasks), ## Notes (key points as bullets). Do not add facts not present."
)


def chat(system, user):
    payload = json.dumps({
        "model": LLM_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"].strip()


def split_chunks(text, size):
    """Split on paragraph/line boundaries so chunks stay <= `size` chars."""
    if len(text) <= size:
        return [text]
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > size and cur:
            chunks.append(cur)
            cur = ""
        # a single oversized line: hard-split it
        while len(line) > size:
            chunks.append(line[:size])
            line = line[size:]
        cur += line
    if cur.strip():
        chunks.append(cur)
    return chunks


def summarize(transcript):
    transcript = transcript.strip()
    chunks = split_chunks(transcript, CHUNK_CHARS)

    if len(chunks) == 1:
        # Short transcript: single call, same as the plugin's direct path.
        body = chat(FINAL_PROMPT, transcript)
    else:
        partials = []
        for i, ch in enumerate(chunks, 1):
            partials.append(f"### Part {i}/{len(chunks)}\n" + chat(MAP_PROMPT, ch))
        combined = "\n\n".join(partials)
        reduce_user = (
            "Below are partial notes from consecutive parts of one meeting. Merge "
            "them into a single coherent set of notes, de-duplicating and ordering "
            "logically:\n\n" + combined
        )
        body = chat(FINAL_PROMPT, reduce_user)

    # Always append the full verbatim transcript (never sent to the model whole,
    # so it is never truncated).
    return body.rstrip() + "\n\n## Transcript\n\n" + transcript + "\n"


def main():
    if len(sys.argv) > 1 and sys.argv[1] not in ("-", "--"):
        transcript = open(sys.argv[1], encoding="utf-8").read()
    else:
        transcript = sys.stdin.read()
    if not transcript.strip():
        sys.exit("error: empty transcript")
    sys.stdout.write(summarize(transcript))


if __name__ == "__main__":
    main()
