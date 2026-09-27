"""Local, bounded performance records; never send telemetry over the network.

Qt samples cached Python state only. A daemon writes JSONL and samples process
resources; slow storage cannot make playback or emergency exit wait.
"""
from __future__ import annotations

from collections import deque
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import platform
import threading
import time

from PyQt6.QtCore import QObject, QTimer

from . import __version__
from .constants import LOG_FILE, STATS_COUNTER_PROPS, STATS_INFO_PROPS, STATS_ENABLED

logger = logging.getLogger("HyperWall")


def _number(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def cached_cell_snapshot(cell, index: int) -> dict:
    """No libmpv reads and no waiting for a stats/render lock."""
    item = cell.current_item or {}
    plan = getattr(cell, "_playback_plan", None)
    streams = item.get("MediaStreams") or []
    video = next((s for s in streams if isinstance(s, dict) and s.get("Type") == "Video"), {})
    state = getattr(getattr(cell, "_playback_controller", None), "state", None)
    row = {
        "cell": index, "item_id": item.get("Id"),
        "prepared": item.get("_hyperwall_prepared") is True,
        "position_s": _number(getattr(cell, "_play_pos", None)),
        "paused": bool(getattr(cell, "_paused", False)),
        "switching": bool(getattr(cell, "_switching", False)),
        "muted": bool(cell.muted),
        "audio_mode": "continuous" if getattr(cell, "_continuous_audio", False) else "lazy",
        "freezes": getattr(cell, "_freeze_count", 0),
        "freeze_seconds": getattr(cell, "_freeze_total_s", 0),
        "buffering": bool(getattr(cell, "_freeze_t0", 0)),
        "software_fallbacks": getattr(cell, "_decoder_software_fallbacks", 0),
        "decoder_faults": getattr(cell, "_decoder_fault_count", 0),
        "resource_quarantined": bool(getattr(cell, "_resource_quarantined", False)),
        "playback_state": getattr(state, "value", None),
        "source": {key: video.get(key) for key in ("Codec", "Width", "Height", "AverageFrameRate", "BitRate")},
        "server_mode": getattr(plan, "server_mode", None),
        "requested_decoder": getattr(plan, "client_decoder", None),
    }
    lock = getattr(cell, "_stats_lock", None)
    if lock is not None and lock.acquire(blocking=False):
        try:
            row["counters"] = {
                key: cell._stats_total.get(key, 0) + cell._stats_current.get(key, 0)
                for key in STATS_COUNTER_PROPS
            }
            # These are fixed libmpv diagnostic property names, never URLs,
            # tokens, filenames, titles or the user's configuration.
            row["media"] = {key: cell._stats_info.get(key) for key in STATS_INFO_PROPS}
        finally:
            lock.release()
    else:
        row["stats_busy"] = True
    render = getattr(getattr(cell, "video_frame", None), "_render_telemetry", None)
    if render is not None:
        row["render"] = render.try_snapshot()
    return row


class LocalWriter:
    """Bounded latest-record queue. Emergency shutdown never joins this thread."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.pending = deque(maxlen=32)
        self.dropped = 0
        self._wake = threading.Event()
        self._stop = False
        self.path = self.directory / (
            f"hyperwall_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.jsonl"
        )
        self.thread = threading.Thread(target=self._run, name="local-telemetry", daemon=True)
        self.thread.start()

    def submit(self, record: dict) -> None:
        if len(self.pending) == self.pending.maxlen:
            self.dropped += 1
        self.pending.append(record)
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def _run(self) -> None:
        handler = None
        previous_cpu = time.process_time()
        previous_time = time.monotonic()
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            handler = RotatingFileHandler(self.path, maxBytes=5 * 1024**2,
                                          backupCount=2, encoding="utf-8")
            os.chmod(self.path, 0o600)
            handler.setFormatter(logging.Formatter("%(message)s"))
            while not self._stop or self.pending:
                self._wake.wait(0.1)
                self._wake.clear()
                while self.pending:
                    record = dict(self.pending.popleft())
                    if record.get("event") == "sample":
                        now, cpu = time.monotonic(), time.process_time()
                        record["resources"] = {
                            "cpu_percent_one_core_100": round(
                                100 * (cpu - previous_cpu) / max(0.001, now - previous_time), 2),
                            "python_threads": threading.active_count(),
                        }
                        previous_cpu, previous_time = cpu, now
                        try:
                            import resource
                            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                            record["resources"]["peak_rss_mib"] = round(
                                rss / (1024**2 if platform.system() == "Darwin" else 1024), 2)
                            if platform.system() == "Linux":
                                pages = int(Path("/proc/self/statm").read_text().split()[1])
                                record["resources"]["current_rss_mib"] = round(
                                    pages * os.sysconf("SC_PAGE_SIZE") / 1024**2, 2)
                        except (ImportError, OSError, ValueError, IndexError):
                            pass
                    record["queue_dropped_records"] = self.dropped
                    text = json.dumps(record, sort_keys=True, allow_nan=False)
                    handler.emit(logging.LogRecord("local", logging.INFO, "", 0, text, (), None))
                    handler.flush()
                    os.chmod(self.path, 0o600)
        except Exception as exc:
            # Diagnostics must never become a playback dependency.
            logger.warning("Local telemetry stopped (%s).", type(exc).__name__)
        finally:
            if handler is not None:
                handler.close()


class LocalTelemetry(QObject):
    def __init__(self, wall, directory: Path | None = None):
        super().__init__()
        self.wall = wall
        self.started = self.last_tick = time.monotonic()
        self.lags = deque(maxlen=100)
        self.writer = LocalWriter(directory or Path(LOG_FILE).parent / "logs" / "telemetry")
        self.record_event("start", version=__version__, platform=platform.system(),
                          release=platform.release(), machine=platform.machine(),
                          cpu_count=os.cpu_count(), cells=len(wall.cells),
                          native_stats_enabled=STATS_ENABLED)
        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(100)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start()
        self.sample_timer = QTimer(self)
        self.sample_timer.setInterval(5000)
        self.sample_timer.timeout.connect(self.sample)
        self.sample_timer.start()
        logger.info("Local telemetry: %s (5s samples; no upload).", self.writer.path)

    def record_event(self, event: str, **fields) -> None:
        self.writer.submit({"event": event, "elapsed_s": round(time.monotonic() - self.started, 4),
                            "time_unix": time.time(), **fields})

    def _tick(self) -> None:
        now = time.monotonic()
        self.lags.append(max(0.0, (now - self.last_tick) * 1000 - 100))
        self.last_tick = now

    def sample(self) -> None:
        # Do not serialize, query libmpv, run commands or touch disk in Qt.
        lags = sorted(self.lags)
        self.lags.clear()
        self.record_event("sample", library_status=getattr(self.wall, "_library_status", None),
                          cells=[cached_cell_snapshot(c, i)
                                          for i, c in enumerate(self.wall.cells)],
                          loop_lag_ms={"max": round(max(lags, default=0), 3),
                                       "p95": round(lags[min(len(lags)-1, int(len(lags)*.95))], 3)
                                       if lags else 0})

    def stop(self) -> None:
        self.tick_timer.stop()
        self.sample_timer.stop()
        self.record_event("stop")
        self.writer.stop()
