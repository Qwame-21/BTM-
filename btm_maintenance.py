"""
btm_maintenance.py — AID PLUS+ BTM Maintenance Manager
===========================================================
Technician notification and service scheduling layer, built on top of
btm_ml's MaintenancePredictor. Where MaintenancePredictor evaluates
component wear and produces raw predictions, this module turns those
(and any other subsystem's maintenance alerts already flowing over the
bus — Helix jams, Bin thresholds, etc.) into dispatched TechnicianAlerts,
tracks resolution, and escalates anything left unresolved too long.

Integration:
    - Reads btm_ml.MaintenancePredictor for component wear predictions
    - Publishes MAINTENANCE_ALERT to btm_bus, destination="MaintenanceSystem"
    - Keeps a local service history (record_service) for audit + ML feedback

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Dict, List, Optional

from btm_bus import bus, MessageType, Priority
from btm_ml import MaintenanceUrgency as _MLUrgency
from config import UNRESOLVED_ESCALATION_MINUTES

log = logging.getLogger("btm_maintenance")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class AlertUrgency(Enum):
    LOW      = "LOW"
    NORMAL   = "NORMAL"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"


class AlertState(Enum):
    OPEN       = "OPEN"
    ESCALATED  = "ESCALATED"
    RESOLVED   = "RESOLVED"


_URGENCY_ORDER = {AlertUrgency.LOW: 0, AlertUrgency.NORMAL: 1,
                  AlertUrgency.HIGH: 2, AlertUrgency.CRITICAL: 3}

_ESCALATION_STEP = {
    AlertUrgency.LOW: AlertUrgency.NORMAL,
    AlertUrgency.NORMAL: AlertUrgency.HIGH,
    AlertUrgency.HIGH: AlertUrgency.CRITICAL,
    AlertUrgency.CRITICAL: AlertUrgency.CRITICAL,   # already at ceiling
}

# btm_ml.MaintenancePredictor.evaluate() only returns alerts for components
# that already crossed its own "warn" threshold — every one of these is
# already actionable, so unlike a fresh prediction, ROUTINE here still
# means "worth a technician alert", just the lowest-priority one.
_ML_URGENCY_MAP = {
    _MLUrgency.ROUTINE:   AlertUrgency.LOW,
    _MLUrgency.SOON:      AlertUrgency.NORMAL,
    _MLUrgency.URGENT:    AlertUrgency.HIGH,
    _MLUrgency.IMMEDIATE: AlertUrgency.CRITICAL,
}


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class TechnicianAlert:
    alert_id      : str
    component     : str
    urgency        : AlertUrgency
    message        : str
    device_id       : str
    raised_at        : str = field(default_factory=_now_iso)
    state             : AlertState = AlertState.OPEN
    resolved_at        : Optional[str] = None
    escalated_at         : Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "alert_id": self.alert_id, "component": self.component,
            "urgency": self.urgency.value, "message": self.message,
            "device_id": self.device_id, "raised_at": self.raised_at,
            "state": self.state.value, "resolved_at": self.resolved_at,
            "escalated_at": self.escalated_at,
        }


@dataclass
class ServiceRecord:
    component      : str
    action          : str
    technician_id    : str
    performed_at      : str = field(default_factory=_now_iso)
    resolution         : Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "component": self.component, "action": self.action,
            "technician_id": self.technician_id, "performed_at": self.performed_at,
            "resolution": self.resolution,
        }


# ─────────────────────────────────────────────
#  MAIN MANAGER
# ─────────────────────────────────────────────

class BTMMaintenanceManager:
    """
    AID PLUS+ BTM Maintenance Manager

    Usage:
        mgr = BTMMaintenanceManager(device_id="BTM-KIOSK-001", ml_engine=btm_ml_engine)
        mgr.evaluate_and_dispatch()             # per-session, reads MaintenancePredictor
        mgr.record_service("helix", "lubricated rail", "TECH-042")
        mgr.check_escalations()                 # periodic, run_btm.py connectivity loop
    """

    def __init__(self, device_id: str, ml_engine=None):
        self._device_id     = device_id
        self._ml_engine     = ml_engine   # btm_ml.BTMLocalMLEngine — optional, injected
        self._alerts: Dict[str, TechnicianAlert] = {}
        self._history: List[ServiceRecord] = []
        self._next_alert_seq = 1
        log.info("BTMMaintenanceManager ready | device=%s", device_id)

    # ── Public API ─────────────────────────────

    def evaluate_and_dispatch(self) -> List[TechnicianAlert]:
        """
        Reads the ML predictor's current component-wear evaluation (if a
        BTMLocalMLEngine was injected) and dispatches a TechnicianAlert for
        each MaintenanceAlert it returns. Safe to call every session —
        dispatch_alert() is idempotent per component while an alert is
        still OPEN.
        """
        ml_alerts = self._read_ml_alerts()
        dispatched = []
        for ml_alert in ml_alerts:
            urgency = _ML_URGENCY_MAP.get(ml_alert.urgency, AlertUrgency.NORMAL)
            alert = self.dispatch_alert(ml_alert.component, urgency, ml_alert.recommendation)
            dispatched.append(alert)
        return dispatched

    def dispatch_alert(self, component: str, urgency: AlertUrgency, message: str) -> TechnicianAlert:
        """Creates (or returns the existing OPEN alert for) a component issue
        and publishes it to the bus."""
        existing = self._find_open_alert(component)
        if existing:
            log.info("Alert already open for %s (id=%s) — not duplicating.", component, existing.alert_id)
            return existing

        alert = TechnicianAlert(
            alert_id  = f"ALERT-{self._device_id}-{self._next_alert_seq:04d}",
            component = component,
            urgency   = urgency,
            message   = message,
            device_id = self._device_id,
        )
        self._next_alert_seq += 1
        self._alerts[alert.alert_id] = alert

        self._publish_alert(alert)
        log.info("Dispatched alert | id=%s | component=%s | urgency=%s", alert.alert_id, component, urgency.value)
        return alert

    def record_service(self, component: str, action: str, technician_id: str,
                       resolution: Optional[str] = None) -> ServiceRecord:
        """Records a completed service action and resolves any matching OPEN alert."""
        record = ServiceRecord(component=component, action=action, technician_id=technician_id,
                               resolution=resolution)
        self._history.append(record)

        alert = self._find_open_alert(component)
        if alert:
            alert.state = AlertState.RESOLVED
            alert.resolved_at = _now_iso()
            log.info("Alert resolved | id=%s | technician=%s | action=%s",
                     alert.alert_id, technician_id, action)

        try:
            bus.publish(
                message_type = MessageType.STATUS_UPDATE,
                payload      = {"event": "SERVICE_RECORDED", "device_id": self._device_id,
                                **record.to_dict()},
                priority     = Priority.NORMAL,
            )
        except Exception as e:
            log.error("Could not report service record to bus: %s", e)

        return record

    def get_service_history(self, limit: int = 50) -> List[Dict]:
        return [r.to_dict() for r in self._history[-limit:]]

    def get_open_alerts(self) -> List[Dict]:
        return [a.to_dict() for a in self._alerts.values() if a.state != AlertState.RESOLVED]

    def escalate(self, alert_id: str) -> Optional[TechnicianAlert]:
        """Manually escalates a specific alert's urgency by one level."""
        alert = self._alerts.get(alert_id)
        if not alert or alert.state == AlertState.RESOLVED:
            return None
        return self._escalate_alert(alert)

    def check_escalations(self) -> List[TechnicianAlert]:
        """
        Escalates any OPEN alert that has sat unresolved past
        UNRESOLVED_ESCALATION_MINUTES. Intended to run periodically from
        run_btm.py's connectivity/maintenance loop.
        """
        escalated = []
        cutoff = _now() - timedelta(minutes=UNRESOLVED_ESCALATION_MINUTES)
        for alert in self._alerts.values():
            if alert.state == AlertState.RESOLVED:
                continue
            raised = datetime.fromisoformat(alert.raised_at)
            if raised <= cutoff and alert.urgency != AlertUrgency.CRITICAL:
                escalated.append(self._escalate_alert(alert))
        return escalated

    # ── Internals ──────────────────────────────

    def _read_ml_alerts(self) -> List:
        """
        Pulls maintenance alerts from the injected ML engine's
        MaintenancePredictor (btm_ml.BTMLocalMLEngine.maintenance).
        Returns [] gracefully if no engine was injected or it doesn't
        expose the expected interface — maintenance dispatch should
        never crash the session loop over an ML-layer issue.
        """
        if self._ml_engine is None:
            return []
        try:
            predictor = getattr(self._ml_engine, "maintenance", None)
            if predictor is None:
                return []
            return predictor.evaluate() or []
        except Exception as e:
            log.error("Could not read ML maintenance predictions: %s", e)
            return []

    def _find_open_alert(self, component: str) -> Optional[TechnicianAlert]:
        for alert in self._alerts.values():
            if alert.component == component and alert.state != AlertState.RESOLVED:
                return alert
        return None

    def _escalate_alert(self, alert: TechnicianAlert) -> TechnicianAlert:
        new_urgency = _ESCALATION_STEP[alert.urgency]
        log.warning("Escalating alert | id=%s | %s -> %s", alert.alert_id,
                   alert.urgency.value, new_urgency.value)
        alert.urgency = new_urgency
        alert.state = AlertState.ESCALATED
        alert.escalated_at = _now_iso()
        self._publish_alert(alert)
        return alert

    def _publish_alert(self, alert: TechnicianAlert) -> None:
        try:
            bus.send_maintenance_alert(
                alert_type = f"COMPONENT_{alert.component.upper()}",
                details    = alert.to_dict(),
                critical   = alert.urgency == AlertUrgency.CRITICAL,
            )
        except Exception as e:
            log.error("Could not publish alert to bus: %s", e)


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Maintenance Manager — Test Suite ===\n")

    import sys, time
    sys.path.insert(0, ".")
    from btm_bus import bus as _bus

    _bus.activate(hw_simulation=True)

    class _FakeMaintenancePredictor:
        """Mimics btm_ml.MaintenancePredictor.evaluate()'s real return shape."""
        def evaluate(self):
            from types import SimpleNamespace
            return [
                SimpleNamespace(component="suction_regulator", urgency=_MLUrgency.URGENT,
                               recommendation="Inspect suction tube for micro-fractures; replace if needed."),
                SimpleNamespace(component="helix_motor", urgency=_MLUrgency.IMMEDIATE,
                               recommendation="Schedule Helix motor inspection and lubrication."),
                SimpleNamespace(component="tissue_dispenser", urgency=_MLUrgency.ROUTINE,
                               recommendation="Restock wet tissue cartridge."),
            ]

    class _FakeMLEngine:
        """Mimics btm_ml.BTMLocalMLEngine's .maintenance attribute for this test."""
        def __init__(self):
            self.maintenance = _FakeMaintenancePredictor()

    mgr = BTMMaintenanceManager(device_id="BTM-KIOSK-TEST", ml_engine=_FakeMLEngine())

    print("  [evaluate_and_dispatch]")
    dispatched = mgr.evaluate_and_dispatch()
    for a in dispatched:
        print(f"    {a.alert_id} | {a.component} | {a.urgency.value} | {a.message}")

    print("\n  [Duplicate dispatch — should not create a second alert for suction_regulator]")
    again = mgr.dispatch_alert("suction_regulator", AlertUrgency.HIGH, "duplicate check")
    print(f"    same alert_id returned: {again.alert_id == dispatched[0].alert_id}")

    print("\n  [Manual escalate]")
    escalated = mgr.escalate(dispatched[0].alert_id)
    print(f"    {escalated.alert_id} escalated to {escalated.urgency.value} | state={escalated.state.value}")

    print("\n  [record_service — resolves the helix alert]")
    helix_alert = [a for a in dispatched if a.component == "helix_motor"][0]
    record = mgr.record_service("helix_motor", "Replaced bearing assembly", "TECH-014",
                                resolution="Bearing replaced, wear reset.")
    print(f"    service recorded: {record.action} by {record.technician_id}")
    print(f"    open alerts remaining: {[a['component'] for a in mgr.get_open_alerts()]}")

    print("\n  [check_escalations — artificially aging an alert]")
    # tissue_dispenser was dispatched at ROUTINE — age it past the threshold
    tissue_alert = [a for a in dispatched if a.component == "tissue_dispenser"][0]
    tissue_alert.raised_at = (datetime.now(timezone.utc) - timedelta(minutes=UNRESOLVED_ESCALATION_MINUTES + 5)).isoformat()
    escalations = mgr.check_escalations()
    print(f"    escalated {len(escalations)} stale alert(s): "
          f"{[(a.alert_id, a.urgency.value) for a in escalations]}")

    print("\n  [Service history]")
    for rec in mgr.get_service_history():
        print(f"    {rec['performed_at']} | {rec['component']} | {rec['action']} | {rec['technician_id']}")

    _bus.deactivate()
    print("\n✓ BTM Maintenance Manager test complete\n")
