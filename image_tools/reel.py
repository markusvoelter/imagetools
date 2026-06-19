"""Instagram Reel (9:16 vertical) slideshow video from a folder of images."""

import os
import random
import subprocess
from pathlib import Path

from PIL import Image

from . import OUTPUT_DIR, RunContext, ensure_output_dir


WIDTH, HEIGHT = 1080, 1920
FPS = 30
DEFAULT_INTERVAL = 2.0
FADE_RATIO = 0.25
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.wav', '.flac', '.aac', '.ogg', '.opus'}
PADDING = 20
MAX_CROP = 0.10
DEFAULT_BG = (0, 0, 0)


def load_image_paths(folder):
    folder = Path(folder)
    paths = sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not paths:
        raise RuntimeError(f"No images found in {folder}")
    return paths


def get_region_targets(n):
    if n == 1:
        regions = [(0, 0, WIDTH, HEIGHT)]
    elif n == 2:
        half = HEIGHT // 2
        regions = [
            (0, 0, WIDTH, half - PADDING // 2),
            (0, half + PADDING // 2, WIDTH, HEIGHT),
        ]
    else:
        third = HEIGHT // 3
        regions = [
            (0, 0, WIDTH, third - PADDING),
            (0, third + PADDING // 2, WIDTH, 2 * third - PADDING // 2),
            (0, 2 * third + PADDING, WIDTH, HEIGHT),
        ]
    targets = []
    for rx, ry, rw, rh in regions:
        targets.append(((rw - rx) - 2 * PADDING, (rh - ry) - 2 * PADDING))
    return targets


def cropped_aspect(img_aspect, target_w, target_h):
    target_aspect = target_w / target_h
    if img_aspect > target_aspect:
        needed = 1 - target_aspect / img_aspect
        crop = min(needed, MAX_CROP)
        return img_aspect * (1 - crop)
    else:
        needed = 1 - img_aspect / target_aspect
        crop = min(needed, MAX_CROP)
        return img_aspect / (1 - crop)


def fill_ratio(img_aspect, target_w, target_h):
    effective = cropped_aspect(img_aspect, target_w, target_h)
    target_aspect = target_w / target_h
    if effective > target_aspect:
        used_w = target_w
        used_h = target_w / effective
    else:
        used_h = target_h
        used_w = target_h * effective
    return (used_w * used_h) / (target_w * target_h)


def group_into_slides(image_paths, ctx):
    ctx.log("Scanning aspect ratios...")
    aspects = []
    for p in image_paths:
        with Image.open(p) as img:
            aspects.append(img.width / img.height)

    indexed = sorted(range(len(image_paths)), key=lambda i: aspects[i])
    sorted_aspects = [aspects[i] for i in indexed]
    sorted_paths = [image_paths[i] for i in indexed]

    single_target = get_region_targets(1)[0]
    half_target = get_region_targets(2)[0]
    third_target = get_region_targets(3)[0]

    def score_single(i):
        return fill_ratio(sorted_aspects[i], *single_target)

    def score_half(i):
        return fill_ratio(sorted_aspects[i], *half_target)

    def score_third(i):
        return fill_ratio(sorted_aspects[i], *third_target)

    n = len(sorted_paths)
    dp = [0.0] * (n + 1)
    choice = [0] * (n + 1)

    for i in range(1, n + 1):
        dp[i] = dp[i - 1] + score_single(i - 1)
        choice[i] = 1
        if i >= 2:
            score = dp[i - 2] + score_half(i - 2) + score_half(i - 1)
            if score > dp[i]:
                dp[i] = score
                choice[i] = 2
        if i >= 3:
            score = dp[i - 3] + score_third(i - 3) + score_third(i - 2) + score_third(i - 1)
            if score > dp[i]:
                dp[i] = score
                choice[i] = 3

    slides = []
    i = n
    while i > 0:
        c = choice[i]
        slides.append(sorted_paths[i - c:i])
        i -= c
    slides.reverse()
    random.shuffle(slides)

    avg_fill = dp[n] / n
    ctx.log(f"  Average fill ratio: {avg_fill:.1%}")
    return slides


def fit_image(img, target_w, target_h):
    img_aspect = img.width / img.height
    target_aspect = target_w / target_h

    if img_aspect > target_aspect:
        needed = 1 - target_aspect / img_aspect
        crop = min(needed, MAX_CROP)
        if crop > 0:
            crop_px = int(img.width * crop)
            left = crop_px // 2
            right = img.width - (crop_px - left)
            img = img.crop((left, 0, right, img.height))
    else:
        needed = 1 - img_aspect / target_aspect
        crop = min(needed, MAX_CROP)
        if crop > 0:
            crop_px = int(img.height * crop)
            top = crop_px // 2
            bottom = img.height - (crop_px - top)
            img = img.crop((0, top, img.width, bottom))

    ratio = img.width / img.height
    target_ratio = target_w / target_h
    if ratio > target_ratio:
        new_w = target_w
        new_h = int(target_w / ratio)
    else:
        new_h = target_h
        new_w = int(target_h * ratio)
    return img.resize((new_w, new_h), Image.LANCZOS)


def prepare_slide_images(slide_paths):
    n = len(slide_paths)
    if n == 1:
        regions = [(0, 0, WIDTH, HEIGHT)]
    elif n == 2:
        half = HEIGHT // 2
        regions = [
            (0, 0, WIDTH, half - PADDING // 2),
            (0, half + PADDING // 2, WIDTH, HEIGHT),
        ]
    else:
        third = HEIGHT // 3
        regions = [
            (0, 0, WIDTH, third - PADDING),
            (0, third + PADDING // 2, WIDTH, 2 * third - PADDING // 2),
            (0, 2 * third + PADDING, WIDTH, HEIGHT),
        ]

    images = []
    for path, (rx, ry, rw, rh) in zip(slide_paths, regions):
        img = Image.open(path).convert('RGBA')
        region_w = rw - rx
        region_h = rh - ry
        fitted = fit_image(img, region_w - 2 * PADDING, region_h - 2 * PADDING)
        x = rx + (region_w - fitted.width) // 2
        y = ry + (region_h - fitted.height) // 2
        images.append((fitted, x, y))
    return images


def render_first_k_slots(slide_images, k, bg):
    """Compose a frame showing the first k slots of slide_images (rest left as bg)."""
    frame = Image.new('RGB', (WIDTH, HEIGHT), bg)
    for j in range(k):
        img, x, y = slide_images[j]
        frame.paste(img, (x, y), img)
    return frame


def render_swap_at(prev_slide, new_slide, swap_count, n, bg):
    """Compose: first `swap_count` slots come from new_slide, the remaining from prev_slide.

    Assumes prev_slide and new_slide use the same slot layout (same n) so the
    slot rectangles match. Used for per-slot crossfade between consecutive
    same-layout slides.
    """
    frame = Image.new('RGB', (WIDTH, HEIGHT), bg)
    for j in range(n):
        src = new_slide if j < swap_count else prev_slide
        img, x, y = src[j]
        frame.paste(img, (x, y), img)
    return frame


def _count_transitions(all_slides):
    """Total number of crossfade transitions, including the closing fade-out."""
    if not all_slides:
        return 0
    total = 0
    n_slides = len(all_slides)
    for slide_idx, slide_imgs in enumerate(all_slides):
        n = len(slide_imgs)
        total += n  # reveals (first slide) / per-slot swaps / build-up — all yield N transitions
        if slide_idx == n_slides - 1:
            total += 1  # closing fade-out
    return total


def _iter_transitions(all_slides, bg_color):
    """Yield (slide_idx, start_frame, end_frame) tuples for each transition.

    Encodes the same first-slide-build-up / same-layout-swap / layout-change
    semantics as before, but separated from the timing so rendering can be
    driven externally by a schedule.
    """
    bg_frame = Image.new('RGB', (WIDTH, HEIGHT), bg_color)
    prev_slide_imgs = None
    prev_n = 0
    n_slides = len(all_slides)

    for slide_idx, slide_imgs in enumerate(all_slides):
        n = len(slide_imgs)
        is_first = (slide_idx == 0)
        is_last = (slide_idx == n_slides - 1)
        same_layout = (prev_slide_imgs is not None and prev_n == n)

        if is_first:
            for k in range(n):
                start_frame = (bg_frame if k == 0
                               else render_first_k_slots(slide_imgs, k, bg_color))
                end_frame = render_first_k_slots(slide_imgs, k + 1, bg_color)
                yield (slide_idx, start_frame, end_frame)
        elif same_layout:
            for slot_idx in range(n):
                start_frame = render_swap_at(prev_slide_imgs, slide_imgs,
                                             slot_idx, n, bg_color)
                end_frame = render_swap_at(prev_slide_imgs, slide_imgs,
                                           slot_idx + 1, n, bg_color)
                yield (slide_idx, start_frame, end_frame)
        else:
            old_full = render_first_k_slots(prev_slide_imgs, prev_n, bg_color)
            new_first = render_first_k_slots(slide_imgs, 1, bg_color)
            yield (slide_idx, old_full, new_first)
            for k in range(1, n):
                start_frame = render_first_k_slots(slide_imgs, k, bg_color)
                end_frame = render_first_k_slots(slide_imgs, k + 1, bg_color)
                yield (slide_idx, start_frame, end_frame)

        if is_last:
            full_frame = render_first_k_slots(slide_imgs, n, bg_color)
            yield (slide_idx, full_frame, bg_frame)

        prev_slide_imgs = slide_imgs
        prev_n = n


def _fixed_schedule(num_transitions, interval):
    """Fade-start times spaced by `interval` seconds, first fade starts at 0."""
    return [i * interval for i in range(num_transitions)]


def _compute_beat_schedule(audio_track, num_transitions, beats_per_transition,
                           fade_seconds, ctx):
    """Fade-start times so each fade ends exactly on a beat.

    Returns None and logs a reason if librosa is missing, beat detection fails,
    or the track has fewer beats than the reel needs — caller falls back to
    _fixed_schedule.
    """
    try:
        import librosa
    except ImportError:
        ctx.log("librosa not installed; using fixed interval timing.")
        return None

    ctx.log(f"Analyzing audio for beats "
            f"(snapping every {beats_per_transition} beat(s))...")
    try:
        y, sr = librosa.load(audio_track, mono=True)
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units='time')
        try:
            tempo_val = float(tempo)
        except TypeError:
            tempo_val = float(tempo[0])
        ctx.log(f"Detected tempo: {tempo_val:.1f} BPM, {len(beats)} beats")
    except Exception as e:
        ctx.log(f"Audio analysis failed: {e}; using fixed interval timing.")
        return None

    beat_times = [float(t) for t in beats]
    if not beat_times:
        ctx.log("No beats detected; using fixed interval timing.")
        return None

    target_indices = [i * beats_per_transition for i in range(num_transitions)]
    if target_indices[-1] >= len(beat_times):
        ctx.log(f"Audio has only {len(beat_times)} beats; need "
                f"{target_indices[-1] + 1}. Using fixed interval timing.")
        return None

    schedule = []
    for idx in target_indices:
        end_time = beat_times[idx]
        start_time = max(0.0, end_time - fade_seconds)
        schedule.append(start_time)
    return schedule


def _pick_audio_track(music_folder, ctx):
    """Return absolute path to a randomly chosen audio file in music_folder,
    or None if the folder is empty / has no audio files."""
    music_folder = os.path.abspath(music_folder)
    if not os.path.isdir(music_folder):
        raise ValueError(f"Music folder not a directory: {music_folder}")
    audio_files = []
    for ext in AUDIO_EXTENSIONS:
        audio_files.extend(Path(music_folder).glob(f"*{ext}"))
        audio_files.extend(Path(music_folder).glob(f"*{ext.upper()}"))
    audio_files = sorted(set(audio_files))
    if not audio_files:
        ctx.log(f"No audio files in {music_folder}; video will be silent.")
        return None
    return str(random.choice(audio_files))


def run(*, folder, interval=DEFAULT_INTERVAL, output=None, bg=None,
        music_folder=None, beats_per_transition=None, ctx=None):
    """Build the reel.

    folder                absolute path to the folder of source images
    interval              seconds per image transition (used when not beat-snapping)
    output                absolute output .mp4 path; auto in OUTPUT_DIR if None
    bg                    background color as "#rrggbb" hex; defaults to black
    music_folder          if set, pick one audio file at random as background
    beats_per_transition  if music is loaded and this is > 0, snap each fade
                          to land on every Nth beat (e.g. 4 = once per bar)
    ctx                   RunContext
    """
    if ctx is None:
        ctx = RunContext()

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    if bg:
        h = bg.lstrip("#")
        bg_color = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    else:
        bg_color = DEFAULT_BG

    interval = float(interval)
    folder_name = Path(folder).name

    if output is None:
        ensure_output_dir()
        output = os.path.join(OUTPUT_DIR, f"{folder_name}.mp4")
    else:
        output = os.path.abspath(output)
        os.makedirs(os.path.dirname(output), exist_ok=True)

    fade_seconds = interval * FADE_RATIO

    image_paths = load_image_paths(folder)
    slides = group_into_slides(image_paths, ctx)

    ctx.log(f"Found {len(image_paths)} images, grouped into {len(slides)} slides")
    for i, s in enumerate(slides):
        ctx.log(f"  Slide {i + 1}: {len(s)} image(s)")

    all_slides = [prepare_slide_images(s) for s in slides]
    num_transitions = _count_transitions(all_slides)

    audio_track = None
    if music_folder:
        audio_track = _pick_audio_track(music_folder, ctx)
        if audio_track:
            ctx.log(f"Audio track: {audio_track}")

    schedule = None
    if audio_track and beats_per_transition and int(beats_per_transition) > 0:
        schedule = _compute_beat_schedule(
            audio_track, num_transitions, int(beats_per_transition),
            fade_seconds, ctx,
        )
    if schedule is None:
        schedule = _fixed_schedule(num_transitions, interval)
        ctx.log(f"Timing: fixed interval {interval}s "
                f"(fade {fade_seconds:.2f}s per transition)")
    else:
        ctx.log(f"Timing: beat-snapped, {len(schedule)} transitions "
                f"(fade {fade_seconds:.2f}s)")

    video_duration = schedule[-1] + fade_seconds if schedule else 0.0
    ctx.log(f"Video duration: ~{video_duration:.2f}s")

    cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo',
        '-vcodec', 'rawvideo',
        '-s', f'{WIDTH}x{HEIGHT}',
        '-pix_fmt', 'rgb24',
        '-r', str(FPS),
        '-i', '-',
    ]
    if audio_track:
        audio_fade_start = max(0.0, video_duration - fade_seconds)
        cmd += [
            '-i', audio_track,
            '-map', '0:v', '-map', '1:a',
            '-c:a', 'aac', '-b:a', '192k',
            '-af', f'afade=t=out:st={audio_fade_start:.3f}:d={fade_seconds:.3f}',
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

    try:
        prev_fade_end_time = 0.0
        last_slide_logged = -1
        transitions = _iter_transitions(all_slides, bg_color)
        for i, (slide_idx, start_frame, end_frame) in enumerate(transitions):
            if ctx.cancelled():
                break

            if slide_idx != last_slide_logged:
                ctx.log(f"Rendering slide {slide_idx + 1}/{len(all_slides)}...")
                last_slide_logged = slide_idx

            fade_start_time = schedule[i]
            if i + 1 < len(schedule):
                max_fade = max(1.0 / FPS, schedule[i + 1] - fade_start_time)
                fade_dur = min(fade_seconds, max_fade)
            else:
                fade_dur = fade_seconds

            hold_dur = max(0.0, fade_start_time - prev_fade_end_time)
            hold_count = int(round(hold_dur * FPS))
            fade_count = max(1, int(round(fade_dur * FPS)))

            start_bytes = start_frame.tobytes()
            for _ in range(hold_count):
                proc.stdin.write(start_bytes)
            for f in range(fade_count):
                t = f / fade_count
                frame = Image.blend(start_frame, end_frame, t)
                proc.stdin.write(frame.tobytes())

            prev_fade_end_time = fade_start_time + fade_dur
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
