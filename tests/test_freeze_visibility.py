"""Freeze-visibility regression tests (v10.11.0).

paused-for-cache episodes (network starvation) are the freeze class the
frame-drop counters can't see and the 20s stall watchdog is too slow for —
the gap behind every 'it still freezes sometimes' report. These pin the
detection layer: counters, WARNING logs, BUFFERING card, stats surfacing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    _PYQT = os.name == "nt"
except ImportError:
    _PYQT = False


class _FakeMpv:
    def __init__(self):
        self.props = {"mute": True, "volume": 100.0, "aid": "no",
                      "pause": False, "time-pos": 12.0, "eof-reached": False,
                      "cache-buffering-state": 42}
        self.seek_calls = []

    def __setitem__(self, k, v):
        self.props[k] = v

    def __getitem__(self, k):
        return self.props[k]

    def __getattr__(self, name):
        key = name.replace("_", "-")
        if key in self.props:
            return self.props[key]
        raise AttributeError(name)

    def seek(self, target, mode="absolute"):
        self.seek_calls.append((target, mode))

    def command(self, *a):
        pass


class _Ctl:
    controls_visible = True


def _make_cell():
    from hyperwall.cell import VideoCell
    cell = VideoCell(_Ctl())
    cell.resize(1280, 720)
    cell.show()
    _app.processEvents()
    cell._mpv = _FakeMpv()
    cell._duration_s = 100.0
    cell._played_anything = True
    return cell


def _context(cell, gen=None, track=None):
    return (
        cell._mpv_gen if gen is None else gen,
        cell._track_generation if track is None else track,
        None,
        cell._stream_url,
        None,
    )


def test_buffering_episode_counts_and_shows_card():
    cell = _make_cell()
    gen = cell._mpv_gen
    cell._handle_buffering(_context(cell, gen), True)
    assert cell._freeze_count == 1
    assert cell._buffering_card is True
    assert cell._title_overlay.isVisible()
    assert "BUFFERING" in cell._title_overlay.text()
    cell._handle_buffering(_context(cell, gen), False)
    assert cell._freeze_total_s >= 0.0
    assert cell._freeze_t0 == 0.0
    assert cell._buffering_card is False
    assert not cell._title_overlay.isVisible()


def test_startup_fill_is_not_a_freeze():
    cell = _make_cell()
    cell._played_anything = False
    cell._handle_buffering(
        _context(cell), True,
    )
    assert cell._freeze_count == 0
    assert cell._buffering_card is False


def test_stale_generation_buffering_ignored():
    cell = _make_cell()
    cell._handle_buffering(
        _context(cell, gen=cell._mpv_gen - 1), True,
    )
    assert cell._freeze_count == 0


def test_track_change_closes_open_freeze():
    cell = _make_cell()
    gen = cell._mpv_gen
    cell._handle_buffering(
        _context(cell, gen), True,
    )
    assert cell._freeze_t0 > 0
    cell._begin_track({"Id": "x", "Name": "n"})
    assert cell._freeze_t0 == 0.0
    assert cell._freeze_total_s >= 0.0
    assert cell._buffering_card is False


def test_seek_cap_is_98_percent():
    cell = _make_cell()
    cell.seek_slider.setSliderDown(True)
    cell.seek_slider.setValue(1000)   # drag to the very end
    cell.seek_slider.setSliderDown(False)
    assert cell._mpv.seek_calls, "seek must fire"
    target, _mode = cell._mpv.seek_calls[-1]
    assert abs(target - 98.0) < 0.01, f"expected 98.0 (0.98 cap), got {target}"


def run_all() -> int:
    if not _PYQT:
        print("  SKIP  PyQt6/Windows unavailable — freeze tests run on the "
              "windows-build job.")
        return 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
