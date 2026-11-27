"""Build a north-up, multi-pass MSU-MR mosaic without changing decoded products.

Each completed pass retains a small row-time file. Older CADU recordings can be
decoded on demand when that file is absent. Processing runs outside the receiver
thread; output is replaced atomically after a complete rebuild.
"""
from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timezone

import numpy as np

from . import enhance, geo, imaging, lrpt, overlay, view_products

BUILD_LOCK = threading.Lock()
COMPOSITE_VIEWS = (
    "RGB 124", "RGB 124 (raw)", "RGB 221", "AVHRR 221 False Color",
    "MSU-MR 124 False Color", "3.9 µm Shortwave IR (Uncalibrated)",
    view_products.PRECIPITATION, "MCIR", "MSA",
    "Channel 1", "Channel 2", "Channel 4",
)
COMPOSITE_VIEWS_123 = (
    "RGB 123", "RGB 123 (raw)", "RGB 221", "AVHRR 221 False Color",
    "AVHRR 3a21 False Color", "MSA", "Channel 1", "Channel 2", "Channel 3",
)
COMPOSITE_VIEWS_NIGHT = ("Night IR", "Channel 4")
ALL_COMPOSITE_VIEWS = tuple(dict.fromkeys(
    COMPOSITE_VIEWS + COMPOSITE_VIEWS_123 + COMPOSITE_VIEWS_NIGHT + view_products.PRODUCTS +
    tuple(f"Channel {n}" for n in range(1, 7))))


def composite_views(channels: tuple[int, ...], has_raw: bool = True,
                    has_bounds: bool = True) -> tuple[str, ...]:
    if channels == (4,):
        return COMPOSITE_VIEWS_NIGHT if has_raw else ("Night IR",)
    if not has_raw:
        return (f"RGB {''.join(map(str, channels))}",)
    if channels == (1, 2, 4):
        return COMPOSITE_VIEWS if has_bounds else tuple(
            view for view in COMPOSITE_VIEWS if view not in ("MCIR", "MSA"))
    if channels == (1, 2, 3):
        return COMPOSITE_VIEWS_123 if has_bounds else tuple(
            view for view in COMPOSITE_VIEWS_123 if view != "MSA")
    return (f"RGB {''.join(map(str, channels))}",)


def _day(info):
    try:
        t = datetime.fromisoformat(info["start_utc"])
        return t.astimezone().strftime("%Y-%m-%d")
    except (KeyError, TypeError, ValueError):
        return None


def _sources(output_dir: str, day: str):
    for entry in os.scandir(output_dir):
        if not entry.is_dir() or entry.name == "Mosaics":
            continue
        for file in os.scandir(entry.path):
            if not file.name.endswith("_info.json"):
                continue
            try:
                with open(file.path, encoding="utf-8") as f:
                    info = json.load(f)
            except (OSError, ValueError):
                continue
            if _day(info) == day and info.get("tle"):
                yield file.path, info


def _load_pass(info_path: str, info: dict, spec: str = "124"):
    # A mosaic must contain real data from every channel in its recipe.
    required = {int(ch) for ch in spec}
    present = info.get("channels")
    if present is not None and not required.issubset(set(present)):
        return None
    folder = os.path.dirname(info_path)
    prefix = os.path.basename(info_path)[:-10]  # strip _info.json
    times_name = "_mosaic_times_ch4.npy" if spec == "444" else "_mosaic_times.npy"
    times_path = os.path.join(folder, prefix + times_name)
    image_path = os.path.join(folder, prefix + f"_rgb{spec}.png")
    if os.path.isfile(times_path) and os.path.isfile(image_path):
        times = np.load(times_path, allow_pickle=False)
        img = imaging.read_png(image_path)
        if info.get("rotated"):
            img = img[::-1, ::-1]
    elif os.path.isfile(times_path) and all(os.path.isfile(os.path.join(folder, prefix + f"_ch{n}.png"))
                                             for n in required):
        times = np.load(times_path, allow_pickle=False)
        channels = {n: imaging.read_png(os.path.join(folder, prefix + f"_ch{n}.png"))
                    for n in required}
        img = imaging.composite(channels, spec)
        if info.get("rotated"):
            img = img[::-1, ::-1]
    else:
        cadu = os.path.join(folder, prefix + ".cadu")
        if not os.path.isfile(cadu):
            return None
        imager = lrpt.MsuMrImager()
        lrpt.process_cadu_file(cadu, imager)
        if not {n - 1 for n in required}.issubset(imager.channels_present):
            return None
        channels = imaging.channel_images(imager, fill=True)
        img = imaging.composite(channels, spec)
        if img is None:
            return None
        times = imager.row_times(3 if spec == "444" else 1)
    if img.ndim != 3 or img.shape[2] != 3 or len(times) < 2:
        return None
    n = min(len(times), img.shape[0])
    if n < 2:
        return None
    timed = overlay.track_for(info.get("satellite"), info.get("tle"), times[:n])
    if timed is None:
        return None
    track, scan_deg = timed
    raw = np.ascontiguousarray(img[:n])
    if spec == "444":
        ir = view_products._inverted_grey(raw[:, :, 0])
        img = np.repeat(ir[:, :, None], 3, axis=2)
    else:
        img = enhance.colour(raw, balance=0.2, saturation=0.78)
    return img, track, scan_deg, prefix, raw


def _sunlit_rows(track: geo.Track) -> np.ndarray:
    """Classify each scan line by solar elevation at the swath centre."""
    lat, lon = geo.look_latlon(track, np.array([0.0]))
    lat, lon = np.radians(lat[:, 0]), lon[:, 0]
    utc = [datetime.fromtimestamp(float(t), timezone.utc) for t in track.t]
    hour = np.array([d.hour + d.minute / 60 + d.second / 3600 for d in utc])
    day = np.array([d.timetuple().tm_yday for d in utc])
    gamma = 2 * np.pi / 365 * (day - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * np.cos(gamma) + 0.070257 * np.sin(gamma)
            - 0.006758 * np.cos(2 * gamma) + 0.000907 * np.sin(2 * gamma)
            - 0.002697 * np.cos(3 * gamma) + 0.00148 * np.sin(3 * gamma))
    eqtime = 229.18 * (0.000075 + 0.001868 * np.cos(gamma) - 0.032077 * np.sin(gamma)
                       - 0.014615 * np.cos(2 * gamma) - 0.040849 * np.sin(2 * gamma))
    hour_angle = np.radians(((hour * 60 + eqtime + 4 * lon) % 1440) / 4 - 180)
    cos_zenith = (np.sin(lat) * np.sin(decl) +
                  np.cos(lat) * np.cos(decl) * np.cos(hour_angle))
    return np.isfinite(cos_zenith) & (cos_zenith > 0)


def _unwrap(lon, centre):
    return centre + (lon - centre + 180.0) % 360.0 - 180.0


def _bounds(passes):
    first_img, first_track, first_scan, _, _ = passes[0]
    first_valid = np.flatnonzero(first_img.max(axis=(1, 2)) > 0)
    mid = int(first_valid[len(first_valid) // 2])
    _, lon0 = geo.look_latlon(first_track, np.array([0.0]), np.array([mid]))
    centre = float(lon0[0, 0])
    latitudes, longitudes = [], []
    for img, track, scan_deg, _, _ in passes:
        valid_rows = np.flatnonzero(img.max(axis=(1, 2)) > 0)
        if not valid_rows.size:
            continue
        rows = valid_rows[np.unique(np.linspace(
            0, len(valid_rows) - 1, min(80, len(valid_rows))).astype(int))]
        columns = np.linspace(0, img.shape[1] - 1, 13).astype(int)
        angles = geo.scan_angles(img.shape[1], scan_deg)[columns]
        lat, lon = geo.look_latlon(track, angles, rows)
        good = np.isfinite(lat) & np.isfinite(lon)
        latitudes.extend(lat[good].tolist())
        longitudes.extend(_unwrap(lon[good], centre).tolist())
    if not latitudes:
        raise ValueError("No valid geolocated pixels")
    south, north = min(latitudes) - 0.7, max(latitudes) + 0.7
    west, east = min(longitudes) - 0.7, max(longitudes) + 0.7
    cos_lat = max(0.2, math.cos(math.radians((south + north) / 2.0)))
    step = max(0.035, (north - south) / 1800.0, (east - west) * cos_lat / 1800.0)
    width = int(math.ceil((east - west) * cos_lat / step)) + 1
    height = int(math.ceil((north - south) / step)) + 1
    return south, north, west, east, cos_lat, step, width, height, centre


def _raster_pass(img, raw, track, scan_deg, bounds):
    _, north, west, _, cos_lat, step, width, height, centre = bounds
    size = width * height
    total = np.zeros(size, np.float32)
    channels = np.zeros((3, size), np.float32)
    raw_channels = np.zeros((3, size), np.float32)
    opacity = np.zeros(size, np.float32)
    h, w = img.shape[:2]
    angles = geo.scan_angles(w, scan_deg)
    edge_x = np.minimum(np.arange(w) + 1, w - np.arange(w)) / 35.0
    edge_x = np.clip(edge_x, 0.0, 1.0).astype(np.float32)
    for start in range(0, h, 32):
        stop = min(start + 32, h)
        rows = np.arange(start, stop)
        lat, lon = geo.look_latlon(track, angles, rows)
        x = np.rint((_unwrap(lon, centre) - west) * cos_lat / step).astype(np.int32)
        y = np.rint((north - lat) / step).astype(np.int32)
        pixels = img[start:stop]
        valid = (np.isfinite(lat) & np.isfinite(lon) &
                 (x >= 0) & (x < width) & (y >= 0) & (y < height) &
                 (pixels.max(axis=2) > 0))
        if not valid.any():
            continue
        indexes = (y[valid] * width + x[valid]).astype(np.intp)
        values = pixels[valid].astype(np.float32)
        raw_values = raw[start:stop][valid].astype(np.float32)
        edge_y = np.clip(np.minimum(rows + 1, h - rows) / 24.0, 0.0, 1.0)
        weight = (edge_y[:, None] * edge_x[None, :])[valid].astype(np.float32)
        np.add.at(total, indexes, 1.0)
        np.maximum.at(opacity, indexes, weight)
        for c in range(3):
            np.add.at(channels[c], indexes, values[:, c])
            np.add.at(raw_channels[c], indexes, raw_values[:, c])
    covered = total > 0
    if not covered.any():
        return None
    result = np.zeros((size, 3), np.float32)
    raw_result = np.zeros((size, 3), np.float32)
    for c in range(3):
        result[covered, c] = channels[c, covered] / total[covered]
        raw_result[covered, c] = raw_channels[c, covered] / total[covered]
    return result.reshape(height, width, 3), raw_result.reshape(height, width, 3), opacity.reshape(height, width)


def _draw_coastlines(img: np.ndarray, bounds, colour_key: str = overlay.DEFAULT_COAST) -> None:
    """Draw Natural Earth coastlines in the mosaic's map projection, on received pixels only."""
    _, north, west, _, cos_lat, step, width, height, centre = bounds
    covered = img.max(axis=2) > 0
    rgb = overlay.colour(colour_key)
    for part in overlay.load_coastline():
        if len(part) < 2:
            continue
        lat = part[:, 1]
        lon = _unwrap(part[:, 0], centre)
        if lat.max() < north - step * height or lat.min() > north or \
                lon.max() < west or lon.min() > west + step * width / cos_lat:
            continue
        x = np.rint((lon - west) * cos_lat / step).astype(np.int32)
        y = np.rint((north - lat) / step).astype(np.int32)
        inside = (x >= 0) & (x < width) & (y >= 0) & (y < height)
        for i in np.flatnonzero(inside[:-1] & inside[1:]):
            dx, dy = int(x[i + 1] - x[i]), int(y[i + 1] - y[i])
            n = max(abs(dx), abs(dy))
            if n > 100:  # discontinuity at the date line or between coastline parts
                continue
            if n:
                t = np.arange(n + 1)
                xs = x[i] + np.rint(dx * t / n).astype(np.int32)
                ys = y[i] + np.rint(dy * t / n).astype(np.int32)
            else:
                xs, ys = x[i:i + 1], y[i:i + 1]
            valid = covered[ys, xs]
            img[ys[valid], xs[valid]] = rgb


def _label_cities(img: np.ndarray, bounds, min_population: int,
                  colour_key: str = overlay.DEFAULT_CITY, scale: float = 1.3) -> list[str]:
    """Place major cities on received mosaic pixels using the mosaic's own map projection."""
    _, north, west, _, cos_lat, step, width, height, centre = bounds
    covered = img.max(axis=2) > 0
    city_rgb = overlay.colour(colour_key, overlay.DEFAULT_CITY)
    font = overlay.font()
    occupied = []
    names = []
    for name, lat, lon, _, population in sorted(overlay.load_cities(), key=lambda c: c[4], reverse=True):
        if population < min_population:
            break
        x = int(round((_unwrap(lon, centre) - west) * cos_lat / step))
        y = int(round((north - lat) / step))
        if not (0 <= x < width and 0 <= y < height):
            continue
        if not covered[max(0, y - 3):min(height, y + 4), max(0, x - 3):min(width, x + 4)].any():
            continue
        mask = overlay.scaled_mask(font.mask(name), scale)
        label_x = x + 8 if x + 8 + mask.shape[1] < width else x - 8 - mask.shape[1]
        label_y = y - round(font.ascent * scale / 2)
        box = (label_x - 4, label_y - 4, label_x + mask.shape[1] + 4, label_y + mask.shape[0] + 4)
        if any(box[0] < b[2] and box[2] > b[0] and box[1] < b[3] and box[3] > b[1]
               for b in occupied):
            continue
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                if dx * dx + dy * dy <= 9 and 0 <= y + dy < height and 0 <= x + dx < width:
                    img[y + dy, x + dx] = city_rgb
        overlay._blit(img, mask, label_y, label_x, city_rgb, overlay._outline(city_rgb))
        occupied.append(box)
        names.append(name)
        if len(names) >= 80:
            break
    return names


def render_composite(base: np.ndarray, bounds, min_city_population: int = 200_000, *,
                     colour_enhancement: bool = False, infrared_enhancement: bool = False,
                     infrared_grey: bool = False,
                     coastlines: bool = False, cities: bool = True,
                     coast_colour: str = overlay.DEFAULT_COAST,
                     city_colour: str = overlay.DEFAULT_CITY,
                     city_scale: float = 1.3, mirror: bool = False) -> np.ndarray:
    """Enhance a finished mosaic without changing its source or geographic proportions."""
    image = np.ascontiguousarray(base.copy())
    if colour_enhancement:
        image = enhance.colour(image)
    if infrared_enhancement:
        valid = image.max(axis=2) > 0
        blue = enhance.infrared(image[:, :, 2])
        if infrared_grey:
            image[:, :, :] = blue[:, :, None]
        else:
            image[:, :, 2] = blue
        image[~valid] = 0
    if coastlines:
        _draw_coastlines(image, bounds, coast_colour)
    if cities:
        _label_cities(image, bounds, min_city_population, city_colour, city_scale)
    return image[:, ::-1].copy() if mirror else image


def composite_view(display_base: np.ndarray, raw_base: np.ndarray | None, name: str,
                   source_channels: tuple[int, ...] = (1, 2, 4), bounds=None) -> np.ndarray:
    """Select a mapped view using only channels actually stored in the composite."""
    if source_channels == (4,):
        if name == "Night IR":
            return display_base.copy()
        if name == "Channel 4" and raw_base is not None:
            return np.repeat(raw_base[:, :, 0, None], 3, axis=2)
        raise ValueError(f"Composite view unavailable: {name}")
    spec = "".join(map(str, source_channels))
    if name == f"RGB {spec}":
        return display_base.copy()
    if raw_base is None:
        raise ValueError("Rebuild this composite to add channel views")
    if name == f"RGB {spec} (raw)":
        return raw_base.copy()
    channels = {ch: raw_base[:, :, i] for i, ch in enumerate(source_channels)}
    if name == "RGB 221":
        return imaging.composite(channels, "221")
    if name.startswith("Channel "):
        channel = int(name.split()[-1])
        return np.repeat(channels[channel][:, :, None], 3, axis=2)
    if name in ("MCIR", "MSA") and bounds is not None:
        _, north, west, _, cos_lat, step, width, height, _ = bounds
        source = channels[4 if name == "MCIR" else 1]
        result = np.zeros((height, width, 3), np.uint8)
        lon = west + np.arange(width)[None, :] * step / cos_lat
        for start in range(0, height, 128):
            stop = min(start + 128, height)
            lat = north - np.arange(start, stop)[:, None] * step
            result[start:stop] = view_products._underlay_chunk(
                source[start:stop], np.broadcast_to(lat, (stop - start, width)),
                np.broadcast_to(lon, (stop - start, width)), name == "MCIR")
        return result
    if name in composite_views(source_channels):
        result = view_products.render(channels, name)
        if result is not None:
            return (np.repeat(result[:, :, None], 3, axis=2)
                    if result.ndim == 2 else result)
    raise ValueError(f"Composite view unavailable: {name}")


def _build_spec(output_dir: str, day: str, spec: str, period: str, log,
                min_city_population: int, city_label_scale: float) -> str | None:
    """Build one local day's mosaic from passes carrying a specific channel triple."""
    passes = []
    for path, info in _sources(output_dir, day):
        try:
            item = _load_pass(path, info, spec)
            if item is not None:
                img, track, scan_deg, prefix, raw = item
                day_rows = _sunlit_rows(track)
                keep = day_rows if period == "day" else ~day_rows
                if not keep.any() or not img[keep].any():
                    continue
                img = img.copy()
                raw = raw.copy()
                img[~keep] = 0
                raw[~keep] = 0
                passes.append((info.get("start_utc", ""),
                               (img, track, scan_deg, prefix, raw)))
        except (OSError, ValueError, IndexError) as exc:
            if log:
                log(f"Composite skipped {os.path.basename(path)}: {exc}")
    if not passes:
        return None
    # Put dim visible-channel passes underneath brighter daylight passes. A
    # later night pass must not paint a large blue rectangle over a day map.
    def visible_brightness(pair):
        raw = pair[1][4]
        sample = raw[::16, ::16, :2]
        valid = (sample > 0).all(axis=2)
        return float(np.median(sample[valid])) if valid.any() else 0.0

    passes.sort(key=(lambda pair: pair[0]) if period == "night" else
                (lambda pair: (visible_brightness(pair), pair[0])))
    sources = [item for _, item in passes]
    bounds = _bounds(sources)
    _, _, _, _, _, _, width, height, _ = bounds
    canvas = np.zeros((height, width, 3), np.float32)
    raw_canvas = np.zeros_like(canvas)
    included = []
    for img, track, scan_deg, prefix, raw in sources:
        rendered = _raster_pass(img, raw, track, scan_deg, bounds)
        if rendered is None:
            continue
        layer, raw_layer, alpha = rendered
        new = alpha > 0
        existing = canvas.max(axis=2) > 0
        overlap = new & existing
        uncovered = new & ~existing
        if overlap.any():
            weight = alpha[overlap][:, None]
            canvas[overlap] = canvas[overlap] * (1.0 - weight) + layer[overlap] * weight
            raw_canvas[overlap] = (raw_canvas[overlap] * (1.0 - weight) +
                                   raw_layer[overlap] * weight)
        canvas[uncovered] = layer[uncovered]
        raw_canvas[uncovered] = raw_layer[uncovered]
        included.append(prefix)
    if not included:
        return None
    folder = os.path.join(output_dir, "Mosaics")
    os.makedirs(folder, exist_ok=True)
    stem = day + ("_night_ir" if period == "night" else f"_rgb{spec}")
    path = os.path.join(folder, stem + ".png")
    meta_path = os.path.join(folder, stem + ".json")
    try:
        with open(meta_path, encoding="utf-8") as source:
            previous = json.load(source)
    except (OSError, ValueError):
        previous = {}
    if previous.get("period") == period and not set(previous.get("passes", ())).issubset(included):
        # Some original reception folders have been removed. Keep their pixels
        # in the existing map, while allowing a label-size change from the INI.
        if (previous.get("city_label_scale") != city_label_scale or
                previous.get("city_min_population") != min_city_population):
            base_name = previous.get("base_image")
            bounds = previous.get("bounds")
            if isinstance(base_name, str) and isinstance(bounds, list) and len(bounds) == 9:
                try:
                    old_base = imaging.read_png(os.path.join(folder, base_name))
                    relabelled = render_composite(
                        old_base, tuple(bounds), min_city_population,
                        cities=True, city_scale=city_label_scale)
                    imaging.write_png(path, relabelled)
                    previous["city_label_scale"] = city_label_scale
                    previous["city_min_population"] = min_city_population
                    with open(meta_path + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(previous, f, indent=2)
                    os.replace(meta_path + ".tmp", meta_path)
                except (OSError, ValueError):
                    pass
        if log:
            log(f"Composite preserved: {path} (older source passes are unavailable)")
        return path
    base = np.clip(canvas, 0, 255).astype(np.uint8)
    base_path = os.path.join(folder, stem + "_base.png")
    imaging.write_png(base_path, base)
    raw_path = os.path.join(folder, stem + "_channels.png")
    imaging.write_png(raw_path, np.clip(raw_canvas, 0, 255).astype(np.uint8))
    image = base.copy()
    city_names = _label_cities(image, bounds, min_city_population,
                               scale=city_label_scale)
    imaging.write_png(path, image)
    with open(meta_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"local_day": day, "passes": included, "width": width,
                   "height": height, "projection": "local equirectangular",
                   "image": os.path.basename(path),
                   "channels": sorted({int(ch) for ch in spec}), "period": period,
                   "cities": city_names,
                   "base_image": os.path.basename(base_path), "bounds": list(bounds),
                   "channel_image": os.path.basename(raw_path),
                   "city_min_population": min_city_population,
                   "city_label_scale": city_label_scale}, f, indent=2)
    os.replace(meta_path + ".tmp", meta_path)
    if log:
        log(f"Composite updated: {path} ({len(included)} pass(es), {len(city_names)} cities)")
    return path


def build_day(output_dir: str, day: str, log=None, min_city_population: int = 200_000,
              city_label_scale: float = 1.3) -> str | None:
    """Build separate daylight colour and nighttime channel 4 IR mosaics."""
    city_label_scale = min(5.0, max(0.8, city_label_scale))
    rgb124 = _build_spec(output_dir, day, "124", "day", log, min_city_population, city_label_scale)
    rgb123 = _build_spec(output_dir, day, "123", "day", log, min_city_population, city_label_scale)
    night_ir = _build_spec(output_dir, day, "444", "night", log, min_city_population, city_label_scale)
    return rgb124 or rgb123 or night_ir
