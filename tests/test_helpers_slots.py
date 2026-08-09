"""Tests for generate_slots() — pure slot-generation arithmetic."""

from datetime import time

import pytest

from app.utils.helpers import generate_slots


def test_generate_slots_exact_division():
    result = generate_slots(time(9, 0), time(11, 0), 30)
    assert result == ["09:00", "09:30", "10:00", "10:30"]


def test_generate_slots_with_remainder_is_truncated():
    # 09:00-10:15 in 20-min steps: 09:00, 09:20, 09:40, 10:00 (10:20 would exceed 10:15)
    result = generate_slots(time(9, 0), time(10, 15), 20)
    assert result == ["09:00", "09:20", "09:40", "10:00"]


def test_generate_slots_start_equal_end_returns_empty():
    assert generate_slots(time(9, 0), time(9, 0), 30) == []


def test_generate_slots_start_after_end_returns_empty():
    assert generate_slots(time(11, 0), time(9, 0), 30) == []


def test_generate_slots_zero_duration_returns_empty():
    assert generate_slots(time(9, 0), time(11, 0), 0) == []


def test_generate_slots_negative_duration_returns_empty():
    assert generate_slots(time(9, 0), time(11, 0), -10) == []
