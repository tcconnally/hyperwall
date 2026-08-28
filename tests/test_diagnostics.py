"""Pure-logic tests for the no-image soak diagnostics.

The live runner is intentionally exercised on the target Mac. These tests
pin the parser, redaction, and gate contract without importing PyQt, mpv, or
making network requests.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperwall.diagnostics import (  # noqa: E402
    analyze_run,
    parse_app_log,
    parse_soak_jsonl,
    redact_text,
)


def _load_runner_module():
    import importlib.util

    path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
    )
    spec = importlib.util.spec_from_file_location("runner_test_module", path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def test_runner_disables_image_capture_and_has_no_capture_commands():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "image_capture" in source
    assert "screencapture" not in source
    assert "--skip-live" in source
    assert "--expected-cells" in source
    assert "INCOMPLETE" in source


def test_runner_records_only_safe_environment_fields():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "_safe_env_manifest" in source
    assert "os.environ" in source


def test_runner_has_safe_preflight_and_manual_wizard_notice():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "def _preflight" in source
    assert "SetupWizard acceptance" in source


def test_runner_validates_endpoint_before_network_work():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "_validate_endpoint" in source
    assert "parts.port" in source
    assert "_validate_endpoint(_configured_url())" in source
    assert "source-health.json" in source
    assert "all(isinstance(c, dict) and c.get(\"ok\") for c in checks)" in source
    assert "parser.error(str(exc))" in source
    assert "if not args.skip_source_health" in source
    assert "ProxyHandler({})" in source
    assert "_NoRedirect" in source


def test_runner_does_not_dump_config_or_full_environment():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "config.ini" in source
    assert "dict(os.environ)" not in source


def test_runner_keeps_live_artifact_directories_private():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "mode=0o700" in source
    assert "_force_private_permissions" in source
    assert "_install_signal_cleanup" in source


def test_runner_does_not_silently_ignore_permission_failures():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "except OSError" not in source


def test_process_group_cleanup_falls_back_after_permission_denied():
    from unittest import mock

    runner = _load_runner_module()
    process = mock.Mock()
    process.pid = 1234
    process.wait.return_value = None
    with (
        mock.patch.object(runner, "_group_exists", side_effect=[True, False, False]),
        mock.patch.object(
            runner.os,
            "killpg",
            side_effect=PermissionError(1, "Operation not permitted"),
            create=True,
        ),
        mock.patch.object(runner.signal, "SIGKILL", new=9, create=True),
    ):
        runner._terminate_process_group(process, process.pid)

    process.terminate.assert_called_once_with()

def test_permission_enforcement_raises_on_chmod_failure():
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py")
    spec = importlib.util.spec_from_file_location("runner_permission_test", path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    original = runner.Path.chmod
    try:
        runner.Path.chmod = lambda self, mode: (_ for _ in ()).throw(OSError("denied"))
        try:
            runner._force_private_permissions(Path(tempfile.gettempdir()))
        except OSError:
            pass
        else:
            raise AssertionError("chmod failure must block")
    finally:
        runner.Path.chmod = original


def test_runner_writes_preflight_and_health_artifacts_privately():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "preflight.json" in source
    assert "source-health.json" in source
    assert "redacted_artifacts" in source


def test_source_health_records_backend_without_credentials():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "_configured_backend" in source
    assert "endpoint_hash" in source


def test_source_health_records_latency_without_response_body():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "latency_ms" in source


def test_emby_source_health_uses_public_info_without_unrelated_health_probe():
    runner = _load_runner_module()
    calls = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout))
            return _Response()

    original_url = runner._configured_url
    original_backend = runner._configured_backend
    original_builder = runner.urllib.request.build_opener
    try:
        runner._configured_url = lambda: "http://emby.example:8096"
        runner._configured_backend = lambda: "emby"
        runner.urllib.request.build_opener = lambda *_args: _Opener()
        result = runner._source_health(Path("."), 1.0)
    finally:
        runner._configured_url = original_url
        runner._configured_backend = original_backend
        runner.urllib.request.build_opener = original_builder

    assert result["status"] == "PASS"
    assert [url.rsplit("/", 1)[-1] for url, _ in calls] == ["Public"]
    assert [check["path"] for check in result["checks"]] == [
        "/System/Info/Public"
    ]


def test_runner_metadata_records_effective_phase_environment():
    runner = _load_runner_module()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory, "runner.json")
        runner._write_run_metadata(
            path,
            phase="phase-01-no",
            decoder="no",
            minutes=5,
            dwell=20,
            env={
                "HYPERWALL_HWDEC": "no",
                "HYPERWALL_CACHE_BUDGET_MB": "1024",
                "HYPERWALL_STABLE_DIRECT_ONLY": "on",
                "HYPERWALL_STABLE_MAX_FPS": "30",
                "HYPERWALL_STABLE_MAX_BITRATE_MBPS": "20",
                "UNSAFE_SECRET": "must-not-be-recorded",
            },
        )
        payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["environment"]["HYPERWALL_HWDEC"] == "no"
    assert payload["environment"]["HYPERWALL_CACHE_BUDGET_MB"] == "1024"
    assert payload["environment"]["HYPERWALL_STABLE_DIRECT_ONLY"] == "on"
    assert payload["environment"]["HYPERWALL_STABLE_MAX_FPS"] == "30"
    assert payload["environment"]["HYPERWALL_STABLE_MAX_BITRATE_MBPS"] == "20"
    assert "UNSAFE_SECRET" not in payload["environment"]



def test_runner_metadata_records_expected_cell_count():
    runner = _load_runner_module()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory, "runner.json")
        runner._write_run_metadata(
            path,
            phase="phase-01-no",
            decoder="no",
            minutes=5,
            dwell=20,
            expected_cells=4,
            env={},
        )
        payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["expected_cells"] == 4


def test_analyze_run_blocks_when_expected_cell_count_mismatches():
    cells = [
        {"cell": index, "totals": {}, "info": {}, "freezes": 0, "freeze_seconds": 0}
        for index in range(2)
    ]
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text("ready" + chr(10), encoding="utf-8")
        Path(directory, "hyperwall_stats_a.json").write_text(
            json.dumps({"cells": cells}), encoding="utf-8"
        )
        result = analyze_run(directory, expected_cells=4)

    assert result["stats"]["n_cells"] == 2
    assert result["gates"]["cell_count"]["status"] == "BLOCK"
    assert result["gates"]["cell_count"]["value"] == {
        "expected": 4,
        "observed": 2,
    }


def test_analyze_run_accepts_matching_expected_cell_count():
    cells = [
        {"cell": index, "totals": {}, "info": {}, "freezes": 0, "freeze_seconds": 0}
        for index in range(2)
    ]
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text("ready" + chr(10), encoding="utf-8")
        Path(directory, "hyperwall_stats_a.json").write_text(
            json.dumps({"cells": cells}), encoding="utf-8"
        )
        result = analyze_run(directory, expected_cells=2)

    assert result["stats"]["n_cells"] == 2
    assert result["gates"]["cell_count"]["status"] == "PASS"


def test_analyze_run_blocks_when_active_duration_is_short():
    cells = [
        {"cell": 0, "totals": {}, "info": {}, "freezes": 0, "freeze_seconds": 0}
    ]
    records = [
        {"event": "start", "baseline": {"ws_mb": 1}},
        {"event": "sample", "wall_seconds": 1200, "resources": {"ws_mb": 1}},
        {
            "event": "finish",
            "wall_seconds": 2673,
            "resources": {"ws_mb": 1},
            "invariant_violations": 0,
        },
    ]
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text("ready\n", encoding="utf-8")
        Path(directory, "hyperwall_stats_a.json").write_text(
            json.dumps({"cells": cells}), encoding="utf-8"
        )
        Path(directory, "hyperwall_soak_a.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        result = analyze_run(directory, expected_duration_seconds=3600)

    gate = result["gates"]["duration_coverage"]
    assert gate["status"] == "BLOCK"
    assert gate["value"]["observed_seconds"] == 2673
    assert gate["value"]["expected_seconds"] == 3600


def test_runner_passes_expected_duration_to_analysis():
    runner = _load_runner_module()
    assert runner.__file__ is not None
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "expected_duration_seconds=args.minutes * 60" in source


def test_repo_tests_do_not_inherit_soak_overrides():
    runner = _load_runner_module()
    observed_envs = []

    def fake_run(command, *, cwd=runner.ROOT, output=None, env=None):
        observed_envs.append(dict(env) if env is not None else None)
        return 0

    original_run = runner._run
    names = (
        "HYPERWALL_CACHE_BUDGET_MB",
        "HYPERWALL_DEMUXER_PER_CELL_MB",
        "HYPERWALL_HWDEC",
    )
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = "soak-override"
        runner._run = fake_run
        with tempfile.TemporaryDirectory() as directory:
            assert runner._run_repo_tests(Path(directory), Path(directory)) == 0
    finally:
        runner._run = original_run
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    assert len(observed_envs) == 2
    assert all(env is not None for env in observed_envs)
    assert all(
        not any(name.startswith("HYPERWALL_") for name in env)
        for env in observed_envs
    )



def test_parse_app_log_counts_direct_and_transcode_plan_decisions():
    parsed = parse_app_log(
        chr(10).join(
            [
                "INFO Playback plan: DIRECT server=direct",
                "INFO Playback plan: DIRECT/prefetch server=direct",
                "INFO Playback plan: TRANSCODE server=server_transcode",
                "INFO Playback plan: TRANSCODE/prefetch server=server_transcode",
            ]
        )
    )

    assert parsed["playback_plan_counts"] == {
        "direct": 1,
        "direct_prefetch": 1,
        "server_transcode": 1,
        "server_transcode_prefetch": 1,
    }


def test_shutdown_stall_is_not_used_as_playback_responsiveness_failure():
    parsed = parse_app_log(
        "\n".join(
            [
                "[12:00:00] WARNING PERF loop stall: main thread blocked ~180ms",
                "[12:00:01] INFO Shutdown requested.",
                "[12:00:02] WARNING PERF loop stall: main thread blocked ~1062ms",
            ]
        )
    )

    assert parsed["max_loop_stall_ms"] == 180.0
    assert parsed["max_shutdown_loop_stall_ms"] == 1062.0
    assert parsed["shutdown_loop_stalls"] == 1


def test_peak_rss_growth_is_investigation_warning_not_leak_proof():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text(
            "[12:00:00] INFO ready\n", encoding="utf-8"
        )
        Path(directory, "hyperwall_stats_a.json").write_text(
            json.dumps(
                {
                    "cells": [
                        {
                            "cell": 0,
                            "totals": {},
                            "info": {},
                            "freezes": 0,
                            "freeze_seconds": 0,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        Path(directory, "hyperwall_soak_a.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "event": "start",
                            "baseline": {"ws_mb": 234, "ws_metric": "peak_rss_mb"},
                        }
                    ),
                    json.dumps(
                        {
                            "event": "sample",
                            "wall_seconds": 1,
                            "resources": {"ws_mb": 2000, "ws_metric": "peak_rss_mb"},
                        }
                    ),
                    json.dumps(
                        {
                            "event": "finish",
                            "wall_seconds": 2,
                            "resources": {"ws_mb": 3561, "ws_metric": "peak_rss_mb"},
                            "invariant_violations": 0,
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        result = analyze_run(directory)

    assert result["manifest"]["ws_metric"] == "peak_rss_mb"
    assert result["gates"]["working_set_growth_mb"]["status"] == "WARNING"


def test_runner_refuses_startup_cleanup_mode():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "cleanup_on_startup" in source
    assert "Refusing diagnostic run" in source


def test_runner_uses_no_image_capture_tools():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    for forbidden in ("screencapture", "AVFoundation", "QWidget.grab", "framebuffer"):
        assert forbidden not in source


def test_runner_can_import_without_optional_gui_dependencies():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "from hyperwall.diagnostics import analyze_run, redact_tree" in source
    assert "--skip-live" in source


def test_missing_run_log_is_warning_not_false_pass():
    with tempfile.TemporaryDirectory() as directory:
        result = analyze_run(directory)
        assert result["verdict"] == "WARNING"
        assert result["presentation_quality"] == "unmeasured_without_capture"


def test_empty_log_and_missing_stats_block_complete_run():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text("", encoding="utf-8")
        result = analyze_run(directory)
        assert result["gates"]["log_presence"]["status"] == "BLOCK"
        assert result["gates"]["stats_presence"]["status"] == "BLOCK"
        assert result["verdict"] == "BLOCK"


def test_stats_schema_requires_cells():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text("ready\n", encoding="utf-8")
        Path(directory, "hyperwall_stats_a.json").write_text("{\"x\": 1}\n", encoding="utf-8")
        result = analyze_run(directory)
        assert result["gates"]["stats_presence"]["status"] == "BLOCK"


def test_stats_schema_rejects_boolean_negative_and_missing_cell_fields():
    for cell in (
        {"cell": True, "totals": {}, "info": {}, "freezes": 0, "freeze_seconds": 0},
        {"cell": -1, "totals": {}, "info": {}, "freezes": 0, "freeze_seconds": 0},
        {"cell": 0, "totals": {}, "info": {}},
    ):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "hyperwall.log").write_text("ready\n", encoding="utf-8")
            Path(directory, "hyperwall_stats_a.json").write_text(
                json.dumps({"cells": [cell]}), encoding="utf-8"
            )
            result = analyze_run(directory)
            assert result["gates"]["stats_presence"]["status"] == "BLOCK"


def test_sample_without_required_shape_blocks_complete_run():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text("ready\n", encoding="utf-8")
        Path(directory, "hyperwall_stats_a.json").write_text(
            json.dumps({"cells": [{"cell": 0, "totals": {}, "info": {}}]}), encoding="utf-8"
        )
        Path(directory, "hyperwall_soak_a.jsonl").write_text(
            "\n".join([
                json.dumps({"event": "start", "baseline": {"ws_mb": 1}}),
                json.dumps({"event": "sample"}),
                json.dumps({"event": "finish", "wall_seconds": 2, "resources": {"ws_mb": 1}, "invariant_violations": 0}),
            ]) + "\n", encoding="utf-8"
        )
        result = analyze_run(directory)
        assert result["gates"]["required_events"]["status"] == "BLOCK"


def test_sample_resource_values_are_strictly_validated():
    bad_resources = ({}, {"ws_mb": "NaN"}, {"ws_mb": -1}, {"ws_mb": True}, {"private_mb": "bad"})
    for resources in bad_resources:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "hyperwall.log").write_text("ready\n", encoding="utf-8")
            Path(directory, "hyperwall_stats_a.json").write_text(
                json.dumps({"cells": [{"cell": 0, "totals": {}, "info": {}, "freezes": 0, "freeze_seconds": 0}]}),
                encoding="utf-8",
            )
            records = [
                {"event": "start", "baseline": {"ws_mb": 1}},
                {"event": "sample", "wall_seconds": 1, "resources": resources},
                {"event": "finish", "wall_seconds": 2, "resources": {"ws_mb": 1}, "invariant_violations": 0},
            ]
            Path(directory, "hyperwall_soak_a.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
            )
            result = analyze_run(directory)
            assert result["gates"]["required_events"]["status"] == "BLOCK"


def test_duplicate_lifecycle_events_block_complete_run():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text("ready\n", encoding="utf-8")
        Path(directory, "hyperwall_stats_a.json").write_text(
            json.dumps({"cells": [{"cell": 0, "totals": {}, "info": {}, "freezes": 0, "freeze_seconds": 0}]}),
            encoding="utf-8",
        )
        records = [
            {"event": "start", "baseline": {"ws_mb": 1}},
            {"event": "start", "baseline": {"ws_mb": 1}},
            {"event": "sample", "wall_seconds": 1, "resources": {"ws_mb": 1}},
            {"event": "finish", "wall_seconds": 2, "resources": {"ws_mb": 1}, "invariant_violations": 0},
            {"event": "finish", "wall_seconds": 2, "resources": {"ws_mb": 1}, "invariant_violations": 0},
        ]
        Path(directory, "hyperwall_soak_a.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
        )
        result = analyze_run(directory)
        assert result["verdict"] == "BLOCK"
        assert result["gates"]["manifest_shape"]["status"] == "BLOCK"


def test_runner_skip_live_is_incomplete_not_pass():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    assert "def _aggregate_verdict" in source


def test_runner_aggregates_warning_without_promoting_it_to_block():
    runner = _load_runner_module()
    assert runner._aggregate_verdict(
        incomplete=False,
        failures=0,
        phase_results=[{"verdict": "WARNING"}],
        auxiliary_statuses=["PASS"],
    ) == "WARNING"
    assert runner._aggregate_verdict(
        incomplete=False,
        failures=0,
        phase_results=[{"verdict": "PASS"}],
        auxiliary_statuses=["PASS"],
    ) == "PASS"
    assert runner._aggregate_verdict(
        incomplete=False,
        failures=1,
        phase_results=[{"verdict": "WARNING"}],
        auxiliary_statuses=["PASS"],
    ) == "BLOCK"
    assert runner._aggregate_verdict(
        incomplete=True,
        failures=0,
        phase_results=[],
        auxiliary_statuses=[],
    ) == "INCOMPLETE"


def test_redaction_masks_stream_credentials_and_host_identifiers():
    auth_scheme = "Be" + "arer"
    credential = "bearer" + "789"
    raw = (
        "GET /Videos/1/stream?api_key=secret123&PlaySessionId=session456 "
        + "Authorization: " + auth_scheme + " " + credential + "\n"
        + "Serial Number (system): SERIAL\n"
        + "Hardware UUID: UUID\n"
    )
    safe = redact_text(raw)
    assert "secret123" not in safe
    assert "session456" not in safe
    assert credential not in safe
    assert "Authorization: " + auth_scheme + " " + credential not in safe
    assert "SERIAL" not in safe
    assert "Hardware UUID: UUID" not in safe
    assert "api_key=<redacted>" in safe
    assert "PlaySessionId=<redacted>" in safe


def test_redaction_masks_real_authorization_value():
    value = "".join(("Bearer", " ", "real-bearer-value"))
    safe = redact_text("Authorization: " + value)
    assert "real-bearer-value" not in safe


def test_redaction_masks_authorization_equals_and_basic():
    safe = redact_text(
        "Authorization=Bearer real-eq-token\n"
        "Authorization: Basic real-basic-token\n"
    )
    assert "real-eq-token" not in safe
    assert "real-basic-token" not in safe


def test_redaction_masks_malformed_jsonl_credential_line():
    from hyperwall.diagnostics import write_redacted_copy

    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
        src = Path(source, "events.jsonl")
        src.write_text('{"api_key":"unredacted"\n', encoding="utf-8")
        try:
            write_redacted_copy(src, Path(target, "events.jsonl"))
        except ValueError:
            pass
        else:
            raise AssertionError("malformed credential record must not be copied")


def test_redaction_rejects_malformed_jsonl_authorization_key():
    from hyperwall.diagnostics import write_redacted_copy

    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
        src = Path(source, "events.jsonl")
        src.write_text('{"Authorization": "Bearer malformed-secret"\n', encoding="utf-8")
        try:
            write_redacted_copy(src, Path(target, "events.jsonl"))
        except ValueError:
            pass
        else:
            raise AssertionError("malformed authorization record must not be copied")


def test_redaction_rejects_all_malformed_sensitive_json_keys():
    from hyperwall.diagnostics import write_redacted_copy

    keys = ("authToken", "clientSecret", "apiKey", "X-Emby-Token", "accessToken")
    for key in keys:
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            src = Path(source, "events.jsonl")
            src.write_text(f'{{"{key}": "MALFORMED_SECRET"\n', encoding="utf-8")
            try:
                write_redacted_copy(src, Path(target, "events.jsonl"))
            except ValueError:
                continue
            raise AssertionError(f"malformed {key} record must not be copied")


def test_redaction_masks_home_paths():
    safe = redact_text("path=/Users/thomas/hyperwall/report.log")
    assert "/Users/thomas" not in safe
    assert "/Users/<user>/hyperwall/report.log" in safe


def test_windows_private_permissions_uses_owner_acl():
    from unittest import mock

    from hyperwall import diagnostics

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        with (
            mock.patch.object(diagnostics.os, "name", "nt"),
            mock.patch.dict(os.environ, {"USERNAME": "fixture-user"}, clear=False),
            mock.patch.object(
                diagnostics.subprocess, "run", return_value=mock.Mock(returncode=0)
            ) as run,
        ):
            diagnostics.force_private_permissions(target, 0o700)

        assert run.call_args.args[0] == [
            "icacls",
            str(target),
            "/inheritance:r",
            "/grant:r",
            "fixture-user:F",
        ]
        assert run.call_args.kwargs["check"] is False


def test_redacted_tree_excludes_unexpected_binary_artifacts():
    from hyperwall.diagnostics import redact_tree

    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
        Path(source, "safe.log").write_text("api_key=secret\n", encoding="utf-8")
        Path(source, "capture.png").write_bytes(b"not copied")
        redact_tree(source, target)
        assert Path(target, "safe.log").exists()
        assert not Path(target, "capture.png").exists()


def test_redacted_tree_refuses_source_or_nested_destination():
    from hyperwall.diagnostics import redact_tree

    with tempfile.TemporaryDirectory() as source:
        try:
            redact_tree(source, source)
        except ValueError:
            pass
        else:
            raise AssertionError("source destination should be rejected")


def test_redacted_tree_skips_symlinked_files():
    from hyperwall.diagnostics import redact_tree

    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
        Path(source, "real.log").write_text("safe\n", encoding="utf-8")
        Path(source, "leak.log").symlink_to("/etc/hosts")
        redact_tree(source, target)
        assert not Path(target, "leak.log").exists()


def test_redacted_tree_rejects_destination_symlinks():
    from hyperwall.diagnostics import redact_tree

    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
        outside = Path(target, "outside")
        outside.mkdir()
        link = Path(target, "linked")
        link.symlink_to(outside, target_is_directory=True)
        try:
            redact_tree(source, link)
        except ValueError:
            pass
        else:
            raise AssertionError("destination symlink must be rejected")


def test_redaction_rejects_symlinked_parent_components():
    from hyperwall.diagnostics import redact_tree, write_redacted_copy

    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        source = root_path / "source"
        source.mkdir()
        src = source / "safe.log"
        src.write_text("safe\n", encoding="utf-8")
        outside = root_path / "outside"
        outside.mkdir()
        parent_link = root_path / "parent-link"
        parent_link.symlink_to(outside, target_is_directory=True)
        try:
            write_redacted_copy(src, parent_link / "copy.log")
        except ValueError:
            pass
        else:
            raise AssertionError("symlinked destination parent must be rejected")


def test_redaction_rejects_symlinked_source_root():
    from hyperwall.diagnostics import redact_tree

    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        real = root_path / "real"
        real.mkdir()
        link = root_path / "link"
        link.symlink_to(real, target_is_directory=True)
        try:
            redact_tree(link, root_path / "out")
        except ValueError:
            pass
        else:
            raise AssertionError("symlinked source root must be rejected")



def test_macos_private_var_alias_is_allowed_but_other_symlinks_are_not():
    from unittest import mock

    from hyperwall import diagnostics

    def fake_lstat(path):
        if PurePosixPath(path) == PurePosixPath("/var"):
            return type("Stat", (), {"st_mode": diagnostics.stat.S_IFLNK})()
        raise FileNotFoundError(path)

    for target, rejected in (("/private/var", False), ("/private/other", True)):
        with (
            mock.patch.object(diagnostics.sys, "platform", "darwin"),
            mock.patch.object(diagnostics, "Path", PurePosixPath),
            mock.patch.object(
                diagnostics.os.path, "abspath", return_value="/var/folders/test"
            ),
            mock.patch.object(diagnostics.os.path, "realpath", return_value=target),
            mock.patch.object(diagnostics.os, "lstat", side_effect=fake_lstat),
        ):
            try:
                resolved = diagnostics._reject_symlink_components("/var/folders/test")
            except ValueError as exc:
                if not rejected:
                    raise
                assert "path contains symlink component: /var" in str(exc)
            else:
                if rejected:
                    raise AssertionError("non-system /var symlink must be rejected")
                assert resolved == PurePosixPath("/var/folders/test")


def test_redacted_tree_masks_values_in_copied_text():
    from hyperwall.diagnostics import redact_tree

    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
        Path(source, "safe.log").write_text("api_key=secret\n", encoding="utf-8")
        redact_tree(source, target)
        assert "secret" not in Path(target, "safe.log").read_text(encoding="utf-8")


def test_redacted_json_is_recursive():
    from hyperwall.diagnostics import write_redacted_copy

    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
        Path(source, "stats.json").write_text(
            json.dumps({"nested": {"url": "?api_key=secret"}}),
            encoding="utf-8",
        )
        write_redacted_copy(Path(source, "stats.json"), Path(target, "stats.json"))
        content = Path(target, "stats.json").read_text(encoding="utf-8")
        assert "secret" not in content
        assert "<redacted>" in content


def test_redaction_masks_endpoint_query_credentials():
    safe = redact_text("http://host:8096/stream?api_key=secret&x=1")
    assert "secret" not in safe
    assert "api_key=<redacted>" in safe


def test_redaction_masks_jsonl_records():
    from hyperwall.diagnostics import write_redacted_copy

    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
        src = Path(source, "events.jsonl")
        src.write_text(
            json.dumps({"AccessToken": "secret-token"}) + "\n",
            encoding="utf-8",
        )
        write_redacted_copy(src, Path(target, "events.jsonl"))
        assert "secret-token" not in Path(target, "events.jsonl").read_text(
            encoding="utf-8"
        )


def test_redaction_masks_access_tokens_and_home_paths_in_json():
    from hyperwall.diagnostics import redact_json_value

    safe = redact_json_value({
        "token": "Authorization: Bearer " + "bearer789",
        "path": "/Users/thomas/hyperwall/config.ini",
    })
    assert "bearer789" not in safe["token"]
    assert "/Users/thomas" not in safe["path"]


def test_redaction_masks_named_sensitive_json_keys():
    from hyperwall.diagnostics import redact_json_value

    safe = redact_json_value({
        "AccessToken": "secret-token",
        "X-Emby-Token": "secret-token",
        "password": "secret-password",
    })
    assert safe == {
        "AccessToken": "<redacted>",
        "X-Emby-Token": "<redacted>",
        "password": "<redacted>",
    }


def test_redaction_masks_embedded_sensitive_fields_and_headers():
    safe = redact_text(
        "X-Emby-Token: secret-token\n"
        "AccessToken=secret-token\n"
        "password=secret-password\n"
        "token=secret-token\n"
        "secret=secret-value\n"
    )
    assert "secret-token" not in safe
    assert "secret-password" not in safe
    assert "secret-value" not in safe


def test_malformed_invariant_value_blocks():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text("[12:00:00] INFO ready\n", encoding="utf-8")
        Path(directory, "hyperwall_soak_a.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"event": "start", "baseline": {"ws_mb": 1}}),
                    json.dumps({"event": "sample", "wall_seconds": 1}),
                    json.dumps({
                        "event": "finish",
                        "wall_seconds": 2,
                        "resources": {"ws_mb": 1},
                        "invariant_violations": ["bad"],
                    }),
                ]
            ) + "\n",
            encoding="utf-8",
        )
        result = analyze_run(directory)
        assert result["gates"]["invariant_violations"]["status"] == "BLOCK"
        assert result["verdict"] == "BLOCK"


def test_failure_classes_are_gated():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text(
            "[12:00:00] ERROR Playback error\n", encoding="utf-8"
        )
        result = analyze_run(directory)
        assert result["gates"]["playback_errors"]["status"] == "BLOCK"
        assert result["verdict"] == "BLOCK"


def test_analyze_run_keeps_missing_manifest_metrics_as_warning():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text(
            "[12:00:00] INFO ready\n", encoding="utf-8"
        )
        result = analyze_run(directory)
        assert result["gates"]["working_set_growth_mb"]["status"] == "WARNING"
        assert result["gates"]["invariant_violations"]["status"] == "WARNING"


def test_malformed_manifest_is_blocked_and_bad_shapes_do_not_raise():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text(
            "[12:00:00] INFO ready\n", encoding="utf-8"
        )
        Path(directory, "hyperwall_soak_a.jsonl").write_text(
            json.dumps({"event": "start", "baseline": []}) + "\n"
            + "not-json\n",
            encoding="utf-8",
        )
        result = analyze_run(directory)
        assert result["verdict"] == "BLOCK"
        assert result["gates"]["manifest_shape"]["status"] == "BLOCK"


def test_non_object_jsonl_record_is_malformed():
    parsed = parse_soak_jsonl("[]\n" + json.dumps({"event": "finish"}) + "\n")
    assert parsed["malformed_records"] == 1


def test_parse_app_log_counts_independent_failure_classes_and_stats():
    log = "\n".join(
        [
            "[12:00:00] WARNING FREEZE: 4.5s cache starvation on 'x' (buffering-state=100)",
            "[12:00:01] ERROR mpv[ffmpeg] tcp: Connection to tcp://host:8096 failed: Connection refused",
            "[12:00:02] WARNING mpv[ffmpeg/demuxer] hls: Failed to open segment 3 of playlist 0",
            "[12:00:03] ERROR mpv[ffmpeg/video] h264: hardware accelerator failed to decode picture",
            "[12:00:04] WARNING mpv[ffmpeg/video] h264: vt decoder cb: output image buffer is null: -12909, reconfig 1",
            "[12:00:05] WARNING mpv[cplayer] Audio/Video desynchronisation detected!",
            "[12:00:06] WARNING mpv[cplayer] Audio device underrun detected.",
            "[12:00:07] WARNING PERF slow slot wall.next_video: 599ms",
            "[12:00:08] WARNING PERF loop stall: main thread blocked ~600ms",
            "[12:00:09] INFO STATS cell 0  drop=12  mistimed=0  vo-delayed=0  dec-drop=0 freezes=1(4.5s) postseek=0 hwdec=videotoolbox fps=30 bitrate=1000000",
        ]
    )
    parsed = parse_app_log(log)
    assert parsed["freeze_count"] == 1
    assert parsed["freeze_seconds"] == 4.5
    assert parsed["connection_refused"] == 1
    assert parsed["hls_segment_failures"] == 1
    assert parsed["hardware_decode_failures"] == 1
    assert parsed["decoder_buffer_warnings"] == 1
    assert parsed["av_desync"] == 1
    assert parsed["audio_underrun"] == 1
    assert parsed["max_loop_stall_ms"] == 600.0
    assert parsed["max_slow_slot_ms"] == 599.0
    assert parsed["stats"][0]["freeze_seconds"] == 4.5


def test_malformed_log_numbers_do_not_raise_or_create_failures():
    parsed = parse_app_log(
        "FREEZE: nope s\n"
        "PERF loop stall: main thread blocked ~nope ms\n"
        "PERF slow slot wall.next: nope ms\n"
        "STATS cell 0 drop=nan freezes=1(nan s) hwdec=x fps=nan bitrate=nan\n"
    )
    assert parsed["freeze_count"] == 0
    assert parsed["max_loop_stall_ms"] == 0.0
    assert parsed["max_slow_slot_ms"] == 0.0
    assert parsed["stats"] == []
    assert parsed["malformed_numeric_fields"] >= 4


def test_malformed_log_numbers_block_complete_run():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text(
            "ready\nFREEZE: nope s\nPERF loop stall: main thread blocked ~NaN ms\n",
            encoding="utf-8",
        )
        Path(directory, "hyperwall_stats_a.json").write_text(
            json.dumps({"cells": [{"cell": 0, "totals": {}, "info": {}}]}), encoding="utf-8"
        )
        Path(directory, "hyperwall_soak_a.jsonl").write_text(
            "\n".join([
                json.dumps({"event": "start", "baseline": {"ws_mb": 1}}),
                json.dumps({"event": "sample", "wall_seconds": 1, "resources": {"ws_mb": 1}}),
                json.dumps({"event": "finish", "wall_seconds": 2, "resources": {"ws_mb": 1}, "invariant_violations": 0}),
            ]) + "\n", encoding="utf-8"
        )
        result = analyze_run(directory)
        assert result["gates"]["malformed_numeric_fields"]["status"] == "BLOCK"
        assert result["verdict"] == "BLOCK"


def test_parse_soak_jsonl_tracks_baseline_samples_and_finish():
    text = "\n".join(
        json.dumps(record)
        for record in [
            {"event": "start", "wall_seconds": 0.0, "baseline": {"ws_mb": 226, "threads": 1, "current_ws_mb": 220, "current_ws_metric": "resident_rss_mb"}},
            {"event": "sample", "wall_seconds": 60.0, "resources": {"ws_mb": 1315, "current_ws_mb": 700, "current_ws_metric": "resident_rss_mb", "threads": 9}},
            {"event": "finish", "wall_seconds": 120.0, "resources": {"ws_mb": 3312, "current_ws_mb": 900, "current_ws_metric": "resident_rss_mb", "threads": 9}, "invariant_violations": 0},
        ]
    )
    parsed = parse_soak_jsonl(text)
    assert parsed["sample_count"] == 1
    assert parsed["baseline_ws_mb"] == 226
    assert parsed["final_ws_mb"] == 3312
    assert parsed["working_set_growth_mb"] == 3086
    assert parsed["baseline_current_ws_mb"] == 220
    assert parsed["final_current_ws_mb"] == 900
    assert parsed["current_working_set_growth_mb"] == 680
    assert parsed["current_ws_metric"] == "resident_rss_mb"
    assert parsed["invariant_violations"] == 0
    assert parsed["duration_seconds"] == 120.0


def test_zero_invariant_finish_is_clean_when_required_evidence_exists():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "hyperwall.log").write_text("[1] INFO ready\n", encoding="utf-8")
        Path(directory, "hyperwall_stats_a.json").write_text("{\"cell\": 1}\n", encoding="utf-8")
        Path(directory, "hyperwall_soak_a.jsonl").write_text(
            "\n".join([
                json.dumps({"event": "start", "baseline": {"ws_mb": 1}}),
                json.dumps({"event": "sample", "wall_seconds": 1}),
                json.dumps({"event": "finish", "wall_seconds": 2, "resources": {"ws_mb": 1}, "invariant_violations": 0}),
            ]) + "\n",
            encoding="utf-8",
        )
        result = analyze_run(directory)
        assert result["gates"]["invariant_violations"]["status"] == "PASS"


def test_analyze_run_blocks_on_unacceptable_media_and_memory_results():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "hyperwall_soak_test.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"event": "start", "wall_seconds": 0, "baseline": {"ws_mb": 226, "threads": 1}})
                + "\n"
                + json.dumps({"event": "finish", "wall_seconds": 60, "resources": {"ws_mb": 2000, "threads": 9}, "invariant_violations": 0})
                + "\n"
            )
        with open(os.path.join(directory, "hyperwall.log"), "w", encoding="utf-8") as handle:
            handle.write(
                "[12:00:00] WARNING FREEZE: 4.5s cache starvation on 'x'\n"
                "[12:00:01] WARNING PERF loop stall: main thread blocked ~600ms\n"
                "[12:00:02] ERROR mpv[ffmpeg/video] h264: hardware accelerator failed to decode picture\n"
            )
        result = analyze_run(directory)
        assert result["verdict"] == "BLOCK"
        assert result["gates"]["freeze_count"]["status"] == "BLOCK"
        assert result["gates"]["working_set_growth_mb"]["status"] == "BLOCK"
        assert result["gates"]["max_loop_stall_ms"]["status"] == "BLOCK"


def test_runner_metadata_records_auto_transcode_mode():
    runner = _load_runner_module()
    manifest = runner._safe_env_manifest({"HYPERWALL_AUTO_TRANSCODE": "0"})
    assert manifest["HYPERWALL_AUTO_TRANSCODE"] == "0"


def test_phase_analysis_is_written_before_redaction():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "scripts", "run-soak-diagnostics.py"
        ),
        encoding="utf-8",
    ).read()
    start = source.index("def _analyze_phase")
    end = source.index("\ndef main", start)
    body = source[start:end]
    assert body.index("analysis_path.write_text") < body.index("_redact_phase")


def run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    raise SystemExit(run_all())
