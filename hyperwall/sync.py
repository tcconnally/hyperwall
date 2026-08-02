"""
Hyperwall — network sync layer.

Allows multiple Hyperwall instances (e.g. two laptops + a wall driver) to
share playlist state and remote-control solo fullscreen.

Protocol: newline-delimited JSON over TCP.
One instance runs as the sync server; the others connect as clients.
The server is authoritative for the shared playlist state.

Message types:
  hello         client -> server  (identify self on connect)
  full_state    server -> client  (snapshot of playlist + cell state)
  cell_update   bidirectional     (a cell changed to a new item)
  solo          bidirectional     (solo a cell on a target display)
  exit_solo     bidirectional     (exit solo on a display)
  filter        bidirectional     (filter mode changed)
  ping / pong   bidirectional     (keepalive)
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
import uuid
from typing import Any

























































































logger = logging.getLogger("HyperWall")

DEFAULT_SYNC_PORT = 9876


class SyncMsg:
    HELLO = "hello"
    FULL_STATE = "full_state"
    CELL_UPDATE = "cell_update"
    SOLO = "solo"
    EXIT_SOLO = "exit_solo"
    FILTER = "filter"
    REMOTE_SOLO = "remote_solo"
    PING = "ping"
    PONG = "pong"


class SyncPeer:
    """Wraps a asyncio StreamReader/Writer pair with JSON line framing."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.display_name: str = ""
        self.peer_id: str = uuid.uuid4().hex[:8]
        self.displays: list[str] = []
        self._closed = False

    async def send(self, msg: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            line = json.dumps(msg, separators=(",", ":"), default=str).encode("utf-8")
            self.writer.write(line + b"\n")
            await self.writer.drain()
        except Exception as e:
            logger.debug("Sync send to %s failed: %s", self.display_name or self.peer_id, e)
            self._closed = True

    async def recv(self) -> dict[str, Any] | None:
        while True:
            try:
                line = await self.reader.readline()
            except Exception as e:
                logger.debug("Sync recv from %s failed: %s", self.display_name or self.peer_id, e)
                return None
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                logger.warning("Sync malformed JSON from %s", self.display_name or self.peer_id)
                continue

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.writer.close()
            except Exception:
                pass


class SyncServer:
    """TCP sync server. Accepts clients and broadcasts state changes."""

    def __init__(
        self,
        controller: Any,
        host: str = "0.0.0.0",
        port: int = DEFAULT_SYNC_PORT,
    ):
        self.controller = controller
        self.host = host
        self.port = port
        self.peers: list[SyncPeer] = []
        self._server: asyncio.Server | None = None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    def start(self) -> None:
        def _run_loop():
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._task = loop.create_task(self._run())
            loop.run_forever()

        threading.Thread(target=_run_loop, daemon=True, name="hyperwall-sync-srv").start()
        logger.info("Sync server starting on %s:%d", self.host, self.port)

    async def _run(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client, self.host, self.port
        )
        async with self._server:
            await self._server.serve_forever()

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = SyncPeer(reader, writer)
        addr = writer.get_extra_info("peername")
        logger.info("Sync client connected: %s", addr)
        async with self._lock:
            self.peers.append(peer)
        try:
            while True:
                msg = await peer.recv()
                if msg is None:
                    break
                await self._handle(peer, msg)
        finally:
            async with self._lock:
                if peer in self.peers:
                    self.peers.remove(peer)
            peer.close()
            logger.info("Sync client disconnected: %s", peer.display_name or addr)

    async def _handle(self, peer: SyncPeer, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == SyncMsg.HELLO:
            peer.display_name = msg.get("display_name", "unknown")
            peer.displays = msg.get("displays", []) or []
            logger.info("Sync hello from %s", peer.display_name)
            full = self._build_full_state()
            await peer.send(full)
            return

        if mtype == SyncMsg.PING:
            await peer.send({"type": SyncMsg.PONG})
            return

        # State-changing messages are applied locally and rebroadcast.
        if mtype in (SyncMsg.CELL_UPDATE, SyncMsg.SOLO, SyncMsg.EXIT_SOLO, SyncMsg.FILTER):
            self._apply_local(msg)
            await self._broadcast(msg, exclude=peer)
            return

        if mtype == SyncMsg.REMOTE_SOLO:
            await self._route_remote_solo(peer, msg)
            return

        logger.debug("Sync unknown message type: %s", mtype)

    async def _route_remote_solo(
        self, sender: SyncPeer, msg: dict[str, Any]
    ) -> None:
        """Forward a remote-solo request to every other peer's first display."""
        item_id = msg.get("item_id")
        if not item_id:
            return
        async with self._lock:
            peers = list(self.peers)
        for peer in peers:
            if peer is sender or not peer.displays:
                continue
            target_display = peer.displays[0]
            await peer.send({
                "type": SyncMsg.SOLO,
                "display_id": target_display,
                "item_id": item_id,
            })

    def _apply_local(self, msg: dict[str, Any]) -> None:
        """Apply a remote message on the GUI thread via the controller."""
        if self.controller is None:
            return
        try:
            self.controller.run_on_main(lambda: self.controller.sync_apply(msg))
        except Exception as e:
            logger.warning("Sync local apply failed: %s", e)

    def _build_full_state(self) -> dict[str, Any]:
        """Snapshot current playlist + cell state for new clients."""
        if self.controller is None:
            return {"type": SyncMsg.FULL_STATE}

        # Headless relay stores state directly; GUI controller stores it on cells.
        if hasattr(self.controller, "_cell_states"):
            cells = dict(self.controller._cell_states)
            solo = dict(getattr(self.controller, "_solo_state", {}))
        else:
            cells = {}
            for cell in getattr(self.controller, "cells", []):
                cid = getattr(cell, "cell_id", None)
                item = getattr(cell, "current_item", None) or {}
                if cid:
                    cells[cid] = item.get("Id")
            solo = {}
            if getattr(self.controller, "_solo_cell", None) is not None:
                sc = self.controller._solo_cell
                sw = self.controller._solo_window
                sid = getattr(sc, "cell_id", None)
                did = None
                if sw is not None:
                    did = self.controller._window_meta.get(id(sw), {}).get("display_id")
                item_id = (sc.current_item or {}).get("Id")
                solo = {"display_id": did, "cell_id": sid, "item_id": item_id}
        return {
            "type": SyncMsg.FULL_STATE,
            "cells": cells,
            "solo": solo,
            "filter": getattr(self.controller, "filter_mode", "all"),
        }

    async def _broadcast(
        self, msg: dict[str, Any], exclude: SyncPeer | None = None
    ) -> None:
        async with self._lock:
            peers = list(self.peers)
        for peer in peers:
            if peer is exclude:
                continue
            await peer.send(msg)

    def broadcast(self, msg: dict[str, Any]) -> None:
        """Fire-and-forget broadcast from the GUI thread."""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)
        except Exception as e:
            logger.debug("Sync broadcast scheduling failed: %s", e)

    def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for peer in list(self.peers):
            peer.close()
        self.peers.clear()
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception as e:
                logger.debug("Sync server loop stop failed: %s", e)


class SyncClient:
    """TCP sync client. Connects to a sync server and forwards local events."""

    def __init__(
        self,
        controller: Any,
        host: str,
        port: int = DEFAULT_SYNC_PORT,
        display_name: str = "",
    ):
        self.controller = controller
        self.host = host
        self.port = port
        self.display_name = display_name or socket.gethostname()
        self.peer: SyncPeer | None = None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def start(self) -> None:
        def _run_loop():
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._task = loop.create_task(self._run())
            loop.run_forever()

        threading.Thread(target=_run_loop, daemon=True, name="hyperwall-sync-cli").start()
        logger.info("Sync client starting; server %s:%d", self.host, self.port)

    async def _run(self) -> None:
        while not self._closed:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=5.0,
                )
            except Exception as e:
                logger.warning("Sync connect to %s:%d failed: %s", self.host, self.port, e)
                await asyncio.sleep(5.0)
                continue

            self.peer = SyncPeer(reader, writer)
            logger.info("Sync connected to server")
            await self._send_hello()
            try:
                while not self._closed:
                    msg = await self.peer.recv()
                    if msg is None:
                        break
                    await self._handle(msg)
            except Exception as e:
                logger.warning("Sync client loop error: %s", e)
            finally:
                if self.peer is not None:
                    self.peer.close()
                    self.peer = None
                logger.info("Sync disconnected; retrying in 5s")
                await asyncio.sleep(5.0)

    async def _send_hello(self) -> None:
        cells = []
        for cell in getattr(self.controller, "cells", []):
            cid = getattr(cell, "cell_id", None)
            if cid:
                cells.append(cid)
        displays = []
        for win in getattr(self.controller, "windows", []):
            did = self.controller._window_meta.get(id(win), {}).get("display_id")
            if did:
                displays.append(did)
        await self._send({
            "type": SyncMsg.HELLO,
            "display_name": self.display_name,
            "cells": cells,
            "displays": displays,
        })

    async def _handle(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == SyncMsg.PING:
            await self._send({"type": SyncMsg.PONG})
            return
        if mtype == SyncMsg.PONG:
            return
        self._apply_local(msg)

    def _apply_local(self, msg: dict[str, Any]) -> None:
        if self.controller is None:
            return
        try:
            self.controller.run_on_main(lambda: self.controller.sync_apply(msg))
        except Exception as e:
            logger.warning("Sync local apply failed: %s", e)

    async def _send(self, msg: dict[str, Any]) -> None:
        if self.peer is not None:
            await self.peer.send(msg)

    def send(self, msg: dict[str, Any]) -> None:
        """Fire-and-forget send from the GUI thread."""
        if self._closed or self.peer is None or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send(msg), self._loop)
        except Exception as e:
            logger.debug("Sync send scheduling failed: %s", e)

    def broadcast(self, msg: dict[str, Any]) -> None:
        """Clients have only one peer (the server), so broadcast == send."""
        self.send(msg)

    def stop(self) -> None:
        self._closed = True
        if self.peer is not None:
            self.peer.close()
        if self._task is not None:
            self._task.cancel()
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception as e:
                logger.debug("Sync client loop stop failed: %s", e)


# ── headless relay controller ──────────────────────────────────────────────────────────────────

class RelayController:
    """Minimal controller for running the sync server headlessly on a relay host.

    The relay does not play video; it just remembers the last authoritative
    state and forwards messages between Hyperwall peers.
    """

    def __init__(self):
        self.cells: list[Any] = []
        self.windows: list[Any] = []
        self._window_meta: dict[int, dict] = {}
        self.filter_mode = "all"
        self._solo_cell = None
        self._solo_window = None
        self._cell_states: dict[str, str] = {}
        self._solo_state: dict[str, Any] = {}

    def run_on_main(self, fn: Any) -> None:
        fn()

    def sync_apply(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == SyncMsg.CELL_UPDATE:
            cid = msg.get("cell_id")
            iid = msg.get("item_id")
            if cid and iid:
                self._cell_states[cid] = iid
        elif mtype == SyncMsg.SOLO:
            self._solo_state = {
                "display_id": msg.get("display_id"),
                "cell_id": msg.get("cell_id"),
                "item_id": msg.get("item_id"),
            }
        elif mtype == SyncMsg.EXIT_SOLO:
            self._solo_state = {}
        elif mtype == SyncMsg.FILTER:
            mode = msg.get("mode")
            if mode in ("all", "favorites"):
                self.filter_mode = mode


def run_sync_relay(host: str = "0.0.0.0", port: int = DEFAULT_SYNC_PORT) -> None:
    """Run a headless sync relay. Blocks until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    controller = RelayController()
    server = SyncServer(controller, host=host, port=port)
    server.start()
    logger.info("Hyperwall sync relay listening on %s:%d", host, port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Relay shutting down.")
    finally:
        server.stop()
