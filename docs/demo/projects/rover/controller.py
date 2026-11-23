"""Motor command clamping for the rover drive loop."""

from dataclasses import dataclass

from safety import speed_limit_for
from sensors import RangeReading


@dataclass(frozen=True)
class DriveCommand:
    throttle: float
    steering: float = 0.0


def safe_command(requested: DriveCommand, readings: list[RangeReading]) -> DriveCommand:
    """Clamp forward throttle to the active safety speed limit."""
    limit = speed_limit_for(readings)
    clamped = min(requested.throttle, limit) if requested.throttle > 0 else requested.throttle
    return DriveCommand(throttle=clamped, steering=requested.steering)
