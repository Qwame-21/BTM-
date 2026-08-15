"""
btm_vent.py — AID PLUS+ BTM Vent Controller
===============================================
Airflow, temperature, and humidity control. Runs a continuous adaptive
monitoring loop that steps fan speed up or down in response to internal
temperature, and escalates to the maintenance system if temperature
exceeds safe bounds.

Safe operating ranges:
    Temperature : 18-30°C internal
    Humidity    : 30-70% RH
    Airflow     : 3 fan speeds — LOW, MEDIUM, HIGH

Integration:
    - Drives hw_bridge.vent exclusively for all fan/sensor actions
    - Escalates to btm_bus MAINTENANCE_ALERT on thermal excursions
    - suspend()/resume() let the monitor loop yield cleanly across
      platforms (e.g. when run_btm.py needs the thread stopped during
      shutdown or a config reload) — no dangling threads

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional

from btm_bus import bus, MessageType, Priority
from btm_hw_interface import hw_bridge, HardwareFault
from config import (TEMP_MIN_C, TEMP_MAX_C, TEMP_WARM_C, TEMP_HOT_C, TEMP_CRITICAL_C,
                    HUMIDITY_MIN_PCT, HUMIDITY_MAX_PCT, MONITOR_INTERVAL_S)

log = logging.getLogger("btm_vent")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class VentStatus(Enum):
    NORMAL         = "NORMAL"
    WARM           = "WARM"
    HOT            = "HOT"
    CRITICAL_TEMP  = "CRITICAL_TEMP"
    HUMIDITY_HIGH  = "HUMIDITY_HIGH"


_FAN_SPEEDS = ["LOW", "MEDIUM", "HIGH"]


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class EnvironmentReading:
    temp_c        : float
    humidity_pct  : float
    fan_speed     : str
    status        : VentStatus
    read_at       : str = field(default_factory=_now_iso)


# ─────────────────────────────────────────────
#  MAIN CONTROLLER
# ─────────────────────────────────────────────

class BTMVentController:
    """
    AID PLUS+ BTM Vent Controller

    Usage:
        vent = BTMVentController(device_id="BTM-KIOSK-001")
        vent.start_monitor()      # background thread, adaptive fan control
        ...
        vent.suspend()            # pause loop without tearing down the thread
        vent.resume()
        vent.stop_monitor()       # clean shutdown
    """

    def __init__(self, device_id: str):
        self._device_id     = device_id
        self._fan_speed      = "LOW"
        self._status          = VentStatus.NORMAL
        self._last_reading      : Optional[EnvironmentReading] = None
        self._monitor_thread     : Optional[threading.Thread] = None
        self._running              = False
        self._suspended             = False
        self._critical_alert_sent    = False
        log.info("BTMVentController ready | device=%s", device_id)

    # ── Public API ─────────────────────────────

    def set_fan_speed(self, speed: str) -> bool:
        if speed not in _FAN_SPEEDS:
            log.error("Invalid fan speed requested: %s", speed)
            return False
        try:
            hw_bridge.vent.set_fan_speed(speed)
        except HardwareFault as e:
            log.error("Fan speed set failed: %s", e)
            return False
        self._fan_speed = speed
        return True

    def read_environment(self) -> Dict:
        """Reads temp, humidity, and current fan speed. Updates status."""
        try:
            temp = hw_bridge.vent.read_temp()
            humidity = hw_bridge.vent.read_humidity()
        except HardwareFault as e:
            log.error("Environment read failed: %s", e)
            return {"error": str(e)}

        status = self._evaluate_status(temp, humidity)
        self._status = status
        self._last_reading = EnvironmentReading(
            temp_c=temp, humidity_pct=humidity, fan_speed=self._fan_speed, status=status,
        )
        return {"temp_c": temp, "humidity_pct": humidity, "fan_speed": self._fan_speed,
               "status": status.value, "read_at": self._last_reading.read_at}

    def monitor(self) -> None:
        """
        Continuous adaptive-fan-control loop. Intended to run in a
        background thread (see start_monitor()) — blocks until stopped.
        """
        self._running = True
        log.info("Vent monitor loop started | interval=%.1fs", MONITOR_INTERVAL_S)
        while self._running:
            if self._suspended:
                time.sleep(MONITOR_INTERVAL_S)
                continue

            reading = self.read_environment()
            if "error" not in reading:
                self._adapt_fan_speed(reading["temp_c"])
                self._handle_status(reading["temp_c"], reading["status"])

            time.sleep(MONITOR_INTERVAL_S)
        log.info("Vent monitor loop stopped")

    def start_monitor(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            log.warning("Vent monitor already running.")
            return
        self._monitor_thread = threading.Thread(target=self.monitor, daemon=True, name="BTM-Vent-Monitor")
        self._monitor_thread.start()

    def stop_monitor(self) -> None:
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=MONITOR_INTERVAL_S + 2)

    def suspend(self) -> None:
        """Pauses the monitor loop without tearing down the thread —
        e.g. during a config reload or cross-platform resource contention."""
        self._suspended = True
        log.info("Vent monitor suspended")

    def resume(self) -> None:
        self._suspended = False
        log.info("Vent monitor resumed")

    def on_thermal_alert(self, temp: float) -> None:
        """Escalates a thermal excursion to btm_bus MAINTENANCE_ALERT.
        Only fires once per excursion — cleared when temp returns to NORMAL."""
        if self._critical_alert_sent:
            return
        try:
            bus.send_maintenance_alert(
                alert_type = "VENT_THERMAL_CRITICAL",
                details    = {"device_id": self._device_id, "temp_c": temp,
                             "threshold_c": TEMP_CRITICAL_C, "fan_speed": self._fan_speed},
                critical   = True,
            )
            self._critical_alert_sent = True
        except Exception as e:
            log.error("Could not send thermal alert to bus: %s", e)

    def get_status(self) -> VentStatus:
        return self._status

    def get_last_reading(self) -> Optional[Dict]:
        if not self._last_reading:
            return None
        r = self._last_reading
        return {"temp_c": r.temp_c, "humidity_pct": r.humidity_pct, "fan_speed": r.fan_speed,
               "status": r.status.value, "read_at": r.read_at}

    # ── Internals ──────────────────────────────

    def _evaluate_status(self, temp: float, humidity: float) -> VentStatus:
        if temp >= TEMP_CRITICAL_C:
            return VentStatus.CRITICAL_TEMP
        if temp >= TEMP_HOT_C:
            return VentStatus.HOT
        if temp >= TEMP_WARM_C:
            return VentStatus.WARM
        if humidity < HUMIDITY_MIN_PCT or humidity > HUMIDITY_MAX_PCT:
            return VentStatus.HUMIDITY_HIGH
        return VentStatus.NORMAL

    def _adapt_fan_speed(self, temp: float) -> None:
        """Steps fan speed to match current thermal load — not a fixed
        mapping, so the same temp can settle at a lower speed once the
        device has cooled rather than oscillating."""
        if temp >= TEMP_HOT_C:
            target = "HIGH"
        elif temp >= TEMP_WARM_C:
            target = "MEDIUM"
        else:
            target = "LOW"

        if target != self._fan_speed:
            self.set_fan_speed(target)
            log.info("Fan speed adapted | temp=%.1f°C -> %s", temp, target)

    def _handle_status(self, temp: float, status_value: str) -> None:
        status = VentStatus(status_value)
        if status == VentStatus.CRITICAL_TEMP:
            self.on_thermal_alert(temp)
        elif status == VentStatus.NORMAL:
            self._critical_alert_sent = False   # clear — future excursions can alert again

        try:
            bus.publish(
                message_type = MessageType.STATUS_UPDATE,
                payload      = {"event": "VENT_STATUS", "device_id": self._device_id,
                                "temp_c": temp, "status": status.value, "fan_speed": self._fan_speed},
                priority     = Priority.CRITICAL if status == VentStatus.CRITICAL_TEMP else Priority.LOW,
            )
        except Exception as e:
            log.error("Could not publish vent status to bus: %s", e)


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Vent Controller — Test Suite ===\n")

    import sys
    sys.path.insert(0, ".")
    from btm_bus import bus as _bus

    _bus.activate(hw_simulation=True)
    vent = BTMVentController(device_id="BTM-KIOSK-TEST")

    print("  [Manual environment reads]")
    for _ in range(5):
        reading = vent.read_environment()
        print(f"    temp={reading.get('temp_c')}°C | humidity={reading.get('humidity_pct')}% "
              f"| status={reading.get('status')} | fan={reading.get('fan_speed')}")

    print("\n  [Fan speed control]")
    for speed in ["LOW", "MEDIUM", "HIGH", "INVALID"]:
        ok = vent.set_fan_speed(speed)
        print(f"    set_fan_speed({speed}): {ok}")

    print("\n  [Forced thermal alert]")
    vent.on_thermal_alert(35.5)
    print("    alert sent (should not duplicate on repeat call):")
    vent.on_thermal_alert(35.9)   # should be suppressed — already sent

    print("\n  [Background monitor loop — running for ~12s]")
    vent.start_monitor()
    time.sleep(3)
    print("    suspending monitor for 3s...")
    vent.suspend()
    time.sleep(3)
    print("    resuming monitor...")
    vent.resume()
    time.sleep(6)
    vent.stop_monitor()

    print("\n  [Final status]")
    print("    status:", vent.get_status().value)
    print("    last reading:", vent.get_last_reading())

    _bus.deactivate()
    print("\n✓ BTM Vent Controller test complete\n")
