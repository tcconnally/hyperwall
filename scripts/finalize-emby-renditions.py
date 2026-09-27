#!/usr/bin/env python3
"""Remux completed Emby alternates once, then attest preparation with an MP4 box.

Run this bounded foreground worker on the NAS host. Media tools run as Emby's
99:100 user inside its existing container; no packages or services are added.
Only the supplied alternate tree is modified. Stdout contains counts/reasons;
the private ledger in --work may contain relative media paths.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import subprocess
import sys
import time
import uuid


NORMALIZATION_RECEIPT = (
    struct.pack(">I4s", 64, b"free")
    + b"Hyperwall normalized mp4 v1\n".ljust(56, b"\0")
)
MIN_FREE_BYTES = 50 * 1024**3
MIN_STABLE_SECONDS = 60.0
LOADER = "/app/emby/lib/ld-linux-x86-64.so.2"
LIBRARIES = "/app/emby/lib:/app/emby/extra/lib"


class FinalizeError(Exception):
    """An intentionally credential/path-free error code."""


class LowDisk(FinalizeError):
    pass


@dataclass(frozen=True)
class Identity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def read(cls, path: Path) -> Identity:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise FinalizeError("not_regular_file")
        return cls(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def token(self) -> list[int]:
        return [self.device, self.inode, self.size, self.mtime_ns, self.ctime_ns]


def no_symlinks(path: Path) -> None:
    """Reject links in every existing component, including root ancestors."""
    for component in (path, *path.parents):
        if component.is_symlink():
            raise FinalizeError("symlink_path")


def safe_file(path: Path, root: Path) -> Identity:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise FinalizeError("outside_root") from None
    if not relative.parts or ".." in relative.parts:
        raise FinalizeError("outside_root")
    no_symlinks(path)
    return Identity.read(path)


def has_receipt(path: Path) -> bool:
    # O_NOFOLLOW prevents a final-component link being followed after discovery.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        if os.fstat(handle.fileno()).st_size <= len(NORMALIZATION_RECEIPT):
            return False
        handle.seek(-len(NORMALIZATION_RECEIPT), os.SEEK_END)
        return handle.read() == NORMALIZATION_RECEIPT


def number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and not isinstance(value, bool) else None


def frame_rate(value) -> float | None:
    if isinstance(value, str) and "/" in value:
        parts = value.split("/")
        if len(parts) != 2:
            return None
        numerator, denominator = map(number, parts)
        return numerator / denominator if numerator is not None and denominator else None
    return number(value)


def rotation(stream: dict) -> float:
    values = [
        side.get("rotation") for side in stream.get("side_data_list", [])
        if isinstance(side, dict) and "rotation" in side
    ]
    if not values and "rotate" in stream.get("tags", {}):
        values = [stream["tags"]["rotate"]]
    value = number(values[0]) if values else 0.0
    if value is None:
        raise FinalizeError("invalid_rotation")
    return value % 360.0


def validate_probe(payload: dict, *, output: bool = False) -> dict:
    """Validate the selected first video/audio and a finite MP4 duration."""
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise FinalizeError("invalid_probe_shape")
    fmt = payload.get("format")
    if not isinstance(fmt, dict) or "mp4" not in str(fmt.get("format_name", "")).split(","):
        raise FinalizeError("not_mp4")
    duration = number(fmt.get("duration"))
    if duration is None or duration <= 0:
        raise FinalizeError("invalid_duration")
    streams = [s for s in payload["streams"] if isinstance(s, dict)]
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if not videos or (output and (len(videos) != 1 or len(audios) > 1)):
        raise FinalizeError("invalid_stream_count")
    video = videos[0]
    if video.get("codec_name") != "h264":
        raise FinalizeError("video_codec")
    if video.get("pix_fmt") not in {"yuv420p", "nv12"}:
        raise FinalizeError("video_pixel_format")
    width, height = number(video.get("width")), number(video.get("height"))
    if (width is None or height is None or min(width, height) < 2
            or max(width, height) > 1920 or min(width, height) > 1080):
        raise FinalizeError("video_dimensions")
    fps = frame_rate(video.get("avg_frame_rate"))
    nominal = frame_rate(video.get("r_frame_rate"))
    if fps is None or fps <= 0 or fps > 60.001 or (nominal is not None and nominal > 60.001):
        raise FinalizeError("video_fps")
    audio = audios[0] if audios else None
    if audio is not None:
        channels = number(audio.get("channels"))
        if audio.get("codec_name") != "aac" or channels is None or not 1 <= channels <= 2:
            raise FinalizeError("audio_format")
    return {"duration": duration, "width": width, "height": height,
            "fps": fps, "rotation": rotation(video), "audio": audio is not None}


def validate_remux(before: dict, after: dict) -> None:
    original = validate_probe(before)
    remuxed = validate_probe(after, output=True)
    tolerance = max(0.5, min(2.0, original["duration"] * 0.001))
    if abs(original["duration"] - remuxed["duration"]) > tolerance:
        raise FinalizeError("duration_changed")
    for key in ("width", "height", "audio"):
        if original[key] != remuxed[key]:
            raise FinalizeError("selected_stream_changed")
    angle = abs(original["rotation"] - remuxed["rotation"]) % 360
    if min(angle, 360 - angle) > 0.01:
        raise FinalizeError("rotation_changed")
    if abs(original["fps"] - remuxed["fps"]) > 0.05:
        raise FinalizeError("frame_rate_changed")


class DockerMedia:
    def __init__(self, container: str, timeout_s: float = 3600.0):
        self.container = container
        self.timeout_s = timeout_s
        self.timeout_binary = None

    def base(self) -> list[str]:
        return ["docker", "exec", "--user", "99:100", self.container]

    def preflight(self) -> None:
        try:
            result = subprocess.run(self.base() + ["/bin/sh", "-c", "command -v timeout"],
                                    capture_output=True, text=True, timeout=20)
            candidate = result.stdout.strip()
            if result.returncode or not candidate.startswith("/") or "\n" in candidate:
                raise FinalizeError("container_timeout_missing")
            check = subprocess.run(self.base() + [candidate, "-s", "TERM", "-k", "5", "3", "/bin/true"],
                                   capture_output=True, timeout=20)
            if check.returncode:
                raise FinalizeError("container_timeout_unsupported")
            self.timeout_binary = candidate
        except (OSError, subprocess.TimeoutExpired):
            raise FinalizeError("container_preflight_failed") from None

    def run(self, tool: str, arguments: list[str], timeout_s: float) -> str:
        if self.timeout_binary is None:
            raise FinalizeError("container_not_preflighted")
        duration = max(1, int(min(timeout_s, self.timeout_s)))
        command = self.base() + [self.timeout_binary, "-s", "TERM", "-k", "5", str(duration),
                                 LOADER, "--library-path", LIBRARIES,
                                 f"/app/emby/bin/{tool}", *arguments]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=duration + 15)
        except (OSError, subprocess.TimeoutExpired):
            raise FinalizeError(f"{tool}_execution_failed") from None
        if result.returncode:
            raise FinalizeError(f"{tool}_failed")
        return result.stdout

    def probe(self, path: str, timeout_s: float) -> dict:
        raw = self.run("ffprobe", ["-v", "error", "-show_format", "-show_streams",
                                   "-of", "json", path], min(120, timeout_s))
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            raise FinalizeError("ffprobe_invalid_json") from None

    def remux(self, source: str, destination: str, timeout_s: float) -> None:
        self.run("ffmpeg", ["-nostdin", "-hide_banner", "-v", "error", "-xerror", "-y",
                            "-noautorotate", "-i", source, "-map", "0:v:0", "-map", "0:a:0?",
                            "-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
                            "-max_interleave_delta", "1000000", "-movflags", "+faststart",
                            destination], timeout_s)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Finalizer:
    def __init__(self, root: Path, container_root: PurePosixPath, work: Path,
                 container_work: PurePosixPath, media: DockerMedia, *,
                 min_free_bytes: int = MIN_FREE_BYTES, deadline: float | None = None):
        self.root, self.container_root = root, container_root
        self.work, self.container_work = work, container_work
        self.media = media
        self.min_free_bytes = min_free_bytes
        self.deadline = deadline
        self.observed: dict[str, tuple[Identity, float]] = {}
        self.ledger_path = work / "ledger.json"
        self.records: dict[str, dict] = {}
        if self.ledger_path.exists():
            no_symlinks(self.ledger_path)
            try:
                data = json.loads(self.ledger_path.read_text())
                if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("files"), dict):
                    raise ValueError()
                self.records = data["files"]
            except (OSError, ValueError):
                raise FinalizeError("invalid_ledger") from None

    def remaining(self) -> float:
        remaining = self.media.timeout_s if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 1:
            raise FinalizeError("deadline_reached")
        return remaining

    def save(self) -> None:
        temp = self.work / ("ledger-" + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("x", encoding="utf-8") as handle:
                os.chmod(temp, 0o600)
                json.dump({"version": 1, "files": self.records}, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.ledger_path)
            fsync_directory(self.work)
        finally:
            temp.unlink(missing_ok=True)

    def check_disk(self, needed: int = 0) -> None:
        if any(shutil.disk_usage(path).free <= self.min_free_bytes + needed
               for path in (self.root, self.work)):
            raise LowDisk("disk_reserve_reached")

    def finalize(self, path: Path, identity: Identity) -> None:
        """Publish only after full validation and a final unchanged-source check."""
        if safe_file(path, self.root) != identity:
            raise FinalizeError("source_changed")
        self.check_disk(int(identity.size * 1.1) + 16 * 1024**2)
        source = str(self.container_root / path.relative_to(self.root).as_posix())
        before = self.media.probe(source, self.remaining())
        validate_probe(before)
        if safe_file(path, self.root) != identity:
            raise FinalizeError("source_changed")
        name = "remux-" + uuid.uuid4().hex + ".partial.mp4"
        temp = self.work / name
        container_temp = str(self.container_work / name)
        try:
            # Reserve a unique writable path for the non-root container user.
            with temp.open("xb"):
                pass
            os.chown(temp, 99, 100)
            os.chmod(temp, 0o600)
            self.media.remux(source, container_temp, self.remaining())
            safe_file(temp, self.work)
            after = self.media.probe(container_temp, self.remaining())
            validate_remux(before, after)
            self.check_disk()
            if safe_file(path, self.root) != identity:
                raise FinalizeError("source_changed")
            fd = os.open(temp, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "ab") as handle:
                if os.fstat(handle.fileno()).st_size <= 0:
                    raise FinalizeError("empty_output")
                handle.write(NORMALIZATION_RECEIPT)
                handle.flush()
                os.fsync(handle.fileno())
            os.chown(temp, 99, 100)
            # Emby must be able to serve the finished alternate, and all paths
            # must still be ordinary files inside the configured trees.
            os.chmod(temp, 0o644)
            safe_file(temp, self.work)
            if safe_file(path, self.root) != identity:
                raise FinalizeError("source_changed")
            os.replace(temp, path)
            fsync_directory(path.parent)
        finally:
            temp.unlink(missing_ok=True)

    def scan(self, *, wall_time: float | None = None, monotonic: float | None = None) -> dict:
        self.check_disk()
        now = time.time() if wall_time is None else wall_time
        tick = time.monotonic() if monotonic is None else monotonic
        counts = Counter(discovered=0, ready=0, finalized=0, waiting=0, failed=0, rejected=0)
        reasons = Counter()
        seen = set()
        for directory, subdirectories, files in os.walk(self.root, followlinks=False):
            subdirectories[:] = sorted(d for d in subdirectories
                                       if not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                if Path(name).suffix.lower() != ".mp4":
                    continue
                path = Path(directory) / name
                key = path.relative_to(self.root).as_posix()
                seen.add(key)
                counts["discovered"] += 1
                try:
                    identity = safe_file(path, self.root)
                    if identity.size > 64 and has_receipt(path):
                        counts["ready"] += 1
                        continue
                    previous = self.observed.get(key)
                    if previous is None or previous[0] != identity:
                        self.observed[key] = (identity, tick)
                        counts["waiting"] += 1
                        continue
                    if (now - identity.mtime_ns / 1e9 < MIN_STABLE_SECONDS
                            or tick - previous[1] < MIN_STABLE_SECONDS):
                        counts["waiting"] += 1
                        continue
                    old = self.records.get(key, {})
                    if old.get("identity") == identity.token() and old.get("status") == "failed":
                        counts["failed"] += 1
                        reasons[old.get("reason", "previous_failure")] += 1
                        continue
                    try:
                        self.finalize(path, identity)
                    except LowDisk:
                        raise
                    except (FinalizeError, OSError) as exc:
                        reason = str(exc) if isinstance(exc, FinalizeError) else type(exc).__name__
                        if reason in {"source_changed", "deadline_reached"}:
                            self.observed.pop(key, None)
                            counts["waiting"] += 1
                        else:
                            self.records[key] = {"identity": identity.token(), "status": "failed", "reason": reason}
                            counts["failed"] += 1
                        reasons[reason] += 1
                    else:
                        self.records[key] = {"identity": Identity.read(path).token(), "status": "ready"}
                        counts["finalized"] += 1
                        counts["ready"] += 1
                    self.save()
                except LowDisk:
                    raise
                except (FinalizeError, OSError) as exc:
                    counts["rejected"] += 1
                    reasons[str(exc) if isinstance(exc, FinalizeError) else type(exc).__name__] += 1
        self.observed = {key: value for key, value in self.observed.items() if key in seen}
        return {**dict(counts), "reasons": dict(reasons)}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True, type=Path)
    result.add_argument("--container-root", required=True, type=PurePosixPath)
    result.add_argument("--work", required=True, type=Path)
    result.add_argument("--container-work", required=True, type=PurePosixPath)
    result.add_argument("--container", default="Emby")
    result.add_argument("--watch-seconds", type=float, default=120.0)
    result.add_argument("--max-hours", type=float, default=72.0)
    result.add_argument("--expected", type=int, default=None)
    result.add_argument("--file-timeout-seconds", type=float, default=3600.0)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    lock = None
    try:
        if sys.platform != "linux":
            raise FinalizeError("linux_host_required")
        if (not math.isfinite(args.watch_seconds) or args.watch_seconds < 1
                or not math.isfinite(args.max_hours) or not 0 < args.max_hours <= 72
                or not math.isfinite(args.file_timeout_seconds) or args.file_timeout_seconds < 1
                or (args.expected is not None and args.expected < 1)):
            raise FinalizeError("invalid_bounds")
        for path in (args.root, args.work, args.container_root, args.container_work):
            if not path.is_absolute() or ".." in path.parts or path == type(path)("/"):
                raise FinalizeError("invalid_root")
        if args.work.is_relative_to(args.root) or args.root.is_relative_to(args.work):
            raise FinalizeError("overlapping_roots")
        if args.container_work.is_relative_to(args.container_root) or args.container_root.is_relative_to(args.container_work):
            raise FinalizeError("overlapping_container_roots")
        no_symlinks(args.root)
        no_symlinks(args.work)
        if not args.root.is_dir():
            raise FinalizeError("missing_root")
        args.work.mkdir(parents=True, exist_ok=True)
        os.chown(args.work, 99, 100)
        os.chmod(args.work, 0o700)
        if args.root.stat().st_dev != args.work.stat().st_dev:
            raise FinalizeError("atomic_replace_requires_same_filesystem")
        import fcntl
        lock_path = args.work / "worker.lock"
        no_symlinks(lock_path)
        lock = lock_path.open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FinalizeError("worker_already_running") from None
        media = DockerMedia(args.container, args.file_timeout_seconds)
        media.preflight()
        deadline = time.monotonic() + args.max_hours * 3600
        finalizer = Finalizer(args.root, args.container_root, args.work, args.container_work,
                              media, deadline=deadline)
        last = {}
        while time.monotonic() < deadline:
            last = finalizer.scan()
            completed = args.expected is not None and last["ready"] >= args.expected
            print(json.dumps({"status": "completed" if completed else "watching", **last}), flush=True)
            if completed:
                return 0
            time.sleep(min(args.watch_seconds, max(0, deadline - time.monotonic())))
        print(json.dumps({"status": "deadline_reached", **last}), flush=True)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "stopped"}), flush=True)
        return 130
    except (FinalizeError, OSError) as exc:
        reason = str(exc) if isinstance(exc, FinalizeError) else type(exc).__name__
        print(json.dumps({"status": "blocked", "reason": reason}), flush=True)
        return 2
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
