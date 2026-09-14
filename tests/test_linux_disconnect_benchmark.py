"""Tests for the KVM/HDMI-disconnect-safe Linux benchmark contract."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
            "/tmp/wall-safe.mp4",
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
