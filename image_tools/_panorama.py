"""Shared helper: assemble a horizontal panorama strip from a folder of source
images. Used by both `carousel` (cuts the strip into slide JPEGs) and
`scroll_video` (renders the strip as a panning MP4).
"""

import os
import random
import re

from PIL import Image

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


def build_panorama_strip(folder, num_slides, aspect_ratio, ctx=None,
                         random_order=False):
    """Assemble a horizontal strip from images in `folder`.

    If `num_slides` is an int: build a strip exactly `num_slides * section_width`
    wide, stopping once enough source images are accumulated. May reduce
    `num_slides` (and pad the last section) if there aren't enough images.

    If `num_slides` is None: include every image in the folder and derive the
    slide count from the resulting width (rounded up to whole slides).

    If `random_order` is True, source images are shuffled instead of
    natural-sorted by filename, so only the images needed to fill the strip are
    picked, in random order.

    Returns (strip, num_slides_actual, section_width, working_height,
             out_width, out_height).
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
    use_all = num_slides is None
    total_needed = None if use_all else num_slides * section_width

    ctx.log(f"Output slide size: {out_width}x{out_height} ({aspect_ratio})")
    ctx.log(f"Working height: {working_height}, section width: {section_width}")
    if use_all:
        ctx.log("Using all images in folder.")
    else:
        ctx.log(f"Total strip width needed: {total_needed}px for {num_slides} slides")

    files = [f for f in os.listdir(folder)
             if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    if not files:
        raise RuntimeError(f"No images found in {folder}")

    if random_order:
        random.shuffle(files)
        ctx.log("Random order: source images shuffled.")
    else:
        files.sort(key=natural_sort_key)

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
        scaled.append((img, gap))
        accumulated_width += gap + new_width
        ctx.log(f"  {f}: {new_width}x{working_height}  "
                f"(total so far: {accumulated_width}px)")

        if not use_all and accumulated_width >= total_needed:
            break

    if use_all:
        num_slides = max(1, -(-accumulated_width // section_width))  # ceil
        total_needed = num_slides * section_width
        remaining = total_needed - accumulated_width
        if remaining > 0:
            ctx.log(f"Producing {num_slides} slides "
                    f"(last slide has {remaining}px black padding)")
        else:
            ctx.log(f"Producing {num_slides} slides (exact fit)")
    elif accumulated_width < total_needed:
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

    return strip, num_slides, section_width, working_height, out_width, out_height
