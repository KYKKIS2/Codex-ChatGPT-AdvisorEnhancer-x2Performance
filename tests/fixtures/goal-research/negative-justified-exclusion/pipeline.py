"""Tiny negative control with a documented, goal-irrelevant exclusion."""

from __future__ import annotations

from dataclasses import dataclass


EXCLUDED_FIELDS = {
    "display_label": "Presentation-only text is unavailable at decision time and is not required."
}


@dataclass(frozen=True)
class Reading:
    device_id: str
    observed_at: int
    value: float
    display_label: str


def encode_reading(reading: Reading) -> tuple[str, int, float]:
    return (reading.device_id, reading.observed_at, reading.value)


def latest_value(readings: list[Reading]) -> float:
    return max(readings, key=lambda item: item.observed_at).value
