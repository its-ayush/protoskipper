# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Deterministic fuzzer regression tests (P4.D)."""

from __future__ import annotations

import pytest

from protoskipper.builtin_drivers.iec104.fuzzer import (
    fuzz_apdu_mutation,
    fuzz_asdu_codec,
    fuzz_codec_roundtrip,
)


@pytest.mark.parametrize("seed", [0, 1, 42, 0xDEADBEEF])
def test_codec_roundtrip_no_unexpected_errors(seed: int) -> None:
    report = fuzz_codec_roundtrip(iterations=2000, seed=seed)
    assert report.unexpected_errors == 0, (
        f"unexpected exceptions in APCI parser (seed={seed}): {report}"
    )


@pytest.mark.parametrize("seed", [0, 7, 99, 0xC0FFEE])
def test_asdu_codec_no_unexpected_errors(seed: int) -> None:
    report = fuzz_asdu_codec(iterations=2000, seed=seed)
    assert report.unexpected_errors == 0, (
        f"unexpected exceptions in ASDU parser (seed={seed}): {report}"
    )


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_apdu_mutation_no_unexpected_errors(seed: int) -> None:
    report = fuzz_apdu_mutation(iterations=2000, seed=seed)
    assert report.unexpected_errors == 0, (
        f"unexpected exceptions in mutation fuzz (seed={seed}): {report}"
    )


def test_fuzz_report_ok_property() -> None:
    report = fuzz_codec_roundtrip(iterations=100, seed=0)
    assert report.ok is True
