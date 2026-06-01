#!/usr/bin/env python3
"""
Botanist – Sensor Test Script
==============================
Reads all sensors and prints a formatted report every 60 seconds.
No outputs are activated. Safe to run before any wiring to outputs.

Sensors tested:
  - DHT22        temperature and humidity (board.D18)
  - ADS1115      soil moisture ADC via I2C (0x48, channel 0)
  - Overflow      digital input (physical pin 22)

Run with:
  source ~/botanist/venv/bin/activate
  python3 sensor_test.py
"""

import time
from datetime import datetime
from math import exp

import lgpio
import board
import busio
import adafruit_dht
from adafruit_ads1x15.ads1115 import ADS1115
from adafruit_ads1x15.analog_in import AnalogIn

# ── GPIO CHIP ────────────────────────────────────────────────────────────────
# Run: gpioinfo
# Find the chip that lists the most GPIO lines (usually gpiochip0 on H618)
GPIO_CHIP = 0

# ── GPIO LINE NUMBER ─────────────────────────────────────────────────────────
# TODO: Fill this in using the output of: gpioinfo
# Match physical pin 22 to its line number on the chip above.
# Example output line:  line  80: "PC16"  unused  input  active-high
# The number after "line" is what goes here.
LINE_OVERFLOW = 22   # TODO: replace with correct line number

# ── ADS1115 ──────────────────────────────────────────────────────────────────
ADS1115_I2C_ADDRESS  = 0x48
ADS1115_SOIL_CHANNEL = 0        # channel 0

# ── SOIL MOISTURE CALIBRATION ────────────────────────────────────────────────
# Update these after running: python3 botanist.py --calibrate
SOIL_ADC_DRY = 15000    # raw ADC value in open air
SOIL_ADC_WET = 8000     # raw ADC value fully submerged

# ── VPD CONSTANTS (matching botanist.py) ─────────────────────────────────────
LEAF_TEMP_OFFSET     = 2.8      # °C — leaf surface cooler than air
VPD_HUMIDITY_CEILING = 70.0     # % RH

# ── POLL INTERVAL ─────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 60

# ── INITIALISE HARDWARE (once, not inside the loop) ───────────────────────────

# GPIO chip
_h = lgpio.gpiochip_open(GPIO_CHIP)
if _h < 0:
    raise RuntimeError(f"Failed to open gpiochip{GPIO_CHIP} — check GPIO_CHIP value")

# Overflow input pin with pull-up
lgpio.gpio_claim_input(_h, LINE_OVERFLOW, lgpio.SET_PULL_UP)

# DHT22 — single persistent instance
dht_device = adafruit_dht.DHT22(board.D18)

# I2C + ADS1115 — single persistent instance
_i2c       = busio.I2C(board.SCL, board.SDA)
_ads       = ADS1115(_i2c, address=ADS1115_I2C_ADDRESS)
_soil_chan = AnalogIn(_ads, ADS1115_SOIL_CHANNEL)

# ── HELPERS ───────────────────────────────────────────────────────────────────

def clamp(value, lo, hi):
    return max(lo, min(value, hi))


def read_dht22():
    """Return (temperature_c, humidity_pct) with 3 retries."""
    for attempt in range(3):
        try:
            temp = dht_device.temperature
            hum  = dht_device.humidity
            if temp is not None and hum is not None:
                return temp, hum
        except RuntimeError:
            time.sleep(2)
    return None, None


def read_soil():
    """Return (raw_adc, moisture_percent)."""
    raw = _soil_chan.value
    pct = clamp((SOIL_ADC_DRY - raw) / (SOIL_ADC_DRY - SOIL_ADC_WET) * 100, 0, 100)
    return raw, pct


def read_overflow():
    """Return True if overflow is detected."""
    return bool(lgpio.gpio_read(_h, LINE_OVERFLOW))


def calculate_vpd(temperature_c, humidity_rh):
    """Calculate VPD in kPa using the same formula as botanist.py."""
    leaf_temp = temperature_c - LEAF_TEMP_OFFSET
    svp_leaf  = 0.6108 * exp(17.27 * leaf_temp      / (leaf_temp      + 237.3))
    svp_air   = 0.6108 * exp(17.27 * temperature_c  / (temperature_c  + 237.3))
    avp       = svp_air * (humidity_rh / 100)
    return svp_leaf - avp

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

print("Botanist Sensor Test")
print("====================")
print(f"Polling every {POLL_INTERVAL_SECONDS}s — press Ctrl+C to stop\n")

try:
    while True:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        temperature, humidity = read_dht22()
        soil_raw, soil_pct    = read_soil()
        overflow              = read_overflow()

        if temperature is not None and humidity is not None:
            vpd = calculate_vpd(temperature, humidity)
            vpd_str = f"{vpd:.3f} kPa"

            if humidity > VPD_HUMIDITY_CEILING and vpd > 1.2:
                vpd_note = "WARNING: high VPD caused by temp, not low humidity"
            elif vpd < 0.4:
                vpd_note = "too low"
            elif vpd > 1.6:
                vpd_note = "too high (danger)"
            elif vpd < 0.8:
                vpd_note = "optimal (seedling range)"
            elif vpd < 1.2:
                vpd_note = "optimal (veg range)"
            elif vpd <= 1.6:
                vpd_note = "optimal (bloom range)"
            else:
                vpd_note = "unknown"
        else:
            vpd_str  = "N/A (DHT22 read failed)"
            vpd_note = ""

        print(f"┌─ {timestamp} ───────────────────────────")
        print(f"│  Temperature  : {f'{temperature:.1f} °C' if temperature is not None else 'READ FAILED'}")
        print(f"│  Humidity     : {f'{humidity:.1f} %' if humidity is not None else 'READ FAILED'}")
        print(f"│  VPD          : {vpd_str}{f'  ({vpd_note})' if vpd_note else ''}")
        print(f"│  Soil raw ADC : {soil_raw}")
        print(f"│  Soil moisture: {soil_pct:.1f} %")
        print(f"│  Overflow     : {'DETECTED ⚠' if overflow else 'clear'}")
        print(f"└{'─' * 52}\n")

        time.sleep(POLL_INTERVAL_SECONDS)

except KeyboardInterrupt:
    print("\nStopped by user.")

finally:
    dht_device.exit()
    lgpio.gpiochip_close(_h)
    print("Hardware released cleanly.")
