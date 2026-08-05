"""
Unit tests for hyperwall.playlist (Epic 4) — pure multi-source playout.

No PyQt / mpv / Emby. Run: python tests/test_playlist.py
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from hyperwall.playlist import DEFAULT_GROUP, PlaylistManager  # noqa: E402


def _items(n):
    return [{"Id": str(i), "Name": f"item{i}"} for i in range(n)]


def _noshuffle(seq):
    """Deterministic 'shuffle' that leaves order untouched, for assertions."""
    return None


# ── default (single group) parity with the old global deque ───────────────────

def test_empty_pool_returns_none():
    pm = PlaylistManager(shuffle=_noshuffle)
    assert pm.next() is None


def test_global_dedup_no_repeat_until_exhausted():
    pm = PlaylistManager(shuffle=_noshuffle)
    pm.set_source(_items(5))
    seen = [pm.next()["Id"] for _ in range(5)]
    # All 5 distinct before any repeat — the global de-dup guarantee.
    assert sorted(seen) == ["0", "1", "2", "3", "4"]


def test_refills_after_exhaustion():
    pm = PlaylistManager(shuffle=_noshuffle)
    pm.set_source(_items(3))
    first = [pm.next()["Id"] for _ in range(3)]
    # 4th call starts a fresh cycle (refill), not None.
    fourth = pm.next()
    assert fourth is not None
    assert sorted(first) == ["0", "1", "2"]


def test_pool_size_and_groups():
    pm = PlaylistManager(shuffle=_noshuffle)
    pm.set_source(_items(7))
    assert pm.pool_size() == 7
    assert pm.groups() == [DEFAULT_GROUP]


# ── multi-source groups (Epic 4) ──────────────────────────────────────────────

def test_groups_are_independent():
    pm = PlaylistManager(shuffle=_noshuffle)
    pm.set_source([{"Id": "movies-1"}], group="movies")
    pm.set_source([{"Id": "mv-1"}, {"Id": "mv-2"}], group="musicvids")
    assert pm.next("movies")["Id"] == "movies-1"
    got = {pm.next("musicvids")["Id"] for _ in range(2)}
    assert got == {"mv-1", "mv-2"}
    # movies group refills independently, still only its own item.
    assert pm.next("movies")["Id"] == "movies-1"


def test_unknown_group_returns_none():
    pm = PlaylistManager(shuffle=_noshuffle)
    pm.set_source(_items(3), group="a")
    assert pm.next("does-not-exist") is None


def test_push_front_returns_reserved_item_without_dropping_it():
    pm = PlaylistManager(shuffle=_noshuffle)
    pm.set_source(_items(2))
    item = pm.next()
    assert item is not None
    pm.push_front(DEFAULT_GROUP, item)
    returned = pm.next()
    assert returned is not None
    assert returned["Id"] == item["Id"]


def test_clear_group_preserves_pool():
    pm = PlaylistManager(shuffle=_noshuffle)
    pm.set_source(_items(3), group="a")
    pm.next("a")
    pm.clear("a")            # drops live queue only
    assert pm.pool_size("a") == 3
    assert pm.next("a") is not None   # refills from preserved pool


def test_set_source_replaces_pool():
    pm = PlaylistManager(shuffle=_noshuffle)
    pm.set_source(_items(3))
    pm.set_source(_items(10))
    assert pm.pool_size() == 10


def test_shuffle_is_invoked():
    calls = {"n": 0}

    def counting_shuffle(seq):
        calls["n"] += 1

    pm = PlaylistManager(shuffle=counting_shuffle)
    pm.set_source(_items(2))
    pm.next()
    pm.next()
    pm.next()  # triggers a refill → second shuffle
    assert calls["n"] == 2


def run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
