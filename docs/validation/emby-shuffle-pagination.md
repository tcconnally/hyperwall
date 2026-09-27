# Full-library shuffle validation — 2026-09-27

Emby has client-specific reports of truncated shuffle queues, including a
[100-song Android report](https://emby.media/community/topic/136586-emby-app-does-not-take-all-files-from-my-playlist/)
and a [larger playlist report acknowledged by Emby support](https://emby.media/community/topic/131166-shuffling-a-large-playlist-wont-select-songs-later-in-the-playlist/).
These do not establish a universal 100-item server limit. The official
[Items API](https://dev.emby.media/reference/RestAPI/ItemsService/getItems.html)
supports `StartIndex` and `Limit` for enumeration.

Hyperwall does not use Emby's shuffle command or `SortBy=Random`. It enumerates
the selected libraries, then PlaylistManager shuffles the full eligible pool
locally. A normal uninterrupted cycle visits each item once before refilling.
Explicit filters, failure quarantine, manual actions and restarting the app can
change that behavior; shuffle history is not persisted between launches.

The loader now explicitly sorts enumeration by name, advances by the actual
response count, continues without a reported total until an empty page, checks
HTTP failures, rejects pages that make no progress, and deduplicates shared
item IDs across selected libraries. Failed libraries are logged and omitted
from that load, rather than admitting an incomplete prefix as a complete pool.

## Validation

- Read-only requests against the configured NAS returned 850 unique videos
  from the currently selected library. This is the selected library count,
  not the total across every library on the server.
- Forcing requests to `Limit=100` returned page sizes
  `100, 100, 100, 100, 100, 100, 100, 100, 50`, with exactly the same item IDs.
- One local shuffle cycle selected all 850 IDs exactly once.
- Regression tests cover 7,503 items with 100-item response caps, missing
  totals, two complete 1,207-item shuffle cycles with metadata refreshes,
  overlapping libraries, repeated pages and an HTTP failure after page one.
- `tests/run_all.py`: all suites passed on the development Mac, with the
  suite's platform-specific skips. No graphical wall was launched.

This validates library selection and queue coverage, not smooth rendered
playback. All files remain eligible by default; conversion still requires
observed playback-failure evidence.
