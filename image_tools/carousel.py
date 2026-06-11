"""Joins images horizontally and cuts into Instagram carousel slides."""

import os
import re
from datetime import datetime, timedelta

import piexif
from PIL import Image, ImageDraw, ImageFont

from . import RunContext


GAP_PX = 5
BASE_WIDTH = 1080

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


def run(*, folder, num_slides=20, aspect_ratio="9:16",
        output_dir=None, ctx=None):
    """Build the carousel.

    folder        absolute path with source images
    num_slides    integer target slide count (may be reduced if not enough source)
    aspect_ratio  key into ASPECT_RATIOS
    output_dir    absolute output dir; defaults to sibling of `folder` named
                  "<folder>-swipey"
    ctx           RunContext
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

    ctx.log("Done!")
    return output_dir
