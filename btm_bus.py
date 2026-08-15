"""
btm_bus.py — AID PLUS+ BTM Service Bus
=======================================
Activates BTM-v1 on ADW_VARIANT_BT and manages all inter-system
communication between the BTM, AidPlusOS, and the Aid Plus Infobox.

Service Bus Contract : BTM-v1
ADW Variant          : ADW_VARIANT_BT
Layer                : AidPlusOS → BTM Intelligence → BTM App
Author               : Aid Plus Engineering
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Any

from shared.adw_contracts import ADW_CONTRACTS


# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [BTM-BUS] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("btm_bus")


# ─────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────

# Sourced from shared.adw_contracts — the registry is now the single
# source of truth for the contract ID and ADW variant; these constants
# stay so every existing call site in this file (and downstream
# modules importing them) keeps working unchanged.
_BTM_CONTRACT       = ADW_CONTRACTS["BTM-v1"]
BTM_CONTRACT_ID     = _BTM_CONTRACT["contract_id"]
ADW_VARIANT         = _BTM_CONTRACT["adw_variant"]
BUS_VERSION         = "1.0.0"
HEARTBEAT_INTERVAL  = 30          # seconds
MAX_RETRY_ATTEMPTS  = 5
RETRY_BACKOFF_BASE  = 2           # seconds, exponential backoff


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class BTMStatus(Enum):
    DORMANT      = "DORMANT"
    REGISTERING  = "REGISTERING"
    ACTIVE       = "ACTIVE"
    SUSPENDED    = "SUSPENDED"
    MAINTENANCE  = "MAINTENANCE"
    ERROR        = "ERROR"


class MessageType(Enum):
    REGISTRATION        = "REGISTRATION"
    HEARTBEAT           = "HEARTBEAT"
    AUTH_REQUEST        = "AUTH_REQUEST"
    AUTH_RESPONSE       = "AUTH_RESPONSE"
    SAMPLE_EVENT        = "SAMPLE_EVENT"
    ANALYSIS_RESULT     = "ANALYSIS_RESULT"
    INFOBOX_DELIVERY    = "INFOBOX_DELIVERY"
    MAINTENANCE_ALERT   = "MAINTENANCE_ALERT"
    STATUS_UPDATE       = "STATUS_UPDATE"
    HARDWARE_EVENT      = "HARDWARE_EVENT"
    ERROR_REPORT        = "ERROR_REPORT"
    SYSTEM_COMMAND      = "SYSTEM_COMMAND"


class Priority(Enum):
    LOW      = 0
    NORMAL   = 1
    HIGH     = 2
    CRITICAL = 3


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class BTMMessage:
    """
    Universal message envelope for all BTM service bus communication.
    Every module communicates through this structure.
    """
    message_id   : str           = field(default_factory=lambda: str(uuid.uuid4()))
    contract_id  : str           = BTM_CONTRACT_ID
    adw_variant  : str           = ADW_VARIANT
    message_type : str           = ""
    priority     : int           = Priority.NORMAL.value
    timestamp    : str           = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source       : str           = ""
    destination  : str           = ""
    session_id   : Optional[str] = None
    user_card_id : Optional[str] = None
    payload      : Dict          = field(default_factory=dict)
    checksum     : Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "BTMMessage":
        return cls(**json.loads(raw))


@dataclass
class BTMRegistration:
    """
    Contract registration record for BTM-v1 on ADW_VARIANT_BT.
    Sent to AidPlusOS on activation.
    """
    contract_id     : str = BTM_CONTRACT_ID
    adw_variant     : str = ADW_VARIANT
    bus_version     : str = BUS_VERSION
    registered_at   : str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    capabilities    : List[str] = field(default_factory=lambda: [
        "CBC_ANALYSIS",
        "CHOLESTEROL_ANALYSIS",
        "BLOOD_SUGAR_ANALYSIS",
        "LIVER_FUNCTION",
        "KIDNEY_FUNCTION",
        "DISEASE_MARKERS",
        "AID_CARD_AUTH",
        "INFOBOX_DELIVERY",
        "MAINTENANCE_ALERTS",
        "SIMULATION_MODE",
    ])
    hw_simulation   : bool = True
    status          : str  = BTMStatus.REGISTERING.value


# ─────────────────────────────────────────────
#  CHANNEL
# ─────────────────────────────────────────────

class BTMChannel:
    """
    In-process message channel simulating the ADW_VARIANT_BT service bus.
    In production this maps to the physical ADW communication layer.
    In simulation mode (HW_SIMULATION_MODE=True) it runs entirely in-process.
    """

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._message_log: List[BTMMessage] = []
        self._lock = threading.Lock()

    def subscribe(self, message_type: str, handler: Callable[[BTMMessage], None]) -> None:
        with self._lock:
            self._subscribers.setdefault(message_type, []).append(handler)
        log.info(f"Subscriber registered for [{message_type}]")

    def publish(self, message: BTMMessage) -> None:
        with self._lock:
            self._message_log.append(message)
            handlers = self._subscribers.get(message.message_type, []) + \
                       self._subscribers.get("*", [])

        if not handlers:
            log.warning(f"No subscribers for message type [{message.message_type}]")
            return

        for handler in handlers:
            try:
                handler(message)
            except Exception as e:
                log.error(f"Handler error on [{message.message_type}]: {e}")

    def get_log(self, limit: int = 50) -> List[BTMMessage]:
        with self._lock:
            return self._message_log[-limit:]

    def clear_log(self) -> None:
        with self._lock:
            self._message_log.clear()


# ─────────────────────────────────────────────
#  CORE BUS
# ─────────────────────────────────────────────

class BTMServiceBus:
    """
    AID PLUS+ BTM Service Bus — BTM-v1 / ADW_VARIANT_BT

    Central communication layer between:
    - AidPlusOS  ↔  BTM Intelligence Layer
    - BTM modules ↔  Aid Plus Infobox
    - BTM        ↔  Maintenance / Technician system

    Usage:
        bus = BTMServiceBus()
        bus.activate()
        bus.subscribe(MessageType.ANALYSIS_RESULT, my_handler)
        bus.publish(MessageType.SAMPLE_EVENT, payload={...})
    """

    _instance: Optional["BTMServiceBus"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "BTMServiceBus":
        """Singleton — one bus per process."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized   = True
        self._status        = BTMStatus.DORMANT
        self._channel       = BTMChannel()
        self._registration  : Optional[BTMRegistration] = None
        self._session_map   : Dict[str, Dict] = {}
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._active        = False
        self._hw_simulation = True
        log.info(f"BTMServiceBus instantiated | Contract: {BTM_CONTRACT_ID} | Variant: {ADW_VARIANT}")

    # ── Activation ────────────────────────────

    def activate(self, hw_simulation: bool = True) -> bool:
        """
        Activate BTM-v1 on ADW_VARIANT_BT.
        Registers the contract with AidPlusOS and starts the heartbeat.
        """
        if self._status == BTMStatus.ACTIVE:
            log.warning("Bus already active.")
            return True

        self._hw_simulation = hw_simulation
        self._status = BTMStatus.REGISTERING
        log.info(f"Activating BTM-v1 | HW_SIMULATION={hw_simulation}")

        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            try:
                self._registration = BTMRegistration(hw_simulation=hw_simulation)
                self._registration.status = BTMStatus.ACTIVE.value

                reg_message = BTMMessage(
                    message_type = MessageType.REGISTRATION.value,
                    priority     = Priority.HIGH.value,
                    source       = BTM_CONTRACT_ID,
                    destination  = "AidPlusOS",
                    payload      = asdict(self._registration),
                )
                self._channel.publish(reg_message)
                self._status = BTMStatus.ACTIVE
                self._active = True
                self._start_heartbeat()
                log.info(f"✓ BTM-v1 ACTIVE on {ADW_VARIANT} | Bus version {BUS_VERSION}")
                return True

            except Exception as e:
                wait = RETRY_BACKOFF_BASE ** attempt
                log.error(f"Activation attempt {attempt} failed: {e}. Retrying in {wait}s...")
                time.sleep(wait)

        self._status = BTMStatus.ERROR
        log.critical("BTM-v1 activation failed after max retries.")
        return False

    def deactivate(self) -> None:
        """Gracefully deactivate the bus."""
        self._active = False
        self._status = BTMStatus.DORMANT
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)
        log.info("BTM-v1 deactivated.")

    # ── Heartbeat ─────────────────────────────

    def _start_heartbeat(self) -> None:
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="BTM-Heartbeat",
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while self._active:
            try:
                hb = BTMMessage(
                    message_type = MessageType.HEARTBEAT.value,
                    priority     = Priority.LOW.value,
                    source       = BTM_CONTRACT_ID,
                    destination  = "AidPlusOS",
                    payload      = {
                        "status"        : self._status.value,
                        "active_sessions": len(self._session_map),
                        "hw_simulation" : self._hw_simulation,
                        "uptime_ts"     : datetime.now(timezone.utc).isoformat(),
                    },
                )
                self._channel.publish(hb)
            except Exception as e:
                log.error(f"Heartbeat error: {e}")
            time.sleep(HEARTBEAT_INTERVAL)

    # ── Session Management ────────────────────

    def open_session(self, user_card_id: str) -> str:
        """Open a new BTM test session for a validated user."""
        session_id = str(uuid.uuid4())
        self._session_map[session_id] = {
            "user_card_id" : user_card_id,
            "opened_at"    : datetime.now(timezone.utc).isoformat(),
            "stage"        : "AUTH_COMPLETE",
            "events"       : [],
        }
        log.info(f"Session opened | session_id={session_id} | user={user_card_id}")
        return session_id

    def update_session(self, session_id: str, stage: str, data: Dict = None) -> None:
        if session_id not in self._session_map:
            log.warning(f"Unknown session: {session_id}")
            return
        self._session_map[session_id]["stage"] = stage
        if data:
            self._session_map[session_id]["events"].append({
                "stage": stage,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": data,
            })

    def close_session(self, session_id: str) -> Optional[Dict]:
        session = self._session_map.pop(session_id, None)
        if session:
            log.info(f"Session closed | session_id={session_id}")
        return session

    def get_session(self, session_id: str) -> Optional[Dict]:
        return self._session_map.get(session_id)

    # ── Publish / Subscribe ───────────────────

    def publish(
        self,
        message_type : MessageType,
        payload      : Dict,
        destination  : str = "AidPlusOS",
        priority     : Priority = Priority.NORMAL,
        session_id   : Optional[str] = None,
        user_card_id : Optional[str] = None,
    ) -> BTMMessage:
        """Publish a message onto the BTM service bus."""
        if not self._active:
            raise RuntimeError("BTMServiceBus is not active. Call activate() first.")

        msg = BTMMessage(
            message_type = message_type.value,
            priority     = priority.value,
            source       = BTM_CONTRACT_ID,
            destination  = destination,
            session_id   = session_id,
            user_card_id = user_card_id,
            payload      = payload,
        )
        self._channel.publish(msg)
        return msg

    def subscribe(self, message_type: MessageType, handler: Callable[[BTMMessage], None]) -> None:
        """Subscribe to a message type on the BTM service bus."""
        self._channel.subscribe(message_type.value, handler)

    def subscribe_all(self, handler: Callable[[BTMMessage], None]) -> None:
        """Subscribe to all messages on the bus (useful for logging/monitoring)."""
        self._channel.subscribe("*", handler)

    # ── Infobox Delivery ─────────────────────

    def deliver_to_infobox(
        self,
        user_card_id : str,
        result_data  : Dict,
        session_id   : Optional[str] = None,
    ) -> BTMMessage:
        """
        Deliver blood test results to the user's Aid Plus Infobox.
        Accessible from the kiosk, future mobile app, and web.
        """
        log.info(f"Delivering results to Infobox | user={user_card_id}")
        return self.publish(
            message_type = MessageType.INFOBOX_DELIVERY,
            payload      = {
                "user_card_id"  : user_card_id,
                "result_type"   : "BLOOD_TEST",
                "contract"      : BTM_CONTRACT_ID,
                "results"       : result_data,
                "delivered_at"  : datetime.now(timezone.utc).isoformat(),
                "accessible_via": ["kiosk", "mobile_app", "aid_system"],
            },
            destination  = "AidPlusInfobox",
            priority     = Priority.HIGH,
            session_id   = session_id,
            user_card_id = user_card_id,
        )

    # ── Maintenance Alerts ────────────────────

    def send_maintenance_alert(self, alert_type: str, details: Dict, critical: bool = False) -> BTMMessage:
        """Send a maintenance or restocking alert to the technician system."""
        priority = Priority.CRITICAL if critical else Priority.HIGH
        log.warning(f"Maintenance alert [{alert_type}] | critical={critical}")
        return self.publish(
            message_type = MessageType.MAINTENANCE_ALERT,
            payload      = {
                "alert_type" : alert_type,
                "details"    : details,
                "raised_at"  : datetime.now(timezone.utc).isoformat(),
            },
            destination  = "MaintenanceSystem",
            priority     = priority,
        )

    # ── Status ────────────────────────────────

    @property
    def status(self) -> BTMStatus:
        return self._status

    @property
    def is_active(self) -> bool:
        return self._active

    def set_status(self, status: BTMStatus) -> None:
        self._status = status
        self.publish(
            message_type = MessageType.STATUS_UPDATE,
            payload      = {"status": status.value},
        )

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "contract_id"     : BTM_CONTRACT_ID,
            "adw_variant"     : ADW_VARIANT,
            "bus_version"     : BUS_VERSION,
            "status"          : self._status.value,
            "hw_simulation"   : self._hw_simulation,
            "active_sessions" : len(self._session_map),
            "message_log_size": len(self._channel.get_log()),
            "heartbeat_alive" : self._heartbeat_thread.is_alive() if self._heartbeat_thread else False,
            "registration"    : asdict(self._registration) if self._registration else None,
        }

    def __repr__(self) -> str:
        return f"<BTMServiceBus contract={BTM_CONTRACT_ID} variant={ADW_VARIANT} status={self._status.value}>"


# ─────────────────────────────────────────────
#  GLOBAL BUS INSTANCE
# ─────────────────────────────────────────────

bus = BTMServiceBus()


# ─────────────────────────────────────────────
#  QUICK SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Service Bus — Activation Test ===\n")

    received_messages = []

    def monitor(msg: BTMMessage):
        received_messages.append(msg)
        print(f"  [BUS EVENT] {msg.message_type} | priority={msg.priority} | dest={msg.destination}")

    bus.subscribe_all(monitor)
    success = bus.activate(hw_simulation=True)

    if success:
        print(f"\n  Status     : {bus.status.value}")
        print(f"  Active     : {bus.is_active}")
        print(f"\n  Opening test session...")
        session = bus.open_session("AID-CARD-0001")
        print(f"  Session ID : {session}")

        bus.update_session(session, "SAMPLE_COLLECTING", {"finger": "right_index"})

        print(f"\n  Delivering mock result to Infobox...")
        bus.deliver_to_infobox(
            user_card_id = "AID-CARD-0001",
            result_data  = {
                "CBC": {"RBC": 4.8, "WBC": 6.2, "hemoglobin": 14.5, "platelets": 250},
                "glucose": 95,
                "cholesterol": {"total": 180, "LDL": 110, "HDL": 55},
            },
            session_id = session,
        )

        bus.close_session(session)

        print(f"\n  Diagnostics:")
        diag = bus.diagnostics()
        for k, v in diag.items():
            if k != "registration":
                print(f"    {k:<22}: {v}")

        print(f"\n  Total bus events captured : {len(received_messages)}")
        print(f"\n✓ BTM-v1 bus operational on {ADW_VARIANT}\n")

    bus.deactivate()
