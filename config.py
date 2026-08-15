"""
config.py — AID PLUS+ BTM Single Source of Truth
======================================================
Every constant, deployment setting, hardware calibration value, and
threshold used across the BTM stack lives here. Every other module
imports from config.py rather than defining its own — this is what
actually makes "single source of truth" true: two modules can no
longer silently drift out of sync on a threshold that's supposed to
be shared (e.g. HW_SIMULATION_MODE, N_SLOTS).

Constant names are kept identical to what each originating module
used before centralisation, so this is a pure extraction — no
call-site behaviour changes anywhere downstream.

Override any runtime setting via environment variable where noted;
physical/calibration constants are direct values here since they
describe real hardware, not deployment-time choices.

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import os

# ═══════════════════════════════════════════════
#  RUNTIME / DEPLOYMENT
# ═══════════════════════════════════════════════

HW_SIMULATION_MODE = os.environ.get("BTM_HW_SIMULATION", "true").strip().lower() != "false"
DEPLOYMENT_MODE     = os.environ.get("BTM_DEPLOYMENT_MODE", "KIOSK").strip().upper()   # KIOSK | HOME | NETWORK
DEVICE_ID            = os.environ.get("BTM_DEVICE_ID", "BTM-UNIT-001")
CLOUD_ENDPOINT         = os.environ.get("BTM_CLOUD_ENDPOINT") or None   # required for HOME/NETWORK
WIFI_AVAILABLE_DEFAULT   = os.environ.get("BTM_WIFI_AVAILABLE", "true").strip().lower() != "false"
BLE_AVAILABLE_DEFAULT     = os.environ.get("BTM_BLE_AVAILABLE", "true").strip().lower() != "false"
OFFLINE_BUFFER_ENABLED      = os.environ.get("BTM_OFFLINE_BUFFER", "true").strip().lower() != "false"

# API keys — never hardcoded, always sourced from environment
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

RESULTS_OFFLINE_BUFFER_DIR = os.environ.get("BTM_OFFLINE_BUFFER_DIR", "btm_offline_queue")

# ═══════════════════════════════════════════════
#  SCANNER  (btm_sample.py)
# ═══════════════════════════════════════════════
SCANNER_BACTERIA_WINDOW_MS      = 300
SCANNER_DNA_WINDOW_MS           = 500
SCANNER_CONTAMINATION_THRESHOLD = 0.15

# ═══════════════════════════════════════════════
#  PIN STRIKE  (btm_sample.py)
# ═══════════════════════════════════════════════
PIN_BASE_DEPTH_MM               = 1.5
PIN_MIN_DEPTH_MM                = 0.8
PIN_MAX_DEPTH_MM                = 2.3
PIN_CHILD_ADJUSTMENT_MM         = -0.3
PIN_STABILIZATION_PRESSURE_KPA  = 2.0
PIN_STRIKE_VELOCITY_MS          = 0.050
PIN_RETRACTION_DELAY_MS         = 10
PIN_BLOOD_CONFIRM_TIMEOUT_S     = 5.0

# ═══════════════════════════════════════════════
#  SUCTION  (btm_sample.py)
# ═══════════════════════════════════════════════
SUCTION_INITIAL_PRESSURE_KPA    = -3.0
SUCTION_MAX_PRESSURE_KPA        = -15.0
SUCTION_INCREMENT_KPA           = -1.0
SUCTION_FLOW_CHECK_INTERVAL_S   = 0.5
SUCTION_TIMEOUT_S               = 15.0
SUCTION_TARGET_VOLUME_UL        = 50.0
SUCTION_VOLUME_PER_STEP_UL      = 5.0

# ═══════════════════════════════════════════════
#  CLEANING / SEQUENCE  (btm_sample.py)
# ═══════════════════════════════════════════════
TISSUE_DISPENSE_DURATION_S      = 1.5
POST_CLEAN_PRESSURE_S           = 3.0
TOTAL_SEQUENCE_TIMEOUT_S        = 35.0

# ═══════════════════════════════════════════════
#  HARDWARE INTERFACE  (btm_hw_interface.py)
# ═══════════════════════════════════════════════
N_SLOTS = 24   # Helix physical slot count

HW_FAILURE_PROBABILITY = {
    "pin": 0.015, "suction": 0.01, "scanner": 0.008, "helix": 0.02,
    "spinner": 0.01, "hygiene": 0.005, "vent": 0.005, "skin_probe": 0.008,
    "pressure_sensor": 0.005, "flow_sensor": 0.005, "bin_sensor": 0.005,
}

# ═══════════════════════════════════════════════
#  HELIX  (btm_helix.py)
# ═══════════════════════════════════════════════
HELIX_BIN_HEIGHT_MM     = 0.0
HELIX_SPINNER_HEIGHT_MM = 220.0
HELIX_MOVE_STEPS        = 5      # sub-moves per full cycle, for progress granularity
MAX_JAM_RETRIES         = 3

# ═══════════════════════════════════════════════
#  BIN & REPLACER  (btm_bin.py)
# ═══════════════════════════════════════════════
LOW_NEW_THRESHOLD_PCT      = 20.0
HIGH_USED_THRESHOLD_PCT    = 75.0
CRITICAL_NEW_THRESHOLD_PCT = 5.0
FULL_USED_THRESHOLD_PCT    = 95.0

# Keys match ConsumableType.value in btm_bin.py
DEFAULT_CONSUMABLE_CAPACITY = {
    "PINS": 500, "TEST_PLATES": 200, "WET_TISSUES": 300,
    "SUCTION_TUBES": 200, "COTTON_BUDS": 300,
}

# ═══════════════════════════════════════════════
#  MAINTENANCE  (btm_maintenance.py)
# ═══════════════════════════════════════════════
UNRESOLVED_ESCALATION_MINUTES = 30

# ═══════════════════════════════════════════════
#  HYGIENE  (btm_hygiene.py)
# ═══════════════════════════════════════════════
LOW_FLUID_THRESHOLD_PCT      = 20.0
CRITICAL_FLUID_THRESHOLD_PCT = 5.0

HYGIENE_DISINFECTANT_CAPACITY_ML = 500.0
HYGIENE_WATER_CAPACITY_ML        = 1000.0

# Fluid/tissue consumption per zone per cycle type
CYCLE_RECIPES = {
    "PRE_TEST":  {"disinfectant_ml": 2.0, "water_ml": 3.0, "tissue_s": 1.5,
                 "zones": ["TEST_LOBBY"]},
    "POST_TEST": {"disinfectant_ml": 4.0, "water_ml": 5.0, "tissue_s": 1.5,
                 "zones": ["TEST_LOBBY", "SUCTION_CHANNEL"]},
    "DEEP_CLEAN": {"disinfectant_ml": 15.0, "water_ml": 20.0, "tissue_s": 3.0,
                  "zones": ["TEST_LOBBY", "SUCTION_CHANNEL", "SPINNER", "COLLECTION_PLATE"]},
    "EMERGENCY": {"disinfectant_ml": 10.0, "water_ml": 10.0, "tissue_s": 2.0,
                 "zones": ["TEST_LOBBY", "SUCTION_CHANNEL"]},
}

# ═══════════════════════════════════════════════
#  VENT  (btm_vent.py)
# ═══════════════════════════════════════════════
TEMP_MIN_C          = 18.0
TEMP_MAX_C          = 30.0
TEMP_WARM_C         = 27.0
TEMP_HOT_C          = 30.0
TEMP_CRITICAL_C     = 34.0
HUMIDITY_MIN_PCT    = 30.0
HUMIDITY_MAX_PCT    = 70.0
MONITOR_INTERVAL_S  = 5.0

# ═══════════════════════════════════════════════
#  AI INTERPRETER  (btm_ai_interpreter.py)
# ═══════════════════════════════════════════════
DEFAULT_MODEL       = "claude-sonnet-5"    # current Claude API model — see docs.claude.com for updates
DEFAULT_TIMEOUT_S   = 12.0
DEFAULT_MAX_TOKENS  = 900


def summary() -> dict:
    """Flat snapshot of runtime-critical config — for startup logging/diagnostics."""
    return {
        "hw_simulation_mode": HW_SIMULATION_MODE,
        "deployment_mode": DEPLOYMENT_MODE,
        "device_id": DEVICE_ID,
        "cloud_endpoint": CLOUD_ENDPOINT,
        "n_slots": N_SLOTS,
        "ai_model": DEFAULT_MODEL,
        "anthropic_api_key_configured": ANTHROPIC_API_KEY is not None,
    }


if __name__ == "__main__":
    import json
    print("\n=== BTM Config — Summary ===\n")
    print(json.dumps(summary(), indent=2))
    print()
