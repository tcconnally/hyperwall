"""Construction/wiring tests for the opt-in instrumentation.

Regression for the 10.6.0 soak-launch crash: LoopLagSampler was handed the
WallController as a QObject parent, but WallController is a plain object —
the app died right after wall init whenever HYPERWALL_PERFTRACE=1. These
tests construct both instruments exactly the way app.py wires them, against
a non-QObject wall stand-in, and drive their timer slots directly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HYPERWALL_SOAK_MINUTES"] = "1"
# Plain-wall churn test needs deterministic advances (fake cells
# have no buttons for the function exerciser).
os.environ["HYPERWALL_SOAK_ACTIONS"] = "0"

# The ubuntu CI job is the pure-logic lane and deliberately has no PyQt
# (and hyperwall.soak needs ctypes.wintypes anyway); these tests then skip
# there and run for real on the windows-build job, which installs pyqt6
# and runs the suite before building.
try:
    from PyQt6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    _PYQT = os.name == "nt"
except ImportError:
    _PYQT = False


class _FakeCell:
    current_item = {"Name": "x", "Id": "1"}


class _PlainWall:
    """Deliberately NOT a QObject — mirrors WallController."""

    def __init__(self):
        self.cells = [_FakeCell(), _FakeCell()]
        self.advances = []
        self.shutdowns = 0

    def next_video(self, cell, is_retry=False):
        self.advances.append(cell)

    def _shutdown(self):
        self.shutdowns += 1


def test_loop_lag_sampler_constructs_and_ticks():
    from hyperwall.perftrace import LoopLagSampler
    s = LoopLagSampler()  # app.py passes no parent — wall is not a QObject
    s.start()
    s._tick()
    s._tick()
    s._log_summary()  # must not raise with samples present
    assert True


def test_resource_snapshot_returns_real_values():
    # Regression: truncated GetCurrentProcess pseudo-handle made every
    # Win32 call fail silently — an hour-long soak logged gdi=0/ws=None.
    # A live process always has a nonzero working set and ≥1 thread.
    from hyperwall.soak import _resource_snapshot
    snap = _resource_snapshot()
    assert snap.get("ws_mb", 0) > 0, f"working set missing/zero: {snap}"
    assert snap.get("private_mb", 0) > 0, f"private bytes missing/zero: {snap}"
    assert snap.get("threads", 0) >= 1
    # USER objects: any process with a QCoreApplication has at least one
    # (message-only) window on Windows; GDI can legitimately be 0 offscreen.
    assert "user" in snap and "gdi" in snap


def test_traced_slot_survives_qt_signal_payload_args():
    """Regression for the 2026-07-14 soak finding: Qt's clicked signal
    carries a `checked` bool; the @traced wrapper's *args defeated PyQt's
    arity inspection, the bool got through, and EVERY traced click handler
    crashed with TypeError whenever tracing was enabled — silently drifting
    button state from cached state (caught by the exerciser invariants)."""
    from hyperwall import perftrace
    from PyQt6.QtWidgets import QPushButton

    calls = []

    class _Obj:
        def handler(self):     # 1-arg method, like _toggle_mute
            calls.append(1)

    # Force a real wrapper regardless of the env var.
    orig = perftrace.PERFTRACE_ENABLED
    perftrace.PERFTRACE_ENABLED = True
    try:
        _Obj.handler = perftrace.traced("test.handler")(_Obj.handler)
    finally:
        perftrace.PERFTRACE_ENABLED = orig

    obj = _Obj()
    btn = QPushButton()
    btn.clicked.connect(obj.handler)
    btn.click()   # emits clicked(False) — must not raise, must run
    assert calls == [1], "traced slot swallowed or crashed on the payload arg"


def test_soak_function_exerciser_drives_real_cell():
    """Every soak action runs against a real VideoCell through the real
    handlers, and the state invariants hold after each one."""
    from hyperwall.soak import SoakController
    from hyperwall.cell import VideoCell

    class _FakeMpv:
        def __init__(self):
            self.props = {"mute": True, "volume": 100.0, "aid": "no",
                          "pause": False, "time-pos": 12.0,
                          "eof-reached": False}
        def __setitem__(self, k, v): self.props[k] = v
        def __getitem__(self, k): return self.props[k]
        def __getattr__(self, name):
            key = name.replace("_", "-")
            if key in self.props: return self.props[key]
            raise AttributeError(name)
        def seek(self, *a, **k): pass
        def command(self, *a): pass

    class _Ctl:
        controls_visible = True
        def update_favorite(self, *a): pass

    cell = VideoCell(_Ctl())
    cell.resize(1280, 720)
    cell.show()
    _app.processEvents()
    cell._mpv = _FakeMpv()
    cell._duration_s = 100.0
    cell.current_item = {"Id": "x", "Name": "n",
                         "UserData": {"IsFavorite": False}, "Tags": []}

    class _W:
        def __init__(self): self.cells = [cell]; self.nexts = 0; self.prevs = 0
        def next_video(self, c, r=False): self.nexts += 1
        def prev_video(self, c): self.prevs += 1
        def _shutdown(self): pass

    wall = _W()
    soak = SoakController(wall)
    for action in ("advance", "prev", "seek", "audio", "volume",
                   "pause", "loop", "favorite", "audio"):
        soak._do_action(action, cell)
        soak._verify_invariants(cell, action)
    assert soak._invariant_violations == 0, "state drift under exerciser"
    assert wall.nexts == 1 and wall.prevs == 1
    assert cell.muted is True, "second audio action must re-mute"
    assert cell.looping is False, "loop must double-toggle to off"
    assert cell.current_item["UserData"]["IsFavorite"] is False


def test_soak_controller_constructs_against_plain_wall():
    from hyperwall.soak import SoakController
    wall = _PlainWall()
    c = SoakController(wall)  # regression: must not use wall as QObject parent
    c._sample()               # resource snapshot logs without raising
    c._churn()                # advances a random cell via wall.next_video
    assert len(wall.advances) == 1
    c._finish()               # summary + graceful shutdown hook
    assert wall.shutdowns == 1


def run_all() -> int:
    if not _PYQT:
        print("  SKIP  PyQt6/Windows unavailable — instrumentation tests "
              "run on the windows-build job.")
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
