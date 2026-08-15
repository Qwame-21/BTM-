"""
btm_helix.py — AID PLUS+ BTM Helix Transport Controller
===========================================================
Orchestrates the full DNA-spiral lift cycle: upward transport of new
material from the Bin to the Collective Spinner, and downward return of
used material from the Spinner back to the Bin.

This module owns sequencing, retries, and callbacks. All physical
movement state (current position, jam detection) lives in
btm_hw_interface.py — btm_helix.py drives hw_bridge.helix and
hw_bridge.spinner, it never talks to hardware directly.

State machine (descriptive stages, published on every HARDWARE_EVENT):
    IDLE → HOMING → READY → LOADING → TRANSPORTING → DELIVERING →
    RETURNING → UNLOADING → READY

HelixStatus (the coarse status the rest of the system reads):
    IDLE, HOMING, READY, MOVING, JAM_DETECTED, ERROR
    (the finer LOADING/TRANSPORTING/DELIVERING/RETURNING/UNLOADING
    stages above are all reported as MOVING at the status level, with
    the specific stage carried in the HARDWARE_EVENT payload)

Movement model:
    Each full cycle (Bin ⇄ Spinner) is broken into HELIX_MOVE_STEPS
    sub-moves so progress callbacks are meaningful rather than a single
    jump from 0% to 100%. Height ramps smoothly across all sub-steps;
    the slot index changes on the final sub-step — modelling the lift
    rising along its current column before rotating into the target
    slot, which is mechanically how a spiral lift path behaves.

Jam handling:
    A jam (hw_bridge.helix.move_to_position returning False) is a
    normal, recoverable operational event — retried up to
    MAX_JAM_RETRIES times with a short settle delay between attempts.
    Only after retries are exhausted does this escalate to a critical
    maintenance alert on the bus.

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, Optional

from btm_bus import bus, MessageType, Priority
from btm_hw_interface import hw_bridge, HardwareFault, N_SLOTS
from config import HELIX_BIN_HEIGHT_MM, HELIX_SPINNER_HEIGHT_MM, HELIX_MOVE_STEPS, MAX_JAM_RETRIES

log = logging.getLogger("btm_helix")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class HelixCycle(Enum):
    UPWARD_NEW    = "UPWARD_NEW"     # Bin → Spinner, new material
    DOWNWARD_USED = "DOWNWARD_USED"  # Spinner → Bin, used material


class HelixStatus(Enum):
    IDLE         = "IDLE"
    HOMING       = "HOMING"
    READY        = "READY"
    MOVING       = "MOVING"
    JAM_DETECTED = "JAM_DETECTED"
    ERROR        = "ERROR"


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class HelixPosition:
    slot_index      : int
    lift_height_mm  : float
    is_loaded       : bool          = False
    material_type   : Optional[str] = None


# ─────────────────────────────────────────────
#  MAIN CONTROLLER
# ─────────────────────────────────────────────

class BTMHelix:
    """
    AID PLUS+ BTM Helix Transport Controller

    Usage:
        helix = BTMHelix(
            on_move_start=lambda frm, to: print(f"{frm} -> {to}"),
            on_move_progress=lambda pct, h: print(f"{pct}% @ {h}mm"),
        )
        helix.startup_home()
        helix.transport_new_material("PINS", session_id=session_id)
        helix.return_used_material(session_id=session_id)
    """

    def __init__(
        self,
        n_slots          : int = N_SLOTS,
        on_move_start     : Optional[Callable[[HelixPosition, HelixPosition], None]] = None,
        on_move_progress  : Optional[Callable[[float, float], None]] = None,
        on_move_complete  : Optional[Callable[[HelixPosition], None]] = None,
        on_jam            : Optional[Callable[[HelixPosition, int], None]] = None,
    ):
        self._n_slots         = n_slots
        self._status          = HelixStatus.IDLE
        self._position        = HelixPosition(slot_index=0, lift_height_mm=0.0)
        self._next_new_slot   = 0
        self._emergency       = False

        self._on_move_start    = on_move_start    or (lambda frm, to: None)
        self._on_move_progress = on_move_progress or (lambda pct, height: None)
        self._on_move_complete = on_move_complete or (lambda pos: None)
        self._on_jam           = on_jam           or (lambda pos, retry: None)

        log.info("BTMHelix ready | n_slots=%d", n_slots)

    # ── Public API ─────────────────────────────

    def startup_home(self) -> bool:
        """Moves to home position, confirms, reports to bus. Must be called
        once before any transport cycle — later cycles auto-home if needed."""
        self._status = HelixStatus.HOMING
        self._publish_event("HOMING", {"stage": "HOMING"})

        try:
            ok = hw_bridge.helix.home()
        except HardwareFault as e:
            log.error("Helix homing hardware fault: %s", e)
            self._status = HelixStatus.ERROR
            self._publish_event("ERROR", {"stage": "HOMING", "error": str(e)}, priority=Priority.HIGH)
            return False

        if not ok:
            self._status = HelixStatus.ERROR
            self._publish_event("ERROR", {"stage": "HOMING", "error": "home() returned False"},
                                priority=Priority.HIGH)
            return False

        slot, height = hw_bridge.helix.get_position()
        self._position = HelixPosition(slot_index=slot, lift_height_mm=height)
        self._status = HelixStatus.READY
        self._publish_event("READY", {"stage": "HOMING_COMPLETE", "position": self._position_dict()})
        log.info("Helix homed | position=%s", self._position)
        return True

    def transport_new_material(self, material_type: str, session_id: Optional[str] = None) -> bool:
        """Upward cycle — carries new material from Bin to Collective Spinner."""
        if not self._ensure_ready():
            return False

        target_slot = self._next_new_slot
        self._next_new_slot = (self._next_new_slot + 1) % self._n_slots
        target = HelixPosition(slot_index=target_slot, lift_height_mm=HELIX_SPINNER_HEIGHT_MM,
                                is_loaded=True, material_type=material_type)

        self._status = HelixStatus.MOVING
        self._publish_event("LOADING", {"stage": "LOADING", "cycle": HelixCycle.UPWARD_NEW.value,
                                        "material_type": material_type, "target_slot": target_slot},
                            session_id)

        self._publish_event("TRANSPORTING", {"stage": "TRANSPORTING", "cycle": HelixCycle.UPWARD_NEW.value},
                            session_id)
        if not self._execute_move(target, session_id, HelixCycle.UPWARD_NEW):
            self._finish_failed_move()
            return False

        self._publish_event("DELIVERING", {"stage": "DELIVERING", "cycle": HelixCycle.UPWARD_NEW.value,
                                           "target_slot": target_slot}, session_id)
        try:
            hw_bridge.spinner.rotate_to_slot(target_slot)
            confirmed = hw_bridge.spinner.confirm_slot()
        except HardwareFault as e:
            log.error("Spinner fault during delivery: %s", e)
            self._status = HelixStatus.ERROR
            self._publish_event("ERROR", {"stage": "DELIVERING", "error": str(e)}, session_id,
                                priority=Priority.HIGH)
            return False

        if not confirmed:
            log.error("Spinner slot confirmation failed | slot=%d", target_slot)
            self._status = HelixStatus.ERROR
            self._publish_event("ERROR", {"stage": "DELIVERING", "error": "slot_confirm_failed"},
                                session_id, priority=Priority.HIGH)
            return False

        self._position = target
        self._status = HelixStatus.READY
        self._on_move_complete(self._position)
        self._publish_event("READY", {"stage": "MATERIAL_DELIVERED", "position": self._position_dict()},
                            session_id)
        log.info("New material delivered | slot=%d | type=%s", target_slot, material_type)
        return True

    def return_used_material(self, session_id: Optional[str] = None) -> bool:
        """Downward cycle — returns used material from Spinner back to Bin."""
        if not self._ensure_ready():
            return False

        source_slot = self._position.slot_index
        target = HelixPosition(slot_index=source_slot, lift_height_mm=HELIX_BIN_HEIGHT_MM,
                                is_loaded=False, material_type=self._position.material_type)

        self._status = HelixStatus.MOVING
        self._publish_event("RETURNING", {"stage": "RETURNING", "cycle": HelixCycle.DOWNWARD_USED.value,
                                          "source_slot": source_slot}, session_id)

        if not self._execute_move(target, session_id, HelixCycle.DOWNWARD_USED):
            self._finish_failed_move()
            return False

        self._publish_event("UNLOADING", {"stage": "UNLOADING", "cycle": HelixCycle.DOWNWARD_USED.value},
                            session_id)
        self._position = target
        self._status = HelixStatus.READY
        self._on_move_complete(self._position)
        self._publish_event("READY", {"stage": "MATERIAL_RETURNED", "position": self._position_dict()},
                            session_id)
        log.info("Used material returned to bin | slot=%d", source_slot)
        return True

    def emergency_stop(self) -> None:
        """Immediate halt. No further cycles run until reset_after_emergency()."""
        self._emergency = True
        self._status = HelixStatus.ERROR
        log.critical("Helix EMERGENCY STOP triggered | position=%s", self._position)
        self._publish_event("EMERGENCY_STOP", {"position": self._position_dict()},
                            priority=Priority.CRITICAL)

    def reset_after_emergency(self) -> bool:
        """Clears the emergency flag and re-homes. Required after emergency_stop()."""
        self._emergency = False
        return self.startup_home()

    def on_jam_detected(self, target: HelixPosition, session_id: Optional[str] = None) -> bool:
        """
        Public retry-sequence entry point, matching the blueprint's explicit
        method — internally, jam handling already runs automatically inside
        every move via _move_with_jam_handling(). This method exists for
        callers that want to force a manual retry sequence toward a known
        target (e.g. after inspecting hw_bridge.helix.detect_jam() themselves).
        """
        return self._move_with_jam_handling(target.slot_index, target.lift_height_mm,
                                            session_id, cycle=None)

    def get_status(self) -> HelixStatus:
        return self._status

    def get_position(self) -> HelixPosition:
        return self._position

    # ── Internals ──────────────────────────────

    def _ensure_ready(self) -> bool:
        if self._emergency:
            log.error("Helix is in emergency stop — call reset_after_emergency() first.")
            return False
        if self._status == HelixStatus.IDLE:
            log.info("Helix not yet homed — homing before first use.")
            return self.startup_home()
        if self._status != HelixStatus.READY:
            log.warning("Helix busy or faulted (status=%s) — cannot start new cycle.", self._status.value)
            return False
        return True

    def _finish_failed_move(self) -> None:
        try:
            still_jammed = hw_bridge.helix.detect_jam()
        except Exception:
            still_jammed = True
        self._status = HelixStatus.JAM_DETECTED if still_jammed else HelixStatus.ERROR

    def _execute_move(self, target: HelixPosition, session_id: Optional[str],
                      cycle: HelixCycle) -> bool:
        """Executes a multi-step move toward target, invoking progress callbacks
        and delegating jam handling per sub-step. Returns True on success."""
        start = self._position
        self._on_move_start(start, target)

        for step in range(1, HELIX_MOVE_STEPS + 1):
            frac = step / HELIX_MOVE_STEPS
            step_height = start.lift_height_mm + (target.lift_height_mm - start.lift_height_mm) * frac
            # Slot rotation happens on arrival — lift rises along its current
            # column first, then rotates into the target slot on the final step.
            step_slot = target.slot_index if step == HELIX_MOVE_STEPS else start.slot_index

            if not self._move_with_jam_handling(step_slot, step_height, session_id, cycle):
                return False

            self._on_move_progress(round(frac * 100, 1), round(step_height, 2))

        return True

    def _move_with_jam_handling(self, slot_index: int, lift_height_mm: float,
                                session_id: Optional[str], cycle: Optional[HelixCycle]) -> bool:
        cycle_value = cycle.value if cycle else None

        for attempt in range(MAX_JAM_RETRIES + 1):
            try:
                ok = hw_bridge.helix.move_to_position(slot_index, lift_height_mm)
            except HardwareFault as e:
                log.error("Helix hardware fault mid-move: %s", e)
                self._status = HelixStatus.ERROR
                self._publish_event("ERROR", {"stage": "MOVE", "cycle": cycle_value, "error": str(e)},
                                    session_id, priority=Priority.HIGH)
                return False

            if ok:
                return True

            # Jam detected — recoverable, not an exception.
            self._status = HelixStatus.JAM_DETECTED
            self._on_jam(self._position, attempt)
            self._publish_event("JAM_DETECTED", {"cycle": cycle_value, "attempt": attempt,
                                                  "slot_index": slot_index,
                                                  "lift_height_mm": lift_height_mm},
                                session_id, priority=Priority.HIGH)
            log.warning("Helix jam detected | attempt=%d/%d | target=(slot=%d, height=%.1fmm)",
                       attempt + 1, MAX_JAM_RETRIES, slot_index, lift_height_mm)

            if attempt < MAX_JAM_RETRIES:
                time.sleep(0.3)   # brief settle before retry
                continue

            log.critical("Helix jam unresolved after %d retries — escalating to maintenance.",
                        MAX_JAM_RETRIES)
            try:
                bus.send_maintenance_alert(
                    alert_type = "HELIX_JAM_UNRESOLVED",
                    details    = {"slot_index": slot_index, "lift_height_mm": lift_height_mm,
                                 "retries": MAX_JAM_RETRIES, "cycle": cycle_value,
                                 "session_id": session_id},
                    critical   = True,
                )
            except Exception as e:
                log.error("Could not send maintenance alert to bus: %s", e)
            self._status = HelixStatus.ERROR
            return False

        return False   # unreachable, defensive

    def _position_dict(self) -> Dict:
        return {"slot_index": self._position.slot_index, "lift_height_mm": self._position.lift_height_mm,
                "is_loaded": self._position.is_loaded, "material_type": self._position.material_type}

    def _publish_event(self, stage: str, extra: Dict, session_id: Optional[str] = None,
                       priority: Priority = Priority.NORMAL) -> None:
        try:
            payload = {"component": "btm_helix", "stage": stage, "status": self._status.value,
                      "position": self._position_dict(), "timestamp": _now_iso()}
            payload.update(extra)
            bus.publish(
                message_type = MessageType.HARDWARE_EVENT,
                payload      = payload,
                priority     = priority,
                session_id   = session_id,
            )
        except Exception as e:
            log.error("Could not publish HARDWARE_EVENT to bus: %s", e)


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Helix Transport Controller — Test Suite ===\n")

    import sys
    sys.path.insert(0, ".")
    from btm_bus import bus as _bus

    _bus.activate(hw_simulation=True)
    session = _bus.open_session("AID-HELIX-TEST-0001")

    events = {"start": 0, "progress": 0, "complete": 0, "jam": 0}

    def on_start(frm, to):
        events["start"] += 1
        print(f"    [move_start]    {frm.slot_index}@{frm.lift_height_mm:.0f}mm -> "
              f"{to.slot_index}@{to.lift_height_mm:.0f}mm")

    def on_progress(pct, height):
        events["progress"] += 1
        print(f"    [progress]      {pct:5.1f}% | height={height:.1f}mm")

    def on_complete(pos):
        events["complete"] += 1
        print(f"    [move_complete] slot={pos.slot_index} height={pos.lift_height_mm:.1f}mm "
              f"loaded={pos.is_loaded} material={pos.material_type}")

    def on_jam(pos, retry):
        events["jam"] += 1
        print(f"    [JAM]           retry={retry} at slot={pos.slot_index}")

    helix = BTMHelix(on_move_start=on_start, on_move_progress=on_progress,
                     on_move_complete=on_complete, on_jam=on_jam)

    print("  [Startup]")
    print("    homed:", helix.startup_home())
    print("    status:", helix.get_status().value, "| position:", helix.get_position())

    print("\n  [Transport new material — PINS]")
    ok = helix.transport_new_material("PINS", session_id=session)
    print("    success:", ok, "| status:", helix.get_status().value)

    print("\n  [Return used material]")
    ok = helix.return_used_material(session_id=session)
    print("    success:", ok, "| status:", helix.get_status().value)

    print("\n  [Emergency stop + recovery]")
    helix.emergency_stop()
    print("    status after stop:", helix.get_status().value)
    blocked = helix.transport_new_material("TEST_PLATES", session_id=session)
    print("    transport while stopped (should be False):", blocked)
    print("    reset:", helix.reset_after_emergency())
    print("    status after reset:", helix.get_status().value)

    print("\n  [Running 25 cycles to exercise jam/fault handling under normal operation]")
    successes = 0
    recoveries = 0
    for i in range(25):
        if helix.get_status() != HelixStatus.READY:
            # This is exactly what run_btm.py's session loop will do between
            # sessions: a HardwareFault (unlike a jam) is never auto-retried
            # mid-move — it requires an explicit re-home before continuing.
            recoveries += 1
            helix.startup_home()
        result = helix.transport_new_material("PINS", session_id=session)
        if result:
            successes += 1
            helix.return_used_material(session_id=session)
    print(f"    successful transport cycles: {successes}/25 | recoveries needed: {recoveries}")
    print(f"    callback totals: start={events['start']} progress={events['progress']} "
          f"complete={events['complete']} jam_events={events['jam']}")

    _bus.close_session(session)
    _bus.deactivate()
    print("\n✓ BTM Helix Transport Controller test complete\n")
