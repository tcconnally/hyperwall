"""
Hyperwall — opt-in soak/endurance harness (HYPERWALL_SOAK_MINUTES=N).

Turns a normal, manually-launched wall session into a fixed-length
randomized endurance run and self-terminates cleanly at the end (the
graceful shutdown path — stats dump included when HYPERWALL_STATS=1).

Three instruments:

- Churn driver: every HYPERWALL_SOAK_DWELL_S (default 75s, ±25% jitter,
  0 disables) a random cell advances, on top of natural EOF advances.
  This forces far more transitions per hour than natural playout —
  coverage of the advance/prefetch/retry machinery across the library.

- Resource sampler (every 60s): working set, private bytes, GDI and
  USER object counts, thread count. Leaks here are the classic
  long-running-wall killers (a GDI leak of a few objects per player
  reuse kills the process at the 10k default cap).

- End-of-run: logs a SOAK summary and triggers the normal shutdown.

Analysis happens offline from the log: SOAK res lines (leak slopes),
PERF lines (loop lag over time), [PREFETCH→]/[DIRECT]/stall/error lines
(advance health), and the hyperwall_stats_*.json dump (decode quality).
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer

logger = logging.getLogger("HyperWall")

SOAK_MINUTES = int(os.environ.get("HYPERWALL_SOAK_MINUTES", "0") or 0)
SOAK_DWELL_S = int(os.environ.get("HYPERWALL_SOAK_DWELL_S", "75") or 0)
SOAK_PROFILE = os.environ.get("HYPERWALL_SOAK_PROFILE", "mixed").strip().lower()
SOAK_REPORT_DIR = os.environ.get("HYPERWALL_SOAK_REPORT_DIR", "").strip()
# Function exerciser: each churn tick drives a random USER ACTION through
# the same handlers a real click takes (advance/prev/seek/mute/volume/
# pause/loop/favorite), then verifies state invariants. 0 = advances only.
SOAK_ACTIONS = os.environ.get("HYPERWALL_SOAK_ACTIONS", "1") == "1"

_RES_SAMPLE_S = 60


def _current_rss_mb() -> int | None:
    """Return current resident memory without confusing it with peak RSS."""
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        value = result.stdout.strip().splitlines()[0]
        return max(0, int(value) // 1024)
    except (IndexError, OSError, ValueError, subprocess.SubprocessError):
        return None


def _resource_snapshot() -> dict[str, int | str]:
    """Return macOS RSS and thread telemetry for offline soak analysis."""
    out: dict[str, int | str] = {}
    out["ws_metric"] = "peak_rss_mb"
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS ru_maxrss is bytes, not KiB.
        out["ws_mb"] = int(rss // (1024 * 1024))
    except Exception as e:
        logger.debug("SOAK resource snapshot failed: %s", e)
    try:
        import threading
        out["threads"] = threading.active_count()
    except Exception:
        pass
    current_rss = _current_rss_mb()
    if current_rss is not None:
        out["current_ws_metric"] = "resident_rss_mb"
        out["current_ws_mb"] = current_rss
    return out


class SoakController(QObject):
    """Owns the churn, sampling, and end-of-run timers. GUI thread only."""

    def __init__(self, wall) -> None:
        # No Qt parent: wall (WallController) is not a QObject.
        super().__init__()
        self._wall = wall
        self._t0 = time.monotonic()
        self._advances = 0
        self._action_counts: dict[str, int] = {}
        self._invariant_violations = 0
        self._unmuted_cell = None   # at most one audible cell during a soak
        self._paused_cell = None    # at most one paused cell during a soak
        self._baseline = _resource_snapshot()
        self._profile = SOAK_PROFILE if SOAK_PROFILE in {"mixed", "audio", "advance"} else "mixed"
        if self._profile != SOAK_PROFILE:
            logger.warning("SOAK unknown profile %r; using mixed.", SOAK_PROFILE)
        self._report_path = self._init_report()

        self._res_timer = QTimer(self)
        self._res_timer.setInterval(_RES_SAMPLE_S * 1000)
        self._res_timer.timeout.connect(self._sample)
        self._res_timer.start()

        self._churn_timer = QTimer(self)
        self._churn_timer.setSingleShot(True)
        if SOAK_DWELL_S > 0:
            self._churn_timer.timeout.connect(self._churn)
            self._arm_churn()

        self._end_timer = QTimer(self)
        self._end_timer.setSingleShot(True)
        self._end_timer.timeout.connect(self._finish)
        self._end_timer.start(SOAK_MINUTES * 60 * 1000)

        logger.info(
            "SOAK start: %d min, profile=%s, dwell %ds (0=natural only), baseline %s, report=%s",
            SOAK_MINUTES, self._profile, SOAK_DWELL_S, self._baseline,
            self._report_path or "disabled",
        )
        self._write_report("start", baseline=self._baseline)

    def _init_report(self) -> Path | None:
        """Create a JSONL telemetry artifact for offline run correlation."""
        if not SOAK_REPORT_DIR:
            return None
        try:
            root = Path(SOAK_REPORT_DIR).expanduser()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root.chmod(0o700)
            path = root / f"hyperwall_soak_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
            path.touch(exist_ok=False)
            path.chmod(0o600)
            return path
        except Exception as e:
            logger.warning("SOAK report disabled: %s", e)
            return None

    def _write_report(self, event: str, **payload) -> None:
        if self._report_path is None:
            return
        record = {
            "event": event,
            "wall_seconds": round(time.monotonic() - self._t0, 3),
            "profile": self._profile,
            "cells": len(self._wall.cells),
            **payload,
        }
        try:
            with self._report_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except Exception as e:
            logger.warning("SOAK report write failed: %s", e)
            self._report_path = None

    def _arm_churn(self) -> None:
        # Per-cell dwell: with N cells, one advance per DWELL/N keeps each
        # cell's average dwell at DWELL. ±25% jitter desynchronizes.
        cells = max(1, len(self._wall.cells))
        base_ms = SOAK_DWELL_S * 1000 / cells
        self._churn_timer.start(int(base_ms * random.uniform(0.75, 1.25)))

    # (action, weight). The trash tag is deliberately ABSENT: soak-writing
    # ToDelete tags feeds the cleanup-delete pipeline - a stray one could
    # cost a real file. Favorites are exercised as immediate double-toggles
    # (net zero; the writes are HTTP-status-verified since the audit).
    _ACTIONS = (
        ("advance", 40), ("seek", 15), ("audio", 15), ("volume", 10),
        ("pause", 10), ("prev", 4), ("loop", 3), ("favorite", 3),
    )
    # Stress the reported transition without unrelated seeks/pause actions
    # dominating the signal.  One audible cell remains the safety invariant.
    _AUDIO_ACTIONS = (("audio", 70), ("volume", 20), ("advance", 10))
    _ADVANCE_ACTIONS = (("advance", 80), ("prev", 10), ("seek", 10))

    def _churn(self) -> None:
        if not SOAK_ACTIONS:
            # The control phase must leave natural playback untouched; do not
            # synthesize the old advance-only fallback.
            self._arm_churn()
            return
        try:
            cell = random.choice(self._wall.cells)
            if SOAK_ACTIONS:
                actions = {
                    "audio": self._AUDIO_ACTIONS,
                    "advance": self._ADVANCE_ACTIONS,
                }.get(self._profile, self._ACTIONS)
                names = [a for a, _ in actions]
                weights = [w for _, w in actions]
                action = random.choices(names, weights=weights, k=1)[0]
            else:
                action = "advance"
            self._action_counts[action] = self._action_counts.get(action, 0) + 1
            self._do_action(action, cell)
            self._verify_invariants(cell, action)
        except Exception as e:
            logger.warning("SOAK action failed: %s", e)
        self._arm_churn()

    def _do_action(self, action: str, cell) -> None:
        """Drive one user action through the real handlers (button clicks /
        slider grabs), never through private state pokes."""
        if action == "advance":
            self._advances += 1
            self._wall.next_video(cell, False)
        elif action == "prev":
            self._wall.prev_video(cell)
        elif action == "seek":
            if cell._mpv is None or cell._duration_s <= 0:
                return
            cell.seek_slider.setSliderDown(True)
            cell.seek_slider.setValue(random.randint(50, 980))
            cell.seek_slider.setSliderDown(False)
        elif action == "audio":
            # Cycle audibility with at most ONE cell unmuted wall-wide, at a
            # civilized volume - an hour of testing must not be an hour of
            # random noise. Exercises the lazy audio arm + relock seek.
            prev = self._unmuted_cell
            if prev is not None and prev is not cell and not prev.muted:
                prev.btn_mute.click()          # re-mute the previous one
            if cell.muted:
                cell.btn_mute.click()          # unmute (restores last vol)
                cell.vol_slider.setValue(25)
                self._unmuted_cell = cell
            else:
                cell.btn_mute.click()          # mute it back
                self._unmuted_cell = None
        elif action == "volume":
            if not cell.muted:
                cell.vol_slider.setValue(random.randint(10, 60))
            # volume drag on a muted cell would unmute it - audibility is
            # owned by the "audio" action to keep the one-audible invariant.
        elif action == "pause":
            prev = self._paused_cell
            if prev is not None and prev is not cell and prev._paused:
                prev.btn_play.click()          # resume the previous one
            cell.btn_play.click()
            self._paused_cell = cell if cell._paused else None
        elif action == "loop":
            cell.btn_loop.click()              # on
            cell.btn_loop.click()              # immediately off (net zero)
        elif action == "favorite":
            if cell.current_item is not None:
                cell.btn_fav.click()           # toggle
                cell.btn_fav.click()           # restore (net zero)

    def _verify_invariants(self, cell, action: str) -> None:
        """The exerciser is a TEST: after every action, cached state, button
        state, and QSS properties must agree. A mismatch is exactly the
        state-drift class the 2026-07-13 audit hunted."""
        problems = []
        if cell.muted != cell.btn_mute.isChecked():
            problems.append(
                f"muted={cell.muted} vs btn_checked={cell.btn_mute.isChecked()}"
            )
        if cell.btn_mute.property("audible") is not (not cell.muted):
            problems.append(
                f"audible prop={cell.btn_mute.property('audible')} "
                f"vs muted={cell.muted}"
            )
        if not cell.muted and cell.vol_slider.value() == 0:
            problems.append("unmuted with volume slider at 0")
        if cell.looping != cell.btn_loop.isChecked():
            problems.append(
                f"looping={cell.looping} vs btn={cell.btn_loop.isChecked()}"
            )
        if problems:
            self._invariant_violations += 1
            logger.warning(
                "SOAK INVARIANT violated after %r: %s",
                action, "; ".join(problems),
            )

    def _telemetry_snapshot(self, *, reset_interval: bool) -> dict | None:
        snapshot_fn = getattr(self._wall, "soak_telemetry_snapshot", None)
        if not callable(snapshot_fn):
            return None
        result = snapshot_fn(reset_interval=reset_interval)
        return result if isinstance(result, dict) else None

    def _sample(self) -> None:
        snap = _resource_snapshot()
        mins = (time.monotonic() - self._t0) / 60
        logger.info(
            "SOAK res @%.0fmin: ws=%sMB current=%sMB private=%sMB gdi=%s user=%s "
            "threads=%s actions=%s invariant_violations=%d",
            mins, snap.get("ws_mb"), snap.get("current_ws_mb"), snap.get("private_mb"),
            snap.get("gdi"), snap.get("user"), snap.get("threads"),
            dict(sorted(self._action_counts.items())),
            self._invariant_violations,
        )
        telemetry = self._telemetry_snapshot(reset_interval=True)
        if telemetry is not None:
            logger.info(
                "SOAK telemetry @%.0fmin: %s",
                mins,
                telemetry,
            )
        report = {
            "resources": snap,
            "actions": dict(sorted(self._action_counts.items())),
            "invariant_violations": self._invariant_violations,
        }
        if telemetry is not None:
            report["render_telemetry"] = telemetry
        self._write_report("sample", **report)

    def _finish(self) -> None:
        snap = _resource_snapshot()
        telemetry = self._telemetry_snapshot(reset_interval=False)
        logger.info(
            "SOAK done after %d min: actions=%s invariant_violations=%d  "
            "baseline %s → final %s",
            SOAK_MINUTES, dict(sorted(self._action_counts.items())),
            self._invariant_violations, self._baseline, snap,
        )
        report = {
            "baseline": self._baseline,
            "resources": snap,
            "actions": dict(sorted(self._action_counts.items())),
            "invariant_violations": self._invariant_violations,
        }
        if telemetry is not None:
            report["render_telemetry"] = telemetry
        self._write_report("finish", **report)
        self._res_timer.stop()
        self._churn_timer.stop()
        # Leave the wall silent regardless of where the audio cycle ended.
        if self._unmuted_cell is not None and not self._unmuted_cell.muted:
            try:
                self._unmuted_cell.btn_mute.click()
            except Exception:
                pass
        self._wall._shutdown()
