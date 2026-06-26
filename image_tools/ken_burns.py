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

from PIL import Image, ImageFilter

from . import OUTPUT_DIR, RunContext, ensure_output_dir


FPS = 30
DEFAULT_DURATION = 4.0
DEFAULT_KB_STRENGTH = 0.5     # 0..1
MAX_ZOOM_AT_FULL_STRENGTH = 0.5  # strength=1.0 → up to 1.5x zoom
MAX_ROTATION_DEG = 7.5        # strength=1.0 → rotations sampled in ±MAX_ROTATION_DEG
BLUR_RADIUS = 60              # heavy blur radius for the background fill
CROSSFADE_S = 0.3
KB_AUDIO_FADE_OUT_S = 1.5     # tail fade applied to the music
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


def _collect_images(folder, orientation, target_count=None, ctx=None):
    """Return image paths matching the requested orientation, in random order.

    The file list is shuffled before scanning, then images are opened one at a
    time (just enough to read dimensions). If `target_count` is given, scanning
    stops as soon as that many matching images are found — so for a small
    target in a huge folder we don't open thousands of files unnecessarily.
    Yields progress logs and honors cancellation via `ctx`.
    """
    folder = Path(folder)
    files = [p for p in folder.iterdir()
             if p.suffix.lower() in IMAGE_EXTENSIONS]
    if not files:
        return []
    random.shuffle(files)

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
        music=None, output=None, ctx=None):
    """Render the Ken Burns video.

    folder              folder of source images
    num_images          how many images to pick (capped at available)
    aspect              "16:9" or "9:16"; selects orientation filter too
    duration_per_image  seconds per image (independent of crossfade)
    kb_strength         Ken Burns intensity 0..1 (0 = static, 1 = up to 1.5x zoom)
    music               either an audio file (used directly) or a folder
                        (random audio file inside is picked)
    output              .mp4 path; auto-named in OUTPUT_DIR if None
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
                                 target_count=num_images, ctx=ctx)
    ctx.log(f"Found {len(candidates)} {orientation} image(s) in {folder}")
    if not candidates:
        raise RuntimeError(f"No {orientation} images in {folder}")

    n = min(num_images, len(candidates))
    if n < num_images:
        ctx.log(f"Only {n} candidate(s) available; reducing from {num_images}.")
    # Candidates already come back shuffled from _collect_images, so take the
    # first n (random.sample would be equivalent but redundant here).
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
    total_seconds = (n * duration_per_image
                     - (n - 1) * (crossfade_frames / FPS))
    ctx.log(f"Per image: {duration_per_image:.2f}s "
            f"({frames_per_image} frames), "
            f"crossfade {crossfade_frames} frames "
            f"({crossfade_frames/FPS:.2f}s)")
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
    if audio_track:
        audio_fade_start = max(0.0, total_seconds - KB_AUDIO_FADE_OUT_S)
        cmd += [
            '-i', audio_track,
            '-map', '0:v', '-map', '1:a',
            '-c:a', 'aac', '-b:a', '192k',
            '-af', f'afade=t=out:st={audio_fade_start:.3f}:d={KB_AUDIO_FADE_OUT_S:.3f}',
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

    prev_tail = None  # list of PIL.Image with previous image's last K frames

    try:
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
                    proc.stdin.write(out_frame.tobytes())

            # Middle frames: solo
            mid_start = crossfade_frames
            mid_end = frames_per_image - (crossfade_frames if i < n - 1 else 0)
            for k in range(mid_start, mid_end):
                if ctx.cancelled():
                    break
                proc.stdin.write(
                    render_local(scene, start_v, end_v, k).tobytes())

            # Tail K frames: saved for next iter; the last image writes them solo
            if i < n - 1 and crossfade_frames > 0:
                prev_tail = [
                    render_local(scene, start_v, end_v, k)
                    for k in range(frames_per_image - crossfade_frames,
                                   frames_per_image)
                ]
            else:
                prev_tail = None
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
