"""Fail-closed NVIDIA preflight for the closed Pop!_OS wall."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Iterable


class LinuxPreflightError(RuntimeError):
    """The target GPU inventory is absent, malformed, or unsafe."""


@dataclass(frozen=True)
class GPURecord:
    name: str
    memory_mib: int
    driver_version: str


_MEMORY_RE = re.compile(r"(\d+)")


def parse_nvidia_smi(output: str) -> list[GPURecord]:
    """Parse `nvidia-smi --query-gpu` CSV output without trusting row shape."""
    if not isinstance(output, str):
        raise LinuxPreflightError("nvidia-smi output is not text")
    records: list[GPURecord] = []
    for row in csv.reader(io.StringIO(output)):
        if not row or not any(field.strip() for field in row):
            continue
        if len(row) != 3:
            raise LinuxPreflightError("nvidia-smi rows must have three columns")
        name, memory_text, driver = (field.strip() for field in row)
        if not name or not driver:
            raise LinuxPreflightError("nvidia-smi row has an empty identity field")
        match = _MEMORY_RE.search(memory_text)
        if match is None:
            raise LinuxPreflightError("nvidia-smi row has invalid memory")
        records.append(
            GPURecord(
                name=name,
                memory_mib=int(match.group(1)),
                driver_version=driver,
            )
        )
    return records


def validate_gpu_inventory(
    output: str,
    *,
    expected_model: str = "RTX 5070 Ti",
    min_vram_mib: int = 12_000,
) -> GPURecord:
    """Return the sole acceptable GPU or raise before production playback."""
    if not expected_model.strip():
        raise LinuxPreflightError("expected GPU model is empty")
    if min_vram_mib < 1:
        raise LinuxPreflightError("minimum GPU memory must be positive")
    records = parse_nvidia_smi(output)
    if len(records) != 1:
        raise LinuxPreflightError(
            f"expected exactly one NVIDIA GPU, found {len(records)}"
        )
    record = records[0]
    if expected_model.casefold() not in record.name.casefold():
        raise LinuxPreflightError(
            f"GPU {record.name!r} does not match {expected_model!r}"
        )
    if record.memory_mib < min_vram_mib:
        raise LinuxPreflightError(
            f"GPU reports {record.memory_mib} MiB; need at least {min_vram_mib} MiB"
        )
    return record
