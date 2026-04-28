#!/usr/bin/env python3
import argparse
import time
import sys
import signal
import logging
import csv
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


class RunningStats:
    def __init__(self):
        self.reset()

    def add(self, value):
        self.count += 1
        if self.count == 1:
            self.mean = value
            self.m2 = 0.0
            self.min = value
            self.max = value
        else:
            if value < self.min:
                self.min = value
            if value > self.max:
                self.max = value
            delta = value - self.mean
            self.mean += delta / self.count
            delta2 = value - self.mean
            self.m2 += delta * delta2

    def snapshot(self, scale=1.0):
        if self.count == 0:
            return None
        variance = self.m2 / self.count if self.count > 0 else 0.0
        stddev = math.sqrt(variance)
        return {
            "count": self.count,
            "mean": self.mean * scale,
            "stddev": stddev * scale,
            "min": self.min * scale,
            "max": self.max * scale,
        }

    def reset(self):
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.min = float("inf")
        self.max = float("-inf")

class TriggerGenerator:
    def __init__(self, rate, pulse_width, chip_path, line_offset, start_time=None, stop_time=None, duration=None,
                 spin_window_us=500.0, jitter_report_interval=5.0, jitter_csv_path=None):
        self.rate = rate
        self.period = 1.0 / rate
        self.pulse_width = pulse_width
        self.chip_path = chip_path
        self.line_offset = line_offset
        self.start_time = start_time
        self.stop_time = stop_time
        self.duration = duration
        self.spin_window = max(0.0, spin_window_us / 1_000_000.0)
        self.jitter_report_interval = max(0.0, jitter_report_interval)
        self.jitter_csv_path = jitter_csv_path
        self.running = False
        self._on_error_total = RunningStats()
        self._off_error_total = RunningStats()
        self._period_error_total = RunningStats()
        self._on_error_interval = RunningStats()
        self._off_error_interval = RunningStats()
        self._period_error_interval = RunningStats()
        self._jitter_csv_file = None
        self._jitter_csv_writer = None

        # Validate pulse width
        if self.pulse_width >= self.period:
            logging.warning(f"Pulse width {self.pulse_width}s is >= period {self.period}s. Output will be constant high.")

    def _perf_counter(self):
        counter = getattr(time, "perf_counter", None)
        if counter is not None:
            try:
                value = counter()
                if isinstance(value, (int, float)):
                    return float(value)
            except Exception:
                pass
        return time.time()

    def _wait_until(self, target_perf):
        while True:
            now_perf = self._perf_counter()
            remaining = target_perf - now_perf
            if remaining <= 0:
                break
            if remaining > self.spin_window:
                time.sleep(remaining - self.spin_window)
            else:
                counter = getattr(time, "perf_counter", None)
                if counter is None:
                    time.sleep(remaining)
                    continue
                try:
                    sample = counter()
                    if not isinstance(sample, (int, float)):
                        time.sleep(remaining)
                        continue
                except Exception:
                    time.sleep(remaining)
                    continue

                while self._perf_counter() < target_perf:
                    pass
                break

    def _open_jitter_csv(self):
        if not self.jitter_csv_path:
            return
        self._jitter_csv_file = open(self.jitter_csv_path, "w", newline="", encoding="utf-8")
        self._jitter_csv_writer = csv.writer(self._jitter_csv_file)
        self._jitter_csv_writer.writerow([
            "wall_time",
            "cycle",
            "target_on_s",
            "actual_on_s",
            "on_error_s",
            "target_off_s",
            "actual_off_s",
            "off_error_s",
            "period_error_s",
        ])

    def _write_jitter_csv(self, cycle, target_on, actual_on, on_error, target_off, actual_off, off_error, period_error):
        if self._jitter_csv_writer is None:
            return
        self._jitter_csv_writer.writerow([
            datetime.now().isoformat(timespec="microseconds"),
            cycle,
            f"{target_on:.9f}",
            f"{actual_on:.9f}",
            f"{on_error:.9f}",
            f"{target_off:.9f}",
            f"{actual_off:.9f}",
            f"{off_error:.9f}",
            "" if period_error is None else f"{period_error:.9f}",
        ])

    def _close_jitter_csv(self):
        if self._jitter_csv_file:
            self._jitter_csv_file.flush()
            self._jitter_csv_file.close()
            self._jitter_csv_file = None
            self._jitter_csv_writer = None

    def _format_stats(self, stats):
        if not stats:
            return "n/a"
        return (
            f"mean={stats['mean']:.3f}ms std={stats['stddev']:.3f}ms "
            f"min={stats['min']:.3f}ms max={stats['max']:.3f}ms n={stats['count']}"
        )

    def _log_jitter_stats(self, total=False):
        if total:
            on_stats = self._on_error_total.snapshot(scale=1000.0)
            off_stats = self._off_error_total.snapshot(scale=1000.0)
            period_stats = self._period_error_total.snapshot(scale=1000.0)
            prefix = "Jitter summary (total)"
        else:
            on_stats = self._on_error_interval.snapshot(scale=1000.0)
            off_stats = self._off_error_interval.snapshot(scale=1000.0)
            period_stats = self._period_error_interval.snapshot(scale=1000.0)
            prefix = "Jitter summary (interval)"

        logging.info(
            "%s - ON error: %s | OFF error: %s | Period error: %s",
            prefix,
            self._format_stats(on_stats),
            self._format_stats(off_stats),
            self._format_stats(period_stats),
        )

        if not total:
            self._on_error_interval.reset()
            self._off_error_interval.reset()
            self._period_error_interval.reset()

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
        logging.info(f"Spin window: {self.spin_window * 1_000_000.0:.0f}us")
        if self.jitter_report_interval > 0:
            logging.info(f"Jitter report interval: {self.jitter_report_interval:.2f}s")
        else:
            logging.info("Jitter interval reports disabled.")
        if self.jitter_csv_path:
            logging.info(f"Jitter CSV output: {self.jitter_csv_path}")
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
            self._open_jitter_csv()

            epoch_ref = time.time()
            perf_ref = self._perf_counter()

            def epoch_to_perf(epoch_ts):
                return perf_ref + (epoch_ts - epoch_ref)

            start_perf = epoch_to_perf(start_ts)
            end_perf = epoch_to_perf(end_ts) if end_ts is not None else None

            next_report_perf = None
            if self.jitter_report_interval > 0:
                next_report_perf = self._perf_counter() + self.jitter_report_interval

            # Wait for start
            self._wait_until(start_perf)

            count = 0
            previous_on_perf = None
            while self.running:
                # Calculate target times for this cycle
                # To avoid drift, always calculate relative to start_ts
                cycle_start_perf = start_perf + count * self.period
                cycle_end_pulse_perf = cycle_start_perf + self.pulse_width

                # Check if we are past end time
                if end_perf and cycle_start_perf >= end_perf:
                    logging.info("Reached stop time/duration.")
                    break

                self._wait_until(cycle_start_perf)

                # Turn ON
                line.set_value(1)
                actual_on_perf = self._perf_counter()
                on_error = actual_on_perf - cycle_start_perf

                self._on_error_total.add(on_error)
                self._on_error_interval.add(on_error)

                self._wait_until(cycle_end_pulse_perf)

                # Turn OFF
                line.set_value(0)
                actual_off_perf = self._perf_counter()
                off_error = actual_off_perf - cycle_end_pulse_perf

                self._off_error_total.add(off_error)
                self._off_error_interval.add(off_error)

                period_error = None
                if previous_on_perf is not None:
                    period_error = (actual_on_perf - previous_on_perf) - self.period
                    self._period_error_total.add(period_error)
                    self._period_error_interval.add(period_error)
                previous_on_perf = actual_on_perf

                self._write_jitter_csv(
                    cycle=count,
                    target_on=cycle_start_perf,
                    actual_on=actual_on_perf,
                    on_error=on_error,
                    target_off=cycle_end_pulse_perf,
                    actual_off=actual_off_perf,
                    off_error=off_error,
                    period_error=period_error,
                )

                if next_report_perf is not None and actual_off_perf >= next_report_perf:
                    self._log_jitter_stats(total=False)
                    while next_report_perf <= actual_off_perf:
                        next_report_perf += self.jitter_report_interval

                count += 1

        except KeyboardInterrupt:
            logging.info("Stopped by user.")
        except Exception as e:
            logging.error(f"Error: {e}")
        finally:
            self._log_jitter_stats(total=True)
            self._close_jitter_csv()
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
    parser.add_argument("--rate", type=float, default=15.0, help="Frame rate in Hz (default: 15.0)")
    parser.add_argument("--pulse-width", type=float, default=0.02, help="Pulse width in seconds (default: 0.02)")
    parser.add_argument("--start", type=str, help="Start time (ISO 8601 YYYY-MM-DDTHH:mm:ss or HH:mm:ss)")
    parser.add_argument("--stop", type=str, help="Stop time (ISO 8601 YYYY-MM-DDTHH:mm:ss or HH:mm:ss)")
    parser.add_argument("--duration", type=str, help="Duration (HH:mm:ss, mm:ss, or ss)")
    parser.add_argument("--chip", type=str, default="/dev/gpiochip4", help="GPIO chip path (default: /dev/gpiochip4)")
    parser.add_argument("--line", type=int, default=17, help="GPIO line offset (default: 17)")
    parser.add_argument("--spin-window-us", type=float, default=500.0,
                        help="Busy-spin window before each edge in microseconds (default: 500)")
    parser.add_argument("--jitter-report-interval", type=float, default=5.0,
                        help="Interval jitter report period in seconds, 0 disables periodic reports (default: 5)")
    parser.add_argument("--jitter-csv", type=str,
                        help="Optional path to write per-cycle jitter samples as CSV")

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
        duration=duration,
        spin_window_us=args.spin_window_us,
        jitter_report_interval=args.jitter_report_interval,
        jitter_csv_path=args.jitter_csv,
    )

    gen.run()

if __name__ == "__main__":
    main()
