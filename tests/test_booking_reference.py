"""Tests for booking reference generator volume and collision resistance (T6.2 / KRIYA-001)."""

import pytest
from datetime import datetime
from app.utils.helpers import generate_booking_reference


def test_booking_reference_format():
    """Verify booking reference follows format PREFIX-YYYY-XXXXXXXX."""
    ref = generate_booking_reference(prefix="MC")
    year = datetime.now().year
    assert ref.startswith(f"MC-{year}-")
    parts = ref.split("-")
    assert len(parts) == 3
    assert parts[0] == "MC"
    assert parts[1] == str(year)
    assert len(parts[2]) == 8

    # 32-char alphabet (no O, I, 0, 1)
    allowed_chars = set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
    assert set(parts[2]).issubset(allowed_chars)


def test_booking_ref_volume_100k():
    """T6.2: Generate 100,000 booking references and assert ZERO collisions."""
    num_samples = 100_000
    generated = set()

    for _ in range(num_samples):
        ref = generate_booking_reference()
        assert ref not in generated, f"Collision detected for reference {ref}"
        generated.add(ref)

    assert len(generated) == num_samples
