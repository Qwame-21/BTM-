"""
run_btm.py — AID PLUS+ BTM Standalone Entry Point
======================================================
The full BTM device runtime. Runs entirely independently of the Aid
System kiosk — wires every module built so far into one session loop.

Startup sequence:
    1. Load config
    2. Activate btm_bus
    3. Home the Helix
    4. Check Bin inventory
    5. Run pre-session hygiene cycle (deep clean)
    6. Start vent monitor
    7. ML engine online (imported singleton)
    8. Start background connectivity + maintenance loops
    9. Ready for sessions

Session loop (run_session()):
    auth → pre-test hygiene → helix delivers new material → sample
    collection → analysis → AI interpretation → results delivery →
    helix returns used material → bin updated → post-test hygiene

Background loops:
    Connectivity — periodic btm_results.sync_pending() to flush the
                  offline buffer once connectivity returns (HOME/NETWORK)
    Maintenance  — periodic evaluate_and_dispatch() + check_escalations()

Graceful shutdown on SIGTERM/SIGINT: stops background loops, stops the
vent monitor, deactivates the bus.

Known integration gap (flagged, not silently patched):
    btm_auth.py's AIDCardProfile doesn't carry sex/age — those fields
    don't exist anywhere in the current auth contract, but
    btm_analysis.py's engine requires them. Production should resolve
    this from the real AidPlusOS user registry. Until then, this file
    keeps a small simulation-only demographic lookup for the known test
    cards in btm_auth's _SIMULATED_REGISTRY, with a default fallback for
    anything else — see _demographics_for() below.

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import Callable, Optional

import config
from btm_bus import bus
from btm_auth import BTMAuthManager, AuthMethod, AuthStatus
from btm_sample import BTMSampleCollector, DeploymentContext, DeploymentMode, Hand, Finger
from btm_analysis import BTMAnalysisEngine, Sex
from btm_ai_interpreter import BTMAIInterpreter
from btm_results import BTMResultsEngine
from btm_helix import BTMHelix
from btm_bin import BTMBin, ConsumableType
from btm_hygiene import BTMHygieneManager
from btm_vent import BTMVentController
from btm_maintenance import BTMMaintenanceManager, AlertUrgency
from btm_ml import ml_engine   # module-level singleton — see note in BTMRuntime.__init__

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [RUN_BTM] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_btm")

CONNECTIVITY_CHECK_INTERVAL_S = 60.0
MAINTENANCE_CHECK_INTERVAL_S  = 300.0

# Simulation-only demographic lookup — see module docstring. Keys match
# btm_auth._SIMULATED_REGISTRY exactly.
_SIM_DEMOGRAPHICS = {
    "AID-A1B2-C3D4-E5F6": (Sex.MALE, 38),     # Kwame Asante
    "AID-G7H8-I9J0-K1L2": (Sex.FEMALE, 29),   # Ama Boateng
    "AID-M3N4-O5P6-Q7R8": (Sex.MALE, 52),     # Kofi Mensah
}
_DEFAULT_DEMOGRAPHICS = (Sex.MALE, 35)


def _demographics_for(card_id: str):
    return _SIM_DEMOGRAPHICS.get(card_id, _DEFAULT_DEMOGRAPHICS)


# ─────────────────────────────────────────────
#  RUNTIME
# ─────────────────────────────────────────────

class BTMRuntime:
    """
    Owns the full BTM device lifecycle: startup, session loop,
    background connectivity/maintenance checks, and graceful shutdown.
    """

    def __init__(self):
        self._deployment = self._build_deployment_context()
        self._running = False
        self._shutdown_event = threading.Event()

        # Core modules
        self._auth = BTMAuthManager(hw_simulation=config.HW_SIMULATION_MODE)
        self._analysis_engine = BTMAnalysisEngine(hw_simulation=config.HW_SIMULATION_MODE)
        self._interpreter = BTMAIInterpreter()
        self._results_engine = BTMResultsEngine(self._deployment)
        self._helix = BTMHelix()
        self._bin = BTMBin()
        self._hygiene = BTMHygieneManager(device_id=self._deployment.device_id)
        self._vent = BTMVentController(device_id=self._deployment.device_id)

        # ml_engine is a singleton (btm_ml.BTMLocalMLEngine enforces this via
        # __new__) — it's already constructed with its module's own default
        # device_id the moment btm_ml.py is imported anywhere in the process.
        # Re-instantiating here with our configured device_id would silently
        # be a no-op (the singleton just returns the already-built instance),
        # so we use the shared `ml_engine` import directly rather than
        # pretending we can reconfigure it. Worth knowing if device_id ever
        # needs to vary per-device in a multi-unit deployment — that would
        # need the singleton pattern itself revisited, not a workaround here.
        self._ml_engine = ml_engine
        self._maintenance = BTMMaintenanceManager(device_id=self._deployment.device_id,
                                                  ml_engine=self._ml_engine)

        self._connectivity_thread: Optional[threading.Thread] = None
        self._maintenance_thread: Optional[threading.Thread] = None

        log.info("BTMRuntime constructed | device=%s | mode=%s | sim=%s",
                 self._deployment.device_id, self._deployment.mode.value, config.HW_SIMULATION_MODE)

    def _build_deployment_context(self) -> DeploymentContext:
        mode = (DeploymentMode(config.DEPLOYMENT_MODE)
                if config.DEPLOYMENT_MODE in DeploymentMode.__members__
                else DeploymentMode.KIOSK)
        return DeploymentContext(
            mode           = mode,
            device_id      = config.DEVICE_ID,
            wifi_available = config.WIFI_AVAILABLE_DEFAULT,
            ble_available  = config.BLE_AVAILABLE_DEFAULT,
            cloud_endpoint = config.CLOUD_ENDPOINT,
            offline_buffer = config.OFFLINE_BUFFER_ENABLED,
            hw_simulation  = config.HW_SIMULATION_MODE,
        )

    # ── Startup ────────────────────────────────

    def startup(self) -> bool:
        log.info("=== BTM Startup Sequence ===")
        log.info("[1/7] Config loaded | %s", config.summary())

        log.info("[2/7] Activating service bus...")
        if not bus.activate(hw_simulation=config.HW_SIMULATION_MODE):
            log.critical("Bus activation failed — aborting startup.")
            return False

        log.info("[3/7] Homing Helix...")
        if not self._helix.startup_home():
            log.error("Helix homing failed — device will run in degraded mode "
                     "(sessions requiring the Helix will fail until resolved).")

        log.info("[4/7] Checking Bin inventory...")
        bin_status = self._bin.check_status()
        log.info("Bin status: %s", bin_status.value)
        if bin_status.value in ("CRITICAL", "FULL_USED"):
            log.warning("Bin requires attention before sessions can safely run.")

        log.info("[5/7] Running pre-session deep clean...")
        hygiene_result = self._hygiene.run_deep_clean()
        if not hygiene_result.success:
            log.warning("Pre-session deep clean failed: %s", hygiene_result.detail)

        log.info("[6/7] Starting vent monitor...")
        self._vent.start_monitor()

        log.info("[7/7] ML engine online | device=%s", self._ml_engine.device_id)

        self._running = True
        self._start_background_loops()
        log.info("=== BTM Startup Complete — Ready for Sessions ===")
        return True

    def _start_background_loops(self) -> None:
        self._connectivity_thread = threading.Thread(
            target=self._connectivity_loop, daemon=True, name="BTM-Connectivity")
        self._connectivity_thread.start()

        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop, daemon=True, name="BTM-Maintenance")
        self._maintenance_thread.start()

    def _connectivity_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                synced = self._results_engine.sync_pending()
                if synced:
                    log.info("Connectivity loop: synced %d buffered result(s)", synced)
            except Exception as e:
                log.error("Connectivity loop error: %s", e)
            self._shutdown_event.wait(CONNECTIVITY_CHECK_INTERVAL_S)

    def _maintenance_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                self._maintenance.evaluate_and_dispatch()
                self._maintenance.check_escalations()
            except Exception as e:
                log.error("Maintenance loop error: %s", e)
            self._shutdown_event.wait(MAINTENANCE_CHECK_INTERVAL_S)

    # ── Session Loop ───────────────────────────

    def authenticate(self, credential: str, method: AuthMethod = AuthMethod.AID_CARD):
        """Runs auth only. Returns the AuthResult regardless of outcome —
        callers (btm_ui.py) branch on .status themselves rather than this
        method swallowing a rejection into a bare bool."""
        return self._auth.authenticate(credential, method)

    def complete_session(
        self,
        auth_result,
        hand: Hand = Hand.RIGHT,
        finger: Finger = Finger.INDEX,
        on_collection_status: Optional["Callable"] = None,
        on_phase: Optional["Callable[[str], None]"] = None,
        on_results: Optional["Callable[[dict], None]"] = None,
    ) -> bool:
        """
        Runs everything after a successful authenticate() call: pre-test
        hygiene → helix delivers new material → sample collection →
        analysis → AI interpretation → results delivery → helix returns
        used material → bin update → post-test hygiene.

        on_collection_status: forwarded to BTMSampleCollector — fine-grained
            phase updates during physical collection (CollectionStatus enum).
        on_phase: coarse milestone callback fired at "COLLECTING",
            "PROCESSING", and "RESULTS_READY" — what a UI needs to switch
            screens without caring about collection sub-phases.
        on_results: fired with the delivered ResultPackage's kiosk_view
            dict (headline, urgency, chi_score/grade, follow_up_actions,
            etc.) right when results are ready — the actual patient-facing
            summary, not just a "done" signal.

        Returns True if results were handed to btm_results (delivered,
        buffered, or locally saved all count as success — only a hard
        failure before that point returns False).
        """
        if auth_result.status != AuthStatus.APPROVED:
            log.warning("complete_session called with non-approved auth | status=%s",
                       auth_result.status.value)
            return False

        session_id = auth_result.session_id
        card_id    = auth_result.card_id
        _phase = on_phase or (lambda p: None)

        try:
            self._hygiene.run_pre_test_cycle(session_id=session_id)

            material_delivered = self._helix.transport_new_material("PINS", session_id=session_id)
            if not material_delivered:
                log.error("Session aborted — Helix failed to deliver new material "
                         "(status=%s). Cannot proceed without hardware readiness.",
                         self._helix.get_status().value)
                self._hygiene.run_emergency_clean(session_id=session_id)
                self._maintenance.dispatch_alert(
                    "helix", AlertUrgency.HIGH,
                    f"Helix failed to deliver material for session {session_id} — "
                    f"status={self._helix.get_status().value}.",
                )
                return False

            self._bin.dispense(ConsumableType.PINS)

            _phase("COLLECTING")
            collector = BTMSampleCollector(self._deployment, on_status=on_collection_status)
            collection = collector.collect(session_id, card_id, hand, finger)

            if not collection.ready_for_analysis:
                log.warning("Session aborted at collection | reason=%s",
                           collection.abort_reason.value if collection.abort_reason else "unknown")
                self._hygiene.run_emergency_clean(session_id=session_id)
                return False

            _phase("PROCESSING")
            sex, age = _demographics_for(card_id)
            report = self._analysis_engine.analyse(collection, sex=sex, age=age)

            narrative = self._interpreter.interpret(report)

            package = self._results_engine.deliver(report, narrative)
            log.info("Results delivered | status=%s | destination=%s",
                     package.delivery_status.value, package.delivery_destination)
            _phase("RESULTS_READY")
            if on_results:
                on_results(package.kiosk_view)

            material_returned = self._helix.return_used_material(session_id=session_id)
            if material_returned:
                self._bin.receive_used(ConsumableType.PINS)
            else:
                log.error("Helix failed to return used material | status=%s",
                         self._helix.get_status().value)
                self._maintenance.dispatch_alert(
                    "helix", AlertUrgency.NORMAL,
                    f"Helix failed to return used material for session {session_id} — "
                    f"manual retrieval may be required.",
                )

            self._hygiene.run_post_test_cycle(session_id=session_id)

            log.info("── Session complete ──")
            return True

        except Exception as e:
            log.exception("Unhandled session error")
            try:
                self._hygiene.run_emergency_clean(session_id=session_id)
            except Exception:
                pass
            return False

        finally:
            self._auth.end_session(session_id)

    def run_session(self, credential: str, method: AuthMethod = AuthMethod.AID_CARD,
                    hand: Hand = Hand.RIGHT, finger: Finger = Finger.INDEX) -> bool:
        """
        Convenience wrapper — authenticate() + complete_session() in one
        call. Used by the bounded smoke test and any caller that doesn't
        need to show a distinct "card approved" screen before collection
        starts. btm_ui.py calls authenticate()/complete_session()
        separately instead, for the real touchscreen flow.
        """
        log.info("── New session starting ──")
        auth_result = self.authenticate(credential, method)
        if auth_result.status != AuthStatus.APPROVED:
            log.warning("Session rejected at auth | status=%s | reason=%s",
                       auth_result.status.value, auth_result.failure_reason)
            return False
        return self.complete_session(auth_result, hand, finger)

    # ── Shutdown ───────────────────────────────

    def shutdown(self) -> None:
        log.info("=== BTM Shutdown Sequence ===")
        self._running = False
        self._shutdown_event.set()

        self._vent.stop_monitor()

        if self._connectivity_thread:
            self._connectivity_thread.join(timeout=5)
        if self._maintenance_thread:
            self._maintenance_thread.join(timeout=5)

        bus.deactivate()
        log.info("=== BTM Shutdown Complete ===")


# ─────────────────────────────────────────────
#  PRODUCTION ENTRY POINT (signal handling + session loop)
# ─────────────────────────────────────────────

_runtime: Optional[BTMRuntime] = None


def _handle_signal(signum, frame) -> None:
    log.info("Received signal %s — initiating graceful shutdown.", signum)
    if _runtime:
        _runtime.shutdown()
    sys.exit(0)


def main() -> None:
    """
    Real deployment entry point. Starts the runtime, installs signal
    handlers for graceful shutdown, and blocks waiting for touchscreen-
    driven session events (btm_ui.py, not yet built, will call
    runtime.run_session() per user interaction — until then this loop
    just keeps the process alive with all background loops running).
    """
    global _runtime
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    _runtime = BTMRuntime()
    if not _runtime.startup():
        log.critical("Startup failed — exiting.")
        sys.exit(1)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _runtime.shutdown()


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Runtime — Test Suite ===\n")
    print("(Exercises BTMRuntime directly — main()'s blocking session-event "
          "loop is for real deployment, not this bounded test.)\n")

    runtime = BTMRuntime()

    print("  [Startup]")
    ok = runtime.startup()
    print(f"    startup success: {ok}\n")

    print("  [Session 1 — valid PREMIUM card]")
    result = runtime.run_session("AID-A1B2-C3D4-E5F6")
    print(f"    session success: {result}\n")

    print("  [Session 2 — valid STANDARD card, different hand/finger]")
    result = runtime.run_session("AID-G7H8-I9J0-K1L2", hand=Hand.LEFT, finger=Finger.MIDDLE)
    print(f"    session success: {result}\n")

    print("  [Session 3 — expired card, should be rejected at auth]")
    result = runtime.run_session("AID-M3N4-O5P6-Q7R8")
    print(f"    session success (expect False): {result}\n")

    print("  [Session 4 — unknown card, should be rejected at auth]")
    result = runtime.run_session("AID-ZZZZ-ZZZZ-ZZZZ")
    print(f"    session success (expect False): {result}\n")

    print("  [Bin inventory after 2 successful sessions]")
    print("   ", runtime._bin.get_inventory_report()["PINS"])

    print("\n  [Helix status]")
    print(f"    status={runtime._helix.get_status().value} | position={runtime._helix.get_position()}")

    print("\n  [Maintenance — manual evaluate_and_dispatch]")
    alerts = runtime._maintenance.evaluate_and_dispatch()
    print(f"    dispatched {len(alerts)} alert(s): {[a.component for a in alerts]}")

    print("\n  [Shutdown]")
    runtime.shutdown()

    print("\n✓ BTM Runtime test complete\n")
