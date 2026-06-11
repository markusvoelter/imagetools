"""Collage generator with four layout styles: grid, columns, creative, mosaic."""

import glob
import math
import os
import random
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFilter

from . import OUTPUT_DIR, RunContext, ensure_output_dir


# --- Shared settings ---
CANVAS_WIDTH = 4000
DEFAULT_BG = (51, 51, 51)

# --- Grid settings ---
GRID_GAP = 15
TARGET_ROW_HEIGHT = 550
MIN_ROW_HEIGHT = 300
MAX_ROW_HEIGHT = 900

# --- Column grid settings ---
TARGET_COL_WIDTH = 550
MIN_COL_WIDTH = 300
MAX_COL_WIDTH = 900
COLUMN_CANVAS_HEIGHT = 4000

# --- Mosaic settings ---
MOSAIC_GRID_COLS = 12
MOSAIC_MIN_IMAGE_DIM = 200
MOSAIC_TILE_SIZES = [
    (1, 1), (2, 1), (1, 2), (2, 2),
    (3, 2), (2, 3), (3, 3), (4, 3), (3, 4),
]

# --- Creative settings ---
CREATIVE_CANVAS_HEIGHT = 2800
MARGIN = 40
SHADOW_OFFSET = 8
SHADOW_BLUR = 20
SHADOW_COLOR = (0, 0, 0, 80)
MAX_ROTATION = 6.0
OVERLAP_FRACTION = 0.04

STYLES = ("grid", "columns", "creative", "mosaic")


# =========================================================================
#  Shared
# =========================================================================

def load_images(folder, exclude=None, ctx=None):
    paths = sorted(glob.glob(os.path.join(folder, "*.jpg")))
    if exclude:
        exclude_abs = os.path.abspath(exclude)
        paths = [p for p in paths if os.path.abspath(p) != exclude_abs]
    images = []
    for p in paths:
        try:
            with Image.open(p) as img:
                w, h = img.size
            images.append({"path": p, "w": w, "h": h, "aspect": w / h})
        except Exception as e:
            if ctx:
                ctx.log(f"  Skipping {os.path.basename(p)}: {e}")
    return images


def open_and_resize(img_data, target_w, target_h):
    with Image.open(img_data["path"]) as img:
        return img.resize((target_w, target_h), Image.LANCZOS)


def print_image_list(images, ctx):
    for im in images:
        orient = "portrait" if im["aspect"] < 0.9 else (
            "square" if im["aspect"] <= 1.2 else "landscape")
        ctx.log(f"  {os.path.basename(im['path'])}: "
                f"{im['w']}x{im['h']} ({orient}, ar={im['aspect']:.2f})")


# =========================================================================
#  Grid layout
# =========================================================================

def compute_row_height(aspects, canvas_w, gap):
    n = len(aspects)
    total_gaps = gap * (n - 1)
    available_w = canvas_w - total_gaps
    return available_w / sum(aspects)


def row_cost(height):
    ratio = height / TARGET_ROW_HEIGHT
    if height < MIN_ROW_HEIGHT or height > MAX_ROW_HEIGHT:
        return float("inf")
    return (ratio - 1.0) ** 2


def dp_layout(images, canvas_w, gap):
    n = len(images)
    aspects = [im["aspect"] for im in images]
    INF = float("inf")
    dp = [INF] * (n + 1)
    dp[0] = 0.0
    parent = [0] * (n + 1)

    for i in range(1, n + 1):
        for j in range(i, 0, -1):
            row_aspects = aspects[j - 1:i]
            h = compute_row_height(row_aspects, canvas_w, gap)
            c = row_cost(h)
            if c == INF:
                if h < MIN_ROW_HEIGHT:
                    break
                continue
            total = dp[j - 1] + c
            if total < dp[i]:
                dp[i] = total
                parent[i] = j - 1

    rows = []
    i = n
    while i > 0:
        j = parent[i]
        rows.append(list(range(j, i)))
        i = j
    rows.reverse()
    return rows


def interleave_by_aspect(images):
    portraits = sorted([i for i in range(len(images)) if images[i]["aspect"] < 1.0],
                       key=lambda i: images[i]["aspect"])
    landscapes = sorted([i for i in range(len(images)) if images[i]["aspect"] >= 1.0],
                        key=lambda i: images[i]["aspect"])
    result = []
    li, pi = 0, 0
    while li < len(landscapes) or pi < len(portraits):
        for _ in range(3):
            if li < len(landscapes):
                result.append(landscapes[li])
                li += 1
        if pi < len(portraits):
            result.append(portraits[pi])
            pi += 1
    return result


def make_grid_collage(images, output_path, ctx, bg_color=DEFAULT_BG):
    orderings = {
        "original": list(range(len(images))),
        "by_aspect": sorted(range(len(images)), key=lambda i: images[i]["aspect"]),
        "by_aspect_rev": sorted(range(len(images)),
                                key=lambda i: images[i]["aspect"], reverse=True),
        "landscapes_first": (
            sorted([i for i in range(len(images)) if images[i]["aspect"] >= 1.0],
                   key=lambda i: images[i]["aspect"]) +
            sorted([i for i in range(len(images)) if images[i]["aspect"] < 1.0],
                   key=lambda i: images[i]["aspect"])
        ),
        "interleaved": interleave_by_aspect(images),
    }
    for r in range(50):
        order = list(range(len(images)))
        random.shuffle(order)
        orderings[f"random_{r}"] = order

    best_result = None
    best_cost = float("inf")

    for name, order in orderings.items():
        ctx.check_cancelled()
        ordered = [images[i] for i in order]
        rows = dp_layout(ordered, CANVAS_WIDTH, GRID_GAP)
        total_cost = 0
        total_height = 0
        row_info = []
        for row_indices in rows:
            row_aspects = [ordered[i]["aspect"] for i in row_indices]
            h = compute_row_height(row_aspects, CANVAS_WIDTH, GRID_GAP)
            h = max(MIN_ROW_HEIGHT, min(MAX_ROW_HEIGHT, h))
            total_cost += row_cost(h)
            row_info.append((row_indices, h))
            total_height += h
        total_height += GRID_GAP * (len(rows) - 1)
        if total_cost < best_cost:
            best_cost = total_cost
            best_result = (ordered, row_info, int(total_height), name)

    ordered, row_info, canvas_h, best_name = best_result
    ctx.log(f"\nBest ordering: {best_name}")
    ctx.log(f"Canvas: {CANVAS_WIDTH}x{canvas_h}")
    ctx.log(f"Rows: {len(row_info)}")

    canvas = Image.new("RGB", (CANVAS_WIDTH, canvas_h), bg_color)
    y = 0
    for row_indices, row_h in row_info:
        ctx.check_cancelled()
        row_h_int = int(round(row_h))
        x = 0
        for idx, img_idx in enumerate(row_indices):
            img_data = ordered[img_idx]
            iw = int(round(row_h * img_data["aspect"]))
            if idx == len(row_indices) - 1:
                iw = CANVAS_WIDTH - x
            resized = open_and_resize(img_data, iw, row_h_int)
            canvas.paste(resized, (x, y))
            del resized
            x += iw + GRID_GAP
        y += row_h_int + GRID_GAP

    canvas = canvas.crop((0, 0, CANVAS_WIDTH, y - GRID_GAP))
    canvas.save(output_path, "JPEG", quality=92)
    final_h = y - GRID_GAP
    ctx.log(f"Saved collage to {output_path} ({CANVAS_WIDTH}x{final_h})")


# =========================================================================
#  Column grid layout
# =========================================================================

def compute_col_width(inv_aspects, canvas_h, gap):
    n = len(inv_aspects)
    total_gaps = gap * (n - 1)
    available_h = canvas_h - total_gaps
    return available_h / sum(inv_aspects)


def col_cost(width):
    ratio = width / TARGET_COL_WIDTH
    if width < MIN_COL_WIDTH or width > MAX_COL_WIDTH:
        return float("inf")
    return (ratio - 1.0) ** 2


def dp_column_layout(images, canvas_h, gap):
    n = len(images)
    inv_aspects = [1.0 / im["aspect"] for im in images]
    INF = float("inf")
    dp = [INF] * (n + 1)
    dp[0] = 0.0
    parent = [0] * (n + 1)

    for i in range(1, n + 1):
        for j in range(i, 0, -1):
            col_inv = inv_aspects[j - 1:i]
            w = compute_col_width(col_inv, canvas_h, gap)
            c = col_cost(w)
            if c == INF:
                if w < MIN_COL_WIDTH:
                    break
                continue
            total = dp[j - 1] + c
            if total < dp[i]:
                dp[i] = total
                parent[i] = j - 1

    cols = []
    i = n
    while i > 0:
        j = parent[i]
        cols.append(list(range(j, i)))
        i = j
    cols.reverse()
    return cols


def fixed_column_layout(images, num_cols, canvas_h, gap):
    n = len(images)
    base = n // num_cols
    remainder = n % num_cols
    cols = []
    idx = 0
    for c in range(num_cols):
        size = base + (1 if c < remainder else 0)
        cols.append(list(range(idx, idx + size)))
        idx += size
    return cols


def compute_column_canvas_height(images, num_cols):
    total_inv_ar = sum(1.0 / im["aspect"] for im in images)
    total_gaps = GRID_GAP * (num_cols - 1)
    target_w = CANVAS_WIDTH - total_gaps
    h = target_w * total_inv_ar / (num_cols * num_cols)
    return max(int(h), 1000)


def make_columns_collage(images, output_path, ctx, num_cols=None, bg_color=DEFAULT_BG):
    if num_cols:
        canvas_h = compute_column_canvas_height(images, num_cols)
        ctx.log(f"Computed canvas height: {canvas_h} (for {num_cols} columns)")
    else:
        canvas_h = COLUMN_CANVAS_HEIGHT

    orderings = {
        "original": list(range(len(images))),
        "by_aspect": sorted(range(len(images)), key=lambda i: images[i]["aspect"]),
        "by_aspect_rev": sorted(range(len(images)),
                                key=lambda i: images[i]["aspect"], reverse=True),
        "interleaved": interleave_by_aspect(images),
    }
    for r in range(50):
        order = list(range(len(images)))
        random.shuffle(order)
        orderings[f"random_{r}"] = order

    best_result = None
    best_cost = float("inf")

    for name, order in orderings.items():
        ctx.check_cancelled()
        ordered = [images[i] for i in order]
        if num_cols:
            cols = fixed_column_layout(ordered, num_cols, canvas_h, GRID_GAP)
        else:
            cols = dp_column_layout(ordered, canvas_h, GRID_GAP)
        total_cost = 0
        total_width = 0
        col_info = []
        col_widths = []
        for col_indices in cols:
            col_inv = [1.0 / ordered[i]["aspect"] for i in col_indices]
            w = compute_col_width(col_inv, canvas_h, GRID_GAP)
            if not num_cols:
                w = max(MIN_COL_WIDTH, min(MAX_COL_WIDTH, w))
                total_cost += col_cost(w)
            col_widths.append(w)
            col_info.append((col_indices, w))
            total_width += w
        total_width += GRID_GAP * (len(cols) - 1)
        if num_cols:
            avg_w = sum(col_widths) / len(col_widths)
            total_cost = sum((w - avg_w) ** 2 for w in col_widths)
        if total_cost < best_cost:
            best_cost = total_cost
            best_result = (ordered, col_info, int(total_width), name)

    ordered, col_info, canvas_w, best_name = best_result
    ctx.log(f"\nBest ordering: {best_name}")
    ctx.log(f"Canvas: {canvas_w}x{canvas_h}")
    ctx.log(f"Columns: {len(col_info)}")

    canvas = Image.new("RGB", (canvas_w, canvas_h), bg_color)
    x = 0
    for col_indices, col_w in col_info:
        ctx.check_cancelled()
        col_w_int = int(round(col_w))
        y = 0
        for idx, img_idx in enumerate(col_indices):
            img_data = ordered[img_idx]
            ih = int(round(col_w / img_data["aspect"]))
            if idx == len(col_indices) - 1:
                ih = canvas_h - y
            resized = open_and_resize(img_data, col_w_int, ih)
            canvas.paste(resized, (x, y))
            del resized
            y += ih + GRID_GAP
        x += col_w_int + GRID_GAP

    canvas = canvas.crop((0, 0, x - GRID_GAP, canvas_h))
    final_w = x - GRID_GAP
    canvas.save(output_path, "JPEG", quality=92)
    ctx.log(f"Saved collage to {output_path} ({final_w}x{canvas_h})")


# =========================================================================
#  Mosaic layout
# =========================================================================

def crop_cost(image_aspect, cell_aspect):
    if image_aspect > cell_aspect:
        used_fraction = cell_aspect / image_aspect
    else:
        used_fraction = image_aspect / cell_aspect
    return 1.0 - used_fraction


def center_crop_and_resize(img_data, target_w, target_h):
    with Image.open(img_data["path"]) as img:
        src_w, src_h = img.size
        target_aspect = target_w / target_h
        src_aspect = src_w / src_h

        if src_aspect > target_aspect:
            new_w = int(src_h * target_aspect)
            left = (src_w - new_w) // 2
            img = img.crop((left, 0, left + new_w, src_h))
        else:
            new_h = int(src_w / target_aspect)
            top = (src_h - new_h) // 2
            img = img.crop((0, top, src_w, top + new_h))

        return img.resize((target_w, target_h), Image.LANCZOS)


def select_best_images(images, count, tile_sizes):
    if count is None or count >= len(images):
        return list(images)

    tile_aspects = [tw / th for tw, th in tile_sizes]
    scored = []
    for img in images:
        best_cost = min(crop_cost(img["aspect"], ta) for ta in tile_aspects)
        jittered = best_cost + random.uniform(0, 0.25)
        scored.append((jittered, img))
    scored.sort(key=lambda x: x[0])
    return [img for _, img in scored[:count]]


def generate_mosaic_grid(images, grid_cols, grid_rows, tile_sizes, gap, ctx):
    total_cells = grid_cols * grid_rows
    sorted_tiles = sorted(tile_sizes, key=lambda t: t[0] * t[1], reverse=True)
    max_tile_area = sorted_tiles[0][0] * sorted_tiles[0][1]

    def fits(grid, gx, gy, tw, th):
        if gx + tw > grid_cols or gy + th > grid_rows:
            return False
        for dy in range(th):
            for dx in range(tw):
                if grid[gy + dy][gx + dx]:
                    return False
        return True

    def mark(grid, gx, gy, tw, th, val=True):
        for dy in range(th):
            for dx in range(tw):
                grid[gy + dy][gx + dx] = val

    def count_filled(grid):
        return sum(1 for gy in range(grid_rows)
                   for gx in range(grid_cols) if grid[gy][gx])

    def score_tile(img, tw, th):
        tile_aspect = tw / th
        cost = crop_cost(img["aspect"], tile_aspect)
        size_bonus = (tw * th) / max_tile_area
        return cost - size_bonus * 0.3

    def best_tile_for(img, grid, gx, gy):
        best = None
        best_score = float("inf")
        for tw, th in sorted_tiles:
            if fits(grid, gx, gy, tw, th):
                s = score_tile(img, tw, th)
                if s < best_score:
                    best_score = s
                    best = (tw, th)
        if best is None and fits(grid, gx, gy, 1, 1):
            best = (1, 1)
        return best

    def all_tiles_for(img, grid, gx, gy):
        candidates = []
        for tw, th in sorted_tiles:
            if fits(grid, gx, gy, tw, th):
                candidates.append((score_tile(img, tw, th), tw, th))
        if not any(tw == 1 and th == 1 for _, tw, th in candidates):
            if fits(grid, gx, gy, 1, 1):
                candidates.append((score_tile(img, 1, 1), 1, 1))
        candidates.sort(key=lambda x: x[0])
        return [(tw, th) for _, tw, th in candidates]

    # Phase 1: greedy placement
    grid = [[False] * grid_cols for _ in range(grid_rows)]
    placements = []
    img_idx = 0
    for gy in range(grid_rows):
        for gx in range(grid_cols):
            if grid[gy][gx] or img_idx >= len(images):
                continue
            img = images[img_idx]
            tile = best_tile_for(img, grid, gx, gy)
            if tile:
                tw, th = tile
                mark(grid, gx, gy, tw, th)
                placements.append((img, gx, gy, tw, th))
                img_idx += 1

    filled = count_filled(grid)
    if filled >= total_cells:
        return placements

    # Phase 2: iterative refinement
    start = time.time()
    time_limit = 5.0
    best_placements = list(placements)
    best_filled = filled

    for iteration in range(200):
        if time.time() - start > time_limit:
            break
        if best_filled >= total_cells:
            break
        ctx.check_cancelled()

        empty_cells = [(gx, gy)
                       for gy in range(grid_rows)
                       for gx in range(grid_cols)
                       if not grid[gy][gx]]
        if not empty_cells:
            break

        ex, ey = random.choice(empty_cells)
        neighbors = set()
        for dx in range(-4, 5):
            for dy in range(-4, 5):
                nx, ny = ex + dx, ey + dy
                if 0 <= nx < grid_cols and 0 <= ny < grid_rows:
                    for i, (img, px, py, tw, th) in enumerate(placements):
                        if px <= nx < px + tw and py <= ny < py + th:
                            neighbors.add(i)

        if not neighbors:
            continue

        to_remove = random.sample(list(neighbors),
                                  min(len(neighbors), random.randint(1, 2)))

        old_placements = list(placements)
        old_grid = [row[:] for row in grid]

        removed = []
        for i in sorted(to_remove, reverse=True):
            img, px, py, tw, th = placements[i]
            mark(grid, px, py, tw, th, False)
            removed.append(placements.pop(i))

        replaced_all = True
        for img, old_px, old_py, old_tw, old_th in removed:
            placed = False
            candidates = all_tiles_for(img, grid, old_px, old_py)
            candidates = [(tw, th) for tw, th in candidates
                          if (tw, th) != (old_tw, old_th)]
            for tw, th in candidates:
                mark(grid, old_px, old_py, tw, th)
                placements.append((img, old_px, old_py, tw, th))
                placed = True
                break
            if not placed:
                for gy2 in range(grid_rows):
                    for gx2 in range(grid_cols):
                        if grid[gy2][gx2]:
                            continue
                        tile = best_tile_for(img, grid, gx2, gy2)
                        if tile:
                            tw, th = tile
                            mark(grid, gx2, gy2, tw, th)
                            placements.append((img, gx2, gy2, tw, th))
                            placed = True
                            break
                    if placed:
                        break
            if not placed:
                replaced_all = False

        new_filled = count_filled(grid)

        if replaced_all and new_filled >= best_filled:
            placed_imgs = {id(p[0]) for p in placements}
            for img in images:
                if id(img) in placed_imgs:
                    continue
                if len(placements) >= len(images):
                    break
                for gy2 in range(grid_rows):
                    for gx2 in range(grid_cols):
                        if grid[gy2][gx2]:
                            continue
                        tile = best_tile_for(img, grid, gx2, gy2)
                        if tile:
                            tw, th = tile
                            mark(grid, gx2, gy2, tw, th)
                            placements.append((img, gx2, gy2, tw, th))
                            placed_imgs.add(id(img))
                            break

            new_filled = count_filled(grid)
            if new_filled > best_filled:
                best_filled = new_filled
                best_placements = list(placements)
                if best_filled >= total_cells:
                    break
            elif new_filled < best_filled:
                placements = old_placements
                grid = old_grid
        else:
            placements = old_placements
            grid = old_grid

    return best_placements


def make_mosaic_collage(images, output_path, ctx, aspect="16:9",
                        count=None, bg_color=DEFAULT_BG):
    aw, ah = map(int, aspect.split(":"))
    aspect_ratio = aw / ah

    if count is not None and count > len(images):
        raise ValueError(f"Requested {count} images but only {len(images)} "
                         f"available in folder.")

    selected = select_best_images(images, count, MOSAIC_TILE_SIZES)
    random.shuffle(selected)

    ctx.log(f"\nMosaic: using {len(selected)} images, aspect {aspect}")

    total_expected_area = 0
    for img in selected:
        best_tile = min(MOSAIC_TILE_SIZES,
                        key=lambda t: crop_cost(img["aspect"], t[0] / t[1]))
        total_expected_area += best_tile[0] * best_tile[1]
    total_cells_needed = total_expected_area * 1.05

    grid_cols = max(4, round(math.sqrt(total_cells_needed * aspect_ratio)))
    grid_rows = max(4, round(grid_cols / aspect_ratio))

    if grid_cols > MOSAIC_GRID_COLS:
        grid_cols = MOSAIC_GRID_COLS
        grid_rows = max(4, round(grid_cols / aspect_ratio))

    cell_px = CANVAS_WIDTH // grid_cols
    if cell_px < MOSAIC_MIN_IMAGE_DIM:
        grid_cols = CANVAS_WIDTH // MOSAIC_MIN_IMAGE_DIM
        grid_rows = max(4, round(grid_cols / aspect_ratio))

    grid_variants = set()
    for dc in range(-2, 3):
        for dr in range(-2, 3):
            gc = max(4, grid_cols + dc)
            gr = max(4, grid_rows + dr)
            grid_ar = gc / gr
            if 0.5 * aspect_ratio <= grid_ar <= 2.0 * aspect_ratio:
                gcp = CANVAS_WIDTH // gc
                if gcp >= MOSAIC_MIN_IMAGE_DIM:
                    grid_variants.add((gc, gr))
    grid_variants.add((grid_cols, grid_rows))

    best_placements = None
    best_score = -float("inf")
    best_grid = (grid_cols, grid_rows)

    for gc, gr in grid_variants:
        ctx.check_cancelled()
        attempts = max(20, 80 // len(grid_variants))
        for attempt in range(attempts):
            shuffled = list(selected)
            random.shuffle(shuffled)
            placements = generate_mosaic_grid(
                shuffled, gc, gr, MOSAIC_TILE_SIZES, GRID_GAP, ctx)

            if not placements:
                continue

            placed_count = len(placements)
            total_crop = sum(
                crop_cost(img["aspect"], tw / th)
                for img, _, _, tw, th in placements
            )
            cells_used = sum(tw * th for _, _, _, tw, th in placements)
            tc = gc * gr
            coverage = cells_used / tc

            score = (placed_count * 10
                     + coverage * 20
                     - total_crop
                     + (100 if coverage >= 0.99 else 0))
            if score > best_score:
                best_score = score
                best_placements = placements
                best_grid = (gc, gr)

    if best_placements is None:
        raise RuntimeError("Could not find a valid mosaic layout!")

    grid_cols, grid_rows = best_grid
    cell_px = CANVAS_WIDTH // grid_cols
    ctx.log(f"Grid: {grid_cols}x{grid_rows}, cell ~{cell_px}px")

    placed_count = len(best_placements)
    cells_used = sum(tw * th for _, _, _, tw, th in best_placements)
    total_cells = grid_cols * grid_rows
    coverage = cells_used / total_cells
    total_crop = sum(
        crop_cost(img["aspect"], tw / th)
        for img, _, _, tw, th in best_placements
    )
    avg_crop = total_crop / placed_count if placed_count else 0

    ctx.log(f"Placed {placed_count}/{len(selected)} images")
    ctx.log(f"Grid coverage: {coverage * 100:.1f}%")
    ctx.log(f"Average crop: {avg_crop * 100:.1f}%")

    canvas_w = CANVAS_WIDTH
    canvas_h = int(canvas_w / aspect_ratio)
    canvas = Image.new("RGB", (canvas_w, canvas_h), bg_color)

    total_gap_x = GRID_GAP * (grid_cols - 1)
    total_gap_y = GRID_GAP * (grid_rows - 1)
    cell_w = (canvas_w - total_gap_x) / grid_cols
    cell_h = (canvas_h - total_gap_y) / grid_rows

    for img_data, gx, gy, tw, th in best_placements:
        px = int(gx * (cell_w + GRID_GAP))
        py = int(gy * (cell_h + GRID_GAP))
        pw = int(tw * cell_w + (tw - 1) * GRID_GAP)
        ph = int(th * cell_h + (th - 1) * GRID_GAP)
        pw = min(pw, canvas_w - px)
        ph = min(ph, canvas_h - py)
        if pw < 1 or ph < 1:
            continue
        cropped = center_crop_and_resize(img_data, pw, ph)
        canvas.paste(cropped, (px, py))
        del cropped

    canvas.save(output_path, "JPEG", quality=92)
    ctx.log(f"Saved mosaic to {output_path} ({canvas_w}x{canvas_h})")


# =========================================================================
#  Creative layout
# =========================================================================

def make_shadow(w, h, rotation):
    pad = SHADOW_BLUR * 3
    shadow_img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow_img)
    draw.rectangle(
        [pad + SHADOW_OFFSET, pad + SHADOW_OFFSET,
         pad + w + SHADOW_OFFSET, pad + h + SHADOW_OFFSET],
        fill=SHADOW_COLOR,
    )
    shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(SHADOW_BLUR))
    if rotation != 0:
        shadow_img = shadow_img.rotate(rotation, expand=True, resample=Image.BICUBIC)
    return shadow_img


def place_image_on_canvas(canvas, img_data, x, y, w, h, rotation):
    resized = open_and_resize(img_data, w, h)
    if rotation != 0:
        border = 6
        bordered = Image.new("RGB", (w + border * 2, h + border * 2), (255, 255, 255))
        bordered.paste(resized, (border, border))
        del resized
        shadow = make_shadow(bordered.width, bordered.height, rotation)
        rotated = bordered.convert("RGBA").rotate(
            rotation, expand=True, resample=Image.BICUBIC)
        del bordered
        sx = x - shadow.width // 2 + w // 2
        sy = y - shadow.height // 2 + h // 2
        canvas.paste(Image.alpha_composite(
            Image.new("RGBA", shadow.size, (0, 0, 0, 0)), shadow
        ), (sx, sy), shadow)
        del shadow
        rx = x - rotated.width // 2 + w // 2
        ry = y - rotated.height // 2 + h // 2
        canvas.paste(rotated, (rx, ry), rotated)
        del rotated
    else:
        pad = SHADOW_BLUR * 3
        shadow = make_shadow(w, h, 0)
        sx = x - pad
        sy = y - pad
        canvas.paste(Image.alpha_composite(
            Image.new("RGBA", shadow.size, (0, 0, 0, 0)), shadow
        ), (sx, sy), shadow)
        del shadow
        canvas.paste(resized, (x, y))
        del resized


def generate_layout(images, canvas_w, canvas_h, margin):
    n = len(images)
    random.shuffle(images)
    usable_w = canvas_w - margin * 2
    usable_h = canvas_h - margin * 2
    placements = []
    _subdivide(placements, images, 0, len(images),
               margin, margin, usable_w, usable_h, depth=0)
    return placements


def _pick_rotation(neighbor_rot):
    if random.random() > 0.55:
        return 0.0
    for _ in range(20):
        rot = random.uniform(-MAX_ROTATION, MAX_ROTATION)
        if neighbor_rot is not None and abs(rot - neighbor_rot) < 3.0:
            continue
        return rot
    if neighbor_rot is not None:
        sign = -1 if neighbor_rot >= 0 else 1
        return sign * random.uniform(2.0, MAX_ROTATION)
    return 0.0


def _subdivide(placements, images, start, count, x, y, w, h, depth,
               neighbor_rot=None):
    if count <= 0 or w < 100 or h < 100:
        return

    if count == 1:
        img = images[start]
        ar = img["aspect"]
        pad = random.randint(4, 10)
        avail_w = w - pad * 2
        avail_h = h - pad * 2
        if avail_w <= 0 or avail_h <= 0:
            return
        if avail_w / avail_h > ar:
            iw = int(avail_h * ar)
            ih = avail_h
        else:
            iw = avail_w
            ih = int(avail_w / ar)

        fill_ratio = (iw * ih) / (avail_w * avail_h)
        if fill_ratio < 0.65:
            max_overflow = 1.30
            scale_w = (avail_w * max_overflow) / iw if iw < avail_w else 1.0
            scale_h = (avail_h * max_overflow) / ih if ih < avail_h else 1.0
            scale = min(scale_w, scale_h, max_overflow)
            iw = int(iw * scale)
            ih = int(ih * scale)

        jitter = min(8, pad // 2)
        ox = (avail_w - iw) // 2 + random.randint(-jitter, jitter)
        oy = (avail_h - ih) // 2 + random.randint(-jitter, jitter)
        px = x + pad + ox
        py = y + pad + oy
        rotation = _pick_rotation(neighbor_rot)
        placements.append((img, px, py, iw, ih, rotation))
        return

    if w / h > 1.4:
        split_dir = "v"
    elif h / w > 1.4:
        split_dir = "h"
    else:
        split_dir = random.choice(["h", "v"])

    if count == 2:
        left_count = 1
    elif depth <= 1 and random.random() < 0.25:
        left_count = 1
    else:
        base = count / 2.0
        jitter = max(1, count // 4)
        left_count = int(base + random.randint(-jitter, jitter))
        left_count = max(1, min(count - 1, left_count))

    right_count = count - left_count
    ratio = left_count / count
    ratio += random.uniform(-0.08, 0.08)
    ratio = max(0.2, min(0.8, ratio))

    if split_dir == "v":
        max_overlap = int(w * OVERLAP_FRACTION)
        gap = random.randint(-max_overlap, 20)
        left_w = int(w * ratio) - gap // 2
        right_w = w - left_w - gap
        if left_w > 50 and right_w > 50:
            before = len(placements)
            _subdivide(placements, images, start, left_count,
                       x, y, left_w, h, depth + 1, neighbor_rot)
            first_rot = _last_rotation(placements, before)
            _subdivide(placements, images, start + left_count, right_count,
                       x + left_w + gap, y, right_w, h, depth + 1, first_rot)
    else:
        max_overlap = int(h * OVERLAP_FRACTION)
        gap = random.randint(-max_overlap, 20)
        top_h = int(h * ratio) - gap // 2
        bot_h = h - top_h - gap
        if top_h > 50 and bot_h > 50:
            before = len(placements)
            _subdivide(placements, images, start, left_count,
                       x, y, w, top_h, depth + 1, neighbor_rot)
            first_rot = _last_rotation(placements, before)
            _subdivide(placements, images, start + left_count, right_count,
                       x, y + top_h + gap, w, bot_h, depth + 1, first_rot)


def _last_rotation(placements, since_idx):
    added = placements[since_idx:]
    if not added:
        return None
    return sum(p[5] for p in added) / len(added)


def make_creative_collage(images, output_path, ctx, bg_color=DEFAULT_BG):
    best_placements = None
    best_coverage = 0

    for attempt in range(80):
        ctx.check_cancelled()
        imgs_copy = list(images)
        placements = generate_layout(
            imgs_copy, CANVAS_WIDTH, CREATIVE_CANVAS_HEIGHT, MARGIN)
        if len(placements) < len(images):
            continue
        img_area = sum(w * h for (_, _, _, w, h, _) in placements)
        canvas_area = CANVAS_WIDTH * CREATIVE_CANVAS_HEIGHT
        coverage = img_area / canvas_area
        if coverage > best_coverage:
            best_coverage = coverage
            best_placements = placements

    if best_placements is None:
        raise RuntimeError("Could not find a valid layout!")

    ctx.log(f"\nBest coverage: {best_coverage * 100:.1f}%")
    ctx.log(f"Placed {len(best_placements)} images")

    best_placements.sort(key=lambda p: -(p[3] * p[4]))

    canvas = Image.new("RGBA", (CANVAS_WIDTH, CREATIVE_CANVAS_HEIGHT),
                        (*bg_color, 255))
    for (img_data, px, py, pw, ph, rot) in best_placements:
        place_image_on_canvas(canvas, img_data, px, py, pw, ph, rot)

    final = Image.new("RGB", canvas.size, bg_color)
    final.paste(canvas, mask=canvas.split()[3])
    del canvas
    final.save(output_path, "JPEG", quality=92)
    ctx.log(f"Saved collage to {output_path} "
            f"({CANVAS_WIDTH}x{CREATIVE_CANVAS_HEIGHT})")


# =========================================================================
#  Entry point
# =========================================================================

def auto_output_filename(folder, style, num_images, num_cols=None,
                         aspect=None, count=None):
    folder_name = os.path.basename(os.path.normpath(folder))
    parts = [folder_name, style, f"{num_images}imgs"]
    if style == "columns" and num_cols is not None:
        parts.append(f"{num_cols}cols")
    if style == "mosaic":
        if aspect:
            parts.append(aspect.replace(":", "x"))
        if count is not None:
            parts.append(f"{count}sel")
    parts.append(datetime.now().strftime("%Y%m%d_%H%M%S"))
    return "_".join(parts) + ".jpg"


def run(*, folder, style="creative", output=None, num_cols=None,
        aspect="16:9", count=None, bg=None, ctx=None):
    """Generate a collage. Pure-Python; logs via ctx.log.

    folder        absolute path to folder containing .jpg images
    style         one of STYLES
    output        absolute output path; auto-generated in OUTPUT_DIR if None
    num_cols      integer (columns style only)
    aspect        "W:H" (mosaic style only)
    count         integer (mosaic style only)
    bg            hex string like "#333333"
    ctx           RunContext
    """
    if ctx is None:
        ctx = RunContext()
    if style not in STYLES:
        raise ValueError(f"Unknown style: {style}. Choose from {STYLES}.")

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    if bg:
        h = bg.lstrip("#")
        bg_color = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    else:
        bg_color = DEFAULT_BG

    images = load_images(folder, ctx=ctx)
    if not images:
        raise RuntimeError("No images found!")

    if output is None:
        ensure_output_dir()
        output = os.path.join(
            OUTPUT_DIR,
            auto_output_filename(folder, style, len(images),
                                 num_cols=num_cols, aspect=aspect, count=count),
        )
    else:
        output = os.path.abspath(output)
        os.makedirs(os.path.dirname(output), exist_ok=True)

    # Exclude output from inputs if it lives in the source folder
    images = load_images(folder, exclude=output, ctx=ctx)

    ctx.log(f"Loaded {len(images)} images:")
    print_image_list(images, ctx)
    ctx.log(f"Style: {style}")
    ctx.log(f"Output: {output}")

    if style == "grid":
        make_grid_collage(images, output, ctx, bg_color=bg_color)
    elif style == "columns":
        make_columns_collage(images, output, ctx,
                             num_cols=num_cols, bg_color=bg_color)
    elif style == "mosaic":
        make_mosaic_collage(images, output, ctx,
                            aspect=aspect, count=count, bg_color=bg_color)
    else:
        make_creative_collage(images, output, ctx, bg_color=bg_color)

    return output
