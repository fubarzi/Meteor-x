"""Additional live MSU-MR display recipes.

The land/sea mask is a 0.05-degree raster made from Natural Earth's 1:10m land
polygons. MCIR and MSA need a timed, geolocated pass to colour the surface.
These are display-only products; decoded channel files are not changed.
"""
from __future__ import annotations

import gzip
import os

import numpy as np


PRECIPITATION = "Precipitation Potential (Cloud Proxy)"
PRODUCTS = (
    "AVHRR 221 False Color",
    "AVHRR 3a21 False Color",
    "MSU-MR 124 False Color",
    "AVHRR 124 False Color",
    "Day Microphysics",
    "10.8 µm Thermal IR (Uncalibrated)",
    "3.9 µm Shortwave IR (Uncalibrated)",
    "AVHRR 543b IR False Color",
    "AVHRR 3b45 IR False Color",
    PRECIPITATION,
    "MCIR",
    "MSA",
)
REQUIRED = {
    "AVHRR 221 False Color": (1, 2),
    "AVHRR 3a21 False Color": (1, 2, 3),
    "AVHRR 124 False Color": (1, 2, 5),
    "Day Microphysics": (1, 4, 5),
    "10.8 µm Thermal IR (Uncalibrated)": (5,),
    "3.9 µm Shortwave IR (Uncalibrated)": (4,),
    "AVHRR 543b IR False Color": (4, 5, 6),
    "AVHRR 3b45 IR False Color": (4, 5, 6),
    PRECIPITATION: (),
    "MCIR": (4,),
    "MSA": (1,),
    "MSU-MR 124 False Color": (1, 2, 4),
}
LAND_MASK = os.path.join(os.path.dirname(__file__), "data", "landmask.bin.gz")
_land_mask = None
_PRECIP_STOPS = np.array([0, 145, 175, 205, 232, 255], dtype=np.float32)
_PRECIP_COLORS = np.array([
    (20, 20, 20), (145, 145, 145), (60, 175, 80),
    (245, 220, 65), (230, 75, 40), (65, 10, 20),
], dtype=np.float32)
_PRECIP_LUT = np.stack([
    np.interp(np.arange(256), _PRECIP_STOPS, _PRECIP_COLORS[:, i]) for i in range(3)
], axis=1).astype(np.uint8)


def land_mask() -> np.ndarray:
    global _land_mask
    if _land_mask is None:
        with gzip.open(LAND_MASK, "rb") as f:
            raw = f.read()
        if len(raw) != 7200 * 3600:
            raise ValueError("invalid land mask")
        _land_mask = np.frombuffer(raw, dtype=np.uint8).reshape(3600, 7200)
    return _land_mask


def _stretch(img: np.ndarray, valid: np.ndarray) -> np.ndarray:
    out = np.zeros(img.shape, np.uint8)
    if valid.sum() < 100:
        out[valid] = img[valid]
        return out
    lo, hi = np.percentile(img[valid], (0.5, 99.5))
    if hi <= lo:
        out[valid] = img[valid]
    else:
        out[valid] = np.clip((img[valid].astype(np.float32) - lo) * (255.0 / (hi - lo)),
                             0, 255).astype(np.uint8)
    return out


def missing_channels(name: str, chs: dict[int, np.ndarray]) -> list[int]:
    if name == PRECIPITATION:
        return [] if 4 in chs or 5 in chs else [4, 5]
    return [ch for ch in REQUIRED[name] if ch not in chs]


def _stack(chs: dict[int, np.ndarray], spec: tuple[int, int, int], invert_last=False,
           balance=False, invert_all=False) -> np.ndarray:
    h = min(chs[i].shape[0] for i in set(spec))
    layers = [chs[i][:h] for i in spec]
    valid = np.logical_and.reduce([layer > 0 for layer in layers])
    if invert_all:
        layers = [255 - layer for layer in layers]
    elif invert_last:
        layers[-1] = 255 - layers[-1]
    if balance:
        layers = [_stretch(layer, valid) for layer in layers]
    out = np.stack(layers, axis=2).astype(np.uint8)
    out[~valid] = 0
    return out


def _inverted_grey(source: np.ndarray) -> np.ndarray:
    valid = source > 0
    out = _stretch(255 - source, valid)
    out[~valid] = 0
    return out


def _precipitation_proxy(chs: dict[int, np.ndarray]) -> np.ndarray:
    """Colour the coldest-looking cloud structure; this does not estimate rainfall rate."""
    source = chs[5] if 5 in chs else chs[4]
    use_visible = 1 in chs and 2 in chs
    if use_visible:
        h = min(source.shape[0], chs[1].shape[0], chs[2].shape[0])
        w = min(source.shape[1], chs[1].shape[1], chs[2].shape[1])
        source = source[:h, :w]
    valid = source > 0
    if use_visible:
        visible = np.minimum(chs[1][:h, :w], chs[2][:h, :w])
        valid &= visible > 0
    cold = _inverted_grey(source)
    cloud = (_stretch(visible, valid) >= 150) if use_visible else (cold >= 180)
    strength = np.clip((cold.astype(np.float32) - 145.0) / 75.0, 0.0, 1.0) * cloud
    base = (cold.astype(np.float32) * 0.75)[..., None]
    color = _PRECIP_LUT[cold].astype(np.float32)
    out = np.clip(base * (1.0 - strength[..., None]) + color * strength[..., None], 0, 255).astype(np.uint8)
    out[~valid] = 0
    return out


def _underlay_chunk(source: np.ndarray, lat: np.ndarray, lon: np.ndarray, infrared: bool) -> np.ndarray:
    """Blend bright cloud pixels over a geographic land/sea colour underlay."""
    mask = land_mask()
    geo_ok = np.isfinite(lat) & np.isfinite(lon)
    mh, mw = mask.shape
    yf = np.clip((90.0 - np.nan_to_num(lat)) * (mh / 180.0) - 0.5, 0, mh - 1)
    xf = np.mod((np.nan_to_num(lon) + 180.0) * (mw / 360.0) - 0.5, mw)
    y0 = np.floor(yf).astype(np.int32)
    x0 = np.floor(xf).astype(np.int32)
    y1, x1 = np.minimum(y0 + 1, mh - 1), (x0 + 1) % mw
    dy, dx = (yf - y0).astype(np.float32), (xf - x0).astype(np.float32)
    land = ((mask[y0, x0] * (1 - dx) + mask[y0, x1] * dx) * (1 - dy) +
            (mask[y1, x0] * (1 - dx) + mask[y1, x1] * dx) * dy) / 255.0
    sea_rgb = np.array((27, 77, 126), dtype=np.float32)
    land_rgb = np.array((73, 126, 72), dtype=np.float32)
    base = sea_rgb + land[..., None] * (land_rgb - sea_rgb)
    value = 255 - source if infrared else source
    cloud = np.clip((value.astype(np.float32) - 80.0) / 175.0, 0, 1) ** 1.3
    out = (base + (255.0 - base) * cloud[..., None]).astype(np.uint8)
    out[(source == 0) | ~geo_ok] = 0
    return out


def render(chs: dict[int, np.ndarray], name: str, track=None, scan_deg: float | None = None) -> np.ndarray | None:
    """Render a named product; return None when its channels or geometry are unavailable."""
    if name not in REQUIRED or missing_channels(name, chs):
        return None
    if name == "AVHRR 221 False Color":
        return _stack(chs, (2, 2, 1), balance=True)
    if name == "AVHRR 3a21 False Color":
        return _stack(chs, (3, 2, 1), balance=True)
    if name == "MSU-MR 124 False Color":
        return _stack(chs, (1, 2, 4), invert_last=True, balance=True)
    if name == "AVHRR 124 False Color":
        return _stack(chs, (1, 2, 5), invert_last=True, balance=True)
    if name == "Day Microphysics":
        gamma = np.clip((chs[4].astype(np.float32) / 255.0) ** 2.5 * 255.0, 0, 255).astype(np.uint8)
        return _stack({**chs, 4: gamma}, (1, 4, 5), balance=True)
    if name == "10.8 µm Thermal IR (Uncalibrated)":
        return _inverted_grey(chs[5])
    if name == "3.9 µm Shortwave IR (Uncalibrated)":
        return _inverted_grey(chs[4])
    if name == "AVHRR 543b IR False Color":
        return _stack(chs, (6, 5, 4), invert_all=True, balance=True)
    if name == "AVHRR 3b45 IR False Color":
        return _stack(chs, (4, 5, 6), invert_all=True, balance=True)
    if name == PRECIPITATION:
        return _precipitation_proxy(chs)
    if track is None or scan_deg is None:
        return None
    from . import geo
    channel = 4 if name == "MCIR" else 1
    source = chs[channel]
    h, w = source.shape
    nrows = min(h, len(track))
    out = np.zeros((h, w, 3), np.uint8)
    angles = geo.scan_angles(w, scan_deg)
    for start in range(0, nrows, 32):
        rows = np.arange(start, min(start + 32, nrows))
        lat, lon = geo.look_latlon(track, angles, rows)
        out[rows] = _underlay_chunk(source[rows], lat, lon, name == "MCIR")
    return out
