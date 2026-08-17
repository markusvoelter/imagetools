"""Generate a Ken Burns-style video from random images in a folder.

The output is a 16:9 (1920x1080) or 9:16 (1080x1920) MP4. Source images are
filtered to landscape or portrait orientation accordingly. Each image is shown
with a smooth zoom/pan ("Ken Burns"); when the image's aspect doesn't fit the
output frame, the bars are filled with a very heavy blur of the same image so
no flat color shows through. Consecutive images are stitched with a brief
crossfade.
"""

import json
import math
import os
import platform
import random
import re
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import (Image, ImageColor, ImageDraw, ImageEnhance, ImageFilter,
                 ImageFont)

from . import RunContext


FPS = 30
DEFAULT_DURATION = 4.0
MIN_IMAGE_DURATION_S = 0.35  # bar-timed images below this get absorbed by a
# neighbour. Kept just above CROSSFADE_S (0.3): a segment shorter than one
# crossfade can't complete its transition, so only those degenerate slivers
# merge — every real musical bar (typically >=0.5s apart) drives a change.
SHORT_SLIDE_MAX_S = 0.7  # bar-timed images on screen shorter than this render
# as a hard cut (no crossfade) with no Ken Burns motion: a fade + zoom that
# brief reads as a flickery smear, so a crisp static snap looks better.
FLASH_DURATION_S = 0.025       # per-flash-event overlay hold time
FLASH_BRIGHTNESS_FACTOR = 3.0  # a flash brightens the current image by this
# factor (blown-out highlights, shadow detail kept) instead of going pure white
GLOW_COLOR_WHITE_MIX = 0.9     # glow peak = this much white blended into the
# image's average color (0 = plain average color, 1.0 = pure white)
QUIET_FADE_IN_FRACTION = 0.8    # fade-IN duration as fraction of quiet length; span is [T_q, T_q+dur]. The remaining ~20% holds constant black (bar the fixed restart fade).
QUIET_RESTART_FADE_S = 0.15    # fixed short fade-OUT ending exactly at T_r
STOP_FADE_IN_BAR_FRACTION = 0.25  # "stop" blackout fades in over this fraction
# of the local bar interval (much quicker than a "quiet"), then holds black.
BLEAK_FADE_IN_S = 0.75         # "bleak" grade fades in over this many seconds
BLEAK_MAX_CONTRAST = 12.0      # ImageEnhance.Contrast factor at contrast=+10
# (approximates a pure black/white threshold; contrast=0 → factor 1 = normal)
BLACK_FADE_IN_EXP = 0.0        # curvature of the fade-TO-black; >0 darkens
# faster at the start of the fade (blacker sooner), 0 = linear (even darkening
# spread across the whole fade span).
DEFAULT_KB_STRENGTH = 0.5     # 0..1
MAX_ZOOM_AT_FULL_STRENGTH = 0.5  # strength=1.0 → up to 1.5x zoom
MAX_ROTATION_DEG = 7.5        # strength=1.0 → rotations sampled in ±MAX_ROTATION_DEG
BLUR_RADIUS = 60              # heavy blur radius for the background fill
CROSSFADE_S = 0.3
KB_AUDIO_FADE_OUT_S = 1.5     # tail fade applied to the music
KB_END_SCREEN_FADE_S = 0.5          # crossfade from last main frame to end screen
KB_END_SCREEN_HOLD_S = 3.0          # how long the end screen is held
KB_END_SCREEN_BG = (0, 0, 0)        # bg color behind the end-screen image
KB_TITLE_DURATION_S = 3.0           # title slide hold time (seconds)
KB_TITLE_WIDTH_FRAC = 0.75          # title spans this fraction of canvas width
KB_TITLE_LINE_MAX_CHARS = 25        # split over two lines if longer than this
KB_TITLE_LINE_MAX_CHARS_PORTRAIT = 12  # portrait: fewer chars/line, more lines allowed
KB_TITLE_SHADOW_OFFSET = (4, 6)     # drop shadow offset in pixels
KB_TITLE_SHADOW_BLUR = 8            # gaussian blur radius for soft shadow
KB_TITLE_SHADOW_ALPHA = 220         # shadow opacity 0..255
KB_TEXT_SHADOW_COLOR = (76, 76, 76) # drop shadow tint (70% grey)
KB_TITLE_FONT_CANDIDATES = (
    "Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
KB_SUBTITLE_FONT_CANDIDATES = (
    "Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
KB_SUBTITLE_WIDTH_FRAC = 0.55       # subtitle spans this fraction of canvas width
KB_SUBTITLE_COLOR = (210, 210, 210) # light grey for subtitle text
# Text-slide hold time scales linearly with word count between these bounds.
TEXT_SLIDE_MIN_FACTOR = 1.875  # held this many image-slide durations at MIN_WORDS or fewer
TEXT_SLIDE_MAX_FACTOR = 3.75   # capped at this many image-slide durations at MAX_WORDS or more
TEXT_SLIDE_MIN_WORDS = 4      # ≤ this many words: use MIN_FACTOR
TEXT_SLIDE_MAX_WORDS = 20     # ≥ this many words: use MAX_FACTOR
KB_TEXT_SLIDE_WIDTH_FRAC = 0.70     # landscape: text spans this fraction of canvas width (15% margin each side)
KB_TEXT_SLIDE_WIDTH_FRAC_PORTRAIT = 0.90  # portrait: text spans this fraction of canvas width (5% margin each side)
KB_TEXT_SLIDE_LINE_SPACING = 1.5    # multiline line-height multiplier (1.0 = default tight)
KB_TEXT_SLIDE_LINE_MAX_CHARS = 30   # landscape text slide: soft wrap a sentence past this many chars
KB_TEXT_SLIDE_LINE_MAX_CHARS_PORTRAIT = 30  # portrait: wider lines → ~50% smaller font, more words per line
KB_TEXT_SLIDE_MAX_LINES = 3         # landscape text slide: hard cap on total lines (widens chars/line to fit)
KB_TEXT_SLIDE_BG_TOP = (0, 0, 0)    # gradient fallback: 100% black
KB_TEXT_SLIDE_BG_BOTTOM = (38, 38, 38)  # gradient fallback: 85% black tint
KB_TEXT_SLIDE_BACKDROP_DARKEN = 0.80       # text slide bg: darken previous photo by this (1 = pure black)
KB_TEXT_SLIDE_BACKDROP_DESATURATE = 0.5    # text slide bg: desaturate previous photo by this (1 = fully grey)
GIMMICK_START_FRAMES_PER_PIC = 1  # fastest gimmick pace (1 frame = 30 pics/sec at FPS=30)
GIMMICK_END_FRAMES_PER_PIC = 3    # slowest gimmick pace (3 frames = 10 pics/sec at FPS=30)
GIMMICK_CLICK_FREQ_HZ = 700   # carrier (tonal) frequency of the per-image "click"
GIMMICK_CLICK_DECAY_S = 0.003 # exponential decay time constant of the click
GIMMICK_CLICK_AMP = 0.6       # peak amplitude of the first click
GIMMICK_CLICK_NOISE_WEIGHT = 0.75  # noise fraction of the click waveform
GIMMICK_CLICK_TONE_WEIGHT = 0.25   # low-freq tonal fraction of the click
GIMMICK_CLICK_MIX_WEIGHT = 0.7  # click track weight when mixing with music
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.wav', '.flac', '.aac', '.ogg', '.opus'}
LANDSCAPE_MIN_AR = 1.05
PORTRAIT_MAX_AR = 1.0 / 1.05


def _scene_size(out_w, out_h):
    """Scene canvas size that contains any rotated viewport up to MAX_ROTATION_DEG.

    The bounding box of an out_w × out_h rectangle rotated by θ has
    width = out_w·cos θ + out_h·sin θ and height = out_w·sin θ + out_h·cos θ.
    Using θ = MAX_ROTATION_DEG gives the worst-case scene size.
    """
    theta = math.radians(MAX_ROTATION_DEG)
    sw = int(math.ceil(out_w * math.cos(theta) + out_h * math.sin(theta)))
    sh = int(math.ceil(out_w * math.sin(theta) + out_h * math.cos(theta)))
    return sw, sh

ASPECTS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
}


def _parse_bleak_params(raw):
    """Normalise a bleak event's `params` into a clamped grade dict.

    Keys: brightness/-10..+10, contrast/-10..+10, color_rgb/(r,g,b),
    transparency/0..100. Missing or invalid values fall back to a no-op
    grade (brightness 0, contrast 0, fully-transparent overlay)."""
    raw = raw or {}

    def _num(key, lo, hi, default):
        try:
            return max(lo, min(hi, float(raw[key])))
        except (KeyError, TypeError, ValueError):
            return default

    try:
        color_rgb = ImageColor.getrgb(str(raw.get("color", "#000000")))[:3]
    except (ValueError, TypeError):
        color_rgb = (0, 0, 0)

    return {
        "brightness": _num("brightness", -10.0, 10.0, 0.0),
        "contrast": _num("contrast", -10.0, 10.0, 0.0),
        "color_rgb": color_rgb,
        "transparency": _num("transparency", 0.0, 100.0, 100.0),
    }


def _apply_bleak(frame, alpha, params):
    """Apply a bleak grade to `frame` at strength `alpha` (0 = untouched,
    1 = full grade). brightness/contrast/overlay each interpolate from
    identity at alpha=0 to their full param value at alpha=1."""
    # Brightness: -10 → black, +10 → white, 0 → unchanged.
    b = params["brightness"] * alpha
    if b < 0:
        frame = ImageEnhance.Brightness(frame).enhance(1.0 + b / 10.0)
    elif b > 0:
        white = Image.new("RGB", frame.size, (255, 255, 255))
        frame = Image.blend(frame, white, b / 10.0)

    # Contrast: -10 → flat (one colour), 0 → normal, +10 → near black/white.
    c = params["contrast"] * alpha
    if c < 0:
        frame = ImageEnhance.Contrast(frame).enhance(1.0 + c / 10.0)
    elif c > 0:
        factor = 1.0 + (c / 10.0) * (BLEAK_MAX_CONTRAST - 1.0)
        frame = ImageEnhance.Contrast(frame).enhance(factor)

    # Colour overlay: transparency 0 → solid colour, 100 → invisible.
    overlay_alpha = (1.0 - params["transparency"] / 100.0) * alpha
    if overlay_alpha > 0:
        color = Image.new("RGB", frame.size, params["color_rgb"])
        frame = Image.blend(frame, color, overlay_alpha)
    return frame


def _load_bar_data(audio_path, start_at_crop=False):
    """Load bar timing and event structure from a sidecar JSON, or None.

    Returns a dict with:
      bar_times:     sorted list of bar timestamps (music seconds).
      quiet_ranges:  [(T_q, T_r)] pairs — quieting → restart.
      glow_ranges:   [(T_s, T_e)] pairs — glow → next event (any kind).
      flash_times:   [T] — each flash lasts FLASH_DURATION_S from T.
      events:        raw sorted list of {"time", "kind"}.
      crop_offset_s: seconds skipped from the audio start (0 if not cropped).

    If `start_at_crop=True` and the JSON has a "crop" event, all bar times
    and event times are rebased so the crop moment becomes t=0; anything
    before that is dropped, and the audio input will be seeked forward by
    the same offset (see caller). The glow "next event" falls back to
    bar_times[-1] if no later event exists. Any parse failure returns
    None.
    """
    if not audio_path:
        return None
    json_path = os.path.splitext(audio_path)[0] + ".json"
    if not os.path.isfile(json_path):
        return None
    try:
        with open(json_path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    bars = data.get("bars") or []
    times = []
    muted_times = []
    manual_times = []
    for b in bars:
        try:
            t = float(b["time"])
        except (KeyError, TypeError, ValueError):
            continue
        is_manual = b.get("manual") is True
        if b.get("muted") is True and not is_manual:
            muted_times.append(t)
        else:
            times.append(t)
            if is_manual:
                manual_times.append(t)
    if len(times) < 2:
        return None

    raw_events = data.get("events") or []
    events = []
    for e in raw_events:
        try:
            t = float(e["time"])
        except (KeyError, TypeError, ValueError):
            continue
        kind = str(e.get("kind", "")).strip()
        if not kind:
            continue
        events.append({"time": t, "kind": kind, "params": e.get("params")})
    events.sort(key=lambda e: e["time"])

    crop_offset_s = 0.0
    if start_at_crop:
        for e in events:
            if e["kind"] == "crop":
                crop_offset_s = e["time"]
                break
        if crop_offset_s > 0:
            times = [t - crop_offset_s for t in times if t >= crop_offset_s]
            if len(times) < 2:
                return None
            muted_times = [t - crop_offset_s for t in muted_times
                           if t >= crop_offset_s]
            manual_times = [t - crop_offset_s for t in manual_times
                            if t >= crop_offset_s]
            events = [{"time": e["time"] - crop_offset_s, "kind": e["kind"],
                       "params": e.get("params")}
                      for e in events
                      if e["time"] >= crop_offset_s and e["kind"] != "crop"]

    bar_times = sorted(times)
    muted_bar_times = sorted(muted_times)

    def _times_of(kind):
        return [e["time"] for e in events if e["kind"] == kind]

    restart_ts = _times_of("restart")
    fallback_end = bar_times[-1]

    def _first_after(sorted_ts, t):
        for x in sorted_ts:
            if x > t:
                return x
        return None

    # Event taxonomy:
    #   - Point events (e.g. flash) sit at a single instant and never
    #     terminate a range.
    #   - Range events (glow, quiet, stop) span a period and CANNOT nest:
    #     each ends when the next range event starts or a restart occurs
    #     (whichever is first), falling back to the last bar.
    #   - restart is a pure terminator; bleak is an independent grade (below).
    RANGE_KINDS = ("glow", "quieting", "stop")
    range_start_ts = sorted(e["time"] for e in events
                            if e["kind"] in RANGE_KINDS)

    def _range_end(t):
        nxt_range = _first_after(range_start_ts, t)
        nxt_restart = _first_after(restart_ts, t)
        ends = [x for x in (nxt_range, nxt_restart) if x is not None]
        return min(ends) if ends else fallback_end

    quiet_ranges = [(t, _range_end(t)) for t in _times_of("quieting")]
    stop_ranges = [(t, _range_end(t)) for t in _times_of("stop")]
    glow_ranges = [(t, _range_end(t)) for t in _times_of("glow")]
    flash_times = _times_of("flash")

    # "bleak" is an independent colour grade (pictures keep changing), not a
    # freeze/blackout range event, so it spans from its start to the next
    # restart regardless of any range events inside it. Each bleak carries a
    # grade (brightness/contrast/colour overlay) via its params.
    def _next_restart_after(t):
        r = _first_after(restart_ts, t)
        return r if r is not None else fallback_end

    bleak_ranges = [(e["time"], _next_restart_after(e["time"]),
                     _parse_bleak_params(e.get("params")))
                    for e in events if e["kind"] == "bleak"]

    return {
        "json_path": os.path.abspath(json_path),
        "bar_times": bar_times,
        "muted_bar_times": muted_bar_times,
        "muted_bar_count": len(muted_bar_times),
        "manual_bar_times": sorted(manual_times),
        "quiet_ranges": quiet_ranges,
        "stop_ranges": stop_ranges,
        "bleak_ranges": bleak_ranges,
        "glow_ranges": glow_ranges,
        "flash_times": flash_times,
        "events": events,
        "crop_offset_s": crop_offset_s,
    }


def _compute_overlay_windows(bar_data, image_onset_s, bar_first_s,
                             flash_duration_s, quiet_fade_in_fraction,
                             quiet_restart_fade_s):
    """Translate flash/glow/quiet events into per-frame overlay windows.

    Returns (flash_windows, glow_ramps, quiet_windows) all in video-time
    seconds.

    - Each quiet event scales its FADE-IN duration to its own length. The
      fade-in is *asymmetric*, starting at T_q (not centered on it): black
      α ramps 0→1 across [T_q, T_q + fade_in_dur] where fade_in_dur =
      quiet_fade_in_fraction · L. This makes the picture stay fully
      visible until the music actually goes quiet.
    - For very short quiets we clamp fade-in duration so it doesn't
      overlap the fade-out.
    - The FADE-OUT is a fixed short asymmetric ramp ending exactly at
      T_r — the restart feels near-instantaneous.
    - `glow_ramps` entries are (ramp_start, ramp_end, hold_end): for a
      glow whose next event is a quieting, hold_end extends through the
      quiet's fade-in so white α stays opaque until black α reaches 1.
      Otherwise `hold_end == ramp_end`.
    - `quiet_windows` entries are (fi_s, fi_e, fo_s, fo_e).
    """
    def m2v(t):
        return image_onset_s + (t - bar_first_s)

    # Per-blackout fade-in duration, keyed by start time and clamped so it
    # never encroaches on the fade-out span. "quiet" scales to its own length;
    # "stop" fades in quickly over a fraction of the local bar interval.
    quiet_fade_in_dur = {}
    for s, e in bar_data["quiet_ranges"]:
        L = e - s
        max_fade_in = max(0.0, L - quiet_restart_fade_s)
        quiet_fade_in_dur[s] = min(quiet_fade_in_fraction * L, max_fade_in)
    for s, e in bar_data.get("stop_ranges", []):
        L = e - s
        max_fade_in = max(0.0, L - quiet_restart_fade_s)
        interval = _local_bar_interval(s, bar_data["bar_times"])
        quiet_fade_in_dur[s] = min(
            STOP_FADE_IN_BAR_FRACTION * interval, max_fade_in)

    # Floor the flash width at one frame: a window narrower than a frame
    # period can fall entirely between two sampled frames and never render,
    # making the flash randomly invisible.
    flash_w = max(flash_duration_s, 1.0 / FPS)
    flash_windows = [(m2v(t), m2v(t) + flash_w)
                     for t in bar_data["flash_times"]]

    glow_ramps = []
    for s, e in bar_data["glow_ranges"]:
        ramp_end_v = m2v(e)
        chained_dur = None
        for qt, dur in quiet_fade_in_dur.items():
            if abs(qt - e) < 1e-6:
                chained_dur = dur
                break
        # If a glow ends exactly where a quiet starts, hold white α=1 all
        # the way through the quiet's fade-in — otherwise the image would
        # peek through between the glow snapping off and black reaching 1.
        hold_end_v = (m2v(e + chained_dur) if chained_dur is not None
                      else ramp_end_v)
        glow_ramps.append((m2v(s), ramp_end_v, hold_end_v))

    # Adjacent blackout ranges (a quiet/stop ending exactly where the next
    # begins) form one continuous black period. Suppress the fade-out into,
    # and the fade-in out of, that internal boundary so the picture doesn't
    # briefly reappear between them.
    blackout_ranges = (list(bar_data["quiet_ranges"])
                       + list(bar_data.get("stop_ranges", [])))
    b_starts = {s for s, _ in blackout_ranges}
    b_ends = {e for _, e in blackout_ranges}
    quiet_windows = []
    for s, e in blackout_ranges:
        dur = quiet_fade_in_dur[s]
        # preceded by another blackout → start already black (no fade-in);
        # followed by another blackout → stay black (no fade-out).
        fi_e = m2v(s) if s in b_ends else m2v(s + dur)
        fo_s = m2v(e) if e in b_starts else m2v(e - quiet_restart_fade_s)
        quiet_windows.append((m2v(s), fi_e, fo_s, m2v(e)))

    # "bleak" windows share the fade-in/hold/fade-out shape of quiet windows
    # but drive a colour grade rather than a black overlay. The grade fades
    # in over BLEAK_FADE_IN_S and lifts over the restart fade back to normal.
    bleak_windows = []
    for s, e, params in bar_data.get("bleak_ranges", []):
        L = e - s
        fade_in = min(BLEAK_FADE_IN_S, max(0.0, L - quiet_restart_fade_s))
        bleak_windows.append((
            m2v(s), m2v(s + fade_in),                   # fade-in
            m2v(e - quiet_restart_fade_s), m2v(e),      # fade-out
            params,
        ))
    return flash_windows, glow_ramps, quiet_windows, bleak_windows


def _local_bar_interval(t, bar_times):
    """Spacing (music seconds) between the two bars bracketing time `t`.
    Falls back to the median bar spacing when `t` is outside the bar range."""
    diffs = [bar_times[i + 1] - bar_times[i]
             for i in range(len(bar_times) - 1)]
    if not diffs:
        return 0.0
    for i, d in enumerate(diffs):
        if bar_times[i] <= t < bar_times[i + 1]:
            return d
    diffs.sort()
    return diffs[len(diffs) // 2]


def _ease_black_in(p, k=BLACK_FADE_IN_EXP):
    """Map linear fade progress p in [0,1] to black opacity, curved so the
    picture darkens faster near the start of the fade (concave). k > 0 sets
    the curvature; k -> 0 degrades to linear."""
    if k <= 1e-9:
        return p
    return (1.0 - math.exp(-k * p)) / (1.0 - math.exp(-k))


def _overlay_at(t, flash_windows, glow_ramps, quiet_windows, bleak_windows):
    """Return (flash_alpha, glow_alpha, black_alpha, bleak_alpha,
    bleak_params) at video time `t`.

    flash_alpha drives a brightened-image pop; glow_alpha drives the white
    glow ramp; black_alpha drives the fade-to-black; bleak_alpha + bleak_params
    drive the bleak colour grade. They're returned separately because they
    render differently."""
    flash_alpha = 0.0
    for s, e in flash_windows:
        if s <= t <= e:
            flash_alpha = 1.0
            break
    glow_alpha = 0.0
    for ramp_start, ramp_end, hold_end in glow_ramps:
        if ramp_start <= t <= hold_end:
            if t <= ramp_end:
                span = ramp_end - ramp_start
                local = ((t - ramp_start) / span
                         if span > 1e-9 else 1.0)
            else:
                local = 1.0
            if local > glow_alpha:
                glow_alpha = local
    black_alpha = 0.0
    for fi_s, fi_e, fo_s, fo_e in quiet_windows:
        if fi_s <= t <= fo_e:
            if t <= fi_e:
                span = fi_e - fi_s
                black_alpha = (_ease_black_in((t - fi_s) / span)
                               if span > 1e-9 else 1.0)
            elif t <= fo_s:
                black_alpha = 1.0
            else:
                span = fo_e - fo_s
                black_alpha = 1.0 - (t - fo_s) / span if span > 1e-9 else 0.0
            break
    if black_alpha < 0.0:
        black_alpha = 0.0
    elif black_alpha > 1.0:
        black_alpha = 1.0
    bleak_alpha = 0.0
    bleak_params = None
    for fi_s, fi_e, fo_s, fo_e, params in bleak_windows:
        if fi_s <= t <= fo_e:
            if t <= fi_e:
                span = fi_e - fi_s
                bleak_alpha = (t - fi_s) / span if span > 1e-9 else 1.0
            elif t <= fo_s:
                bleak_alpha = 1.0
            else:
                span = fo_e - fo_s
                bleak_alpha = 1.0 - (t - fo_s) / span if span > 1e-9 else 0.0
            bleak_params = params
            break
    if bleak_alpha < 0.0:
        bleak_alpha = 0.0
    elif bleak_alpha > 1.0:
        bleak_alpha = 1.0
    return flash_alpha, glow_alpha, black_alpha, bleak_alpha, bleak_params


def _merge_tiny_image_segments(segments, min_duration_s):
    """Absorb image segments shorter than `min_duration_s` into an adjacent
    image, so no picture flashes by too briefly to recognise.

    Direction:
      - Default: merge into the previous image (extend its end into the
        tiny segment). Keeps the "keep showing the picture I was showing"
        feel for bar-driven tiny segments.
      - If the tiny segment was born from a quiet's synthetic cut
        (`from_cut=True`), prefer merging FORWARD into the next image so
        the picture emerging from black is the *new* one, not the pre-quiet
        one. The `from_cut` flag propagates onto the target so cascading
        tiny segments keep flowing forward.
    """
    result = list(segments)
    i = 0
    while i < len(result):
        seg = result[i]
        if seg["kind"] != "image":
            i += 1
            continue
        if seg["end"] - seg["start"] >= min_duration_s:
            i += 1
            continue
        if seg.get("manual"):
            # Manual bars are deliberate picture changes — always keep them,
            # even when the resulting slot is shorter than min_duration_s.
            i += 1
            continue
        prev = result[i - 1] if i > 0 else None
        nxt = result[i + 1] if i + 1 < len(result) else None
        prefer_next = seg.get("from_cut", False)

        def merge_into_prev():
            result[i - 1] = {**prev, "end": seg["end"]}
            del result[i]

        def merge_into_next():
            propagated = (seg.get("from_cut", False)
                          or nxt.get("from_cut", False))
            result[i + 1] = {**nxt, "start": seg["start"],
                             "from_cut": propagated}
            del result[i]

        def can_prev():
            return prev is not None and prev["kind"] == "image"

        def can_next():
            # Never shift a manual segment's start earlier — that would move
            # its (deliberate) picture-change moment.
            return (nxt is not None and nxt["kind"] == "image"
                    and not nxt.get("manual"))

        if prefer_next:
            if can_next():
                merge_into_next()
                continue
            if can_prev():
                merge_into_prev()
                continue
        else:
            if can_prev():
                merge_into_prev()
                continue
            if can_next():
                merge_into_next()
                continue
        i += 1
    return result


def _log_music_video_timeline(ctx, bar_data, bar_segments, music_start_s):
    """Log a chronological view of every music-JSON timestamp with the
    video consequence, to help debug alignment issues.

    Times shown are music-time (rebased if start_at_crop was used) →
    video-time. Video time is computed as `music_start_s + t`.

    Returns the list of formatted lines (including the header) so the caller
    can also persist them, e.g. to timeline.txt.
    """
    if not bar_data or not bar_segments:
        return []

    half_crossfade = CROSSFADE_S / 2.0

    # Freeze ranges — quiet range is used as-is now that fade-in starts at T_q.
    freezes = []
    for s, e in bar_data["quiet_ranges"]:
        freezes.append((s, e, "quiet"))
    for s, e in bar_data.get("stop_ranges", []):
        freezes.append((s, e, "stop"))
    for s, e in bar_data["glow_ranges"]:
        freezes.append((s, e, "glow"))

    def in_freeze(t):
        for s, e, kind in freezes:
            if s <= t < e:
                return kind
        return None

    # Segment starts are the image-change moments in the rendered video.
    seg_start_by_time = {}
    for i, seg in enumerate(bar_segments):
        seg_start_by_time[round(seg["start"], 6)] = i

    # Synthetic quiet-cut timestamps (mirrors the placement rule used by
    # _build_bar_segments so the log line matches the actual segment cut).
    cut_times = set()
    for s, e in bar_data["quiet_ranges"]:
        L = e - s
        if L < CROSSFADE_S:
            continue
        hold_end = e - QUIET_RESTART_FADE_S
        cut = max(s + half_crossfade, hold_end - half_crossfade)
        cut_times.add(round(cut, 6))

    # Build entries: (music_time, order_key, kind, description).
    # order_key breaks ties at the same time (event before bar before cut).
    entries = []

    for t in bar_data["bar_times"]:
        key = round(t, 6)
        if key in seg_start_by_time:
            img_i = seg_start_by_time[key]
            if img_i == 0:
                desc = f"→ image 0 begins"
            else:
                desc = f"→ image {img_i - 1} → image {img_i}"
            entries.append((t, 2, "bar", desc))
        else:
            fk = in_freeze(t)
            if fk:
                entries.append((t, 2, "bar",
                                f"CONSUMED (inside {fk} range)"))
            else:
                entries.append((t, 2, "bar",
                                "CONSUMED (absorbed by tiny-merge)"))

    for t in bar_data.get("muted_bar_times", []):
        entries.append((t, 2, "bar (muted)", "ignored — no picture change"))

    for e in bar_data["events"]:
        kind = e["kind"]
        t = e["time"]
        if kind == "quieting":
            desc = (f"start fade-to-black, current image held; "
                    f"synthetic cut hidden inside hold")
        elif kind == "stop":
            desc = ("start quick fade-to-black (¼ bar), held until restart; "
                    "synthetic cut hidden inside hold")
        elif kind == "bleak":
            desc = ("fade to darkened b/w grade, pictures keep changing, "
                    "until restart")
        elif kind == "restart":
            desc = "end quiet/stop/bleak, image normal again"
        elif kind == "glow":
            desc = "start white glow, α 0→1 until next event"
        elif kind == "flash":
            desc = f"white overlay for {FLASH_DURATION_S:.2f}s"
        elif kind == "crop":
            desc = "crop reference (should already be t=0 if rebased)"
        else:
            desc = "(unknown event kind)"
        entries.append((t, 1, kind, desc))

    for ct in cut_times:
        img_i = seg_start_by_time.get(ct)
        if img_i is not None:
            label = (f"→ image {img_i - 1} → image {img_i} "
                     f"(crossfade under black)")
        else:
            label = ("→ new-image slot merged forward "
                     "(next surviving image emerges from black)")
        entries.append((ct, 3, "cut", label))

    entries.sort(key=lambda x: (x[0], x[1]))

    lines = ["Music/video timeline (music_t → video_t):"]
    for mt, _order, kind, desc in entries:
        vt = music_start_s + mt
        lines.append(f"  {mt:8.3f} → {vt:8.3f}  {kind:14s}  {desc}")
    for ln in lines:
        ctx.log(ln)
    return lines


def _build_bar_segments(bar_data):
    """Emit image-only segments spanning [bar_times[0], bar_times[-1]].

    Bars falling inside any freeze range (quiet, stop, glow) are
    consumed — no picture change happens while the underlying image is
    being held or overlaid.

    Quiet ranges get special treatment beyond the freeze:
      - The fade-in now starts *at* T_q (asymmetric), so no backward
        extension of the freeze range is needed — the raw quiet range
        [T_q, T_r) already covers every frame that shouldn't trigger a
        picture change.
      - A synthetic "cut" is injected inside the quiet so a NEW image
        starts rendering underneath while the overlay is still fully
        opaque. When the fade-out ramps down at T_r, that new image
        emerges from black. The segment starting at the cut is tagged
        `from_cut=True` so `_merge_tiny_image_segments` merges *forward*
        if it's tiny.
      - Cut placement: as late as possible (crossfade completes exactly
        at fade-out start → clean reveal), clamped so the crossfade
        *start* is never before T_q (never blend before the quiet
        begins). Skipped for quiets so short there's no room even for
        the clamped-early cut.
    """
    bar_times = bar_data["bar_times"]
    half_crossfade = CROSSFADE_S / 2.0
    # "stop" ranges behave exactly like quiet ranges here (freeze + a
    # synthetic cut so a new image emerges from black at restart).
    blackout_ranges = (list(bar_data["quiet_ranges"])
                       + list(bar_data.get("stop_ranges", [])))
    b_starts = {s for s, _ in blackout_ranges}
    freeze_ranges = (blackout_ranges
                     + list(bar_data["glow_ranges"]))

    quiet_cuts = []
    for s, e in blackout_ranges:
        if e in b_starts:
            # Immediately followed by another blackout: no reveal here — the
            # new image emerges at the end of the blackout chain instead, so
            # the picture never flashes between the two blackouts.
            continue
        L = e - s
        if L < CROSSFADE_S:
            # Even the earliest possible cut (T_q + half_crossfade) would
            # push the crossfade beyond T_r. Not worth doing — skip.
            continue
        hold_end = e - QUIET_RESTART_FADE_S
        # Prefer late cut so image B is fully rendered by fade-out start;
        # clamp early so cross start ≥ T_q. When the hold is very short
        # the clamp wins and the crossfade end spills a bit into the
        # fade-out, but that only masks image B fading up under lifting
        # black — no old-image leak.
        cut = max(s + half_crossfade, hold_end - half_crossfade)
        quiet_cuts.append(cut)
    quiet_cuts_set = set(quiet_cuts)

    def is_frozen(t):
        for s, e in freeze_ranges:
            if s <= t < e:
                return True
        return False

    manual_set = {round(t, 6) for t in bar_data.get("manual_bar_times", [])}

    def _seg(start, end, from_cut):
        return {"kind": "image", "start": start, "end": end,
                "from_cut": from_cut,
                "manual": round(start, 6) in manual_set}

    boundaries = sorted(list(bar_times[1:]) + quiet_cuts)
    segments = []
    cur_start = bar_times[0]
    cur_start_from_cut = False
    for t in boundaries:
        if t in quiet_cuts_set:
            # Forced segment boundary inside a quiet's hold phase. The
            # segment that STARTS here will emerge from black at fade-out.
            if t > cur_start:
                segments.append(_seg(cur_start, t, cur_start_from_cut))
            cur_start = t
            cur_start_from_cut = True
        elif is_frozen(t):
            continue
        else:
            if t > cur_start:
                segments.append(_seg(cur_start, t, cur_start_from_cut))
            cur_start = t
            cur_start_from_cut = False
    if cur_start < bar_times[-1]:
        segments.append(_seg(cur_start, bar_times[-1], cur_start_from_cut))
    return segments


def _resolve_audio_track(music_path, ctx):
    """If music_path is a file, return it; if a folder, pick a random audio file."""
    music_path = os.path.abspath(music_path)
    if os.path.isfile(music_path):
        ext = os.path.splitext(music_path)[1].lower()
        if ext not in AUDIO_EXTENSIONS:
            ctx.log(f"Warning: '{ext}' is not a known audio extension; "
                    f"passing the file to ffmpeg anyway.")
        return music_path
    if not os.path.isdir(music_path):
        raise ValueError(f"Music path not a file or folder: {music_path}")
    audio_files = []
    for ext in AUDIO_EXTENSIONS:
        audio_files.extend(Path(music_path).glob(f"*{ext}"))
        audio_files.extend(Path(music_path).glob(f"*{ext.upper()}"))
    audio_files = sorted(set(audio_files))
    if not audio_files:
        ctx.log(f"No audio files in {music_path}; video will be silent.")
        return None
    return str(random.choice(audio_files))


def _collect_images(folder, orientation, target_count=None, ctx=None,
                    random_order=True):
    """Return image paths matching the requested orientation.

    The file list is ordered (shuffled if `random_order`, else sorted by name),
    then images are opened one at a time (just enough to read dimensions). If
    `target_count` is given, scanning stops as soon as that many matching
    images are found — so for a small target in a huge folder we don't open
    thousands of files unnecessarily. Yields progress logs and honors
    cancellation via `ctx`.
    """
    folder = Path(folder)
    files = [p for p in folder.iterdir()
             if p.suffix.lower() in IMAGE_EXTENSIONS]
    if not files:
        return []
    if random_order:
        random.shuffle(files)
    else:
        files.sort(key=lambda p: p.name.lower())

    if ctx is not None:
        if target_count is not None:
            ctx.log(f"Scanning up to {len(files)} file(s) for {orientation} "
                    f"images (need {target_count})...")
        else:
            ctx.log(f"Scanning {len(files)} file(s) for {orientation} images...")

    candidates = []
    for idx, p in enumerate(files, 1):
        if ctx is not None and ctx.cancelled():
            break
        try:
            with Image.open(p) as img:
                ar = img.width / img.height
        except Exception:
            continue
        if orientation == "landscape" and ar >= LANDSCAPE_MIN_AR:
            candidates.append(p)
        elif orientation == "portrait" and ar <= PORTRAIT_MAX_AR:
            candidates.append(p)
        if target_count is not None and len(candidates) >= target_count:
            if ctx is not None:
                ctx.log(f"  scanned {idx}/{len(files)}, found {len(candidates)} — "
                        f"target reached.")
            break
        if ctx is not None and idx % 100 == 0:
            ctx.log(f"  scanned {idx}/{len(files)}, "
                    f"found {len(candidates)} so far...")
    return candidates


def _build_end_screen_frame(end_screen_path, out_w, out_h,
                            bg_color=KB_END_SCREEN_BG):
    """Compose the end-screen frame at (out_w, out_h): bg color filling the
    canvas with the user's image scaled aspect-preserved, alpha respected.

    Landscape canvas: image scales to 100% of width, vertically centered
    (letterboxed top/bottom if image is shorter than the canvas).
    Portrait canvas: image scales to 100% of height, then center-cropped
    horizontally to canvas width.
    """
    frame = Image.new("RGB", (out_w, out_h), bg_color)
    img = Image.open(end_screen_path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    iw, ih = img.size
    if out_h > out_w:
        # Portrait: fit height, center-crop horizontally
        scale = out_h / ih
        new_h = out_h
        new_w = max(1, int(round(iw * scale)))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        x = (out_w - new_w) // 2  # negative when image wider than canvas → crop
        frame.paste(img, (x, 0), img)
    else:
        # Landscape: fit width, vertically center
        scale = out_w / iw
        new_w = out_w
        new_h = max(1, int(round(ih * scale)))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        y = (out_h - new_h) // 2
        frame.paste(img, (0, y), img)
    return frame


def _load_font(candidates, size):
    """Load the first truetype font from `candidates` that exists, else default.

    A candidate may be suffixed with `#N` to select face index N inside a
    .ttc font collection (e.g. `Avenir Next.ttc#7` picks the Regular face).
    Bare paths use face index 0, which for many system .ttc files is Bold
    rather than Regular.
    """
    for candidate in candidates:
        path = candidate
        index = 0
        if isinstance(candidate, str) and "#" in candidate:
            head, _, suffix = candidate.rpartition("#")
            if suffix.isdigit():
                path = head
                index = int(suffix)
        try:
            return ImageFont.truetype(path, size, index=index)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _arial_bold_font(size):
    """Load Arial Bold (or closest fallback) at the given pixel size."""
    return _load_font(KB_TITLE_FONT_CANDIDATES, size)


def _arial_font(size):
    """Load regular Arial (or closest fallback) at the given pixel size."""
    return _load_font(KB_SUBTITLE_FONT_CANDIDATES, size)


def _font_loader_for(font_path, default_candidates):
    """Return a `loader(size) -> ImageFont` that prefers `font_path` (if set)
    and falls back to `default_candidates`."""
    if font_path:
        candidates = (font_path,) + tuple(default_candidates)
    else:
        candidates = default_candidates
    return lambda size: _load_font(candidates, size)


def _split_title_lines(text, max_chars=KB_TITLE_LINE_MAX_CHARS):
    """If `text` exceeds `max_chars`, split it into two lines at the word break
    that produces the most balanced line lengths. Returns the text unchanged
    (sans surrounding whitespace) when ≤ max_chars or unsplittable (1 word)."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    words = text.split()
    if len(words) < 2:
        return text
    best_split, best_diff = 1, float("inf")
    for split_at in range(1, len(words)):
        line1 = " ".join(words[:split_at])
        line2 = " ".join(words[split_at:])
        diff = abs(len(line1) - len(line2))
        if diff < best_diff:
            best_diff = diff
            best_split = split_at
    return (" ".join(words[:best_split])
            + "\n" + " ".join(words[best_split:]))


def _measure_text_bbox(text, font, line_spacing_px=4):
    """Return (left, top, right, bottom) bbox of `text` (multi-line aware).
    `line_spacing_px` is extra pixels between lines (PIL's default is 4)."""
    measure_img = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(measure_img)
    if "\n" in text:
        return draw.multiline_textbbox(
            (0, 0), text, font=font, align="center", spacing=line_spacing_px)
    return draw.textbbox((0, 0), text, font=font, anchor="lt")


def _find_font_size_for_width(text, target_w, max_size,
                              font_loader=_arial_bold_font):
    """Binary-search the largest font size where `text` fits in `target_w`."""
    lo, hi = 8, max(8, max_size)
    best = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        font = font_loader(mid)
        bbox = _measure_text_bbox(text, font)
        text_w = bbox[2] - bbox[0]
        if text_w <= target_w:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _draw_text_with_shadow(overlay, text, font, x, y, text_color,
                           line_spacing_px=4):
    """Composite a drop-shadowed text run onto `overlay` (RGBA) at (x, y).
    `line_spacing_px` is extra pixels between lines for multi-line text."""
    is_multiline = "\n" in text

    def _draw(target_image, dx, dy, fill):
        d = ImageDraw.Draw(target_image)
        if is_multiline:
            d.multiline_text((dx, dy), text, font=font, fill=fill,
                             align="center", spacing=line_spacing_px)
        else:
            d.text((dx, dy), text, font=font, fill=fill)

    shadow_layer = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
    _draw(shadow_layer,
          x + KB_TITLE_SHADOW_OFFSET[0],
          y + KB_TITLE_SHADOW_OFFSET[1],
          fill=(*KB_TEXT_SHADOW_COLOR, KB_TITLE_SHADOW_ALPHA))
    shadow_layer = shadow_layer.filter(
        ImageFilter.GaussianBlur(KB_TITLE_SHADOW_BLUR))
    overlay.alpha_composite(shadow_layer)

    text_layer = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
    _draw(text_layer, x, y, fill=text_color)
    overlay.alpha_composite(text_layer)


def _build_title_overlay(title_text, subtitle_text, out_w, out_h,
                         title_font_path=None, subtitle_font_path=None):
    """Return an RGBA image (out_w × out_h) with `title_text` centered (Arial
    Bold or `title_font_path`, white, soft black drop shadow) and, if
    `subtitle_text` is non-empty, a subtitle below it (regular Arial or
    `subtitle_font_path`, light grey, same shadow style). The combined
    title+subtitle block is centered vertically. Each text may contain a
    "\\n" for two-line layout."""
    overlay = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    max_font = min(out_w, out_h)

    title_loader = _font_loader_for(title_font_path, KB_TITLE_FONT_CANDIDATES)
    sub_loader = _font_loader_for(subtitle_font_path, KB_SUBTITLE_FONT_CANDIDATES)

    title_target_w = out_w * KB_TITLE_WIDTH_FRAC
    title_size = _find_font_size_for_width(
        title_text, title_target_w, max_font, font_loader=title_loader)
    title_font = title_loader(title_size)
    title_bbox = _measure_text_bbox(title_text, title_font)
    title_w = title_bbox[2] - title_bbox[0]
    title_h = title_bbox[3] - title_bbox[1]

    has_subtitle = bool(subtitle_text)
    if has_subtitle:
        sub_target_w = out_w * KB_SUBTITLE_WIDTH_FRAC
        sub_size = _find_font_size_for_width(
            subtitle_text, sub_target_w, max_font, font_loader=sub_loader)
        sub_font = sub_loader(sub_size)
        sub_bbox = _measure_text_bbox(subtitle_text, sub_font)
        sub_w = sub_bbox[2] - sub_bbox[0]
        sub_h = sub_bbox[3] - sub_bbox[1]
        sub_ascent, sub_descent = sub_font.getmetrics()
        gap = sub_ascent + sub_descent  # one subtitle-font line height
        block_h = title_h + gap + sub_h
    else:
        gap = 0
        block_h = title_h

    block_top = (out_h - block_h) // 2
    title_x = (out_w - title_w) // 2 - title_bbox[0]
    title_y = block_top - title_bbox[1]
    _draw_text_with_shadow(overlay, title_text, title_font,
                           title_x, title_y, (255, 255, 255, 255))

    if has_subtitle:
        sub_x = (out_w - sub_w) // 2 - sub_bbox[0]
        sub_y = block_top + title_h + gap - sub_bbox[1]
        _draw_text_with_shadow(overlay, subtitle_text, sub_font,
                               sub_x, sub_y, (*KB_SUBTITLE_COLOR, 255))

    return overlay


def _greedy_wrap_words(words, max_chars):
    """Greedy word-wrap a list of words into lines of ≤ max_chars."""
    lines = []
    line = ""
    for word in words:
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= max_chars:
            line += " " + word
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _sentence_aware_wrap(text, max_chars):
    """Each sentence on its own line; long sentences greedy-wrapped within
    `max_chars`. Returns a list of lines."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    lines = []
    for sentence in sentences:
        sentence = sentence.strip()
        if sentence:
            lines.extend(_greedy_wrap_words(sentence.split(), max_chars))
    return lines


def _wrap_text_for_slide(text,
                         base_max_chars=KB_TEXT_SLIDE_LINE_MAX_CHARS,
                         max_lines=KB_TEXT_SLIDE_MAX_LINES):
    """Lay out a text-slide string into at most `max_lines` lines.

    First pass keeps sentence boundaries (`.!?` + whitespace) as forced
    breaks, greedy-wrapping long sentences at `base_max_chars`. If that
    exceeds `max_lines`, `max_chars` is widened until it fits. When the text
    has more than `max_lines` sentences, falls back to plain greedy wrap
    (ignoring sentence boundaries). Pass `max_lines=None` to disable the
    line cap entirely (sentence-aware wrap at `base_max_chars`). The font
    sizer that consumes the result scales the font down for wider lines, so
    widening here is what "scales the font accordingly".
    """
    text = text.strip()
    if not text:
        return text
    if max_lines is None:
        return "\n".join(_sentence_aware_wrap(text, base_max_chars))
    safety_cap = max(base_max_chars, len(text))
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]

    if len(sentences) <= max_lines:
        max_chars = base_max_chars
        while True:
            lines = _sentence_aware_wrap(text, max_chars)
            if len(lines) <= max_lines or max_chars >= safety_cap:
                if len(lines) <= max_lines:
                    return "\n".join(lines)
                break
            max_chars += 5

    # Fallback: plain greedy wrap, ignoring sentence boundaries.
    words = text.split()
    max_chars = max(base_max_chars,
                    (len(text) + max_lines - 1) // max_lines)
    while True:
        lines = _greedy_wrap_words(words, max_chars)
        if len(lines) <= max_lines or max_chars >= safety_cap:
            return "\n".join(lines)
        max_chars += 5


def _wrap_title_text(text, out_w, out_h):
    """Wrap a title (or subtitle) for the title slide.

    Landscape: classic 1- or 2-line balanced split via _split_title_lines.
    Portrait: sentence-aware wrap with KB_TITLE_LINE_MAX_CHARS_PORTRAIT
    chars/line and no upper bound on line count.
    """
    if out_h > out_w:
        return _wrap_text_for_slide(
            text,
            base_max_chars=KB_TITLE_LINE_MAX_CHARS_PORTRAIT,
            max_lines=None,
        )
    return _split_title_lines(text)


def _build_title_slide_frame(title_text, subtitle_text, out_w, out_h,
                              title_font_path=None, subtitle_font_path=None,
                              background_image_path=None):
    """Render a static RGB frame for the opening title slide.

    Background: `background_image_path` (fitted like the end screen —
    100% width landscape / 100% height + center-crop portrait) if supplied,
    otherwise the same vertical gradient as text slides.
    Foreground: `title_text` (Arial Bold default, white) with optional
    `subtitle_text` (regular Arial, light grey) below, both with the soft
    grey drop shadow."""
    if background_image_path is not None:
        bg = _build_end_screen_frame(background_image_path, out_w, out_h)
    else:
        bg = _vertical_gradient(out_w, out_h,
                                KB_TEXT_SLIDE_BG_TOP, KB_TEXT_SLIDE_BG_BOTTOM)
    overlay = _build_title_overlay(
        title_text, subtitle_text, out_w, out_h,
        title_font_path=title_font_path,
        subtitle_font_path=subtitle_font_path)
    return _composite_title_onto_frame(bg, overlay, 1.0)


def _text_slide_factor(text):
    """Duration multiplier (vs. one image slide) for a text slide, based on
    word count. Clamped between TEXT_SLIDE_MIN_FACTOR and
    TEXT_SLIDE_MAX_FACTOR, linearly interpolated between MIN_WORDS and
    MAX_WORDS."""
    wc = len(text.split())
    if wc <= TEXT_SLIDE_MIN_WORDS:
        return TEXT_SLIDE_MIN_FACTOR
    if wc >= TEXT_SLIDE_MAX_WORDS:
        return TEXT_SLIDE_MAX_FACTOR
    t = ((wc - TEXT_SLIDE_MIN_WORDS)
         / (TEXT_SLIDE_MAX_WORDS - TEXT_SLIDE_MIN_WORDS))
    return TEXT_SLIDE_MIN_FACTOR + t * (TEXT_SLIDE_MAX_FACTOR - TEXT_SLIDE_MIN_FACTOR)


def _prepare_photo_backdrop(image_path, out_w, out_h,
                            darken=KB_TEXT_SLIDE_BACKDROP_DARKEN,
                            desaturate=KB_TEXT_SLIDE_BACKDROP_DESATURATE):
    """Return an RGB image at (out_w, out_h) filled with `image_path`
    center-cropped scale-to-fill, then desaturated by `desaturate` (0..1)
    and darkened by `darken` (0..1). Used as a soft text-slide backdrop
    derived from the preceding photo."""
    src = Image.open(image_path).convert("RGB")
    iw, ih = src.size
    src_ar = iw / ih
    target_ar = out_w / out_h
    if src_ar > target_ar:
        scale = out_h / ih
        new_w = max(1, int(round(iw * scale)))
        scaled = src.resize((new_w, out_h), Image.LANCZOS)
        left = (new_w - out_w) // 2
        scaled = scaled.crop((left, 0, left + out_w, out_h))
    else:
        scale = out_w / iw
        new_h = max(1, int(round(ih * scale)))
        scaled = src.resize((out_w, new_h), Image.LANCZOS)
        top = (new_h - out_h) // 2
        scaled = scaled.crop((0, top, out_w, top + out_h))
    saturation_factor = max(0.0, 1.0 - desaturate)
    scaled = ImageEnhance.Color(scaled).enhance(saturation_factor)
    brightness_factor = max(0.0, 1.0 - darken)
    scaled = ImageEnhance.Brightness(scaled).enhance(brightness_factor)
    return scaled


def _vertical_gradient(out_w, out_h, top_rgb, bottom_rgb):
    """Return an RGB image with a vertical gradient from `top_rgb` (y=0) to
    `bottom_rgb` (y=out_h-1)."""
    if top_rgb == bottom_rgb:
        return Image.new("RGB", (out_w, out_h), top_rgb)
    strip = Image.new("RGB", (1, out_h))
    pixels = strip.load()
    for y in range(out_h):
        t = y / max(1, out_h - 1)
        pixels[0, y] = (
            int(round(top_rgb[0] + (bottom_rgb[0] - top_rgb[0]) * t)),
            int(round(top_rgb[1] + (bottom_rgb[1] - top_rgb[1]) * t)),
            int(round(top_rgb[2] + (bottom_rgb[2] - top_rgb[2]) * t)),
        )
    return strip.resize((out_w, out_h), Image.NEAREST)


def _build_text_slide_frame(text, out_w, out_h, font_path=None,
                            backdrop_path=None):
    """Render a static RGB frame for an interspersed text slide.

    Background: darkened + desaturated version of `backdrop_path` if
    supplied (typically the last image before this slide); otherwise the
    vertical gradient KB_TEXT_SLIDE_BG_TOP → BG_BOTTOM.
    Text: centered, Arial Bold (or `font_path`), white with soft grey drop
    shadow, sized to fit KB_TEXT_SLIDE_WIDTH_FRAC of canvas width (more
    padding than the title), line height multiplied by
    KB_TEXT_SLIDE_LINE_SPACING. Sentences are placed on separate lines and
    long sentences wrap greedily at KB_TEXT_SLIDE_LINE_MAX_CHARS.
    """
    if backdrop_path is not None:
        bg = _prepare_photo_backdrop(backdrop_path, out_w, out_h)
    else:
        bg = _vertical_gradient(out_w, out_h,
                                KB_TEXT_SLIDE_BG_TOP, KB_TEXT_SLIDE_BG_BOTTOM)
    if out_h > out_w:
        wrapped = _wrap_text_for_slide(
            text,
            base_max_chars=KB_TEXT_SLIDE_LINE_MAX_CHARS_PORTRAIT,
            max_lines=None,
        )
    else:
        wrapped = _wrap_text_for_slide(text)
    loader = _font_loader_for(font_path, KB_TITLE_FONT_CANDIDATES)
    max_font = min(out_w, out_h)
    width_frac = (KB_TEXT_SLIDE_WIDTH_FRAC_PORTRAIT
                  if out_h > out_w else KB_TEXT_SLIDE_WIDTH_FRAC)
    target_w = out_w * width_frac
    size = _find_font_size_for_width(
        wrapped, target_w, max_font, font_loader=loader)
    font = loader(size)
    extra_line_px = int(round(size * (KB_TEXT_SLIDE_LINE_SPACING - 1.0)))
    bbox = _measure_text_bbox(wrapped, font, line_spacing_px=extra_line_px)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    pos_x = (out_w - text_w) // 2 - bbox[0]
    pos_y = (out_h - text_h) // 2 - bbox[1]
    overlay = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    _draw_text_with_shadow(overlay, wrapped, font, pos_x, pos_y,
                           (255, 255, 255, 255),
                           line_spacing_px=extra_line_px)
    return _composite_title_onto_frame(bg, overlay, 1.0)


def _composite_title_onto_frame(frame_rgb, title_overlay_rgba, alpha):
    """Paste the title overlay onto a copy of `frame_rgb` with the title's
    alpha channel scaled by `alpha`. Returns a new RGB image."""
    if alpha <= 0:
        return frame_rgb
    r, g, b, a = title_overlay_rgba.split()
    if alpha < 1.0:
        a = a.point(lambda p, factor=alpha: int(p * factor))
    rgb_layer = Image.merge("RGB", (r, g, b))
    out = frame_rgb.copy()
    out.paste(rgb_layer, (0, 0), mask=a)
    return out


def _gimmick_frame(image_path, out_w, out_h, bg_color=(0, 0, 0)):
    """A lightweight "flip-through" frame: image fitted in frame on plain bg.

    Used by the gimmick intro; no blur, no Ken Burns motion — just a quick
    snap-to of each picked image so the viewer sees what's coming.
    """
    src = Image.open(image_path).convert("RGB")
    iw, ih = src.size
    fit = min(out_w / iw, out_h / ih)
    fg_w = max(1, int(round(iw * fit)))
    fg_h = max(1, int(round(ih * fit)))
    fg = src.resize((fg_w, fg_h), Image.LANCZOS)
    frame = Image.new("RGB", (out_w, out_h), bg_color)
    fx = (out_w - fg_w) // 2
    fy = (out_h - fg_h) // 2
    frame.paste(fg, (fx, fy))
    return frame


def _prepare_scene(image_path, out_w, out_h, blur_radius=BLUR_RADIUS):
    """Compose a scene image larger than the output frame so the rotated
    viewport can sweep into oversized blur area without exposing canvas.

    Returns (scene_image, scene_w, scene_h). The sharp, fitted source image is
    placed at the center of the scene; the rest of the scene is filled with a
    heavily blurred version of the same image scaled to fill the scene.
    """
    scene_w, scene_h = _scene_size(out_w, out_h)
    src = Image.open(image_path).convert("RGB")
    iw, ih = src.size
    target_ar = scene_w / scene_h
    src_ar = iw / ih

    # Background: scale-to-fill the SCENE, center-cropped, heavy blur.
    if src_ar > target_ar:
        new_w = int(round(ih * target_ar))
        left = (iw - new_w) // 2
        bg = src.crop((left, 0, left + new_w, ih))
    else:
        new_h = int(round(iw / target_ar))
        top = (ih - new_h) // 2
        bg = src.crop((0, top, iw, top + new_h))
    bg = bg.resize((scene_w, scene_h), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(blur_radius))

    # Foreground: scale-to-fit within out_w × out_h, place centered in scene.
    fit = min(out_w / iw, out_h / ih)
    fg_w = max(1, int(round(iw * fit)))
    fg_h = max(1, int(round(ih * fit)))
    fg = src.resize((fg_w, fg_h), Image.LANCZOS)

    scene = bg.copy()
    fx = (scene_w - fg_w) // 2
    fy = (scene_h - fg_h) // 2
    scene.paste(fg, (fx, fy))
    return scene, scene_w, scene_h


def _random_kb_views(out_w, out_h, scene_w, scene_h, strength):
    """Pick (start, end) viewports = (cx, cy, scale, rotation_deg).

    cx, cy are in scene coordinates and constrained so the viewport stays
    within the inner out_w × out_h region centered in the scene. Rotation is
    sampled in ±MAX_ROTATION_DEG · strength and operates on the scene canvas;
    the surrounding blur fills any rotated corners that fall outside the inner
    region.
    """
    s = max(0.0, min(1.0, strength))
    max_zoom = 1.0 + s * MAX_ZOOM_AT_FULL_STRENGTH
    max_rot = MAX_ROTATION_DEG * s

    offset_x = (scene_w - out_w) / 2
    offset_y = (scene_h - out_h) / 2

    def view(scale):
        vw = out_w / scale
        vh = out_h / scale
        cx_lo = offset_x + vw / 2
        cx_hi = offset_x + out_w - vw / 2
        cy_lo = offset_y + vh / 2
        cy_hi = offset_y + out_h - vh / 2
        cx = random.uniform(cx_lo, cx_hi) if cx_hi > cx_lo else scene_w / 2
        cy = random.uniform(cy_lo, cy_hi) if cy_hi > cy_lo else scene_h / 2
        rot = random.uniform(-max_rot, max_rot) if max_rot > 0 else 0.0
        return cx, cy, scale, rot

    # Sample two scales and assign the smaller to start, larger to end —
    # so every shot is a slow zoom-IN (classic Ken Burns feel).
    s1 = random.uniform(1.0, max_zoom)
    s2 = random.uniform(1.0, max_zoom)
    start_scale = min(s1, s2)
    end_scale = max(s1, s2)
    return view(start_scale), view(end_scale)


def _kb_frame(scene, out_w, out_h, cx, cy, scale, rotation_deg):
    """Sample a rotated/scaled viewport from `scene` into an out_w × out_h frame.

    Uses a single affine transform that combines translate (to viewport center),
    rotate, and scale, mapping output pixels back to scene pixels:

        scene_x = (cos θ / s)·u + (-sin θ / s)·v + (cx − (out_w/2)·a − (out_h/2)·b)
        scene_y = (sin θ / s)·u + ( cos θ / s)·v + (cy − (out_w/2)·d − (out_h/2)·e)
    """
    theta = math.radians(rotation_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    a = cos_t / scale
    b = -sin_t / scale
    c = cx - (out_w / 2) * a - (out_h / 2) * b
    d = sin_t / scale
    e = cos_t / scale
    f = cy - (out_w / 2) * d - (out_h / 2) * e
    return scene.transform(
        (out_w, out_h),
        Image.AFFINE,
        (a, b, c, d, e, f),
        Image.BILINEAR,
    )


def run(*, folder, num_images=20, aspect="16:9",
        duration_per_image=DEFAULT_DURATION,
        kb_strength=DEFAULT_KB_STRENGTH,
        music=None, gimmick=False, end_screen=None,
        title=None, subtitle=None, text_slides=None,
        title_font=None, subtitle_font=None, text_slide_font=None,
        title_screen=None, project_name=None,
        random_order=True, start_at_crop=False, debug=False, ctx=None):
    """Render the Ken Burns video.

    folder              folder of source images
    num_images          how many images to pick (capped at available)
    aspect              "16:9" or "9:16"; selects orientation filter too
    duration_per_image  seconds per image (independent of crossfade)
    kb_strength         Ken Burns intensity 0..1 (0 = static, 1 = up to 1.5x zoom)
    music               either an audio file (used directly) or a folder
                        (random audio file inside is picked)
    gimmick             if True, prepend a flip-through showing every picked
                        image at 10x speed; music gets brief silences synced
                        to each flip during the intro
    end_screen          optional path to an image used as the closing frame;
                        the main sequence crossfades into it and holds for
                        KB_END_SCREEN_HOLD_S before the video ends
    title               optional title string rendered on its own opening
                        slide (same vertical gradient background as text
                        slides, held for KB_TITLE_DURATION_S, then crossfades
                        into the first image). White Arial Bold, centered,
                        soft grey drop shadow.
    subtitle            optional subtitle string rendered below the title in
                        regular Arial, light grey, same drop-shadow style;
                        ignored if `title` is empty
    text_slides         optional iterable of strings; each becomes a static
                        text slide (no Ken Burns motion) interspersed between
                        the image slides, evenly distributed and never at the
                        very start or end. Each slide's hold time scales
                        with word count: TEXT_SLIDE_MIN_FACTOR × image
                        duration at ≤TEXT_SLIDE_MIN_WORDS words, linearly up
                        to TEXT_SLIDE_MAX_FACTOR × at ≥TEXT_SLIDE_MAX_WORDS.
                        If the count exceeds num_images-1 the list is
                        truncated.
    title_font          optional path to a TTF/TTC/OTF font file for the
                        title (overrides Arial Bold default)
    subtitle_font       optional path to a font file for the subtitle
                        (overrides regular Arial default)
    text_slide_font     optional path to a font file for interspersed text
                        slides (overrides Arial Bold default)
    title_screen        optional path to an image used as the title slide's
                        background (fitted like the end screen); the
                        title/subtitle text is overlaid on top. Falls back
                        to the vertical gradient when omitted.
    project_name        optional stem for the output filename; the .mp4 is
                        written into `folder`. If omitted, a timestamped
                        `kenburns_{aspect}_{ts}.mp4` is used instead.
    random_order        if True (default), pick images randomly; otherwise
                        scan in alphabetical order and take the first
                        `num_images` matching ones
    ctx                 RunContext
    """
    if ctx is None:
        ctx = RunContext()
    if aspect not in ASPECTS:
        raise ValueError(f"Unknown aspect: {aspect}")
    if num_images < 1:
        raise ValueError("num_images must be >= 1")
    if duration_per_image <= 0:
        raise ValueError("duration_per_image must be > 0")

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    out_w, out_h = ASPECTS[aspect]
    orientation = "landscape" if aspect == "16:9" else "portrait"
    ctx.log(f"Output: {out_w}x{out_h} ({aspect})")
    ctx.log(f"Orientation filter: {orientation}")

    candidates = _collect_images(folder, orientation,
                                 target_count=num_images, ctx=ctx,
                                 random_order=random_order)
    ctx.log(f"Found {len(candidates)} {orientation} image(s) in {folder}")
    if not candidates:
        raise RuntimeError(f"No {orientation} images in {folder}")

    n = min(num_images, len(candidates))
    if n < num_images:
        ctx.log(f"Only {n} candidate(s) available; reducing from {num_images}.")

    audio_track = None
    if music:
        audio_track = _resolve_audio_track(music, ctx)

    bar_data = _load_bar_data(audio_track, start_at_crop=start_at_crop)
    bar_segments = None
    if bar_data is not None:
        if bar_data.get("crop_offset_s", 0.0) > 0:
            ctx.log(f"Start-at-crop active: skipping first "
                    f"{bar_data['crop_offset_s']:.3f}s of audio; "
                    f"bars/events rebased to t=0 at the crop moment.")
        elif start_at_crop:
            ctx.log("Start-at-crop was requested but the sidecar JSON has "
                    "no crop event — starting from the beginning.")
        if bar_data["muted_bar_count"]:
            ctx.log(f"Ignoring {bar_data['muted_bar_count']} muted bar(s) "
                    f"— no picture change at those times.")
        bar_segments = _build_bar_segments(bar_data)
        raw_image_count = len(bar_segments)
        bar_segments = _merge_tiny_image_segments(
            bar_segments, MIN_IMAGE_DURATION_S)
        image_slot_count = len(bar_segments)
        absorbed = raw_image_count - image_slot_count
        if absorbed > 0:
            ctx.log(f"Absorbed {absorbed} tiny image slot(s) "
                    f"(<{MIN_IMAGE_DURATION_S:.2f}s) into adjacent image(s) "
                    f"to avoid flash-by pictures.")
        if bar_data["manual_bar_times"]:
            ctx.log(f"{len(bar_data['manual_bar_times'])} manual bar(s) kept "
                    f"as picture changes (exempt from tiny-merge).")
        freeze_summary = []
        if bar_data["quiet_ranges"]:
            freeze_summary.append(f"{len(bar_data['quiet_ranges'])} quiet")
        if bar_data.get("stop_ranges"):
            freeze_summary.append(f"{len(bar_data['stop_ranges'])} stop")
        if bar_data["glow_ranges"]:
            freeze_summary.append(f"{len(bar_data['glow_ranges'])} glow")
        if n > image_slot_count:
            extra = (f" (bars during {', '.join(freeze_summary)} range(s) consumed)"
                     if freeze_summary else "")
            ctx.log(f"Bar timing provides {image_slot_count} image slot(s)"
                    f"{extra}; reducing image count from {n}.")
            n = image_slot_count

    # Candidates already come back in the requested order from
    # _collect_images (shuffled or alphabetical), so take the first n.
    picks = candidates[:n]

    # Normalise text_slides: drop blank lines.
    text_slides_list = [t.strip() for t in (text_slides or []) if t and t.strip()]

    # Two dispositions for text slides:
    #   quiet-overlay mode: bar_data with quiet ranges present → texts are
    #     laid over the fade-to-black periods, driven by the black overlay's
    #     α. Handled in the overlay pipeline (see quiet_text_overlays below).
    #   sequence mode: no bars, or bars without quiet events → the classic
    #     between-images text slides inserted into slides_seq.
    quiet_overlay_texts = []  # [(text, quiet_index)] in quiet-overlay mode
    if bar_segments is not None:
        if text_slides_list and bar_data["quiet_ranges"]:
            n_quiets = len(bar_data["quiet_ranges"])
            if len(text_slides_list) > n_quiets:
                ctx.log(f"Warning: {len(text_slides_list)} text slide(s) but "
                        f"only {n_quiets} quiet range(s); truncating to "
                        f"{n_quiets}.")
                text_slides_list = text_slides_list[:n_quiets]
            # Sequential mapping: text[i] → quiet[i]. Extra quiet ranges
            # beyond the number of texts stay text-free.
            for i, t in enumerate(text_slides_list):
                quiet_overlay_texts.append((t, i))
            ctx.log(f"Placing {len(text_slides_list)} text slide(s) into the "
                    f"first {len(text_slides_list)} of {n_quiets} quiet "
                    f"range(s) as fade-in overlays.")
            text_slides_list = []  # don't also insert into slides_seq
        elif text_slides_list:
            ctx.log(f"Warning: {len(text_slides_list)} text slide(s) requested "
                    f"but no quiet ranges to overlay them on — dropping.")
            text_slides_list = []
    else:
        max_text = max(0, n - 1)
        if len(text_slides_list) > max_text:
            ctx.log(f"Warning: {len(text_slides_list)} text slide(s) requested "
                    f"but only {n} image(s); truncating to {max_text}.")
            text_slides_list = text_slides_list[:max_text]
    k_text = len(text_slides_list)
    slides_seq = []
    if bar_segments is not None:
        for img_i, seg in enumerate(bar_segments):
            if img_i >= n:
                break
            slides_seq.append({"kind": "image",
                               "path": picks[img_i],
                               "duration_s": seg["end"] - seg["start"]})
    else:
        # Insertion points: number of images preceding each text slide. Even
        # distribution divides the n images into k_text+1 chunks of ~equal size.
        text_positions = [round((j + 1) * n / (k_text + 1)) for j in range(k_text)]
        img_i = 0
        for ti, pos in enumerate(text_positions):
            while img_i < pos:
                slides_seq.append({"kind": "image", "path": picks[img_i]})
                img_i += 1
            slides_seq.append({"kind": "text", "text": text_slides_list[ti]})
        while img_i < n:
            slides_seq.append({"kind": "image", "path": picks[img_i]})
            img_i += 1

    # Optional opening title slide (its own gradient-backed slide; not
    # overlaid on an image). Prepended so it plays first, then crossfades
    # into the first image via the standard slide-to-slide mechanism.
    title_label = ""
    if title and title.strip():
        title_text_wrapped = _wrap_title_text(title.strip(), out_w, out_h)
        subtitle_text_wrapped = (_wrap_title_text(subtitle.strip(), out_w, out_h)
                                 if subtitle and subtitle.strip() else "")
        title_screen_path = None
        if title_screen and title_screen.strip():
            title_screen_path = os.path.abspath(title_screen.strip())
            if not os.path.isfile(title_screen_path):
                raise ValueError(f"Title screen image not found: {title_screen_path}")
        title_slide_frame = _build_title_slide_frame(
            title_text_wrapped, subtitle_text_wrapped, out_w, out_h,
            title_font_path=title_font,
            subtitle_font_path=subtitle_font,
            background_image_path=title_screen_path)
        title_label = title_text_wrapped.replace("\n", " / ")
        slides_seq.insert(0, {
            "kind": "title",
            "frame": title_slide_frame,
            "label": title_label,
            "subtitle": subtitle_text_wrapped,
        })
    total_slides = len(slides_seq)

    if project_name and project_name.strip():
        stem = project_name.strip()
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"kenburns_{aspect.replace(':', 'x')}_{ts}"
    output = os.path.join(folder, f"{stem}.mp4")
    os.makedirs(os.path.dirname(output), exist_ok=True)

    # If a sidecar JSON supplied bar timings, each image slide carries its
    # own duration_s (from bar diffs, possibly stretched by freeze events).
    # The UI's duration_per_image value is ignored in that case. Title hold
    # stays fixed at KB_TITLE_DURATION_S.
    if bar_segments is not None:
        image_durations = [s["duration_s"] for s in slides_seq
                           if s["kind"] == "image"]
        ref_duration = (sum(image_durations) / max(1, len(image_durations))
                        if image_durations else duration_per_image)
    else:
        image_durations = None
        ref_duration = duration_per_image

    frames_per_image_ref = max(2, int(round(ref_duration * FPS)))
    crossfade_frames = min(int(round(CROSSFADE_S * FPS)),
                           frames_per_image_ref // 2)

    # Per-slide frame counts. Computed from CUMULATIVE video-time positions
    # so per-slide rounding errors never compound. With the old formula
    # (`round(dur*FPS + K)` per slide) errors of ~+0.4 frame per slide
    # would accumulate across dozens of image slides, shifting later
    # crossfade midpoints hundreds of milliseconds off their target bar
    # times — and causing a visible "flicker" when the crossfade for a
    # post-quiet image failed to complete before the fade-out ended.
    #
    # By tracking cumulative_end_time in seconds (float) and rounding only
    # for each slide's end-frame position, the total drift stays bounded
    # at ±0.5 frame instead of growing.
    per_slide_frames = []
    cumulative_end_time = 0.0
    prev_end_frame = 0
    n_slides = len(slides_seq)
    for i, slide in enumerate(slides_seq):
        if slide["kind"] == "title":
            # Title has no incoming crossfade — its written region shrinks
            # by K on the outgoing side. Subtracting K/FPS from the on-
            # screen duration produces fpi_title = TITLE * FPS and puts
            # slide 1's cross-in midpoint at image_onset_s (= TITLE − K/2FPS).
            cumulative_end_time += (KB_TITLE_DURATION_S
                                    - crossfade_frames / FPS)
        elif slide["kind"] == "text":
            text_fpi = max(2, int(round(
                frames_per_image_ref * _text_slide_factor(slide["text"]))))
            cumulative_end_time += (text_fpi - crossfade_frames) / FPS
        else:
            # image; bar-aligned when duration_s is set, else falls back
            # to the UI's duration_per_image.
            d = slide.get("duration_s")
            cumulative_end_time += d if d is not None else duration_per_image
        end_frame = int(round(cumulative_end_time * FPS))
        written = end_frame - prev_end_frame
        if i < n_slides - 1:
            # Non-last: K tail frames saved for next slide's cross-in.
            fpi = written + crossfade_frames
        else:
            # Last: no tail saving — fpi equals what's actually written.
            fpi = written
        per_slide_frames.append(max(2, fpi))
        prev_end_frame = end_frame

    total_main_frames = (sum(per_slide_frames)
                         - (total_slides - 1) * crossfade_frames)
    main_seconds = total_main_frames / FPS

    if gimmick:
        # Image ramp: integer frames per image, one-frame steps from
        # GIMMICK_START_FRAMES_PER_PIC (fastest) to GIMMICK_END_FRAMES_PER_PIC
        # (slowest). Split n images across those K = (end - start + 1)
        # buckets as evenly as possible; earlier (faster) buckets get any
        # remainder so the ramp starts fast and slows down.
        n_buckets = (GIMMICK_END_FRAMES_PER_PIC
                     - GIMMICK_START_FRAMES_PER_PIC + 1)
        base = n // n_buckets
        remainder = n % n_buckets
        gimmick_frames_list = []
        for k in range(n_buckets):
            size = base + (1 if k < remainder else 0)
            frames_per_pic = GIMMICK_START_FRAMES_PER_PIC + k
            gimmick_frames_list.extend([frames_per_pic] * size)
        gimmick_total_frames = sum(gimmick_frames_list)
        gimmick_duration = gimmick_total_frames / FPS
        # Cumulative start times (seconds) of each gimmick image
        gimmick_image_starts = []
        _cum = 0
        for gfpi in gimmick_frames_list:
            gimmick_image_starts.append(_cum / FPS)
            _cum += gfpi
        # Audio ramp: continuous linear interpolation of click intervals so
        # the click track sounds like a smooth deceleration, decoupled from
        # the frame-quantized visuals. The n clicks are placed so the LAST
        # one lands exactly at gimmick_duration — i.e., on the title-slide
        # reveal (or the first main slide if no title). (n-1) intervals
        # ramp fastest → slowest and fill [0, gimmick_duration], leaving
        # the last flip-through image silent after the (n-1)-th click.
        ratio = (GIMMICK_END_FRAMES_PER_PIC
                 / GIMMICK_START_FRAMES_PER_PIC)  # slowest/fastest
        if n == 1:
            click_starts = [gimmick_duration]
        elif n == 2:
            click_starts = [0.0, gimmick_duration]
        else:
            # sum = (n-1) * a * (1 + ratio) / 2 = gimmick_duration
            a = 2 * gimmick_duration / ((n - 1) * (1 + ratio))
            click_intervals = [
                a * (1 + (ratio - 1) * i / (n - 2)) for i in range(n - 1)
            ]
            click_starts = [0.0]
            _c = 0.0
            for iv in click_intervals:
                _c += iv
                click_starts.append(_c)
        # The final click sits at gimmick_duration; give its ~3ms decay
        # tail room to sound by extending the last region into the title
        # slide (the extra frames are cheap and inaudibly short).
        click_end_boundary = gimmick_duration + 0.1
    else:
        gimmick_frames_list = []
        gimmick_total_frames = 0
        gimmick_duration = 0.0
        gimmick_image_starts = []
        click_starts = []
        click_end_boundary = 0.0

    end_screen_frame = None
    end_screen_fade_frames = 0
    end_screen_hold_frames = 0
    if end_screen:
        end_screen = os.path.abspath(end_screen)
        if not os.path.isfile(end_screen):
            raise ValueError(f"End screen image not found: {end_screen}")
        ctx.log(f"End screen: {end_screen}")
        end_screen_frame = _build_end_screen_frame(end_screen, out_w, out_h)
        end_screen_fade_frames = max(1, int(round(KB_END_SCREEN_FADE_S * FPS)))
        end_screen_hold_frames = int(round(KB_END_SCREEN_HOLD_S * FPS))

    end_screen_seconds = (end_screen_fade_frames + end_screen_hold_frames) / FPS
    total_seconds = gimmick_duration + main_seconds + end_screen_seconds

    # Once gimmick_duration + per_slide_frames are known, we can locate the
    # perceptual onset of image[0] in the video timeline. That anchor
    # translates every music-time timestamp (bars + events) into video time.
    if bar_data is not None:
        if title_label:
            image_onset_s = gimmick_duration + (
                per_slide_frames[0] - crossfade_frames / 2.0) / FPS
        else:
            image_onset_s = gimmick_duration
        (flash_windows, glow_ramps, quiet_windows,
         bleak_windows) = _compute_overlay_windows(
            bar_data, image_onset_s, bar_data["bar_times"][0],
            FLASH_DURATION_S, QUIET_FADE_IN_FRACTION,
            QUIET_RESTART_FADE_S)
    else:
        image_onset_s = 0.0
        flash_windows = []
        glow_ramps = []
        quiet_windows = []
        bleak_windows = []

    # Text overlays sit on top of the quiet's black overlay: for each text
    # assigned to a quiet range, the text's α curve tracks the black
    # overlay's α curve, so the text fades in while the picture fades to
    # black, holds during the black hold, and fades out at restart.
    text_overlay_windows = []  # [(fi_s, fi_e, fo_s, fo_e, text_frame)]
    if quiet_overlay_texts and quiet_windows:
        for text, qi in quiet_overlay_texts:
            if qi >= len(quiet_windows):
                continue
            text_frame = _build_text_slide_frame(
                text, out_w, out_h,
                font_path=text_slide_font,
                backdrop_path=None)
            text_overlay_windows.append(
                quiet_windows[qi] + (text_frame,))

    if title_label:
        bg_desc = f"image bg ({title_screen_path})" if title_screen_path else "gradient bg"
        ctx.log(f"Title slide: \"{title_label}\" "
                f"({KB_TITLE_DURATION_S:.1f}s, {bg_desc})")
        sub_text = slides_seq[0].get("subtitle", "")
        if sub_text:
            ctx.log(f"Subtitle: \"{sub_text.replace(chr(10), ' / ')}\"")
    if image_durations is not None:
        dmin = min(image_durations)
        dmax = max(image_durations)
        ctx.log(f"Per image: bar-timed, {len(image_durations)} intervals, "
                f"{dmin:.2f}–{dmax:.2f}s (mean {ref_duration:.2f}s); "
                f"crossfade {crossfade_frames} frames "
                f"({crossfade_frames/FPS:.2f}s). "
                f"Duration-per-image UI value ({duration_per_image:.2f}s) ignored.")
    else:
        ctx.log(f"Per image: {duration_per_image:.2f}s "
                f"({frames_per_image_ref} frames), "
                f"crossfade {crossfade_frames} frames "
                f"({crossfade_frames/FPS:.2f}s)")
    if k_text:
        ctx.log(f"Text slides: {k_text} interspersed at image positions "
                f"{text_positions} of {n}; "
                f"hold scales {TEXT_SLIDE_MIN_FACTOR:g}x at ≤{TEXT_SLIDE_MIN_WORDS} "
                f"words up to {TEXT_SLIDE_MAX_FACTOR:g}x at "
                f"≥{TEXT_SLIDE_MAX_WORDS} words")
    if gimmick:
        speed_start = FPS / gimmick_frames_list[0]
        speed_end = FPS / gimmick_frames_list[-1]
        click_target = "title reveal" if title_label else "first slide"
        ctx.log(f"Gimmick intro: {n} image(s), {gimmick_duration:.2f}s total "
                f"({gimmick_frames_list[0]}→{gimmick_frames_list[-1]} "
                f"frame(s) per image = {speed_start:.0f}→{speed_end:.0f} pics/sec); "
                f"{n} decelerating clicks ending on {click_target}; "
                f"music starts after gimmick"
                + (" + half of title" if title_label else ""))
    s_clamped = max(0.0, min(1.0, kb_strength))
    ctx.log(f"Ken Burns strength: {kb_strength:.2f} "
            f"(max zoom {1.0 + s_clamped * MAX_ZOOM_AT_FULL_STRENGTH:.2f}x, "
            f"rotation up to ±{MAX_ROTATION_DEG * s_clamped:.1f}°)")
    ctx.log(f"Total length: ~{total_seconds:.1f}s")

    if audio_track:
        ctx.log(f"Audio track: {audio_track}")
        if bar_data is not None:
            bar_first = bar_data["bar_times"][0]
            shift = image_onset_s - bar_first
            if shift >= 0:
                ctx.log(f"Music aligned to bars: starts at video "
                        f"t={shift:.3f}s so bar[0] ({bar_first:.3f}s) "
                        f"lands on the image[0] crossfade midpoint "
                        f"({image_onset_s:.3f}s).")
            else:
                ctx.log(f"Music aligned to bars: trimming "
                        f"{-shift:.3f}s from audio start so bar[0] "
                        f"({bar_first:.3f}s) lands on image[0] onset "
                        f"({image_onset_s:.3f}s).")
        overlay_bits = []
        if flash_windows:
            overlay_bits.append(f"{len(flash_windows)} flash "
                                f"({FLASH_DURATION_S:.2f}s)")
        if glow_ramps:
            overlay_bits.append(f"{len(glow_ramps)} glow (0→1 to next event)")
        if bar_data is not None and bar_data["quiet_ranges"]:
            overlay_bits.append(f"{len(bar_data['quiet_ranges'])} quiet "
                                f"(fade→black→fade)")
        if bar_data is not None and bar_data.get("stop_ranges"):
            overlay_bits.append(f"{len(bar_data['stop_ranges'])} stop "
                                f"(quick fade→black→fade)")
        if bar_data is not None and bar_data.get("bleak_ranges"):
            overlay_bits.append(f"{len(bar_data['bleak_ranges'])} bleak "
                                f"(darkened b/w grade)")
        if overlay_bits:
            ctx.log("Freeze/overlay events: " + ", ".join(overlay_bits) + ".")

    cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo',
        '-vcodec', 'rawvideo',
        '-s', f'{out_w}x{out_h}',
        '-pix_fmt', 'rgb24',
        '-r', str(FPS),
        '-i', '-',
    ]
    # Audio pipeline. Routed through -filter_complex so we can synthesize
    # spoke-click sounds (aevalsrc) and mix them with the music if any.
    extra_inputs = []
    filter_complex_parts = []
    audio_label = None
    music_start_s = 0.0  # video time when the audio track first plays

    if audio_track:
        extra_inputs.extend(['-i', audio_track])
        music_chain = ['aresample=44100']
        # Without bars: music starts after any gimmick intro, then half-way
        # through the opening title slide (if one is set), so it swells under
        # the title.
        # With bars: align music so bar[0] lands at the first image's start.
        # slides_start_s is the video time when image[0] begins (assuming
        # crossfades are symmetric enough that the perceptual transition sits
        # on the boundary). Any pre-image time (gimmick, title) plays music-free
        # or requires trimming the audio start (via -ss) — see audio_input_ss.
        audio_input_ss = 0.0
        if bar_data is not None:
            # bar[0] should play at image[0]'s perceptual onset (crossfade
            # midpoint if there's a title, else its literal onset). That
            # anchor was precomputed above as image_onset_s.
            music_start_s = image_onset_s - bar_data["bar_times"][0]
            if music_start_s < 0:
                audio_input_ss = -music_start_s
                music_start_s = 0.0
            # If bar_data was rebased for start_at_crop, skip that much
            # more of the music input so the trimmed audio starts at the
            # crop moment (which is now bar_data's t=0).
            audio_input_ss += bar_data.get("crop_offset_s", 0.0)
        else:
            music_start_s = 0.0
            if gimmick and gimmick_duration > 0:
                music_start_s += gimmick_duration
            if title_label:
                music_start_s += KB_TITLE_DURATION_S / 2
        if music_start_s > 0:
            delay_ms = int(round(music_start_s * 1000))
            music_chain.append(f"adelay={delay_ms}|{delay_ms}")
            music_chain.append(
                f"afade=t=in:st={music_start_s:.3f}:d=0.5"
            )
        audio_fade_start = max(0.0, total_seconds - KB_AUDIO_FADE_OUT_S)
        music_chain.append(
            f"afade=t=out:st={audio_fade_start:.3f}:d={KB_AUDIO_FADE_OUT_S:.3f}"
        )
        if audio_input_ss > 0:
            # Trim leading audio so bar[0] plays at video time 0 (used when
            # there is no title/gimmick and bar[0] > 0).
            extra_inputs[:0] = ['-ss', f"{audio_input_ss:.3f}"]
        filter_complex_parts.append(f"[1:a]{','.join(music_chain)}[music]")

    if gimmick and gimmick_duration > 0 and gimmick_frames_list:
        # Damped impulse at the start of every gimmick interval — a low-freq
        # tonal "thump" mixed with white noise gives a darker, spoke-like
        # click rather than a sine beep. Each click is expressed as a gated
        # decay term and the whole track is the SUM of these terms; a naive
        # nested-if chain overflows ffmpeg's expression parser once you get
        # past ~50 images, so we keep the expression flat instead. `max(0,
        # t-start)` guards the exp envelope from blowing up outside a click's
        # window (the between() gate is 0 there and would otherwise multiply
        # by exp(+huge) → inf → NaN).
        boundaries = list(click_starts) + [click_end_boundary]

        def _click_term(start, end):
            tau = f"max(0,t-{start:.4f})"
            return (
                f"between(t,{start:.4f},{end:.4f})"
                f"*{GIMMICK_CLICK_AMP}*exp(-{tau}/{GIMMICK_CLICK_DECAY_S})"
                f"*((2*random(0)-1)*{GIMMICK_CLICK_NOISE_WEIGHT}"
                f"+cos(2*PI*{GIMMICK_CLICK_FREQ_HZ}*{tau})"
                f"*{GIMMICK_CLICK_TONE_WEIGHT})"
            )

        terms = [
            _click_term(boundaries[i], boundaries[i + 1])
            for i in range(n)
        ]
        click_expr = "+".join(terms) if terms else "0"
        filter_complex_parts.append(
            f"aevalsrc=exprs='{click_expr}|{click_expr}':"
            f"d={total_seconds:.3f}:s=44100[clicks]"
        )

    has_music = audio_track is not None
    has_clicks = gimmick and gimmick_duration > 0 and bool(gimmick_frames_list)

    if has_music and has_clicks:
        filter_complex_parts.append(
            f"[music][clicks]amix=inputs=2:duration=longest:"
            f"weights='1 {GIMMICK_CLICK_MIX_WEIGHT}':normalize=0[aout]"
        )
        audio_label = "[aout]"
    elif has_music:
        audio_label = "[music]"
    elif has_clicks:
        audio_label = "[clicks]"

    timeline_lines = []
    if bar_data is not None:
        timeline_lines = _log_music_video_timeline(
            ctx, bar_data, bar_segments, music_start_s)

    cmd += extra_inputs
    if audio_label is not None:
        cmd += [
            '-filter_complex', ";".join(filter_complex_parts),
            '-map', '0:v',
            '-map', audio_label,
            '-c:a', 'aac', '-b:a', '192k',
            '-shortest',
        ]
    else:
        cmd += ['-an']

    # Encoder selection. On macOS we prefer h264_videotoolbox (hardware
    # H.264), which typically encodes 3-10x faster than libx264 for these
    # slideshow-style videos. Elsewhere we fall back to libx264 with a
    # faster preset in debug mode (debug videos are throwaway previews).
    use_hw = platform.system() == "Darwin"
    if use_hw:
        # ~5 Mbps per megapixel gives sensible file sizes across 720p / 1080p /
        # 4K. Debug mode halves the bitrate for smaller/faster throwaway files.
        megapixels = (out_w * out_h) / 1_000_000
        bitrate_mbps = max(4, int(megapixels * (2.5 if debug else 5)))
        cmd += [
            '-c:v', 'h264_videotoolbox',
            '-pix_fmt', 'yuv420p',
            '-b:v', f'{bitrate_mbps}M',
            '-profile:v', 'high',
            '-allow_sw', '1',  # silently fall back if hw unavailable
            output,
        ]
        ctx.log(f"Encoder: h264_videotoolbox (hardware) @ {bitrate_mbps} Mbps"
                + ("  [debug]" if debug else ""))
    else:
        preset = 'ultrafast' if debug else 'medium'
        cmd += [
            '-vcodec', 'libx264',
            '-pix_fmt', 'yuv420p',
            '-preset', preset,
            '-crf', '18',
            output,
        ]
        ctx.log(f"Encoder: libx264 (software) preset={preset}")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    # NOTE: intentionally *not* calling ctx.register_process(proc) so that
    # ctx.cancel() does NOT terminate ffmpeg. On stop we want to finish the
    # current slide, close the pipe cleanly, and let ffmpeg finalise a
    # valid MP4 of what was rendered up to that point.

    def render_local(scene, start_v, end_v, f, fpi):
        t = f / max(1, fpi - 1)
        te = 3 * t * t - 2 * t * t * t   # smoothstep
        cx = start_v[0] + (end_v[0] - start_v[0]) * te
        cy = start_v[1] + (end_v[1] - start_v[1]) * te
        scale = start_v[2] + (end_v[2] - start_v[2]) * te
        rot = start_v[3] + (end_v[3] - start_v[3]) * te
        return _kb_frame(scene, out_w, out_h, cx, cy, scale, rot)

    slide_frame_index = [0]
    have_overlays = bool(flash_windows or glow_ramps or quiet_windows
                         or bleak_windows or text_overlay_windows)
    black_frame_cached = (Image.new("RGB", (out_w, out_h), (0, 0, 0))
                          if quiet_windows else None)

    # Debug overlay state: build a compact chronological timeline of every
    # bar / event / synthetic cut, plus a monospace font. Cached per
    # current-index so the strip only re-renders when a new event fires.
    debug_active = debug and bar_data is not None
    debug_font = None
    debug_timeline = []  # list of dicts: {time, kind, desc}
    debug_current_slide = [None]  # (index, kind, label) — mutated in slide loop
    debug_strip_state = None

    if debug_active:
        for path in ("/System/Library/Fonts/Menlo.ttc",
                     "/System/Library/Fonts/Courier.ttc",
                     "/Library/Fonts/Courier New.ttf"):
            try:
                debug_font = ImageFont.truetype(path, 16)
                break
            except (OSError, IOError):
                continue
        if debug_font is None:
            debug_font = ImageFont.load_default()

        half_xfade = CROSSFADE_S / 2.0
        freezes = []
        for s, e in bar_data["quiet_ranges"]:
            freezes.append((s, e, "quiet"))
        for s, e in bar_data.get("stop_ranges", []):
            freezes.append((s, e, "stop"))
        for s, e in bar_data["glow_ranges"]:
            freezes.append((s, e, "glow"))

        def _in_freeze(t):
            for s, e, kind in freezes:
                if s <= t < e:
                    return kind
            return None

        seg_start_by_time = {round(seg["start"], 6): i
                             for i, seg in enumerate(bar_segments)}

        def _pick_name(img_i):
            if 0 <= img_i < len(picks):
                return picks[img_i].name
            return "?"

        for t in bar_data["bar_times"]:
            key = round(t, 6)
            if key in seg_start_by_time:
                img_i = seg_start_by_time[key]
                d = f"→ img#{img_i} {_pick_name(img_i)}"
                debug_timeline.append({"time": t, "kind": "bar",
                                       "desc": d, "order": 2})
            else:
                fk = _in_freeze(t)
                debug_timeline.append({
                    "time": t, "kind": "bar",
                    "desc": f"[skip: {fk}]" if fk else "[skip: merged]",
                    "order": 2})
        for t in bar_data.get("muted_bar_times", []):
            debug_timeline.append({"time": t, "kind": "muted",
                                   "desc": "(muted)", "order": 2})
        event_desc = {
            "quieting": "fade→black start",
            "stop":     "quick fade→black start",
            "bleak":    "→ darkened b/w grade",
            "restart":  "quiet/stop/bleak ended, image normal",
            "glow":     "→ white ramp start",
            "flash":    f"white {FLASH_DURATION_S:.2f}s",
            "crop":     "(crop rebase)",
        }
        for e in bar_data["events"]:
            debug_timeline.append({
                "time": e["time"], "kind": e["kind"],
                "desc": event_desc.get(e["kind"], e["kind"]),
                "order": 1})
        for s, e in bar_data["quiet_ranges"]:
            L = e - s
            if L < CROSSFADE_S:
                continue
            hold_end = e - QUIET_RESTART_FADE_S
            cut = max(s + half_xfade, hold_end - half_xfade)
            key = round(cut, 6)
            img_i = seg_start_by_time.get(key)
            d = (f"→ img#{img_i} {_pick_name(img_i)} (under black)"
                 if img_i is not None else "→ (merged forward)")
            debug_timeline.append({"time": cut, "kind": "cut",
                                   "desc": d, "order": 3})
        debug_timeline.sort(key=lambda x: (x["time"], x["order"]))

        strip_w = min(500, out_w // 2)
        line_h = 22
        hud_pad = 6
        hud_line_h = 22
        hud_line_count = 3
        hud_h = hud_pad * 2 + hud_line_h * hud_line_count
        strip_body_top = hud_h
        strip_body_h = max(line_h, out_h - hud_h)
        n_visible = max(4, strip_body_h // line_h)
        current_row = (n_visible * 2) // 3
        debug_strip_state = {
            "width": strip_w,
            "line_h": line_h,
            "hud_h": hud_h,
            "hud_pad": hud_pad,
            "hud_line_h": hud_line_h,
            "strip_body_top": strip_body_top,
            "strip_body_h": strip_body_h,
            "n_visible": n_visible,
            "current_row": current_row,
            "cached_idx": [-2],
            "cached_body": [None],
        }

    def _render_debug_strip_body(current_idx):
        """Render just the scrolling-timeline portion (below the HUD).
        Cached per current_idx — only re-renders when a new event fires."""
        st = debug_strip_state
        if (st["cached_idx"][0] == current_idx
                and st["cached_body"][0] is not None):
            return st["cached_body"][0]
        body = Image.new("RGB", (st["width"], st["strip_body_h"]),
                         (0, 0, 0))
        draw = ImageDraw.Draw(body)
        start_row = current_idx - st["current_row"]
        for row in range(st["n_visible"]):
            entry_idx = start_row + row
            if entry_idx < 0 or entry_idx >= len(debug_timeline):
                continue
            e = debug_timeline[entry_idx]
            y = row * st["line_h"]
            is_current = (entry_idx == current_idx)
            if is_current:
                draw.rectangle([(0, y),
                                (st["width"], y + st["line_h"])],
                               fill=(35, 55, 90))
            color = ((255, 255, 255) if is_current
                     else (170, 170, 170) if entry_idx < current_idx
                     else (110, 130, 160))
            marker = "▶" if is_current else " "
            line = (f"{marker} {e['time']:7.3f} {e['kind']:>8s}  "
                    f"{e['desc']}")[:60]
            draw.text((4, y + 2), line, fill=color, font=debug_font)
        st["cached_idx"][0] = current_idx
        st["cached_body"][0] = body
        return body

    def _debug_overlay(frame, music_t, video_t, wa, ba, ta):
        """Paint the debug HUD (top) + scrolling timeline (below) as a
        full-height opaque strip on the left of the frame."""
        st = debug_strip_state
        current_idx = -1
        for i, e in enumerate(debug_timeline):
            if e["time"] <= music_t:
                current_idx = i
            else:
                break
        body = _render_debug_strip_body(current_idx)
        frame.paste(body, (0, st["strip_body_top"]))
        draw = ImageDraw.Draw(frame)
        cur = debug_current_slide[0]
        slide_line = (f"[{cur[0]}] {cur[1]}: {cur[2]}"[:60]
                      if cur is not None else "slide: -")
        hud_lines = [
            f"music={music_t:7.3f}s  video={video_t:7.3f}s",
            slide_line,
            f"α w={wa:.2f} b={ba:.2f} t={ta:.2f}",
        ]
        draw.rectangle([(0, 0), (st["width"], st["hud_h"])],
                       fill=(0, 0, 0))
        for i, ln in enumerate(hud_lines):
            draw.text((st["hud_pad"],
                       st["hud_pad"] + i * st["hud_line_h"]),
                      ln, fill=(255, 220, 120), font=debug_font)
        return frame

    def _text_overlay_at(t):
        """Return (text_frame, alpha) for text overlay active at video time
        `t`, or (None, 0.0). α follows the same shape as the black overlay
        of the associated quiet range."""
        for fi_s, fi_e, fo_s, fo_e, text_frame in text_overlay_windows:
            if fi_s <= t <= fo_e:
                if t <= fi_e:
                    span = fi_e - fi_s
                    a = (t - fi_s) / span if span > 1e-9 else 1.0
                elif t <= fo_s:
                    a = 1.0
                else:
                    span = fo_e - fo_s
                    a = 1.0 - (t - fo_s) / span if span > 1e-9 else 0.0
                if a < 0.0:
                    a = 0.0
                elif a > 1.0:
                    a = 1.0
                return text_frame, a
        return None, 0.0

    def main_write(frame):
        """Write a single RGB frame to ffmpeg and return it (so callers can
        capture it, e.g. for `last_main_frame`). Applies white (flash/glow),
        black (quiet), text overlays, and optionally a debug HUD on top
        when their windows cover this frame's video time."""
        t_v = gimmick_duration + slide_frame_index[0] / FPS
        wa = ba = ta = 0.0
        if have_overlays:
            fa, ga, ba, bl, bleak_params = _overlay_at(
                t_v, flash_windows, glow_ramps, quiet_windows, bleak_windows)
            if bl > 0.0 and bleak_params is not None:
                # Bleak: brightness/contrast/colour-overlay grade. Applied
                # first so any flash/glow/black overlays sit on top of the
                # graded image. Interpolates from normal (bl=0) to full
                # grade (bl=1).
                frame = _apply_bleak(frame, bl, bleak_params)
            wa = max(fa, ga)  # combined white level, for the debug HUD
            if fa > 0.0:
                # Flash = the current image, blown bright — not pure white.
                brightened = ImageEnhance.Brightness(frame).enhance(
                    FLASH_BRIGHTNESS_FACTOR)
                frame = Image.blend(frame, brightened, fa)
            if ga > 0.0:
                # Glow, two phases across the ramp: first half fades the
                # picture to its own average colour; second half fades that
                # flat colour up to a near-white tint of it.
                avg = frame.resize((1, 1), Image.BOX).getpixel((0, 0))
                if ga <= 0.5:
                    target = Image.new("RGB", frame.size, avg)
                    frame = Image.blend(frame, target, ga / 0.5)
                else:
                    bright = tuple(
                        int(round(c + (255 - c) * GLOW_COLOR_WHITE_MIX))
                        for c in avg)
                    base = Image.new("RGB", frame.size, avg)
                    peak = Image.new("RGB", frame.size, bright)
                    frame = Image.blend(base, peak, (ga - 0.5) / 0.5)
            if ba > 0.0 and black_frame_cached is not None:
                frame = Image.blend(frame, black_frame_cached, ba)
            if text_overlay_windows:
                text_frame, ta = _text_overlay_at(t_v)
                if ta > 0.0 and text_frame is not None:
                    frame = Image.blend(frame, text_frame, ta)
        if debug_active:
            music_t = t_v - music_start_s
            frame = _debug_overlay(frame, music_t, t_v, wa, ba, ta)
        proc.stdin.write(frame.tobytes())
        slide_frame_index[0] += 1
        return frame

    prev_tail = None  # list of PIL.Image with previous image's last K frames
    last_main_frame = None  # final rendered frame of last image (for end screen)

    try:
        if gimmick:
            ctx.log("Rendering gimmick intro...")
            for i, path in enumerate(picks):
                if ctx.cancelled():
                    break
                frame = _gimmick_frame(path, out_w, out_h)
                fb = frame.tobytes()
                for _ in range(gimmick_frames_list[i]):
                    proc.stdin.write(fb)

        def _song_time_prefix():
            """Elapsed time in the song at the start of the current slide,
            formatted as mm:ss.ff. Reads slide_frame_index[0] which is the
            next-to-be-written frame index into the slides portion of the
            pipe, so it's the position at which this slide begins."""
            v = gimmick_duration + slide_frame_index[0] / FPS
            song_s = v - music_start_s
            if song_s < 0:
                return "  --:--.--"
            m = int(song_s // 60)
            r = song_s - m * 60
            return f"{m:>3d}:{r:05.2f}"

        for i, slide in enumerate(slides_seq):
            if ctx.cancelled():
                # User asked to stop. Finish the previous slide's saved
                # tail as solo frames so it ends cleanly (as if it had
                # been the last slide), then stop rendering.
                if prev_tail is not None:
                    for frame in prev_tail:
                        try:
                            written = main_write(frame)
                            last_main_frame = written
                        except BrokenPipeError:
                            break
                    prev_tail = None
                break
            fpi_local = per_slide_frames[i]
            song_prefix = _song_time_prefix()
            hard_cut_in = False  # incoming transition is a hard cut, not a fade
            if slide["kind"] == "image":
                dur_s = slide.get("duration_s")
                is_short = dur_s is not None and dur_s < SHORT_SLIDE_MAX_S
                short_tag = "  [hard cut, static]" if is_short else ""
                ctx.log(f"{song_prefix}  [{i + 1}/{total_slides}] "
                        f"{slide['path'].name} ({fpi_local/FPS:.2f}s)"
                        f"{short_tag}")
                debug_current_slide[0] = (i, "image", slide["path"].name)
                if debug:
                    # Debug mode: skip Ken Burns motion + blurred scene
                    # prep. A static gimmick-style fit is ~10x faster per
                    # frame and lets the debug HUD/timeline stay fluid.
                    # Copy per-call so the shared static isn't mutated by
                    # the debug HUD's in-place drawing.
                    static_img = _gimmick_frame(slide["path"], out_w, out_h)

                    def frame_at(k, _f=static_img):
                        return _f.copy()
                elif is_short:
                    # Too brief for motion to read: freeze the scene at the
                    # centered, unzoomed view (same blurred-bg composition as
                    # the moving slides, just no pan/zoom) and hard-cut in.
                    scene, scene_w, scene_h = _prepare_scene(
                        slide["path"], out_w, out_h)
                    static_img = _kb_frame(scene, out_w, out_h,
                                           scene_w / 2, scene_h / 2, 1.0, 0.0)
                    hard_cut_in = True

                    def frame_at(k, _f=static_img):
                        return _f
                else:
                    scene, scene_w, scene_h = _prepare_scene(
                        slide["path"], out_w, out_h)
                    start_v, end_v = _random_kb_views(
                        out_w, out_h, scene_w, scene_h, kb_strength)

                    def frame_at(k, _s=scene, _sv=start_v, _ev=end_v,
                                 _fpi=fpi_local):
                        return render_local(_s, _sv, _ev, k, _fpi)
            elif slide["kind"] == "title":
                ctx.log(f"{song_prefix}  [{i + 1}/{total_slides}] "
                        f"[title] {slide['label']} ({fpi_local/FPS:.1f}s)")
                debug_current_slide[0] = (i, "title", slide.get("label", ""))
                title_static_frame = slide["frame"]

                def frame_at(k, _f=title_static_frame):
                    return _f
            else:
                wc = len(slide["text"].split())
                # Backdrop = last image before this slide, darkened + desaturated.
                backdrop_path = None
                for prev in reversed(slides_seq[:i]):
                    if prev["kind"] == "image":
                        backdrop_path = prev["path"]
                        break
                ctx.log(f"{song_prefix}  [{i + 1}/{total_slides}] "
                        f"[text] {slide['text']} "
                        f"({wc} word{'s' if wc != 1 else ''}, "
                        f"{fpi_local/FPS:.1f}s)")
                debug_current_slide[0] = (i, "text", slide["text"][:40])
                static_text_frame = _build_text_slide_frame(
                    slide["text"], out_w, out_h,
                    font_path=text_slide_font,
                    backdrop_path=backdrop_path)

                def frame_at(k, _f=static_text_frame):
                    return _f

            # Once we've started a slide we always render it to the end —
            # a cancel is only honoured at the slide-loop boundary above.
            # This gives the user a "stop after this slide" semantic and
            # avoids truncating the video mid-frame.
            if crossfade_frames > 0:
                for k in range(crossfade_frames):
                    new_frame = frame_at(k)
                    if prev_tail is not None:
                        if hard_cut_in:
                            # No blend: show the old picture until the point
                            # where a fade would have crossed 50% (which is
                            # the bar-aligned moment), then snap to the new
                            # one. Keeps the change on the same frame a fade
                            # would land on.
                            past_mid = (k + 1) / (crossfade_frames + 1) >= 0.5
                            out_frame = new_frame if past_mid else prev_tail[k]
                        else:
                            alpha = (k + 1) / (crossfade_frames + 1)
                            out_frame = Image.blend(
                                prev_tail[k], new_frame, alpha)
                    else:
                        out_frame = new_frame
                    written = main_write(out_frame)
                    if i == total_slides - 1:
                        last_main_frame = written

            mid_start = crossfade_frames
            mid_end = fpi_local - (
                crossfade_frames if i < total_slides - 1 else 0)
            for k in range(mid_start, mid_end):
                frame = frame_at(k)
                written = main_write(frame)
                if i == total_slides - 1:
                    last_main_frame = written

            # Tail K frames: saved for next iter; the last slide writes them solo
            if i < total_slides - 1 and crossfade_frames > 0:
                prev_tail = [
                    frame_at(k)
                    for k in range(fpi_local - crossfade_frames, fpi_local)
                ]
            else:
                prev_tail = None

        # End-screen segment: crossfade from the last main frame, then hold.
        if (end_screen_frame is not None
                and last_main_frame is not None
                and not ctx.cancelled()):
            for f in range(end_screen_fade_frames):
                if ctx.cancelled():
                    break
                t = f / end_screen_fade_frames
                blended = Image.blend(last_main_frame, end_screen_frame, t)
                proc.stdin.write(blended.tobytes())
            end_bytes = end_screen_frame.tobytes()
            for _ in range(end_screen_hold_frames):
                if ctx.cancelled():
                    break
                proc.stdin.write(end_bytes)
    except BrokenPipeError:
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")

    if ctx.cancelled():
        ctx.log(f"Stopped by user — partial video saved to {output}")
    else:
        ctx.log(f"Video saved to {output}")

    # Persist the full music/video timeline next to where the app runs, with
    # the source music JSON path so both can be used together for debugging.
    if timeline_lines:
        timeline_path = os.path.abspath("timeline.txt")
        header = [
            f"Music JSON: {bar_data.get('json_path', '?')}",
            f"Video:      {os.path.abspath(output)}",
            "",
        ]
        try:
            with open(timeline_path, "w") as f:
                f.write("\n".join(header + timeline_lines) + "\n")
            ctx.log(f"Timeline written to {timeline_path}")
        except OSError as exc:
            ctx.log(f"Could not write timeline.txt: {exc}")

    return output
