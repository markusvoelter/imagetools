"""Joins images horizontally and cuts into Instagram carousel slides."""

import os
import random
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import piexif
from PIL import Image, ImageDraw, ImageFont

from . import RunContext


GAP_PX = 5
BASE_WIDTH = 1080

# Scroll-video settings
SCROLL_FPS = 60
SCROLL_SECONDS_PER_SLIDE = 1.0   # seconds per slide-width at REFERENCE_SCROLL_PCT (continuous mode)
REFERENCE_SCROLL_PCT = 200       # speed value that maps to the reference duration
SCROLL_AUDIO_FADE_OUT_S = 1.5    # tail fade applied to the music
SCROLL_END_SCREEN_PADDING_FRAC = 0.08   # padding around end-screen image (frac of width)
SCROLL_END_SCREEN_FADE_S = 0.5          # crossfade from last slide to end screen
SCROLL_END_SCREEN_HOLD_S = 3.0          # how long the end screen is held
SCROLL_END_SCREEN_BG = (0, 0, 0)        # bg color behind the end-screen image

# Stepped mode (per-slide hold + quick transition)
STEPPED_HOLD_S = 2.0             # default seconds each slide is held (UI-configurable)
STEPPED_TRANSITION_S = 0.15      # seconds for the quick scroll between slides

MODE_CONTINUOUS = "continuous"
MODE_STEPPED = "stepped"
SCROLL_MODES = (MODE_CONTINUOUS, MODE_STEPPED)

AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.wav', '.flac', '.aac', '.ogg', '.opus'}

ASPECT_RATIOS = {
    "1:1":  (1, 1),
    "4:3":  (4, 3),
    "16:9": (16, 9),
    "9:16": (9, 16),
}


def natural_sort_key(filename):
    return [int(c) if c.isdigit() else c.lower()
            for c in re.split(r'(\d+)', filename)]


def add_swipe_indicator(slide):
    draw = ImageDraw.Draw(slide)
    target_width = slide.width * 0.20
    label = "SWIPE"

    font = ImageFont.load_default()
    for size in range(10, 400):
        try:
            test_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
        except (OSError, IOError):
            break
        bbox = draw.textbbox((0, 0), label, font=test_font)
        if bbox[2] - bbox[0] >= target_width:
            font = test_font
            break
        font = test_font

    text_bbox = draw.textbbox((0, 0), label, font=font)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]

    margin = round(slide.width * 0.03)
    x = margin
    y = slide.height - text_h - margin - 10

    pad = 12
    overlay = Image.new("RGBA", slide.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(
        [x - pad, y - pad, x + text_w + pad, y + text_h + pad],
        radius=10, fill=(0, 0, 0, 140),
    )
    slide.paste(Image.alpha_composite(slide.convert("RGBA"), overlay).convert("RGB"))

    draw = ImageDraw.Draw(slide)
    draw.text((x, y), label, fill="white", font=font)
    return slide


def _build_scroll_end_screen_frame(end_screen_path, out_width, out_height,
                                   bg_color=SCROLL_END_SCREEN_BG):
    """Compose an end-screen frame at (out_width, out_height): bg color filling
    the canvas with the user's image centered on top, padded, with the image's
    alpha channel used as the paste mask."""
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
        raise ValueError(f"Music path not a file or folder: {music_path}")
    audio_files = []
    for ext in AUDIO_EXTENSIONS:
        audio_files.extend(Path(music_path).glob(f"*{ext}"))
        audio_files.extend(Path(music_path).glob(f"*{ext.upper()}"))
    audio_files = sorted(set(audio_files))
    if not audio_files:
        ctx.log(f"No audio files in {music_path}; scroll video will be silent.")
        return None
    return str(random.choice(audio_files))


def _render_scroll_video(strip, num_slides, section_width, working_height,
                         out_width, out_height, output_path, ctx,
                         speed_pct=REFERENCE_SCROLL_PCT, music=None,
                         end_screen=None, mode=MODE_CONTINUOUS,
                         stepped_hold_s=STEPPED_HOLD_S):
    """Render an MP4 that pans through the strip.

    Two modes:
      MODE_CONTINUOUS — linear pan at constant speed across the whole strip.
      MODE_STEPPED    — hold each slide for STEPPED_HOLD_S, then quick scroll
                        to the next (STEPPED_TRANSITION_S, smoothstep eased).

    `speed_pct` scales the durations in both modes
    (REFERENCE_SCROLL_PCT = current default; lower = slower).
    If `end_screen` is set, the video closes with a crossfade into the
    end-screen image which is then held.
    """
    if num_slides < 2:
        ctx.log("Scroll video skipped: need at least 2 slides.")
        return
    if speed_pct <= 0:
        raise ValueError("Scroll speed percent must be > 0.")
    if mode not in SCROLL_MODES:
        raise ValueError(f"Unknown scroll mode: {mode}")

    fps = SCROLL_FPS
    total_distance = strip.width - section_width
    speed_factor = REFERENCE_SCROLL_PCT / speed_pct

    if mode == MODE_STEPPED:
        hold_s = stepped_hold_s * speed_factor
        trans_s = STEPPED_TRANSITION_S * speed_factor
        hold_frames = max(1, int(round(hold_s * fps)))
        trans_frames = max(1, int(round(trans_s * fps)))
        pan_seconds = num_slides * hold_s + (num_slides - 1) * trans_s
        pan_frames = None  # not used in stepped mode
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
        ctx.log(f"  End screen: {end_screen}")
        end_screen_frame = _build_scroll_end_screen_frame(
            end_screen, out_width, out_height)
        end_screen_fade_frames = max(1, int(round(SCROLL_END_SCREEN_FADE_S * fps)))
        end_screen_hold_frames = int(round(SCROLL_END_SCREEN_HOLD_S * fps))

    total_seconds = pan_seconds + (end_screen_fade_frames + end_screen_hold_frames) / fps
    ctx.log(f"  Scroll video [{mode}]: {total_seconds:.1f}s @ {fps}fps"
            + (f" — hold {stepped_hold_s * speed_factor:.2f}s, "
               f"transition {STEPPED_TRANSITION_S * speed_factor:.2f}s"
               if mode == MODE_STEPPED else ""))

    audio_track = None
    if music:
        audio_track = _resolve_audio_track(music, ctx)
        if audio_track:
            ctx.log(f"  Audio track: {audio_track}")

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
        output_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    ctx.register_process(proc)

    try:
        if mode == MODE_STEPPED:
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
        return
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")
    ctx.log(f"Scroll video saved to {output_path}")


def run(*, folder, num_slides=20, aspect_ratio="9:16",
        output_dir=None, create_video=False,
        scroll_speed_pct=REFERENCE_SCROLL_PCT, music=None,
        end_screen=None, scroll_mode=MODE_CONTINUOUS,
        stepped_hold_s=STEPPED_HOLD_S, ctx=None):
    """Build the carousel.

    folder              absolute path with source images
    num_slides          target slide count (may be reduced if not enough source)
    aspect_ratio        key into ASPECT_RATIOS
    output_dir          absolute output dir; defaults to sibling of `folder`
                        named "<folder>-swipey"
    create_video        if True, also produce an MP4 that scrolls through slides
    scroll_speed_pct    pan speed for the scroll video; 200 = current default,
                        100 = half speed (twice as long), 400 = double speed
    music               either an audio file (used directly) or a folder
                        (random audio file picked); only used when create_video
    end_screen          optional path to an image used as the closing frame of
                        the scroll video; only used when create_video
    ctx                 RunContext
    """
    if ctx is None:
        ctx = RunContext()
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
    total_needed = num_slides * section_width

    ctx.log(f"Output slide size: {out_width}x{out_height} ({aspect_ratio})")
    ctx.log(f"Working height: {working_height}, section width: {section_width}")
    ctx.log(f"Total strip width needed: {total_needed}px for {num_slides} slides")

    if output_dir is None:
        parent = os.path.dirname(folder)
        output_dir = os.path.join(parent, os.path.basename(folder) + "-swipey")
    else:
        output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    ctx.log(f"Output folder: {output_dir}")

    files = sorted(
        [f for f in os.listdir(folder)
         if f.lower().endswith((".jpg", ".jpeg", ".png"))],
        key=natural_sort_key,
    )
    if not files:
        raise RuntimeError(f"No images found in {folder}")

    ctx.log(f"Found {len(files)} source images")

    scaled = []
    accumulated_width = 0
    for idx, f in enumerate(files):
        ctx.check_cancelled()
        img = Image.open(os.path.join(folder, f))
        ratio = working_height / img.height
        new_width = round(img.width * ratio)
        img = img.resize((new_width, working_height), Image.LANCZOS)

        gap = GAP_PX if idx > 0 else 0
        needed = accumulated_width + gap + new_width

        scaled.append((img, gap))
        accumulated_width = needed
        ctx.log(f"  {f}: {new_width}x{working_height}  "
                f"(total so far: {accumulated_width}px)")

        if accumulated_width >= total_needed:
            break

    if accumulated_width < total_needed:
        needed_slides = -(-accumulated_width // section_width)
        if needed_slides == 0:
            raise RuntimeError("Not enough images to fill even one slide.")
        num_slides = needed_slides
        total_needed = num_slides * section_width
        remaining = total_needed - accumulated_width
        ctx.log(f"Producing {num_slides} slides "
                f"(last slide has {remaining}px black padding)")

    ctx.log(f"Using {len(scaled)} of {len(files)} source images")

    strip = Image.new("RGB", (total_needed, working_height), (0, 0, 0))
    x = 0
    for img, gap in scaled:
        x += gap
        if gap > 0:
            gap_fill = Image.new("RGB", (gap, working_height), (255, 255, 255))
            strip.paste(gap_fill, (x - gap, 0))
        strip.paste(img, (x, 0))
        x += img.width

    ctx.log(f"Strip: {strip.width}x{strip.height} = "
            f"{num_slides} x {section_width}x{working_height}")

    for i in range(num_slides):
        ctx.check_cancelled()
        left = i * section_width
        right = left + section_width
        slide = strip.crop((left, 0, right, working_height))
        slide = slide.resize((out_width, out_height), Image.LANCZOS)

        if i == 0:
            slide = add_swipe_indicator(slide)

        capture_time = (datetime.now().replace(hour=12, minute=0, second=0)
                        + timedelta(minutes=i))
        time_str = capture_time.strftime("%Y:%m:%d %H:%M:%S")
        exif_dict = {
            "Exif": {
                piexif.ExifIFD.DateTimeOriginal: time_str,
                piexif.ExifIFD.DateTimeDigitized: time_str,
            },
            "0th": {
                piexif.ImageIFD.DateTime: time_str,
            },
        }
        exif_bytes = piexif.dump(exif_dict)

        out_path = os.path.join(output_dir, f"slide_{i + 1:02d}.jpg")
        slide.save(out_path, quality=95, exif=exif_bytes)
        ctx.log(f"  Saved {out_path}  ({time_str})")

    if create_video:
        ctx.check_cancelled()
        video_name = os.path.basename(folder) + "-scroll.mp4"
        video_path = os.path.join(output_dir, video_name)
        ctx.log(f"Creating scroll video: {video_path}")
        _render_scroll_video(
            strip, num_slides, section_width, working_height,
            out_width, out_height, video_path, ctx,
            speed_pct=scroll_speed_pct, music=music,
            end_screen=end_screen, mode=scroll_mode,
            stepped_hold_s=stepped_hold_s,
        )

    ctx.log("Done!")
    return output_dir
