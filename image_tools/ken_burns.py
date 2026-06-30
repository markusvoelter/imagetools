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
KB_END_SCREEN_PADDING_FRAC = 0.08   # padding around end-screen image (frac of width)
KB_END_SCREEN_FADE_S = 0.5          # crossfade from last main frame to end screen
KB_END_SCREEN_HOLD_S = 3.0          # how long the end screen is held
KB_END_SCREEN_BG = (0, 0, 0)        # bg color behind the end-screen image
KB_TITLE_DURATION_S = 3.0           # title visible for this many seconds
KB_TITLE_FADE_S = 0.3               # title fade in/out duration
KB_TITLE_WIDTH_FRAC = 0.75          # title spans this fraction of canvas width
KB_TITLE_LINE_MAX_CHARS = 25        # split over two lines if longer than this
KB_TITLE_SHADOW_OFFSET = (4, 6)     # drop shadow offset in pixels
KB_TITLE_SHADOW_BLUR = 8            # gaussian blur radius for soft shadow
KB_TITLE_SHADOW_ALPHA = 220         # shadow opacity 0..255
KB_TITLE_FONT_CANDIDATES = (
    "Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
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
    canvas with the user's image centered on top, padded, alpha respected."""
    frame = Image.new("RGB", (out_w, out_h), bg_color)
    img = Image.open(end_screen_path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    pad = int(round(out_w * KB_END_SCREEN_PADDING_FRAC))
    avail_w = max(1, out_w - 2 * pad)
    avail_h = max(1, out_h - 2 * pad)
    iw, ih = img.size
    scale = min(avail_w / iw, avail_h / ih)
    new_w = max(1, int(round(iw * scale)))
    new_h = max(1, int(round(ih * scale)))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    x = (out_w - new_w) // 2
    y = (out_h - new_h) // 2
    frame.paste(img, (x, y), img)
    return frame


def _arial_bold_font(size):
    """Load Arial Bold (or closest fallback) at the given pixel size."""
    for candidate in KB_TITLE_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


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


def _measure_text_bbox(text, font):
    """Return (left, top, right, bottom) bbox of `text` (multi-line aware)."""
    measure_img = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(measure_img)
    if "\n" in text:
        return draw.multiline_textbbox((0, 0), text, font=font, align="center")
    return draw.textbbox((0, 0), text, font=font, anchor="lt")


def _find_font_size_for_width(text, target_w, max_size):
    """Binary-search the largest font size where `text` fits in `target_w`."""
    lo, hi = 8, max(8, max_size)
    best = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _arial_bold_font(mid)
        bbox = _measure_text_bbox(text, font)
        text_w = bbox[2] - bbox[0]
        if text_w <= target_w:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _build_title_overlay(text, out_w, out_h):
    """Return an RGBA image (out_w × out_h) with `text` centered, white with a
    soft black drop shadow, font sized so the text is KB_TITLE_WIDTH_FRAC of
    the canvas width. `text` may contain a "\\n" for two-line titles."""
    target_w = out_w * KB_TITLE_WIDTH_FRAC
    size = _find_font_size_for_width(text, target_w, max_size=min(out_w, out_h))
    font = _arial_bold_font(size)

    bbox = _measure_text_bbox(text, font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    pos_x = (out_w - text_w) // 2 - bbox[0]
    pos_y = (out_h - text_h) // 2 - bbox[1]
    is_multiline = "\n" in text

    def _draw(target_image, x, y, fill):
        d = ImageDraw.Draw(target_image)
        if is_multiline:
            d.multiline_text((x, y), text, font=font, fill=fill, align="center")
        else:
            d.text((x, y), text, font=font, fill=fill)

    # Soft shadow on its own layer so we can blur it without smudging the text.
    shadow = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    _draw(shadow,
          pos_x + KB_TITLE_SHADOW_OFFSET[0],
          pos_y + KB_TITLE_SHADOW_OFFSET[1],
          fill=(0, 0, 0, KB_TITLE_SHADOW_ALPHA))
    shadow = shadow.filter(ImageFilter.GaussianBlur(KB_TITLE_SHADOW_BLUR))

    _draw(shadow, pos_x, pos_y, fill=(255, 255, 255, 255))
    return shadow


def _title_alpha(idx, total_frames, fade_in_frames, fade_out_frames):
    """Linear fade-in then fade-out alpha (0..1) for the title overlay."""
    if total_frames <= 0:
        return 0.0
    if idx < fade_in_frames:
        return (idx + 1) / max(1, fade_in_frames)
    tail_start = total_frames - fade_out_frames
    if idx >= tail_start:
        return max(0.0, (total_frames - idx) / max(1, fade_out_frames))
    return 1.0


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
        title=None, output=None, random_order=True, ctx=None):
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
    title               optional title string overlaid on the first
                        KB_TITLE_DURATION_S seconds of the main sequence
                        (white, Arial Bold, centered, soft black drop shadow)
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
    main_seconds = (n * duration_per_image
                    - (n - 1) * (crossfade_frames / FPS))

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

    title_overlay = None
    title_total_frames = 0
    title_fade_in_frames = 0
    title_fade_out_frames = 0
    if title and title.strip():
        title_total_frames = max(2, int(round(KB_TITLE_DURATION_S * FPS)))
        title_fade_in_frames = max(1, int(round(KB_TITLE_FADE_S * FPS)))
        title_fade_out_frames = max(1, int(round(KB_TITLE_FADE_S * FPS)))
        title_text = _split_title_lines(title.strip())
        title_overlay = _build_title_overlay(title_text, out_w, out_h)
        title_display = title_text.replace("\n", " / ")
        ctx.log(f"Title: \"{title_display}\" "
                f"({title_total_frames/FPS:.1f}s, "
                f"fade {title_fade_in_frames/FPS:.2f}s in/out)")
    ctx.log(f"Per image: {duration_per_image:.2f}s "
            f"({frames_per_image} frames), "
            f"crossfade {crossfade_frames} frames "
            f"({crossfade_frames/FPS:.2f}s)")
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

    # main_frame_idx is a 1-element list so the inner closure can mutate it
    # without needing `nonlocal` in Python 3.
    main_frame_idx = [0]

    def main_write(frame):
        """Optionally overlay the title, then write to ffmpeg. Returns the
        possibly-overlaid frame so callers can also use it (e.g. last_main_frame)."""
        if title_overlay is not None and main_frame_idx[0] < title_total_frames:
            alpha = _title_alpha(main_frame_idx[0], title_total_frames,
                                 title_fade_in_frames, title_fade_out_frames)
            if alpha > 0:
                frame = _composite_title_onto_frame(frame, title_overlay, alpha)
        proc.stdin.write(frame.tobytes())
        main_frame_idx[0] += 1
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

        for i, path in enumerate(picks):
            if ctx.cancelled():
                break
            ctx.log(f"  [{i + 1}/{n}] {path.name}")
            scene, scene_w, scene_h = _prepare_scene(path, out_w, out_h)
            start_v, end_v = _random_kb_views(
                out_w, out_h, scene_w, scene_h, kb_strength)

            # First K frames: crossfade with prev tail, else write solo
            if crossfade_frames > 0:
                for k in range(crossfade_frames):
                    if ctx.cancelled():
                        break
                    new_frame = render_local(scene, start_v, end_v, k)
                    if prev_tail is not None:
                        alpha = (k + 1) / (crossfade_frames + 1)
                        out_frame = Image.blend(prev_tail[k], new_frame, alpha)
                    else:
                        out_frame = new_frame
                    written = main_write(out_frame)
                    if i == n - 1:
                        last_main_frame = written

            # Middle frames: solo
            mid_start = crossfade_frames
            mid_end = frames_per_image - (crossfade_frames if i < n - 1 else 0)
            for k in range(mid_start, mid_end):
                if ctx.cancelled():
                    break
                frame = render_local(scene, start_v, end_v, k)
                written = main_write(frame)
                if i == n - 1:
                    last_main_frame = written

            # Tail K frames: saved for next iter; the last image writes them solo
            if i < n - 1 and crossfade_frames > 0:
                prev_tail = [
                    render_local(scene, start_v, end_v, k)
                    for k in range(frames_per_image - crossfade_frames,
                                   frames_per_image)
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
