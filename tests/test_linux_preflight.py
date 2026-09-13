"""Tests for the closed-environment Linux GPU preflight."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hyperwall.linux_preflight import (  # noqa: E402
    LinuxPreflightError,
    parse_nvidia_smi,
    validate_gpu_inventory,
)


_VALID = "NVIDIA GeForce RTX 5070 Ti, 16376 MiB, 570.00\n"


def test_parse_nvidia_smi_returns_one_gpu_record():
    records = parse_nvidia_smi(_VALID)
    assert len(records) == 1
    assert records[0].name == "NVIDIA GeForce RTX 5070 Ti"
    assert records[0].memory_mib == 16376
    assert records[0].driver_version == "570.00"


def test_validate_gpu_inventory_accepts_exact_target():
    record = validate_gpu_inventory(_VALID)
    assert record.name.endswith("RTX 5070 Ti")
    assert record.memory_mib >= 12000


def test_validate_gpu_inventory_rejects_wrong_gpu():
    try:
        validate_gpu_inventory("NVIDIA GeForce RTX 4060, 8192 MiB, 570.00\n")
    except LinuxPreflightError as exc:
        assert "RTX 5070 Ti" in str(exc)
    else:
        raise AssertionError("wrong GPU must fail closed")


def test_validate_gpu_inventory_rejects_multiple_gpus():
    output = _VALID + "NVIDIA GeForce RTX 5070 Ti, 16376 MiB, 570.00\n"
    try:
        validate_gpu_inventory(output)
    except LinuxPreflightError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("multiple GPUs must fail closed")


def test_parse_nvidia_smi_rejects_malformed_rows():
    try:
        parse_nvidia_smi("not,csv\n")
    except LinuxPreflightError as exc:
        assert "three columns" in str(exc)
    else:
        raise AssertionError("malformed nvidia-smi output must fail closed")


def run_all() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print("PASS", test.__name__)
        except Exception as exc:
            failed += 1
            print("FAIL", test.__name__, exc)
    print(f"{len(tests) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(run_all())
