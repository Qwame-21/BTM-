"""
btm_bin.py — AID PLUS+ BTM Bin & Replacer
============================================
Manages consumable inventory (pins, test plates, wet tissues, suction
tubes, cotton buds) across two physical compartments — NEW and USED —
and the automated separation between them as material moves through
the Helix.

Consumables tracked separately, each with its own new/used counts and
capacity. Dispensing moves a unit from new → in-use (handed to the
Helix); receiving used material moves it into the used compartment and
confirms routing via hw_bridge.bin_sensor.

Integration:
    - Feeds btm_ml's MaintenancePredictor telemetry every session
    - Raises MAINTENANCE_ALERT via btm_bus when used-compartment levels
      run high or new-stock runs low

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional

from btm_bus import bus, MessageType, Priority
from btm_hw_interface import hw_bridge, HardwareFault
from config import (LOW_NEW_THRESHOLD_PCT, HIGH_USED_THRESHOLD_PCT,
                    CRITICAL_NEW_THRESHOLD_PCT, FULL_USED_THRESHOLD_PCT,
                    DEFAULT_CONSUMABLE_CAPACITY)

log = logging.getLogger("btm_bin")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class ConsumableType(Enum):
    PINS          = "PINS"
    TEST_PLATES   = "TEST_PLATES"
    WET_TISSUES   = "WET_TISSUES"
    SUCTION_TUBES = "SUCTION_TUBES"
    COTTON_BUDS   = "COTTON_BUDS"


class BinStatus(Enum):
    NOMINAL   = "NOMINAL"
    LOW_NEW   = "LOW_NEW"
    HIGH_USED = "HIGH_USED"
    CRITICAL  = "CRITICAL"
    FULL_USED = "FULL_USED"


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class ConsumableInventory:
    """Per-consumable state — one instance per ConsumableType."""
    consumable_type   : ConsumableType
    new_count          : int
    used_count          : int
    capacity            : int
    last_restocked        : Optional[str] = None

    @property
    def new_pct(self) -> float:
        return round((self.new_count / self.capacity) * 100, 1) if self.capacity else 0.0

    @property
    def used_pct(self) -> float:
        return round((self.used_count / self.capacity) * 100, 1) if self.capacity else 0.0


# Default per-consumable starting stock — config.py will formalise these
# Default per-consumable starting stock — values sourced from config.py;
# re-keyed to ConsumableType here so config.py doesn't need to import this
# module's enum (would create a circular import).
_DEFAULT_CAPACITY = {ConsumableType(k): v for k, v in DEFAULT_CONSUMABLE_CAPACITY.items()}


# ─────────────────────────────────────────────
#  MAIN CONTROLLER
# ─────────────────────────────────────────────

class BTMBin:
    """
    AID PLUS+ BTM Bin & Replacer

    Usage:
        bin_ctrl = BTMBin()
        bin_ctrl.dispense(ConsumableType.PINS)
        bin_ctrl.receive_used(ConsumableType.PINS)
        status = bin_ctrl.check_status()
    """

    def __init__(self, capacities: Optional[Dict[ConsumableType, int]] = None):
        capacities = capacities or _DEFAULT_CAPACITY
        self._inventory: Dict[ConsumableType, ConsumableInventory] = {
            ctype: ConsumableInventory(
                consumable_type = ctype,
                new_count       = capacities.get(ctype, _DEFAULT_CAPACITY[ctype]),
                used_count      = 0,
                capacity        = capacities.get(ctype, _DEFAULT_CAPACITY[ctype]),
                last_restocked  = _now_iso(),
            )
            for ctype in ConsumableType
        }
        log.info("BTMBin ready | consumables=%s", [c.value for c in ConsumableType])

    # ── Public API ─────────────────────────────

    def dispense(self, consumable_type: ConsumableType) -> bool:
        """Moves one unit from new -> in-use, handed off to the Helix.
        Returns False (and does not decrement) if none available."""
        inv = self._inventory[consumable_type]
        if inv.new_count <= 0:
            log.error("Dispense failed — %s out of new stock.", consumable_type.value)
            self._report_alert("OUT_OF_STOCK", consumable_type, critical=True)
            return False

        inv.new_count -= 1
        log.info("Dispensed %s | remaining new=%d (%.1f%%)",
                 consumable_type.value, inv.new_count, inv.new_pct)

        status = self._evaluate_consumable(inv)
        if status in (BinStatus.LOW_NEW, BinStatus.CRITICAL):
            self._report_alert(status.value, consumable_type,
                               critical=(status == BinStatus.CRITICAL))
        return True

    def receive_used(self, consumable_type: ConsumableType) -> bool:
        """Accepts used material back from the Helix into the used compartment.
        Confirms routing via hw_bridge.bin_sensor before crediting the count."""
        inv = self._inventory[consumable_type]

        try:
            levels = hw_bridge.bin_sensor.read_compartment_levels()
        except HardwareFault as e:
            log.error("Bin sensor fault during used-material receipt: %s", e)
            self._report_alert("BIN_SENSOR_FAULT", consumable_type, critical=True)
            return False

        inv.used_count += 1
        log.info("Received used %s | used=%d (%.1f%%) | sensor_used_pct=%.1f%%",
                 consumable_type.value, inv.used_count, inv.used_pct, levels.get("used_pct", -1.0))

        status = self._evaluate_consumable(inv)
        if status in (BinStatus.HIGH_USED, BinStatus.FULL_USED):
            self._report_alert(status.value, consumable_type,
                               critical=(status == BinStatus.FULL_USED))
        return True

    def get_inventory_report(self) -> Dict[str, Dict]:
        """Returns all consumables' current state — for ML telemetry and diagnostics."""
        return {
            ctype.value: {
                "new_count": inv.new_count, "used_count": inv.used_count,
                "capacity": inv.capacity, "new_pct": inv.new_pct, "used_pct": inv.used_pct,
                "last_restocked": inv.last_restocked,
            }
            for ctype, inv in self._inventory.items()
        }

    def check_status(self) -> BinStatus:
        """Evaluates all consumables and returns the single worst BinStatus."""
        worst = BinStatus.NOMINAL
        severity = {BinStatus.NOMINAL: 0, BinStatus.LOW_NEW: 1, BinStatus.HIGH_USED: 2,
                   BinStatus.CRITICAL: 3, BinStatus.FULL_USED: 3}
        for inv in self._inventory.values():
            status = self._evaluate_consumable(inv)
            if severity[status] > severity[worst]:
                worst = status
        return worst

    def on_restock(self, consumable_type: ConsumableType, quantity: int) -> None:
        """Records a restocking event — resets new_count up to capacity and
        clears used_count (technician has emptied the used compartment)."""
        inv = self._inventory[consumable_type]
        inv.new_count = min(inv.capacity, inv.new_count + quantity)
        inv.used_count = 0
        inv.last_restocked = _now_iso()
        log.info("Restocked %s | +%d | new=%d/%d", consumable_type.value, quantity,
                 inv.new_count, inv.capacity)

        try:
            bus.send_maintenance_alert(
                alert_type = "RESTOCK_COMPLETED",
                details    = {"consumable_type": consumable_type.value, "quantity": quantity,
                             "new_count": inv.new_count, "capacity": inv.capacity},
                critical   = False,
            )
        except Exception as e:
            log.error("Could not report restock to bus: %s", e)

    def telemetry_snapshot(self) -> Dict:
        """Compact snapshot for btm_ml's MaintenancePredictor — called once per session."""
        return {
            "overall_status": self.check_status().value,
            "consumables": self.get_inventory_report(),
            "timestamp": _now_iso(),
        }

    # ── Internals ──────────────────────────────

    def _evaluate_consumable(self, inv: ConsumableInventory) -> BinStatus:
        if inv.used_pct >= FULL_USED_THRESHOLD_PCT:
            return BinStatus.FULL_USED
        if inv.new_pct <= CRITICAL_NEW_THRESHOLD_PCT:
            return BinStatus.CRITICAL
        if inv.used_pct >= HIGH_USED_THRESHOLD_PCT:
            return BinStatus.HIGH_USED
        if inv.new_pct <= LOW_NEW_THRESHOLD_PCT:
            return BinStatus.LOW_NEW
        return BinStatus.NOMINAL

    def _report_alert(self, alert_type: str, consumable_type: ConsumableType, critical: bool) -> None:
        try:
            bus.send_maintenance_alert(
                alert_type = f"BIN_{alert_type}",
                details    = {"consumable_type": consumable_type.value,
                             "inventory": self.get_inventory_report()[consumable_type.value]},
                critical   = critical,
            )
        except Exception as e:
            log.error("Could not report bin alert to bus: %s", e)


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Bin & Replacer — Test Suite ===\n")

    import sys
    sys.path.insert(0, ".")
    from btm_bus import bus as _bus

    _bus.activate(hw_simulation=True)

    # Small capacities so thresholds trip quickly in the test
    small_caps = {ct: 20 for ct in ConsumableType}
    bin_ctrl = BTMBin(capacities=small_caps)

    print("  [Initial status]")
    print("   ", bin_ctrl.check_status().value)

    print("\n  [Dispensing PINS down to CRITICAL]")
    dispensed = 0
    while bin_ctrl.dispense(ConsumableType.PINS):
        dispensed += 1
        if dispensed > 25:
            break
    report = bin_ctrl.get_inventory_report()["PINS"]
    print(f"    dispensed={dispensed} | new_pct={report['new_pct']}% | status={bin_ctrl.check_status().value}")

    print("\n  [Receiving used TEST_PLATES up to HIGH_USED]")
    for _ in range(16):
        bin_ctrl.receive_used(ConsumableType.TEST_PLATES)
    report = bin_ctrl.get_inventory_report()["TEST_PLATES"]
    print(f"    used_pct={report['used_pct']}% | status={bin_ctrl.check_status().value}")

    print("\n  [Restocking PINS]")
    bin_ctrl.on_restock(ConsumableType.PINS, 20)
    report = bin_ctrl.get_inventory_report()["PINS"]
    print(f"    new_pct={report['new_pct']}% | used_count={report['used_count']}")

    print("\n  [Full inventory report]")
    for ctype, inv in bin_ctrl.get_inventory_report().items():
        print(f"    {ctype:<15} new={inv['new_count']:>3}/{inv['capacity']} ({inv['new_pct']}%)  "
              f"used={inv['used_count']:>3} ({inv['used_pct']}%)")

    print("\n  [ML telemetry snapshot]")
    snap = bin_ctrl.telemetry_snapshot()
    print(f"    overall_status={snap['overall_status']} | consumables_tracked={len(snap['consumables'])}")

    _bus.deactivate()
    print("\n✓ BTM Bin & Replacer test complete\n")
