"""Obstacle avoidance speed limits."""

from sensors import RangeReading, nearest_obstacle_m

STOP_DISTANCE_M = 0.5
SLOW_DISTANCE_M = 1.5
SLOW_THROTTLE = 0.3


def speed_limit_for(readings: list[RangeReading]) -> float:
    """Return max allowed throttle in [0.0, 1.0] given current range readings."""
    nearest = nearest_obstacle_m(readings)
    if nearest is None:
        return 0.0  # failsafe stop when no fresh sensor data is available
    if nearest <= STOP_DISTANCE_M:
        return 0.0
    if nearest <= SLOW_DISTANCE_M:
        return SLOW_THROTTLE
    return 1.0
