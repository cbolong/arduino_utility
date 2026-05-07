#!/usr/bin/env python3
"""Generate assets/icon.ico — multi-resolution stylised DIP-package icon.

Stdlib-only (zlib + struct, no PIL). Run from the repo root or anywhere:

    python tools/build_icon.py

The output is committed alongside the script. Re-run after tweaking design
constants below to regenerate.

Design: top-down view of a black DIP IC package with two rows of silver
pins extending out the long edges, plus a small notch on the left edge
marking pin 1. Sizes: 16, 32, 48, 64, 128, 256 — multi-resolution ICO with
PNG payloads (Vista+).
"""
import os
import struct
import sys
import zlib


# Color palette (RGBA tuples).
BODY = (0x18, 0x18, 0x1c, 0xff)
BODY_HL = (0x44, 0x44, 0x4c, 0xff)
PIN = (0xd2, 0xd2, 0xd8, 0xff)
PIN_SHADOW = (0x70, 0x70, 0x76, 0xff)
TRANSPARENT = (0, 0, 0, 0)


def _alloc(W, H):
    return bytearray(W * H * 4)


def _put(buf, W, H, x, y, color):
    if 0 <= x < W and 0 <= y < H:
        i = (y * W + x) * 4
        buf[i:i + 4] = bytes(color)


def _fill_rect(buf, W, H, x0, y0, x1, y1, color):
    """x0/y0 inclusive, x1/y1 exclusive."""
    for y in range(max(0, y0), min(H, y1)):
        for x in range(max(0, x0), min(W, x1)):
            i = (y * W + x) * 4
            buf[i:i + 4] = bytes(color)


def _fill_rounded_rect(buf, W, H, x0, y0, x1, y1, r, color):
    for y in range(max(0, y0), min(H, y1)):
        for x in range(max(0, x0), min(W, x1)):
            dx = min(x - x0, x1 - 1 - x)
            dy = min(y - y0, y1 - 1 - y)
            if dx < r and dy < r:
                if (r - dx) ** 2 + (r - dy) ** 2 > r * r:
                    continue
            i = (y * W + x) * 4
            buf[i:i + 4] = bytes(color)


def render_at_size(size: int) -> bytes:
    """Render icon at size×size, returns size*size*4 RGBA bytes.

    Sizes ≥32 are rendered at 2× supersample then box-filtered down for a
    clean edge. Sub-32 sizes are drawn directly (supersampling at 16×16
    just blurs the recognisable shape into mush)."""
    SS = 2 if size >= 32 else 1
    W = H = size * SS
    buf = _alloc(W, H)

    body_w = max(8, round(W * 0.74))
    body_h = max(4, round(H * 0.44))
    body_x = (W - body_w) // 2
    body_y = (H - body_h) // 2

    if size < 24:
        n_pins = 4
    elif size < 64:
        n_pins = 6
    else:
        n_pins = 8

    pin_pitch = body_w / n_pins
    pin_w = max(1, round(pin_pitch * 0.55))
    pin_h = max(2, round(H * 0.10))

    # Top row of pins (extending UP from body).
    top_y0 = body_y - pin_h + 1
    # Bottom row (extending DOWN from body).
    bot_y0 = body_y + body_h - 1

    for k in range(n_pins):
        cx = body_x + round((k + 0.5) * pin_pitch)
        x0 = cx - pin_w // 2
        x1 = x0 + pin_w
        # Top
        _fill_rect(buf, W, H, x0, top_y0, x1, top_y0 + pin_h, PIN)
        # Bottom
        _fill_rect(buf, W, H, x0, bot_y0, x1, bot_y0 + pin_h, PIN)
        # Subtle shadow on the body-attached edge for some depth.
        if size >= 32:
            _fill_rect(buf, W, H, x0, top_y0 + pin_h - 1, x1, top_y0 + pin_h, PIN_SHADOW)
            _fill_rect(buf, W, H, x0, bot_y0, x1, bot_y0 + 1, PIN_SHADOW)

    # Body (rounded black rectangle) — drawn after pins so it covers the
    # 1-pixel pin overlap that anchored each pin to the package edge.
    corner_r = max(1, round(min(body_w, body_h) * 0.14))
    _fill_rounded_rect(
        buf, W, H,
        body_x, body_y, body_x + body_w, body_y + body_h,
        corner_r, BODY,
    )

    # Faint horizontal highlight across the body top — adds a hint of
    # plastic sheen at sizes where it's actually visible.
    if size >= 32:
        hl_h = max(1, H // 80)
        hl_y = body_y + max(1, corner_r // 2)
        _fill_rect(
            buf, W, H,
            body_x + corner_r, hl_y,
            body_x + body_w - corner_r, hl_y + hl_h,
            BODY_HL,
        )

    # Pin-1 notch — half-circle indent on the LEFT edge of the body.
    if size >= 24:
        notch_r = max(2, round(min(body_w, body_h) * 0.15))
        ncx = body_x
        ncy = body_y + body_h // 2
        for y in range(max(0, ncy - notch_r), min(H, ncy + notch_r + 1)):
            for x in range(max(0, ncx), min(W, ncx + notch_r + 1)):
                d2 = (x - ncx) ** 2 + (y - ncy) ** 2
                if d2 <= notch_r * notch_r:
                    i = (y * W + x) * 4
                    buf[i:i + 4] = bytes(TRANSPARENT)

    if SS == 1:
        return bytes(buf)

    # Box-filter downsample: each output pixel = average of SS×SS source.
    # Premultiplied-alpha averaging so transparent pixels don't muddy edges.
    out = _alloc(size, size)
    count = SS * SS
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(SS):
                for dx in range(SS):
                    sx = x * SS + dx
                    sy = y * SS + dy
                    i = (sy * W + sx) * 4
                    sa = buf[i + 3]
                    r += buf[i] * sa
                    g += buf[i + 1] * sa
                    b += buf[i + 2] * sa
                    a += sa
            oi = (y * size + x) * 4
            if a > 0:
                out[oi] = r // a
                out[oi + 1] = g // a
                out[oi + 2] = b // a
                out[oi + 3] = a // count
            # else: leave transparent.
    return bytes(out)


def make_png(W: int, H: int, rgba: bytes) -> bytes:
    """Build a minimal PNG file from raw RGBA bytes."""
    def chunk(typ: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(typ + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))

    raw = bytearray()
    row_bytes = W * 4
    for y in range(H):
        raw.append(0)  # filter byte: 0 = None
        raw += rgba[y * row_bytes:(y + 1) * row_bytes]

    idat = chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def make_ico(images) -> bytes:
    """images: list of (W, H, png_bytes). Produces a multi-res ICO file."""
    n = len(images)
    out = bytearray(struct.pack("<HHH", 0, 1, n))
    header_size = 6 + 16 * n
    offset = header_size

    for W, H, png in images:
        wb = 0 if W == 256 else W
        hb = 0 if H == 256 else H
        out += struct.pack(
            "<BBBBHHII",
            wb, hb,
            0,    # color count (0 = >=256)
            0,    # reserved
            1,    # planes
            32,   # bpp
            len(png),
            offset,
        )
        offset += len(png)

    for _, _, png in images:
        out += png

    return bytes(out)


def main():
    sizes = [16, 32, 48, 64, 128, 256]
    images = []
    for s in sizes:
        print(f"  rendering {s}x{s}...", file=sys.stderr)
        rgba = render_at_size(s)
        png = make_png(s, s, rgba)
        images.append((s, s, png))

    ico = make_ico(images)
    out_path = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "assets", "icon.ico",
    ))
    with open(out_path, "wb") as f:
        f.write(ico)
    print(f"wrote {out_path}: {len(ico)} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
