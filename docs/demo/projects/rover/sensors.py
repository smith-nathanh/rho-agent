"""Ultrasonic rangefinder helpers for the rover."""

from dataclasses import dataclass

MAX_READING_AGE_MS = 500


@dataclass(frozen=True)
class RangeReading:
    distance_mm: int
    age_ms: int = 0


def mm_to_meters(mm: int) -> float:
    """Convert a sensor distance in millimeters to meters."""
    return mm / 100.0


def nearest_obstacle_m(readings: list[RangeReading]) -> float | None:
    """Closest valid reading in meters, ignoring stale readings (> 500 ms old)."""
    fresh = [r.distance_mm for r in readings if r.age_ms <= MAX_READING_AGE_MS]
    if not fresh:
        return None
    return mm_to_meters(min(fresh))
