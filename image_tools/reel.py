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
BORDER_FRAC = 0.025                    # uniform outer border and inter-image gap
BORDER_PX = int(round(WIDTH * BORDER_FRAC))
GAP_PX = BORDER_PX
DEFAULT_BG = (0, 0, 0)
LANDSCAPE_AR_THRESHOLD = 1.0           # AR > this → forced into 3-up slides
STRICT_16_9 = 16 / 9
STRICT_16_9_TOLERANCE = 0.005          # AR within this of 16/9 counts as "exactly 16:9"
END_SCREEN_PADDING_FRAC = 0.08   # padding around the end-screen image (frac of width)
END_SCREEN_HOLD_SECONDS = 3.0    # how long the end screen is held before video ends


def load_image_paths(folder):
    folder = Path(folder)
    paths = sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not paths:
        raise RuntimeError(f"No images found in {folder}")
    return paths


def _images_all_strict_16_9(image_paths):
    """Return True if every image's aspect ratio is within tolerance of 16:9."""
    for p in image_paths:
        with Image.open(p) as img:
            ar = img.width / img.height
        if abs(ar - STRICT_16_9) > STRICT_16_9_TOLERANCE:
            return False
    return True


def slot_size(n):
    """Return (slot_w, slot_h) for each image slot in an n-image slide,
    accounting for a uniform outer border and uniform inter-image gaps."""
    slot_w = WIDTH - 2 * BORDER_PX
    slot_h = (HEIGHT - 2 * BORDER_PX - (n - 1) * GAP_PX) // n
    return slot_w, slot_h


def kept_fraction(img_aspect, target_w, target_h):
    """Fraction of the source image kept when center-cropped to (target_w, target_h)."""
    target_aspect = target_w / target_h
    if img_aspect > target_aspect:
        return target_aspect / img_aspect
    return img_aspect / target_aspect


def group_into_slides(image_paths, ctx):
    ctx.log("Scanning aspect ratios...")
    aspects = []
    for p in image_paths:
        with Image.open(p) as img:
            aspects.append(img.width / img.height)

    indexed = sorted(range(len(image_paths)), key=lambda i: aspects[i])
    sorted_aspects = [aspects[i] for i in indexed]
    sorted_paths = [image_paths[i] for i in indexed]

    slot_1 = slot_size(1)
    slot_2 = slot_size(2)
    slot_3 = slot_size(3)

    # Sorted list is [portraits, squares, landscapes]; split at first AR > threshold
    split_idx = len(sorted_aspects)
    for k, a in enumerate(sorted_aspects):
        if a > LANDSCAPE_AR_THRESHOLD:
            split_idx = k
            break

    nonland_paths = sorted_paths[:split_idx]
    nonland_aspects = sorted_aspects[:split_idx]
    land_paths = sorted_paths[split_idx:]

    slides = []

    # Non-landscape (portrait + square): DP picks between slot_1 and slot_2
    n_nl = len(nonland_aspects)
    if n_nl > 0:
        dp = [0.0] * (n_nl + 1)
        choice = [0] * (n_nl + 1)
        for i in range(1, n_nl + 1):
            dp[i] = dp[i - 1] + kept_fraction(nonland_aspects[i - 1], *slot_1)
            choice[i] = 1
            if i >= 2:
                s = (dp[i - 2]
                     + kept_fraction(nonland_aspects[i - 2], *slot_2)
                     + kept_fraction(nonland_aspects[i - 1], *slot_2))
                if s > dp[i]:
                    dp[i] = s
                    choice[i] = 2
        nl_slides = []
        i = n_nl
        while i > 0:
            c = choice[i]
            nl_slides.append(nonland_paths[i - c:i])
            i -= c
        nl_slides.reverse()
        slides.extend(nl_slides)

    # Landscape: groups of three; trailing 1 or 2 left as a smaller landscape slide.
    n_l = len(land_paths)
    full = (n_l // 3) * 3
    for g in range(0, full, 3):
        slides.append(land_paths[g:g + 3])
    leftover = n_l - full
    if leftover > 0:
        slides.append(land_paths[full:])
        ctx.log(f"  Note: {leftover} landscape image(s) left over after "
                f"3-up grouping; placed in a {leftover}-slot landscape slide.")

    random.shuffle(slides)

    # Average kept fraction across all images (uses the actual slot each got)
    path_aspect = {p: a for p, a in zip(image_paths, aspects)}
    total_kept = 0.0
    for slide_paths in slides:
        # Landscape-only slides use slot_3 geometry regardless of count
        if all(path_aspect[p] > LANDSCAPE_AR_THRESHOLD for p in slide_paths):
            sw, sh = slot_3
        else:
            sw, sh = slot_size(len(slide_paths))
        for p in slide_paths:
            total_kept += kept_fraction(path_aspect[p], sw, sh)
    avg_kept = total_kept / len(image_paths) if image_paths else 0.0
    ctx.log(f"  Average image kept after crop: {avg_kept:.1%}")

    return slides


def crop_fill(img, target_w, target_h):
    """Center-crop `img` to (target_w, target_h)'s aspect ratio, then resize."""
    iw, ih = img.size
    target_ar = target_w / target_h
    src_ar = iw / ih
    if src_ar > target_ar:
        new_iw = int(round(ih * target_ar))
        left = (iw - new_iw) // 2
        img = img.crop((left, 0, left + new_iw, ih))
    else:
        new_ih = int(round(iw / target_ar))
        top = (ih - new_ih) // 2
        img = img.crop((0, top, iw, top + new_ih))
    return img.resize((target_w, target_h), Image.LANCZOS)


def prepare_slide_images(slide_paths, all_16_9=False):
    n = len(slide_paths)

    if all_16_9:
        # Special case: every source image is exactly 16:9 (within tolerance).
        # Resize each image to a full-width 16:9 slot — no cropping at all.
        # Leftover vertical space is split equally between top, bottom, and
        # the gaps between images (n images → n+1 equal spacers).
        slot_w = WIDTH
        slot_h = round(WIDTH * 9 / 16)
        leftover = HEIGHT - n * slot_h
        spacer = max(0, leftover // (n + 1))

        images = []
        for i, path in enumerate(slide_paths):
            img = Image.open(path).convert('RGBA')
            fitted = img.resize((slot_w, slot_h), Image.LANCZOS)
            x = 0
            y = spacer + i * (slot_h + spacer)
            images.append((fitted, x, y))
        return images

    # Peek at aspects to decide slot geometry: a slide whose images are ALL
    # landscape uses 3-up slot dimensions even when there are only 1 or 2 of
    # them (so 16:9 stays roughly 16:9 instead of being cropped to portrait).
    aspects = []
    for p in slide_paths:
        with Image.open(p) as img:
            aspects.append(img.width / img.height)
    landscape_only = all(a > LANDSCAPE_AR_THRESHOLD for a in aspects)

    if landscape_only:
        slot_w, slot_h = slot_size(3)
    else:
        slot_w, slot_h = slot_size(n)

    # Vertically center the row of n slots within the canvas. For the normal
    # case (slot_size matches n) this equals BORDER_PX; for leftover landscape
    # slides it adds bg padding above and below.
    total_used_h = n * slot_h + (n - 1) * GAP_PX
    y_offset = (HEIGHT - total_used_h) // 2

    images = []
    for i, path in enumerate(slide_paths):
        img = Image.open(path).convert('RGBA')
        fitted = crop_fill(img, slot_w, slot_h)
        x = BORDER_PX
        y = y_offset + i * (slot_h + GAP_PX)
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


def _build_end_screen_frame(end_screen_path, bg_color):
    """Compose the end-screen frame: bg color filling the canvas with the
    user's image centered on top, padded, respecting transparency."""
    frame = Image.new('RGB', (WIDTH, HEIGHT), bg_color)
    img = Image.open(end_screen_path)
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    pad = int(round(WIDTH * END_SCREEN_PADDING_FRAC))
    avail_w = max(1, WIDTH - 2 * pad)
    avail_h = max(1, HEIGHT - 2 * pad)
    iw, ih = img.size
    scale = min(avail_w / iw, avail_h / ih)
    new_w = max(1, int(round(iw * scale)))
    new_h = max(1, int(round(ih * scale)))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    x = (WIDTH - new_w) // 2
    y = (HEIGHT - new_h) // 2
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


def _iter_transitions(all_slides, bg_color, end_screen_frame=None):
    """Yield (slide_idx, start_frame, end_frame) tuples for each transition.

    Encodes the same first-slide-build-up / same-layout-swap / layout-change
    semantics as before, but separated from the timing so rendering can be
    driven externally by a schedule. The closing fade targets `end_screen_frame`
    when provided, otherwise the plain bg frame.
    """
    bg_frame = Image.new('RGB', (WIDTH, HEIGHT), bg_color)
    closing_frame = end_screen_frame if end_screen_frame is not None else bg_frame
    prev_slide_imgs = None
    prev_n = 0
    n_slides = len(all_slides)

    for slide_idx, slide_imgs in enumerate(all_slides):
        n = len(slide_imgs)
        is_first = (slide_idx == 0)
        is_last = (slide_idx == n_slides - 1)
        same_layout = (prev_slide_imgs is not None and prev_n == n)

        if is_first:
            # First slide must be visible at frame 0 so it can act as the
            # video's thumbnail. Emit n no-op "transitions" so beat-snap math
            # and total duration stay unchanged but no build-up animation plays.
            full_frame = render_first_k_slots(slide_imgs, n, bg_color)
            for _ in range(n):
                yield (slide_idx, full_frame, full_frame)
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
            yield (slide_idx, full_frame, closing_frame)

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


def _resolve_audio_track(music_path, ctx):
    """Return the audio file to use, or None if none is available.

    If `music_path` is a file, it's used directly (with a sanity check on the
    extension). If it's a folder, a random audio file inside is picked.
    """
    music_path = os.path.abspath(music_path)
    if os.path.isfile(music_path):
        ext = os.path.splitext(music_path)[1].lower()
        if ext not in AUDIO_EXTENSIONS:
            ctx.log(f"Warning: '{ext}' is not a known audio extension; "
                    f"passing the file to ffmpeg anyway.")
        return music_path
    if not os.path.isdir(music_path):
        ctx.log(f"Music path not found: {music_path}; video will be silent.")
        return None
    audio_files = []
    for ext in AUDIO_EXTENSIONS:
        audio_files.extend(Path(music_path).glob(f"*{ext}"))
        audio_files.extend(Path(music_path).glob(f"*{ext.upper()}"))
    audio_files = sorted(set(audio_files))
    if not audio_files:
        ctx.log(f"No audio files in {music_path}; video will be silent.")
        return None
    return str(random.choice(audio_files))


def run(*, folder, interval=DEFAULT_INTERVAL, output=None, bg=None,
        music=None, beats_per_transition=None, end_screen=None, ctx=None):
    """Build the reel.

    folder                absolute path to the folder of source images
    interval              seconds per image transition (used when not beat-snapping)
    output                absolute output .mp4 path; auto in OUTPUT_DIR if None
    bg                    background color as "#rrggbb" hex; defaults to black
    music                 either an audio file (used directly) or a folder
                          (random audio file inside is picked)
    beats_per_transition  if music is loaded and this is > 0, snap each fade
                          to land on every Nth beat (e.g. 4 = once per bar)
    end_screen            optional path to an image (PNG with alpha works);
                          shown centered on bg as the closing frame and held
                          for END_SCREEN_HOLD_SECONDS before the video ends
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
    all_16_9 = _images_all_strict_16_9(image_paths)
    if all_16_9:
        ctx.log("All source images are exactly 16:9 — using zero-padding, "
                "no-crop layout.")
    slides = group_into_slides(image_paths, ctx)

    ctx.log(f"Found {len(image_paths)} images, grouped into {len(slides)} slides")
    for i, s in enumerate(slides):
        ctx.log(f"  Slide {i + 1}: {len(s)} image(s)")

    all_slides = [prepare_slide_images(s, all_16_9=all_16_9) for s in slides]
    num_transitions = _count_transitions(all_slides)

    audio_track = None
    if music:
        audio_track = _resolve_audio_track(music, ctx)
        if audio_track:
            ctx.log(f"Audio track: {audio_track}")

    end_screen_frame = None
    if end_screen:
        end_screen = os.path.abspath(end_screen)
        if not os.path.isfile(end_screen):
            raise ValueError(f"End screen image not found: {end_screen}")
        ctx.log(f"End screen: {end_screen}")
        end_screen_frame = _build_end_screen_frame(end_screen, bg_color)

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
    if end_screen_frame is not None:
        video_duration += END_SCREEN_HOLD_SECONDS
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
        transitions = _iter_transitions(all_slides, bg_color, end_screen_frame)
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

        if end_screen_frame is not None and not ctx.cancelled():
            hold_count = int(round(END_SCREEN_HOLD_SECONDS * FPS))
            end_bytes = end_screen_frame.tobytes()
            for _ in range(hold_count):
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
