"""METEOR-X main window (PySide6). Left: controls and signal validation. Right: imagery."""
from __future__ import annotations

import json
import filecmp
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from . import enhance, i18n
from PySide6.QtCore import QByteArray, QRectF, Qt, QTimer, QUrl, Signal, QSize
from PySide6.QtGui import (QAction, QBrush, QColor, QDesktopServices, QFont, QGuiApplication, QImage, QImageReader, QKeySequence, QLinearGradient,
                           QPainter, QPalette, QPen, QPixmap, QShortcut)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QDialog, QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QGridLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenuBar,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                               QSlider, QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
                               QWidget)

from . import __build__, __version__, calib, config, geometry, imaging, lrpt, orbit, outputs, overlay, profiles, sdr, tracker, view_products, weather
from .engine import Engine

# ----------------------------------------------------------------------------
# Translations
# ----------------------------------------------------------------------------
T = {code: i18n.texts(code, "gui") for code in i18n.LANGUAGES}   # all UI texts: meteorrx/i18n.py
DEV_KEYS = ["rtlsdr", "sdrplay", "hackrf", "lime", "pluto"]
LIME_ANT = ["auto", "LNAW", "LNAH", "LNAL"]
BIAS_KEYS = {"rtlsdr": "rtl_bias_tee", "sdrplay": "sdrplay_bias_tee", "hackrf": "hackrf_bias_tee"}

DEFAULT_ACCENT = QColor("#4cff9a")
ACCENT = QColor(DEFAULT_ACCENT)


def theme_color(value) -> QColor:
    """Recolour green highlights while leaving dark panels and neutral text alone."""
    colour = QColor(value)
    if ACCENT == DEFAULT_ACCENT:
        return colour
    if colour.name().lower() == DEFAULT_ACCENT.name():
        return QColor(ACCENT)
    hue, saturation, brightness, alpha = colour.getHsvF()
    if hue < 0 or saturation < 0.18 or brightness < 0.28:
        return colour
    original_hue = DEFAULT_ACCENT.hueF()
    distance = (hue - original_hue + 0.5) % 1.0 - 0.5
    if abs(distance) > 0.22:
        return colour  # warning red, yellow, satellite blue and neutral colours
    target_hue = ACCENT.hueF()
    if target_hue < 0:
        target_hue = original_hue
    colour.setHsvF((hue + target_hue - original_hue) % 1.0,
                   min(1.0, saturation * ACCENT.saturationF() / DEFAULT_ACCENT.saturationF()),
                   brightness, alpha)
    return colour


def recorded_cadu_info(path: str) -> dict:
    """Find pass metadata for a CADU, including a renamed copy beside its original."""
    folder = os.path.dirname(path)
    exact = os.path.splitext(path)[0] + "_info.json"

    def read(info_path):
        try:
            with open(info_path, encoding="utf-8") as source:
                info = json.load(source)
            return info if isinstance(info, dict) else None
        except (OSError, ValueError):
            return None

    info = read(exact)
    if info is not None:
        return info
    try:
        for name in os.listdir(folder):
            if not name.endswith("_info.json"):
                continue
            info_path = os.path.join(folder, name)
            original = info_path[:-10] + ".cadu"
            if info_path != exact and os.path.isfile(original) and filecmp.cmp(path, original, shallow=False):
                info = read(info_path)
                if info is not None:
                    return info
    except OSError:
        pass
    return {}


# ----------------------------------------------------------------------------
# Signal widgets
# ----------------------------------------------------------------------------
def _phosphor_lut() -> np.ndarray:
    x = np.linspace(0, 1, 256)
    r = np.clip(3 * x - 1.6, 0, 1)
    g = np.clip(1.6 * x, 0, 1)
    b = np.clip(2.2 * x - 1.2, 0, 1) * 0.8 + 0.08 * (1 - x)
    lut = (0xFF << 24) | ((r * 255).astype(np.uint32) << 16) | ((g * 255).astype(np.uint32) << 8) | \
          (b * 255 + 14).clip(0, 255).astype(np.uint32)
    if ACCENT != DEFAULT_ACCENT:
        for i, pixel in enumerate(lut):
            source = QColor.fromRgb(int((pixel >> 16) & 255), int((pixel >> 8) & 255), int(pixel & 255))
            tinted = theme_color(source)
            lut[i] = 0xFF000000 | (tinted.red() << 16) | (tinted.green() << 8) | tinted.blue()
    return lut.astype(np.uint32)


_LUT = _phosphor_lut()


class DensityWidget(QWidget):
    """Persistence display base (eye diagram / constellation)."""

    def __init__(self, w=300, h=170, decay=0.80, parent=None):
        super().__init__(parent)
        self.W, self.H = w, h
        self.acc = np.zeros((h, w), np.float32)
        self.decay = decay
        self.setMinimumHeight(140)
        self._img = None
        self.title = ""
        self.overlay = ""

    def _render(self):
        a = self.acc
        m = np.percentile(a[a > 0], 99.7) if (a > 0).any() else 1.0
        idx = np.clip(np.log1p(4.0 * a / max(m, 1e-6)) / np.log1p(4.0) * 255, 0, 255).astype(np.uint8)
        buf = np.ascontiguousarray(_LUT[idx])
        self._buf = buf
        self._img = QImage(buf.data, self.W, self.H, self.W * 4, QImage.Format_ARGB32)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), theme_color(QColor(4, 16, 19)))
        if self._img is not None:
            p.drawImage(QRectF(self.rect()), self._img)
        self.draw_grid(p)
        r = self.rect().adjusted(1, 1, -2, -2)
        p.setPen(QPen(theme_color(QColor(28, 101, 99)), 1))
        p.drawRect(r)
        p.setPen(QPen(theme_color(QColor(82, 247, 193)), 2))
        for x, direction in ((r.left(), 1), (r.right(), -1)):
            p.drawLine(x, r.top(), x + 15 * direction, r.top())
            p.drawLine(x, r.bottom(), x + 15 * direction, r.bottom())
        p.setPen(theme_color(QColor(98, 242, 212)))
        p.setFont(QFont("Consolas", 8, QFont.Bold))
        p.drawText(9, 16, self.title.upper())
        if self.overlay:
            p.drawText(self.rect().adjusted(0, 0, -6, -4), Qt.AlignRight | Qt.AlignBottom, self.overlay)
        p.end()

    def draw_grid(self, p):
        pass


class EyeWidget(DensityWidget):
    def __init__(self, parent=None):
        super().__init__(320, 180, 0.70, parent)
        self.ylim = 1.8

    def update_traces(self, eye):
        self.acc *= self.decay
        if eye is not None:
            t, tr = eye
            # dense resampling along x for continuous traces
            xs = np.linspace(t[0], t[-1], self.W)
            for y in tr:
                yy = np.interp(xs, t, y)
                yi = np.clip(((self.ylim - yy) / (2 * self.ylim) * (self.H - 1)).astype(int), 0, self.H - 1)
                np.add.at(self.acc, (yi, np.arange(self.W)), 1.0)
            # eye quality at the decision instant: 20 log10(mean |y| / std |y|)
            c = np.abs(tr[:, tr.shape[1] // 2])
            q = 20 * np.log10(c.mean() / c.std()) if c.std() > 0 else 40.0
            self.overlay = f"Q {q:4.1f} dB"
        else:
            self.overlay = ""
        self._render()
        self.update()

    def draw_grid(self, p):
        r = self.rect()
        p.setPen(QPen(theme_color(QColor(34, 90, 91)), 1, Qt.DotLine))
        for k in (0.25, 0.5, 0.75):
            x = r.left() + k * r.width()
            p.drawLine(int(x), r.top(), int(x), r.bottom())
        for v in (-1, 0, 1):
            y = r.top() + (self.ylim - v) / (2 * self.ylim) * r.height()
            p.drawLine(r.left(), int(y), r.right(), int(y))


class ConstWidget(DensityWidget):
    def __init__(self, parent=None):
        super().__init__(160, 160, 0.6, parent)
        self.setMinimumHeight(140)

    def update_syms(self, s):
        self.acc *= self.decay
        if s is not None and len(s):
            lim = 1.8
            xi = np.clip(((s.real + lim) / (2 * lim) * (self.W - 1)).astype(int), 0, self.W - 1)
            yi = np.clip(((lim - s.imag) / (2 * lim) * (self.H - 1)).astype(int), 0, self.H - 1)
            np.add.at(self.acc, (yi, xi), 1.0)
        self._render()
        self.update()

    def draw_grid(self, p):
        r = self.rect()
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(theme_color(QColor(35, 100, 102)), 1, Qt.DotLine))
        p.drawLine(r.center().x(), r.top(), r.center().x(), r.bottom())
        p.drawLine(r.left(), r.center().y(), r.right(), r.center().y())
        side = min(r.width(), r.height())
        for fraction in (0.25, 0.42):
            radius = int(side * fraction)
            p.drawEllipse(r.center(), radius, radius)


class SpectrumWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.data = None
        self.smooth = None
        self.base_floor = None
        self.floor_offset_db = 0

    def set_floor_offset(self, db: int):
        self.floor_offset_db = db
        self.update()

    def update_spec(self, spec):
        if spec is None:
            if self.data is not None:
                self.data = self.smooth = self.base_floor = None
                self.update()
            return
        f, db, marks = spec
        if self.base_floor is None:
            self.base_floor = float(np.percentile(db, 5)) - 8.0
        if self.smooth is None or len(self.smooth) != len(db):
            self.smooth = db.copy()
        else:
            self.smooth = 0.25 * self.smooth + 0.75 * db
        self.data = (f, self.smooth, marks)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, theme_color(QColor(4, 16, 19)))
        p.setPen(QPen(theme_color(QColor(28, 80, 83)), 1, Qt.DotLine))
        for i in range(1, 8):
            x = r.left() + i * r.width() // 8
            p.drawLine(x, r.top(), x, r.bottom())
        for i in range(1, 4):
            y = r.top() + i * r.height() // 4
            p.drawLine(r.left(), y, r.right(), y)
        if self.data is not None:
            f, db, marks = self.data
            n = r.width()
            edges = np.linspace(0, len(db), n + 1).astype(int)
            edges[-1] = len(db)
            sums = np.r_[0.0, np.cumsum(db, dtype=np.float64)]
            mean = (sums[edges[1:]] - sums[edges[:-1]]) / np.maximum(1, np.diff(edges))
            peaks = np.maximum.reduceat(db, np.minimum(edges[:-1], len(db) - 1))
            v = 0.65 * mean + 0.35 * peaks  # retain narrow carriers without a wide glow
            lo = self.base_floor + self.floor_offset_db
            hi = lo + 45.0  # fixed span while receiving: a rising floor remains visible
            bottom, top = r.bottom() - 3, r.top() + 20
            ys = bottom - np.clip((v - lo) / (hi - lo), 0.0, 1.0) * (bottom - top)
            pts = [(r.left() + i, int(y)) for i, y in enumerate(ys)]
            p.setPen(QPen(ACCENT, 1))
            for i in range(1, len(pts)):
                p.drawLine(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
            p.setPen(QPen(QColor(230, 180, 60), 1, Qt.DashLine))
            for m in marks:
                x = r.left() + (m - f[0]) / (f[-1] - f[0]) * r.width()
                p.drawLine(int(x), r.top() + 12, int(x), r.bottom())
                p.drawText(int(x) + 3, r.top() + 11, f"{m / 1e6:.3f}")
        frame = r.adjusted(1, 1, -2, -2)
        p.setPen(QPen(theme_color(QColor(28, 101, 99)), 1))
        p.drawRect(frame)
        p.setPen(theme_color(QColor(98, 242, 212)))
        p.setFont(QFont("Consolas", 8, QFont.Bold))
        p.drawText(9, 16, "RF SPECTRUM  /  137 MHz")
        p.end()


class Led(QLabel):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.set(False)

    def set(self, on: bool, warn: bool = False):
        col = theme_color("#3ddc84").name() if on else ("#e0a030" if warn else "#3a4540")
        fg = "#06140c" if on else "#c8d4cc"
        self.setStyleSheet(f"QLabel{{background:{col};color:{fg};border-radius:9px;padding:2px 8px;"
                           f"font-weight:600;font-size:11px}}")


class GainSlider(QWidget):
    """A stepped gain slider with a value readout; callers use hardware units."""

    valueChanged = Signal(float)

    def __init__(self, low: float, high: float, step: float = 1.0, unit: str = "dB", parent=None):
        super().__init__(parent)
        self.low, self.step, self.unit = low, step, unit
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, round((high - low) / step))
        self.slider.setPageStep(max(1, round(5 / step)))
        self.readout = QLabel()
        self.readout.setMinimumWidth(49 if step < 1 else 38)
        self.readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.slider, 1)
        row.addWidget(self.readout)
        self.slider.valueChanged.connect(self._changed)
        self._changed(self.slider.value())

    def value(self):
        v = self.low + self.slider.value() * self.step
        return round(v, 1) if self.step < 1 else int(round(v))

    def setValue(self, value):
        self.slider.setValue(round((float(value) - self.low) / self.step))
        self._update_readout()

    def _update_readout(self):
        v = self.value()
        num = f"{v:.1f}" if self.step < 1 else str(v)
        self.readout.setText(f"{num} {self.unit}".rstrip())

    def _changed(self, _):
        self._update_readout()
        self.valueChanged.emit(float(self.value()))


class SMeterWidget(QWidget):
    """Meteor channel power: calibrated dBm + IARU S-units, or dBFS when no calibration matches."""
    LO, HI = -141.0, -53.0          # S1 .. S9+40 (IARU R1 VHF scale)

    def __init__(self, uncal_text: str, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(38)
        self.uncal_text = uncal_text
        self.value = None
        self.cal = False
        self.line2 = ""

    def set_level(self, dbfs, n0_dbfs_hz, cal_k):
        if dbfs is None or dbfs < -900:
            self.value = None
            self.line2 = ""
        else:
            v = dbfs + (cal_k or 0.0)
            self.value = v if self.value is None else 0.7 * self.value + 0.3 * v
            self.cal = cal_k is not None
            cn0 = dbfs - n0_dbfs_hz if n0_dbfs_hz > -900 else float("nan")
            n0 = n0_dbfs_hz + (cal_k or 0.0)
            unit = "dBm/Hz" if self.cal else "dBFS/Hz"
            self.line2 = f"N0 {n0:.1f} {unit}  C/N0 {cn0:.1f} dBHz"
        self.update()

    def paintEvent(self, _):
        from . import calib
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, QColor(8, 12, 10))
        bar = r.adjusted(4, 14, -196, -4)
        p.setPen(QPen(theme_color(QColor(60, 90, 70)), 1))
        p.drawRect(bar)
        lo, hi = (self.LO, self.HI) if self.cal else (-110.0, -20.0)
        if self.value is not None:
            frac = min(max((self.value - lo) / (hi - lo), 0.0), 1.0)
            fill = bar.adjusted(1, 1, -bar.width() + int(frac * bar.width()), -1)
            col = ACCENT if (not self.cal or self.value < calib.S9_DBM_VHF) else QColor(230, 160, 60)
            p.fillRect(fill, col)
        p.setFont(QFont("Consolas", 7))
        p.setPen(QColor(150, 180, 160))
        if self.cal:
            for lab, dbm in (("1", -141), ("3", -129), ("5", -117), ("7", -105), ("9", -93), ("+20", -73),
                             ("+40", -53)):
                x = bar.left() + (dbm - lo) / (hi - lo) * bar.width()
                p.drawText(int(x) - (12 if lab == "+40" else 4), 11, lab)
        p.setFont(QFont("Consolas", 9))
        p.setPen(QColor(220, 235, 225))
        if self.value is None:
            txt = "–"
        elif self.cal:
            txt = f"{self.value:6.1f} dBm  {calib.s_units(self.value)}"
        else:
            txt = f"{self.value:6.1f} dBFS ({self.uncal_text})"
        p.drawText(r.adjusted(r.width() - 186, 0, 0, -16), Qt.AlignLeft | Qt.AlignVCenter, txt)
        p.setFont(QFont("Consolas", 7))
        p.drawText(r.adjusted(r.width() - 186, 18, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, self.line2)
        p.end()


class WeatherHeader(QFrame):
    """Station weather across the full header, underneath its menu and labels."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Current station weather background")
        self._art = QPixmap()
        self._crop_top = 0.5

    def clear_condition(self):
        self._art = QPixmap()
        self.setToolTip("")
        self.update()

    def show_condition(self, image_name: str) -> bool:
        path = os.path.join(os.path.dirname(__file__), "..", "assets", "weather", image_name + ".png")
        art = QPixmap(path)
        if art.isNull():
            return False
        self._art = art
        self._crop_top = weather.CROP_TOP[image_name]
        self.update()
        return True

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), theme_color(QColor("#0c1e18")))
        if not self._art.isNull():
            self._paint_condition(p)
        p.setPen(QPen(theme_color(QColor("#2e9e61")), 2))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()

    def mousePressEvent(self, event):
        # The weather artwork is display only. Keep presses from reaching the
        # window beneath the header; the child menu bar handles its own clicks.
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def _paint_condition(self, p):
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        box = QRectF(self.rect())
        source_height = min(self._art.height(), self._art.width() * box.height() / box.width())
        source_width = source_height * box.width() / box.height()
        left = (self._art.width() - source_width) / 2
        # Keep the horizon or moon visible as a wide window needs a shallower crop.
        top = min(self._art.height() - source_height,
                  self._art.height() * self._crop_top + min(180, self._art.height() - source_height) - source_height)
        p.drawPixmap(box, self._art, QRectF(left, max(0, top), source_width, source_height))
        shade = QLinearGradient(0, 0, self.width(), 0)
        for position, opacity in ((0.0, 215), (0.27, 165), (0.48, 75), (0.72, 155), (1.0, 220)):
            shade.setColorAt(position, QColor(4, 19, 15, opacity))
        p.fillRect(box, QBrush(shade))

class ImageView(QGraphicsView):
    imageDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.item.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self.item)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setBackgroundBrush(QColor(5, 13, 13))
        self.auto_fit = True
        self.fit_mode = "width"
        self.follow_live = True
        self.live_edge = None
        self._fit_source_width = None
        self._fit_viewport_width = None
        self.verticalScrollBar().sliderPressed.connect(self._pause_live_follow)
        self.empty_title = "AWAITING IMAGE DOWNLINK"
        self.empty_hint = "START A PASS OR OPEN A .CADU FILE"
        artwork = os.path.join(os.path.dirname(__file__), "..", "assets", "meteor-placeholder.png")
        self.empty_artwork = QPixmap(artwork)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.item.pixmap().isNull():
            return
        p = QPainter(self.viewport())
        r = self.viewport().rect()
        p.fillRect(r, QColor(5, 17, 13))
        p.setPen(QPen(theme_color(QColor(17, 48, 32)), 1))
        for x in range(0, r.width(), 48):
            p.drawLine(x, 0, x, r.height())
        for y in range(0, r.height(), 48):
            p.drawLine(0, y, r.width(), y)
        if not self.empty_artwork.isNull():
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            limit = QSize(max(1, min(420, int(r.width() * 0.55))),
                          max(1, min(250, int(r.height() * 0.45))))
            size = self.empty_artwork.size().scaled(limit, Qt.KeepAspectRatio)
            top = max(8, (r.height() - size.height()) / 2 - 52)
            target = QRectF((r.width() - size.width()) / 2, top,
                            size.width(), size.height())
            p.drawPixmap(target, self.empty_artwork, QRectF(self.empty_artwork.rect()))
            p.setFont(QFont("Segoe UI", 13, QFont.Bold))
            p.setPen(ACCENT)
            p.drawText(QRectF(0, top + size.height() + 19, r.width(), 28),
                       Qt.AlignCenter, self.empty_title)
            p.setFont(QFont("Consolas", 9))
            p.setPen(theme_color(QColor(111, 166, 128)))
            p.drawText(QRectF(0, top + size.height() + 50, r.width(), 22),
                       Qt.AlignCenter, self.empty_hint)
            p.end()
            return
        cx, cy = r.center().x(), r.center().y() - 18
        p.setPen(QPen(theme_color(QColor(38, 104, 66)), 1))
        p.drawEllipse(cx - 120, cy - 120, 240, 240)
        p.drawEllipse(cx - 74, cy - 74, 148, 148)
        p.drawLine(cx - 150, cy, cx + 150, cy)
        p.drawLine(cx, cy - 150, cx, cy + 150)
        p.setPen(QPen(ACCENT, 2))
        p.drawEllipse(cx - 8, cy - 8, 16, 16)
        p.setFont(QFont("Segoe UI", 13, QFont.Bold))
        p.drawText(QRectF(0, cy + 134, r.width(), 26), Qt.AlignCenter, self.empty_title)
        p.setPen(theme_color(QColor(111, 166, 128)))
        p.setFont(QFont("Consolas", 9))
        p.drawText(QRectF(0, cy + 166, r.width(), 23), Qt.AlignCenter, self.empty_hint)
        p.end()

    def show_array(self, img: np.ndarray, *, follow_edge: str | None = None):
        img = np.ascontiguousarray(img)
        h, w = img.shape[:2]
        if img.ndim == 2:
            q = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
        else:
            q = QImage(img.data, w, h, 3 * w, QImage.Format_RGB888)
        self.show_pixmap(QPixmap.fromImage(q.copy()), follow_edge=follow_edge)

    def show_pixmap(self, pixmap: QPixmap, *, follow_edge: str | None = None):
        self.live_edge = follow_edge
        self.item.setPixmap(pixmap)
        self.scene().setSceneRect(QRectF(pixmap.rect()))
        if self.auto_fit:
            if self.fit_mode == "image":
                self.fit_image()
            elif (self._fit_source_width != pixmap.width() or
                  self._fit_viewport_width != self.viewport().width()):
                self.fit_width(resume_live=False)
        if follow_edge and self.follow_live:
            QTimer.singleShot(0, self._scroll_to_live_edge)

    def begin_live_follow(self):
        self.follow_live = True
        self.auto_fit = True
        self.fit_mode = "width"
        self._fit_source_width = None

    def _pause_live_follow(self):
        if self.live_edge:
            self.follow_live = False

    def _scroll_to_live_edge(self):
        if self.live_edge and self.follow_live:
            bar = self.verticalScrollBar()
            bar.setValue(bar.minimum() if self.live_edge == "top" else bar.maximum())

    def clear_image(self):
        self.live_edge = None
        self.follow_live = True
        self._fit_source_width = None
        self.item.setPixmap(QPixmap())
        self.scene().setSceneRect(QRectF())
        self.viewport().update()

    def fit_width(self, *, resume_live: bool = True):
        r = self.scene().sceneRect()
        if r.width() <= 0:
            return
        self.resetTransform()
        # Leave room for the vertical scrollbar once the growing strip exceeds the pane height.
        available = self.viewport().width() - self.verticalScrollBar().sizeHint().width() - 4
        s = max(1, available) / r.width()
        self.scale(s, s)
        self.auto_fit = True
        self.fit_mode = "width"
        self._fit_source_width = int(r.width())
        self._fit_viewport_width = self.viewport().width()
        if resume_live and self.live_edge:
            self.follow_live = True
            QTimer.singleShot(0, self._scroll_to_live_edge)

    def fit_image(self):
        r = self.scene().sceneRect()
        if r.width() <= 0:
            return
        self.resetTransform()
        self.fitInView(r, Qt.KeepAspectRatio)
        self.auto_fit = True
        self.fit_mode = "image"

    def actual_size(self):
        self.resetTransform()
        self.auto_fit = False

    def wheelEvent(self, e):
        self._pause_live_follow()
        f = 1.25 if e.angleDelta().y() > 0 else 0.8
        self.scale(f, f)
        self.auto_fit = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pause_live_follow()
        super().mousePressEvent(event)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.auto_fit:
            self.fit_image() if self.fit_mode == "image" else self.fit_width(resume_live=False)
        if self.live_edge and self.follow_live:
            QTimer.singleShot(0, self._scroll_to_live_edge)

    @staticmethod
    def _dropped_image_path(event) -> str | None:
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if os.path.splitext(path)[1].lower() in (".png", ".jpg", ".jpeg", ".bmp",
                                                       ".webp", ".tif", ".tiff"):
                    return path
        return None

    def dragEnterEvent(self, event):
        if self._dropped_image_path(event):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._dropped_image_path(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        path = self._dropped_image_path(event)
        if path:
            self.imageDropped.emit(path)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


def read_saved_image(path: str) -> np.ndarray:
    """Decode a saved picture to the RGB array used by the received-image pane."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise ValueError(reader.errorString() or "Could not read image")
    image = image.convertToFormat(QImage.Format_RGB888)
    h, w, stride = image.height(), image.width(), image.bytesPerLine()
    pixels = np.frombuffer(image.constBits(), dtype=np.uint8, count=h * stride)
    return pixels.reshape(h, stride)[:, :w * 3].reshape(h, w, 3).copy()


class FullImageDialog(QDialog):
    """Full-screen view of the same pixmap shown in the received-image pane."""

    def __init__(self, parent, labels, *, allow_save=True):
        super().__init__(parent)
        self.labels = labels
        self.setWindowTitle("METEOR-X - " + labels["full_image"])
        self.setWindowFlag(Qt.Window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        bar = QHBoxLayout()
        self.caption = QLabel()
        bar.addWidget(self.caption)
        bar.addStretch(1)
        self.toolbar = bar
        if allow_save:
            self.save_button = QPushButton(labels["full_save"])
            self.save_button.clicked.connect(self._save_image)
            bar.addWidget(self.save_button)
        fit = QPushButton(labels["full_fit"])
        fit.clicked.connect(lambda: self.view.fit_image())
        bar.addWidget(fit)
        actual = QPushButton(labels["full_actual"])
        actual.clicked.connect(lambda: self.view.actual_size())
        bar.addWidget(actual)
        close = QPushButton(labels["enh_close"])
        close.clicked.connect(self.close)
        bar.addWidget(close)
        layout.addLayout(bar)
        self.view = ImageView(self)
        self.view.fit_mode = "image"
        layout.addWidget(self.view, 1)

    def update_image(self, pixmap: QPixmap, caption: str):
        self.caption.setText(caption)
        self.view.show_pixmap(pixmap)

    def _save_image(self):
        pixmap = self.view.item.pixmap()
        if pixmap.isNull():
            return
        parent = self.parent()
        source = getattr(parent, "_opened_image_path", None)
        if source:
            suggested = os.path.splitext(source)[0] + "_displayed.png"
        else:
            last_pass = getattr(parent.engine, "last_pass", None) or {}
            folder = last_pass.get("folder") or parent.cfg.output_dir
            stem = last_pass.get("prefix") or "METEOR-X"
            suggested = os.path.join(folder, f"{stem}_displayed.png")
        path, _ = QFileDialog.getSaveFileName(
            self, self.labels["full_save"], outputs.unique_path(suggested), "PNG (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        if not pixmap.save(path, "PNG"):
            QMessageBox.warning(self, self.labels["full_save"],
                                self.labels["full_save_failed"].format(path=path))


class CompositeDialog(FullImageDialog):
    """Preview and save optional enhancements of an already mapped daily composite."""

    def __init__(self, parent, labels):
        super().__init__(parent, labels, allow_save=False)
        self.setWindowTitle("METEOR-X - " + labels["composite"])
        self.labels = labels
        self.source_path = None
        self.source_image = None
        self.channel_image = None
        self.source_channels = (1, 2, 4)
        self.bounds = None
        self.min_city_population = 200_000
        self.default_city_scale = parent.cfg.map_text_scale if parent is not None else 1.3
        self.displayed = None
        self.options = {"colour": False, "infrared": False, "coast": False,
                        "cities": True, "mirror": False,
                        "coast_colour": overlay.DEFAULT_COAST,
                        "city_colour": overlay.DEFAULT_CITY,
                        "city_scale": self.default_city_scale}
        self.enhancement_dialog = None
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self._render)
        self.view_selector = QComboBox()
        self.view_selector.setMinimumContentsLength(16)
        self.view_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.view_selector.view().setMinimumWidth(260)
        self.view_selector.addItem("RGB 124")
        self.view_selector.currentIndexChanged.connect(self._view_changed)
        self.toolbar.insertWidget(2, QLabel(labels["view"]))
        self.toolbar.insertWidget(3, self.view_selector)
        enhance_button = QPushButton(labels["enhance"])
        enhance_button.clicked.connect(self._open_enhancements)
        self.toolbar.insertWidget(4, enhance_button)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        save_button = QPushButton(labels["composite_save"])
        save_button.clicked.connect(self._save)
        bottom.addWidget(save_button)
        self.layout().addLayout(bottom)

    def load_composite(self, path: str):
        meta_path = os.path.splitext(path)[0] + ".json"
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        base_name = meta.get("base_image")
        bounds = meta.get("bounds")
        usable_base = (isinstance(base_name, str) and isinstance(bounds, list) and len(bounds) == 9 and
                       os.path.isfile(os.path.join(os.path.dirname(path), base_name)))
        source = os.path.join(os.path.dirname(path), base_name) if usable_base else path
        image = imaging.read_png(source)
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("Composite image is not RGB")
        self.source_path = path
        self.source_image = image
        channel_name = meta.get("channel_image")
        channel_path = (os.path.join(os.path.dirname(path), channel_name)
                        if isinstance(channel_name, str) else None)
        self.channel_image = (imaging.read_png(channel_path) if channel_path and
                              os.path.isfile(channel_path) else None)
        if self.channel_image is not None and self.channel_image.shape != image.shape:
            self.channel_image = None
        fallback_channels = ([4] if path.lower().endswith("_night_ir.png") else
                             [1, 2, 3] if path.lower().endswith("_rgb123.png") else [1, 2, 4])
        received = meta.get("channels", fallback_channels)
        self.source_channels = (tuple(received) if received in ([1, 2, 3], [1, 2, 4], [4])
                                else (1, 2, 4))
        self.bounds = tuple(bounds) if usable_base else None
        from . import mosaic
        self.view_selector.blockSignals(True)
        self.view_selector.clear()
        available = mosaic.composite_views(
            self.source_channels, self.channel_image is not None, self.bounds is not None)
        self.view_selector.addItems(mosaic.ALL_COMPOSITE_VIEWS)
        for index, name in enumerate(mosaic.ALL_COMPOSITE_VIEWS):
            if name not in available:
                item = self.view_selector.model().item(index)
                item.setEnabled(False)
                item.setToolTip("Unavailable in this composite; its mapped source channels or location data are missing")
        preferred = ("Night IR" if self.source_channels == (4,) else
                     "MSU-MR 124 False Color" if self.source_channels == (1, 2, 4)
                     else "AVHRR 3a21 False Color")
        preferred_index = self.view_selector.findText(preferred)
        self.view_selector.setCurrentIndex(preferred_index if preferred in available else
                                           self.view_selector.findText(available[0]))
        self.view_selector.blockSignals(False)
        self.min_city_population = int(meta.get("city_min_population", 200_000))
        self.caption.setText(os.path.basename(path))
        self.options.update({"colour": False, "infrared": False, "coast": False,
                             "cities": self.bounds is not None, "mirror": False,
                             "coast_colour": overlay.DEFAULT_COAST,
                             "city_colour": overlay.DEFAULT_CITY,
                             "city_scale": self.default_city_scale})
        if self.enhancement_dialog is not None:
            self.enhancement_dialog.close()
            self.enhancement_dialog = None
        self._render()

    def _set_option(self, key, value):
        self.options[key] = value
        self.render_timer.start(100)

    def _view_changed(self, _=None):
        uses_ir = self.view_selector.currentText() not in (
            "RGB 221", "AVHRR 221 False Color", "RGB 123", "RGB 123 (raw)",
            "AVHRR 3a21 False Color", "MSA", "Night IR",
            "Channel 1", "Channel 2", "Channel 3")
        if self.enhancement_dialog is not None and self.enhancement_dialog.isVisible():
            self.ir_checkbox.setEnabled(uses_ir)
            if not uses_ir:
                self.ir_checkbox.setChecked(False)
        elif not uses_ir:
            self.options["infrared"] = False
        self.render_timer.start(100)

    def _render(self):
        if self.source_image is None:
            return
        from . import mosaic
        selected = self.view_selector.currentText()
        source = mosaic.composite_view(self.source_image, self.channel_image, selected,
                                       self.source_channels, self.bounds)
        if self.bounds is not None:
            image = mosaic.render_composite(
                source, self.bounds, self.min_city_population,
                colour_enhancement=self.options["colour"],
                infrared_enhancement=self.options["infrared"],
                infrared_grey=selected in ("Channel 4", "3.9 µm Shortwave IR (Uncalibrated)"),
                coastlines=self.options["coast"], cities=self.options["cities"],
                coast_colour=self.options["coast_colour"],
                city_colour=self.options["city_colour"],
                city_scale=self.options["city_scale"], mirror=self.options["mirror"])
        else:
            image = source
            if self.options["colour"]:
                image = enhance.colour(image)
            if self.options["infrared"]:
                image[:, :, 2] = enhance.infrared(image[:, :, 2])
            if self.options["mirror"]:
                image = image[:, ::-1].copy()
        self.displayed = np.ascontiguousarray(image)
        self.view.show_array(self.displayed)

    def _open_enhancements(self):
        if self.enhancement_dialog is not None and self.enhancement_dialog.isVisible():
            self.enhancement_dialog.raise_()
            return
        t = self.labels
        dialog = QDialog(self)
        dialog.setWindowTitle("METEOR-X - " + t["enh_title"])
        layout = QVBoxLayout(dialog)
        for key, label in (("colour", t["enh_colour"]), ("infrared", t["enh_ir"]),
                           ("coast", t["enh_coast"]), ("cities", t["enh_cities"]),
                           ("mirror", t["mirror"])):
            check = QCheckBox(label)
            check.setChecked(self.options[key])
            if key == "infrared":
                self.ir_checkbox = check
                check.setEnabled(self.view_selector.currentText() not in (
                    "RGB 221", "AVHRR 221 False Color", "RGB 123", "RGB 123 (raw)",
                    "AVHRR 3a21 False Color", "MSA", "Night IR",
                    "Channel 1", "Channel 2", "Channel 3"))
            if self.bounds is None and key in ("coast", "cities"):
                check.setEnabled(False)
            check.toggled.connect(lambda on, k=key: self._set_option(k, bool(on)))
            layout.addWidget(check)
        for key, label in (("coast_colour", t["enh_coast_col"]),
                           ("city_colour", t["enh_city_col"])):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            choices = QComboBox()
            for name, rgb in overlay.PALETTE.items():
                choices.addItem(t.get(f"col_{name}", name.title()), name)
                choices.setItemData(choices.count() - 1, QColor(*rgb), Qt.DecorationRole)
            choices.setCurrentIndex(max(0, choices.findData(self.options[key])))
            choices.setEnabled(self.bounds is not None)
            choices.currentIndexChanged.connect(
                lambda _, widget=choices, k=key: self._set_option(k, widget.currentData()))
            row.addWidget(choices, 1)
            layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel(t["composite_city_size"]))
        size = QDoubleSpinBox()
        size.setRange(0.8, 5.0)
        size.setSingleStep(0.1)
        size.setDecimals(1)
        size.setValue(self.options["city_scale"])
        size.setEnabled(self.bounds is not None)
        size.valueChanged.connect(lambda value: self._set_option("city_scale", value))
        row.addWidget(size)
        layout.addLayout(row)
        note = QLabel(t["composite_enh_note"])
        note.setWordWrap(True)
        layout.addWidget(note)
        credit = QLabel(t["enh_credit"])
        credit.setWordWrap(True)
        layout.addWidget(credit)
        close = QPushButton(t["enh_close"])
        close.clicked.connect(dialog.close)
        layout.addWidget(close)
        self.enhancement_dialog = dialog
        dialog.show()

    def _save(self):
        if self.displayed is None or self.source_path is None:
            return
        stem, _ = os.path.splitext(self.source_path)
        view_slug = re.sub(r"[^a-z0-9]+", "_", self.view_selector.currentText().lower()).strip("_")
        suggested = outputs.unique_path(f"{stem}_{view_slug}_enhanced.png")
        path, _ = QFileDialog.getSaveFileName(
            self, self.labels["composite_save"], suggested, "PNG (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        # Edits are separate exports. In particular, never replace the source
        # mosaic (which an automatic rebuild may also be writing).
        path = outputs.unique_path(path)
        try:
            imaging.write_png(path, self.displayed)
        except OSError as exc:
            QMessageBox.warning(self, "METEOR-X", f"{path}\n{exc}")
            return
        self.caption.setText(f"{os.path.basename(self.source_path)}  ·  {self.labels['composite_saved']}: {path}")


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, cfg: config.Config):
        super().__init__()
        self.cfg = cfg
        self.lang = i18n.normalize(cfg.get("gui", "language", "fr"))
        self.t = T[self.lang]
        self.setWindowTitle(f"METEOR-X {__version__} · Build {__build__} · {cfg.get('station', 'callsign', '')}")
        self._fit_to_screen(1440, 900)
        self.logq: queue.Queue = queue.Queue()
        self.engine = Engine(cfg, log_cb=self.logq.put)
        self._cleared_imager = None
        self._opened_image_path = None
        self._pending_cadu_view = False
        self._auto_live_view = None
        self._live_view_imager = None
        self._preview_rev = -1
        self._auto_enhanced_key = None
        self._last_view_update = 0
        self._build()
        self._latest_passes: list[orbit.Pass] = []
        self._selected_pass: orbit.Pass | None = None
        self._pass_preview_timer = QTimer(self)
        self._pass_preview_timer.setSingleShot(True)
        self._pass_preview_timer.timeout.connect(self._show_next_pass)
        self._weather_manager = QNetworkAccessManager(self)
        self._weather_request = None
        self._weather_last_ok = 0.0
        self._weather_failures = 0
        self._weather_timer = QTimer(self)
        self._weather_timer.timeout.connect(self._refresh_weather)
        self._weather_timer.start(weather.REFRESH_MS)
        self._weather_retry = QTimer(self)
        self._weather_retry.setSingleShot(True)
        self._weather_retry.timeout.connect(self._refresh_weather)
        QTimer.singleShot(0, self._refresh_weather)
        self._connected: set = set()
        self._profiles_detect(startup=True)
        QShortcut(QKeySequence(Qt.Key_F11), self, self._toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self._esc)
        self.engine.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)
        self._tickn = 0

    # -- window placement -------------------------------------------------------
    def _fit_to_screen(self, w: int, h: int):
        """Never larger than the usable screen (laptop at 125-150 % scaling = ~1280x720 logical)."""
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            a = scr.availableGeometry()
            w, h = min(w, a.width() - 20), min(h, a.height() - 40)
            self.setGeometry(a.x() + (a.width() - w) // 2, a.y() + (a.height() - h) // 2, w, h)
        else:
            self.resize(w, h)

    def _state_path(self) -> str:
        return os.path.join(self.cfg.data_dir, "window.json")

    def show_initial(self):
        """[gui] window = maximized (default) | fullscreen | last."""
        mode = (self.cfg.get("gui", "window", "maximized") or "maximized").strip().lower()
        st = {}
        try:
            with open(self._state_path(), "r", encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            pass
        try:
            floor = int(st.get("spectrum_floor_db", 0))
        except (TypeError, ValueError):
            floor = 0
        self.sl_spec_floor.blockSignals(True)
        self.sl_spec_floor.setValue(floor)
        self.sl_spec_floor.blockSignals(False)
        self._spectrum_floor_changed(self.sl_spec_floor.value(), save=False)
        self.chk_mirror.setChecked(bool(st.get("mirror", False)))
        if isinstance(st.get("enh"), dict):
            for k, v in st["enh"].items():
                if k not in self.enh:
                    continue                                  # a key from a newer version: ignore it
                if k.endswith("_colour"):                     # a palette key, not a checkbox
                    self.enh[k] = v if v in overlay.PALETTE else self.enh[k]
                else:
                    self.enh[k] = bool(v)
        for k, sp in (("splitter", self.split), ("rsplitter", self.rsplit),
                      ("view_splitter", self.view_split)):
            if st.get(k):
                try:
                    sp.restoreState(QByteArray.fromHex(st[k].encode()))
                except Exception:  # noqa: BLE001
                    pass
        if mode == "last" and st.get("geometry"):
            self.restoreGeometry(QByteArray.fromHex(st["geometry"].encode()))
            if not any(sc.availableGeometry().intersects(self.frameGeometry()) for sc in QGuiApplication.screens()):
                self._fit_to_screen(1440, 900)       # screen removed since last time
            if st.get("fullscreen"):
                self.showFullScreen()
            elif st.get("maximized", True):
                self.showMaximized()
            else:
                self.show()
        elif mode == "fullscreen":
            self.showFullScreen()
        else:
            self.showMaximized()

    def _save_window_state(self):
        try:
            os.makedirs(self.cfg.data_dir, exist_ok=True)
            st = {"geometry": bytes(self.saveGeometry().toHex()).decode(),
                  "splitter": bytes(self.split.saveState().toHex()).decode(),
                  "rsplitter": bytes(self.rsplit.saveState().toHex()).decode(),
                  "view_splitter": bytes(self.view_split.saveState().toHex()).decode(),
                  "maximized": self.isMaximized(), "fullscreen": self.isFullScreen(),
                  "mirror": self.chk_mirror.isChecked(), "enh": self.enh,
                  "spectrum_floor_db": self.sl_spec_floor.value()}
            tmp = self._state_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f)
            os.replace(tmp, self._state_path())
        except OSError:
            pass

    def _spectrum_floor_changed(self, value: int, save: bool = True):
        self.spec.set_floor_offset(value)
        self.spec_floor_value.setText(f"{value:+d} dB" if value else "0 dB")
        if save:
            self._save_window_state()

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showMaximized()
        else:
            self.showFullScreen()

    def _esc(self):
        if self.isFullScreen():
            self.showMaximized()

    def _refresh_weather(self):
        if self._weather_request is not None:
            return
        self._weather_retry.stop()
        try:
            url = weather.request_url(self.cfg.getf("station", "latitude", float("nan")),
                                      self.cfg.getf("station", "longitude", float("nan")))
        except ValueError as exc:
            self.weather_header.clear_condition()
            self.engine.log(f"Weather image: {exc}")
            return
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(12_000)
        request.setRawHeader(b"User-Agent", b"METEOR-X/3.2.4")
        reply = self._weather_manager.get(request)
        self._weather_request = reply
        reply.finished.connect(lambda r=reply: self._weather_finished(r))

    def _weather_finished(self, reply):
        if reply is not self._weather_request:
            reply.deleteLater()
            return
        self._weather_request = None
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                raise ValueError(reply.errorString())
            conditions = weather.parse_current(json.loads(bytes(reply.readAll())))
            if not self.weather_header.show_condition(conditions.image):
                raise ValueError(f"Missing weather image: {conditions.image}")
            self.weather_header.setToolTip(
                f"Station weather at {conditions.valid_time or 'current time'} · "
                "Weather data by Open-Meteo.com")
            self._weather_last_ok = time.monotonic()
            self._weather_failures = 0
        except (ValueError, TypeError, KeyError) as exc:
            self._weather_failures += 1
            self.engine.log(f"Weather image unavailable: {exc}")
            if self._weather_last_ok and time.monotonic() - self._weather_last_ok > 3600:
                self.weather_header.clear_condition()
            delay = min(weather.REFRESH_MS, 60_000 * 2 ** min(self._weather_failures - 1, 4))
            self._weather_retry.start(delay)
        finally:
            reply.deleteLater()

    # -- construction -----------------------------------------------------------
    def _build(self):
        t = self.t
        shell = QWidget(self)
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        self.setCentralWidget(shell)

        header = WeatherHeader()
        header.setObjectName("brandHeader")
        self.weather_header = header
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)
        self.header_menu = QMenuBar(header)
        header_layout.addWidget(self.header_menu)
        brand_row = QWidget(header)
        brand_row.setMinimumHeight(92)
        hh = QHBoxLayout(brand_row)
        hh.setContentsMargins(18, 8, 18, 8)
        hh.setSpacing(14)
        mark = QLabel()
        mark.setObjectName("brandMark")
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "meteor-x.png")
        if os.path.isfile(icon_path):
            mark.setPixmap(QPixmap(icon_path).scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        mark.setFixedSize(46, 46)
        hh.addWidget(mark)
        name_col = QVBoxLayout()
        name_col.setSpacing(0)
        name = QLabel("METEOR-X")
        name.setObjectName("brandName")
        name_col.addWidget(name)
        mission = QLabel("LRPT  /  SATELLITE GROUND STATION")
        mission.setObjectName("brandMission")
        name_col.addWidget(mission)
        hh.addLayout(name_col)
        hh.addStretch(1)
        station_col = QVBoxLayout()
        station_col.setContentsMargins(0, 0, 0, 0)
        station_col.setSpacing(4)
        station_col.addStretch(1)
        station = QLabel((self.cfg.get("station", "callsign", "") or "GROUND STATION").upper())
        station.setObjectName("stationBadge")
        station.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        station_col.addWidget(station, 0, Qt.AlignRight)
        version = QLabel(f"V {__version__}  /  BUILD {__build__}")
        version.setObjectName("buildBadge")
        version.setAlignment(Qt.AlignCenter)
        version.setFixedHeight(25)
        station_col.addWidget(version, 0, Qt.AlignRight)
        station_col.addStretch(1)
        hh.addLayout(station_col)
        self.header_state = QLabel("STANDBY")
        self.header_state.setObjectName("headerState")
        self.header_state.setAlignment(Qt.AlignCenter)
        self.header_state.setMinimumWidth(120)
        hh.addWidget(self.header_state)
        header_layout.addWidget(brand_row)
        shell_layout.addWidget(header)

        split = QSplitter(Qt.Horizontal, self)
        self.split = split
        shell_layout.addWidget(split, 1)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 8, 8, 8)
        lv.setSpacing(8)

        # --- receiver
        g = QGroupBox(t["source"])
        fl = QFormLayout(g)
        ph = QHBoxLayout()
        ph.setContentsMargins(0, 0, 0, 0)
        self.prof_cb = QComboBox()
        self.prof_cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.prof_cb.setMinimumContentsLength(14)
        self.btn_detect = QPushButton(t["detect"])
        self.btn_detect.clicked.connect(lambda: self._profiles_detect(startup=False))
        pm = QToolButton()
        pm.setText("⋯")
        pm.setPopupMode(QToolButton.InstantPopup)
        mn = QMenu(pm)
        mn.addAction(t["rename"], self._profile_rename)
        mn.addAction(t["delete"], self._profile_delete)
        pm.setMenu(mn)
        self.profile_menu_button = pm
        ph.addWidget(self.prof_cb, 1)
        ph.addWidget(self.btn_detect)
        ph.addWidget(pm)
        prow = QWidget()
        prow.setLayout(ph)
        fl.addRow(t["profile"], prow)
        self.prof_lbl = QLabel("")
        self.prof_lbl.setWordWrap(True)
        self.prof_lbl.setStyleSheet("color:#9fb3a8;font-size:11px")
        fl.addRow(self.prof_lbl)
        self.dev = QComboBox()
        for key, label in zip(DEV_KEYS, t["devices"]):
            self.dev.addItem(label, key)
        cur = (self.cfg.get("sdr", "device", "rtlsdr") or "rtlsdr").lower()
        self.dev.setCurrentIndex(max(self.dev.findData(cur), 0))
        fl.addRow(t["device"], self.dev)
        self.rtl_gain = GainSlider(0, 50, 0.1)
        try:
            self.rtl_gain.setValue(float(self.cfg.get("sdr", "rtl_gain_db", "38.6")))
        except ValueError:
            self.rtl_gain.setValue(38.6)
        self.lbl_rtl_gain = QLabel(t["gain"])
        fl.addRow(self.lbl_rtl_gain, self.rtl_gain)
        self.sp_grdb = GainSlider(20, 59)
        self.sp_grdb.setValue(self.cfg.geti("sdr", "sdrplay_grdb", 40))
        self.sp_lna = GainSlider(0, 9, unit="")
        self.sp_lna.setValue(self.cfg.geti("sdr", "sdrplay_lna_state", 3))
        self.lbl_grdb, self.lbl_lna = QLabel(t["grdb"]), QLabel(t["lna"])
        fl.addRow(self.lbl_grdb, self.sp_grdb)
        fl.addRow(self.lbl_lna, self.sp_lna)
        self.cb_ant = QComboBox()
        self.cb_ant.addItems(["A", "B", "C"])
        self.cb_ant.setCurrentText((self.cfg.get("sdr", "sdrplay_antenna", "A") or "A").upper()[:1])
        self.lbl_ant = QLabel(t["ant"])
        fl.addRow(self.lbl_ant, self.cb_ant)
        self.sp_hlna, self.sp_hvga = GainSlider(0, 40, 8), GainSlider(0, 62, 2)
        self.sp_hlna.setValue(self.cfg.geti("sdr", "hackrf_lna_gain", 24))
        self.sp_hvga.setValue(self.cfg.geti("sdr", "hackrf_vga_gain", 30))
        self.lbl_hlna, self.lbl_hvga = QLabel(t["hlna"]), QLabel(t["hvga"])
        fl.addRow(self.lbl_hlna, self.sp_hlna)
        fl.addRow(self.lbl_hvga, self.sp_hvga)
        self.chk_hamp = QCheckBox(t["hamp"])
        self.chk_hamp.setChecked(self.cfg.getb("sdr", "hackrf_amp", False))
        fl.addRow("", self.chk_hamp)
        self.sp_lgain = GainSlider(0, 73)
        self.sp_lgain.setValue(self.cfg.geti("sdr", "lime_gain_db", 40))
        self.lbl_lgain = QLabel(t["lgain"])
        fl.addRow(self.lbl_lgain, self.sp_lgain)
        self.cb_lant = QComboBox()
        self.cb_lant.addItems(LIME_ANT)
        self._set_lant(self.cfg.get("sdr", "lime_antenna", "auto"))
        self.lbl_lant = QLabel(t["lant"])
        fl.addRow(self.lbl_lant, self.cb_lant)
        self.ed_puri = QLineEdit(self.cfg.get("sdr", "pluto_uri", "ip:192.168.2.1") or "ip:192.168.2.1")
        self.lbl_puri = QLabel(t["puri"])
        fl.addRow(self.lbl_puri, self.ed_puri)
        self.sp_pgain = GainSlider(0, 71)
        try:
            self.sp_pgain.setValue(int(float(self.cfg.get("sdr", "pluto_gain_db", "50"))))
        except ValueError:
            self.sp_pgain.setValue(50)
        self.lbl_pgain = QLabel(t["pgain"])
        fl.addRow(self.lbl_pgain, self.sp_pgain)
        self.bias = QCheckBox(t["bias"])
        fl.addRow("", self.bias)
        self.dc_remove = QCheckBox(t["dc_remove"])
        self.dc_remove.setChecked(self.cfg.getb("decoder", "dc_spike_removal", False))
        self.dc_remove.setToolTip(t["dc_remove_tip"])
        self.dc_remove.toggled.connect(self._dc_remove_toggled)
        fl.addRow("", self.dc_remove)
        self.chk_preamp = QCheckBox("")
        self.chk_preamp.setVisible(False)
        self.chk_preamp.toggled.connect(self._preamp_toggled)
        fl.addRow(self.chk_preamp)
        self.dev.currentIndexChanged.connect(self._dev_changed)
        self.dev.activated.connect(self._dev_picked)
        self._gain_timer = QTimer(self)
        self._gain_timer.setSingleShot(True)
        self._gain_timer.timeout.connect(self._apply_live_gain)
        self._gain_save_timer = QTimer(self)
        self._gain_save_timer.setSingleShot(True)
        self._gain_save_timer.timeout.connect(self._save_gain_settings)
        for w in (self.rtl_gain, self.sp_grdb, self.sp_lna, self.sp_hlna, self.sp_hvga, self.sp_lgain, self.sp_pgain):
            w.valueChanged.connect(self._gain_changed)
            w.slider.sliderReleased.connect(self._apply_live_gain)
        self.prof_cb.activated.connect(self._profile_picked)
        lv.addWidget(g)

        # --- satellite / control
        g = QGroupBox(t["sat"])
        gl = QGridLayout(g)
        self.sat = QComboBox()
        for s in self.cfg.satellites:
            self.sat.addItem(s.name)
        self.sat.addItem(t["manual"])
        self.mode = QComboBox()
        self.mode.addItems(["auto", "72k", "80k"])
        self.mode.setCurrentText(self.cfg.get("decoder", "mode", "auto"))
        gl.addWidget(self.sat, 0, 0)
        gl.addWidget(QLabel(t["mode"]), 0, 1)
        gl.addWidget(self.mode, 0, 2)
        self.btn_start = QPushButton("▶  " + t["start"])
        self.btn_stop = QPushButton("■  " + t["stop"])
        self.btn_start.setMinimumHeight(30)
        self.btn_stop.setMinimumHeight(30)
        self.btn_start.setObjectName("startButton")
        self.btn_stop.setObjectName("stopButton")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(lambda: self.engine.command("stop"))
        gl.addWidget(self.btn_start, 1, 0)
        gl.addWidget(self.btn_stop, 1, 1, 1, 2)
        self.chk_auto = QCheckBox(t["auto"])
        self.chk_auto.setChecked(self.engine.auto)
        self.chk_auto.toggled.connect(lambda on: self.engine.command("auto", enabled=on))
        gl.addWidget(self.chk_auto, 2, 0, 1, 3)
        self.state_lbl = QLabel("")
        self.state_lbl.setStyleSheet("font-size:14px;font-weight:700;padding:4px")
        gl.addWidget(self.state_lbl, 3, 0, 1, 3)
        lv.addWidget(g)

        # --- signal validation
        g = QGroupBox(t["signal"])
        sv = QVBoxLayout(g)
        leds = QHBoxLayout()
        self.led_sig, self.led_vit = Led(t["led_sig"]), Led(t["led_vit"])
        self.led_frm, self.led_img, self.led_rec = Led(t["led_frm"]), Led(t["led_img"]), Led("● " + t["rec"])
        for w in (self.led_sig, self.led_vit, self.led_frm, self.led_img, self.led_rec):
            leds.addWidget(w)
        sv.addLayout(leds)
        self.smeter = SMeterWidget(t["uncal"])
        sv.addWidget(self.smeter)
        ah = QHBoxLayout()
        self.btn_audio = QPushButton(t["muted"])
        self.btn_audio.setCheckable(True)
        self.btn_audio.setToolTip(t["listen_tip"])
        self.btn_audio.toggled.connect(self._audio_toggled)
        self.sl_vol = QSlider(Qt.Horizontal)
        self.sl_vol.setRange(0, 100)
        self.sl_vol.setValue(self.cfg.geti("gui", "audio_volume", 50))
        self.sl_vol.setToolTip(t["volume"])
        self.sl_vol.valueChanged.connect(lambda v: self.engine.audio.set(self.btn_audio.isChecked(), v / 100.0))
        ah.addWidget(self.btn_audio)
        ah.addWidget(QLabel(t["volume"]))
        ah.addWidget(self.sl_vol, 1)
        sv.addLayout(ah)
        scopes = QWidget()
        scopes_layout = QVBoxLayout(scopes)
        scopes_layout.setContentsMargins(4, 8, 4, 4)
        scope_header = QLabel(f"{t['signal_scopes'].upper()}   /   LIVE LRPT TELEMETRY")
        scope_header.setObjectName("scopeHeader")
        scopes_layout.addWidget(scope_header)
        row = QHBoxLayout()
        self.eye = EyeWidget()
        self.eye.title = t["eye"]
        self.eye.setMinimumHeight(170)
        row.addWidget(self.eye, 1)
        self.const = ConstWidget()
        self.const.title = t["const"]
        self.const.setMinimumHeight(170)
        self.const.setFixedWidth(130)
        row.addWidget(self.const)
        scopes_layout.addLayout(row, 1)
        spec_row = QHBoxLayout()
        spec_row.setSpacing(2)
        self.spec = SpectrumWidget()
        self.spec.setMinimumHeight(100)
        spec_row.addWidget(self.spec, 1)
        floor_controls = QVBoxLayout()
        floor_controls.setSpacing(0)
        self.spec_floor_label = QLabel(t["spec_floor"])
        self.spec_floor_label.setAlignment(Qt.AlignHCenter)
        self.spec_floor_label.setStyleSheet(
            f"font: 700 9px Consolas; color:{theme_color('#5ef7da').name()}")
        floor_controls.addWidget(self.spec_floor_label)
        self.sl_spec_floor = QSlider(Qt.Vertical)
        self.sl_spec_floor.setRange(-25, 25)
        self.sl_spec_floor.setValue(0)
        self.sl_spec_floor.setToolTip(t["spec_floor_tip"])
        floor_controls.addWidget(self.sl_spec_floor, 1, Qt.AlignHCenter)
        self.spec_floor_value = QLabel("0 dB")
        self.spec_floor_value.setAlignment(Qt.AlignHCenter)
        self.spec_floor_value.setStyleSheet("font: 9px Consolas; color:#a9c8bc")
        floor_controls.addWidget(self.spec_floor_value)
        spec_row.addLayout(floor_controls)
        self.sl_spec_floor.valueChanged.connect(self._spectrum_floor_changed)
        scopes_layout.addLayout(spec_row, 1)
        sv.addWidget(scopes, 1)
        lv.addWidget(g, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left)
        scroll.setMinimumWidth(500)
        scroll.setFrameShape(QFrame.NoFrame)
        split.addWidget(scroll)

        # --- right: result (top) + passes and log (bottom, always visible)
        self.rsplit = QSplitter(Qt.Vertical)
        right = QWidget()

        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 8, 8, 8)
        bar = QHBoxLayout()
        title = QLabel(t["result"])
        title.setObjectName("workspaceTitle")
        bar.addWidget(title)
        self.btn_clear_image = QPushButton(t["clear_image"])
        self.btn_clear_image.setObjectName("clearImageButton")
        self.btn_clear_image.setToolTip(t["clear_image_tip"])
        self.btn_clear_image.clicked.connect(self._clear_received_image)
        bar.addWidget(self.btn_clear_image)
        bar.addStretch(1)
        self.btn_full_image = QPushButton(t["full_image"])
        self.btn_full_image.setToolTip(t["full_image_tip"])
        self.btn_full_image.setEnabled(False)
        self.btn_full_image.clicked.connect(self._open_full_image)
        bar.addWidget(self.btn_full_image)
        bar.addWidget(QLabel(t["view"]))
        self.view_sel = QComboBox()
        self.view_sel.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.view_sel.setMinimumContentsLength(16)
        self.view_sel.addItems(["RGB 221", "RGB 123", "RGB 124"] + list(view_products.PRODUCTS) +
                               [f"{t['ch']} {i}" for i in range(1, 7)])
        precip_index = self.view_sel.findText(view_products.PRECIPITATION)
        self.view_sel.setItemData(precip_index,
                                  "Qualitative infrared cloud proxy; colours do not measure rainfall.",
                                  Qt.ToolTipRole)
        self.view_sel.view().setMinimumWidth(230)
        self.view_sel.currentIndexChanged.connect(lambda _: self._refresh_view(force=True))
        bar.addWidget(self.view_sel)
        self.enh = {"colour": False, "infrared": False, "fill": True,
                    "texture_fill": self.cfg.getb("recording", "fill_texture", True),
                    "resize": False, "coast": False, "cities": False,
                    "coast_colour": overlay.DEFAULT_COAST, "city_colour": overlay.DEFAULT_CITY}
        self.enh_dlg = None
        self.full_image_dlg = None
        self.mosaic_dlg = None
        self._track_cache = (None, None)    # (imager id, (track, scan angle)) - SGP4 once per pass
        self._product_track_cache = {}      # one geolocation track per MCIR/MSA source channel
        self._geo_warned = False
        self.btn_enhance = QPushButton(t["enhance"])
        self.btn_enhance.clicked.connect(self._open_enhance)
        bar.addWidget(self.btn_enhance)
        self.chk_mirror = QPushButton(t["mirror"])
        self.chk_mirror.setCheckable(True)
        self.chk_mirror.setToolTip(t["mirror_tip"])
        self.chk_mirror.toggled.connect(lambda _: self._refresh_view(force=True))
        bar.addWidget(self.chk_mirror)
        b = QPushButton(t["fit"])
        b.clicked.connect(lambda: self.view.fit_width())
        bar.addWidget(b)
        b = QPushButton(t["cadu"])
        b.clicked.connect(self._open_cadu)
        bar.addWidget(b)
        b = QPushButton(t["open_image"])
        b.clicked.connect(self._open_saved_image)
        bar.addWidget(b)
        mosaics = QPushButton(t["composite"])
        mosaic_menu = QMenu(mosaics)
        mosaic_menu.addAction(t["mosaic_view"], self._view_latest_mosaic)
        mosaic_menu.addAction(t["mosaic_view_night"],
                              lambda checked=False: self._view_latest_mosaic("night"))
        mosaic_menu.addAction(t["mosaic_open"], self._open_mosaics)
        mosaic_menu.addAction(t["mosaic_rebuild"], self._rebuild_mosaic)
        mosaics.setMenu(mosaic_menu)
        bar.addWidget(mosaics)
        rv.addLayout(bar)
        self.view_split = QSplitter(Qt.Horizontal)
        self.view = ImageView()
        self.view.empty_title = t["view_empty_title"]
        self.view.empty_hint = t["view_empty_hint"]
        self.view.setAcceptDrops(True)
        self.view.viewport().setAcceptDrops(True)
        self.view.imageDropped.connect(self._show_saved_image)
        self.view_split.addWidget(self.view)
        tracker_side = QWidget()
        tracker_side.setMinimumWidth(280)
        tracker_side.setMaximumWidth(630)
        tracker_column = QVBoxLayout(tracker_side)
        tracker_column.setContentsMargins(0, 0, 0, 0)
        self.tracker = tracker.SatelliteTracker(self.cfg)
        tracker_column.addWidget(self.tracker, 1)
        self.view_split.addWidget(tracker_side)
        self.view_split.setChildrenCollapsible(False)
        self.view_split.setSizes([820, 600])
        rv.addWidget(self.view_split, 1)
        self.img_info = QLabel(t["no_image"])
        rv.addWidget(self.img_info)
        self.rsplit.addWidget(right)
        bottom = QWidget()
        bh = QHBoxLayout(bottom)
        bh.setContentsMargins(6, 0, 8, 4)

        # --- passes
        g = QGroupBox(t["passes"])
        pv = QVBoxLayout(g)
        pv.setContentsMargins(4, 4, 4, 4)
        self.next_lbl = QLabel("")
        pv.addWidget(self.next_lbl)
        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels([t["col_sat"], t["col_aos"], t["col_el"], t["col_dir"], t["col_dur"]])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.verticalHeader().setDefaultSectionSize(20)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl.cellClicked.connect(self._preview_pass_row)
        self.tbl.setMinimumHeight(60)
        pv.addWidget(self.tbl, 1)
        bh.addWidget(g, 1)

        # --- log
        g = QGroupBox(t["log"])
        gv = QVBoxLayout(g)
        gv.setContentsMargins(4, 4, 4, 4)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(500)
        self.logbox.setFont(QFont("Consolas", 8))
        gv.addWidget(self.logbox)
        bh.addWidget(g, 1)
        self.rsplit.addWidget(bottom)
        self.rsplit.setStretchFactor(0, 1)
        self.rsplit.setStretchFactor(1, 0)
        self.rsplit.setCollapsible(0, False)
        self.rsplit.setCollapsible(1, False)
        self.rsplit.setSizes([700, 190])
        split.addWidget(self.rsplit)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([510, 980])

        self.status = self.statusBar()
        self._dev_changed()

        m = self.header_menu.addMenu("TLE")
        act = QAction(t["reload_tle"], self)
        act.triggered.connect(lambda: self.engine.command("reload_tle"))
        m.addAction(act)
        act = QAction(t["import_tle"], self)
        act.triggered.connect(self._import_tle)
        m.addAction(act)
        m = self.header_menu.addMenu(t["drivers"])
        act = QAction(t["drv_diag"], self)
        act.triggered.connect(self._driver_diag)
        m.addAction(act)
        act = QAction(t["drv_zadig"], self)
        act.triggered.connect(self._zadig)
        m.addAction(act)
        m.addSeparator()
        mm = self.header_menu.addMenu(t["measures"])
        a2 = QAction(t["calib"], self)
        a2.triggered.connect(self._calibration)
        mm.addAction(a2)
        colours = self.header_menu.addMenu(t["theme_menu"])
        colours.addAction(t["theme_pick"], self._choose_interface_colour)
        colours.addAction(t["theme_default"], lambda: self._set_interface_colour(DEFAULT_ACCENT))
        act = QAction(t["open_log"], self)
        act.triggered.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.path.join(self.cfg.data_dir, "meteorrx.log"))))
        m.addAction(act)

    # -- UI handlers --------------------------------------------------------------
    def _choose_interface_colour(self):
        colour = QColorDialog.getColor(ACCENT, self, self.t["theme_pick"])
        if colour.isValid():
            self._set_interface_colour(colour)

    def _set_interface_colour(self, colour: QColor):
        self.cfg.set("gui", "accent_color", colour.name())
        try:
            self.cfg.save()
        except OSError as exc:
            QMessageBox.warning(self, "METEOR-X", str(exc))
        apply_dark(QApplication.instance(), colour, self.cfg.geti("gui", "callsign_font_size", 10))
        self.spec_floor_label.setStyleSheet(
            f"font: 700 9px Consolas; color:{theme_color('#5ef7da').name()}")
        self.header_state.setProperty("receiverState", None)

    def _device_kind(self) -> str:
        return self.dev.currentData() or ""

    def _dev_changed(self):
        k = self._device_kind()
        for w in (self.rtl_gain, self.lbl_rtl_gain):
            w.setVisible(k == "rtlsdr")
        for w in (self.sp_grdb, self.sp_lna, self.lbl_grdb, self.lbl_lna, self.cb_ant, self.lbl_ant):
            w.setVisible(k == "sdrplay")
        for w in (self.sp_hlna, self.sp_hvga, self.lbl_hlna, self.lbl_hvga, self.chk_hamp):
            w.setVisible(k == "hackrf")
        for w in (self.sp_lgain, self.lbl_lgain, self.cb_lant, self.lbl_lant):
            w.setVisible(k == "lime")
        for w in (self.ed_puri, self.lbl_puri, self.sp_pgain, self.lbl_pgain):
            w.setVisible(k == "pluto")
        self.bias.setVisible(k in BIAS_KEYS)
        if k in BIAS_KEYS:
            self.bias.setChecked(self.cfg.getb("sdr", BIAS_KEYS[k], False))
        if hasattr(self, "_connected"):
            self._profile_status()

    # -- receiver profiles -------------------------------------------------------
    def _pstore(self) -> profiles.ProfileStore:
        return profiles.ProfileStore(self.cfg.data_dir)

    def _busy(self) -> bool:
        return self.engine.snapshot().state in ("armed", "recording", "saving")

    def _profiles_detect(self, startup: bool = False):
        """Identify the connected receivers, create profiles for new ones, select the active one."""
        try:
            found = sdr.detect_receivers(self.cfg) if not (not startup and self._busy()) else None
        except Exception as e:  # noqa: BLE001
            self.engine.log(f"Receiver detection failed: {e}")
            found = []
        if found is None:
            QMessageBox.information(self, "METEOR-X", self.t["busy_prof"])
            return
        self._connected = {d.key for d in found}
        st = self._pstore()
        self._sync_cfg()                             # keep what is on screen for the current profile
        st.update_settings(st.active_id, self.cfg)
        act, msgs, _new = profiles.reconcile(st, found, self.cfg)
        for m in msgs:
            self.engine.log(m)
        if not found:
            self.engine.log(self.t["no_rx"])
        if act is not None:
            profiles.ProfileStore.apply(act, self.cfg)
            self._save_ini()
            self._widgets_from_cfg()
            if found:
                self.engine.log(f"{self.t['prof_active']}: {act['name']}  [{act['identity']['key']}]")
        self._refresh_device_list(found)
        self._profiles_refresh()

    def _refresh_device_list(self, found):
        """Offer only detected hardware types."""
        previous = self._device_kind() or (self.cfg.get("sdr", "device", "") or "").lower()
        connected_kinds = {device.kind for device in found}
        self.dev.blockSignals(True)
        self.dev.clear()
        for key, label in zip(DEV_KEYS, self.t["devices"]):
            if key in connected_kinds:
                self.dev.addItem(label, key)
        index = self.dev.findData(previous)
        if index < 0:
            active = self._pstore().active
            if active and active["identity"]["key"] in self._connected:
                index = self.dev.findData(active["identity"]["kind"])
        self.dev.setCurrentIndex(index)
        self.dev.blockSignals(False)
        self._dev_changed()

    def _profiles_refresh(self):
        st = self._pstore()
        self.prof_cb.blockSignals(True)
        self.prof_cb.clear()
        for p in st.profiles:
            if p["identity"]["key"] not in self._connected:
                continue
            self.prof_cb.addItem(p["name"], p["id"])
            self.prof_cb.setItemData(self.prof_cb.count() - 1,
                                     f"{p['identity']['key']}\n{self.t['connected']}",
                                     Qt.ToolTipRole)
        i = self.prof_cb.findData(st.active_id)
        self.prof_cb.setCurrentIndex(i)
        self.prof_cb.blockSignals(False)
        self.profile_menu_button.setEnabled(i >= 0)
        self._profile_status()

    def _profile_status(self):
        t = self.t
        st = self._pstore()
        p = st.active
        k = self._device_kind()
        if p is None or p["identity"]["key"] not in self._connected:
            self.prof_lbl.setText(t["no_rx"])
            self.chk_preamp.setVisible(False)
            return
        self._sync_cfg()
        lines = [f"{p['identity']['kind'].upper()} · SN {p['identity'].get('serial') or '?'}"
                 + (f" · {p['identity']['tuner']}" if p["identity"].get("tuner") else "")
                 + f" · ✔ {t['connected']}"]
        cs = calib.CalStore(os.path.join(self.cfg.data_dir, "calibration.json"))
        c = cs.get_for(self.cfg, p["id"]) if p["identity"]["kind"] == k else None
        if c is not None:
            nf = f", NF {c.nf_db:.1f} dB" if c.nf_db is not None else ""
            lines.append(("✔ " if c.ok else "⚠ ") + t["cal_ok"].format(date=c.date, k=c.k_db, nf=nf))
        else:
            lines.append("✖ " + t["cal_none"])
            others = sorted({x.key.split("#", 1)[-1].split("|")[1] for x in cs.for_profile(p["id"])})
            if others:
                lines.append("   " + t["cal_others"].format(lst=", ".join(others)))
        self.prof_lbl.setText("\n".join(lines))
        self.prof_lbl.setStyleSheet("font-size:11px;color:" + ("#9fb3a8" if c is not None else "#e0b040"))
        pa = p.get("preamp")
        self.chk_preamp.blockSignals(True)
        self.chk_preamp.setVisible(bool(pa))
        if pa:
            self.chk_preamp.setText(t["preamp_in"].format(name=pa.get("name", ""), g=float(pa.get("gain_db") or 0)))
            self.chk_preamp.setChecked(bool(pa.get("in_service")))
        self.chk_preamp.blockSignals(False)

    def _audio_toggled(self, on: bool):
        self.btn_audio.setText(self.t["listen"] if on else self.t["muted"])
        self.engine.audio.set(on, self.sl_vol.value() / 100.0)

    def _preamp_toggled(self, on: bool):
        st = self._pstore()
        st.set_preamp_in_service(st.active_id, on)
        self._profile_status()

    def _switch_profile(self, pid: str) -> bool:
        st = self._pstore()
        target = st.get(pid)
        if target is None or target["identity"]["key"] not in self._connected:
            return False
        if pid == st.active_id:
            return True
        if self._busy():
            QMessageBox.information(self, "METEOR-X", self.t["busy_prof"])
            return False
        self._sync_cfg()
        st.update_settings(st.active_id, self.cfg)
        st.set_active(pid)
        p = st.get(pid)
        profiles.ProfileStore.apply(p, self.cfg)
        self._save_ini()
        self._widgets_from_cfg()
        self.engine.log(f"{self.t['prof_active']}: {p['name']}  [{p['identity']['key']}]")
        return True

    def _profile_picked(self, idx: int):
        pid = self.prof_cb.itemData(idx)
        if pid:
            self._switch_profile(pid)
        self._profiles_refresh()

    def _dev_picked(self, idx: int):
        """Device type chosen by hand: use a connected receiver of that type."""
        k = self.dev.itemData(idx)
        st = self._pstore()
        act = st.active
        if act and act["identity"]["kind"] == k:
            self._profile_status()
            return
        cands = [p for p in st.profiles if p["identity"]["kind"] == k
                 and p["identity"]["key"] in self._connected]
        if cands:
            self._switch_profile(cands[0]["id"])
        self._profiles_refresh()

    def _profile_rename(self):
        st = self._pstore()
        p = st.active
        if not p or p["identity"]["key"] not in self._connected:
            return
        name, ok = QInputDialog.getText(self, "METEOR-X", self.t["rename_q"], text=p["name"])
        if ok and name.strip():
            st.rename(p["id"], name)
            self._profiles_refresh()

    def _profile_delete(self):
        st = self._pstore()
        p = st.active
        if not p or p["identity"]["key"] not in self._connected or self._busy():
            return
        if QMessageBox.question(self, "METEOR-X", self.t["delete_q"].format(name=p["name"])) == QMessageBox.Yes:
            st.delete(p["id"])
            next_connected = next((item for item in st.profiles
                                   if item["identity"]["key"] in self._connected), None)
            if next_connected:
                st.set_active(next_connected["id"])
                profiles.ProfileStore.apply(next_connected, self.cfg)
                self._save_ini()
                self._widgets_from_cfg()
            self._profiles_refresh()

    def _widgets_from_cfg(self):
        c = self.cfg
        cur = (c.get("sdr", "device", "rtlsdr") or "rtlsdr").lower()
        ws = (self.dev, self.rtl_gain, self.sp_grdb, self.sp_lna, self.sp_hlna, self.sp_hvga, self.sp_lgain, self.sp_pgain)
        for w in ws:
            w.blockSignals(True)
        self.dev.setCurrentIndex(self.dev.findData(cur))
        try:
            self.rtl_gain.setValue(float(c.get("sdr", "rtl_gain_db", "38.6")))
        except ValueError:
            pass
        self.sp_grdb.setValue(c.geti("sdr", "sdrplay_grdb", 40))
        self.sp_lna.setValue(c.geti("sdr", "sdrplay_lna_state", 3))
        self.cb_ant.setCurrentText((c.get("sdr", "sdrplay_antenna", "A") or "A").upper()[:1])
        self.sp_hlna.setValue(c.geti("sdr", "hackrf_lna_gain", 24))
        self.sp_hvga.setValue(c.geti("sdr", "hackrf_vga_gain", 30))
        self.chk_hamp.setChecked(c.getb("sdr", "hackrf_amp", False))
        self.sp_lgain.setValue(c.geti("sdr", "lime_gain_db", 40))
        self._set_lant(c.get("sdr", "lime_antenna", "auto"))
        self.ed_puri.setText(c.get("sdr", "pluto_uri", "ip:192.168.2.1") or "ip:192.168.2.1")
        try:
            self.sp_pgain.setValue(int(float(c.get("sdr", "pluto_gain_db", "50"))))
        except ValueError:
            pass
        for w in ws:
            w.blockSignals(False)
        self._dev_changed()

    def _set_lant(self, v):
        v = (v or "auto").strip()
        self.cb_lant.setCurrentText(next((a for a in LIME_ANT if a.lower() == v.lower()), "auto"))

    def _save_ini(self):
        try:
            self.cfg.save()
        except OSError as e:
            self.logq.put(f"INI save failed: {e}")

    def _sync_cfg(self):
        """Screen values -> cfg (memory only)."""
        c = self.cfg
        k = self._device_kind()
        if k:
            c.set("sdr", "device", k)
        c.set("sdr", "rtl_gain_db", f"{self.rtl_gain.value():.1f}")
        c.set("sdr", "sdrplay_grdb", self.sp_grdb.value())
        c.set("sdr", "sdrplay_lna_state", self.sp_lna.value())
        c.set("sdr", "sdrplay_antenna", self.cb_ant.currentText())
        c.set("sdr", "hackrf_lna_gain", self.sp_hlna.value() // 8 * 8)
        c.set("sdr", "hackrf_vga_gain", self.sp_hvga.value() // 2 * 2)
        c.set("sdr", "hackrf_amp", "yes" if self.chk_hamp.isChecked() else "no")
        c.set("sdr", "lime_gain_db", self.sp_lgain.value())
        c.set("sdr", "lime_antenna", self.cb_lant.currentText())
        c.set("sdr", "pluto_uri", self.ed_puri.text().strip() or "ip:192.168.2.1")
        c.set("sdr", "pluto_gain_db", self.sp_pgain.value())
        if k in BIAS_KEYS:
            c.set("sdr", BIAS_KEYS[k], "yes" if self.bias.isChecked() else "no")
        c.set("decoder", "dc_spike_removal", "yes" if self.dc_remove.isChecked() else "no")

    def _dc_remove_toggled(self, enabled: bool):
        self.cfg.set("decoder", "dc_spike_removal", "yes" if enabled else "no")
        self._save_ini()
        self.engine.command("dc_spike_removal", enabled=enabled)

    def _gain_changed(self, *_):
        # Throttle hardware calls while dragging, but keep applying the newest
        # value during a continuous drag. Persist only after the hand pauses.
        if not self._gain_timer.isActive():
            self._gain_timer.start(120)
        self._gain_save_timer.start(650)

    def _live_gain_values(self):
        return {
            "rtlsdr": {"gain_db": self.rtl_gain.value()},
            "sdrplay": {"grdb": self.sp_grdb.value(), "lna_state": self.sp_lna.value()},
            "hackrf": {"lna_gain": self.sp_hlna.value(), "vga_gain": self.sp_hvga.value()},
            "lime": {"gain_db": self.sp_lgain.value()},
            "pluto": {"gain_db": self.sp_pgain.value()},
        }

    def _apply_live_gain(self):
        self._gain_timer.stop()
        kind = self._device_kind()
        if kind in self._live_gain_values() and self.engine.snapshot().state in ("armed", "recording"):
            self.engine.command("gain", device=kind, values=self._live_gain_values()[kind])

    def _save_gain_settings(self):
        self._sync_cfg()
        if self._device_kind() == "sdrplay":
            self.cfg.set("sdr", "sdrplay_agc", "no")
        store = self._pstore()
        store.update_settings(store.active_id, self.cfg)
        self._save_ini()
        self._profile_status()

    def _apply_settings(self):
        c = self.cfg
        self._sync_cfg()
        st = self._pstore()
        st.update_settings(st.active_id, c)
        c.set("decoder", "mode", self.mode.currentText())
        c.set("gui", "auto_start", "yes" if self.chk_auto.isChecked() else "no")
        c.set("gui", "audio_volume", self.sl_vol.value())
        try:
            c.save()
        except OSError as e:
            self.logq.put(f"INI save failed: {e}")

    def _start(self):
        self._apply_settings()
        idx = self.sat.currentIndex()
        sat = self.cfg.satellites[idx].name if idx < len(self.cfg.satellites) else None
        self.engine.command("start", sat=sat, mode=self.mode.currentText())

    def _calibration(self):
        snap = self.engine.snapshot()
        if snap.state in ("armed", "recording"):
            QMessageBox.information(self, "METEOR-X", self.t["busy_cal"])
            return
        self._apply_settings()
        was_auto = self.chk_auto.isChecked()
        self.engine.command("auto", enabled=False)       # no pass may grab the receiver meanwhile
        try:
            from .caldialog import CalibrationDialog
            CalibrationDialog(self.cfg, self.lang, self, profile_id=self._pstore().active_id).exec()
        finally:
            self.engine.command("auto", enabled=was_auto)
            self._profile_status()

    def _driver_diag(self):
        from . import drivers
        rep = drivers.select_library((self.cfg.get("sdr", "rtl_dll_dir", "") or "").strip())
        box = QMessageBox(self)
        box.setWindowTitle("METEOR-X – RTL-SDR")
        box.setText(self.t["drv_diag"].rstrip("…"))
        box.setDetailedText("\n".join(rep.lines))
        box.setInformativeText(
            ("✔ " + rep.chosen.path) if rep.chosen else "✖ no usable rtlsdr.dll")
        if rep.usb_problem:
            b = box.addButton(self.t["drv_zadig"].rstrip("…"), QMessageBox.ActionRole)
            b.clicked.connect(self._zadig)
        box.addButton(QMessageBox.Close)
        for line in rep.lines:
            self.logq.put("   " + line)
        box.exec()

    def _zadig(self):
        from . import drivers
        err = drivers.launch_zadig()
        if err:
            QMessageBox.warning(self, "Zadig", err)

    def _import_tle(self):
        p, _ = QFileDialog.getOpenFileName(self, self.t["import_tle"], os.path.expanduser("~"),
                                           "TLE (*.txt *.tle *.3le);;All (*.*)")
        if p:
            self.engine.command("import_tle", path=p)

    def _view_latest_mosaic(self, period="day"):
        folder = os.path.join(self.cfg.output_dir, "Mosaics")
        endings = ("_night_ir.png",) if period == "night" else ("_rgb124.png", "_rgb123.png", "_rgb221.png")
        paths = [os.path.join(folder, name) for name in os.listdir(folder)
                 if name.endswith(endings)] if os.path.isdir(folder) else []
        if not paths:
            QMessageBox.information(self, "METEOR-X", self.t["mosaic_no_image"])
            return
        path = max(paths, key=lambda p: (os.path.basename(p)[:10],
                                         p.endswith("_rgb124.png"), os.path.getmtime(p)))
        try:
            if self.mosaic_dlg is None:
                self.mosaic_dlg = CompositeDialog(self, self.t)
            self.mosaic_dlg.load_composite(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, "METEOR-X", str(exc))
            return
        self.mosaic_dlg.resize(1100, 820)
        self.mosaic_dlg.show()
        self.mosaic_dlg.raise_()

    def _open_mosaics(self):
        folder = os.path.join(self.cfg.output_dir, "Mosaics")
        os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _rebuild_mosaic(self):
        day, ok = QInputDialog.getText(self, "METEOR-X", self.t["mosaic_day"],
                                       text=datetime.now().strftime("%Y-%m-%d"))
        if not ok:
            return
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            QMessageBox.warning(self, "METEOR-X", self.t["mosaic_invalid_day"])
            return
        self.logq.put(self.t["mosaic_started"].format(day=day))
        def work():
            try:
                from . import mosaic
                with mosaic.BUILD_LOCK:
                    result = mosaic.build_day(self.cfg.output_dir, day, self.engine.log,
                                              self.cfg.geti("recording", "city_min_population", 200_000),
                                              self.cfg.map_text_scale)
                if result is None:
                    self.engine.log(self.t["mosaic_no_passes"].format(day=day))
            except Exception as exc:
                self.engine.log(f"Composite build failed: {exc}")
        threading.Thread(target=work, name="MeteorX-Mosaic-Rebuild", daemon=True).start()

    def _set_saved_image_mode(self, path: str | None):
        self._opened_image_path = path
        self.view_sel.setEnabled(path is None)
        self.btn_enhance.setEnabled(path is None)
        self.chk_mirror.setEnabled(path is None)
        if path and self.enh_dlg is not None:
            self.enh_dlg.close()

    def _open_saved_image(self):
        snap = self.engine.snapshot()
        folder = snap.out_folder if snap.out_folder and os.path.isdir(snap.out_folder) else self.cfg.output_dir
        os.makedirs(folder, exist_ok=True)
        path, _ = QFileDialog.getOpenFileName(
            self, self.t["open_image"], folder,
            "Images (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff);;All files (*.*)")
        if path:
            self._show_saved_image(path)

    def _show_saved_image(self, path: str):
        try:
            img = read_saved_image(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.t["open_image"], str(exc))
            return
        self._set_saved_image_mode(path)
        self._preview_rev = self.engine.snapshot().preview_rev
        self._force_view = False
        self._shown = img
        self.view.auto_fit = True
        self.view.fit_mode = "width"
        self.view._fit_source_width = None
        self.view.show_array(img)
        self.img_info.setText(f"{self.t['saved_image']}: {os.path.basename(path)}"
                              f"  –  {img.shape[1]}×{img.shape[0]} px")
        self.img_info.setToolTip(path)
        self.btn_clear_image.setEnabled(True)
        self.btn_full_image.setEnabled(True)
        if self.full_image_dlg is not None and self.full_image_dlg.isVisible():
            self.full_image_dlg.update_image(self.view.item.pixmap(), self.img_info.text())

    def _open_full_image(self):
        if getattr(self, "_shown", None) is None:
            return
        if self.full_image_dlg is None:
            self.full_image_dlg = FullImageDialog(self, self.t)
        self.full_image_dlg.update_image(self.view.item.pixmap(), self.img_info.text())
        self.full_image_dlg.showFullScreen()
        self.full_image_dlg.raise_()
        QTimer.singleShot(0, self.full_image_dlg.view.fit_image)

    def _cadu_geodata(self, path, info, imager):
        """Use recorded pass elements, or a nearby cached TLE for a named CADU."""
        tle = info.get("tle")
        match = re.search(r"METEOR[-_ ]*M2[-_ ]*([34])\b", os.path.basename(path).upper())
        name = info.get("satellite") or (f"METEOR-M2 {match.group(1)}" if match else None)
        from_cache = not (isinstance(tle, (list, tuple)) and len(tle) == 2)
        if from_cache and name:
            sat = next((s for s in self.engine.sats
                        if re.sub(r"[-_ ]+", " ", s.name.upper()) == re.sub(r"[-_ ]+", " ", name.upper())), None)
            if sat is not None:
                cached = orbit.read_tle_file(os.path.join(self.cfg.data_dir, "tle.txt"))
                tle = cached.get(sat.norad) or sat.tle
        if not tle or not imager.channels_present:
            return name, None
        try:
            candidate = orbit.Satellite(name or "METEOR", int(tle[0][2:7]), [137.9])
            candidate.load(*tle)
            times = imager.row_times(imager.channels_present[0])
            image_time = datetime.fromtimestamp(float(times[len(times) // 2]), timezone.utc)
            if abs(image_time - candidate.tle_epoch) > timedelta(days=3):
                offset = self._cadu_clock_offset(path, info, times, candidate.tle_epoch)
                if offset is None:
                    self.logq.put(f"{name}: CADU image clock does not match its TLE; map overlays skipped")
                    return name, None
                imager.geo_time_offset_s = offset
                self.logq.put(f"{name}: corrected CADU image clock from recording file times")
        except (OverflowError, OSError, TypeError, ValueError):
            return name, None
        if from_cache:
            self.logq.put(f"{name}: using nearby cached TLE for CADU map overlays")
        return name, list(tle)

    def _cadu_clock_offset(self, path, info, times, tle_epoch):
        """Recover a bad satellite clock when original CADU file times match scan duration."""
        paths = [path]
        source = info.get("source") or ""
        match = re.search(r"File:\s*(.+?)_IQ_", source, re.IGNORECASE)
        if match:
            prefix = os.path.basename(match.group(1))
            paths.extend((os.path.join(self.cfg.output_dir, prefix, prefix + ".cadu"),
                          os.path.join(self.cfg.output_dir, prefix + ".cadu")))
        span = float(times[-1] - times[0])
        if span <= 0:
            return None
        for candidate_path in paths:
            if not os.path.isfile(candidate_path):
                continue
            created, modified = os.path.getctime(candidate_path), os.path.getmtime(candidate_path)
            if abs((modified - created) - span) > max(30.0, span * 0.1):
                continue
            # The downlinked image time leads the written CADU frames by about five seconds.
            first_offset = created - 5.0 - float(times[0])
            last_offset = modified - 5.0 - float(times[-1])
            if abs(first_offset - last_offset) > 30.0:
                continue
            offset = (first_offset + last_offset) / 2.0
            corrected = datetime.fromtimestamp(float(times[len(times) // 2]) + offset, timezone.utc)
            if abs(corrected - tle_epoch) <= timedelta(days=3):
                return offset
        return None

    def _open_cadu(self):
        p, _ = QFileDialog.getOpenFileName(self, ".cadu", self.cfg.output_dir, "CADU (*.cadu);;All (*.*)")
        if not p:
            return
        self._load_cadu(p, save_products=True)

    def _load_cadu(self, p, *, save_products):
        im = lrpt.MsuMrImager()
        self.logq.put(f"Opening {os.path.basename(p)}…")

        def work():
            n = lrpt.process_cadu_file(p, im)
            self.logq.put(f"{os.path.basename(p)}: {n} CADU, {im.packets} MSU-MR packets, "
                          f"channels {[c + 1 for c in im.channels_present]}")
            if im.packets == 0:
                self.logq.put("No imagery decoded; keeping the previous image")
                return
            info = recorded_cadu_info(p)
            folder = os.path.dirname(p)
            if save_products:
                folder = outputs.unique_path(os.path.splitext(p)[0] + "_products")
                files = imaging.save_products(im, folder, os.path.basename(os.path.splitext(p)[0]),
                                              ["221", "123", "124"], True, False, True,
                                              texture_fill=self.enh["texture_fill"])
                self.logq.put(f"Saved {len(files)} image(s) in {folder}")
            elif self.engine.last_imager is not None:
                return
            satellite, tle = self._cadu_geodata(p, info, im)
            self.engine.last_pass = {
                "satellite": satellite,
                "tle": tle,
                "folder": folder,
                "prefix": os.path.basename(os.path.splitext(p)[0]),
                "rotated": bool(info.get("rotated", self.cfg.getb(
                    "recording", "rotate_northbound", True)
                    and (info.get("pass") or {}).get("northbound"))),
                "completed": True,
            }
            self.engine.last_imager = im
            self._auto_enhanced_key = None
            self._pending_cadu_view = True
            self._force_view = True

        def guarded_work():
            try:
                work()
            except Exception as exc:  # surface file and decoding errors in the visible log
                self.logq.put(f"Could not open {os.path.basename(p)}: {exc}")

        threading.Thread(target=guarded_work, daemon=True).start()

    def _select_cadu_view(self):
        """Show a product the opened recording can actually provide."""
        im = self.engine.last_imager
        channels = {channel + 1 for channel in im.channels_present} if im is not None else set()
        if not channels:
            return
        target = "RGB 221" if {1, 2}.issubset(channels) else f"{self.t['ch']} {min(channels)}"
        index = self.view_sel.findText(target)
        if index >= 0 and index != self.view_sel.currentIndex():
            self.view_sel.blockSignals(True)
            self.view_sel.setCurrentIndex(index)
            self.view_sel.blockSignals(False)

    def _select_live_view(self):
        """Keep a new pass visible when the selected recipe needs absent channels."""
        im = self.engine.last_imager
        channels = {channel + 1 for channel in im.channels_present} if im is not None else set()
        if not channels:
            return
        selected = self.view_sel.currentText()
        if selected in view_products.PRODUCTS:
            available = not view_products.missing_channels(selected, dict.fromkeys(channels))
        elif selected.startswith("RGB "):
            available = all(int(channel) in channels for channel in selected.split()[1])
        else:
            try:
                available = int(selected.split()[-1]) in channels
            except (ValueError, IndexError):
                available = False
        # A single-channel fallback can become RGB as soon as channel 2 arrives.
        upgrade = selected == self._auto_live_view and selected != "RGB 221" and {1, 2}.issubset(channels)
        if available and not upgrade:
            return
        self._select_cadu_view()
        self._auto_live_view = self.view_sel.currentText()

    # -- periodic refresh ------------------------------------------------------
    def _tick(self):
        self._tickn += 1
        while True:
            try:
                self.logbox.appendPlainText(self.logq.get_nowait())
            except queue.Empty:
                break
        snap = self.engine.snapshot()
        t = self.t
        st = snap.state
        self.btn_clear_image.setEnabled(getattr(self, "_shown", None) is not None)
        self.btn_full_image.setEnabled(getattr(self, "_shown", None) is not None)
        if self.header_state.property("receiverState") != st:
            self.header_state.setProperty("receiverState", st)
            self.header_state.setText(t.get(f"state_{st}", st).upper())
            live = st in ("recording", "armed")
            color = ACCENT.name() if live else theme_color("#84ad9a").name()
            border = theme_color("#327452" if live else "#2b4a3d").name()
            self.header_state.setStyleSheet(
                f"background:#10241b;color:{color};border:1px solid {border};"
                "border-radius:12px;padding:7px 12px;font-weight:700;letter-spacing:1px")
        if self.btn_audio.isChecked() and not self.engine.audio.enabled and self.engine.audio.error:
            self.btn_audio.setChecked(False)          # no sound device: back to muted, reason in the log
        if self.engine.usb_driver_alert:
            self.engine.usb_driver_alert = False
            if os.name == "nt" and QMessageBox.question(self, "METEOR-X – RTL-SDR", t["drv_alert"]) \
                    == QMessageBox.Yes:
                self._zadig()
        txt = t.get(f"state_{st}", st)
        if snap.sat and st in ("armed", "recording"):
            txt += f" – {snap.sat}"
        col = {"recording": "#ff5a5a", "armed": "#e0b040"}.get(st, "#9fb3a8")
        self.state_lbl.setText(txt)
        self.state_lbl.setStyleSheet(f"font-size:14px;font-weight:700;padding:4px;color:{col}")
        self.btn_start.setEnabled(st in ("idle", "waiting"))
        self.btn_stop.setEnabled(st in ("armed", "recording"))

        active = st in ("armed", "recording")
        self.led_sig.set(active and snap.signal)
        self.led_vit.set(active and snap.vit_locked)
        self.led_frm.set(active and snap.frame_lock and snap.frames_ok > 0)
        self.led_img.set(active and snap.packets > 0)
        self.led_rec.set(snap.recording, warn=False)
        if active:
            self.smeter.set_level(snap.level_dbfs, snap.n0_dbfs_hz, snap.cal_k)
            self.eye.update_traces(snap.eye)
            self.const.update_syms(snap.syms)
            self.spec.update_spec(snap.spectrum)
        elif self._tickn % 10 == 0:
            self.eye.update_traces(None)
            self.const.update_syms(None)
            self.spec.update_spec(None)

        if self._tickn % 10 == 1:
            self._update_passes(snap)
            now = datetime.now(timezone.utc)
            self.status.showMessage(
                f"{self.cfg.get('station', 'callsign', '')}  {self.cfg.getf('station', 'latitude'):.3f}, "
                f"{self.cfg.getf('station', 'longitude'):.3f}   |   UTC {now.strftime('%H:%M:%S')}   |   "
                f"{t['tle']} ({t['tle_age']}): {snap.tle_info}   |   {snap.source or ''}   |   {self.cfg.output_dir}")
        if snap.preview_rev != self._preview_rev or getattr(self, "_force_view", False):
            self._preview_rev = snap.preview_rev
            self._force_view = False
            self._set_saved_image_mode(None)
            if self._pending_cadu_view:
                self._pending_cadu_view = False
                self._auto_live_view = None
                self._select_cadu_view()
            else:
                self._select_live_view()
            self._refresh_view(force=True)

    def _preview_pass_row(self, row: int, _column: int):
        if not 0 <= row < len(self._latest_passes):
            return
        self._selected_pass = self._latest_passes[row]
        self.tracker.show_pass(self._selected_pass, selected=True)
        self._pass_preview_timer.start(15_000)

    def _show_next_pass(self):
        self._selected_pass = None
        self._pass_preview_timer.stop()
        self.tracker.show_pass(self._latest_passes[0] if self._latest_passes else None)
        if self._latest_passes:
            self.tbl.selectRow(0)
        else:
            self.tbl.clearSelection()

    def _update_passes(self, snap):
        t = self.t
        ps = snap.passes or []
        self._latest_passes = ps
        self.tbl.setRowCount(len(ps))
        for i, p in enumerate(ps):
            loc = p.aos.astimezone()
            vals = [p.sat.name, loc.strftime("%a %H:%M"), f"{p.max_el:.0f}°",
                    t["north"] if p.northbound else t["south"], f"{p.duration_s / 60:.0f} min"]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if p.max_el >= 40:
                    it.setForeground(ACCENT)
                self.tbl.setItem(i, j, it)
        if self._selected_pass is None:
            self.tracker.show_pass(ps[0] if ps else None)
            if ps:
                self.tbl.selectRow(0)
            else:
                self.tbl.clearSelection()
        else:
            selected_key = (self._selected_pass.sat.norad, self._selected_pass.aos)
            selected_row = next((i for i, p in enumerate(ps)
                                 if (p.sat.norad, p.aos) == selected_key), None)
            if selected_row is not None:
                self.tbl.selectRow(selected_row)
            else:
                self.tbl.clearSelection()
        if ps:
            dt = (ps[0].aos - datetime.now(timezone.utc)).total_seconds()
            if dt > 0:
                h, m = int(dt // 3600), int(dt % 3600 // 60)
                self.next_lbl.setText(f"{t['next_in']} {h} h {m:02d} min – {ps[0].sat.name}, "
                                      f"{ps[0].max_el:.0f}°")
            else:
                self.next_lbl.setText(f"{ps[0].sat.name} – AOS")
        else:
            self.next_lbl.setText("–")

    def _refresh_view(self, force=False):
        if self._opened_image_path is not None:
            return
        im = self.engine.last_imager
        if im is None or im is self._cleared_imager:
            if getattr(self, "_shown", None) is None:
                self.img_info.setText(self.t["no_image"])
                self.img_info.setToolTip("")
            return
        self._cleared_imager = None
        sel = self.view_sel.currentText()
        chs = imaging.channel_images(im, fill=self.enh["fill"],
                                    texture_fill=self.enh["texture_fill"])
        if not chs:
            return
        img = None
        if sel in view_products.PRODUCTS:
            missing = view_products.missing_channels(sel, chs)
            if missing:
                reason = (self.t["view_missing_3"] if missing == [3] and {1, 2, 4}.issubset(chs)
                          else self.t["view_wait_channels"].format(
                              channels=", ".join(map(str, missing))))
                self._view_unavailable(sel, reason)
                return
            track = scan_deg = None
            if sel in ("MCIR", "MSA"):
                ch = 4 if sel == "MCIR" else 1
                lp = self.engine.last_pass or {}
                if not lp.get("tle") or len(im.line_time[ch - 1]) < 2:
                    self._view_unavailable(sel, self.t["view_needs_geo"])
                    return
                offset = getattr(im, "geo_time_offset_s", 0.0)
                key = (id(im), im._extent(ch - 1), tuple(lp["tle"]), offset)
                if self._product_track_cache.get(ch, (None, None))[0] != key:
                    tr = overlay.track_for(lp.get("satellite"), lp["tle"], im.row_times(ch - 1) + offset)
                    self._product_track_cache[ch] = (key, tr)
                tr = self._product_track_cache[ch][1]
                if tr is None:
                    self._view_unavailable(sel, self.t["view_geo_unavailable"])
                    return
                track, scan_deg = tr
            try:
                img = view_products.render(chs, sel, track, scan_deg)
            except Exception as e:  # an optional view must not interrupt reception
                self.engine.log(f"{sel}: {e}")
                self._view_unavailable(sel, self.t["view_render_error"])
                return
            if img is None:
                self._view_unavailable(sel, self.t["view_source_unavailable"])
                return
        else:
            try:                                # the selector is rebuilt as channels arrive: never trust it
                if sel.startswith("RGB"):
                    spec = sel.split()[1]
                    missing = [int(ch) for ch in spec if int(ch) not in chs]
                    if missing:
                        absent = sorted(set(missing))
                        if absent == [3] and {1, 2, 4}.issubset(chs):
                            reason = self.t["view_missing_3"]
                        else:
                            reason = self.t["view_wait_channels"].format(
                                channels=", ".join(map(str, absent)))
                        self._view_unavailable(sel, reason)
                        return
                    img = imaging.composite(chs, spec)
                else:
                    img = chs.get(int(sel.split()[-1]))
            except (ValueError, IndexError):
                img = None
        label = sel
        if img is None:
            self._view_unavailable(sel, self.t["view_source_unavailable"])
            return
        img = enhance.apply(img, self.enh["colour"], self.enh["infrared"])
        marks = [self.t[v].split(" (")[0].lower()           # "fill" is the normal default: not worth a mention
                 for k, v in (("colour", "enh_colour"), ("infrared", "enh_ir")) if self.enh[k]]
        # same orientation as the saved products: a northbound pass arrives south-first, so without the
        # half turn the picture - and any map drawn on it - would be upside down
        rotated = bool((self.engine.last_pass or {}).get("rotated"))
        if rotated:
            img = img[::-1, ::-1]
        src_w = img.shape[1]
        if self.enh["resize"]:
            img = geometry.correct(img)
            marks.append(self.t["enh_resize"].split(" (")[0].lower())
        mirrored = self.chk_mirror.isChecked()
        if mirrored:
            img = img[:, ::-1]
            marks.append(self.t["mirror"].lower())
        if self.enh["coast"] or self.enh["cities"]:
            img = self._with_overlay(img, src_w, corrected=self.enh["resize"],
                                     rotated=rotated, mirror=mirrored)
            marks += [self.t[v].split(" (")[0].lower()
                      for k, v in (("coast", "enh_coast"), ("cities", "enh_cities")) if self.enh[k]]
        if marks:
            label += " · " + ", ".join(marks)
        self._shown = img
        live = (self.engine.snapshot().state in ("armed", "recording") and
                not (self.engine.last_pass or {}).get("completed", False))
        if live and im is not self._live_view_imager:
            self._live_view_imager = im
            self.view.begin_live_follow()
        # Northbound passes are turned north-up, so their newest lines are at the top;
        # southbound passes grow downward. Follow the edge where lines actually arrive.
        self.view.show_array(img, follow_edge=("top" if rotated else "bottom") if live else None)
        note = (f"  –  Ch {5 if 5 in chs else 4} cloud proxy, not measured rainfall"
                if sel == view_products.PRECIPITATION else "")
        self.img_info.setText(f"{label}  –  {img.shape[1]}×{img.shape[0]} px  –  "
                              f"{self.t['ch']}: {', '.join(str(k) for k in sorted(chs))}  –  {im.packets} packets{note}")
        self.img_info.setToolTip("")
        self.btn_full_image.setEnabled(True)
        if self.full_image_dlg is not None and self.full_image_dlg.isVisible():
            self.full_image_dlg.update_image(self.view.item.pixmap(), self.img_info.text())
        self._save_auto_enhanced(sel, img)

    def _save_auto_enhanced(self, selection: str, img: np.ndarray):
        """Keep the selected enhanced view beside the finished pass's decoded images."""
        lp = self.engine.last_pass or {}
        if not lp.get("completed") or not lp.get("folder") or not lp.get("prefix"):
            return
        slug = re.sub(r"[^a-z0-9]+", "_", selection.lower()).strip("_") or "view"
        path = os.path.join(lp["folder"], f"{lp['prefix']}_{slug}_enhanced.png")
        enabled = (selection in view_products.PRODUCTS or self.chk_mirror.isChecked()
                   or any(self.enh[k] for k in ("colour", "infrared", "resize", "coast", "cities"))
                   or self.enh["fill"] != self.cfg.getb("recording", "fill_missing", True)
                   or os.path.isfile(path))
        if not enabled:
            return
        state = tuple((k, self.enh[k]) for k in sorted(self.enh)) + (self.chk_mirror.isChecked(),)
        key = (lp["folder"], lp["prefix"], selection, state)
        if key == self._auto_enhanced_key:
            return
        try:
            imaging.write_png(path, np.ascontiguousarray(img))
        except OSError as exc:
            self.engine.log(f"Enhanced image save failed: {exc}")
            return
        self._auto_enhanced_key = key
        self.engine.log(f"Saved enhanced view: {path}")

    def _view_unavailable(self, name: str, reason: str):
        self._shown = None
        self.view.clear_image()
        self.img_info.setText(f"{name} – {reason}")
        self.img_info.setToolTip("")
        self.btn_full_image.setEnabled(False)
        if self.full_image_dlg is not None:
            self.full_image_dlg.close()

    def _clear_received_image(self):
        """Hide this pass until a new imager arrives or the user opens a CADU."""
        self._set_saved_image_mode(None)
        self._cleared_imager = self.engine.last_imager
        self._shown = None
        self.view.clear_image()
        self.img_info.setText(self.t["no_image"])
        self.img_info.setToolTip("")
        self.btn_clear_image.setEnabled(False)
        self.btn_full_image.setEnabled(False)
        if self.full_image_dlg is not None:
            self.full_image_dlg.close()

    def _with_overlay(self, img, src_w, *, corrected, rotated, mirror):
        """Draw the map overlays on the picture, or hand it back untouched if the pass cannot be placed."""
        im = self.engine.last_imager
        lp = self.engine.last_pass or {}
        chans = im.channels_present if im is not None else []
        ch = chans[0] if chans else None
        # The imager object lives for the whole pass and grows as frames arrive, so caching on its
        # identity alone would freeze the track at the first few rows and leave the rest of the image
        # without an overlay. Key on what the track actually depends on.
        extent = im._extent(ch) if ch is not None else None
        offset = getattr(im, "geo_time_offset_s", 0.0)
        key = (id(im), ch, extent, tuple(lp.get("tle") or ()), offset)
        if self._track_cache[0] != key:
            tr = None
            if ch is not None:
                tr = overlay.track_for(lp.get("satellite"), lp.get("tle"), im.row_times(ch) + offset)
            self._track_cache = (key, tr)
        tr = self._track_cache[1]
        if tr is None:
            if not self._geo_warned:
                self._geo_warned = True
                self.engine.log(self.t["enh_geo_none"])
            return img
        track, scan_deg = tr
        try:
            return overlay.draw(img, track, scan_deg, src_w, corrected=corrected, rotated=rotated,
                                mirror=mirror, coastlines=self.enh["coast"], cities=self.enh["cities"],
                                min_pop=self.cfg.geti("recording", "city_min_population", 200000),
                                coast_colour=self.enh["coast_colour"], city_colour=self.enh["city_colour"],
                                city_scale=self.cfg.map_text_scale)
        except Exception as e:  # noqa: BLE001  (an overlay must never cost the user the picture)
            self.engine.log(f"Overlay: {e}")
            return img

    def _open_enhance(self):
        """Controls for the preview and the automatically saved enhanced view."""
        from PySide6.QtWidgets import QDialog
        if self.enh_dlg is not None and self.enh_dlg.isVisible():
            self.enh_dlg.raise_()
            return
        t = self.t
        d = QDialog(self)
        d.setWindowTitle("METEOR-X - " + t["enh_title"])
        v = QVBoxLayout(d)
        for key, lab in (("resize", t["enh_resize"]), ("colour", t["enh_colour"]), ("infrared", t["enh_ir"]),
                         ("fill", t["enh_fill"]), ("texture_fill", t["enh_texture_fill"]),
                         ("coast", t["enh_coast"]), ("cities", t["enh_cities"])):
            cb = QCheckBox(lab)
            cb.setChecked(self.enh[key])
            cb.toggled.connect(lambda on, k=key: self._set_enhancement(k, bool(on)))
            v.addWidget(cb)
        for key, lab in (("coast_colour", t["enh_coast_col"]), ("city_colour", t["enh_city_col"])):
            row = QHBoxLayout()
            row.addWidget(QLabel(lab))
            cbx = QComboBox()
            for name, rgb in overlay.PALETTE.items():
                cbx.addItem(t.get(f"col_{name}", name.title()), name)
                cbx.setItemData(cbx.count() - 1, QColor(*rgb), Qt.DecorationRole)   # a swatch, not just a word
            cbx.setCurrentIndex(max(0, cbx.findData(self.enh[key])))
            cbx.currentIndexChanged.connect(
                lambda _, c=cbx, k=key: self._set_enhancement(k, c.currentData()))
            row.addWidget(cbx, 1)
            v.addLayout(row)
        note = QLabel(t["enh_note"])
        note.setWordWrap(True)
        note.setStyleSheet("color:#9bb0a4")
        v.addWidget(note)
        credit = QLabel(t["enh_credit"])      # GeoNames is CC BY 4.0: the attribution has to be shown
        credit.setWordWrap(True)
        credit.setStyleSheet("color:#6f8378; font-size:10px")
        v.addWidget(credit)
        h = QHBoxLayout()
        bs = QPushButton(t["enh_save"])
        bs.clicked.connect(self._save_enhanced)
        h.addWidget(bs)
        bc = QPushButton(t["enh_close"])
        bc.clicked.connect(d.close)
        h.addWidget(bc)
        v.addLayout(h)
        self.enh_dlg = d
        d.show()

    def _set_enhancement(self, key, value):
        self.enh[key] = value
        if key == "texture_fill":
            self.cfg.set("recording", "fill_texture", "yes" if value else "no")
            self._save_ini()
        self._save_window_state()
        self._refresh_view(force=True)

    def _save_enhanced(self):
        """Write the picture exactly as displayed, next to the pass it came from.

        The name is built from the pass prefix, so it carries the pass index and sorts with the rest of
        the session, and `unique_path` numbers each further save: ..._enhanced.png, ..._enhanced_2.png.
        The decoded images and the CADU recording are never touched."""
        img = getattr(self, "_shown", None)
        if img is None:
            QMessageBox.information(self, "METEOR-X", self.t["enh_no_image"])
            return
        lp = self.engine.last_pass or {}
        folder, prefix = lp.get("folder"), lp.get("prefix")
        if not folder or not prefix:        # nothing saved for this pass yet: let the user place it
            p, _ = QFileDialog.getSaveFileName(self, self.t["enh_save"], "meteorrx_enhanced.png", "PNG (*.png)")
            if not p:
                return
            path = p if p.lower().endswith(".png") else p + ".png"
        else:
            path = outputs.unique_path(os.path.join(folder, f"{prefix}_enhanced.png"))
        try:
            imaging.write_png(path, np.ascontiguousarray(img))
            self.engine.log(f"{self.t['enh_save'].rstrip('… .')}: {os.path.basename(path)}")
        except OSError as e:
            QMessageBox.warning(self, "METEOR-X", str(e))

    def closeEvent(self, e):
        snap = self.engine.snapshot()
        if snap.state in ("armed", "recording"):
            if QMessageBox.question(self, "METEOR-X", self.t["quit_q"]) != QMessageBox.Yes:
                e.ignore()
                return
        self._apply_settings()
        self._save_window_state()
        self.engine.quit()
        e.accept()


def apply_dark(app: QApplication, accent=None, callsign_font_size=10):
    global ACCENT, _LUT
    chosen = QColor(accent) if accent is not None else QColor(DEFAULT_ACCENT)
    ACCENT = chosen if chosen.isValid() else QColor(DEFAULT_ACCENT)
    ACCENT.setAlpha(255)
    _LUT = _phosphor_lut()
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 9))
    p = QPalette()
    base, alt, text = QColor(7, 17, 15), QColor(15, 32, 27), QColor(226, 248, 235)
    p.setColor(QPalette.Window, base)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, QColor(8, 22, 19))
    p.setColor(QPalette.AlternateBase, alt)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, alt)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.Highlight, theme_color(QColor(43, 180, 105)))
    p.setColor(QPalette.HighlightedText, QColor(3, 15, 9))
    p.setColor(QPalette.ToolTipBase, alt)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(103, 133, 117))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(103, 133, 117))
    app.setPalette(p)
    check_icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "check.png")
    style = """
        QMainWindow, QDialog { background-color: #07110f; color: #e2f8eb; }
        QLabel { background: transparent; color: #e2f8eb; }
        QLabel#brandMark, QLabel#brandName, QLabel#brandMission,
        QLabel#buildBadge, QLabel#stationBadge, QLabel#headerState {
            background: transparent; border: none; }
        QLabel#brandName { color: #eafff1; font-size: 22px; font-weight: 900; }
        QLabel#brandMission { color: #75b894; font-size: 9px; font-weight: 700; }
        QLabel#buildBadge { color: #4cff9a; background: #112c1f;
            border: 1px solid #2f7f4e; border-radius: 6px; padding: 3px 7px;
            font-family: Consolas; font-size: 9px; }
        QLabel#stationBadge { color: #4cff9a; font-family: Consolas; font-size: __CALLSIGN_FONT_SIZE__px; }
        QLabel#workspaceTitle { color: #f0fff5; font-size: 15px; font-weight: 800; }
        QPushButton#clearImageButton { background: #10251d; color: #a8d8bc;
            border-color: #2c5840; padding: 4px 9px; }
        QPushButton#clearImageButton:hover { background: #1b4030; color: #eafff1; }
        QLabel#scopeHeader { color: #5ef7da; font: 700 10px Consolas;
            padding: 3px 5px; background: #0b2b2a; border-left: 3px solid #4cff9a; }
        QGroupBox { background: #0c1b17; border: 1px solid #24503a; border-radius: 12px;
            margin-top: 12px; padding: 13px 8px 7px 8px; font-weight: 700; }
        QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 7px;
            color: #65f5a0; background: #0c1b17; }
        QPushButton, QToolButton { background: #153026; color: #d8f7e4;
            border: 1px solid #315c43; border-radius: 7px; padding: 5px 11px; }
        QPushButton:hover, QToolButton:hover { background: #1c4932; border-color: #54da88; }
        QPushButton:pressed, QToolButton:pressed { background: #286744; }
        QPushButton:checked, QToolButton:checked { background: #205d3a;
            border-color: #4cff9a; color: #baffd3; }
        QPushButton:disabled, QToolButton:disabled { background: #14231d;
            color: #667f70; border-color: #253b2e; }
        QPushButton#startButton { background: #4cff9a; color: #04180d;
            border: 1px solid #8affba; font-weight: 800; }
        QPushButton#startButton:hover { background: #80ffb6; }
        QPushButton#stopButton { background: #321a20; color: #ffb3b9;
            border: 1px solid #94505a; font-weight: 700; }
        QPushButton#stopButton:hover { background: #4d252c; border-color: #f5878e; }
        QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit { background: #091c16;
            border: 1px solid #2e5940; border-radius: 6px; padding: 4px 7px;
            selection-background-color: #4cff9a; selection-color: #06160d; }
        QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QLineEdit:hover {
            border-color: #55c884; }
        QComboBox QAbstractItemView { background: #10271d; color: #e2f8eb;
            border: 1px solid #4cff9a; selection-background-color: #276e46; }
        QCheckBox { spacing: 8px; color: #d8eade; }
        QCheckBox::indicator { width: 15px; height: 15px; border-radius: 4px;
            border: 1px solid #4c8462; background: #071912; }
        QCheckBox::indicator:checked { background: #4cff9a; border-color: #a0ffc2;
            image: url("__CHECK_ICON__"); }
        QTableWidget, QPlainTextEdit { background: #071611; color: #cbe8d6;
            border: 1px solid #234634; border-radius: 7px; gridline-color: #1d3b2a;
            selection-background-color: #235a3a; }
        QHeaderView::section { background: #153326; color: #8dffc0;
            border: none; border-right: 1px solid #24503a; padding: 5px; font-weight: 700; }
        QTableWidget::item { padding: 2px 4px; }
        QMenuBar { background: transparent; color: #e7fff0; padding: 3px 7px; }
        QMenuBar::item:selected { background: #1d4f34; color: #ecfff3; border-radius: 4px; }
        QMenu { background: #10271d; color: #e2f8eb; border: 1px solid #376e4d; }
        QMenu::item:selected { background: #23613e; }
        QStatusBar { background: #0b1d16; color: #94c9a8; border-top: 1px solid #224f34; }
        QSplitter::handle { background: #163725; }
        QScrollBar:vertical { background: #0c2017; width: 11px; }
        QScrollBar::handle:vertical { background: #36754e; border-radius: 5px; min-height: 25px; }
        QScrollBar:horizontal { background: #0c2017; height: 11px; }
        QScrollBar::handle:horizontal { background: #36754e; border-radius: 5px; min-width: 25px; }
        QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
        QToolTip { background: #173324; color: #eafff1; border: 1px solid #58d988; }
    """
    style = re.sub(r"#[0-9a-fA-F]{6}\b", lambda m: theme_color(m.group()).name(), style)
    style = style.replace("__CALLSIGN_FONT_SIZE__", str(max(8, min(48, callsign_font_size))))
    app.setStyleSheet(style.replace("__CHECK_ICON__", check_icon.replace("\\", "/")))
    for widget in app.allWidgets():
        if isinstance(widget, DensityWidget) and widget._img is not None:
            widget._render()
        widget.update()


def _warm_dsp(app: QApplication, lang: str):
    """Compile/load the Numba DSP kernels, with a small notice when it takes time (first start)."""
    from . import dsp
    res = {}
    th = threading.Thread(target=lambda: res.update(t=dsp.warmup()), daemon=True)
    th.start()
    th.join(1.0)
    if not th.is_alive():
        return
    msg = i18n.texts(lang, "gui")["warmup"]
    lbl = QLabel(f"  METEOR-X {__version__}  /  BUILD {__build__}\n\n  {msg}  ")
    lbl.setWindowFlags(Qt.SplashScreen | Qt.WindowStaysOnTopHint)
    lbl.setStyleSheet("QLabel{background:#0c1e18;color:#eafff1;font-size:13px;padding:18px;"
                      "border:2px solid #4cff9a}")
    lbl.show()
    while th.is_alive():
        app.processEvents()
        th.join(0.05)
    lbl.close()


def main(argv=None):
    cfg = config.load()
    if os.name == "nt":
        try:  # own taskbar entry and icon instead of "Python"
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MARL.METEOR-X")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(argv or sys.argv)
    app.setApplicationName("METEOR-X")
    ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "meteor-x.png")
    if os.path.isfile(ico):
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(ico))
    apply_dark(app, cfg.get("gui", "accent_color", DEFAULT_ACCENT.name()),
               cfg.geti("gui", "callsign_font_size", 10))
    _warm_dsp(app, i18n.normalize(cfg.get("gui", "language", "fr")))
    w = MainWindow(cfg)
    w.show_initial()
    auto = os.environ.get("METEORRX_AUTOTEST")   # unattended test: start, run N s, screenshot, quit
    if auto:
        secs = float(auto)
        QTimer.singleShot(1500, w._start)
        QTimer.singleShot(int(secs * 1000), lambda: (
            w.grab().save(os.path.join(cfg.data_dir, "autotest.png")), w.engine.command("stop")))
        QTimer.singleShot(int(secs * 1000) + 4000, lambda: (
            w.engine.quit(), app.exit(0)))
    return app.exec()
