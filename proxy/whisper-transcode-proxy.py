#!/usr/bin/env python3
"""CORS + transcoding proxy in front of WhisperKit serve.

Obsidian's Whisper plugin records Opus (webm/ogg) via Chromium MediaRecorder,
but WhisperKit's AVFoundation loader only decodes wav/mp3/m4a -> every recording
500s. This proxy: answers CORS preflight, transcodes the uploaded audio to
16kHz mono wav with ffmpeg, and forwards it to WhisperKit. Audio only; chat
post-processing goes straight to Ollama (via the Caddy CORS proxy on :50061).

Run: uv run --with aiohttp python whisper-transcode-proxy.py
Listens on 127.0.0.1:50062 -> forwards to 127.0.0.1:50060.
"""
import os
import subprocess
import tempfile

from aiohttp import ClientSession, FormData, web

WHISPER = "http://127.0.0.1:50060/v1/audio/transcriptions"
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


async def preflight(request):
    return web.Response(status=204, headers=CORS)


async def transcribe(request):
    audio = None
    filename = "audio.webm"
    model = "whisper-1"
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            audio = await part.read(decode=False)
            filename = part.filename or filename
        elif part.name == "model":
            model = (await part.text()).strip() or model
    if not audio:
        return web.json_response({"error": "no file part"}, status=400, headers=CORS)

    ext = os.path.splitext(filename)[1] or ".webm"
    inp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    inp.write(audio)
    inp.close()
    outp = inp.name + ".wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", inp.name, "-ar", "16000", "-ac", "1", outp],
            check=True,
            capture_output=True,
        )
        with open(outp, "rb") as f:
            wav = f.read()
        data = FormData()
        data.add_field("file", wav, filename="audio.wav", content_type="audio/wav")
        data.add_field("model", model)
        async with ClientSession() as s:
            async with s.post(WHISPER, data=data) as resp:
                body = await resp.read()
                ct = resp.headers.get("Content-Type", "application/json")
                return web.Response(status=resp.status, body=body,
                                    headers={**CORS, "Content-Type": ct})
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"").decode(errors="replace")[-600:]
        return web.json_response({"error": "ffmpeg transcode failed", "detail": tail},
                                 status=502, headers=CORS)
    finally:
        for p in (inp.name, outp):
            try:
                os.unlink(p)
            except OSError:
                pass


app = web.Application(client_max_size=256 * 1024 * 1024)
app.router.add_route("OPTIONS", "/{tail:.*}", preflight)
app.router.add_post("/v1/audio/transcriptions", transcribe)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=50062)
