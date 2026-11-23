"""Live world tracker for Meteor-M2-3 and Meteor-M2-4."""
from __future__ import annotations

import math
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QGroupBox, QLabel, QVBoxLayout, QWidget
from sgp4.api import jday

from . import i18n, orbit, overlay


TRACKED = ((57166, "M2-3", QColor(67, 225, 138)),
           (59051, "M2-4", QColor(91, 204, 255)))
MAP_WIDTH, MAP_HEIGHT = 2560, 1280
EARTH_RADIUS_KM = 6371.0
MAJOR_CITY_POPULATION = 5_000_000
MAX_MAJOR_CITY_LABELS = 15


def position(sat: orbit.Satellite, when: datetime) -> tuple[float, float, float]:
    """WGS84 subpoint latitude/longitude and height in km from the current TLE."""
    jd, fr = jday(when.year, when.month, when.day, when.hour, when.minute,
                  when.second + when.microsecond * 1e-6)
    error, r, _ = sat.sat.sgp4(jd, fr)
    if error:
        raise ValueError(f"SGP4 error {error}")
    theta = orbit._gmst(jd + fr)
    c, s = math.cos(theta), math.sin(theta)
    x, y, z = c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2]
    lon = (math.degrees(math.atan2(y, x)) + 180) % 360 - 180
    p = math.hypot(x, y)
    e2 = orbit.WGS84_F * (2 - orbit.WGS84_F)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(5):
        n = orbit.WGS84_A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * n * math.sin(lat), p)
    height = p / math.cos(lat) - n
    return math.degrees(lat), lon, height


def footprint_boundary(lat_deg: float, lon_deg: float, height_km: float,
                       samples: int = 144) -> list[tuple[float, float]]:
    """Ground points where a satellite is on the geometric 0-degree horizon."""
    if not math.isfinite(height_km) or height_km <= 0:
        return []
    radius = math.acos(EARTH_RADIUS_KM / (EARTH_RADIUS_KM + height_km))
    lat0, lon0 = math.radians(lat_deg), math.radians(lon_deg)
    result = []
    for step in range(samples):
        bearing = 2 * math.pi * step / samples
        lat = math.asin(max(-1.0, min(1.0,
            math.sin(lat0) * math.cos(radius) +
            math.cos(lat0) * math.sin(radius) * math.cos(bearing))))
        lon = lon0 + math.atan2(math.sin(bearing) * math.sin(radius) * math.cos(lat0),
                                 math.cos(radius) - math.sin(lat0) * math.sin(lat))
        result.append((math.degrees(lat), math.degrees(lon)))
    return result


class TrackerMap(QWidget):
    def __init__(self, station: tuple[float, float], station_name: str = "HOME",
                 text_scale: float = 1.3, parent=None):
        super().__init__(parent)
        self.station = station
        self.station_name = station_name
        self.text_scale = text_scale
        self.positions: dict[int, tuple[float, float]] = {}
        self.heights: dict[int, float] = {}
        self.tracks: dict[int, tuple[list[tuple[float, float]], list[tuple[float, float]]]] = {}
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._world_pixmap = None
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._drag_last = None
        try:
            self.major_cities = sorted(
                (city for city in overlay.load_cities() if city[4] >= MAJOR_CITY_POPULATION),
                key=lambda city: city[4], reverse=True)
        except (OSError, ValueError):
            self.major_cities = []
        self.setCursor(Qt.OpenHandCursor)

    def heightForWidth(self, width: int) -> int:
        return max(130, width // 2)

    def sizeHint(self) -> QSize:
        return QSize(580, 290)

    def minimumSizeHint(self) -> QSize:
        return QSize(260, 130)

    def _text_size(self, base: int) -> int:
        return max(6, round(base * self.text_scale / 1.3))

    def _point(self, lat: float, lon: float, rect: QRectF) -> QPointF:
        """Flat world map with home longitude at the horizontal centre."""
        delta_lon = (lon - self.station[1] + 180) % 360 - 180
        return QPointF(rect.center().x() + delta_lon / 360 * rect.width(),
                       rect.top() + (90 - lat) / 180 * rect.height())

    def _map_rect(self) -> QRectF:
        width = max(1, min(self.width() - 12, 2 * (self.height() - 12)))
        height = width / 2
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2,
                      width, height)

    def _draw_background(self) -> QPixmap:
        rect = QRectF(0, 0, MAP_WIDTH, MAP_HEIGHT)
        pix = QPixmap(MAP_WIDTH, MAP_HEIGHT)
        pix.fill(QColor(8, 31, 44))
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(39, 78, 92), 2))
        p.setBrush(Qt.NoBrush)
        for fraction in range(1, 6):
            y = rect.top() + fraction * rect.height() / 6
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        for fraction in range(1, 12):
            x = rect.left() + fraction * rect.width() / 12
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        p.setPen(QPen(QColor(79, 117, 128), 2.5))
        try:
            for part in overlay.load_coastline():
                previous = None
                for lon, lat in part:
                    if not math.isfinite(lon) or not math.isfinite(lat):
                        previous = None
                        continue
                    here = self._point(float(lat), float(lon), rect)
                    # Do not join coastlines across the map's wrapped edge.
                    if previous is not None and abs(here.x() - previous.x()) < rect.width() * 0.4:
                        p.drawLine(previous, here)
                    previous = here
        except (OSError, ValueError):
            pass  # the tracker still works if the optional coastline file is absent
        p.end()
        return pix

    def _view_point(self, point: QPointF, rect: QRectF, copy: int = 0) -> QPointF:
        center = rect.center()
        return QPointF(center.x() + self.pan_x +
                       (point.x() - center.x() + copy * rect.width()) * self.zoom,
                       center.y() + self.pan_y + (point.y() - center.y()) * self.zoom)

    def _clamp_pan(self, rect: QRectF):
        world_width = self.zoom * rect.width()
        self.pan_x = (self.pan_x + world_width / 2) % world_width - world_width / 2
        limit_y = (self.zoom - 1) * rect.height() / 2
        self.pan_y = max(-limit_y, min(limit_y, self.pan_y))

    def _draw_track(self, p: QPainter, positions: list[tuple[float, float]], rect: QRectF,
                    copy: int):
        previous = None
        for lat, lon in positions:
            base = self._point(lat, lon, rect)
            if previous is not None and abs(base.x() - previous.x()) < rect.width() * 0.4:
                p.drawLine(self._view_point(previous, rect, copy),
                           self._view_point(base, rect, copy))
            previous = base

    def _draw_footprint(self, p: QPainter, lat: float, lon: float, height_km: float,
                        rect: QRectF, copy: int):
        boundary = footprint_boundary(lat, lon, height_km)
        if not boundary:
            return
        points = []
        previous = None
        for edge_lat, edge_lon in boundary:
            # Unwrap longitude so the outline cannot cut across the date line.
            delta = (edge_lon - self.station[1] + 180) % 360 - 180
            if previous is not None:
                while delta - previous > 180:
                    delta -= 360
                while delta - previous < -180:
                    delta += 360
            previous = delta
            points.append(QPointF(rect.center().x() + delta / 360 * rect.width(),
                                  rect.top() + (90 - edge_lat) / 180 * rect.height()))
        path = QPainterPath(self._view_point(points[0], rect, copy))
        for point in points[1:]:
            path.lineTo(self._view_point(point, rect, copy))
        angle_deg = math.degrees(math.acos(EARTH_RADIUS_KM / (EARTH_RADIUS_KM + height_km)))
        if lat + angle_deg >= 90 or lat - angle_deg <= -90:
            # A polar footprint includes the pole; close along the map edge.
            edge_y = rect.top() if lat > 0 else rect.bottom()
            for x in (points[-1].x(), points[0].x()):
                path.lineTo(self._view_point(QPointF(x, edge_y), rect, copy))
        path.closeSubpath()
        p.drawPath(path)

    def _draw_major_cities(self, p: QPainter, rect: QRectF):
        """Label only large cities, keeping labels apart at every zoom level."""
        p.setFont(QFont("Consolas", self._text_size(8)))
        metrics = p.fontMetrics()
        occupied = []
        home = self._view_point(self._point(*self.station, rect), rect)
        occupied.append(QRectF(home.x() - 8, home.y() - metrics.height(),
                               metrics.horizontalAdvance(self.station_name) + 16,
                               metrics.height() * 2))
        drawn = set()
        home_lat, home_lon = self.station
        closest = sorted(self.major_cities, key=lambda city:
                         (city[1] - home_lat) ** 2 +
                         (math.cos(math.radians(home_lat)) *
                          ((city[2] - home_lon + 180) % 360 - 180)) ** 2)[:3]
        ordered = closest + [city for city in self.major_cities if city not in closest]
        for name, lat, lon, _, _population in ordered:
            if len(drawn) >= MAX_MAJOR_CITY_LABELS:
                break
            base = self._point(lat, lon, rect)
            for copy in (-1, 0, 1):
                point = self._view_point(base, rect, copy)
                if name in drawn or not rect.contains(point):
                    continue
                width = metrics.horizontalAdvance(name) + 4
                left = point.x() + 5 if point.x() + width + 5 < rect.right() else point.x() - width - 5
                label = QRectF(left, point.y() - metrics.height() / 2, width, metrics.height())
                if not rect.contains(label) or any(label.adjusted(-4, -3, 4, 3).intersects(box)
                                                   for box in occupied):
                    continue
                p.setPen(QColor(181, 210, 215))
                p.setBrush(QColor(181, 210, 215))
                p.drawEllipse(point, 1.5, 1.5)
                p.drawText(label, Qt.AlignLeft | Qt.AlignVCenter, name)
                occupied.append(label.adjusted(-4, -3, 4, 3))
                drawn.add(name)

    def paintEvent(self, _event):
        rect = self._map_rect()
        if self._world_pixmap is None:
            self._world_pixmap = self._draw_background()
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(7, 24, 34))
        p.setClipRect(rect)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        for copy in (-1, 0, 1):
            top_left = self._view_point(QPointF(rect.left(), rect.top()), rect, copy)
            target = QRectF(top_left.x(), top_left.y(),
                            rect.width() * self.zoom, rect.height() * self.zoom)
            if target.intersects(rect):
                p.drawPixmap(target, self._world_pixmap, QRectF(self._world_pixmap.rect()))
        self._draw_major_cities(p, rect)
        p.setFont(QFont("Consolas", self._text_size(8), QFont.Bold))
        for norad, name, colour in TRACKED:
            if norad in self.positions and norad in self.heights:
                fill = QColor(colour)
                fill.setAlpha(32)
                edge = QColor(colour)
                edge.setAlpha(165)
                p.setBrush(fill)
                p.setPen(QPen(edge, 1.4))
                lat, lon = self.positions[norad]
                for copy in (-1, 0, 1):
                    self._draw_footprint(p, lat, lon, self.heights[norad], rect, copy)
                p.setBrush(Qt.NoBrush)
            if norad in self.tracks:
                past, future = self.tracks[norad]
                faded = QColor(colour)
                faded.setAlpha(90)
                p.setPen(QPen(faded, 1.6))
                for copy in (-1, 0, 1):
                    self._draw_track(p, past, rect, copy)
                bright = QColor(colour)
                bright.setAlpha(245)
                p.setPen(QPen(bright, 2))
                for copy in (-1, 0, 1):
                    self._draw_track(p, future, rect, copy)
            if norad not in self.positions:
                continue
            base = self._point(*self.positions[norad], rect)
            for copy in (-1, 0, 1):
                point = self._view_point(base, rect, copy)
                if not rect.contains(point):
                    continue
                p.setPen(QPen(colour, 2))
                p.setBrush(QColor(8, 31, 44))
                p.drawEllipse(point, 5, 5)
                p.setPen(colour)
                label_width = p.fontMetrics().horizontalAdvance(name)
                label_x = point.x() + 7 if point.x() < rect.center().x() else point.x() - label_width - 7
                label_y = point.y() - 7 if point.y() > rect.center().y() else point.y() + 13
                label_x = max(rect.left() + 2, min(label_x, rect.right() - label_width - 2))
                label_y = max(rect.top() + 11, min(label_y, rect.bottom() - 2))
                p.drawText(QPointF(label_x, label_y), name)
        p.setFont(QFont("Consolas", self._text_size(7)))
        station_base = self._point(*self.station, rect)
        for copy in (-1, 0, 1):
            station = self._view_point(station_base, rect, copy)
            if rect.contains(station):
                p.setPen(QPen(QColor(255, 216, 103), 1.5))
                p.setBrush(QColor(255, 216, 103))
                p.drawEllipse(station, 4, 4)
                p.setPen(QColor(255, 216, 103))
                p.drawText(QPointF(station.x() + 6, station.y() - 5), self.station_name)
        p.setClipping(False)
        p.setPen(QPen(QColor(41, 71, 82), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(rect)
        p.setPen(QColor(168, 196, 205))
        p.setFont(QFont("Consolas", self._text_size(8)))
        p.drawText(QPointF(rect.left() + 8, rect.top() + 17), f"{self.zoom:.1f}×")
        p.end()

    def wheelEvent(self, event):
        rect = self._map_rect()
        if not rect.contains(event.position()):
            event.ignore()
            return
        old_zoom = self.zoom
        wheel_steps = event.angleDelta().y() / 120
        if not wheel_steps:
            wheel_steps = event.pixelDelta().y() / 120
        self.zoom = max(1.0, min(5.0, old_zoom * 1.25 ** wheel_steps))
        cursor = event.position()
        center = rect.center()
        ratio = self.zoom / old_zoom
        self.pan_x = cursor.x() - center.x() - (cursor.x() - center.x() - self.pan_x) * ratio
        self.pan_y = cursor.y() - center.y() - (cursor.y() - center.y() - self.pan_y) * ratio
        self._clamp_pan(rect)
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._map_rect().contains(event.position()):
            self._drag_last = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_last is not None:
            delta = event.position() - self._drag_last
            self.pan_x += delta.x()
            self.pan_y += delta.y()
            self._drag_last = event.position()
            self._clamp_pan(self._map_rect())
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_last is not None:
            self._drag_last = None
            self.setCursor(Qt.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self._map_rect().contains(event.position()):
            self.zoom = 1.0
            self.pan_x = self.pan_y = 0.0
            self.update()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class SatelliteTracker(QGroupBox):
    """Refreshes SGP4 positions once a second and TLEs in a worker thread."""

    def __init__(self, cfg, parent=None):
        texts = i18n.texts(cfg.get("gui", "language", "en"), "gui")
        super().__init__(texts["tracker_title"], parent)
        self.t = texts
        self.cfg = cfg
        self.sats = {}
        self.results = queue.Queue()
        self.refreshing = False
        self.next_refresh = 0.0
        self.next_track = 0.0
        self.cache_path = os.path.join(cfg.data_dir, "tracker_tle.txt")
        self.observer = orbit.Observer(cfg.getf("station", "latitude", 35.88),
                                       cfg.getf("station", "longitude", 14.45),
                                       cfg.getf("station", "altitude_m", 50))
        self.setMinimumWidth(275)
        self.setMaximumWidth(620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 12, 7, 7)
        layout.setSpacing(4)
        legend = QLabel("<span style='color:#43e18a'>● M2-3</span>  ·  "
                        "<span style='color:#5bccff'>● M2-4</span>  ·  "
                        "<span style='color:#8faab3'>Dim: past · Bright: next 45 min</span>")
        legend.setStyleSheet("font-size:10px")
        layout.addWidget(legend)
        hint = QLabel("Shaded: satellite receiving horizon · Drag to pan · Wheel to zoom · Double-click: home")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#829ca6;font-size:10px")
        layout.addWidget(hint)
        # Tracker labels stay at their normal size; the INI scale is for received imagery.
        self.map = TrackerMap((self.observer.lat, self.observer.lon),
                              cfg.get("station", "callsign", "").strip() or "HOME")
        self.map.setToolTip(f"{self.observer.lat:.4f}°, {self.observer.lon:.4f}°\n"
                            "Flat world map centred on home longitude\n"
                            "City labels: population 5 million or more\n"
                            "Shaded circles: geometric horizon (0° elevation)\n"
                            "Dim: previous 45 minutes · Bright: next 45 minutes\n"
                            "Wheel to zoom · Drag to pan · Double-click to return home")
        layout.addWidget(self.map, 1)
        home_detail = QLabel(f"Home  {self.observer.lat:+.4f}°, "
                             f"{self.observer.lon:+.4f}°  "
                             f"{self.observer.alt:.0f} m")
        home_detail.setStyleSheet("color:#e1bf67;font:9pt Consolas")
        layout.addWidget(home_detail)
        self.labels = {}
        for norad, name, colour in TRACKED:
            label = QLabel(f"{name}  ·  {self.t['tracker_wait']}")
            label.setStyleSheet(f"color:{colour.name()};font:9pt Consolas")
            layout.addWidget(label)
            self.labels[norad] = label
        self.note = QLabel(self.t["tracker_home_time"])
        self.note.setStyleSheet("color:#9fb3a8;font-size:10px")
        self.note.setToolTip(self.t["tracker_source"])
        layout.addWidget(self.note)
        self._load_cache()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self._tick()

    def _load_cache(self):
        cache = orbit.read_tle_file(self.cache_path)
        for norad, tle in orbit.read_tle_file(os.path.join(self.cfg.data_dir, "tle.txt")).items():
            if norad not in cache or orbit.tle_age_days(tle[0]) < orbit.tle_age_days(cache[norad][0]):
                cache[norad] = tle
        for norad, name, _ in TRACKED:
            if norad in cache:
                sat = orbit.Satellite(name, norad, [])
                try:
                    sat.load(*cache[norad])
                    self.sats[norad] = sat
                except (ValueError, TypeError):
                    pass

    def _refresh_tles(self):
        sats = [orbit.Satellite(name, norad, []) for norad, name, _ in TRACKED]
        try:
            orbit.update_tles(sats, self.cache_path, self.cfg.getf("tle", "max_age_hours", 12),
                              log=lambda _message: None,
                              proxy=self.cfg.get("tle", "proxy", ""), force=True)
            self.results.put({sat.norad: sat for sat in sats if sat.sat is not None})
        except Exception as exc:
            self.results.put(exc)

    def _tick(self):
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            result = None
        if result is not None:
            self.refreshing = False
            fresh = (isinstance(result, dict) and len(result) == len(TRACKED) and
                     all(orbit.tle_age_days(sat.tle[0]) < 1 for sat in result.values()))
            self.next_refresh = time.monotonic() + (12 * 3600 if fresh else 15 * 60)
            if isinstance(result, dict):
                for norad, sat in result.items():
                    current = self.sats.get(norad)
                    if current is None or orbit.tle_age_days(sat.tle[0]) < orbit.tle_age_days(current.tle[0]):
                        self.sats[norad] = sat
                        self.next_track = 0.0
        if not self.refreshing and time.monotonic() >= self.next_refresh:
            self.refreshing = True
            self.next_refresh = float("inf")
            threading.Thread(target=self._refresh_tles, daemon=True).start()

        now = datetime.now(timezone.utc)
        if time.monotonic() >= self.next_track:
            tracks = {}
            for norad, _name, _colour in TRACKED:
                sat = self.sats.get(norad)
                if sat is None:
                    continue
                try:
                    past = [position(sat, now + timedelta(minutes=n))[:2]
                            for n in range(-45, 1)]
                    future = [position(sat, now + timedelta(minutes=n))[:2]
                              for n in range(0, 46)]
                    tracks[norad] = (past, future)
                except (ValueError, TypeError, OverflowError):
                    pass
            self.map.tracks = tracks
            self.next_track = time.monotonic() + 30
        positions = {}
        heights = {}
        for norad, name, _ in TRACKED:
            sat = self.sats.get(norad)
            if sat is None:
                self.labels[norad].setText(f"{name}  ·  {self.t['tracker_wait']}")
                continue
            try:
                lat, lon, height = position(sat, now)
                az, el, _rng, _rr, _lat = self.observer.look(sat, now)
                positions[norad] = (lat, lon)
                heights[norad] = height
                age = orbit.tle_age_days(sat.tle[0])
                stale = f"  ⚠ {self.t['tracker_stale']}" if age > 7 else ""
                visible = self.t["tracker_above"] if el >= 0 else self.t["tracker_below"]
                self.labels[norad].setText(f"{name}  Az {az:03.0f}°  El {el:+.0f}° · {visible}{stale}")
                self.labels[norad].setToolTip(f"{name} (NORAD {norad})\n"
                                                 f"{self.t['tracker_subpoint']} {lat:.2f}°, {lon:.2f}°\n"
                                                 f"{self.t['tracker_age']} {age:.1f} d")
            except (ValueError, TypeError, OverflowError):
                self.labels[norad].setText(f"{name}  ·  {self.t['tracker_unavailable']}")
        self.map.positions = positions
        self.map.heights = heights
        self.map.update()
        local = now.astimezone()
        offset = local.strftime("%z")
        zone = f"UTC{offset[:3]}:{offset[3:]}" if offset else local.tzname() or "local"
        self.note.setText(f"{self.t['tracker_home_time']} · {local:%d/%m %H:%M:%S} {zone}")
