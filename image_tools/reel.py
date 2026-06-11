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
PADDING = 20
MAX_CROP = 0.10


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


def render_frame(slide_images, alphas):
    frame = Image.new('RGB', (WIDTH, HEIGHT), (0, 0, 0))
    for (img, x, y), alpha in zip(slide_images, alphas):
        if alpha <= 0:
            continue
        if alpha >= 1.0:
            frame.paste(img, (x, y), img)
        else:
            temp = img.copy()
            r, g, b, a = temp.split()
            a = a.point(lambda p, al=alpha: int(p * al))
            temp = Image.merge('RGBA', (r, g, b, a))
            frame.paste(temp, (x, y), temp)
    return frame


def run(*, folder, interval=DEFAULT_INTERVAL, output=None, ctx=None):
    """Build the reel.

    folder    absolute path to the folder of source images
    interval  seconds per image transition
    output    absolute output .mp4 path; auto in OUTPUT_DIR if None
    ctx       RunContext
    """
    if ctx is None:
        ctx = RunContext()

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    interval = float(interval)
    folder_name = Path(folder).name

    if output is None:
        ensure_output_dir()
        output = os.path.join(OUTPUT_DIR, f"{folder_name}.mp4")
    else:
        output = os.path.abspath(output)
        os.makedirs(os.path.dirname(output), exist_ok=True)

    fade_seconds = interval * FADE_RATIO
    hold_seconds = interval - fade_seconds
    fade_frames = max(1, int(fade_seconds * FPS))
    hold_frames = max(1, int(hold_seconds * FPS))

    ctx.log(f"Interval: {interval}s (fade: {fade_seconds:.2f}s, "
            f"hold: {hold_seconds:.2f}s)")

    image_paths = load_image_paths(folder)
    slides = group_into_slides(image_paths, ctx)

    ctx.log(f"Found {len(image_paths)} images, grouped into {len(slides)} slides")
    for i, s in enumerate(slides):
        ctx.log(f"  Slide {i + 1}: {len(s)} image(s)")

    cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo',
        '-vcodec', 'rawvideo',
        '-s', f'{WIDTH}x{HEIGHT}',
        '-pix_fmt', 'rgb24',
        '-r', str(FPS),
        '-i', '-',
        '-an',
        '-vcodec', 'libx264',
        '-pix_fmt', 'yuv420p',
        '-preset', 'medium',
        '-crf', '18',
        output,
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    ctx.register_process(proc)

    all_slides = [prepare_slide_images(s) for s in slides]

    try:
        for slide_idx, slide_images in enumerate(all_slides):
            if ctx.cancelled():
                break
            ctx.log(f"Rendering slide {slide_idx + 1}/{len(all_slides)}...")
            n = len(slide_images)
            is_first = slide_idx == 0
            is_last = slide_idx == len(all_slides) - 1

            if is_first:
                for f in range(fade_frames):
                    alpha = f / fade_frames
                    alphas = [alpha] + [0.0] * (n - 1)
                    frame = render_frame(slide_images, alphas)
                    proc.stdin.write(frame.tobytes())
                alphas = [1.0] + [0.0] * (n - 1)
                hold_frame = render_frame(slide_images, alphas)
                hold_bytes = hold_frame.tobytes()
                for _ in range(hold_frames):
                    proc.stdin.write(hold_bytes)

            for img_idx in range(1, n):
                for f in range(fade_frames):
                    alpha = f / fade_frames
                    alphas = [1.0 if j < img_idx else (alpha if j == img_idx else 0.0)
                              for j in range(n)]
                    frame = render_frame(slide_images, alphas)
                    proc.stdin.write(frame.tobytes())
                alphas = [1.0 if j <= img_idx else 0.0 for j in range(n)]
                hold_frame = render_frame(slide_images, alphas)
                hold_bytes = hold_frame.tobytes()
                for _ in range(hold_frames):
                    proc.stdin.write(hold_bytes)

            if is_last:
                for f in range(fade_frames):
                    alpha = 1.0 - f / fade_frames
                    alphas = [alpha] * n
                    frame = render_frame(slide_images, alphas)
                    proc.stdin.write(frame.tobytes())
            else:
                next_slide = all_slides[slide_idx + 1]
                old_full = render_frame(slide_images, [1.0] * n)
                new_first = render_frame(
                    next_slide, [1.0] + [0.0] * (len(next_slide) - 1)
                )
                for f in range(fade_frames):
                    t = f / fade_frames
                    blended = Image.blend(old_full, new_first, t)
                    proc.stdin.write(blended.tobytes())
                hold_bytes = new_first.tobytes()
                for _ in range(hold_frames):
                    proc.stdin.write(hold_bytes)
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
