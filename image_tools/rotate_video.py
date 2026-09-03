"""Generate a 9:16 portrait video (1080x1920) from a cover image + horizontal photos."""

import glob
import os
import random
import subprocess
from pathlib import Path

from . import ASSETS_DIR, OUTPUT_DIR, RunContext, ensure_output_dir


WIDTH = 1080
HEIGHT = 1920
FPS = 30

DURATION_IMAGE1 = 3
ROTATION_DURATION = 1.5
TRANSITION_DURATION = 0.35
CROSSFADE_DURATION = 0.2
ENDSCREEN_DURATION = 5

ROTATE_AUDIO_FADE_OUT_S = 1.5
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.wav', '.flac', '.aac', '.ogg', '.opus'}

OVERLAY_PNG = os.path.join(ASSETS_DIR, "rotateoverlay.png")
ENDSCREEN_PNG = os.path.join(ASSETS_DIR, "endscreen.png")


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
        ctx.log(f"No audio files in {music_path}; video will be silent.")
        return None
    return str(random.choice(audio_files))


def find_images(folder, cover_filename):
    cover = os.path.join(folder, cover_filename)
    if not os.path.isfile(cover):
        raise ValueError(f"Cover image '{cover_filename}' not found in '{folder}'")

    extensions = ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG")
    all_files = []
    for ext in extensions:
        all_files.extend(glob.glob(os.path.join(folder, ext)))
    all_files = sorted(set(all_files))

    cover_abs = os.path.abspath(cover)
    horizontal = [f for f in all_files if os.path.abspath(f) != cover_abs]

    if not horizontal:
        raise ValueError(
            f"No horizontal images found in '{folder}' (besides '{cover_filename}')"
        )

    return cover, horizontal


def build_video(cover, horizontal_images, overlay, endscreen, output_path,
                duration_horizontal, ctx, audio_track=None):
    num_horizontal = len(horizontal_images)
    if num_horizontal == 1:
        per_image_dur = duration_horizontal
    else:
        per_image_dur = (
            duration_horizontal + (num_horizontal - 1) * CROSSFADE_DURATION
        ) / num_horizontal

    ctx.log(f"Horizontal images: {num_horizontal}, "
            f"duration each: {per_image_dur:.2f}s")

    num_rotations = int(DURATION_IMAGE1 // ROTATION_DURATION)
    total_rotate_time = num_rotations * ROTATION_DURATION
    rate = -3.14159265 / (2 * ROTATION_DURATION)

    inputs = []
    inputs += ["-loop", "1", "-t", str(DURATION_IMAGE1), "-i", cover]
    inputs += ["-loop", "1", "-t", str(DURATION_IMAGE1), "-i", overlay]
    for img in horizontal_images:
        inputs += ["-loop", "1", "-t", str(per_image_dur), "-i", img]
    endscreen_idx = 2 + num_horizontal
    inputs += ["-loop", "1", "-t", str(ENDSCREEN_DURATION), "-i", endscreen]
    audio_input_idx = None
    if audio_track:
        audio_input_idx = endscreen_idx + 1
        inputs += ["-i", audio_track]

    filters = []
    filters.append(
        f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},setsar=1,fps={FPS}[v0base]"
    )
    filters.append(
        f"[1:v]scale={int(WIDTH * 0.5)}:-1,format=rgba,"
        f"pad=w=hypot(iw\\,ih):h=hypot(iw\\,ih):x=(ow-iw)/2:y=(oh-ih)/2:color=0x00000000,"
        f"rotate='if(lt(t\\,{total_rotate_time})\\,{rate}*mod(t\\,{ROTATION_DURATION})\\,-PI/2)'"
        f":fillcolor=0x00000000,fps={FPS}[ovr]"
    )
    filters.append(
        f"[v0base][ovr]overlay=(W-w)/2:(H-h)/2,"
        f"fade=t=out:st={DURATION_IMAGE1 - TRANSITION_DURATION}:d={TRANSITION_DURATION}[v0]"
    )

    for i in range(num_horizontal):
        input_idx = i + 2
        filters.append(
            f"[{input_idx}:v]transpose=1,"
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},setsar=1,fps={FPS}[h{i}]"
        )

    filters.append(
        f"[{endscreen_idx}:v]transpose=1,"
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},setsar=1,fps={FPS}[endscr]"
    )

    prev = "h0"
    offset = per_image_dur - CROSSFADE_DURATION
    all_to_xfade = [f"h{i}" for i in range(1, num_horizontal)] + ["endscr"]
    for i, label in enumerate(all_to_xfade):
        is_last = (i == len(all_to_xfade) - 1)
        out_label = "hfaded" if is_last else f"xf{i}"
        filters.append(
            f"[{prev}][{label}]xfade=transition=fade:"
            f"duration={CROSSFADE_DURATION}:offset={offset}[{out_label}]"
        )
        prev = out_label
        if not is_last:
            offset += per_image_dur - CROSSFADE_DURATION
        else:
            offset += ENDSCREEN_DURATION - CROSSFADE_DURATION

    filters.append(
        f"[hfaded]fade=t=in:st=0:d={TRANSITION_DURATION}[hall]"
    )
    filters.append(
        f"[v0][hall]concat=n=2:v=1:a=0[out]"
    )

    filter_complex = ";".join(filters)

    # Total video length: cover + horizontal section + endscreen (one crossfade overlap).
    total_video_s = DURATION_IMAGE1 + duration_horizontal + ENDSCREEN_DURATION - CROSSFADE_DURATION

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[out]",
    ]
    if audio_input_idx is not None:
        audio_fade_start = max(0.0, total_video_s - ROTATE_AUDIO_FADE_OUT_S)
        cmd += [
            "-map", f"{audio_input_idx}:a",
            "-c:a", "aac", "-b:a", "192k",
            "-af", f"afade=t=out:st={audio_fade_start:.3f}:d={ROTATE_AUDIO_FADE_OUT_S:.3f}",
            "-shortest",
        ]
    cmd += [
        "-c:v", "libx264",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-movflags", "+faststart",
        output_path,
    ]

    ctx.log("Running ffmpeg...")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    ctx.register_process(proc)
    for line in proc.stdout:
        ctx.log(line.rstrip("\n"))
    rc = proc.wait()
    if rc != 0:
        if ctx.cancelled():
            ctx.log("Cancelled.")
            return
        raise RuntimeError(f"ffmpeg exited with code {rc}")
    ctx.log(f"Video saved to: {output_path}")


def run(*, folder, total_duration_seconds, cover_image, output=None,
        music=None, ctx=None):
    """Build the rotate video.

    folder                   absolute path containing horizontal images + cover
    total_duration_seconds   total length; cover takes 3s, rest is horizontals
    cover_image              filename (relative to folder) of the 9:16 cover
    output                   absolute output .mp4 path; auto in OUTPUT_DIR if None
    music                    either an audio file (used directly) or a folder
                             (random audio file inside is picked)
    ctx                      RunContext
    """
    if ctx is None:
        ctx = RunContext()

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    total_duration = float(total_duration_seconds)
    if total_duration <= DURATION_IMAGE1:
        raise ValueError(
            f"Total duration must be greater than {DURATION_IMAGE1}s (cover duration)"
        )
    duration_horizontal = total_duration - DURATION_IMAGE1

    cover, horizontal = find_images(folder, cover_image)
    ctx.log(f"Cover image:  {cover}")
    for i, img in enumerate(horizontal):
        ctx.log(f"Horizontal {i + 1}: {img}")
    ctx.log(f"Total duration: {total_duration}s "
            f"(cover: {DURATION_IMAGE1}s, horizontal: {duration_horizontal}s)")

    if not os.path.isfile(OVERLAY_PNG):
        raise FileNotFoundError(f"Overlay not found at '{OVERLAY_PNG}'")
    if not os.path.isfile(ENDSCREEN_PNG):
        raise FileNotFoundError(f"Endscreen not found at '{ENDSCREEN_PNG}'")

    if output is None:
        ensure_output_dir()
        folder_name = os.path.basename(os.path.normpath(folder))
        output = os.path.join(OUTPUT_DIR, f"rotate_video_{folder_name}.mp4")
    else:
        output = os.path.abspath(output)
        os.makedirs(os.path.dirname(output), exist_ok=True)

    audio_track = None
    if music:
        audio_track = _resolve_audio_track(music, ctx)
        if audio_track:
            ctx.log(f"Audio track: {audio_track}")

    build_video(cover, horizontal, OVERLAY_PNG, ENDSCREEN_PNG,
                output, duration_horizontal, ctx, audio_track=audio_track)
    return output
