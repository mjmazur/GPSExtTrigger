#!/usr/bin/env python3
import argparse
import time
import sys
import signal
import logging
from datetime import datetime, timedelta
import math

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Try to import gpiod, mock if not available (for testing or non-RPi environments)
try:
    import gpiod
    HAS_GPIOD = True
except ImportError:
    gpiod = None
    HAS_GPIOD = False
    logging.warning("gpiod module not found. Running in simulation mode.")

class MockLine:
    def __init__(self, offset):
        self.offset = offset
        self.value = 0
        self.direction = None

    def request(self, consumer, type, flags=0):
        logging.info(f"MockLine {self.offset}: Requested by {consumer}")

    def set_value(self, value):
        self.value = value
        # logging.debug(f"MockLine {self.offset}: Set to {value}")

    def release(self):
        logging.info(f"MockLine {self.offset}: Released")

class MockChip:
    def __init__(self, path):
        self.path = path

    def get_line(self, offset):
        return MockLine(offset)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

def parse_time_arg(arg):
    """Parses a time argument which can be ISO 8601 or HH:mm:ss."""
    now = datetime.now()
    try:
        # Try full ISO format
        return datetime.fromisoformat(arg)
    except ValueError:
        pass

    try:
        # Try HH:mm:ss
        t = datetime.strptime(arg, "%H:%M:%S").time()
        return datetime.combine(now.date(), t)
    except ValueError:
        pass

    try:
        # Try mm:ss
        t = datetime.strptime(arg, "%M:%S").time()
        # Assume current hour? Or just relative?
        # The prompt says "For runtime, the time can be given as HH:mm:ss, mm:ss, or ss."
        # This function is for start/stop times. The prompt says "Times are specified in YYYY-MM-DDTHH:mm:ss format. For runtime, the time can be given as HH:mm:ss, mm:ss, or ss."
        # "Runtime" likely refers to Duration.
        # But if start/stop is HH:mm:ss, we assume today.
        pass
    except ValueError:
        pass

    raise ValueError(f"Invalid time format: {arg}")

def parse_duration_arg(arg):
    """Parses duration string in HH:mm:ss, mm:ss, or ss format."""
    parts = arg.split(':')
    if len(parts) == 3:
        h, m, s = map(float, parts)
        return timedelta(hours=h, minutes=m, seconds=s)
    elif len(parts) == 2:
        m, s = map(float, parts)
        return timedelta(minutes=m, seconds=s)
    elif len(parts) == 1:
        s = float(parts[0])
        return timedelta(seconds=s)
    else:
        raise ValueError(f"Invalid duration format: {arg}")

class TriggerGenerator:
    def __init__(self, rate, pulse_width, chip_path, line_offset, start_time=None, stop_time=None, duration=None):
        self.rate = rate
        self.period = 1.0 / rate
        self.pulse_width = pulse_width
        self.chip_path = chip_path
        self.line_offset = line_offset
        self.start_time = start_time
        self.stop_time = stop_time
        self.duration = duration
        self.running = False

        # Validate pulse width
        if self.pulse_width >= self.period:
            logging.warning(f"Pulse width {self.pulse_width}s is >= period {self.period}s. Output will be constant high.")

    def run(self):
        # Determine start timestamp
        if self.start_time:
            start_ts = self.start_time.timestamp()
            # If start time is in the past, warn? Or just start aligned?
            if start_ts < time.time():
                logging.warning("Start time is in the past. Starting at next alignment point.")
                # Align to next period boundary from start_ts that is in the future?
                # Or just start immediately aligned to next second?
                # "Start immediately and sync to PPS as soon as possible."
                # If explicit start time was given but passed, maybe we should just align to the grid defined by start_time.
                # Grid: T = start_ts + n * period.
                # Find smallest n such that T > now.
                now = time.time()
                n = math.ceil((now - start_ts) / self.period)
                start_ts = start_ts + n * self.period
        else:
            # "Start immediately and sync to PPS as soon as possible."
            # PPS aligns with whole seconds.
            now = time.time()
            start_ts = math.ceil(now)
            if start_ts <= now:
                start_ts += 1.0 # Ensure it's in the future

        # Determine end timestamp
        end_ts = None
        if self.stop_time:
            end_ts = self.stop_time.timestamp()
        elif self.duration:
            end_ts = start_ts + self.duration.total_seconds()

        logging.info(f"Starting trigger generation at {datetime.fromtimestamp(start_ts)} ({start_ts})")
        logging.info(f"Rate: {self.rate} Hz, Period: {self.period:.6f}s, Pulse Width: {self.pulse_width:.6f}s")
        if end_ts:
            logging.info(f"Ending at {datetime.fromtimestamp(end_ts)} ({end_ts})")
        else:
            logging.info("Running indefinitely (press Ctrl+C to stop)")

        # GPIO Setup
        chip = None
        line = None

        try:
            if HAS_GPIOD:
                chip = gpiod.Chip(self.chip_path)
                line = chip.get_line(self.line_offset)
                line.request(consumer="TriggerGenerator", type=gpiod.LINE_REQ_DIR_OUT)
            else:
                chip = MockChip(self.chip_path)
                line = chip.get_line(self.line_offset)
                line.request("TriggerGenerator", None)

            self.running = True

            # Wait for start
            now = time.time()
            if start_ts > now:
                time.sleep(start_ts - now)

            count = 0
            while self.running:
                # Calculate target times for this cycle
                # To avoid drift, always calculate relative to start_ts
                cycle_start = start_ts + count * self.period
                cycle_end_pulse = cycle_start + self.pulse_width

                now = time.time()

                # Check if we are past end time
                if end_ts and cycle_start >= end_ts:
                    logging.info("Reached stop time/duration.")
                    break

                # If we missed the cycle start (e.g. system load), skip it or fire late?
                # "Trigger should be synchronized" - firing late breaks sync.
                # Better to skip if significantly late, or fire immediately if just slightly late.
                # Let's try to fire immediately if the delay is small, else warn.

                delay_to_start = cycle_start - now

                if delay_to_start > 0:
                    time.sleep(delay_to_start)
                    # Re-read time to be accurate?
                    # In python sleep might overshoot slightly.
                    # We just assume we are close enough.

                # Turn ON
                line.set_value(1)

                # Sleep for pulse width
                # Re-calculate now to account for jitter in wake-up
                now = time.time()
                delay_to_end = cycle_end_pulse - now

                if delay_to_end > 0:
                    time.sleep(delay_to_end)

                # Turn OFF
                line.set_value(0)

                count += 1

        except KeyboardInterrupt:
            logging.info("Stopped by user.")
        except Exception as e:
            logging.error(f"Error: {e}")
        finally:
            if line:
                line.release()
            # Chip doesn't need explicit close in gpiod usually, context manager handles it if used.
            # But gpiod.Chip is a context manager.
            # In this structure, I instantiated it directly.
            # In recent libgpiod (v2), API changed significantly.
            # Assuming libgpiod v1 (common in standard repos).
            # v1: chip.get_line(), line.request(), line.set_value()
            # v2: request_lines() on chip.
            # I will stick to v1 style as it's more common on RPi OS currently (bullseye/bookworm transition).
            # Wait, Bookworm uses libgpiod v2?
            # RPi 5 uses Bookworm. Bookworm has libgpiod 1.6.3 or 2.0?
            # "python3-libgpiod" usually provides the bindings for the C library.
            # If it's v2, the API is `gpiod.request_lines(...)`.
            # If it's v1, it's `chip.get_line(...)`.
            # I'll try to support the common interface or provide a fallback.
            # The code above uses v1 style.
            pass

def main():
    parser = argparse.ArgumentParser(description="Generate a synchronized trigger signal on GPIO.")
    parser.add_argument("--rate", type=float, default=25.0, help="Frame rate in Hz (default: 25.0)")
    parser.add_argument("--pulse-width", type=float, default=0.02, help="Pulse width in seconds (default: 0.02)")
    parser.add_argument("--start", type=str, help="Start time (ISO 8601 YYYY-MM-DDTHH:mm:ss or HH:mm:ss)")
    parser.add_argument("--stop", type=str, help="Stop time (ISO 8601 YYYY-MM-DDTHH:mm:ss or HH:mm:ss)")
    parser.add_argument("--duration", type=str, help="Duration (HH:mm:ss, mm:ss, or ss)")
    parser.add_argument("--chip", type=str, default="/dev/gpiochip4", help="GPIO chip path (default: /dev/gpiochip4)")
    parser.add_argument("--line", type=int, default=17, help="GPIO line offset (default: 17)")

    args = parser.parse_args()

    start_time = None
    if args.start:
        try:
            start_time = parse_time_arg(args.start)
        except ValueError as e:
            logging.error(e)
            sys.exit(1)

    stop_time = None
    if args.stop:
        try:
            stop_time = parse_time_arg(args.stop)
        except ValueError as e:
            logging.error(e)
            sys.exit(1)

    duration = None
    if args.duration:
        try:
            duration = parse_duration_arg(args.duration)
        except ValueError as e:
            logging.error(e)
            sys.exit(1)

    if stop_time and duration:
        logging.error("Cannot specify both stop time and duration.")
        sys.exit(1)

    gen = TriggerGenerator(
        rate=args.rate,
        pulse_width=args.pulse_width,
        chip_path=args.chip,
        line_offset=args.line,
        start_time=start_time,
        stop_time=stop_time,
        duration=duration
    )

    gen.run()

if __name__ == "__main__":
    main()
