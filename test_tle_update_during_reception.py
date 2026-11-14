"""TLE downloads must not interrupt a live SDR stream."""

import queue
from unittest.mock import Mock

from meteorrx.engine import Engine


def test_tle_refresh_waits_for_receiver():
    engine = Engine.__new__(Engine)
    engine.q = queue.Queue()
    engine.session = object()
    engine._pending_tle_update = False
    engine.log = Mock()
    engine._update_tles = Mock()
    engine._refresh_passes = Mock()

    engine.q.put(("reload_tle", {}))
    engine._handle_commands()
    assert engine._pending_tle_update
    engine._update_tles.assert_not_called()
    engine._refresh_passes.assert_not_called()

    engine._apply_pending_tle_update()
    engine._update_tles.assert_not_called()

    engine.session = None
    engine._apply_pending_tle_update()
    engine._update_tles.assert_called_once_with(force=True)
    engine._refresh_passes.assert_called_once_with(force=True)
    assert not engine._pending_tle_update


if __name__ == "__main__":
    test_tle_refresh_waits_for_receiver()
    print("TLE refresh deferral passed")
