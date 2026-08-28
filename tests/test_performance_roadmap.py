"""Repository contract for the macOS performance roadmap."""
from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def test_performance_roadmap_contains_corrected_evidence_and_gates():
    path = _ROOT / "docs" / "performance-roadmap.md"
    assert path.is_file(), f"missing roadmap: {path}"
    text = path.read_text(encoding="utf-8")
    required = (
        "20260828_161931",
        "20260828_172945",
        "0.8275",
        "0.8313",
        "30/25",
        "duration coverage",
        "videotoolbox-copy",
        "coalesce",
    )
    for marker in required:
        assert marker in text, marker


def test_readme_links_to_performance_roadmap():
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/performance-roadmap.md" in text


def run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"  {passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(run_all())
