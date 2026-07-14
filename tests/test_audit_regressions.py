"""Regressions from the 2026-07-13 full performance/quality audit.

Each test pins a specific certain-confidence finding. Offscreen-Qt tests
skip on the pure-logic ubuntu CI lane (they run on windows-build).
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
                      "pause": False, "time-pos": 12.0, "eof-reached": False}

    def __setitem__(self, k, v):
        self.props[k] = v

    def __getitem__(self, k):
        return self.props[k]

    def __getattr__(self, name):
        key = name.replace("_", "-")
        if key in self.props:
            return self.props[key]
        raise AttributeError(name)

    def seek(self, *a, **k):
        pass

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
    return cell


def test_seek_release_restores_pre_drag_pause_state():
    # Finding 3.3: releasing a seek always resumed, silently un-pausing a
    # deliberately paused cell.
    cell = _make_cell()
    cell._paused = True
    cell._mpv.props["pause"] = True
    cell.seek_slider.setSliderDown(True)   # _seek_press
    cell.seek_slider.setValue(500)
    cell.seek_slider.setSliderDown(False)  # _seek_release
    assert cell._paused is True
    assert cell._mpv.props["pause"] is True
    # and an unpaused cell still resumes normally
    cell._paused = False
    cell._mpv.props["pause"] = False
    cell.seek_slider.setSliderDown(True)
    cell.seek_slider.setValue(300)
    cell.seek_slider.setSliderDown(False)
    assert cell._paused is False
    assert cell._mpv.props["pause"] is False


def test_cell_autohide_does_not_clear_global_controls_flag():
    # Finding 3.2: one cell's autohide cleared the wall-wide toggle flag.
    cell = _make_cell()
    cell.controller.controls_visible = True
    cell.controls_visible = True
    cell._mouse_in_cell = False
    cell._autohide_controls()
    assert cell.controls_visible is False
    assert cell.controller.controls_visible is True


def test_paused_cell_does_not_auto_advance_at_eof():
    # Finding 3.7: a cell reaching EOF under a global pause advanced and
    # started playing while the rest of the wall stayed paused.
    cell = _make_cell()
    fired = []
    cell.request_next.connect(lambda c, r: fired.append(1))
    cell._played_anything = True
    cell._paused = True
    cell._mpv.props["eof-reached"] = True
    cell._handle_track_done(cell._mpv_gen)
    _app.processEvents()
    assert not fired, "paused cell must not auto-advance"


def test_parked_cell_manual_play_unparks():
    # Finding 3.6: a manual advance during park left _parked latched, so the
    # next error was swallowed by the parked-guard and the cell went dead.
    cell = _make_cell()
    cell._parked = True
    cell._failure_ts.append(1.0)
    cell.play({"Id": "x", "Name": "n"}, "http://u")
    assert cell._parked is False
    assert len(cell._failure_ts) == 0


def test_fetch_items_paginates_beyond_page_limit():
    # Finding 1.5: fixed Limit silently truncated large libraries.
    from hyperwall.emby import EmbyClient

    client = EmbyClient("http://x", "u", "p")
    client.user_id = "uid"
    calls = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200

        def json(self):
            return self._payload

    total = 7_500  # > one 5 000-item page

    def fake_get(path, **kw):
        if path.endswith("/Views"):
            return _Resp({"Items": [{"Name": "lib", "Id": "L1"}]})
        params = kw.get("params", {})
        start = int(params.get("StartIndex", 0))
        limit = int(params.get("Limit", 0))
        calls.append(start)
        batch = [{"Id": i} for i in range(start, min(start + limit, total))]
        return _Resp({"Items": batch, "TotalRecordCount": total})

    client.get = fake_get
    items = client.fetch_items(["lib"])
    assert len(items) == total, f"expected {total}, got {len(items)}"
    assert len(calls) >= 2, "pagination must issue multiple page requests"


def run_all() -> int:
    if not _PYQT:
        print("  SKIP  PyQt6/Windows unavailable — audit regressions run on "
              "the windows-build job.")
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
