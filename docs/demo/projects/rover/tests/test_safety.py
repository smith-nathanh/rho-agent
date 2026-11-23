from controller import DriveCommand, safe_command
from sensors import RangeReading


def test_clear_path_allows_full_throttle():
    cmd = safe_command(DriveCommand(throttle=0.8), [RangeReading(distance_mm=2500)])
    assert cmd.throttle == 0.8


def test_stale_readings_trigger_failsafe_stop():
    cmd = safe_command(DriveCommand(throttle=0.8), [RangeReading(distance_mm=2500, age_ms=900)])
    assert cmd.throttle == 0.0


def test_missing_readings_trigger_failsafe_stop():
    cmd = safe_command(DriveCommand(throttle=0.5), [])
    assert cmd.throttle == 0.0


def test_close_obstacle_triggers_emergency_stop():
    # 400 mm = 0.4 m (inside 0.5 m emergency stop threshold)
    cmd = safe_command(DriveCommand(throttle=0.8), [RangeReading(distance_mm=400)])
    assert cmd.throttle == 0.0


def test_medium_obstacle_reduces_speed():
    # 1000 mm = 1.0 m (inside 1.5 m caution zone)
    cmd = safe_command(DriveCommand(throttle=0.8), [RangeReading(distance_mm=1000)])
    assert cmd.throttle == 0.3
