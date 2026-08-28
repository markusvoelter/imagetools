"""Render a fast-shuffle "reveal" MP4 from a folder of images.

Each source image is shown in turn (natural-sorted). Between any two
consecutive images a burst of random *other* images scrolls past very, very
fast — a slot-machine style reveal — before the video settles onto the next
real image. The burst length varies per transition (a random count between
`min_intermediate` and `max_intermediate`).

direction "horizontal": images scroll in from the right (motion right-to-left).
direction "vertical":   images scroll in from the top (motion top-to-bottom).
"""

import json
import os
import random
import subprocess
from datetime import datetime

import numpy as np
from PIL import Image

from . import OUTPUT_DIR, RunContext, ensure_output_dir
from ._panorama import ASPECT_RATIOS, BASE_WIDTH, natural_sort_key
from .scroll_video import _center_crop_to, _resolve_audio_track


SHUFFLE_FPS = 60
DEFAULT_HOLD_S = 1.5          # how long each real image is held
FAST_TRANS_FRAMES = 3         # frames per fast scroll ("very very fast")
DEFAULT_MIN_INTERMEDIATE = 3  # fewest random images flashed between two reals
DEFAULT_MAX_INTERMEDIATE = 7  # most random images flashed between two reals
MOTION_BLUR_SAMPLES = 8       # sub-positions averaged per whip frame (1 = off)
AUDIO_FADE_OUT_S = 1.5        # tail fade applied to the music

DIRECTION_HORIZONTAL = "horizontal"
DIRECTION_VERTICAL = "vertical"
DIRECTIONS = (DIRECTION_HORIZONTAL, DIRECTION_VERTICAL)


def _compose(direction, t, cur, nxt, w, h, reverse=False):
    """Composite two full-frame images mid scroll-transition at progress
    `t` in [0, 1]. Returns a new RGB frame of size (w, h).

    `reverse` flips the scroll direction: horizontal new image enters from the
    left (instead of the right), vertical from the bottom (instead of the top).
    """
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    d = -1 if reverse else 1
    if direction == DIRECTION_HORIZONTAL:
        # forward: new enters from the right; reverse: from the left
        canvas.paste(cur, (int(round(-d * t * w)), 0))
        canvas.paste(nxt, (int(round(d * (1 - t) * w)), 0))
    else:
        # forward: new enters from the top; reverse: from the bottom
        canvas.paste(cur, (0, int(round(d * t * h))))
        canvas.paste(nxt, (0, int(round(d * (t - 1) * h))))
    return canvas


def _compose_blurred(direction, t_start, t_end, cur, nxt, w, h, samples,
                     reverse=False):
    """Like `_compose`, but averages `samples` sub-positions spanning the
    frame's shutter interval [t_start, t_end] to fake motion blur. Returns an
    RGB frame. With samples == 1 this is a plain `_compose` at `t_end`."""
    if samples <= 1:
        return _compose(direction, t_end, cur, nxt, w, h, reverse)
    acc = np.zeros((h, w, 3), dtype=np.float32)
    for s in range(samples):
        t = t_start + (t_end - t_start) * ((s + 0.5) / samples)
        acc += np.asarray(_compose(direction, t, cur, nxt, w, h, reverse),
                          dtype=np.float32)
    acc /= samples
    return Image.fromarray(acc.astype(np.uint8), "RGB")


def _load_bar_times(audio_path):
    """Return the sorted bar timestamps (seconds) from `<audio>.json`, or None.

    Mirrors the sidecar format used by ken_burns: a top-level "bars" list of
    {"time": <seconds>} objects. Bars flagged "muted" (and not "manual") carry
    no picture change, so they are skipped. Needs at least two usable bars.
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
    times = []
    for b in data.get("bars") or []:
        try:
            t = float(b["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if t < 0:
            continue
        if b.get("muted") is True and b.get("manual") is not True:
            continue
        times.append(t)
    times = sorted(times)
    return times if len(times) >= 2 else None


def _build_schedule(gap_counts, hold_frames, bar_times, fps):
    """Decide how long each real image is held before its whip burst, plus a
    final tail hold. Returns (pre_holds, tail_frames).

    Without `bar_times`, every hold is the fixed `hold_frames`. With bar times,
    the hold is stretched so each burst *lands* the next real image exactly on
    a bar: for each transition we take the earliest bar that leaves room for the
    burst, and hold the current image for the remainder. Bars are consumed in
    order; if they run out, we continue at the median bar spacing.
    """
    n_gaps = len(gap_counts)
    burst_frames = [(count + 1) * FAST_TRANS_FRAMES for count in gap_counts]

    if not bar_times:
        return [hold_frames] * n_gaps, hold_frames

    bar_frames = [int(round(t * fps)) for t in bar_times]
    diffs = [bar_frames[k + 1] - bar_frames[k]
             for k in range(len(bar_frames) - 1) if bar_frames[k + 1] > bar_frames[k]]
    median_gap = sorted(diffs)[len(diffs) // 2] if diffs else fps

    pre_holds = []
    prev = 0          # cumulative frame where the current image's hold begins
    ptr = 0           # next unconsumed bar
    for i in range(n_gaps):
        earliest = prev + burst_frames[i]
        while ptr < len(bar_frames) and bar_frames[ptr] < earliest:
            ptr += 1
        if ptr < len(bar_frames):
            target = bar_frames[ptr]
            ptr += 1
        else:
            # ran out of annotated bars — keep the beat going at median spacing
            target = prev + max(burst_frames[i], median_gap)
        pre_holds.append(target - prev - burst_frames[i])
        prev = target

    # tail: hold the last image until the next bar, else one median beat
    tail_frames = median_gap
    for bf in bar_frames:
        if bf > prev:
            tail_frames = bf - prev
            break
    return pre_holds, tail_frames


def run(*, folder, aspect_ratio="9:16", direction=DIRECTION_HORIZONTAL,
        output=None, hold_s=DEFAULT_HOLD_S,
        min_intermediate=DEFAULT_MIN_INTERMEDIATE,
        max_intermediate=DEFAULT_MAX_INTERMEDIATE,
        num_images=0, random_order=False, reverse=False, music=None, ctx=None):
    """Render the shuffle-reveal video using images from `folder`.

    folder            folder with source images
    aspect_ratio      key into ASPECT_RATIOS
    direction         DIRECTION_HORIZONTAL or DIRECTION_VERTICAL
    output            output .mp4 path; auto in OUTPUT_DIR if None
    hold_s            seconds each real image is held
    min_intermediate  fewest random images flashed between two real images
    max_intermediate  most random images flashed between two real images; each
                      transition picks a random count in this range, so
                      transitions vary in length
    num_images        how many images to use; 0 = all. When > 0, a random subset
                      of that many images is picked.
    random_order      if True, the reveal sequence is shuffled; otherwise it
                      plays in natural-sorted filename order.
    reverse           if True, each fast scroll step randomly picks the forward
                      or opposite direction; otherwise all scroll forward.
    music             file or folder of audio files (random pick from a folder)
    ctx               RunContext
    """
    if ctx is None:
        ctx = RunContext()
    if direction not in DIRECTIONS:
        raise ValueError(f"Unknown direction: {direction}")
    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"Unknown aspect ratio: {aspect_ratio}")
    if hold_s <= 0:
        raise ValueError("Hold seconds must be > 0.")
    if min_intermediate < 0 or max_intermediate < 0:
        raise ValueError("Intermediate counts must be >= 0.")
    if max_intermediate < min_intermediate:
        raise ValueError("Max intermediate images must be >= min.")
    if num_images < 0:
        raise ValueError("Number of images must be >= 0.")

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    ar_w, ar_h = ASPECT_RATIOS[aspect_ratio]
    out_width = BASE_WIDTH
    out_height = round(BASE_WIDTH * ar_h / ar_w)
    if out_width % 2:
        out_width += 1
    if out_height % 2:
        out_height += 1

    files = [f for f in os.listdir(folder)
             if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    if num_images and num_images < len(files):
        files = random.sample(files, num_images)
    if random_order:
        random.shuffle(files)
    else:
        files.sort(key=natural_sort_key)
    if len(files) < 2:
        raise RuntimeError("Need at least 2 images for a fast scroll video.")

    if output is None:
        ensure_output_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = os.path.join(
            OUTPUT_DIR, f"{os.path.basename(folder)}_fastscroll_{ts}.mp4")
    else:
        output = os.path.abspath(output)
        os.makedirs(os.path.dirname(output), exist_ok=True)

    ctx.log(f"Output frame size: {out_width}x{out_height} ({aspect_ratio})")
    ctx.log(f"Direction: {direction}, "
            f"{min_intermediate}-{max_intermediate} fast images between reveals"
            + (f", motion blur x{MOTION_BLUR_SAMPLES}"
               if MOTION_BLUR_SAMPLES > 1 else ""))
    ctx.log(f"Using {len(files)} images"
            + (" (random subset)" if num_images else " (all)")
            + (", random order" if random_order else ", sorted order"))

    # Pre-crop every image once to the output frame size.
    frames = []
    for f in files:
        ctx.check_cancelled()
        with Image.open(os.path.join(folder, f)) as img:
            frames.append(_center_crop_to(img.convert("RGB"),
                                          out_width, out_height))
    n = len(frames)

    fps = SHUFFLE_FPS
    hold_frames = max(1, int(round(hold_s * fps)))

    audio_track = None
    if music:
        audio_track = _resolve_audio_track(music, ctx)
        if audio_track:
            ctx.log(f"Audio track: {audio_track}")

    # If the music has a sidecar bar-timing JSON, sync image reveals to bars
    # and ignore `hold_s`; otherwise fall back to the fixed hold.
    bar_times = _load_bar_times(audio_track)
    if bar_times:
        ctx.log(f"Bar sync: {len(bar_times)} bars from sidecar JSON — "
                f"ignoring hold-per-image, landing each reveal on a bar.")

    # Pick a random number of intermediate images per transition up front, so
    # the frame budget matches what the render loop actually emits.
    gap_counts = [random.randint(min_intermediate, max_intermediate)
                  for _ in range(n - 1)]
    pre_holds, tail_frames = _build_schedule(
        gap_counts, hold_frames, bar_times, fps)
    # each gap: a pre-burst hold + (`count` whips + 1 reveal), then a tail hold
    total_frames = tail_frames + sum(
        pre_holds[i] + (gap_counts[i] + 1) * FAST_TRANS_FRAMES
        for i in range(n - 1))
    total_seconds = total_frames / fps
    ctx.log(f"Fast scroll: {n} images, ~{total_seconds:.1f}s @ {fps}fps")

    cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo',
        '-vcodec', 'rawvideo',
        '-s', f'{out_width}x{out_height}',
        '-pix_fmt', 'rgb24',
        '-r', str(fps),
        '-i', '-',
    ]
    if audio_track:
        audio_fade_start = max(0.0, total_seconds - AUDIO_FADE_OUT_S)
        cmd += [
            '-i', audio_track,
            '-map', '0:v', '-map', '1:a',
            '-c:a', 'aac', '-b:a', '192k',
            '-af', f'afade=t=out:st={audio_fade_start:.3f}:d={AUDIO_FADE_OUT_S:.3f}',
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

    def emit(frame):
        proc.stdin.write(frame.tobytes())

    try:
        for i in range(n - 1):
            if ctx.cancelled():
                break
            cur = frames[i]
            nxt = frames[i + 1]

            # hold the current real image until its burst is due (bar-timed or
            # the fixed hold); the reveal at the end of the burst then lands on
            # the target moment
            cur_bytes = cur.tobytes()
            for _ in range(pre_holds[i]):
                if ctx.cancelled():
                    break
                proc.stdin.write(cur_bytes)

            # Build the intermediate sequence: random "other" images whipping
            # past, with the LAST intermediate being the next real image itself
            # (so the burst settles directly onto what is then long-shown).
            pool = [j for j in range(n) if j != i and j != i + 1]
            if not pool:
                pool = list(range(n))
            rand_count = gap_counts[i]
            targets = [random.choice(pool) for _ in range(rand_count)] + [i + 1]

            # every step — including landing on the next real image — scrolls at
            # the same fast speed, so the reveal doesn't drag behind the whips.
            # Each frame is motion-blurred across its shutter interval so the
            # fast slides read smoothly instead of strobing.
            for p in targets:
                if ctx.cancelled():
                    break
                target = frames[p]
                # when enabled, randomly flip this step's scroll direction
                step_reverse = reverse and random.random() < 0.5
                for fr in range(FAST_TRANS_FRAMES):
                    t_start = fr / FAST_TRANS_FRAMES
                    t_end = (fr + 1) / FAST_TRANS_FRAMES
                    emit(_compose_blurred(direction, t_start, t_end, cur, target,
                                          out_width, out_height,
                                          MOTION_BLUR_SAMPLES, step_reverse))
                cur = target

        # tail: hold the final real image
        if not ctx.cancelled():
            last_bytes = frames[n - 1].tobytes()
            for _ in range(tail_frames):
                if ctx.cancelled():
                    break
                proc.stdin.write(last_bytes)
    except BrokenPipeError:
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait()

    if ctx.cancelled():
        ctx.log("Fast scroll cancelled.")
        return None
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")

    ctx.log(f"Fast scroll saved to {output}")
    return output
