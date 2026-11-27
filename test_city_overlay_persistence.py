"""A live image must retain its map data across enhancement redraws."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_internal"))
from meteorrx import gui, overlay  # noqa: E402


class Imager:
    channels_present = [0]

    def __init__(self):
        self.extent = (0, 8)

    def _extent(self, _channel):
        return self.extent

    def row_times(self, _channel):
        return np.arange(8, dtype=float)


def test_city_overlay_keeps_geodata_for_same_imager():
    imager = Imager()
    viewer = SimpleNamespace(
        _overlay_geo=None,
        _track_cache=(None, None),
        _geo_warned=False,
        t={"enh_geo_none": "No map location"},
        engine=SimpleNamespace(log=lambda _message: None),
        cfg=SimpleNamespace(geti=lambda *_args: 200_000, map_text_scale=4.0),
        enh={"coast": False, "cities": True,
             "coast_colour": "cyan", "city_colour": "yellow"},
    )
    tle = ("first TLE line", "second TLE line")
    calls = []

    def track_for(satellite, used_tle, _times):
        calls.append((satellite, tuple(used_tle) if used_tle else None))
        return (object(), 55.0) if used_tle else None

    image = np.full((8, 8, 3), 80, dtype=np.uint8)
    with patch.object(overlay, "track_for", side_effect=track_for), \
         patch.object(overlay, "draw", side_effect=lambda img, *_args, **_kwargs: img.copy()):
        first = gui.MainWindow._with_overlay(
            viewer, image, 8, im=imager,
            lp={"satellite": "METEOR-M2 4", "tle": tle},
            corrected=False, rotated=False, mirror=False)
        # The receiver may publish different pass metadata while this imager
        # is still on screen; an enhancement forces a redraw at that moment.
        imager.extent = (0, 9)
        second = gui.MainWindow._with_overlay(
            viewer, image, 8, im=imager, lp={},
            corrected=False, rotated=False, mirror=False)

    assert first.shape == second.shape == image.shape
    assert calls == [("METEOR-M2 4", tle), ("METEOR-M2 4", tle)]


if __name__ == "__main__":
    test_city_overlay_keeps_geodata_for_same_imager()
