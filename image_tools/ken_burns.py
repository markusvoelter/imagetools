"""Generate a Ken Burns-style video from random images in a folder.

The output is a 16:9 (1920x1080) or 9:16 (1080x1920) MP4. Source images are
filtered to landscape or portrait orientation accordingly. Each image is shown
with a smooth zoom/pan ("Ken Burns"); when the image's aspect doesn't fit the
output frame, the bars are filled with a very heavy blur of the same image so
no flat color shows through. Consecutive images are stitched with a brief
crossfade.
"""

import math
import os
import random
import re
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import OUTPUT_DIR, RunContext, ensure_output_dir


FPS = 30
DEFAULT_DURATION = 4.0
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
KB_TEXT_SLIDE_BG_TOP = (0, 0, 0)    # gradient top: 100% black
KB_TEXT_SLIDE_BG_BOTTOM = (38, 38, 38)  # gradient bottom: 85% black tint
GIMMICK_FRAME_SECONDS = 0.05  # each gimmick image is shown for this many seconds
GIMMICK_CLICK_FREQ_HZ = 700   # carrier (tonal) frequency of the per-image "click"
GIMMICK_CLICK_DECAY_S = 0.003 # exponential decay time constant of the click
GIMMICK_CLICK_AMP = 0.6       # peak amplitude of the first click
GIMMICK_CLICK_NOISE_WEIGHT = 0.75  # noise fraction of the click waveform
GIMMICK_CLICK_TONE_WEIGHT = 0.25   # low-freq tonal fraction of the click
GIMMICK_CLICK_FINAL_AMP_FRAC = 0.0  # last click is this fraction of the first
GIMMICK_CLICK_MIX_WEIGHT = 0.7  # click track weight when mixing with music
GIMMICK_MUSIC_FADE_IN_START = 0.5  # fraction of gimmick before music fade-in begins
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
                              title_font_path=None, subtitle_font_path=None):
    """Render a static RGB frame for the opening title slide.

    Background: same vertical gradient as text slides.
    Foreground: `title_text` (Arial Bold default, white) with optional
    `subtitle_text` (regular Arial, light grey) below, both with the soft
    grey drop shadow."""
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


def _build_text_slide_frame(text, out_w, out_h, font_path=None):
    """Render a static RGB frame for an interspersed text slide.

    Background: vertical gradient from KB_TEXT_SLIDE_BG_TOP to BG_BOTTOM.
    Text: centered, Arial Bold (or `font_path`), white with soft grey drop
    shadow, sized to fit KB_TEXT_SLIDE_WIDTH_FRAC of canvas width (more
    padding than the title), line height multiplied by
    KB_TEXT_SLIDE_LINE_SPACING. Sentences are placed on separate lines and
    long sentences wrap greedily at KB_TEXT_SLIDE_LINE_MAX_CHARS.
    """
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
        output=None, random_order=True, ctx=None):
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
    output              .mp4 path; auto-named in OUTPUT_DIR if None
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
    # Candidates already come back in the requested order from
    # _collect_images (shuffled or alphabetical), so take the first n.
    picks = candidates[:n]

    # Normalise text_slides: drop blank lines, then cap so at least one image
    # remains in every chunk (no text at start/end).
    text_slides_list = [t.strip() for t in (text_slides or []) if t and t.strip()]
    max_text = max(0, n - 1)
    if len(text_slides_list) > max_text:
        ctx.log(f"Warning: {len(text_slides_list)} text slide(s) requested but "
                f"only {n} image(s); truncating to {max_text}.")
        text_slides_list = text_slides_list[:max_text]
    k_text = len(text_slides_list)
    # Insertion points: number of images preceding each text slide. Even
    # distribution divides the n images into k_text+1 chunks of ~equal size.
    text_positions = [round((j + 1) * n / (k_text + 1)) for j in range(k_text)]
    slides_seq = []
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
        title_slide_frame = _build_title_slide_frame(
            title_text_wrapped, subtitle_text_wrapped, out_w, out_h,
            title_font_path=title_font,
            subtitle_font_path=subtitle_font)
        title_label = title_text_wrapped.replace("\n", " / ")
        slides_seq.insert(0, {
            "kind": "title",
            "frame": title_slide_frame,
            "label": title_label,
            "subtitle": subtitle_text_wrapped,
        })
    total_slides = len(slides_seq)

    if output is None:
        ensure_output_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = os.path.join(OUTPUT_DIR,
                              f"kenburns_{aspect.replace(':', 'x')}_{ts}.mp4")
    else:
        output = os.path.abspath(output)
        os.makedirs(os.path.dirname(output), exist_ok=True)

    frames_per_image = max(2, int(round(duration_per_image * FPS)))
    crossfade_frames = min(int(round(CROSSFADE_S * FPS)),
                           frames_per_image // 2)

    def _slide_frames(slide):
        if slide["kind"] == "text":
            return max(2, int(round(
                frames_per_image * _text_slide_factor(slide["text"]))))
        if slide["kind"] == "title":
            return max(2, int(round(KB_TITLE_DURATION_S * FPS)))
        return frames_per_image

    total_main_frames = (sum(_slide_frames(s) for s in slides_seq)
                         - (total_slides - 1) * crossfade_frames)
    main_seconds = total_main_frames / FPS

    if gimmick:
        gimmick_frames_per_image = max(1, int(round(GIMMICK_FRAME_SECONDS * FPS)))
        gimmick_total_frames = n * gimmick_frames_per_image
        gimmick_duration = gimmick_total_frames / FPS
        gimmick_period = gimmick_frames_per_image / FPS
    else:
        gimmick_frames_per_image = 0
        gimmick_total_frames = 0
        gimmick_duration = 0.0
        gimmick_period = 0.0

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

    if title_label:
        ctx.log(f"Title slide: \"{title_label}\" "
                f"({KB_TITLE_DURATION_S:.1f}s, gradient bg)")
        sub_text = slides_seq[0].get("subtitle", "")
        if sub_text:
            ctx.log(f"Subtitle: \"{sub_text.replace(chr(10), ' / ')}\"")
    ctx.log(f"Per image: {duration_per_image:.2f}s "
            f"({frames_per_image} frames), "
            f"crossfade {crossfade_frames} frames "
            f"({crossfade_frames/FPS:.2f}s)")
    if k_text:
        ctx.log(f"Text slides: {k_text} interspersed at image positions "
                f"{text_positions} of {n}; "
                f"hold scales {TEXT_SLIDE_MIN_FACTOR:g}x at ≤{TEXT_SLIDE_MIN_WORDS} "
                f"words up to {TEXT_SLIDE_MAX_FACTOR:g}x at "
                f"≥{TEXT_SLIDE_MAX_WORDS} words")
    if gimmick:
        ctx.log(f"Gimmick intro: {gimmick_frames_per_image} frame(s) per image "
                f"× {n} = {gimmick_duration:.2f}s "
                f"(music fades in, spoke-click on each flip)")
    s_clamped = max(0.0, min(1.0, kb_strength))
    ctx.log(f"Ken Burns strength: {kb_strength:.2f} "
            f"(max zoom {1.0 + s_clamped * MAX_ZOOM_AT_FULL_STRENGTH:.2f}x, "
            f"rotation up to ±{MAX_ROTATION_DEG * s_clamped:.1f}°)")
    ctx.log(f"Total length: ~{total_seconds:.1f}s")

    audio_track = None
    if music:
        audio_track = _resolve_audio_track(music, ctx)
        if audio_track:
            ctx.log(f"Audio track: {audio_track}")

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

    if audio_track:
        extra_inputs.extend(['-i', audio_track])
        music_chain = ['aresample=44100']
        if gimmick and gimmick_duration > 0:
            # Music stays silent for the first half of the gimmick, then fades
            # in over the second half so it reaches full volume right at the
            # end of the intro.
            fade_in_start = gimmick_duration * GIMMICK_MUSIC_FADE_IN_START
            fade_in_dur = gimmick_duration - fade_in_start
            music_chain.append(
                f"afade=t=in:st={fade_in_start:.3f}:d={fade_in_dur:.3f}"
            )
        audio_fade_start = max(0.0, total_seconds - KB_AUDIO_FADE_OUT_S)
        music_chain.append(
            f"afade=t=out:st={audio_fade_start:.3f}:d={KB_AUDIO_FADE_OUT_S:.3f}"
        )
        filter_complex_parts.append(f"[1:a]{','.join(music_chain)}[music]")

    if gimmick and gimmick_duration > 0 and gimmick_period > 0:
        # Damped impulse at the start of every gimmick interval — a low-freq
        # tonal "thump" mixed with white noise gives a darker, spoke-like
        # click rather than a sine beep. Subsequent clicks ramp down linearly
        # so the last one is GIMMICK_CLICK_FINAL_AMP_FRAC of the first.
        # Silent after the gimmick window ends.
        tau = f"mod(t,{gimmick_period:.4f})"
        envelope = f"{GIMMICK_CLICK_AMP}*exp(-{tau}/{GIMMICK_CLICK_DECAY_S})"
        waveform = (
            f"((2*random(0)-1)*{GIMMICK_CLICK_NOISE_WEIGHT}"
            f"+cos(2*PI*{GIMMICK_CLICK_FREQ_HZ}*{tau})*{GIMMICK_CLICK_TONE_WEIGHT})"
        )
        final_frac = GIMMICK_CLICK_FINAL_AMP_FRAC
        delta = 1.0 - final_frac
        if n > 1:
            ramp_span = (n - 1) * gimmick_period
            ramp_expr = (f"max({final_frac:.4f},"
                         f"1-{delta:.4f}*t/{ramp_span:.4f})")
        else:
            ramp_expr = "1"
        click_expr = (
            f"if(lt(t,{gimmick_duration:.3f}),"
            f"{envelope}*{waveform}*{ramp_expr},0)"
        )
        filter_complex_parts.append(
            f"aevalsrc=exprs='{click_expr}|{click_expr}':"
            f"d={total_seconds:.3f}:s=44100[clicks]"
        )

    has_music = audio_track is not None
    has_clicks = gimmick and gimmick_duration > 0 and gimmick_period > 0

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
    cmd += [
        '-vcodec', 'libx264',
        '-pix_fmt', 'yuv420p',
        '-preset', 'medium',
        '-crf', '18',
        output,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    ctx.register_process(proc)

    def render_local(scene, start_v, end_v, f):
        t = f / max(1, frames_per_image - 1)
        te = 3 * t * t - 2 * t * t * t   # smoothstep
        cx = start_v[0] + (end_v[0] - start_v[0]) * te
        cy = start_v[1] + (end_v[1] - start_v[1]) * te
        scale = start_v[2] + (end_v[2] - start_v[2]) * te
        rot = start_v[3] + (end_v[3] - start_v[3]) * te
        return _kb_frame(scene, out_w, out_h, cx, cy, scale, rot)

    def main_write(frame):
        """Write a single RGB frame to ffmpeg and return it (so callers can
        capture it, e.g. for `last_main_frame`)."""
        proc.stdin.write(frame.tobytes())
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
                for _ in range(gimmick_frames_per_image):
                    proc.stdin.write(fb)

        for i, slide in enumerate(slides_seq):
            if ctx.cancelled():
                break
            fpi_local = _slide_frames(slide)
            if slide["kind"] == "image":
                ctx.log(f"  [{i + 1}/{total_slides}] {slide['path'].name}")
                scene, scene_w, scene_h = _prepare_scene(
                    slide["path"], out_w, out_h)
                start_v, end_v = _random_kb_views(
                    out_w, out_h, scene_w, scene_h, kb_strength)

                def frame_at(k, _s=scene, _sv=start_v, _ev=end_v):
                    return render_local(_s, _sv, _ev, k)
            elif slide["kind"] == "title":
                ctx.log(f"  [{i + 1}/{total_slides}] [title] {slide['label']} "
                        f"({fpi_local/FPS:.1f}s)")
                title_static_frame = slide["frame"]

                def frame_at(k, _f=title_static_frame):
                    return _f
            else:
                wc = len(slide["text"].split())
                ctx.log(f"  [{i + 1}/{total_slides}] [text] {slide['text']} "
                        f"({wc} word{'s' if wc != 1 else ''}, "
                        f"{fpi_local/FPS:.1f}s)")
                static_text_frame = _build_text_slide_frame(
                    slide["text"], out_w, out_h,
                    font_path=text_slide_font)

                def frame_at(k, _f=static_text_frame):
                    return _f

            # First K frames: crossfade with prev tail, else write solo
            if crossfade_frames > 0:
                for k in range(crossfade_frames):
                    if ctx.cancelled():
                        break
                    new_frame = frame_at(k)
                    if prev_tail is not None:
                        alpha = (k + 1) / (crossfade_frames + 1)
                        out_frame = Image.blend(prev_tail[k], new_frame, alpha)
                    else:
                        out_frame = new_frame
                    written = main_write(out_frame)
                    if i == total_slides - 1:
                        last_main_frame = written

            # Middle frames: solo
            mid_start = crossfade_frames
            mid_end = fpi_local - (
                crossfade_frames if i < total_slides - 1 else 0)
            for k in range(mid_start, mid_end):
                if ctx.cancelled():
                    break
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

    if ctx.cancelled():
        ctx.log("Cancelled.")
        return None
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")

    ctx.log(f"Video saved to {output}")
    return output
