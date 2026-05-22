#!/usr/bin/env python3
"""
Plant Growth Monitor System
===========================

A complete monitoring and control system for indoor plant cultivation with:
- Soil moisture sensing via ADS1115 ADC
- Environmental monitoring via DHT22 sensor
- Automated control of grow lights, fan, pump, and misting
- Web-based dashboard for monitoring and control
- VPD (Vapor Pressure Deficit) management
"""

import sys
import time
import json
import threading
import sqlite3
import logging
from datetime import datetime, timedelta
from math import exp
import OPi.GPIO as GPIO
from adafruit_ads1x15.ads1115 import ADS1115
from adafruit_ads1x15.analog_in import AnalogIn
import board
import busio
import adafruit_dht
from flask import Flask, render_template, request, jsonify, send_from_directory
import os

# ── CONFIGURATION BLOCK ──────────────────────────────────────────────────────

# ── GPIO MODE ────────────────────────────────────────────────────────────────
GPIO.setmode(GPIO.BOARD)   # physical pin numbering

# ── MOSFET LOGIC (active LOW — normally closed modules) ─────────────────────
ACTIVE_ON  = GPIO.HIGH   # GPIO HIGH  = device ON
ACTIVE_OFF = GPIO.LOW  # GPIO LOW   = device OFF

# ── GPIO PIN ASSIGNMENTS (physical board numbering) ─────────────────────────
PIN_GROW_LIGHT = 11   # GPIO 17 (physical pin 11)
PIN_FAN        = 13   # GPIO 27 (physical pin 13)
PIN_PUMP       = 15   # GPIO 22 (physical pin 15)
PIN_MISTER     = 16   # GPIO 23 (physical pin 16)
PIN_DHT22      = 18   # GPIO 24 (physical pin 18)
PIN_OVERFLOW   = 22   # GPIO 25 (physical pin 22)

# ── ADS1115 ──────────────────────────────────────────────────────────────────
ADS1115_I2C_ADDRESS  = 0x48
ADS1115_SOIL_CHANNEL = 0    # ADS1115 channel the soil sensor is wired to

# ── SOIL MOISTURE CALIBRATION (raw ADC values) ───────────────────────────────
# Run calibration helper to determine these values before deployment
SOIL_ADC_DRY = 15000    # ADC reading with sensor in open air
SOIL_ADC_WET = 8000     # ADC reading with sensor fully in water

# ── MOISTURE THRESHOLDS PER STAGE (%) ────────────────────────────────────────
MOISTURE_THRESHOLD_SEEDLING = 60
MOISTURE_THRESHOLD_VEG      = 55
MOISTURE_THRESHOLD_BLOOM    = 50

# ── WATER DISPENSE VOLUMES PER STAGE (ml) ────────────────────────────────────
WATER_VOLUME_SEEDLING = 100
WATER_VOLUME_VEG      = 500
WATER_VOLUME_BLOOM    = 1000

# ── PUMP ──────────────────────────────────────────────────────────────────────
PUMP_FLOW_RATE_ML_PER_SEC = 25.0  # measure physically before deployment

# ── MIST ──────────────────────────────────────────────────────────────────────
MAX_MIST_DURATION_SECONDS    = 60   # hard ceiling, never exceeded regardless of input
MIST_DURATION_PER_VPD_CYCLE  = 30   # seconds of mist per VPD trigger event

# ── VPD TARGETS PER STAGE (kPa) ──────────────────────────────────────────────
VPD_SEEDLING_MIN = 0.4
VPD_SEEDLING_MAX = 0.8
VPD_VEG_MIN      = 0.8
VPD_VEG_MAX      = 1.2
VPD_BLOOM_MIN    = 1.2
VPD_BLOOM_MAX    = 1.6
VPD_DANGER_LOW   = 0.4
VPD_DANGER_HIGH  = 1.6
VPD_HYSTERESIS   = 0.1
LEAF_TEMP_OFFSET = 2.8   # °C, leaf surface is cooler than air

# ── VPD HUMIDITY CEILING (warn if humidity already high but VPD still high) ───
VPD_HUMIDITY_CEILING = 70.0   # % RH — above this, high VPD is caused by
                               # temperature not low humidity; warn instead of misting

# ── LIGHT SCHEDULES (hours ON per 24h cycle) ─────────────────────────────────
LIGHT_HOURS_SEEDLING = 18    # seedling always 18/6h
LIGHT_HOURS_VEG_18_6 = 18    # veg 18/6 mode
LIGHT_HOURS_VEG_24H  = 24    # veg 24h mode
LIGHT_HOURS_BLOOM    = 12    # bloom fixed 12/12

# ── LOGGING & DATA RETENTION ─────────────────────────────────────────────────
LOG_INTERVAL_SECONDS  = 600   # 10 minutes
DATA_RETENTION_MONTHS = 12    # rows older than this are deleted on each write

# ── WEB INTERFACE ───────────────────────────────────────────────────────────
WEB_PORT = 5000

# ── GLOBAL VARIABLES ─────────────────────────────────────────────────────────

# Database lock
db_lock = threading.Lock()
state_lock = threading.Lock()

# Current state shared with Flask
current_state = {
    "soil_raw": None, "soil_pct": None,
    "temperature": None, "humidity": None,
    "vpd": None, "vpd_status": None,
    "stage": None, "overflow": None,
    "grow_light": None, "fan": None,
    "mister": None, "pump": None,
    "warning": None,
    "timestamp": None
}

# Global configuration
config = {
    "stage": "seedling",
    "light_mode": "18_6",
    "light_start_time": "06:00",
    "grow_light_forced": "auto",
    "fan_forced": "auto"
}

# File paths
CONFIG_FILE = "config.json"
DATABASE_FILE = "plant_monitor.db"
FLASK_APP_DIR = "web"

# ── DATABASE SETUP ───────────────────────────────────────────────────────────

def init_database():
    """Initialize the SQLite database with the required schema."""
    with db_lock:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS readings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    DATETIME DEFAULT CURRENT_TIMESTAMP,
                soil_raw     INTEGER,
                soil_pct     REAL,
                temperature  REAL,
                humidity     REAL,
                vpd          REAL,
                vpd_status   TEXT,
                stage        TEXT,
                overflow     INTEGER,
                grow_light   INTEGER,
                fan          INTEGER,
                mister       INTEGER,
                pump         INTEGER,
                warning      TEXT
            )
        ''')
        
        conn.commit()
        conn.close()

# ── CONFIGURATION MANAGEMENT ─────────────────────────────────────────────────

def load_config():
    """Load configuration from JSON file or use defaults."""
    global config
    try:
        with open(CONFIG_FILE, 'r') as f:
            loaded_config = json.load(f)
            # Merge with defaults
            for key, value in loaded_config.items():
                config[key] = value
    except FileNotFoundError:
        save_config()  # Create default config file
    except json.JSONDecodeError:
        print("Error: Invalid JSON in config file. Using defaults.")

def save_config():
    """Save current configuration to JSON file."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

# ── SENSOR READING FUNCTIONS ─────────────────────────────────────────────────

def pull_sensor_data():
    """Read all sensor data and return as a dictionary."""
    # Read soil moisture
    soil_raw = read_soil_moisture()
    soil_pct = clamp((SOIL_ADC_DRY - soil_raw) / (SOIL_ADC_DRY - SOIL_ADC_WET) * 100, 0, 100)
    
    # Read DHT22 with retry logic
    temperature = None
    humidity = None
    for attempt in range(3):
        try:
            dht = adafruit_dht.DHT22(board.D18)
            temperature = dht.temperature
            humidity = dht.humidity
            if temperature is not None and humidity is not None:
                break
        except RuntimeError as e:
            time.sleep(2)
            continue
    
    # If all attempts failed, use last known good values
    if temperature is None or humidity is None:
        temperature = current_state["temperature"] or 25.0
        humidity = current_state["humidity"] or 50.0
        print("Warning: Failed to read DHT22 after 3 attempts. Using last known values.")
    
    # Read overflow sensor
    overflow = GPIO.input(PIN_OVERFLOW)
    
    # Calculate VPD
    vpd_result = calculate_vpd(temperature, humidity, config["stage"])
    
    return {
        "soil_raw": soil_raw,
        "soil_pct": soil_pct,
        "temperature": temperature,
        "humidity": humidity,
        "vpd": vpd_result["vpd"],
        "vpd_status": vpd_result["vpd_status"],
        "stage": config["stage"],
        "overflow": overflow,
        "warning": vpd_result["warning"]
    }

def read_soil_moisture():
    """Read soil moisture from ADS1115 ADC."""
    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS1115(i2c, address=ADS1115_I2C_ADDRESS)
    channel = AnalogIn(ads, ADS1115_SOIL_CHANNEL)
    return channel.value

# ── VPD CALCULATION ───────────────────────────────────────────────────────────

def calculate_vpd(temperature_c, humidity_rh, stage):
    """Calculate VPD (Vapor Pressure Deficit) and determine status."""
    # Calculate leaf temperature
    leaf_temp = temperature_c - LEAF_TEMP_OFFSET
    
    # Calculate saturation vapor pressure for leaf and air
    svp_leaf = 0.6108 * exp(17.27 * leaf_temp / (leaf_temp + 237.3))
    svp_air = 0.6108 * exp(17.27 * temperature_c / (temperature_c + 237.3))
    
    # Calculate actual vapor pressure
    avp = svp_air * (humidity_rh / 100)
    
    # Calculate VPD
    vpd = svp_leaf - avp
    
    # Determine stage thresholds
    if stage == "seedling":
        vpd_min = VPD_SEEDLING_MIN
        vpd_max = VPD_SEEDLING_MAX
    elif stage == "veg":
        vpd_min = VPD_VEG_MIN
        vpd_max = VPD_VEG_MAX
    elif stage == "bloom":
        vpd_min = VPD_BLOOM_MIN
        vpd_max = VPD_BLOOM_MAX
    else:
        vpd_min = VPD_SEEDLING_MIN
        vpd_max = VPD_SEEDLING_MAX
    
    # Determine VPD status with hysteresis
    if vpd < vpd_min - VPD_HYSTERESIS:
        vpd_status = "too_low"
    elif vpd > vpd_max + VPD_HYSTERESIS:
        vpd_status = "too_high"
    elif vpd < vpd_min:
        vpd_status = "danger_low"
    elif vpd > vpd_max:
        vpd_status = "danger_high"
    else:
        vpd_status = "optimal"
    
    # Check for warning condition
    warning = None
    if (vpd_status == "too_high" or vpd_status == "danger_high") and humidity_rh > VPD_HUMIDITY_CEILING:
        warning = "warning_temp"
        vpd_status = "warning_temp"
    
    return {
        "vpd": vpd,
        "vpd_status": vpd_status,
        "warning": warning
    }

# ── DEVICE CONTROL FUNCTIONS ─────────────────────────────────────────────────

def dispense_water(ml):
    """Dispense water for specified volume."""
    # Check overflow sensor
    if current_state["overflow"]:
        warning = "pump aborted: overflow detected"
        print(warning)
        return
    
    # Calculate duration
    duration = ml / PUMP_FLOW_RATE_ML_PER_SEC
    
    # Activate pump
    GPIO.output(PIN_PUMP, ACTIVE_ON)
    current_state["pump"] = 1
    time.sleep(duration)
    GPIO.output(PIN_PUMP, ACTIVE_OFF)
    current_state["pump"] = 0

def exhaust_fan(state, forced=False):
    """Control exhaust fan."""
    if forced:
        # Update config
        config["fan_forced"] = "on" if state else "off"
        save_config()
        # Set GPIO
        GPIO.output(PIN_FAN, ACTIVE_ON if state else ACTIVE_OFF)
        current_state["fan"] = 1 if state else 0
        return
    
    # If not forced, check config
    if config["fan_forced"] != "auto":
        return
    
    # Set GPIO
    GPIO.output(PIN_FAN, ACTIVE_ON if state else ACTIVE_OFF)
    current_state["fan"] = 1 if state else 0

def dispense_mist(duration_seconds=MIST_DURATION_PER_VPD_CYCLE):
    """Dispense mist for specified duration."""
    # Clamp duration
    duration_seconds = min(duration_seconds, MAX_MIST_DURATION_SECONDS)
    
    # Save current fan state
    fan_was_on = current_state["fan"] == 1
    
    # Turn off fan if it's on
    if fan_was_on:
        exhaust_fan(False)
    
    # Activate mister
    GPIO.output(PIN_MISTER, ACTIVE_ON)
    current_state["mister"] = 1
    time.sleep(duration_seconds)
    GPIO.output(PIN_MISTER, ACTIVE_OFF)
    current_state["mister"] = 0
    
    # Restore fan if it was on
    if fan_was_on:
        exhaust_fan(True)

def grow_light(state, forced=False):
    """Control grow light."""
    if forced:
        # Update config
        config["grow_light_forced"] = "on" if state else "off"
        save_config()
        # Set GPIO
        GPIO.output(PIN_GROW_LIGHT, ACTIVE_ON if state else ACTIVE_OFF)
        current_state["grow_light"] = 1 if state else 0
        return
    
    # If not forced, check config
    if config["grow_light_forced"] != "auto":
        return
    
    # Set GPIO
    GPIO.output(PIN_GROW_LIGHT, ACTIVE_ON if state else ACTIVE_OFF)
    current_state["grow_light"] = 1 if state else 0

def get_light_schedule():
    """Determine if lights should be on based on schedule."""
    stage = config["stage"]
    light_mode = config["light_mode"]
    light_start_time = config["light_start_time"]
    
    # Parse start time
    hour, minute = map(int, light_start_time.split(":"))
    start_time = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    
    # Calculate hours on
    if stage == "seedling":
        hours_on = LIGHT_HOURS_SEEDLING
    elif stage == "veg":
        hours_on = LIGHT_HOURS_VEG_24H if light_mode == "24h" else LIGHT_HOURS_VEG_18_6
    elif stage == "bloom":
        hours_on = LIGHT_HOURS_BLOOM
    else:
        hours_on = LIGHT_HOURS_SEEDLING
    
    # Calculate end time
    end_time = start_time + timedelta(hours=hours_on)
    
    # Check if current time is within the light period
    now = datetime.now()
    if start_time <= now < end_time:
        return True
    elif start_time > now:
        # If start time is in the future, check if we're in the previous period
        previous_start = start_time - timedelta(days=1)
        previous_end = previous_start + timedelta(hours=hours_on)
        return previous_start <= now < previous_end
    else:
        # Check if we're in the period that spans midnight
        next_start = start_time + timedelta(days=1)
        next_end = next_start + timedelta(hours=hours_on)
        return next_start <= now < next_end

# ── DATA LOGGING ──────────────────────────────────────────────────────────────

def log_data(sensor_dict, device_states, warning=None):
    """Log sensor data to database."""
    with db_lock:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Insert data
        cursor.execute('''
            INSERT INTO readings (
                soil_raw, soil_pct, temperature, humidity, vpd, vpd_status, stage,
                overflow, grow_light, fan, mister, pump, warning
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            sensor_dict["soil_raw"], sensor_dict["soil_pct"], sensor_dict["temperature"],
            sensor_dict["humidity"], sensor_dict["vpd"], sensor_dict["vpd_status"],
            sensor_dict["stage"], sensor_dict["overflow"], device_states["grow_light"],
            device_states["fan"], device_states["mister"], device_states["pump"],
            warning
        ))
        
        # Delete old data
        cutoff_date = datetime.now() - timedelta(days=30*DATA_RETENTION_MONTHS)
        cursor.execute('DELETE FROM readings WHERE timestamp < ?', (cutoff_date,))
        
        conn.commit()
        conn.close()

# ── CALIBRATION HELPER ───────────────────────────────────────────────────────

def run_calibration():
    """Run calibration to determine soil moisture thresholds."""
    print("Soil Moisture Calibration")
    print("===========================")
    print("1. Place sensor in open air")
    print("2. Record the raw ADC value below")
    print("3. Submerge sensor in water")
    print("4. Record the raw ADC value below")
    print("5. Press Ctrl+C to exit")
    print()
    
    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS1115(i2c, address=ADS1115_I2C_ADDRESS)
    channel = AnalogIn(ads, ADS1115_SOIL_CHANNEL)
    
    try:
        while True:
            raw = channel.value
            print(f"Raw ADC value: {raw}")
            time.sleep(2)
    except KeyboardInterrupt:
        print("Calibration cancelled.")

# ── UTILITY FUNCTIONS ────────────────────────────────────────────────────────

def clamp(value, min_value, max_value):
    """Clamp a value between min and max."""
    return max(min_value, min(value, max_value))

# ── FLASK WEB INTERFACE ──────────────────────────────────────────────────────

app = Flask(__name__, template_folder=FLASK_APP_DIR)

@app.route('/')
def dashboard():
    """Serve the main dashboard page."""
    return render_template('index.html')

@app.route('/data')
def get_data():
    """Return current sensor and device data."""
    with state_lock:
        return jsonify(current_state)

@app.route('/history')
def get_history():
    """Return historical data for graphing."""
    hours = int(request.args.get('hours', 24))
    cutoff_date = datetime.now() - timedelta(hours=hours)
    
    with db_lock:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, soil_pct, temperature, humidity, vpd, vpd_status
            FROM readings
            WHERE timestamp >= ?
            ORDER BY timestamp
        ''', (cutoff_date,))
        
        rows = cursor.fetchall()
        conn.close()
        
        # Convert to list of dicts
        data = []
        for row in rows:
            data.append({
                'timestamp': row[0],
                'soil_pct': row[1],
                'temperature': row[2],
                'humidity': row[3],
                'vpd': row[4],
                'vpd_status': row[5]
            })
        
        return jsonify(data)

@app.route('/stage', methods=['POST'])
def set_stage():
    """Set the growth stage."""
    try:
        stage = request.json.get('stage')
        if stage not in ['seedling', 'veg', 'bloom']:
            return jsonify({'success': False, 'error': 'Invalid stage'}), 400
        
        config["stage"] = stage
        save_config()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/override', methods=['POST'])
def override_device():
    """Override device state."""
    try:
        device = request.json.get('device')
        state = request.json.get('state')
        
        if device not in ['grow_light', 'fan']:
            return jsonify({'success': False, 'error': 'Invalid device'}), 400
        
        if state not in ['auto', 'on', 'off']:
            return jsonify({'success': False, 'error': 'Invalid state'}), 400
        
        # Apply override
        if device == 'grow_light':
            grow_light(state == 'on', forced=True)
        elif device == 'fan':
            exhaust_fan(state == 'on', forced=True)
        
        # Update config
        config[f"{device}_forced"] = state
        save_config()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ── MAIN LOOP ────────────────────────────────────────────────────────────────

def main_loop():
    """Main monitoring loop."""
    # Initialize GPIO
    GPIO.setup(PIN_GROW_LIGHT, GPIO.OUT)
    GPIO.setup(PIN_FAN, GPIO.OUT)
    GPIO.setup(PIN_PUMP, GPIO.OUT)
    GPIO.setup(PIN_MISTER, GPIO.OUT)
    GPIO.setup(PIN_OVERFLOW, GPIO.IN)
    
    # Set initial states
    GPIO.output(PIN_GROW_LIGHT, ACTIVE_OFF)
    GPIO.output(PIN_FAN, ACTIVE_ON)  # Fan on by default
    GPIO.output(PIN_PUMP, ACTIVE_OFF)
    GPIO.output(PIN_MISTER, ACTIVE_OFF)
    
    # Initialize current state
    current_state["grow_light"] = 0
    current_state["fan"] = 1
    current_state["pump"] = 0
    current_state["mister"] = 0
    
    # Load configuration
    load_config()
    
    # Initialize database
    init_database()
    
    print("Plant Growth Monitor System Started")
    print("===================================")
    
    # Main loop
    while True:
        try:
            # Read sensor data
            sensor_data = pull_sensor_data()
            
            # Update current state
            with state_lock:
                current_state.update(sensor_data)
                current_state["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Determine moisture threshold and water volume
            if config["stage"] == "seedling":
                threshold = MOISTURE_THRESHOLD_SEEDLING
                volume = WATER_VOLUME_SEEDLING
            elif config["stage"] == "veg":
                threshold = MOISTURE_THRESHOLD_VEG
                volume = WATER_VOLUME_VEG
            elif config["stage"] == "bloom":
                threshold = MOISTURE_THRESHOLD_BLOOM
                volume = WATER_VOLUME_BLOOM
            else:
                threshold = MOISTURE_THRESHOLD_SEEDLING
                volume = WATER_VOLUME_SEEDLING
            
            # Water if needed
            if sensor_data["soil_pct"] < threshold and not sensor_data["overflow"]:
                dispense_water(volume)
            
            # Handle VPD
            vpd_status = sensor_data["vpd_status"]
            if vpd_status in ["too_low", "danger_low"]:
                # Turn off fan briefly to allow humidity to rise
                exhaust_fan(False)
            elif vpd_status in ["too_high", "danger_high"] and sensor_data["humidity"] < VPD_HUMIDITY_CEILING:
                # Misting to reduce VPD
                dispense_mist()
            elif vpd_status == "warning_temp":
                # Warning condition - no misting
                pass
            elif vpd_status == "optimal":
                # Ensure fan is on if it was paused
                if config["fan_forced"] == "auto":
                    exhaust_fan(True)
            
            # Handle light schedule
            lights_on = get_light_schedule()
            grow_light(lights_on)
            
            # Log data
            device_states = {
                "grow_light": current_state["grow_light"],
                "fan": current_state["fan"],
                "mister": current_state["mister"],
                "pump": current_state["pump"]
            }
            log_data(sensor_data, device_states, sensor_data["warning"])
            
            # Sleep for next cycle
            time.sleep(LOG_INTERVAL_SECONDS)
            
        except KeyboardInterrupt:
            print("Shutting down...")
            GPIO.cleanup()
            break
        except Exception as e:
            print(f"Error in main loop: {e}")
            time.sleep(60)  # Wait before retrying

# ── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Check for calibration argument
    if len(sys.argv) > 1 and sys.argv[1] == "--calibrate":
        run_calibration()
        sys.exit(0)
    
    # Start Flask in a background thread
    flask_thread = threading.Thread(target=lambda: app.run(
        host='0.0.0.0', 
        port=WEB_PORT, 
        use_reloader=False, 
        threaded=True
    ), daemon=True)
    flask_thread.start()
    
    # Start main monitoring loop
    main_loop()
