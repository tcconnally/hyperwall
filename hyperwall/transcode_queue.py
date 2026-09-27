"""Queue only observed file failures on the user's existing Emby server.

Evidence/status stays beside local logs. All disk and HTTP work is on one
daemon; submitting a candidate and emergency exit never wait for that work.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import time

logger = logging.getLogger("HyperWall")
FAILURE_REASONS = frozenset({
    "malformed_stream", "decoder_recovery_exhausted", "playback_recovery_exhausted",
})
_ACTIVE = {"Queued", "Converting", "ReadyToTransfer", "Transferring"}


def _items(body):
    if isinstance(body, list):
        return body
    values = body.get("Items", [])
    if body.get("TotalRecordCount", len(values)) > len(values):
        raise ValueError("incomplete_sync_inventory")
    return values


def ensure_targeted_job(client, item_id: str, target_name: str, *,
                        allow_create: bool = True, before_submit=None) -> dict:
    """Resolve one explicit video and deduplicate before creating a job."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
        raise ValueError("invalid_item_id")
    if getattr(client.backend, "name", None) != "emby":
        raise ValueError("emby_required")

    def get(path, **kwargs):
        response = client.get(path, timeout=10, **kwargs)
        response.raise_for_status()
        return response.json()

    targets = [t for t in _items(get("/Sync/Targets")) if t.get("Name") == target_name]
    if len(targets) != 1 or targets[0].get("Id") in {
        None, "originalmediafolder", "originalmediafolderreplace",
    }:
        raise ValueError("separate_output_target_required")
    target_id = targets[0]["Id"]
    item = get(f"/Users/{client.user_id}/Items/{item_id}")
    if (str(item.get("Id")) != item_id or item.get("IsFolder") is True
            or item.get("Type") not in {"Video", "MusicVideo", "Movie", "Episode"}):
        raise ValueError("individual_video_required")

    def matching():
        return [i for i in _items(get("/Sync/JobItems", params={"TargetId": target_id}))
                if str(i.get("ItemId")) == item_id]

    existing = matching()
    for status in ("Synced", *_ACTIVE):
        found = next((i for i in existing if i.get("Status") == status), None)
        if found is not None:
            return {"state": "existing_copy" if status == "Synced" else "queued",
                    "job_id": found.get("JobId"), "job_item_id": found.get("Id")}
    if not allow_create:
        raise ValueError("awaiting_previous_submission_confirmation")
    cancelled = next((i for i in existing if i.get("Status") == "Cancelled"), None)
    if cancelled:
        if before_submit:
            before_submit()
        response = client.post(f"/Sync/JobItems/{cancelled['Id']}/Enable", timeout=10)
    elif existing:
        # Do not loop on a failed conversion or revive a removal operation.
        raise ValueError("existing_conversion_needs_review")
    else:
        if before_submit:
            before_submit()
        response = client.post("/Sync/Jobs", timeout=10, json={
            "TargetId": target_id, "UserId": client.user_id,
            "ItemIds": [item_id], "Name": f"Hyperwall recovery {item_id}",
            "Profile": "custom", "Quality": "4000000", "Container": "mp4",
            "VideoCodec": "h264", "AudioCodec": "aac",
            "UnwatchedOnly": False, "SyncNewContent": False,
        })
    response.raise_for_status()
    # Creation can finish asynchronously. The worker persists submission intent
    # before POST and only reads on later attempts, even after a lost response.
    confirmed = next((i for i in matching() if i.get("Status") in _ACTIVE | {"Synced"}), None)
    if confirmed is None:
        raise ValueError("sync_job_not_yet_visible")
    return {"state": "queued", "job_id": confirmed.get("JobId"),
            "job_item_id": confirmed.get("Id")}


class FailureTranscodeQueue:
    def __init__(self, client, directory: Path, target_name: str):
        self.client, self.target_name = client, target_name
        server_key = hashlib.sha256(client.server_url.encode()).hexdigest()[:16]
        self.directory = Path(directory) / server_key
        self.pending = queue.Queue(maxsize=128)
        self.seen = set()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="failure-transcode-queue", daemon=True)
        self.thread.start()

    def submit(self, item: dict, reason: str) -> bool:
        item_id = str(item.get("Id") or "")
        if (reason not in FAILURE_REASONS or not re.fullmatch(r"[A-Za-z0-9_-]+", item_id)
                or item.get("IsFolder") is True or self._stop.is_set() or item_id in self.seen):
            return False
        record = {"item_id": item_id, "reason": reason, "state": "pending", "observed_at": time.time()}
        try:
            self.pending.put_nowait(record)
        except queue.Full:
            logger.warning("Failure transcode queue is full; candidate remains in playback diagnostics.")
            return False
        self.seen.add(item_id)
        return True

    def stop(self):
        self._stop.set()  # No join or network call, including on Escape.

    def _save(self, record):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.directory / (record["item_id"] + ".json")
        temporary = destination.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _process(self, record):
        self._save(record)  # Durable evidence before any server mutation.
        def before_submit():
            record["submission_started"] = True
            self._save(record)
        for attempt in range(3):
            if self._stop.is_set():
                return
            try:
                result = ensure_targeted_job(
                    self.client, record["item_id"], self.target_name,
                    allow_create=not record.get("submission_started", False),
                    before_submit=before_submit,
                )
                record.update(result, updated_at=time.time())
                record.pop("error", None)
                self._save(record)
                logger.info("Failure transcode %s: item=%s reason=%s", record["state"],
                            record["item_id"], record["reason"])
                return
            except Exception as exc:
                # Exception text may contain an authenticated URL.
                record.update(state="pending", error=type(exc).__name__, updated_at=time.time())
                self._save(record)
                if attempt < 2 and self._stop.wait(2):
                    return
        logger.warning("Failure transcode remains pending locally: item=%s", record["item_id"])

    def _run(self):
        try:
            processed = set()
            # Resume only persisted evidence, never infer guilt from metadata.
            for path in self.directory.glob("*.json"):
                if self._stop.is_set():
                    return
                try:
                    record = json.loads(path.read_text())
                    if (isinstance(record, dict) and record.get("state") == "pending"
                            and isinstance(record.get("reason"), str)
                            and record["reason"] in FAILURE_REASONS
                            and isinstance(record.get("item_id"), str)
                            and re.fullmatch(r"[A-Za-z0-9_-]+", record["item_id"])):
                        self.seen.add(record["item_id"])
                        self._process(record)
                        processed.add(record["item_id"])
                except (ValueError, OSError):
                    continue
            while not self._stop.is_set():
                try:
                    record = self.pending.get(timeout=.25)
                except queue.Empty:
                    continue
                try:
                    if record["item_id"] not in processed:
                        self._process(record)
                        processed.add(record["item_id"])
                finally:
                    self.pending.task_done()
        except Exception as exc:
            logger.error("Failure transcode worker stopped: %s", type(exc).__name__)
