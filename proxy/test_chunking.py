#!/usr/bin/env python3
"""Unit tests for the chunking/stitching logic (no services required).

These cover the pure functions that make long-audio transcription correct:
window planning (coverage + overlap + silence snapping) and segment stitching
(timestamp offset + overlap de-duplication + id renumbering).

Run: python proxy/test_chunking.py   (or: uv run python proxy/test_chunking.py)
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "proxy_mod", os.path.join(HERE, "whisper-transcode-proxy.py"))
proxy = importlib.util.module_from_spec(spec)
# the module imports aiohttp at top level; provide a stub if it's absent so the
# pure logic can be tested without the dependency.
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    import types
    stub = types.ModuleType("aiohttp")
    for n in ("ClientSession", "FormData", "web"):
        setattr(stub, n, type(n, (), {}))
    sys.modules["aiohttp"] = stub
spec.loader.exec_module(proxy)

fails = []


def check(cond, msg):
    print(("  ok " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def test_plan_windows_covers_and_overlaps():
    proxy.CHUNK_WINDOW_S = 300
    proxy.CHUNK_OVERLAP_S = 10
    wins = proxy.plan_windows(1000.0, mids=[])
    check(wins[0][0] == 0.0, "first window starts at 0")
    check(abs(wins[-1][1] - 1000.0) < 1e-6, "last window ends at duration")
    # contiguous coverage with overlap: each window starts before prev ends
    gap_ok = all(wins[i + 1][0] < wins[i][1] for i in range(len(wins) - 1))
    check(gap_ok, "adjacent windows overlap (no gap)")
    overlap_ok = all(abs((wins[i][1] - wins[i + 1][0]) - 10) < 1e-6
                     for i in range(len(wins) - 1))
    check(overlap_ok, "overlap equals CHUNK_OVERLAP_S")


def test_plan_windows_snaps_to_silence():
    proxy.CHUNK_WINDOW_S = 300
    proxy.CHUNK_OVERLAP_S = 10
    # a silence center at 305 is within ±10 of the 300 target -> snap there
    wins = proxy.plan_windows(1000.0, mids=[305.0])
    check(abs(wins[0][1] - 305.0) < 1e-6, "first cut snaps to silence at 305")


def test_plan_windows_short_audio_single_window():
    proxy.CHUNK_WINDOW_S = 300
    wins = proxy.plan_windows(120.0, mids=[])
    check(wins == [(0.0, 120.0)], "audio under one window -> single window")


def seg(start, end, text, sid=0):
    return {"start": start, "end": end, "text": text, "id": sid, "seek": 0}


def test_stitch_offsets_and_dedups():
    # window 0: 0-300 transcribes 0..300 ; window 1 starts at 290 (10s overlap),
    # its local 0..10 == absolute 290..300, which overlaps -> must be dropped.
    w0 = {"language": "en", "segments": [
        seg(0, 100, " hello"), seg(100, 300, " world")]}
    w1 = {"language": "en", "segments": [
        seg(0, 8, " world"),         # overlap dup (abs 290..298) -> dropped
        seg(12, 120, " after break")]}  # abs 302..410 -> kept
    merged = proxy.stitch([(0.0, w0), (290.0, w1)])
    texts = [s["text"] for s in merged["segments"]]
    check(" after break" in texts, "post-overlap segment kept")
    check(texts.count(" world") == 1, "overlap-duplicated segment removed")
    # absolute timestamps
    last = merged["segments"][-1]
    check(abs(last["start"] - 302.0) < 1e-6, "timestamps offset by window start")
    check(abs(last["end"] - 410.0) < 1e-6, "end timestamp offset by window start")
    # ids renumbered 0..n-1 contiguously
    ids = [s["id"] for s in merged["segments"]]
    check(ids == list(range(len(ids))), "ids renumbered contiguously")
    check(merged["text"].startswith("hello world after break"[:5]),
          "merged text concatenated in order")


def test_stitch_empty():
    merged = proxy.stitch([])
    check(merged["segments"] == [] and merged["text"] == "", "empty input safe")


for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
    print(fn.__name__)
    fn()

print()
if fails:
    print(f"FAILED: {len(fails)}")
    sys.exit(1)
print("all chunking tests passed")
