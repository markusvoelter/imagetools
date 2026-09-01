"""Composite a randomly-chosen photo into the black rectangle of a wall scaffold."""

import os
import random
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import binary_closing, label

from . import OUTPUT_DIR, RunContext, ensure_output_dir


BLACK_THRESHOLD = 15
MIN_AREA_FRAC = 0.005
MIN_RECTANGULARITY = 0.9
FRAME_FRAC = 0.025


def find_empty_rectangle(wall_img):
    arr = np.array(wall_img.convert("RGB"))
    h, w, _ = arr.shape
    total = h * w

    black_mask = np.all(arr <= BLACK_THRESHOLD, axis=2)
    black_mask = binary_closing(black_mask, iterations=2)
    labeled, num = label(black_mask)
    if num == 0:
        raise RuntimeError("No black region found in wall image")

    best = None
    for lbl in range(1, num + 1):
        ys, xs = np.where(labeled == lbl)
        area = len(xs)
        if area / total < MIN_AREA_FRAC:
            continue
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        bbox_area = (x1 - x0 + 1) * (y1 - y0 + 1)
        rectangularity = area / bbox_area
        if rectangularity < MIN_RECTANGULARITY:
            continue
        if best is None or area > best[0]:
            best = (area, (x0, y0, x1, y1))

    if best is None:
        raise RuntimeError("Could not identify a rectangular empty area")
    return best[1]


def fit_image_to_box(img, box_w, box_h):
    iw, ih = img.size
    scale = max(box_w / iw, box_h / ih)
    new_w, new_h = int(round(iw * scale)), int(round(ih * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - box_w) // 2
    top = (new_h - box_h) // 2
    return img.crop((left, top, left + box_w, top + box_h))


def pick_image_for_ratio(image_files, target_ratio):
    weights = []
    for f in image_files:
        try:
            with Image.open(f) as im:
                ratio = im.size[0] / im.size[1]
        except Exception:
            ratio = target_ratio
        diff = abs(np.log(ratio) - np.log(target_ratio))
        weights.append(1.0 / (0.05 + diff))
    return random.choices(image_files, weights=weights, k=1)[0]


def compose(wall_path, image_files, output_path):
    wall = Image.open(wall_path).convert("RGB")
    x0, y0, x1, y1 = find_empty_rectangle(wall)
    box_w = x1 - x0 + 1
    box_h = y1 - y0 + 1

    frame_thickness = max(2, int(round(min(box_w, box_h) * FRAME_FRAC)))
    inner_w = box_w - 2 * frame_thickness
    inner_h = box_h - 2 * frame_thickness
    target_ratio = inner_w / inner_h

    img_path = pick_image_for_ratio(image_files, target_ratio)
    photo = Image.open(img_path).convert("RGB")
    photo_fitted = fit_image_to_box(photo, inner_w, inner_h)

    wall.paste(photo_fitted, (x0 + frame_thickness, y0 + frame_thickness))
    wall.save(output_path)
    return img_path


def collect_files(folder, exts):
    folder = Path(folder)
    files = []
    for ext in exts:
        files.extend(folder.glob(f"*{ext}"))
        files.extend(folder.glob(f"*{ext.upper()}"))
    return sorted(set(files))


def run(*, wall_folder, image_folder, num_outputs, output_dir=None, seed=None,
        ctx=None):
    """Generate `num_outputs` composites.

    wall_folder    folder of wall scaffold images (PNG/JPG)
    image_folder   folder of source photos
    num_outputs    how many composites to produce
    output_dir     where to write composites; defaults to a fresh timestamped
                   subfolder of OUTPUT_DIR
    ctx            RunContext
    """
    if ctx is None:
        ctx = RunContext()
    if seed is not None:
        random.seed(seed)

    wall_folder = os.path.abspath(wall_folder)
    image_folder = os.path.abspath(image_folder)
    if not os.path.isdir(wall_folder):
        raise ValueError(f"Wall folder not a directory: {wall_folder}")
    if not os.path.isdir(image_folder):
        raise ValueError(f"Image folder not a directory: {image_folder}")

    num_outputs = int(num_outputs)
    if num_outputs < 1:
        raise ValueError("num_outputs must be at least 1.")

    wall_files = collect_files(wall_folder, (".png", ".jpg", ".jpeg"))
    image_files = collect_files(image_folder, (".jpg", ".jpeg", ".png"))
    if not wall_files:
        raise RuntimeError(f"No wall files found in {wall_folder}")
    if not image_files:
        raise RuntimeError(f"No image files found in {image_folder}")

    if output_dir is None:
        ensure_output_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join(OUTPUT_DIR, f"walls_{ts}")
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    ctx.log(f"Walls:  {len(wall_files)} candidates in {wall_folder}")
    ctx.log(f"Photos: {len(image_files)} candidates in {image_folder}")
    ctx.log(f"Output: {output_dir}")

    for i in range(num_outputs):
        ctx.check_cancelled()
        wall_path = random.choice(wall_files)
        out_path = os.path.join(output_dir, f"composite_{i + 1:03d}.png")
        try:
            used_img = compose(wall_path, image_files, out_path)
            ctx.log(f"[{i + 1}/{num_outputs}] {wall_path.name} + "
                    f"{used_img.name} -> {out_path}")
        except Exception as e:
            ctx.log(f"[{i + 1}/{num_outputs}] FAILED on {wall_path.name}: {e}")

    return output_dir
