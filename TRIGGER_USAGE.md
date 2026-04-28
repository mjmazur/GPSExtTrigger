# Trigger Generator for Raspberry Pi

This utility generates a precise external trigger signal for industrial cameras using a Raspberry Pi 5. The trigger signal is synchronized to the system clock (which should be conditioned by a GPS PPS signal via NTP for high accuracy).

## Requirements

*   Raspberry Pi 5 (recommended) or Raspberry Pi 4.
*   Raspberry Pi OS (Bookworm or newer recommended).
*   A GPS module with PPS output configured as a Stratum 1 NTP source (see [RaspberryNtpServer](https://github.com/domschl/RaspberryNtpServer) for setup instructions).
*   `libgpiod` installed.

## Installation

1.  Clone this repository.
2.  Install the required Python library:
    ```bash
    sudo apt install python3-libgpiod
    ```
    (Note: On some systems, you might need `pip install gpiod` or `pip install libgpiod`, but the system package is preferred for RPi OS).

## Wiring

Connect the camera trigger input to a GPIO pin on the Raspberry Pi.
The default configuration uses **GPIO 17** (Physical Pin 11) on **gpiochip4** (RPi 5 default).

*   **Trigger Out**: GPIO 17 (Pin 11) -> Camera Trigger +
*   **Ground**: Ground (Pin 6 or similar) -> Camera Trigger -

**Note on Voltage**: The Raspberry Pi GPIO outputs 3.3V. Ensure your camera's trigger input is compatible with 3.3V logic. If 5V is required, use a level shifter or transistor circuit.

## Usage

Run the script using Python 3:

```bash
python3 trigger_gen.py [options]
```

### Options

*   `--rate`: Frame rate in Hz. Default: `25.0`.
*   `--pulse-width`: Width of the trigger pulse in seconds. Default: `0.02` (20ms).
    *   *Note*: If not specified, it defaults to 20ms. Ensure this is compatible with your camera's requirements.
*   `--start`: Start time for the trigger.
    *   Format: ISO 8601 `YYYY-MM-DDTHH:mm:ss` or `HH:mm:ss` (for today).
    *   If omitted, the trigger starts immediately, aligned to the next whole second (PPS edge).
*   `--stop`: Stop time for the trigger.
    *   Format: ISO 8601 `YYYY-MM-DDTHH:mm:ss` or `HH:mm:ss`.
*   `--duration`: Duration to run.
    *   Format: `HH:mm:ss`, `mm:ss`, or `ss`.
*   `--chip`: Path to the GPIO chip. Default: `/dev/gpiochip4` (standard for RPi 5 user GPIO).
    *   For RPi 4, this is usually `/dev/gpiochip0`.
*   `--line`: GPIO line offset. Default: `17`.
*   `--spin-window-us`: Busy-spin window before each edge in microseconds. Default: `500`.
    *   Larger values can reduce trigger edge latency variation, but increase CPU load.
*   `--jitter-report-interval`: Print interval jitter statistics every N seconds. Default: `5`.
    *   Set to `0` to disable interval reports.
*   `--jitter-csv`: Optional CSV path to write per-cycle timing error samples.

### Examples

1.  **Start immediately at 25 fps (default):**
    ```bash
    python3 trigger_gen.py
    ```

2.  **Start at a specific time today:**
    ```bash
    python3 trigger_gen.py --start 14:30:00
    ```

3.  **Run for 10 minutes at 50 fps:**
    ```bash
    python3 trigger_gen.py --rate 50 --duration 10:00
    ```

4.  **Use GPIO 27 (Pin 13) on RPi 4:**
    ```bash
    python3 trigger_gen.py --chip /dev/gpiochip0 --line 27
    ```

5.  **Measure jitter and save samples:**
    ```bash
    python3 trigger_gen.py --rate 50 --duration 00:02:00 --spin-window-us 800 --jitter-report-interval 2 --jitter-csv jitter_log.csv
    ```

## Synchronization

The script aligns the first pulse to the specified start time (or next second). Subsequent pulses are calculated based on the start time and frame count to prevent drift accumulation. Accuracy depends on the system clock synchronization (NTP/PPS).

## Jitter Measurement

The generator reports three timing error metrics:

*   **ON error**: `actual_rising_edge_time - scheduled_rising_edge_time`
*   **OFF error**: `actual_falling_edge_time - scheduled_falling_edge_time`
*   **Period error**: `(actual_on[n] - actual_on[n-1]) - configured_period`

All summary statistics are logged in milliseconds (`mean`, `std`, `min`, `max`, `n`) for each report interval and once at shutdown (total summary).
