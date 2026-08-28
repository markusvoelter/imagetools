"""Film-strip collage generator.

Arranges a random selection of photos into vertical film strips (black film
base, punched sprocket holes down both edges, photos stacked as frames),
placed side by side across a configurable number of columns. Columns are
lightly staggered and drop a soft shadow for a photo-on-a-lightbox look.
"""

import glob
import os
import random
from datetime import datetime

from PIL import Image, ImageChops, ImageDraw

from . import OUTPUT_DIR, RunContext, ensure_output_dir
from .scroll_video import _center_crop_to


# --- Look & feel (all in output pixels) ---
DEFAULT_BG = (51, 51, 51)          # dark grey background (overridable via `bg`)
FILM_COLOR = (14, 14, 14)          # near-black film base
PHOTO_W = 700                      # photo width inside a strip
CROP_ASPECT = 9 / 16               # frame width : height when cropping to 9:16
FRAME_BORDER = 30                  # black film between/around frames
SPROCKET_MARGIN = 66               # perforated margin on each side of the strip
SPROCKET_HOLE_W = 34               # sprocket hole size
SPROCKET_HOLE_H = 26
SPROCKET_HOLE_GAP = 24             # vertical gap between sprocket holes
SPROCKET_HOLE_RADIUS = 6
STRIP_CORNER_R = 18                # rounded corners of the whole strip
COLUMN_GAP = 44                    # horizontal gap between strips
CANVAS_MARGIN = 70                 # outer (left/right) margin around the collage
MAX_ROTATION_DEG = 10              # max random tilt of each strip from vertical
VERTICAL_OVERFILL_FRAC = 1.0       # extra strip height (of a 9:16 frame) beyond
                                   # the canvas, so strips bleed off top & bottom
JITTER_FRAC = 0.35                 # random vertical offset per strip (of a frame)

PHOTO_H = round(PHOTO_W / CROP_ASPECT)   # 9:16 frame height / stagger reference
STRIP_W = PHOTO_W + 2 * SPROCKET_MARGIN

IMAGE_EXTS = (".jpg", ".jpeg", ".png")


def _list_images(folder):
    files = []
    for ext in IMAGE_EXTS:
        files.extend(glob.glob(os.path.join(folder, f"*{ext}")))
        files.extend(glob.glob(os.path.join(folder, f"*{ext.upper()}")))
    return sorted(set(files))


def _canvas_size(num_columns):
    """Output canvas size for `num_columns` strips, locked to a 9:16 aspect."""
    w = (num_columns * STRIP_W + (num_columns - 1) * COLUMN_GAP
         + 2 * CANVAS_MARGIN)
    h = round(w * 16 / 9)
    return w, h


def _peek_height(path, crop):
    """The frame height this image will occupy, without decoding pixels."""
    if crop:
        return PHOTO_H
    with Image.open(path) as img:
        iw, ih = img.size
    return max(1, round(PHOTO_W * ih / iw))


def _build_strip(photo_imgs):
    """Render one vertical film strip (RGBA) from a list of photos. Each photo
    is PHOTO_W wide; heights may vary (variable-height frames)."""
    k = len(photo_imgs)
    strip_h = sum(im.height for im in photo_imgs) + (k + 1) * FRAME_BORDER
    strip = Image.new("RGBA", (STRIP_W, strip_h), (*FILM_COLOR, 255))
    draw = ImageDraw.Draw(strip)

    # Sprocket holes punched (transparent) down both perforated margins,
    # vertically centered so the run of holes is symmetric.
    pitch = SPROCKET_HOLE_H + SPROCKET_HOLE_GAP
    n_holes = max(1, (strip_h - SPROCKET_HOLE_GAP) // pitch)
    run = n_holes * SPROCKET_HOLE_H + (n_holes - 1) * SPROCKET_HOLE_GAP
    y0 = (strip_h - run) // 2
    left_cx = SPROCKET_MARGIN // 2
    right_cx = STRIP_W - SPROCKET_MARGIN // 2
    for i in range(n_holes):
        cy = y0 + i * pitch + SPROCKET_HOLE_H // 2
        for cx in (left_cx, right_cx):
            draw.rounded_rectangle(
                [cx - SPROCKET_HOLE_W // 2, cy - SPROCKET_HOLE_H // 2,
                 cx + SPROCKET_HOLE_W // 2, cy + SPROCKET_HOLE_H // 2],
                radius=SPROCKET_HOLE_RADIUS, fill=(0, 0, 0, 0))

    # Photos as frames stacked down the middle (each after the previous).
    py = FRAME_BORDER
    for img in photo_imgs:
        strip.paste(img, (SPROCKET_MARGIN, py))
        py += img.height + FRAME_BORDER

    # Round the strip's outer corners (combine with the sprocket transparency).
    corner = Image.new("L", strip.size, 0)
    ImageDraw.Draw(corner).rounded_rectangle(
        [0, 0, STRIP_W - 1, strip_h - 1], radius=STRIP_CORNER_R, fill=255)
    strip.putalpha(ImageChops.multiply(strip.getchannel("A"), corner))
    return strip


def _parse_bg(bg):
    """Return an (r, g, b) tuple from `bg` (a "#rrggbb" hex or tuple), or the
    dark-grey default when `bg` is empty."""
    if not bg:
        return DEFAULT_BG
    if isinstance(bg, (tuple, list)):
        return tuple(bg[:3])
    h = str(bg).lstrip("#").strip()
    if len(h) != 6:
        raise ValueError(f"Background color must be a hex like #333333, got: {bg!r}")
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise ValueError(f"Invalid background color hex: {bg!r}")


def _fit_width(img, target_w):
    """Resize `img` to `target_w` keeping its aspect ratio (no crop); the frame
    height follows the image's own aspect."""
    iw, ih = img.size
    h = max(1, round(target_w * ih / iw))
    return img.resize((target_w, h), Image.LANCZOS)


def _prepare(path, crop):
    """Load `path` as a frame image: 9:16 crop, or width-fit to keep aspect."""
    with Image.open(path) as img:
        rgb = img.convert("RGB")
        return (_center_crop_to(rgb, PHOTO_W, PHOTO_H) if crop
                else _fit_width(rgb, PHOTO_W))


def _render_filmstrip(pool, num_columns, bg_color, output, ctx, crop=True):
    """Fill a 9:16 canvas: consume as many photos from `pool` (mutated) as fit
    across `num_columns` strips, then render to `output`. Returns `output`.

    crop=True  center-crops each photo to a 9:16 frame (uniform frames).
    crop=False keeps the frame width but sets each frame's height from the
               image's own aspect ratio (no cropping; variable-height frames).
    """
    canvas_w, canvas_h = _canvas_size(num_columns)
    # Build strips taller than the canvas so they bleed off the top and bottom
    # (partial frames at both ends) instead of starting on a full, aligned frame.
    target_h = canvas_h + round(PHOTO_H * VERTICAL_OVERFILL_FRAC)

    # Balanced fill: always add the next photo to the shortest column, until
    # every column has overshot `target_h` (so it overflows the canvas height).
    columns = [[] for _ in range(num_columns)]
    col_h = [FRAME_BORDER] * num_columns
    full = [False] * num_columns
    while pool and not all(full):
        ctx.check_cancelled()
        c = min((c for c in range(num_columns) if not full[c]),
                key=lambda c: col_h[c])
        h = _peek_height(pool[0], crop)
        columns[c].append(_prepare(pool.pop(0), crop))
        col_h[c] += h + FRAME_BORDER
        if col_h[c] >= target_h:
            full[c] = True

    columns = [col for col in columns if col]
    if not columns:
        return None
    counts = "/".join(str(len(c)) for c in columns)
    ctx.log(f"Film strip: {sum(len(c) for c in columns)} photo(s) across "
            f"{len(columns)} column(s) ({counts} per column); "
            f"{'cropped to 9:16' if crop else 'fit to image aspect'}.")

    canvas = Image.new("RGB", (canvas_w, canvas_h), bg_color)
    jitter_max = round(PHOTO_H * JITTER_FRAC)
    # all strips lean the same way (one random direction), each by a different
    # amount, so the composition reads as intentional rather than scattered
    tilt_sign = random.choice((-1, 1))
    for c, col in enumerate(columns):
        ctx.check_cancelled()
        strip = _build_strip(col)
        # random tilt magnitude in the shared direction; strips may overlap
        angle = tilt_sign * random.uniform(0, MAX_ROTATION_DEG)
        rot = strip.rotate(angle, expand=True, resample=Image.BICUBIC)
        # centre in the column, with a random vertical offset so the partial
        # top/bottom frames don't line up between columns
        x_center = CANVAS_MARGIN + c * (STRIP_W + COLUMN_GAP) + STRIP_W / 2
        y_center = canvas_h / 2 + random.uniform(-jitter_max, jitter_max)
        px = round(x_center - rot.width / 2)
        py = round(y_center - rot.height / 2)
        canvas.paste(rot, (px, py), rot)

    canvas.save(output, "JPEG", quality=92)
    ctx.log(f"Saved {canvas.width}x{canvas.height} (9:16) film strip to {output}")
    return output


def run(*, folder, num_columns=2, repetitions=1, allow_repeat=False,
        crop=True, bg=None, output=None, ctx=None):
    """Build one or more 9:16 film-strip collages.

    folder        absolute path with source images
    num_columns   number of vertical film strips side by side
    repetitions   how many output images to generate. Each output is a full
                  9:16 canvas filled with as many photos as fit.
    allow_repeat  if False (default), photos are not reused across outputs
                  (each repetition draws from the remaining pool, stopping when
                  the folder is exhausted). If True, every output draws freshly
                  from the whole folder, so photos may repeat across outputs.
    crop          if True, center-crop each photo to a uniform 9:16 frame;
                  if False, keep the frame width and set each frame's height
                  from the image's own aspect ratio (no cropping)
    bg            background colour as "#rrggbb" hex; dark grey when omitted
    output        absolute output .jpg path; auto in OUTPUT_DIR if None. With
                  repetitions > 1 a "_NN" index is appended to each file
    ctx           RunContext
    """
    if ctx is None:
        ctx = RunContext()
    if num_columns < 1:
        raise ValueError("Number of columns must be at least 1.")
    if repetitions < 1:
        raise ValueError("Repetitions must be at least 1.")

    bg_color = _parse_bg(bg)

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    files = _list_images(folder)
    if output:
        out_abs = os.path.abspath(output)
        files = [f for f in files if os.path.abspath(f) != out_abs]
    if not files:
        raise RuntimeError(f"No images found in {folder}")

    # Resolve an output base/extension used to name each repetition.
    if output:
        output = os.path.abspath(output)
        os.makedirs(os.path.dirname(output), exist_ok=True)
        base, ext = os.path.splitext(output)
    else:
        ensure_output_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(OUTPUT_DIR,
                            f"{os.path.basename(folder)}_filmstrip_{ts}")
        ext = ".jpg"

    # Without allow_repeat, one shared pool is consumed across repetitions so no
    # photo is reused; with allow_repeat, each output draws the full folder fresh.
    shared_pool = list(files)
    random.shuffle(shared_pool)
    reuse_note = "" if (allow_repeat or repetitions == 1) else " (no reuse across outputs)"
    ctx.log(f"{len(files)} image(s) available; filling 9:16 canvas per output"
            f"{reuse_note}.")

    last = None
    for i in range(repetitions):
        ctx.check_cancelled()
        if allow_repeat:
            pool = list(files)
            random.shuffle(pool)
        else:
            pool = shared_pool
            if not pool:
                ctx.log(f"Ran out of unique images; stopped after {i} of "
                        f"{repetitions} repetition(s).")
                break
        if repetitions > 1:
            ctx.log(f"--- Repetition {i + 1}/{repetitions} ---")
            out_path = f"{base}_{i + 1:02d}{ext}"
        else:
            out_path = base + ext
        result = _render_filmstrip(pool, num_columns, bg_color, out_path, ctx,
                                   crop=crop)
        if result is not None:
            last = result

    return last
