from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")


def test_v8_shim_exists_and_delegates_to_package():
    shim = ROOT / "hyperwall.py"
    assert shim.exists()
    text = read("hyperwall.py")
    assert "from hyperwall import main" in text
    assert "main()" in text


def test_legacy_monolith_is_not_active_root_entrypoint():
    # hyperwall.py is the shim entry point (delegates to hyperwall package).
    # The legacy monolith (integrated wall + wizard + emby in one file) is gone.
    text = read("hyperwall.py")
    assert "from hyperwall import main" in text
    assert "main()" in text


def test_launcher_targets_v8_and_never_legacy_monolith():
    launch = read("launch.bat")
    assert "hyperwall_v8.exe" in launch
    assert "hyperwall.py" in launch
    assert "EXE_STALE" in launch


def test_controller_has_no_global_mute_shortcut():
    controller = read("hyperwall/controller.py")
    shortcut_keys = re.findall(r'\(\s*["\']([^"\']+)["\']\s*,', controller)
    assert "M" not in shortcut_keys
    assert "Escape" in shortcut_keys
    assert "Space" in shortcut_keys


def test_escape_has_app_level_emergency_filter():
    controller = read("hyperwall/controller.py")
    assert "class _EmergencyKeyFilter" in controller
    assert "installEventFilter(self._escape_filter)" in controller
    assert "removeEventFilter(self._escape_filter)" in controller
    assert "Qt.Key.Key_Escape" in controller


def test_mute_state_lives_on_video_cell_not_wall_controller():
    cell = read("hyperwall/cell.py")
    controller = read("hyperwall/controller.py")
    assert "self.muted" in cell
    assert "def _toggle_mute" in cell
    assert "_global_toggle_mute" not in controller
    assert "def _toggle_mute" not in controller


def test_runtime_banner_is_logged_on_startup():
    main = read("hyperwall/main.py")
    version = read("hyperwall/version.py")
    assert "runtime_banner" in main
    assert "logger.info(\"Runtime: %s\", runtime_banner())" in main
    assert "APP_VERSION = \"8.2\"" in version
    assert "git_branch" in version
    assert "git_commit" in version
