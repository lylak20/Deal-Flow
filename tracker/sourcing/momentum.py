"""Acceleration: is growth itself speeding up? (Open Source Momentum Tracker, Step 3)

Growth is measured over three consecutive windows (30 days when there is 90 days of history, else 7 days).
The score compares each window with the one before it on a log scale:

    gains 100 -> 300 -> 900     score 97  Explosive
    gains 500 -> 500 -> 500     score 50  Stable
    gains 1000 -> 700 -> 300    score 24  Decelerating
"""

import math
from datetime import date, timedelta

RATINGS = [(80, "Explosive"), (60, "Accelerating"), (40, "Stable"), (0, "Decelerating")]


def rating(score: float | None) -> str:
    if score is None:
        return ""
    return next(label for floor, label in RATINGS if score >= floor)


def acceleration_score(gains: list[float]) -> float | None:
    """0-100 from gains over consecutive windows, oldest first. +1 smoothing keeps empty windows finite."""
    if len(gains) < 2 or sum(gains) <= 0:
        return None
    steps = [math.log2((b + 1) / (a + 1)) for a, b in zip(gains, gains[1:])]
    return round(max(0.0, min(100.0, 50 + 30 * sum(steps) / len(steps))), 1)


def _value_at(history: list[tuple[date, float]], day: date) -> float | None:
    """The latest snapshot on or before day."""
    earlier = [v for d, v in history if d <= day]
    return earlier[-1] if earlier else None


def window_gains(history: list[tuple[date, float]], today: date) -> tuple[list[float], str] | None:
    """Gains over three windows from daily snapshots, or None while there is too little history."""
    history = sorted(history)
    if not history:
        return None
    span = (today - history[0][0]).days
    if span >= 90:
        days, basis = 30, "30-day windows (snapshots)"
    elif span >= 21:
        days, basis = 7, "7-day windows (snapshots)"
    else:
        return None
    bounds = [today - timedelta(days=days * k) for k in (3, 2, 1, 0)]
    values = [_value_at(history, b) for b in bounds]
    if any(v is None for v in values):
        return None
    return [values[i + 1] - values[i] for i in range(3)], basis


def gains_from_events(events: list[date], today: date, days: int = 30) -> list[float]:
    """Gains per window from individual event dates (e.g. when each star was given), oldest window first."""
    bounds = [today - timedelta(days=days * k) for k in (3, 2, 1, 0)]
    return [float(sum(bounds[i] < d <= bounds[i + 1] for d in events)) for i in range(3)]
