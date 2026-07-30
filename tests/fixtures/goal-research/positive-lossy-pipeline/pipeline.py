"""Tiny pipeline whose ordinary behavior tests pass despite information loss."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    wallet_id: str
    timestamp_seconds: int
    side: str
    amount: float
    price: float


def encode_event(event: Event) -> tuple[float, float, float]:
    """The defect: identity and time are dropped before the model boundary."""
    return (1.0 if event.side == "buy" else -1.0, event.amount, event.price)


def mean_pool(encoded: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    """The defect: permutation-invariant pooling destroys sequence order."""
    if not encoded:
        return (0.0, 0.0, 0.0)
    size = float(len(encoded))
    return tuple(sum(row[index] for row in encoded) / size for index in range(3))


def score(events: list[Event]) -> float:
    side, amount, price = mean_pool([encode_event(event) for event in events])
    return side + amount * 0.1 + price * 0.001
