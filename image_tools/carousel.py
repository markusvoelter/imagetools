"""Cut a horizontal panorama into Instagram carousel slides (JPEG files)."""

import os
from datetime import datetime, timedelta

import piexif
from PIL import Image, ImageDraw, ImageFont

from . import RunContext
from ._panorama import ASPECT_RATIOS, build_panorama_strip


def add_swipe_indicator(slide):
    """Add a 'SWIPE' chip at the bottom-left of a slide. Reused by split.py."""
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
    num_slides    target slide count (may be reduced if not enough source)
    aspect_ratio  key into ASPECT_RATIOS
    output_dir    absolute output dir; defaults to sibling of `folder` named
                  "<folder>-swipey"
    ctx           RunContext
    """
    if ctx is None:
        ctx = RunContext()

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    if output_dir is None:
        parent = os.path.dirname(folder)
        output_dir = os.path.join(parent, os.path.basename(folder) + "-swipey")
    else:
        output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    ctx.log(f"Output folder: {output_dir}")

    strip, num_slides, section_width, working_height, out_width, out_height = (
        build_panorama_strip(folder, num_slides, aspect_ratio, ctx)
    )

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
