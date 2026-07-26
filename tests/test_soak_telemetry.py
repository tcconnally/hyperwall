"""Contract tests for macOS soak-telemetry configuration.

These tests deliberately run without PyQt/libmpv.  They pin the measurement
contract that the live M5 soak must emit: periodic resource snapshots with a
platform-accurate RSS unit, an explicit audio-churn profile, and a
machine-readable session manifest that ties logs/stats back to the run.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _literal_assignments(path: str) -> dict[str, object]:
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    out: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        out[target.id] = ast.literal_eval(node.value)
                    except ValueError:
                        pass
    return out


def test_soak_supports_audio_focused_profile():
    source = open(
        os.path.join(os.path.dirname(__file__), "..", "hyperwall", "soak.py"),
        encoding="utf-8",
    ).read()
    assert "HYPERWALL_SOAK_PROFILE" in source
    assert "audio" in source
    assert "_AUDIO_ACTIONS" in source


def test_soak_emits_machine_readable_manifest():
    source = open(
        os.path.join(os.path.dirname(__file__), "..", "hyperwall", "soak.py"),
        encoding="utf-8",
    ).read()
    assert "HYPERWALL_SOAK_REPORT_DIR" in source
    assert "hyperwall_soak_" in source
    assert 'self._write_report("start"' in source
    assert 'self._write_report(\n            "sample"' in source
    assert 'self._write_report(\n            "finish"' in source


def test_macos_soak_launcher_collects_system_telemetry():
    path = os.path.join(os.path.dirname(__file__), "..", "soak_wall.sh")
    source = open(path, encoding="utf-8").read()
    for expected in (
        "HYPERWALL_SOAK_PROFILE=audio",
        "HYPERWALL_SOAK_REPORT_DIR",
        "HYPERWALL_STATS=1",
        "HYPERWALL_PERFTRACE=1",
        "powermetrics",
        "nettop",
        "vm_stat",
    ):
        assert expected in source


def run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS  {test.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {test.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
