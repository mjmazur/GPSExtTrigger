import unittest
from unittest.mock import MagicMock, patch
import sys
import datetime
from trigger_gen import parse_time_arg, parse_duration_arg, TriggerGenerator

class TestTriggerGen(unittest.TestCase):

    def test_parse_duration(self):
        self.assertEqual(parse_duration_arg("10:00:00"), datetime.timedelta(hours=10))
        self.assertEqual(parse_duration_arg("10:00"), datetime.timedelta(minutes=10))
        self.assertEqual(parse_duration_arg("10"), datetime.timedelta(seconds=10))

    def test_parse_time(self):
        # Test ISO format
        iso_str = "2023-01-01T12:00:00"
        dt = parse_time_arg(iso_str)
        self.assertEqual(dt, datetime.datetime(2023, 1, 1, 12, 0, 0))

        # Test HH:mm:ss (relative to today)
        now = datetime.datetime.now()
        t_str = "12:00:00"
        dt = parse_time_arg(t_str)
        expected = datetime.datetime.combine(now.date(), datetime.time(12, 0, 0))
        self.assertEqual(dt, expected)

    @patch('trigger_gen.time')
    @patch('trigger_gen.gpiod')
    @patch('trigger_gen.HAS_GPIOD', True)
    def test_generator_run(self, mock_gpiod, mock_time):
        # Setup mocks
        mock_time.time.return_value = 1000.0

        # Mock sleep to advance time
        def sleep_side_effect(seconds):
            mock_time.time.return_value += seconds

        mock_time.sleep.side_effect = sleep_side_effect

        # Setup GPIO mock
        mock_chip = MagicMock()
        mock_line = MagicMock()
        mock_gpiod.Chip.return_value = mock_chip
        mock_chip.get_line.return_value = mock_line

        # Create generator with duration 0.1s, rate 10Hz (period 0.1s)
        # It should run for 1 cycle if duration matches period?
        # Start time = 1000.0 (immediate)
        # Duration = 0.25s -> 2.5 cycles -> 3 pulses?
        # Let's try 3 cycles.

        gen = TriggerGenerator(
            rate=10.0,
            pulse_width=0.01,
            chip_path="/dev/gpiochip4",
            line_offset=17,
            duration=datetime.timedelta(seconds=0.35) # Should cover 3 cycles: 0.0, 0.1, 0.2, stop at 0.3
        )

        # We need to ensure start_time logic works.
        # If immediate, it aligns to next second.
        # current time is 1000.0. Next second is 1001.0.
        # So it waits 1.0s.

        gen.run()

        # Check if sleep was called correctly
        # First sleep: 1.0s (align to 1001.0)
        # Then loop:
        # Cycle 0: Start 1001.0. Sleep 0. Pulse High. Sleep 0.01. Pulse Low.
        # Cycle 1: Start 1001.1. Sleep 0.09 (since 0.01 elapsed). Pulse High. Sleep 0.01. Pulse Low.
        # Cycle 2: Start 1001.2. Sleep 0.09. Pulse High. Sleep 0.01. Pulse Low.
        # Cycle 3: Start 1001.3. Stop time is 1001.0 + 0.35 = 1001.35.
        # Cycle 3 starts at 1001.3. 1001.3 < 1001.35. Runs.
        # Cycle 4: Start 1001.4. > 1001.35. Stops.

        # Total pulses: 4 (0, 1, 2, 3)
        self.assertEqual(mock_line.set_value.call_count, 8) # 4 High, 4 Low

        # Check alignment logic
        # First sleep should be start_ts - now = 1001.0 - 1000.0 = 1.0
        mock_time.sleep.assert_any_call(1.0)

if __name__ == '__main__':
    unittest.main()
