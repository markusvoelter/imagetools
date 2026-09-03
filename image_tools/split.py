"""Split every (landscape) photo in a folder into Instagram carousel slides.

Each output slide keeps the original photo's height and uses a width derived
from the chosen aspect ratio. Slides are non-overlapping tiles of the source
so scrolling through the carousel reveals a continuous panorama; if the
source width isn't an exact multiple of a slide, the final slide is padded
with a heavily blurred version of the source. A single padded slide is
produced when the source is narrower than one slide.
"""

import os
import shutil
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFilter

from . import RunContext
from .carousel import add_swipe_indicator


ASPECT_RATIOS = {
    "9:16": (9, 16),
    "3:4":  (3, 4),
    "1:1":  (1, 1),
    "4:3":  (4, 3),
    "16:9": (16, 9),
}

DEFAULT_BG = (0, 0, 0)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}

# Overview slide: a very heavy Gaussian blur is applied to the background
# crop so it reads as an out-of-focus ambient wash behind the fitted image.
OVERVIEW_BLUR_RADIUS_FRAC = 0.04  # blur radius as fraction of slide width

# If the final tile would use less than this fraction of a slide's width in
# real image content (the rest being blurred padding), drop it entirely.
MIN_LAST_SLIDE_FRAC = 0.20


def _blurred_center_bg(img, out_w, out_h):
    """Heavily-blurred `out_w × out_h` background derived from a center crop
    of `img` matching the target aspect ratio."""
    src_w, src_h = img.size
    slide_aspect = out_w / out_h
    src_aspect = src_w / src_h
    if src_aspect > slide_aspect:
        bg_crop_w = max(1, int(round(src_h * slide_aspect)))
        left = (src_w - bg_crop_w) // 2
        bg_crop = img.crop((left, 0, left + bg_crop_w, src_h))
    else:
        bg_crop_h = max(1, int(round(src_w / slide_aspect)))
        top = (src_h - bg_crop_h) // 2
        bg_crop = img.crop((0, top, src_w, top + bg_crop_h))
    bg = bg_crop.resize((out_w, out_h), Image.LANCZOS)
    blur_radius = max(20, int(round(out_w * OVERVIEW_BLUR_RADIUS_FRAC)))
    return bg.filter(ImageFilter.GaussianBlur(radius=blur_radius))


def _build_overview_slide(img, piece_w, piece_h):
    """Return a `piece_w × piece_h` slide showing the entire `img` fit
    (letterboxed) inside the slide, backed by a heavily-blurred, center-cropped
    version of `img` filling the empty space above/below (or beside)."""
    src_w, src_h = img.size
    bg = _blurred_center_bg(img, piece_w, piece_h)

    scale = min(piece_w / src_w, piece_h / src_h)
    fg_w = max(1, int(round(src_w * scale)))
    fg_h = max(1, int(round(src_h * scale)))
    fg = img.resize((fg_w, fg_h), Image.LANCZOS)

    canvas = bg.copy()
    canvas.paste(fg, ((piece_w - fg_w) // 2, (piece_h - fg_h) // 2))
    return canvas


def _split_one(image, aspect_ratio, output_dir, bg_color, ctx):
    """Split a single image into carousel slides in `output_dir`."""
    img = Image.open(image).convert("RGB")
    src_w, src_h = img.size
    ar_w, ar_h = ASPECT_RATIOS[aspect_ratio]
    piece_w = round(src_h * ar_w / ar_h)
    if piece_w <= 0:
        raise RuntimeError(f"Computed slide width is zero for {image}.")

    ctx.log(f"Input: {image} ({src_w}x{src_h})")
    ctx.log(f"Slide size: {piece_w}x{src_h} ({aspect_ratio})")

    os.makedirs(output_dir, exist_ok=True)

    # Keep a copy of the untouched source alongside the slides.
    original_copy = os.path.join(
        output_dir, f"_original{os.path.splitext(image)[1].lower()}")
    shutil.copy2(image, original_copy)
    ctx.log(f"  Saved original {original_copy}")

    pieces = []
    if src_w <= piece_w:
        # Input narrower than (or exactly) one slide — pad to slide width.
        piece = Image.new("RGB", (piece_w, src_h), bg_color)
        offset = (piece_w - src_w) // 2
        piece.paste(img, (offset, 0))
        pieces.append(piece)
        ctx.log(f"Source narrower than one slide; producing 1 padded slide.")
    else:
        # Non-overlapping tiles at fixed piece_w stride; if the source
        # width isn't an exact multiple, the last tile is short and gets
        # padded on the right with a heavily-blurred backdrop so it stays
        # full-bleed and visually cohesive.
        num_pieces = -(-src_w // piece_w)  # ceil
        # If the final tile has very little real content (mostly padding),
        # drop it rather than emit a mostly-blurred slide.
        last_content = src_w - (num_pieces - 1) * piece_w
        if num_pieces > 1 and last_content < MIN_LAST_SLIDE_FRAC * piece_w:
            ctx.log(f"Last slide would use only {last_content}px "
                    f"({last_content / piece_w:.0%} of a slide); dropping it.")
            num_pieces -= 1
        last_pad = max(0, num_pieces * piece_w - src_w)
        for i in range(num_pieces):
            ctx.check_cancelled()
            left = i * piece_w
            right = min(left + piece_w, src_w)
            crop = img.crop((left, 0, right, src_h))
            if right - left < piece_w:
                piece = _blurred_center_bg(img, piece_w, src_h).copy()
                piece.paste(crop, (0, 0))
            else:
                piece = crop
            pieces.append(piece)
        pad_note = (f"; last slide padded {last_pad}px with blurred bg"
                    if last_pad > 0 else "")
        ctx.log(f"Number of slides: {num_pieces}{pad_note}")

    for i, piece in enumerate(pieces):
        ctx.check_cancelled()
        if i == 0:
            piece = add_swipe_indicator(piece)
        out_path = os.path.join(output_dir, f"slide_{i + 1:02d}.jpg")
        piece.save(out_path, quality=95)
        ctx.log(f"  Saved {out_path}")

    # Trailing overview slide: only meaningful when the source was actually
    # split (otherwise the single padded slide is already the whole image).
    if len(pieces) > 1:
        ctx.check_cancelled()
        overview = _build_overview_slide(img, piece_w, src_h)
        out_path = os.path.join(
            output_dir, f"slide_{len(pieces) + 1:02d}.jpg")
        overview.save(out_path, quality=95)
        ctx.log(f"  Saved {out_path} (full-image overview)")


def run(*, folder, aspect_ratio="9:16", output_dir=None, bg=None, ctx=None):
    """Slice every image in `folder` into carousel slides.

    folder        absolute path to a folder of source photos
    aspect_ratio  one of ASPECT_RATIOS keys
    output_dir    absolute parent dir for per-image `split_<name>_<ts>/`
                  subfolders; defaults to `folder` itself (subfolders sit
                  next to the source images)
    bg            hex padding color used only when a source is narrower
                  than one slide
    ctx           RunContext
    """
    if ctx is None:
        ctx = RunContext()

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a folder: {folder}")
    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"Unknown aspect ratio: {aspect_ratio}")

    if bg:
        h = bg.lstrip("#")
        bg_color = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    else:
        bg_color = DEFAULT_BG

    images = sorted(
        (p for p in Path(folder).iterdir()
         if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda p: p.name.lower(),
    )
    if not images:
        raise ValueError(f"No images found in {folder}")

    if output_dir is not None:
        output_dir = os.path.abspath(output_dir)
        os.makedirs(output_dir, exist_ok=True)
    parent_dir = output_dir if output_dir is not None else folder

    ctx.log(f"Found {len(images)} image(s) in {folder}")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    for image_path in images:
        ctx.check_cancelled()
        image_out_dir = os.path.join(
            parent_dir, f"split_{image_path.stem}_{ts}")
        _split_one(
            image=str(image_path),
            aspect_ratio=aspect_ratio,
            output_dir=image_out_dir,
            bg_color=bg_color,
            ctx=ctx,
        )

    return parent_dir
