"""Render a panning/scrolling MP4 from a folder of carousel-style images.

Two modes:
  MODE_CONTINUOUS — linear pan from slide 0 to slide N-1 at constant speed.
  MODE_STEPPED    — each slide is held for `stepped_hold_s` seconds, then a
                    quick smoothstep scroll moves to the next.
Optional background music (file or folder) and a final end-screen image
(centered on bg, alpha respected) are supported.
"""

import os
import random
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image

from . import OUTPUT_DIR, RunContext, ensure_output_dir
from ._panorama import ASPECT_RATIOS, BASE_WIDTH, natural_sort_key


SCROLL_FPS = 60
SCROLL_SECONDS_PER_SLIDE = 1.0          # seconds per slide-width at REFERENCE_SCROLL_PCT
REFERENCE_SCROLL_PCT = 200              # speed value mapped to the reference duration
SCROLL_AUDIO_FADE_OUT_S = 1.5           # tail fade applied to the music

SCROLL_END_SCREEN_PADDING_FRAC = 0.08   # padding around end-screen image (frac of width)
SCROLL_END_SCREEN_FADE_S = 0.5          # crossfade from last slide to end screen
SCROLL_END_SCREEN_HOLD_S = 3.0          # how long the end screen is held
SCROLL_END_SCREEN_BG = (0, 0, 0)        # bg color behind the end-screen image

STEPPED_HOLD_S = 2.0                    # default hold per slide in stepped mode
STEPPED_TRANSITION_S = 0.15             # quick scroll between slides in stepped mode

MODE_CONTINUOUS = "continuous"
MODE_STEPPED = "stepped"
SCROLL_MODES = (MODE_CONTINUOUS, MODE_STEPPED)

AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.wav', '.flac', '.aac', '.ogg', '.opus'}


def _center_crop_to(img, target_w, target_h):
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


def _build_image_grid_strip(folder, aspect_ratio, ctx):
    """Center-crop every image in `folder` to the section size and concatenate
    them horizontally with no gaps. Returns the same shape as build_panorama_strip:
    (strip, num_slides, section_width, working_height, out_width, out_height).
    """
    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"Unknown aspect ratio: {aspect_ratio}")

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    ar_w, ar_h = ASPECT_RATIOS[aspect_ratio]
    out_width = BASE_WIDTH
    out_height = round(BASE_WIDTH * ar_h / ar_w)
    working_height = max(out_height, 1000)
    section_width = round(working_height * ar_w / ar_h)

    files = sorted(
        [f for f in os.listdir(folder)
         if f.lower().endswith((".jpg", ".jpeg", ".png"))],
        key=natural_sort_key,
    )
    if not files:
        raise RuntimeError(f"No images found in {folder}")

    ctx.log(f"Output slide size: {out_width}x{out_height} ({aspect_ratio})")
    ctx.log(f"Section size:      {section_width}x{working_height}")
    ctx.log(f"Found {len(files)} source images")

    num_slides = len(files)
    total_width = num_slides * section_width
    strip = Image.new("RGB", (total_width, working_height), (0, 0, 0))

    for idx, f in enumerate(files):
        ctx.check_cancelled()
        with Image.open(os.path.join(folder, f)) as img:
            cropped = _center_crop_to(img.convert("RGB"),
                                      section_width, working_height)
        x = idx * section_width
        strip.paste(cropped, (x, 0))
        ctx.log(f"  {f}: center-cropped to {section_width}x{working_height}")

    ctx.log(f"Strip: {strip.width}x{strip.height} = "
            f"{num_slides} x {section_width}x{working_height}")
    return strip, num_slides, section_width, working_height, out_width, out_height


def _build_end_screen_frame(end_screen_path, out_width, out_height,
                            bg_color=SCROLL_END_SCREEN_BG):
    """Compose an end-screen frame at (out_width, out_height): bg color filling
    the canvas with the user's image centered on top, padded, using the image's
    alpha channel as the paste mask."""
    frame = Image.new('RGB', (out_width, out_height), bg_color)
    img = Image.open(end_screen_path)
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    pad = int(round(out_width * SCROLL_END_SCREEN_PADDING_FRAC))
    avail_w = max(1, out_width - 2 * pad)
    avail_h = max(1, out_height - 2 * pad)
    iw, ih = img.size
    scale = min(avail_w / iw, avail_h / ih)
    new_w = max(1, int(round(iw * scale)))
    new_h = max(1, int(round(ih * scale)))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    x = (out_width - new_w) // 2
    y = (out_height - new_h) // 2
    frame.paste(img, (x, y), img)
    return frame


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
        ctx.log(f"Music path not found: {music_path}; video will be silent.")
        return None
    audio_files = []
    for ext in AUDIO_EXTENSIONS:
        audio_files.extend(Path(music_path).glob(f"*{ext}"))
        audio_files.extend(Path(music_path).glob(f"*{ext.upper()}"))
    audio_files = sorted(set(audio_files))
    if not audio_files:
        ctx.log(f"No audio files in {music_path}; scroll video will be silent.")
        return None
    return str(random.choice(audio_files))


def run(*, folder, aspect_ratio="9:16", output=None,
        scroll_mode=MODE_CONTINUOUS, stepped_hold_s=STEPPED_HOLD_S,
        scroll_speed_pct=REFERENCE_SCROLL_PCT,
        music=None, end_screen=None, ctx=None):
    """Render the scroll video using every image in `folder`.

    folder              folder with source images (all images are used)
    aspect_ratio        key into ASPECT_RATIOS
    output              output .mp4 path; auto in OUTPUT_DIR if None
    scroll_mode         MODE_CONTINUOUS or MODE_STEPPED
    stepped_hold_s      seconds each slide is held in stepped mode
    scroll_speed_pct    pan speed; 200 = reference, lower = slower, higher = faster
    music               file or folder of audio files (random pick from folder)
    end_screen          path to an end-screen image (PNG with alpha works)
    ctx                 RunContext
    """
    if ctx is None:
        ctx = RunContext()
    if scroll_mode not in SCROLL_MODES:
        raise ValueError(f"Unknown scroll mode: {scroll_mode}")
    if scroll_speed_pct <= 0:
        raise ValueError("Scroll speed percent must be > 0.")

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    if output is None:
        ensure_output_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = os.path.join(
            OUTPUT_DIR, f"{os.path.basename(folder)}_scroll_{ts}.mp4")
    else:
        output = os.path.abspath(output)
        os.makedirs(os.path.dirname(output), exist_ok=True)

    strip, num_slides, section_width, working_height, out_width, out_height = (
        _build_image_grid_strip(folder, aspect_ratio, ctx)
    )

    if num_slides < 2:
        raise RuntimeError("Need at least 2 slides for a scroll video.")

    fps = SCROLL_FPS
    total_distance = strip.width - section_width
    speed_factor = REFERENCE_SCROLL_PCT / scroll_speed_pct

    if scroll_mode == MODE_STEPPED:
        hold_s = stepped_hold_s * speed_factor
        trans_s = STEPPED_TRANSITION_S * speed_factor
        hold_frames = max(1, int(round(hold_s * fps)))
        trans_frames = max(1, int(round(trans_s * fps)))
        pan_seconds = num_slides * hold_s + (num_slides - 1) * trans_s
        pan_frames = None
    else:
        seconds_per_slide = SCROLL_SECONDS_PER_SLIDE * speed_factor
        pan_seconds = (num_slides - 1) * seconds_per_slide
        pan_frames = max(2, int(round(pan_seconds * fps)))

    end_screen_frame = None
    end_screen_fade_frames = 0
    end_screen_hold_frames = 0
    if end_screen:
        end_screen = os.path.abspath(end_screen)
        if not os.path.isfile(end_screen):
            raise ValueError(f"End screen image not found: {end_screen}")
        ctx.log(f"End screen: {end_screen}")
        end_screen_frame = _build_end_screen_frame(
            end_screen, out_width, out_height)
        end_screen_fade_frames = max(1, int(round(SCROLL_END_SCREEN_FADE_S * fps)))
        end_screen_hold_frames = int(round(SCROLL_END_SCREEN_HOLD_S * fps))

    total_seconds = pan_seconds + (end_screen_fade_frames + end_screen_hold_frames) / fps
    ctx.log(f"Scroll video [{scroll_mode}]: {total_seconds:.1f}s @ {fps}fps"
            + (f" — hold {stepped_hold_s * speed_factor:.2f}s, "
               f"transition {STEPPED_TRANSITION_S * speed_factor:.2f}s"
               if scroll_mode == MODE_STEPPED else ""))

    audio_track = None
    if music:
        audio_track = _resolve_audio_track(music, ctx)
        if audio_track:
            ctx.log(f"Audio track: {audio_track}")

    def frame_at(offset):
        offset = max(0, min(offset, strip.width - section_width))
        slice_img = strip.crop((offset, 0, offset + section_width, working_height))
        return slice_img.resize((out_width, out_height), Image.LANCZOS)

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
        audio_fade_start = max(0.0, total_seconds - SCROLL_AUDIO_FADE_OUT_S)
        cmd += [
            '-i', audio_track,
            '-map', '0:v', '-map', '1:a',
            '-c:a', 'aac', '-b:a', '192k',
            '-af', f'afade=t=out:st={audio_fade_start:.3f}:d={SCROLL_AUDIO_FADE_OUT_S:.3f}',
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
        if scroll_mode == MODE_STEPPED:
            for i in range(num_slides):
                if ctx.cancelled():
                    break
                slide_offset = i * section_width
                hold_bytes = frame_at(slide_offset).tobytes()
                for _ in range(hold_frames):
                    proc.stdin.write(hold_bytes)
                if i < num_slides - 1:
                    next_offset = (i + 1) * section_width
                    for f in range(trans_frames):
                        if ctx.cancelled():
                            break
                        t = f / trans_frames
                        eased = 3 * t * t - 2 * t * t * t  # smoothstep
                        offset = int(round(slide_offset
                                           + (next_offset - slide_offset) * eased))
                        proc.stdin.write(frame_at(offset).tobytes())
        else:
            for f in range(pan_frames):
                if ctx.cancelled():
                    break
                t = f / (pan_frames - 1)
                offset = int(round(t * total_distance))
                proc.stdin.write(frame_at(offset).tobytes())

        if end_screen_frame is not None and not ctx.cancelled():
            last_pan_frame = frame_at(total_distance)
            for f in range(end_screen_fade_frames):
                if ctx.cancelled():
                    break
                t = f / end_screen_fade_frames
                blended = Image.blend(last_pan_frame, end_screen_frame, t)
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
        ctx.log("Scroll video cancelled.")
        return None
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")

    ctx.log(f"Scroll video saved to {output}")
    return output
