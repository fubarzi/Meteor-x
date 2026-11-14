"""Additional MSU-MR views built from decoded channels.

MCIR and MSA place received cloud detail over a geolocated Earth image. The
precipitation colours are a visual cloud-top estimate, not measured rainfall.
Decoded channel files are never changed.
"""
from __future__ import annotations

import os

import numpy as np


PRODUCTS = ("AVHRR 221 False Color", "Infrared (Ch 4)", "MCIR", "MSA",
            "MSA-Precipitation", "MSU-MR 124 False Color")
REQUIRED = {
    "AVHRR 221 False Color": (1, 2),
    "Infrared (Ch 4)": (4,),
    "MCIR": (4,),
    "MSA": (1,),
    "MSA-Precipitation": (1, 4),
    "MSU-MR 124 False Color": (1, 2, 4),
}
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
EARTH_MAP = os.path.join(DATA_DIR, "earth_underlay.jpg")
PRECIP_LUT = os.path.join(DATA_DIR, "precipitation_lut.png")
_earth_map = None
_precip_lut = None


def _read_rgb(path: str, scaled_size: tuple[int, int] | None = None) -> np.ndarray:
    """Read a bundled image with Qt, already shipped for the desktop UI."""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage, QImageReader

    reader = QImageReader(path)
    if scaled_size is not None:
        reader.setScaledSize(QSize(*scaled_size))
    image = reader.read()
    if image.isNull():
        raise ValueError(f"cannot read {path}: {reader.errorString()}")
    image = image.convertToFormat(QImage.Format_RGB888)
    pixels = np.frombuffer(image.bits(), dtype=np.uint8)
    return pixels.reshape(image.height(), image.bytesPerLine())[:, :image.width() * 3].reshape(
        image.height(), image.width(), 3).copy()


def earth_map() -> np.ndarray:
    global _earth_map
    if _earth_map is None:
        # Half source resolution is enough for a 1568-pixel swath and saves RAM.
        _earth_map = _read_rgb(EARTH_MAP, (6000, 3000))
    return _earth_map


def precipitation_lut() -> np.ndarray:
    global _precip_lut
    if _precip_lut is None:
        # WXtoImg NO colours bundled by SatDump. Every column is the same.
        _precip_lut = _read_rgb(PRECIP_LUT)[:, 128, :]
    return _precip_lut


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


def _stack(chs: dict[int, np.ndarray], spec: tuple[int, int, int], invert_last=False,
           balance=False) -> np.ndarray:
    h = min(chs[i].shape[0] for i in set(spec))
    layers = [chs[i][:h] for i in spec]
    valid = np.logical_and.reduce([layer > 0 for layer in layers])
    if invert_last:
        layers[-1] = 255 - layers[-1]
    if balance:
        layers = [_stretch(layer, valid) for layer in layers]
    out = np.stack(layers, axis=2).astype(np.uint8)
    out[~valid] = 0
    return out


def _cloud_scale(source: np.ndarray, infrared: bool) -> tuple[float, float]:
    valid = source[source > 0]
    if valid.size < 100:
        return (0.0, 255.0)
    return tuple(float(x) for x in np.percentile(valid, (5, 80) if infrared else (30, 98)))


def _cloud_strength(source: np.ndarray, bounds: tuple[float, float], infrared: bool) -> np.ndarray:
    lo, hi = bounds
    if hi <= lo + 5:
        return np.zeros(source.shape, np.float32)
    value = (hi - source.astype(np.float32)) if infrared else (source.astype(np.float32) - lo)
    return np.clip(value / (hi - lo), 0, 1)


def _underlay_chunk(source: np.ndarray, lat: np.ndarray, lon: np.ndarray, infrared: bool,
                    bounds: tuple[float, float], rain: np.ndarray | None = None,
                    rain_bounds: tuple[float, float] | None = None) -> np.ndarray:
    """Blend cloud detail and optional precipitation colours over the Earth map."""
    background = earth_map()
    geo_ok = np.isfinite(lat) & np.isfinite(lon)
    h, w = background.shape[:2]
    yi = np.clip(((90.0 - np.nan_to_num(lat)) * (h / 180.0)).astype(np.int32), 0, h - 1)
    xi = np.mod(((np.nan_to_num(lon) + 180.0) * (w / 360.0)).astype(np.int32), w)
    base = background[yi, xi].astype(np.float32)
    cloud = _cloud_strength(source, bounds, infrared)
    opacity = np.clip((cloud - 0.20) / 0.80, 0, 1) ** 1.15
    out = base + (255.0 - base) * opacity[..., None]

    if rain is not None and rain_bounds is not None:
        # Cold, bright cloud tops suggest possible precipitation; this is a
        # visual estimate because channel 4 does not measure rainfall.
        thermal = cloud if infrared else _cloud_strength(rain, rain_bounds, True)
        visible = _cloud_strength(rain, rain_bounds, False) if infrared else cloud
        score = thermal * visible
        amount = np.clip((score - 0.60) / 0.30, 0, 0.88) * (rain > 0)
        lut_row = np.clip(65.0 - np.clip((score - 0.60) / 0.40, 0, 1) * 30.0,
                          0, 255).astype(np.uint8)
        colour = precipitation_lut()[lut_row].astype(np.float32)
        out = out * (1.0 - amount[..., None]) + colour * amount[..., None]

    out = np.clip(out, 0, 255).astype(np.uint8)
    out[(source == 0) | ~geo_ok] = 0
    return out


def render(chs: dict[int, np.ndarray], name: str, track=None, scan_deg: float | None = None) -> np.ndarray | None:
    """Render a named product; return None when its channels or geometry are unavailable."""
    if name not in REQUIRED or not all(ch in chs for ch in REQUIRED[name]):
        return None
    if name == "AVHRR 221 False Color":
        return _stack(chs, (2, 2, 1), balance=True)
    if name == "MSU-MR 124 False Color":
        return _stack(chs, (1, 2, 4), invert_last=True, balance=True)
    if name == "Infrared (Ch 4)":
        source = chs[4]
        out = 255 - source
        out[source == 0] = 0
        return out
    if track is None or scan_deg is None:
        return None
    from . import geo
    infrared = name == "MCIR"
    channel = 4 if infrared else 1
    source = chs[channel]
    h, w = source.shape
    nrows = min(h, len(track))
    out = np.zeros((h, w, 3), np.uint8)
    angles = geo.scan_angles(w, scan_deg)
    bounds = _cloud_scale(source, infrared)
    rain = chs.get(1 if infrared else 4) if name in ("MCIR", "MSA-Precipitation") else None
    rain_bounds = _cloud_scale(rain, not infrared) if rain is not None else None
    for start in range(0, nrows, 32):
        rows = np.arange(start, min(start + 32, nrows))
        lat, lon = geo.look_latlon(track, angles, rows)
        layer = rain[rows] if rain is not None and rain.shape[0] > rows[-1] else None
        out[rows] = _underlay_chunk(source[rows], lat, lon, infrared, bounds, layer, rain_bounds)
    return out
