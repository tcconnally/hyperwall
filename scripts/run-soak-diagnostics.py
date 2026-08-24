#!/usr/bin/env python3
"""Run Hyperwall's no-image macOS soak diagnostics as one command.

Phases:
  1. repository tests and static checks;
  2. source-health probe against HYPERWALL_SERVER_URL;
  3. one or more bounded live GUI soaks, optionally A/B'ing decoders;
  4. offline parsing, redaction, and machine-readable gate report.

The runner never captures screenshots, screen recordings, or image files. It
also never prints credentials. Live phases require the operator's configured
Hyperwall credentials and a manually usable macOS display session.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hyperwall.diagnostics import analyze_run, redact_tree, force_private_permissions  # noqa: E402


DEFAULT_DECODERS = ("videotoolbox", "videotoolbox-copy")
TEXT_ARTIFACTS = {"hyperwall.log", "run.env", "vm_stat.log", "nettop.log", "powermetrics.log"}
_SOURCE_HEALTH_PATHS = {
    "emby": ("/System/Info/Public",),
    "jellyfin": ("/health",),
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    output: Path | None = None,
    env: dict[str, str] | None = None,
) -> int:
    print("$ " + " ".join(command), flush=True)
    if output is None:
        return subprocess.run(command, cwd=cwd, env=env, check=False).returncode
    with output.open("w", encoding="utf-8") as handle:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        ).returncode


def _repo_test_env() -> dict[str, str]:
    """Run repository checks without live-soak configuration overrides."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("HYPERWALL_")
    }


def _run_repo_tests(root: Path, report_dir: Path) -> int:
    checks = [
        ([sys.executable, "tests/run_all.py"], report_dir / "repo-tests.log"),
        (
            [sys.executable, "-m", "compileall", "-q", "hyperwall", "tests"],
            report_dir / "compileall.log",
        ),
    ]
    repo_env = _repo_test_env()
    failures = 0
    for command, output in checks:
        failures += int(
            _run(command, cwd=root, output=output, env=repo_env) != 0
        )
    return failures


def _configured_url() -> str | None:
    override = os.environ.get("HYPERWALL_SERVER_URL", "").strip()
    if override:
        return override.rstrip("/")
    config = ROOT / "config.ini"
    if not config.exists():
        return None
    for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().lower().startswith("server_url") and "=" in line:
            value = line.split("=", 1)[1].strip()
            return value.rstrip("/") or None
    return None


def _configured_backend() -> str:
    config = ROOT / "config.ini"
    if not config.exists():
        return "emby"
    in_login = False
    for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_login = stripped.lower() == "[login]"
        elif in_login and stripped.lower().startswith("backend") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().lower() or "emby"
    return "emby"


def _configured_setting(name: str, default: str = "") -> str:
    config = ROOT / "config.ini"
    if not config.exists():
        return default
    in_settings = False
    for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_settings = stripped.lower() == "[settings]"
        elif in_settings and stripped.lower().startswith(name.lower()) and "=" in stripped:
            return stripped.split("=", 1)[1].strip()
    return default


def _source_health(root: Path, timeout: float) -> dict[str, object]:
    url = _configured_url()
    backend = _configured_backend()
    result: dict[str, object] = {
        "url_configured": bool(url),
        "backend": backend,
        "endpoint": _safe_endpoint_label(url),
        "endpoint_hash": (
            hashlib.sha256(url.encode()).hexdigest()[:12] if url else None
        ),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": [],
    }
    if not url:
        result["status"] = "WARNING"
        result["note"] = "No configured endpoint; source-health probe skipped."
        return result
    try:
        _validate_endpoint(url)
    except ValueError as exc:
        result["status"] = "BLOCK"
        result["note"] = str(exc)
        return result
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect()
    )
    for suffix in _SOURCE_HEALTH_PATHS.get(
        backend, ("/System/Info/Public",)
    ):
        target = url + suffix
        check: dict[str, object] = {"path": suffix}
        t0 = time.monotonic()
        try:
            request = urllib.request.Request(target, method="GET")
            with opener.open(request, timeout=timeout) as response:
                check["status_code"] = response.status
                check["ok"] = 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            check["status_code"] = exc.code
            check["ok"] = False
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.InvalidURL) as exc:
            check["ok"] = False
            check["error_type"] = type(exc).__name__
        check["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        result["checks"].append(check)
    checks = result["checks"] if isinstance(result["checks"], list) else []
    result["status"] = (
        "PASS"
        if all(isinstance(c, dict) and c.get("ok") for c in checks)
        else "BLOCK"
    )
    result["latency_ms"] = [
        check.get("latency_ms") for check in checks
        if isinstance(check, dict) and check.get("latency_ms") is not None
    ]
    return result


def _source_health_gate(health: dict[str, object]) -> str:
    """Return a gate status without treating an omitted endpoint as failure."""
    return str(health.get("status", "WARNING"))


def _preflight() -> dict[str, object]:
    """Capture safe capability facts without dumping credentials or devices."""
    commands = (
        "vm_stat", "memory_pressure", "nettop", "powermetrics", "ps",
        "vmmap", "footprint", "sw_vers", "system_profiler",
    )
    return {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "commands": {name: shutil.which(name) is not None for name in commands},
        "venv_python": (ROOT / ".venv" / "bin" / "python").exists(),
        "image_capture": False,
    }


def _safe_endpoint_label(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "<invalid-endpoint>"
    if parts.username or parts.password or parts.query:
        return f"{parts.scheme}://{parts.hostname or '<host>'}:{port or ''}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _validate_endpoint(url: str | None) -> None:
    if not url:
        return
    try:
        if any(character.isspace() or ord(character) < 32 for character in url):
            raise ValueError("endpoint must not contain whitespace or control characters")
        parts = urlsplit(url)
        _ = parts.port
    except ValueError as exc:
        raise ValueError("endpoint must contain a valid port") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("endpoint must be a valid HTTP(S) URL")
    if parts.port is None or not (1 <= parts.port <= 65535):
        raise ValueError("endpoint must contain a port from 1 to 65535")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("endpoint must not contain userinfo, query, or fragment credentials")


def _safe_env_manifest(env: object) -> dict[str, object]:
    keys = (
        "HYPERWALL_STATS", "HYPERWALL_PERFTRACE", "HYPERWALL_SOAK_MINUTES",
        "HYPERWALL_SOAK_DWELL_S", "HYPERWALL_SOAK_PROFILE", "HYPERWALL_HWDEC",
        "HYPERWALL_CACHE_BUDGET_MB", "HYPERWALL_DEMUXER_PER_CELL_MB",
        "HYPERWALL_NO_RELAUNCH", "HYPERWALL_NO_LOG_SETUP", "LC_NUMERIC",
    )
    values: dict[str, object] = dict(env) if isinstance(env, dict) else {}
    return {key: values.get(key) for key in keys}


def _force_private_permissions(path: Path) -> None:
    """Keep diagnostic manifests/logs private on shared machines."""
    mode = 0o700 if path.is_dir() else 0o600
    force_private_permissions(path, mode)


def _base_env(report_dir: Path, minutes: int, dwell: int) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "HYPERWALL_HWDEC",
        "HYPERWALL_VO",
        "HYPERWALL_GPU_API",
        "HYPERWALL_PROFILE",
    ):
        env.pop(key, None)
    env.update({
        "HYPERWALL_STATS": "1",
        "HYPERWALL_PERFTRACE": "1",
        "HYPERWALL_SOAK_MINUTES": str(minutes),
        "HYPERWALL_SOAK_DWELL_S": str(dwell),
        "HYPERWALL_SOAK_ACTIONS": "1",
        "HYPERWALL_SOAK_PROFILE": "audio",
        "HYPERWALL_SOAK_REPORT_DIR": str(report_dir),
        "HYPERWALL_SOAK_REPORT_ROOT": str(report_dir.parent),
        "HYPERWALL_NO_RELAUNCH": "1",
        "HYPERWALL_NO_LOG_SETUP": "1",
        "LC_NUMERIC": "C",
    })
    return env


def _write_run_metadata(
    path: Path,
    *,
    phase: str,
    decoder: str,
    minutes: int,
    dwell: int,
    env: dict[str, str],
) -> None:
    metadata = {
        "phase": phase,
        "decoder": decoder,
        "minutes": minutes,
        "dwell_seconds": dwell,
        "image_capture": False,
        "environment": _safe_env_manifest(env),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            capture_output=True, check=False,
        ).stdout.strip() or "unknown",
    }
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def _run_live_phase(
    phase_dir: Path,
    *,
    decoder: str,
    minutes: int,
    dwell: int,
    watchdog: int,
) -> int:
    phase_dir.mkdir(parents=True, exist_ok=True)
    _force_private_permissions(phase_dir)
    env = _base_env(phase_dir, minutes, dwell)
    env["HYPERWALL_HWDEC"] = decoder
    _write_run_metadata(
        phase_dir / "runner.json",
        phase=phase_dir.name,
        decoder=decoder,
        minutes=minutes,
        dwell=dwell,
        env=env,
    )
    _force_private_permissions(phase_dir / "runner.json")
    command = ["bash", "soak_wall.sh", str(minutes)]
    print(
        f"Starting phase {phase_dir.name}: decoder={decoder}, minutes={minutes}",
        flush=True,
    )
    console_path = phase_dir / "runner-console.log"
    process = None
    process_group_id = None
    try:
        with console_path.open("w", encoding="utf-8") as console:
            _force_private_permissions(console_path)
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=console,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            process_group_id = os.getpgid(process.pid)
            deadline = time.monotonic() + minutes * 60 + watchdog
            while True:
                code = process.poll()
                if code is not None:
                    # The shell can exit before descendants; always supervise
                    # and tear down the complete process group before return.
                    _terminate_process_group(process, process_group_id)
                    return code
                if time.monotonic() > deadline:
                    print(
                        f"Watchdog timeout for {phase_dir.name}; terminating process group.",
                        file=sys.stderr,
                    )
                    _terminate_process_group(process, process_group_id)
                    return 124
                time.sleep(2)
    except (KeyboardInterrupt, SystemExit):
        if process is not None:
            _terminate_process_group(process, process_group_id)
        raise


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _direct_process_signal(process: subprocess.Popen, sig: signal.Signals) -> None:
    try:
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        return
    except PermissionError as exc:
        print(
            f"WARNING: direct phase-leader signal {sig.name} failed: {exc}",
            file=sys.stderr,
        )


def _wait_for_process(process: subprocess.Popen, seconds: float) -> bool:
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    except (ChildProcessError, ProcessLookupError):
        return True
    return True


def _terminate_process_group(process: subprocess.Popen, pgid: int | None = None) -> None:
    pgid = process.pid if pgid is None else pgid
    group_signal_denied = False
    for sig, wait_seconds in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 5.0)):
        if group_signal_denied:
            _direct_process_signal(process, sig)
            if _wait_for_process(process, wait_seconds):
                break
            continue
        if _group_exists(pgid):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                group_signal_denied = True
                print(
                    f"WARNING: process-group signal {sig.name} denied for "
                    f"pgid={pgid}: {exc}; falling back to phase leader.",
                    file=sys.stderr,
                )
                _direct_process_signal(process, sig)
                if _wait_for_process(process, wait_seconds):
                    break
                continue
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and _group_exists(pgid):
            time.sleep(0.05)
        if not _group_exists(pgid):
            break
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    except (ChildProcessError, ProcessLookupError):
        pass


def _redact_phase(phase_dir: Path) -> Path:
    safe_dir = phase_dir.parent / (phase_dir.name + "-redacted")
    if safe_dir.exists():
        shutil.rmtree(safe_dir)
    redact_tree(phase_dir, safe_dir)
    return safe_dir


def _redact_root_artifacts(report_root: Path) -> Path:
    safe_dir = report_root.parent / (report_root.name + "-redacted")
    if safe_dir.exists():
        shutil.rmtree(safe_dir)
    redact_tree(report_root, safe_dir)
    return safe_dir


def _install_signal_cleanup() -> None:
    """Convert termination signals into KeyboardInterrupt for phase cleanup."""
    def handle_signal(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def _analyze_phase(phase_dir: Path) -> dict[str, object]:
    result = analyze_run(phase_dir)
    result["redacted_artifacts"] = str(_redact_phase(phase_dir))
    analysis_path = phase_dir / "analysis.json"
    analysis_path.write_text(
        json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
    )
    _force_private_permissions(analysis_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--minutes", type=int, default=10,
        help="Minutes per live phase (default: 10).",
    )
    parser.add_argument(
        "--dwell", type=int, default=20,
        help="Soak action dwell seconds (default: 20).",
    )
    parser.add_argument(
        "--watchdog-grace", type=int, default=90,
        help="Seconds beyond expected phase length before kill (default: 90).",
    )
    parser.add_argument(
        "--decoders", default=",".join(DEFAULT_DECODERS),
        help="Comma-separated decoder A/B phases; use one value to run one phase.",
    )
    parser.add_argument(
        "--report-root", default=str(ROOT / "soak_reports"),
        help="Artifact root (default: ./soak_reports).",
    )
    parser.add_argument(
        "--skip-repo-tests", action="store_true",
        help="Skip local pure-logic/static tests.",
    )
    parser.add_argument(
        "--skip-source-health", action="store_true",
        help="Skip the unauthenticated endpoint health probe.",
    )
    parser.add_argument(
        "--skip-live", action="store_true",
        help="Run repository/source checks only; do not launch the wall.",
    )
    args = parser.parse_args(argv)
    _install_signal_cleanup()
    if args.minutes < 1 or args.dwell < 0 or args.watchdog_grace < 1:
        parser.error("minutes must be >=1, dwell >=0, watchdog-grace >=1")

    if not args.skip_source_health:
        try:
            _validate_endpoint(_configured_url())
        except ValueError as exc:
            parser.error(str(exc))
    if _configured_setting("cleanup_on_startup", "false").lower() in {
        "true", "1", "yes",
    }:
        raise SystemExit("Refusing diagnostic run while cleanup_on_startup=true")
    if not args.skip_live and os.environ.get("HYPERWALL_SOAK_NONINTERACTIVE") != "1":
        print(
            "NOTICE: each live phase requires manual SetupWizard acceptance; "
            "HYPERWALL_SOAK_NONINTERACTIVE is not implemented in this checkout.",
            file=sys.stderr,
        )
    report_root = Path(args.report_root).expanduser().resolve() / _timestamp()
    report_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    _force_private_permissions(report_root)
    summary: dict[str, object] = {
        "image_capture": False,
        "report_root": str(report_root),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "repo_tests": {"status": "SKIP"},
        "source_health": {"status": "SKIP"},
        "phases": [],
    }
    preflight = _preflight()
    preflight_path = report_root / "preflight.json"
    preflight_path.write_text(
        json.dumps(preflight, indent=2) + "\n", encoding="utf-8"
    )
    _force_private_permissions(preflight_path)
    summary["preflight"] = preflight
    failures = 0
    if not args.skip_repo_tests:
        code = _run_repo_tests(ROOT, report_root)
        summary["repo_tests"] = {"status": "PASS" if code == 0 else "BLOCK", "exit_code": code}
        failures += int(code != 0)
    if not args.skip_source_health:
        health = _source_health(report_root, timeout=5.0)
        source_health_path = report_root / "source-health.json"
        source_health_path.write_text(
            json.dumps(health, indent=2) + "\n", encoding="utf-8"
        )
        _force_private_permissions(source_health_path)
        summary["source_health"] = health
        failures += int(_source_health_gate(health) == "BLOCK")
    if not args.skip_live:
        decoders = [part.strip() for part in args.decoders.split(",") if part.strip()]
        if not decoders:
            print("No decoder phases configured; blocking run.", file=sys.stderr)
            failures += 1
        for index, decoder in enumerate(
            decoders, 1
        ):
            phase_dir = report_root / f"phase-{index:02d}-{decoder.replace('/', '-')}"
            code = _run_live_phase(
                phase_dir,
                decoder=decoder,
                minutes=args.minutes,
                dwell=args.dwell,
                watchdog=args.watchdog_grace,
            )
            result = _analyze_phase(phase_dir)
            result["process_exit_code"] = code
            summary["phases"].append(result)
            failures += int(code != 0 or result.get("verdict") != "PASS")
    else:
        summary["phases"] = []
    incomplete = args.skip_live or args.skip_source_health or args.skip_repo_tests
    if incomplete:
        failures += 1
    phase_results = summary.get("phases") if isinstance(summary.get("phases"), list) else []
    if not args.skip_live and not phase_results:
        failures += 1
    summary["verdict"] = "INCOMPLETE" if incomplete and not failures == 0 else (
        "BLOCK" if failures else "PASS"
    )
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary_path = report_root / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    _force_private_permissions(summary_path)
    summary["redacted_artifacts"] = str(_redact_root_artifacts(report_root))
    summary_path.write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    _force_private_permissions(summary_path)
    print(
        json.dumps(
            {
                "verdict": summary["verdict"],
                "report_root": str(report_root),
                "image_capture": False,
            },
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
