#!/usr/bin/env python3
import argparse
import time
import sys
import signal
import logging
import csv
import json
import threading
import os
import gc
import ctypes
import ctypes.util
from datetime import datetime, timedelta
import math

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Try to import gpiod, mock if not available (for testing or non-RPi environments)
try:
    import gpiod
    HAS_GPIOD = True
    # Detect libgpiod version: v2 has request_lines as a module-level function
    GPIOD_V2 = hasattr(gpiod, "request_lines")
except ImportError:
    gpiod = None
    HAS_GPIOD = False
    GPIOD_V2 = False
    logging.warning("gpiod module not found. Running in simulation mode.")


def open_gpio_line(chip_path, line_offset, consumer="TriggerGenerator"):
    """Open a GPIO line for output. Returns an object with set_value(0|1) and release()."""
    if not HAS_GPIOD:
        logging.info("GPIO simulation: chip=%s line=%d consumer=%s", chip_path, line_offset, consumer)
        return _MockGPIOLine(line_offset)

    if GPIOD_V2:
        return _GPIOLineV2(chip_path, line_offset, consumer)
    else:
        return _GPIOLineV1(chip_path, line_offset, consumer)


class _MockGPIOLine:
    """Simulated GPIO line for testing on non-RPi systems."""
    def __init__(self, offset):
        self._offset = offset

    def set_value(self, value):
        pass

    def release(self):
        logging.info("MockGPIO line %d released.", self._offset)


class _GPIOLineV1:
    """Wrapper for libgpiod v1 (chip.get_line / line.request / line.set_value)."""
    def __init__(self, chip_path, offset, consumer):
        self._chip = gpiod.Chip(chip_path)
        self._line = self._chip.get_line(offset)
        self._line.request(consumer=consumer, type=gpiod.LINE_REQ_DIR_OUT)
        logging.info("GPIO opened (libgpiod v1): %s line %d", chip_path, offset)

    def set_value(self, value):
        self._line.set_value(value)

    def release(self):
        self._line.release()


class _GPIOLineV2:
    """Wrapper for libgpiod v2 (gpiod.request_lines / request.set_value)."""
    def __init__(self, chip_path, offset, consumer):
        from gpiod.line import Direction, Value
        self._offset = offset
        self._Value = Value
        self._request = gpiod.request_lines(
            path=chip_path,
            consumer=consumer,
            config={offset: gpiod.LineSettings(direction=Direction.OUTPUT,
                                               output_value=Value.INACTIVE)},
        )
        logging.info("GPIO opened (libgpiod v2): %s line %d", chip_path, offset)

    def set_value(self, value):
        self._request.set_value(self._offset,
                                self._Value.ACTIVE if value else self._Value.INACTIVE)

    def release(self):
        self._request.release()


class LEDBlinker:
    """Blinks the on-board LED (if available) at a 1-second interval via sysfs."""

    # Common sysfs LED paths on Raspberry Pi (ACT / led0)
    _CANDIDATE_PATHS = [
        "/sys/class/leds/ACT",
        "/sys/class/leds/led0",
    ]

    def __init__(self):
        self._led_path = None
        self._original_trigger = None
        self._thread = None
        self._stop_event = threading.Event()

        for path in self._CANDIDATE_PATHS:
            if os.path.isdir(path):
                self._led_path = path
                break

        if self._led_path is None:
            logging.info("No on-board LED found; LED heartbeat disabled.")

    @property
    def available(self):
        return self._led_path is not None

    def start(self):
        """Take manual control of the LED and start the blink thread."""
        if not self.available:
            return
        try:
            trigger_path = os.path.join(self._led_path, "trigger")
            with open(trigger_path, "r") as f:
                # The active trigger is enclosed in [brackets]
                raw = f.read()
                for token in raw.split():
                    if token.startswith("[") and token.endswith("]"):
                        self._original_trigger = token[1:-1]
                        break
            # Switch to manual ("none") so we can drive brightness directly
            with open(trigger_path, "w") as f:
                f.write("none")
            logging.info("On-board LED heartbeat started (%s).", self._led_path)
        except OSError as exc:
            logging.warning("Could not take control of LED: %s", exc)
            self._led_path = None
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop blinking and restore the LED to its original trigger."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=3)
        self._thread = None

        if self._led_path:
            try:
                # Turn the LED off
                with open(os.path.join(self._led_path, "brightness"), "w") as f:
                    f.write("0")
                # Restore original trigger
                if self._original_trigger is not None:
                    with open(os.path.join(self._led_path, "trigger"), "w") as f:
                        f.write(self._original_trigger)
                    logging.info("LED trigger restored to '%s'.", self._original_trigger)
            except OSError as exc:
                logging.warning("Could not restore LED state: %s", exc)

    def _run(self):
        brightness_path = os.path.join(self._led_path, "brightness")
        state = False
        while not self._stop_event.is_set():
            state = not state
            try:
                with open(brightness_path, "w") as f:
                    f.write("1" if state else "0")
            except OSError:
                break
            self._stop_event.wait(1.0)

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

# ── Real-time helpers ──────────────────────────────────────────────────────

def _setup_realtime(cpu_affinity=None, rt_priority=90):
    """Apply real-time scheduling, memory locking, and CPU affinity.

    Requires root / CAP_SYS_NICE.  Each step is attempted independently;
    failures are logged as warnings so the script still runs.
    """
    applied = []

    # 1. SCHED_FIFO
    try:
        SCHED_FIFO = 1
        param = os.sched_param(rt_priority)
        os.sched_setscheduler(0, SCHED_FIFO, param)
        applied.append(f"SCHED_FIFO priority {rt_priority}")
    except (OSError, PermissionError) as exc:
        logging.warning("Could not set SCHED_FIFO (run as root / sudo): %s", exc)
    except AttributeError:
        logging.warning("os.sched_setscheduler not available on this platform.")

    # 2. CPU affinity
    if cpu_affinity is not None:
        try:
            os.sched_setaffinity(0, {cpu_affinity})
            applied.append(f"CPU affinity -> core {cpu_affinity}")
        except (OSError, PermissionError) as exc:
            logging.warning("Could not set CPU affinity: %s", exc)
        except AttributeError:
            logging.warning("os.sched_setaffinity not available on this platform.")

    # 3. mlockall  (MCL_CURRENT | MCL_FUTURE = 1 | 2 = 3)
    try:
        libc_name = ctypes.util.find_library("c")
        if libc_name:
            libc = ctypes.CDLL(libc_name, use_errno=True)
            MCL_CURRENT_FUTURE = 3
            if libc.mlockall(MCL_CURRENT_FUTURE) == 0:
                applied.append("mlockall (MCL_CURRENT | MCL_FUTURE)")
            else:
                errno = ctypes.get_errno()
                logging.warning("mlockall failed (errno %d). Run as root for memory locking.", errno)
        else:
            logging.warning("Could not locate libc for mlockall.")
    except Exception as exc:
        logging.warning("mlockall unavailable: %s", exc)

    if applied:
        logging.info("Real-time optimisations applied: %s", ", ".join(applied))
    else:
        logging.warning("No real-time optimisations could be applied.")


class TriggerGenerator:
    def __init__(self, rate, pulse_width, chip_path, line_offset, start_time=None, stop_time=None, duration=None,
                 spin_window_us=500.0, jitter_report_interval=5.0, jitter_csv_path=None,
                 stats_json_path=None, realtime=False, cpu_affinity=None):
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
        self.stats_json_path = stats_json_path
        self.realtime = realtime
        self.cpu_affinity = cpu_affinity
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

    def _write_stats_json(self, count, elapsed_s):
        """Write final run statistics to a JSON file."""
        if not self.stats_json_path:
            return
        data = {
            "run": {
                "rate_hz": self.rate,
                "period_s": self.period,
                "pulse_width_s": self.pulse_width,
                "spin_window_us": self.spin_window * 1_000_000.0,
                "total_cycles": count,
                "elapsed_s": round(elapsed_s, 6),
                "chip": self.chip_path,
                "line_offset": self.line_offset,
                "start_time": self.start_time.isoformat() if self.start_time else None,
                "stop_time": self.stop_time.isoformat() if self.stop_time else None,
            },
            "jitter_ms": {
                "on_error": self._on_error_total.snapshot(scale=1000.0),
                "off_error": self._off_error_total.snapshot(scale=1000.0),
                "period_error": self._period_error_total.snapshot(scale=1000.0),
            },
            "jitter_us": {
                "on_error": self._on_error_total.snapshot(scale=1_000_000.0),
                "off_error": self._off_error_total.snapshot(scale=1_000_000.0),
                "period_error": self._period_error_total.snapshot(scale=1_000_000.0),
            },
        }
        try:
            with open(self.stats_json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logging.info("Statistics written to %s", self.stats_json_path)
        except OSError as exc:
            logging.error("Failed to write stats JSON: %s", exc)

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

        # Apply real-time optimisations before entering the timing loop
        if self.realtime:
            _setup_realtime(cpu_affinity=self.cpu_affinity)
        elif self.cpu_affinity is not None:
            # Allow affinity without full RT mode
            try:
                os.sched_setaffinity(0, {self.cpu_affinity})
                logging.info("CPU affinity set to core %d.", self.cpu_affinity)
            except (OSError, AttributeError) as exc:
                logging.warning("Could not set CPU affinity: %s", exc)

        # GPIO Setup
        line = None
        count = 0
        start_perf = self._perf_counter()
        gc_was_enabled = gc.isenabled()

        led = LEDBlinker()

        try:
            line = open_gpio_line(self.chip_path, self.line_offset)

            self.running = True
            self._open_jitter_csv()
            led.start()

            epoch_ref = time.time()
            perf_ref = self._perf_counter()

            def epoch_to_perf(epoch_ts):
                return perf_ref + (epoch_ts - epoch_ref)

            # 4. Disable garbage collection during the timing-critical loop
            if gc_was_enabled:
                gc.disable()
                logging.info("Garbage collection disabled for timing loop.")

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
            # Re-enable GC before cleanup
            if gc_was_enabled and not gc.isenabled():
                gc.enable()
            run_end_perf = self._perf_counter()
            led.stop()
            self._log_jitter_stats(total=True)
            self._write_stats_json(count, run_end_perf - start_perf)
            self._close_jitter_csv()
            if line:
                line.release()

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
    parser.add_argument("--stats-json", type=str,
                        help="Optional path to write final run statistics as JSON")
    parser.add_argument("--realtime", action="store_true",
                        help="Enable real-time optimisations: SCHED_FIFO, mlockall, GC disable (requires root)")
    parser.add_argument("--cpu-affinity", type=int, default=None, metavar="CORE",
                        help="Pin process to a specific CPU core (e.g. 3). Use with --realtime for best results.")

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
        stats_json_path=args.stats_json,
        realtime=args.realtime,
        cpu_affinity=args.cpu_affinity,
    )

    gen.run()

if __name__ == "__main__":
    main()
