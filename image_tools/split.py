"""Split a single (landscape) photo into multiple Instagram carousel slides.

Each output slide keeps the original photo's height and uses a width derived
from the chosen aspect ratio. When the source is wider than one slide, slides
overlap evenly so every output is full-bleed; when the source is narrower,
a single padded slide is produced.
"""

import os
from datetime import datetime

from PIL import Image

from . import OUTPUT_DIR, RunContext, ensure_output_dir
from .carousel import add_swipe_indicator


ASPECT_RATIOS = {
    "9:16": (9, 16),
    "3:4":  (3, 4),
    "1:1":  (1, 1),
    "4:3":  (4, 3),
    "16:9": (16, 9),
}

DEFAULT_BG = (0, 0, 0)


def run(*, image, aspect_ratio="9:16", output_dir=None, bg=None, ctx=None):
    """Slice `image` into carousel slides at the given aspect ratio.

    image         absolute path to a single source photo
    aspect_ratio  one of ASPECT_RATIOS keys
    output_dir    absolute output dir; defaults to OUTPUT_DIR/split_<name>_<ts>/
    bg            hex padding color used only when source is narrower than one slide
    ctx           RunContext
    """
    if ctx is None:
        ctx = RunContext()

    image = os.path.abspath(image)
    if not os.path.isfile(image):
        raise ValueError(f"Not a file: {image}")
    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"Unknown aspect ratio: {aspect_ratio}")

    if bg:
        h = bg.lstrip("#")
        bg_color = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    else:
        bg_color = DEFAULT_BG

    img = Image.open(image).convert("RGB")
    src_w, src_h = img.size
    ar_w, ar_h = ASPECT_RATIOS[aspect_ratio]
    piece_w = round(src_h * ar_w / ar_h)
    if piece_w <= 0:
        raise RuntimeError("Computed slide width is zero.")

    ctx.log(f"Input: {image} ({src_w}x{src_h})")
    ctx.log(f"Slide size: {piece_w}x{src_h} ({aspect_ratio})")

    if output_dir is None:
        ensure_output_dir()
        basename = os.path.splitext(os.path.basename(image))[0]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join(OUTPUT_DIR, f"split_{basename}_{ts}")
    else:
        output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    pieces = []
    if src_w <= piece_w:
        # Input narrower than (or exactly) one slide — pad to slide width.
        piece = Image.new("RGB", (piece_w, src_h), bg_color)
        offset = (piece_w - src_w) // 2
        piece.paste(img, (offset, 0))
        pieces.append(piece)
        ctx.log(f"Source narrower than one slide; producing 1 padded slide.")
    else:
        num_pieces = -(-src_w // piece_w)  # ceil
        step = (src_w - piece_w) / (num_pieces - 1) if num_pieces > 1 else 0
        for i in range(num_pieces):
            ctx.check_cancelled()
            left = round(i * step)
            pieces.append(img.crop((left, 0, left + piece_w, src_h)))
        overlap = piece_w - step if num_pieces > 1 else 0
        ctx.log(f"Number of slides: {num_pieces} "
                f"(overlap between slides: {overlap:.0f}px)")

    for i, piece in enumerate(pieces):
        ctx.check_cancelled()
        if i == 0:
            piece = add_swipe_indicator(piece)
        out_path = os.path.join(output_dir, f"slide_{i + 1:02d}.jpg")
        piece.save(out_path, quality=95)
        ctx.log(f"  Saved {out_path}")

    return output_dir
