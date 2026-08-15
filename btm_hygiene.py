"""
btm_hygiene.py — AID PLUS+ BTM Hygiene Manager
==================================================
Manages the internal cleaning cycle via the Test Bolts — two dispensers
(disinfectant, distilled water) serving four cleaning zones (Test Lobby,
Suction Channel, Spinner, Collection Plate).

Cycle types:
    PRE_TEST    — quick clean immediately before sample collection
    POST_TEST   — full clean immediately after sample collected
    DEEP_CLEAN  — scheduled full internal clean between sessions
    EMERGENCY   — triggered on contamination detection (scanner flag)

Integration:
    - Drives hw_bridge.hygiene_dispenser exclusively for all fluid/tissue actions
    - Fluid levels feed btm_ml's MaintenancePredictor telemetry each session
    - Low fluid levels raise MAINTENANCE_ALERT via btm_bus

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List

from btm_bus import bus, MessageType, Priority
from btm_hw_interface import hw_bridge, HardwareFault
from config import (LOW_FLUID_THRESHOLD_PCT, CRITICAL_FLUID_THRESHOLD_PCT,
                    CYCLE_RECIPES as _CYCLE_RECIPES,
                    HYGIENE_DISINFECTANT_CAPACITY_ML, HYGIENE_WATER_CAPACITY_ML)

log = logging.getLogger("btm_hygiene")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class CleaningZone(Enum):
    TEST_LOBBY       = "TEST_LOBBY"
    SUCTION_CHANNEL  = "SUCTION_CHANNEL"
    SPINNER          = "SPINNER"
    COLLECTION_PLATE = "COLLECTION_PLATE"


class HygieneCycle(Enum):
    PRE_TEST   = "PRE_TEST"
    POST_TEST  = "POST_TEST"
    DEEP_CLEAN = "DEEP_CLEAN"
    EMERGENCY  = "EMERGENCY"


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class CycleResult:
    cycle              : HygieneCycle
    zones_cleaned        : List[str]
    disinfectant_used_ml   : float
    water_used_ml            : float
    success                    : bool
    detail                      : str = ""
    completed_at                  : str = field(default_factory=_now_iso)


# ─────────────────────────────────────────────
#  MAIN MANAGER
# ─────────────────────────────────────────────

class BTMHygieneManager:
    """
    AID PLUS+ BTM Hygiene Manager

    Usage:
        hygiene = BTMHygieneManager(device_id="BTM-KIOSK-001")
        hygiene.run_pre_test_cycle()
        ... collection happens ...
        hygiene.run_post_test_cycle()
    """

    def __init__(self, device_id: str,
                 disinfectant_capacity_ml: float = HYGIENE_DISINFECTANT_CAPACITY_ML,
                 water_capacity_ml: float = HYGIENE_WATER_CAPACITY_ML):
        self._device_id = device_id
        self._disinfectant_capacity = disinfectant_capacity_ml
        self._water_capacity = water_capacity_ml
        self._disinfectant_remaining_ml = disinfectant_capacity_ml
        self._water_remaining_ml = water_capacity_ml
        self._history: List[CycleResult] = []
        log.info("BTMHygieneManager ready | device=%s | disinfectant=%.0fml | water=%.0fml",
                 device_id, disinfectant_capacity_ml, water_capacity_ml)

    # ── Public API ─────────────────────────────

    def run_pre_test_cycle(self, session_id: str = None) -> CycleResult:
        return self._run_cycle(HygieneCycle.PRE_TEST, session_id)

    def run_post_test_cycle(self, session_id: str = None) -> CycleResult:
        return self._run_cycle(HygieneCycle.POST_TEST, session_id)

    def run_deep_clean(self) -> CycleResult:
        """Scheduled full internal clean — run between sessions, not during one."""
        return self._run_cycle(HygieneCycle.DEEP_CLEAN, session_id=None)

    def run_emergency_clean(self, session_id: str = None) -> CycleResult:
        """Triggered on contamination detection (scanner flag). Runs
        regardless of fluid thresholds that would otherwise block a
        routine cycle — contamination response takes priority."""
        return self._run_cycle(HygieneCycle.EMERGENCY, session_id, override_low_fluid=True)

    def get_fluid_levels(self) -> Dict:
        return {
            "disinfectant_pct": round((self._disinfectant_remaining_ml / self._disinfectant_capacity) * 100, 1),
            "water_pct": round((self._water_remaining_ml / self._water_capacity) * 100, 1),
            "disinfectant_remaining_ml": round(self._disinfectant_remaining_ml, 1),
            "water_remaining_ml": round(self._water_remaining_ml, 1),
        }

    def get_cycle_history(self, limit: int = 50) -> List[Dict]:
        return [
            {"cycle": r.cycle.value, "zones_cleaned": r.zones_cleaned,
             "disinfectant_used_ml": r.disinfectant_used_ml, "water_used_ml": r.water_used_ml,
             "success": r.success, "detail": r.detail, "completed_at": r.completed_at}
            for r in self._history[-limit:]
        ]

    def telemetry_snapshot(self) -> Dict:
        """Compact snapshot for btm_ml's MaintenancePredictor — called once per session."""
        return {"fluid_levels": self.get_fluid_levels(), "timestamp": _now_iso()}

    # ── Internals ──────────────────────────────

    def _run_cycle(self, cycle: HygieneCycle, session_id: str = None,
                  override_low_fluid: bool = False) -> CycleResult:
        recipe = _CYCLE_RECIPES[cycle.value]
        needed_disinfectant = recipe["disinfectant_ml"]
        needed_water = recipe["water_ml"]

        if not override_low_fluid and not self._sufficient_fluid(needed_disinfectant, needed_water):
            detail = "Insufficient fluid levels for cycle — restock required."
            log.error("%s cycle blocked | %s", cycle.value, detail)
            self._report_alert("INSUFFICIENT_FLUID", cycle, critical=True)
            result = CycleResult(cycle=cycle, zones_cleaned=[], disinfectant_used_ml=0.0,
                                 water_used_ml=0.0, success=False, detail=detail)
            self._history.append(result)
            return result

        try:
            hw_bridge.hygiene_dispenser.dispense_tissue(recipe["tissue_s"])
            hw_bridge.hygiene_dispenser.dispense_disinfectant(needed_disinfectant)
            hw_bridge.hygiene_dispenser.dispense_water(needed_water)
        except HardwareFault as e:
            log.error("%s cycle hardware fault: %s", cycle.value, e)
            self._report_alert("HYGIENE_HARDWARE_FAULT", cycle, critical=True)
            result = CycleResult(cycle=cycle, zones_cleaned=[], disinfectant_used_ml=0.0,
                                 water_used_ml=0.0, success=False, detail=str(e))
            self._history.append(result)
            return result

        self._disinfectant_remaining_ml = max(0.0, self._disinfectant_remaining_ml - needed_disinfectant)
        self._water_remaining_ml = max(0.0, self._water_remaining_ml - needed_water)

        result = CycleResult(
            cycle=cycle, zones_cleaned=recipe["zones"],
            disinfectant_used_ml=needed_disinfectant, water_used_ml=needed_water,
            success=True, detail="Cycle completed.",
        )
        self._history.append(result)
        log.info("%s cycle complete | zones=%s | disinfectant=%.1fml | water=%.1fml | remaining=%.1f%%/%.1f%%",
                 cycle.value, recipe["zones"], needed_disinfectant, needed_water,
                 self.get_fluid_levels()["disinfectant_pct"], self.get_fluid_levels()["water_pct"])

        levels = self.get_fluid_levels()
        if levels["disinfectant_pct"] <= CRITICAL_FLUID_THRESHOLD_PCT or \
           levels["water_pct"] <= CRITICAL_FLUID_THRESHOLD_PCT:
            self._report_alert("FLUID_CRITICAL", cycle, critical=True)
        elif levels["disinfectant_pct"] <= LOW_FLUID_THRESHOLD_PCT or \
             levels["water_pct"] <= LOW_FLUID_THRESHOLD_PCT:
            self._report_alert("FLUID_LOW", cycle, critical=False)

        self._publish_status(cycle, result, session_id)
        return result

    def _sufficient_fluid(self, disinfectant_ml: float, water_ml: float) -> bool:
        return (self._disinfectant_remaining_ml >= disinfectant_ml and
                self._water_remaining_ml >= water_ml)

    def _report_alert(self, alert_type: str, cycle: HygieneCycle, critical: bool) -> None:
        try:
            bus.send_maintenance_alert(
                alert_type = alert_type,
                details    = {"cycle": cycle.value, "device_id": self._device_id,
                             "fluid_levels": self.get_fluid_levels()},
                critical   = critical,
            )
        except Exception as e:
            log.error("Could not report hygiene alert to bus: %s", e)

    def _publish_status(self, cycle: HygieneCycle, result: CycleResult, session_id: str) -> None:
        try:
            bus.publish(
                message_type = MessageType.STATUS_UPDATE,
                payload      = {"event": "HYGIENE_CYCLE_COMPLETE", "cycle": cycle.value,
                                "zones_cleaned": result.zones_cleaned, "device_id": self._device_id,
                                "fluid_levels": self.get_fluid_levels()},
                priority     = Priority.NORMAL,
                session_id   = session_id,
            )
        except Exception as e:
            log.error("Could not publish hygiene status to bus: %s", e)


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Hygiene Manager — Test Suite ===\n")

    import sys
    sys.path.insert(0, ".")
    from btm_bus import bus as _bus

    _bus.activate(hw_simulation=True)
    session = _bus.open_session("AID-HYGIENE-TEST-0001")

    # Small tank sizes so thresholds trip within the test run
    hygiene = BTMHygieneManager(device_id="BTM-KIOSK-TEST",
                                disinfectant_capacity_ml=60.0, water_capacity_ml=80.0)

    print("  [Pre-test cycle]")
    r = hygiene.run_pre_test_cycle(session_id=session)
    print(f"    success={r.success} | zones={r.zones_cleaned} | levels={hygiene.get_fluid_levels()}")

    print("\n  [Post-test cycle]")
    r = hygiene.run_post_test_cycle(session_id=session)
    print(f"    success={r.success} | zones={r.zones_cleaned} | levels={hygiene.get_fluid_levels()}")

    print("\n  [Deep clean]")
    r = hygiene.run_deep_clean()
    print(f"    success={r.success} | zones={r.zones_cleaned} | levels={hygiene.get_fluid_levels()}")

    print("\n  [Running cycles until fluid runs low/critical]")
    for i in range(10):
        r = hygiene.run_post_test_cycle(session_id=session)
        levels = hygiene.get_fluid_levels()
        print(f"    cycle {i+1}: success={r.success} | disinfectant={levels['disinfectant_pct']}% "
              f"water={levels['water_pct']}%")
        if not r.success:
            break

    print("\n  [Emergency clean — should override low-fluid block]")
    r = hygiene.run_emergency_clean(session_id=session)
    print(f"    success={r.success} | detail={r.detail}")

    print("\n  [Telemetry snapshot]")
    print("   ", hygiene.telemetry_snapshot())

    print("\n  [Cycle history count]:", len(hygiene.get_cycle_history()))

    _bus.close_session(session)
    _bus.deactivate()
    print("\n✓ BTM Hygiene Manager test complete\n")
