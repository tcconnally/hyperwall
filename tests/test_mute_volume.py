"""Mute/volume state-machine tests against a real VideoCell (offscreen Qt)
with a recording fake mpv.

Regressions for the 2026-07-13 owner report: unmute didn't restore an
audible volume (old bump only fired from exactly 0), and mute-state
visuals were updated piecemeal by three writers. Runs on the
windows-build CI job; skips on the pure-logic ubuntu lane.
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


def _make_cell():
    from hyperwall.cell import VideoCell

    class _Ctl:
        controls_visible = True

    cell = VideoCell(_Ctl())
    cell.resize(1280, 720)
    cell.show()
    _app.processEvents()
    cell._mpv = _FakeMpv()
    return cell


def test_unmute_from_fresh_restores_default_volume():
    cell = _make_cell()
    cell.btn_mute.click()  # unmute
    assert cell.muted is False
    assert cell._mpv.props["mute"] is False
    assert cell.vol_slider.value() == 70
    assert cell._mpv.props["volume"] == 70.0
    assert cell.btn_mute.property("audible") is True


def test_unmute_from_low_nonzero_restores_last_volume():
    # Owner report: slider left at a low value from earlier dragging meant
    # the old ==0 bump never fired and unmute landed near-silent.
    cell = _make_cell()
    cell.vol_slider.setValue(35)   # deliberate volume: remembered
    cell.vol_slider.setValue(4)    # near-silent fiddle: NOT remembered
    cell.btn_mute.click()          # mute (slider stays at 4)
    assert cell.muted is True
    assert cell.vol_slider.value() < 10
    cell.btn_mute.click()          # unmute — must restore last vol (35)
    assert cell.muted is False
    assert cell.vol_slider.value() == 35
    assert cell._mpv.props["volume"] == 35.0


def test_remute_after_drag_unmute():
    # Owner report: "wouldn't mute after it was unmuted."
    cell = _make_cell()
    cell.vol_slider.setValue(40)   # drag-to-unmute path
    assert cell.muted is False and cell._mpv.props["mute"] is False
    assert cell.btn_mute.isChecked() is False
    cell.btn_mute.click()          # re-mute
    assert cell.muted is True
    assert cell._mpv.props["mute"] is True
    assert cell.btn_mute.isChecked() is True
    assert cell.btn_mute.property("audible") is False


def test_drag_down_does_not_poison_last_vol():
    # Owner report round 2: dragging DOWN from 70 swept every value ≥10
    # through valueChanged, leaving _last_vol ≈ 10 — the next unmute
    # "restored" to a whisper. Mid-drag samples must not count.
    cell = _make_cell()
    cell.btn_mute.click()          # unmute → restores default 70
    assert cell.vol_slider.value() == 70
    # Simulate a real drag down to silence: press, sweep, release at 0.
    cell.vol_slider.setSliderDown(True)
    for v in (55, 30, 12, 6, 0):
        cell.vol_slider.setValue(v)
    cell.vol_slider.setSliderDown(False)   # emits sliderReleased at 0
    assert cell.muted is True              # drag-to-zero mutes
    assert cell._last_vol == 70            # sweep values NOT recorded
    cell.btn_mute.click()                  # unmute again
    assert cell.vol_slider.value() == 70   # restores loud, not a whisper
    assert cell._mpv.props["volume"] == 70.0


def test_drag_release_records_resting_volume():
    cell = _make_cell()
    cell.btn_mute.click()                  # unmute at 70
    cell.vol_slider.setSliderDown(True)
    cell.vol_slider.setValue(45)
    cell.vol_slider.setSliderDown(False)   # release at 45 → recorded
    assert cell._last_vol == 45


def test_drag_to_zero_mutes_and_syncs_ui():
    cell = _make_cell()
    cell.vol_slider.setValue(50)
    cell.vol_slider.setValue(0)
    assert cell.muted is True
    assert cell._mpv.props["mute"] is True
    assert cell.btn_mute.isChecked() is True
    assert cell.btn_mute.property("audible") is False


def run_all() -> int:
    if not _PYQT:
        print("  SKIP  PyQt6/Windows unavailable — mute/volume tests run on "
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
