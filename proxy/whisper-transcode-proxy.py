#!/usr/bin/env python3
"""CORS + transcoding proxy in front of WhisperKit serve.

Obsidian's Whisper plugin records Opus (webm/ogg) via Chromium MediaRecorder,
but WhisperKit's AVFoundation loader only decodes wav/mp3/m4a -> every recording
500s. This proxy: answers CORS preflight, transcodes the uploaded audio to
16kHz mono wav with ffmpeg, and forwards it to WhisperKit. Audio only; chat
post-processing goes straight to Ollama (via the Caddy CORS proxy on :50061).

Long meetings: a multi-hour recording sent to WhisperKit in a single `serve`
request loads the whole clip into CoreML at once -> memory blowup / timeout, and
the transcription degrades. When the transcoded wav exceeds CHUNK_THRESHOLD_S,
this proxy splits it into overlapping windows (silence-aware boundaries when it
can find them, fixed windows otherwise), transcribes each window separately, and
stitches the verbose-JSON segments back together: timestamps are offset by each
window's start and overlap-duplicated segments are dropped. Short recordings take
the original single-call path unchanged.

Run: uv run --with aiohttp python whisper-transcode-proxy.py
Listens on 127.0.0.1:50062 -> forwards to 127.0.0.1:50060.

Env knobs (all optional; defaults suit on-device WhisperKit):
  WHISPER_URL            forward target (default http://127.0.0.1:50060/...)
  CHUNK_THRESHOLD_S      transcribe in one call below this duration (default 600)
  CHUNK_WINDOW_S         target window length when chunking      (default 300)
  CHUNK_OVERLAP_S        overlap between adjacent windows         (default 10)
  CHUNK_SILENCE_DB       silencedetect noise floor, dB           (default -30)
  CHUNK_SILENCE_MIN_S    min silence length to split on, seconds (default 0.5)
"""
import asyncio
import os
import re
import subprocess
import tempfile

from aiohttp import ClientSession, FormData, web

WHISPER = os.environ.get("WHISPER_URL", "http://127.0.0.1:50060/v1/audio/transcriptions")

# --- chunking config (override via env) ----------------------------------
CHUNK_THRESHOLD_S = float(os.environ.get("CHUNK_THRESHOLD_S", "600"))   # 10 min
CHUNK_WINDOW_S = float(os.environ.get("CHUNK_WINDOW_S", "300"))         # 5 min
CHUNK_OVERLAP_S = float(os.environ.get("CHUNK_OVERLAP_S", "10"))        # 10 s
CHUNK_SILENCE_DB = float(os.environ.get("CHUNK_SILENCE_DB", "-30"))     # dBFS
CHUNK_SILENCE_MIN_S = float(os.environ.get("CHUNK_SILENCE_MIN_S", "0.5"))

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


async def preflight(request):
    return web.Response(status=204, headers=CORS)


def _run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True)


def probe_duration(path):
    """Seconds of audio in `path` via ffprobe (falls back to ffmpeg parse)."""
    try:
        out = _run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ]).stdout.decode().strip()
        return float(out)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        # ffprobe missing or odd container: parse ffmpeg's Duration line.
        try:
            err = subprocess.run(["ffmpeg", "-i", path],
                                 capture_output=True).stderr.decode("utf-8", "replace")
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", err)
            if m:
                h, mi, s = m.groups()
                return int(h) * 3600 + int(mi) * 60 + float(s)
        except Exception:
            pass
        return 0.0


def silence_midpoints(path, duration):
    """Silence-center timestamps from ffmpeg silencedetect, sorted ascending."""
    try:
        err = subprocess.run(
            ["ffmpeg", "-i", path, "-af",
             f"silencedetect=noise={CHUNK_SILENCE_DB}dB:d={CHUNK_SILENCE_MIN_S}",
             "-f", "null", "-"],
            capture_output=True).stderr.decode("utf-8", "replace")
    except Exception:
        return []
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", err)]
    mids = []
    for i, st in enumerate(starts):
        en = ends[i] if i < len(ends) else min(st + CHUNK_SILENCE_MIN_S, duration)
        mids.append((st + en) / 2.0)
    return sorted(m for m in mids if 0 < m < duration)


def plan_windows(duration, mids):
    """List of (start, end) windows covering [0, duration] with overlap.

    Boundaries snap to the nearest silence center within the overlap band when
    one exists, so cuts land in pauses rather than mid-word; otherwise a fixed
    window. Each window overlaps its predecessor by CHUNK_OVERLAP_S so no speech
    is lost across a cut (the duplicated text is removed during stitching).
    """
    win = max(CHUNK_WINDOW_S, CHUNK_OVERLAP_S * 2 + 1)
    overlap = CHUNK_OVERLAP_S
    windows = []
    start = 0.0
    while start < duration:
        target_end = min(start + win, duration)
        end = target_end
        if target_end < duration:
            # snap end to a silence center near the target (within ±overlap)
            band = [m for m in mids if abs(m - target_end) <= overlap]
            if band:
                end = min(band, key=lambda m: abs(m - target_end))
        windows.append((start, end))
        if end >= duration:
            break
        start = max(0.0, end - overlap)
    return windows


def cut_window(src, start, end, dst):
    """Extract [start, end) of `src` into wav `dst` (16kHz mono, re-encoded)."""
    _run(["ffmpeg", "-y", "-i", src, "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
          "-ar", "16000", "-ac", "1", dst])


async def transcribe_wav(session, wav_bytes, model):
    """POST one wav to WhisperKit; return (status, parsed_json_or_None, raw)."""
    data = FormData()
    data.add_field("file", wav_bytes, filename="audio.wav", content_type="audio/wav")
    data.add_field("model", model)
    async with session.post(WHISPER, data=data) as resp:
        raw = await resp.read()
        try:
            import json
            return resp.status, json.loads(raw), raw
        except Exception:
            return resp.status, None, raw


def stitch(results):
    """Merge per-window verbose-JSON responses into one.

    `results` is a list of (window_start, parsed_json). Segment timestamps are
    shifted by the window start. From every window after the first, segments that
    *begin* inside the already-transcribed (overlap) region are dropped, so the
    overlapping speech appears once. The boundary is nudged by half the overlap
    so a segment straddling the cut is kept exactly once. ids/seek are
    renumbered and `text` is rebuilt from the kept segments.
    """
    merged_segments = []
    language = None
    prev_content_end = 0.0  # absolute end of last kept segment
    next_id = 0
    margin = CHUNK_OVERLAP_S / 2.0
    for win_start, js in results:
        if not js:
            continue
        language = language or js.get("language")
        for seg in js.get("segments", []):
            s = float(seg.get("start", 0.0)) + win_start
            e = float(seg.get("end", 0.0)) + win_start
            # drop segments that start inside already-transcribed overlap audio
            if merged_segments and s < prev_content_end - margin:
                continue
            seg = dict(seg)
            seg["start"], seg["end"] = s, e
            seg["id"] = next_id
            seg["seek"] = int(s * 100)
            next_id += 1
            merged_segments.append(seg)
            prev_content_end = max(prev_content_end, e)
    text = "".join(seg.get("text", "") for seg in merged_segments).strip()
    out = {
        "type": "CreateTranscriptionResponseVerboseJson",
        "language": language or "en",
        "duration": merged_segments[-1]["end"] if merged_segments else 0.0,
        "segments": merged_segments,
        "text": text,
        "words": [],
    }
    return out


async def transcribe_long(wav_path, duration, model):
    """Chunk -> transcribe each window -> stitch. Returns merged verbose JSON."""
    mids = silence_midpoints(wav_path, duration)
    windows = plan_windows(duration, mids)
    tmpdir = tempfile.mkdtemp(prefix="vn-chunks-")
    chunk_paths = []
    try:
        for i, (s, e) in enumerate(windows):
            dst = os.path.join(tmpdir, f"chunk_{i:04d}.wav")
            cut_window(wav_path, s, e, dst)
            chunk_paths.append((s, dst))
        async with ClientSession() as session:
            results = []
            for win_start, p in chunk_paths:
                with open(p, "rb") as f:
                    wav = f.read()
                status, js, _ = await transcribe_wav(session, wav, model)
                if status != 200 or js is None:
                    raise RuntimeError(f"WhisperKit returned {status} on a chunk")
                results.append((win_start, js))
        return stitch(results)
    finally:
        for _, p in chunk_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


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
        await asyncio.to_thread(
            _run, ["ffmpeg", "-y", "-i", inp.name, "-ar", "16000", "-ac", "1", outp])

        duration = probe_duration(outp)
        if duration > CHUNK_THRESHOLD_S:
            # Long meeting: split, transcribe per-window, stitch.
            merged = await transcribe_long(outp, duration, model)
            return web.json_response(merged, headers=CORS)

        # Short note: original single-call path, byte-for-byte unchanged.
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
    except RuntimeError as e:
        return web.json_response({"error": "chunked transcription failed", "detail": str(e)},
                                 status=502, headers=CORS)
    finally:
        for p in (inp.name, outp):
            try:
                os.unlink(p)
            except OSError:
                pass


def make_app():
    app = web.Application(client_max_size=1024 * 1024 * 1024)
    app.router.add_route("OPTIONS", "/{tail:.*}", preflight)
    app.router.add_post("/v1/audio/transcriptions", transcribe)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="127.0.0.1", port=50062)
