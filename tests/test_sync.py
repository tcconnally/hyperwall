"""Tests for the network sync layer.

These tests exercise the asyncio TCP protocol with mock controllers.
No Qt dependency.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperwall.sync import SyncMsg, SyncPeer, SyncServer


class _FakeController:
    def __init__(self):
        self.cells = []
        self.windows = []
        self._window_meta = {}
        self.filter_mode = "all"
        self.applied: list[dict] = []
        self._solo_cell = None
        self._solo_window = None

    def run_on_main(self, fn):
        fn()

    def sync_apply(self, msg):
        self.applied.append(msg)


def _run(coro):
    return asyncio.run(coro)


async def _make_peer():
    reader = asyncio.StreamReader()
    writer = _FakeWriter()
    return reader, writer, SyncPeer(reader, writer)


def test_sync_msg_constants():
    assert SyncMsg.HELLO == "hello"
    assert SyncMsg.CELL_UPDATE == "cell_update"
    assert SyncMsg.SOLO == "solo"
    assert SyncMsg.EXIT_SOLO == "exit_solo"
    assert SyncMsg.FILTER == "filter"
    assert SyncMsg.REMOTE_SOLO == "remote_solo"
    assert SyncMsg.PING == "ping"
    assert SyncMsg.PONG == "pong"


def test_sync_peer_framing():
    async def _test():
        _, writer, peer = await _make_peer()
        await peer.send({"type": "test", "value": 42})
        assert len(writer.written) == 1
        line = writer.written[0].decode("utf-8").strip()
        assert json.loads(line) == {"type": "test", "value": 42}
    _run(_test())


def test_sync_peer_recv():
    async def _test():
        reader, _, peer = await _make_peer()
        reader.feed_data(b'{"type":"ping"}\n')
        reader.feed_data(b'\n')  # empty line ignored
        reader.feed_data(b'bad json\n')
        reader.feed_data(b'{"type":"pong"}\n')
        reader.feed_eof()
        assert await peer.recv() == {"type": "ping"}
        assert await peer.recv() == {"type": "pong"}
        assert await peer.recv() is None
    _run(_test())


def test_sync_server_hello_replies_with_full_state():
    async def _test():
        ctrl = _FakeController()
        server = SyncServer(ctrl, host="127.0.0.1", port=0)

        _, client_writer, client_peer = await _make_peer()
        server.peers.append(client_peer)

        await server._handle(client_peer, {
            "type": "hello",
            "display_name": "test",
            "displays": ["d1"],
        })

        assert client_peer.display_name == "test"
        assert len(client_writer.written) == 1
        msg = json.loads(client_writer.written[0].decode("utf-8").strip())
        assert msg["type"] == "full_state"
    _run(_test())


def test_sync_server_broadcasts_state_change():
    async def _test():
        ctrl = _FakeController()
        server = SyncServer(ctrl, host="127.0.0.1", port=0)
        server._loop = asyncio.get_running_loop()

        _, peer_writer, peer = await _make_peer()
        server.peers.append(peer)
        server.broadcast({"type": "cell_update", "cell_id": "c1", "item_id": "i1"})
        await asyncio.sleep(0.05)
        assert any(b"cell_update" in data for data in peer_writer.written)
    _run(_test())


def test_sync_server_routes_remote_solo_to_other_peers():
    async def _test():
        ctrl = _FakeController()
        server = SyncServer(ctrl, host="127.0.0.1", port=0)

        _, sender_writer, sender = await _make_peer()
        sender.display_name = "sender"
        sender.displays = ["d1"]

        _, target_writer, target = await _make_peer()
        target.display_name = "target"
        target.displays = ["d2"]

        server.peers = [sender, target]
        await server._route_remote_solo(sender, {"type": "remote_solo", "item_id": "xyz"})

        assert not any(b"xyz" in data for data in sender_writer.written)
        assert any(b"xyz" in data for data in target_writer.written)
        routed = json.loads(target_writer.written[0].decode("utf-8").strip())
        assert routed == {"type": "solo", "display_id": "d2", "item_id": "xyz"}
    _run(_test())


def test_sync_server_applies_local_and_broadcasts_filter():
    async def _test():
        ctrl = _FakeController()
        server = SyncServer(ctrl, host="127.0.0.1", port=0)
        _, sender_writer, sender = await _make_peer()
        _, peer_writer, peer = await _make_peer()
        server.peers = [sender, peer]

        await server._handle(sender, {"type": "filter", "mode": "favorites"})

        assert any(m.get("mode") == "favorites" for m in ctrl.applied)
        assert not any(b"favorites" in data for data in sender_writer.written)
        assert any(b"favorites" in data for data in peer_writer.written)
    _run(_test())


# ── helpers ──

class _FakeWriter:
    def __init__(self):
        self.written: list[bytes] = []
        self._closed = False

    def write(self, data: bytes) -> None:
        if not self._closed:
            self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self._closed = True

    def get_extra_info(self, name: str):
        return ("127.0.0.1", 12345)


# ── runner ──

def run_all() -> int:
    tests = [n for n in globals() if n.startswith("test_")]
    failures = 0
    for name in tests:
        fn = globals()[name]
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            print(f"  FAIL  {name}: {e}")
            failures += 1
    print(f"\n{failures} failed out of {len(tests)} tests.")
    return failures


if __name__ == "__main__":
    sys.exit(run_all())
