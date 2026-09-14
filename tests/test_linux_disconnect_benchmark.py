"""Tests for the KVM/HDMI-disconnect-safe Linux benchmark contract."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperwall.emby_benchmark import (  # noqa: E402
    wall_safe_violations,
)
from hyperwall.linux_disconnect_benchmark import (  # noqa: E402
    build_mpv_command,
    connector_summary,
    parse_mpv_errors,
    read_drm_status,
    safe_input_label,
    summarize_run,
)


def test_drm_status_reads_connector_files_without_shell_tools():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "card0-HDMI-A-1").mkdir()
        (root / "card0-HDMI-A-1" / "status").write_text("disconnected\n", encoding="utf-8")
        (root / "card0-DP-1").mkdir()
        (root / "card0-DP-1" / "status").write_text("connected\n", encoding="utf-8")

        assert read_drm_status(root) == {
            "card0-DP-1": "connected",
            "card0-HDMI-A-1": "disconnected",
        }


def test_connector_summary_marks_all_disconnected_separately_from_unknown():
    assert connector_summary({"HDMI-A-1": "disconnected"}) == {
        "known": 1,
        "connected": 0,
        "disconnected": 1,
        "all_disconnected": True,
        "status": "all_disconnected",
    }
    assert connector_summary({})["status"] == "unknown"
    assert connector_summary({"DP-1": "connected"})["status"] == "connected"


def test_mpv_command_is_display_independent_and_loops_one_input():
    command = build_mpv_command(
        "mpv",
        "http://media.invalid/secret-token.mp4",
        hwdec="auto-safe",
        log_path=Path("/tmp/cell-0.log"),
    )
    assert command[:2] == ["mpv", "--no-config"]
    assert "--vo=null" in command
    assert "--ao=null" in command
    assert "--hwdec=auto-safe" in command
    assert "--loop-file=inf" in command
    assert command[-1] == "http://media.invalid/secret-token.mp4"


def test_input_labels_are_stable_and_do_not_expose_source():
    label = safe_input_label("https://media.invalid/path?api_key=secret")
    assert label.startswith("input-")
    assert "secret" not in label
    assert label == safe_input_label("https://media.invalid/path?api_key=secret")


def test_mpv_error_parser_separates_decoder_audio_and_transport_faults():
    parsed = parse_mpv_errors(
        "\n".join(
            [
                "[ffmpeg/video] Error while decoding frame",
                "Audio device underrun detected",
                "Connection refused",
                "output image buffer is null: -12909",
            ]
        )
    )
    assert parsed == {
        "decoder_faults": 2,
        "audio_underruns": 1,
        "transport_errors": 1,
    }


def test_summary_passes_decode_gate_when_kvm_disconnects_but_cells_survive():
    summary = summarize_run(
        requested_seconds=120,
        elapsed_monotonic=120.2,
        elapsed_boottime=120.2,
        cell_results=[
            {"cell": index, "returncode": 0, "early_exit": False, "errors": parse_mpv_errors("")}
            for index in range(8)
        ],
        display_events=[
            {"summary": {"status": "connected", "all_disconnected": False}},
            {"summary": {"status": "all_disconnected", "all_disconnected": True}},
        ],
    )
    assert summary["decode_transport_verdict"] == "PASS"
    assert summary["headless_disconnect_resilience"] == "PASS"
    assert summary["presentation_gate"] == "BLOCK"
    assert summary["verdict"] == "BLOCK"
    assert summary["display_disconnects"] == 1


def test_summary_blocks_early_cell_exit_even_without_display_evidence():
    summary = summarize_run(
        requested_seconds=120,
        elapsed_monotonic=120.0,
        elapsed_boottime=120.0,
        cell_results=[
            {"cell": 0, "returncode": 1, "early_exit": True, "errors": parse_mpv_errors("")},
        ],
        display_events=[],
    )
    assert summary["decode_transport_verdict"] == "BLOCK"
    assert summary["headless_disconnect_resilience"] == "WARNING"
    assert summary["verdict"] == "BLOCK"


def test_preexisting_display_loss_is_not_counted_as_a_kvm_transition():
    disconnected = {"status": "all_disconnected", "all_disconnected": True}
    summary = summarize_run(
        requested_seconds=60,
        elapsed_monotonic=60.0,
        elapsed_boottime=60.0,
        cell_results=[
            {"cell": index, "returncode": 0, "early_exit": False, "errors": parse_mpv_errors("")}
            for index in range(8)
        ],
        display_events=[
            {"elapsed_monotonic_seconds": 0.0, "summary": disconnected},
            {"elapsed_monotonic_seconds": 60.0, "summary": disconnected},
        ],
    )
    assert summary["display_preexisting_disconnect"] is True
    assert summary["display_loss_transitions"] == 0
    assert summary["display_disconnects"] == 0
    assert summary["display_loss_seconds"] == 0.0
    assert summary["display_preexisting_disconnect_seconds"] == 60.0
    assert summary["headless_disconnect_resilience"] == "WARNING"


def test_summary_records_kvm_loss_and_recovery_interval():
    connected = {"status": "connected", "all_disconnected": False}
    disconnected = {"status": "all_disconnected", "all_disconnected": True}
    summary = summarize_run(
        requested_seconds=60,
        elapsed_monotonic=60.0,
        elapsed_boottime=60.0,
        cell_results=[
            {"cell": index, "returncode": 0, "early_exit": False, "errors": parse_mpv_errors("")}
            for index in range(8)
        ],
        display_events=[
            {"elapsed_monotonic_seconds": 0.0, "summary": connected},
            {"elapsed_monotonic_seconds": 10.0, "summary": disconnected},
            {"elapsed_monotonic_seconds": 40.0, "summary": connected},
        ],
    )
    assert summary["display_loss_transitions"] == 1
    assert summary["display_recoveries"] == 1
    assert summary["display_loss_seconds"] == 30.0
    assert summary["headless_disconnect_resilience"] == "PASS"


def _wall_safe_emby_item(*, video_codec: str = "h264") -> dict:
    return {
        "Id": "safe-item",
        "Name": "safe-item",
        "MediaSources": [
            {
                "Container": "mp4",
                "Bitrate": 8_160_000,
                "MediaStreams": [
                    {
                        "Type": "Video",
                        "Codec": video_codec,
                        "Width": 1920,
                        "Height": 1080,
                        "BitRate": 8_000_000,
                        "AverageFrameRate": 30,
                    },
                    {"Type": "Audio", "Codec": "aac", "Channels": 2},
                ],
            },
        ],
    }


def test_emby_wall_safe_selector_accepts_normalized_contract():
    assert wall_safe_violations(_wall_safe_emby_item()) == []


def test_emby_wall_safe_selector_rejects_hevc_source():
    violations = wall_safe_violations(_wall_safe_emby_item(video_codec="hevc"))
    assert "video_codec" in violations


def test_emby_wall_safe_selector_rejects_heavy_source():
    item = _wall_safe_emby_item()
    item["MediaSources"][0]["MediaStreams"][0]["BitRate"] = 12_000_000
    assert "video_bitrate" in wall_safe_violations(item)


def test_emby_wrapper_runs_non_wall_safe_item_instead_of_blocking():
    script = Path(__file__).resolve().parents[1] / "scripts" / "run-linux-disconnect-benchmark-emby.py"
    spec = importlib.util.spec_from_file_location("benchmark_emby_wrapper", script)
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)

    item = _wall_safe_emby_item(video_codec="hevc")
    launched: list[list[str]] = []

    class _Session:
        def close(self):
            return None

    original_load_login = wrapper._load_login
    original_authenticate = wrapper._authenticate
    original_library_items = wrapper._library_items
    original_run = wrapper.subprocess.run
    try:
        wrapper._load_login = lambda _path: (
            "http://emby.invalid:8096", "user", "password", "mv"
        )
        wrapper._authenticate = lambda *_args: (_Session(), "user-id", "secret-token")
        wrapper._library_items = lambda *_args: [item]
        wrapper.subprocess.run = lambda command, check: (
            launched.append(command) or type("Result", (), {"returncode": 0})()
        )
        with tempfile.TemporaryDirectory() as directory:
            result = wrapper.main([
                "--library", "mv",
                "--item-id", "safe-item",
                "--output", str(Path(directory) / "report"),
                "--duration-s", "8",
            ])
    finally:
        wrapper._load_login = original_load_login
        wrapper._authenticate = original_authenticate
        wrapper._library_items = original_library_items
        wrapper.subprocess.run = original_run

    assert result == 0
    assert len(launched) == 1
    input_index = launched[0].index("--input")
    assert "/Videos/safe-item/stream" in launched[0][input_index + 1]


def test_cli_blocks_inaccessible_keep_awake_before_launch(tmp_path=None):
    if tmp_path is None:
        with tempfile.TemporaryDirectory() as directory:
            return test_cli_blocks_inaccessible_keep_awake_before_launch(Path(directory))
    fake_mpv = tmp_path / "mpv"
    fake_mpv.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    fake_mpv.chmod(0o700)
    fake_nvidia = tmp_path / "nvidia-smi"
    fake_nvidia.write_text(
        "#!/usr/bin/env python3\n"
        "print('NVIDIA GeForce RTX 5070 Ti, 595.84, 16303')\n",
        encoding="utf-8",
    )
    fake_nvidia.chmod(0o700)
    fake_inhibit = tmp_path / "systemd-inhibit"
    fake_inhibit.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('Failed to inhibit: Access denied', file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    fake_inhibit.chmod(0o700)
    environment = os.environ.copy()
    environment["PATH"] = str(tmp_path) + os.pathsep + environment.get("PATH", "")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/run-linux-disconnect-benchmark.py"),
            "--input",
            "/tmp/synthetic-input.mp4",
            "--output",
            str(tmp_path / "report"),
            "--cells",
            "1",
            "--duration-s",
            "0.1",
            "--mpv",
            str(fake_mpv),
            "--nvidia-smi",
            str(fake_nvidia),
            "--keep-awake",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload == {"reason": "keep_awake_unavailable", "status": "BLOCK"}


def test_cli_runs_headless_disconnect_gate_and_redacts_source(tmp_path=None):
    if tmp_path is None:
        with tempfile.TemporaryDirectory() as directory:
            return test_cli_runs_headless_disconnect_gate_and_redacts_source(Path(directory))
    repo_root = Path(__file__).resolve().parents[1]
    fake_mpv = tmp_path / "mpv"
    fake_mpv.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "print('source=' + sys.argv[-1], flush=True)\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    fake_mpv.chmod(0o700)
    drm_root = tmp_path / "drm"
    connector = drm_root / "card0-HDMI-A-1"
    connector.mkdir(parents=True)
    (connector / "status").write_text("connected\n", encoding="utf-8")
    fake_nvidia = tmp_path / "nvidia-smi"
    status_path = connector / "status"
    marker_path = tmp_path / "nvidia.marker"
    fake_nvidia.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"status = Path({str(status_path)!r})\n"
        f"marker = Path({str(marker_path)!r})\n"
        "if not marker.exists():\n"
        "    marker.write_text('seen', encoding='utf-8')\n"
        "    status.write_text('disconnected\\n', encoding='utf-8')\n"
        "print('NVIDIA GeForce RTX 5070 Ti, 595.84, 1, 2, 3, 4')\n",
        encoding="utf-8",
    )
    fake_nvidia.chmod(0o700)
    report = tmp_path / "report"
    source = "https://media.invalid/video.mp4?api_key=secret-token"

    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts/run-linux-disconnect-benchmark.py"),
            "--input",
            source,
            "--output",
            str(report),
            "--cells",
            "1",
            "--duration-s",
            "0.3",
            "--poll-s",
            "0.05",
            "--mpv",
            str(fake_mpv),
            "--nvidia-smi",
            str(fake_nvidia),
            "--drm-root",
            str(drm_root),
            "--require-disconnect",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert summary["headless_disconnect_resilience"] == "PASS"
    assert summary["presentation_gate"] == "BLOCK"
    artifact_text = "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in report.rglob("*")
        if path.is_file()
    )
    assert source not in artifact_text
    assert "secret-token" not in artifact_text


def run_all() -> int:
    tests = [
        test_drm_status_reads_connector_files_without_shell_tools,
        test_connector_summary_marks_all_disconnected_separately_from_unknown,
        test_mpv_command_is_display_independent_and_loops_one_input,
        test_input_labels_are_stable_and_do_not_expose_source,
        test_mpv_error_parser_separates_decoder_audio_and_transport_faults,
        test_summary_passes_decode_gate_when_kvm_disconnects_but_cells_survive,
        test_summary_blocks_early_cell_exit_even_without_display_evidence,
        test_preexisting_display_loss_is_not_counted_as_a_kvm_transition,
        test_summary_records_kvm_loss_and_recovery_interval,
        test_emby_wall_safe_selector_accepts_normalized_contract,
        test_emby_wall_safe_selector_rejects_hevc_source,
        test_emby_wall_safe_selector_rejects_heavy_source,
        test_emby_wrapper_runs_non_wall_safe_item_instead_of_blocking,
    ]
    if sys.platform.startswith("linux"):
        tests.extend([
            test_cli_blocks_inaccessible_keep_awake_before_launch,
            test_cli_runs_headless_disconnect_gate_and_redacts_source,
        ])
    else:
        print("  SKIP  Linux CLI integration tests — Linux-only process/DRM contracts")
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures} passed, {failures} failed")
    return failures


if __name__ == "__main__":
    raise SystemExit(run_all())
