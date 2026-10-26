"""Render sliced toolpaths to PNG so the agent can look at the result.

The file is streamed twice (bounds, then drawing) so memory stays flat no
matter how large the G-code is.
"""
from __future__ import annotations

import io
import math
import re
from pathlib import Path
from typing import Iterator

from PIL import Image, ImageDraw

MOVE = re.compile(r"^G([0-3])(?!\d)", re.IGNORECASE)
PARAM = re.compile(r"([XYZEFIJR])\s*(-?\d*\.?\d+)", re.IGNORECASE)
TYPE_COLORS = {
    "Outer wall": (255, 140, 0), "Inner wall": (255, 210, 80), "Sparse infill": (170, 60, 60),
    "Internal solid infill": (200, 80, 200), "Top surface": (230, 40, 40), "Bottom surface": (80, 120, 230),
    "Bridge": (60, 160, 230), "Support": (60, 200, 120), "Support interface": (30, 140, 80),
    "Skirt": (0, 180, 180), "Brim": (0, 180, 180), "Overhang wall": (60, 90, 240),
    "Gap infill": (255, 255, 255), "Prime tower": (150, 150, 150),
}
MAX_BYTES = 1024 * 1024 * 1024
Segment = tuple[int, float, float, float, float, float, str]  # layer, z, x0, y0, x1, y1, feature


def _arc_points(x0, y0, x1, y1, i, j, clockwise) -> list[tuple[float, float]]:
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
    sweep = a1 - a0
    if clockwise and sweep >= 0:
        sweep -= 2 * math.pi
    elif not clockwise and sweep <= 0:
        sweep += 2 * math.pi
    steps = max(2, min(64, int(abs(sweep) * max(r, 1) / 0.5)))
    pts = [(cx + r * math.cos(a0 + sweep * k / steps), cy + r * math.sin(a0 + sweep * k / steps))
           for k in range(1, steps)]
    return pts + [(x1, y1)]


def segments(path: Path) -> Iterator[Segment]:
    """Extruding XY moves, numbered by non-empty layer (1-based)."""
    x = y = z = e = 0.0
    feature = ""
    relative_e = True
    relative_xy = False
    layer_open = False  # saw a layer change since the last emitted layer
    layer = 0
    started = False
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(";"):
                if line.startswith((";TYPE:", "; FEATURE:")):
                    feature = line.split(":", 1)[1].strip()
                elif line.startswith((";LAYER_CHANGE", "; CHANGE_LAYER")):
                    started, layer_open = True, True
                continue
            code = line.split(";", 1)[0].strip().upper()
            if not code:
                continue
            if code.startswith("M82"):
                relative_e = False
            elif code.startswith("M83"):
                relative_e = True
            elif code.startswith("G90") and not code.startswith("G90."):
                relative_xy = False
            elif code.startswith("G91") and not code.startswith("G91."):
                relative_xy = True
            elif code.startswith("G92"):
                e = next((float(v) for k, v in PARAM.findall(code) if k == "E"), e)
            m = MOVE.match(code)
            if not m:
                continue
            params = {k: float(v) for k, v in PARAM.findall(code[m.end():])}
            nx = x + params.get("X", 0) if relative_xy else params.get("X", x)
            ny = y + params.get("Y", 0) if relative_xy else params.get("Y", y)
            if "Z" in params:
                z = z + params["Z"] if relative_xy else params["Z"]
            extruding = False
            if "E" in params:
                extruding = params["E"] > 0 if relative_e else params["E"] > e
                e = e + params["E"] if relative_e else params["E"]
            # "Custom" is start/purge G-code, not part of the model.
            if extruding and started and feature != "Custom" and (nx, ny) != (x, y):
                if layer_open:
                    layer, layer_open = layer + 1, False
                pts = [(nx, ny)]
                if m.group(1) in "23" and ("I" in params or "J" in params):
                    pts = _arc_points(x, y, nx, ny, params.get("I", 0), params.get("J", 0), m.group(1) == "2")
                px, py = x, y
                for qx, qy in pts:
                    yield layer, z, px, py, qx, qy, feature
                    px, py = qx, qy
            x, y = nx, ny


def render(path: str, layer: int | None = None, size: int = 900) -> tuple[bytes, dict]:
    """Top view. layer=None draws every layer shaded by height; otherwise one layer by feature."""
    src = Path(path)
    if src.stat().st_size > MAX_BYTES:
        raise ValueError("G-code is larger than 1 GB; too big to preview")
    layers = 0
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    layer_z = None
    feats: dict[str, int] = {}
    for n, z, x0, y0, x1, y1, feat in segments(src):
        layers = n
        if layer is None or n == layer:
            min_x, max_x = min(min_x, x0, x1), max(max_x, x0, x1)
            min_y, max_y = min(min_y, y0, y1), max(max_y, y0, y1)
        if n == layer:
            layer_z = z
            feats[feat] = feats.get(feat, 0) + 1
    if not layers:
        raise ValueError("No extrusion moves found")
    if layer is not None and not 1 <= layer <= layers:
        raise ValueError(f"layer must be between 1 and {layers}")

    span = max(max_x - min_x, max_y - min_y, 1.0)
    pad = 30
    scale = (size - 2 * pad) / span

    def tx(px: float, py: float) -> tuple[float, float]:
        return pad + (px - min_x) * scale, size - pad - (py - min_y) * scale

    img = Image.new("RGB", (size, size), (24, 24, 28))
    draw = ImageDraw.Draw(img)
    width = max(1, int(0.42 * scale))
    top = max(layers - 1, 1)
    for n, _, x0, y0, x1, y1, feat in segments(src):
        if layer is None:
            shade = int(60 + 195 * (n - 1) / top)
            color = (shade, int(shade * 0.75), 40)
        elif n == layer:
            color = TYPE_COLORS.get(feat, (200, 200, 200))
        else:
            continue
        draw.line([tx(x0, y0), tx(x1, y1)], fill=color, width=width)
    label = f"all {layers} layers (brighter = higher)" if layer is None else \
        f"layer {layer}/{layers} z={layer_z:.2f}mm"
    draw.text((10, 8), f"{src.name}  |  {label}  |  {max_x - min_x:.1f} x {max_y - min_y:.1f} mm",
              fill=(230, 230, 230))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    meta = {"layers": layers, "footprint_mm": [round(max_x - min_x, 2), round(max_y - min_y, 2)],
            "bed_bbox": [round(min_x, 2), round(min_y, 2), round(max_x, 2), round(max_y, 2)]}
    if layer is not None:
        meta.update(layer=layer, z_mm=round(layer_z, 3), features_in_layer=feats)
    return buf.getvalue(), meta
