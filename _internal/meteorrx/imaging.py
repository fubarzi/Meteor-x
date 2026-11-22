"""Product generation: channel images, composites, PNG output."""
from __future__ import annotations

import os
import struct
import zlib

import numpy as np

from . import geometry, lrpt, outputs


def write_png(path: str, img: np.ndarray) -> None:
    """Minimal PNG writer (8-bit grey or RGB), no external dependency."""
    img = np.ascontiguousarray(img, dtype=np.uint8)
    if img.ndim == 2:
        h, w = img.shape
        ctype, rowbytes = 0, w
    else:
        h, w, _ = img.shape
        ctype, rowbytes = 2, w * 3
    raw = np.empty((h, rowbytes + 1), dtype=np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = img.reshape(h, rowbytes)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b"")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(png)
    os.replace(tmp, path)


def read_png(path: str) -> np.ndarray:
    """Minimal PNG reader (8-bit grey or RGB, non-interlaced), the counterpart of write_png.

    Returns (H, W) or (H, W, 3) uint8. No external dependency: MeteorRX ships no image library."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos, idat, hdr = 8, [], None
    while pos + 8 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body[:13])
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break
        pos += 12 + ln
    if hdr is None or not idat:
        raise ValueError("PNG without IHDR or IDAT")
    w, h, depth, ctype, comp, filt, interlace = hdr
    if depth != 8 or ctype not in (0, 2) or interlace or comp or filt:
        raise ValueError(f"unsupported PNG (depth {depth}, colour type {ctype}, interlace {interlace})")
    nch = 1 if ctype == 0 else 3
    raw = zlib.decompress(b"".join(idat))
    stride = w * nch
    if len(raw) < h * (stride + 1):
        raise ValueError("truncated PNG data")
    out = np.zeros((h, stride), np.uint8)
    prev = np.zeros(stride, np.int16)
    for y in range(h):
        off = y * (stride + 1)
        ft = raw[off]
        line = np.frombuffer(raw, np.uint8, stride, off + 1).astype(np.int16)
        if ft == 0:
            cur = line
        elif ft == 2:                                   # Up
            cur = (line + prev) & 0xFF
        else:                                           # Sub / Average / Paeth: sequential
            cur = line.copy()
            for x in range(stride):
                a_ = int(cur[x - nch]) if x >= nch else 0
                b_ = int(prev[x])
                c_ = int(prev[x - nch]) if x >= nch else 0
                if ft == 1:
                    cur[x] = (cur[x] + a_) & 0xFF
                elif ft == 3:
                    cur[x] = (cur[x] + ((a_ + b_) >> 1)) & 0xFF
                elif ft == 4:
                    pp = a_ + b_ - c_
                    pa, pb, pc = abs(pp - a_), abs(pp - b_), abs(pp - c_)
                    cur[x] = (cur[x] + (a_ if pa <= pb and pa <= pc else (b_ if pb <= pc else c_))) & 0xFF
                else:
                    raise ValueError(f"unknown PNG filter {ft}")
        out[y] = cur.astype(np.uint8)
        prev = cur
    return out.reshape(h, w) if nch == 1 else out.reshape(h, w, 3)


def stretch(img: np.ndarray, lo_pct: float = 0.5, hi_pct: float = 99.5) -> np.ndarray:
    """Percentile contrast stretch ignoring black (missing) pixels."""
    v = img[img > 0]
    if v.size < 100:
        return img
    lo, hi = np.percentile(v, [lo_pct, hi_pct])
    if hi <= lo:
        return img
    out = (img.astype(np.float32) - lo) * (255.0 / (hi - lo))
    out = np.clip(out, 0, 255).astype(np.uint8)
    out[img == 0] = 0
    return out


def channel_images(imager: lrpt.MsuMrImager, fill: bool,
                   texture_fill: bool | None = None) -> dict[int, np.ndarray]:
    raw = {}
    for ch in imager.channels_present:
        img = imager.get_channel(ch)
        if img.size == 0:
            continue
        raw[ch + 1] = img
    if texture_fill is None:
        texture_fill = fill
    if not fill and not texture_fill:
        return raw
    filled = ({ch: lrpt.fill_missing(img, reference=raw.get(2 if ch == 1 else 1)
                                         if ch in (1, 2) else None)
               for ch, img in raw.items()} if fill else
              {ch: img.copy() for ch, img in raw.items()})
    return _fill_shared_bands(raw, filled) if texture_fill else filled


def _fill_shared_bands(raw: dict[int, np.ndarray], filled: dict[int, np.ndarray]) -> dict[int, np.ndarray]:
    """Estimate broad lost-frame bands shared by all transmitted image channels.

    Small per-channel gaps are handled by ``lrpt.fill_missing``. When several
    channels vanish together, their surviving fragments make that interpolation
    look streaked. Mirror intact neighbouring texture into the band from both
    edges and blend it through the middle. This is a display estimate, not
    recovered data.
    """
    if not raw or len({img.shape for img in raw.values()}) != 1:
        return filled
    h, w = next(iter(raw.values())).shape
    groups = h // 8
    if groups < 8:
        return filled
    coverage = {ch: (img[:groups * 8] > 0).reshape(groups, 8, w).mean(axis=(1, 2))
                for ch, img in raw.items()}
    active = [ch for ch, value in coverage.items() if value.mean() > 0.2]
    if not active:
        return filled
    quality = np.stack([coverage[ch] for ch in active])
    damaged = quality.mean(axis=0) < 0.85
    severe = quality.max(axis=0) < 0.25
    # Join short islands of fragmentary reception inside one lost-frame band.
    good_edges = np.flatnonzero(np.diff(np.r_[False, ~damaged, False].astype(np.int8)))
    for a, b in zip(good_edges[::2], good_edges[1::2]):
        if a > 0 and b < groups and b - a <= 3:
            damaged[a:b] = True
    edges = np.flatnonzero(np.diff(np.r_[False, damaged, False].astype(np.int8)))
    for a, b in zip(edges[::2], edges[1::2]):
        start, end = int(a * 8), int(b * 8)
        if (end - start > 320 or end - start < 32 or severe[a:b].sum() < 4
                or start < 24 or end + 24 > h):
            continue
        n = end - start
        if start < n or h - end < n:
            continue  # Need a complete intact source strip on both sides.
        t = ((np.arange(n, dtype=np.float32) + 1) / (n + 1))[:, None]
        blend = t * t * (3 - 2 * t)
        for ch in active:
            img = filled[ch]
            # Reflected source rows preserve clouds, sea texture and coastlines
            # at full resolution; the zero-slope blend hides the two joins.
            top = img[start - n:start][::-1].astype(np.float32)
            bottom = img[end:end + n][::-1].astype(np.float32)
            values = top * (1 - blend) + bottom * blend
            img[start:end] = np.rint(np.clip(values, 1, 255)).astype(np.uint8)
    return filled


def composite(chs: dict[int, np.ndarray], spec: str) -> np.ndarray | None:
    spec = spec.strip()
    if len(spec) != 3 or not spec.isdigit():
        return None
    idx = [int(c) for c in spec]
    if not all(i in chs for i in idx):
        return None
    h = min(chs[i].shape[0] for i in idx)
    if h == 0:
        return None
    out = np.stack([chs[i][:h] for i in idx], axis=2)
    # A lost frame does not cost every channel the same pixels: the channels' segments are interleaved,
    # so a gap often leaves one channel black while the others still have data. Combined naively that
    # became a band of saturated blue or yellow across the picture, which reads as corruption rather
    # than as absence - 1.2 % of a real night pass. Missing anywhere means missing here.
    out[(out == 0).any(axis=2)] = 0
    return out


def preview(imager: lrpt.MsuMrImager, prefer: str = "221") -> tuple[str, np.ndarray] | None:
    chs = channel_images(imager, fill=False)
    if not chs:
        return None
    c = composite(chs, prefer)
    if c is not None:
        return f"RGB {prefer}", c
    k = sorted(chs)[0]
    return f"Ch {k}", chs[k]


def save_products(imager: lrpt.MsuMrImager, folder: str, prefix: str, composites: list[str], fill: bool,
                  rotate: bool, ir_inverted: bool, correct: bool = True,
                  texture_fill: bool | None = None) -> list[str]:
    """Writes the decoded products. With `correct`, a panoramic-corrected copy (`..._corr.png`, 1 km/pixel
    ground spacing, see meteorrx/geometry.py) is written beside each one; the originals are never altered."""
    os.makedirs(folder, exist_ok=True)
    chs = channel_images(imager, fill, texture_fill)
    files = []

    def put(name, img):
        if rotate:
            img = img[::-1, ::-1]
        p = outputs.unique_path(os.path.join(folder, f"{prefix}_{name}.png"))   # never overwrite
        write_png(p, img)
        files.append(p)
        if correct:
            try:
                c = geometry.correct(img)
            except Exception:  # noqa: BLE001  (a correction must never cost us the decoded image)
                return
            pc = outputs.unique_path(os.path.join(folder, f"{prefix}_{name}_corr.png"))
            write_png(pc, c)
            files.append(pc)

    for k, img in sorted(chs.items()):
        put(f"ch{k}", img)
        if ir_inverted and k >= 4:
            inv = 255 - img
            inv[img == 0] = 0
            put(f"ch{k}_inv", inv)
    for spec in composites:
        c = composite(chs, spec)
        if c is not None:
            put(f"rgb{spec}", c)
    return files
