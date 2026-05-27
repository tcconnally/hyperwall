"""Real-time stats HTTP endpoint for HyperWall v8.2.

Serves live per-cell + GPU telemetry as JSON on http://localhost:9090/stats.
Runs in a daemon thread — never blocks the Qt event loop.

Endpoints:
    GET /stats    — full JSON snapshot (cells, GPU, mpv opts, env)
    GET /health   — lightweight 200 OK for uptime monitoring
"""

import json
import logging
import os
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable

logger = logging.getLogger("HyperWall")


class _StatsHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler — no imports, no Qt, pure stdlib."""

    # Set by StatsServer at construction time
    collect_fn: Callable[[], dict] = lambda: {"error": "no collector attached"}

    def log_message(self, fmt, *args):
        """Suppress default stderr logging; route to our logger instead."""
        logger.debug("stats_server: %s", fmt % args)

    def do_GET(self):
        if self.path == "/stats":
            self._json_response(self.collect_fn())
        elif self.path == "/health":
            self._json_response({"status": "ok", "ts": time.time()})
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')

    def _json_response(self, data: dict):
        body = json.dumps(data, default=str, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


class StatsServer:
    """Threaded HTTP server exposing live wall stats.

    Usage:
        server = StatsServer(port=9090, collect_fn=wall.collect_stats)
        server.start()
        ...
        server.stop()
    """

    def __init__(self, port: int = 9090, collect_fn: Callable[[], dict] | None = None):
        self._port = port
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        # Per-instance handler factory — avoids mutable class-level state.
        _fn = collect_fn or (lambda: {"error": "no collector attached"})
        _handler = type("_StatsHandler", (_StatsHandler,), {"collect_fn": _fn})
        # Bind early so we don't discover port-in-use failure at first request.
        self._httpd = HTTPServer(("127.0.0.1", port), _handler)
        self._httpd.timeout = 1.0  # poll _running flag every second

    def start(self):
        if self._thread is not None:
            return
        self._running.set()
        self._thread = threading.Thread(
            target=self._serve, name="hyperwall-stats", daemon=True
        )
        self._thread.start()
        logger.info("Stats server listening on http://127.0.0.1:%d/stats", self._port)

    def _serve(self):
        while self._running.is_set():
            self._httpd.handle_request()

    def stop(self):
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._httpd is not None:
            try:
                self._httpd.server_close()
            except Exception:
                pass
        logger.info("Stats server stopped.")
