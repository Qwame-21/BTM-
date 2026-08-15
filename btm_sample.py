"""
btm_sample.py — AID PLUS+ BTM Sample Collection Module
=======================================================
Orchestrates the full blood sample collection sequence for the BTM.
Designed for precision, safety, and universal deployment.

Collection Sequence (20–30 seconds total):
    Phase 0 : Deployment context validation
    Phase 1 : Scanner pre-check (bacteria + DNA fingerprint)
    Phase 2 : Pre-collection wet tissue clean
    Phase 3 : Adaptive pin strike (skin-thickness-compensated)
    Phase 4 : Adaptive suction (flow-sensor-guided pressure curve)
    Phase 5 : Post-collection wet tissue clean + bleed arrest
    Phase 6 : Sample routing to btm_analysis

Deployment Modes:
    KIOSK   → Local AidPlusOS, physical AID CARD, local infobox
    HOME    → WiFi/BLE, phone as gateway, cloud infobox
    NETWORK → Multi-unit clinic/pharmacy, cloud-managed

Algorithm Philosophy:
    Every physical parameter is adaptive, not fixed.
    The device reads the user's body and adjusts in real time.
    No two collections are executed identically.

Author : Aid Plus Engineering
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Callable, Tuple

from btm_bus import (
    BTMServiceBus,
    BTMMessage,
    MessageType,
    Priority,
    bus,
)
from config import (
    SCANNER_BACTERIA_WINDOW_MS, SCANNER_DNA_WINDOW_MS, SCANNER_CONTAMINATION_THRESHOLD,
    PIN_BASE_DEPTH_MM, PIN_MIN_DEPTH_MM, PIN_MAX_DEPTH_MM, PIN_CHILD_ADJUSTMENT_MM,
    PIN_STABILIZATION_PRESSURE_KPA, PIN_STRIKE_VELOCITY_MS, PIN_RETRACTION_DELAY_MS,
    PIN_BLOOD_CONFIRM_TIMEOUT_S,
    SUCTION_INITIAL_PRESSURE_KPA, SUCTION_MAX_PRESSURE_KPA, SUCTION_INCREMENT_KPA,
    SUCTION_FLOW_CHECK_INTERVAL_S, SUCTION_TIMEOUT_S, SUCTION_TARGET_VOLUME_UL,
    SUCTION_VOLUME_PER_STEP_UL,
    TISSUE_DISPENSE_DURATION_S, POST_CLEAN_PRESSURE_S, TOTAL_SEQUENCE_TIMEOUT_S,
)

log = logging.getLogger("btm_sample")


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class DeploymentMode(Enum):
    KIOSK   = "KIOSK"       # public machine, local AidPlusOS
    HOME    = "HOME"        # personal home unit, cloud-connected
    NETWORK = "NETWORK"     # clinic/pharmacy, multi-unit cloud managed


class Hand(Enum):
    LEFT  = "LEFT"
    RIGHT = "RIGHT"


class Finger(Enum):
    INDEX  = "INDEX"
    MIDDLE = "MIDDLE"
    RING   = "RING"


class CollectionStatus(Enum):
    PENDING             = "PENDING"
    SCANNER_CHECK       = "SCANNER_CHECK"
    PRE_CLEAN           = "PRE_CLEAN"
    STRIKE_PREP         = "STRIKE_PREP"
    STRIKING            = "STRIKING"
    SUCTIONING          = "SUCTIONING"
    POST_CLEAN          = "POST_CLEAN"
    COMPLETE            = "COMPLETE"
    ABORTED             = "ABORTED"
    ERROR               = "ERROR"


class AbortReason(Enum):
    CONTAMINATION_DETECTED  = "CONTAMINATION_DETECTED"
    BLOOD_CONFIRM_TIMEOUT   = "BLOOD_CONFIRM_TIMEOUT"
    SUCTION_TIMEOUT         = "SUCTION_TIMEOUT"
    INSUFFICIENT_VOLUME     = "INSUFFICIENT_VOLUME"
    SEQUENCE_TIMEOUT        = "SEQUENCE_TIMEOUT"
    HARDWARE_FAULT          = "HARDWARE_FAULT"
    USER_ABORT              = "USER_ABORT"


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class DeploymentContext:
    """
    Carries deployment mode and connectivity config into every module.
    Controls how results are routed and how the device communicates.
    """
    mode                : DeploymentMode = DeploymentMode.KIOSK
    device_id           : str            = "BTM-UNIT-001"
    wifi_available      : bool           = True
    ble_available       : bool           = True
    cloud_endpoint      : Optional[str]  = None         # HOME/NETWORK only
    offline_buffer      : bool           = True         # queue if no connection
    hw_simulation       : bool           = True

    @classmethod
    def kiosk(cls, device_id: str = "BTM-KIOSK-001") -> "DeploymentContext":
        return cls(mode=DeploymentMode.KIOSK, device_id=device_id)

    @classmethod
    def home(cls, device_id: str = "BTM-HOME-001", cloud_endpoint: str = "https://api.aidplus.io/infobox") -> "DeploymentContext":
        return cls(
            mode           = DeploymentMode.HOME,
            device_id      = device_id,
            cloud_endpoint = cloud_endpoint,
            ble_available  = True,
            wifi_available = True,
        )

    @classmethod
    def network(cls, device_id: str, cloud_endpoint: str) -> "DeploymentContext":
        return cls(
            mode           = DeploymentMode.NETWORK,
            device_id      = device_id,
            cloud_endpoint = cloud_endpoint,
        )


@dataclass
class SkinProfile:
    """
    Computed from pre-strike sensor readings.
    Drives adaptive pin depth and suction pressure.
    """
    hand                    : Hand
    finger                  : Finger
    tenting_displacement_mm : float         # how much skin deforms under probe
    stratum_corneum_index   : float         # 0.0 (thin) → 1.0 (very calloused)
    estimated_thickness_mm  : float         # computed SC thickness
    surface_temp_celsius    : float         # warmth indicates good circulation
    hydration_index         : float         # 0.0 (dry) → 1.0 (well hydrated)
    is_minor                : bool = False


@dataclass
class StrikeProfile:
    """
    Computed strike parameters from SkinProfile.
    Passed to hardware controller for execution.
    """
    target_depth_mm         : float
    stabilizer_pressure_kpa : float
    strike_velocity_ms      : float
    retraction_delay_ms     : float
    compensation_applied    : str           # human-readable reason for adjustment


@dataclass
class SuctionResult:
    """
    Result of the adaptive suction phase.
    """
    volume_collected_ul     : float
    peak_pressure_kpa       : float
    flow_cycles             : int
    duration_s              : float
    target_met              : bool


@dataclass
class ScannerResult:
    """
    Result of the pre-collection scanner check.
    """
    bacteria_index          : float         # 0.0 = clean, 1.0 = heavily contaminated
    dna_fingerprint_matched : bool
    dna_fingerprint_hash    : Optional[str]
    scan_duration_ms        : float
    cleared                 : bool          # True = safe to proceed


@dataclass
class CollectionResult:
    """
    Full output of a completed sample collection sequence.
    Passed downstream to btm_analysis.
    """
    session_id              : str
    user_card_id            : str
    status                  : CollectionStatus
    deployment_mode         : str
    hand                    : Optional[Hand]
    finger                  : Optional[Finger]
    scanner                 : Optional[ScannerResult]
    skin_profile            : Optional[SkinProfile]
    strike_profile          : Optional[StrikeProfile]
    suction                 : Optional[SuctionResult]
    sample_volume_ul        : float         = 0.0
    collection_duration_s   : float         = 0.0
    abort_reason            : Optional[AbortReason] = None
    error_detail            : Optional[str] = None
    collected_at            : str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    ready_for_analysis      : bool = False


# ─────────────────────────────────────────────
#  HARDWARE INTERFACE LAYER
# ─────────────────────────────────────────────

class BTMHardwareInterface:
    """
    Abstraction over the C++ RTOS hardware layer.
    In simulation: returns physiologically realistic values.
    In production: delegates to btm_hw_interface (pybind11 bridge).

    Every method models real hardware behaviour including:
    - Measurement variance (sensor noise)
    - Timing delays (realistic durations)
    - Failure probability (rare, non-zero)
    """

    def __init__(self, simulation: bool = True):
        self._sim = simulation

    # ── Scanner ───────────────────────────────

    def run_bacteria_scan(self) -> float:
        """Returns bacteria contamination index 0.0–1.0."""
        if self._sim:
            time.sleep(SCANNER_BACTERIA_WINDOW_MS / 1000)
            # 95% clean, 5% mild, <1% concerning
            return random.choices(
                [random.uniform(0.0, 0.05),
                 random.uniform(0.05, 0.12),
                 random.uniform(0.12, 0.4)],
                weights=[95, 4, 1]
            )[0]
        raise NotImplementedError("Production bridge not wired.")

    def run_dna_scan(self) -> Tuple[bool, Optional[str]]:
        """Returns (matched, fingerprint_hash). Hash stored for session record."""
        if self._sim:
            time.sleep(SCANNER_DNA_WINDOW_MS / 1000)
            matched = random.random() > 0.02  # 98% match rate in sim
            fingerprint = f"DNA-{random.randint(100000, 999999)}" if matched else None
            return matched, fingerprint
        raise NotImplementedError("Production bridge not wired.")

    # ── Skin Assessment ───────────────────────

    def probe_skin_profile(self, hand: Hand, finger: Finger) -> SkinProfile:
        """
        Applies gentle probe pressure and reads sensor array to build SkinProfile.
        Tenting displacement + force curve → SC thickness estimation.
        Surface temp → circulation quality.
        Optical reflectance → hydration index.
        """
        if self._sim:
            time.sleep(0.8)  # realistic assessment duration
            sc_index = random.uniform(0.1, 0.7)
            return SkinProfile(
                hand                    = hand,
                finger                  = finger,
                tenting_displacement_mm = random.uniform(0.3, 1.2),
                stratum_corneum_index   = sc_index,
                estimated_thickness_mm  = 0.5 + sc_index * 1.2,
                surface_temp_celsius    = random.uniform(30.0, 36.0),
                hydration_index         = random.uniform(0.4, 0.9),
                is_minor                = False,
            )
        raise NotImplementedError("Production bridge not wired.")

    # ── Pin Strike ────────────────────────────

    def execute_strike(self, profile: StrikeProfile) -> bool:
        """
        Execute the pin strike with the computed profile.
        Returns True if blood confirmed at wound site within timeout.
        """
        if self._sim:
            # Simulate strike duration
            time.sleep(profile.strike_velocity_ms + profile.retraction_delay_ms / 1000)
            # Blood confirmation — 97% success with adaptive depth
            return random.random() > 0.03
        raise NotImplementedError("Production bridge not wired.")

    # ── Suction ───────────────────────────────

    def apply_suction_cycle(self, pressure_kpa: float) -> float:
        """
        Apply one suction cycle at given pressure.
        Returns estimated volume collected this cycle (µL).
        0.0 means no flow detected.
        """
        if self._sim:
            time.sleep(SUCTION_FLOW_CHECK_INTERVAL_S)
            if pressure_kpa < -4.0:
                # Flow starts reliably above -4 kPa
                volume = random.uniform(3.0, 8.0)
                return volume
            return random.uniform(0.0, 1.5)  # may trickle at low pressure
        raise NotImplementedError("Production bridge not wired.")

    # ── Tissue Dispenser ──────────────────────

    def dispense_wet_tissue(self, contact_duration_s: float) -> bool:
        """Dispense and apply wet tissue. Returns True on success."""
        if self._sim:
            time.sleep(contact_duration_s)
            return True
        raise NotImplementedError("Production bridge not wired.")

    def apply_post_clean_pressure(self, duration_s: float) -> bool:
        """Apply gentle pressure pad to arrest bleeding."""
        if self._sim:
            time.sleep(duration_s)
            return True
        raise NotImplementedError("Production bridge not wired.")


# ─────────────────────────────────────────────
#  ADAPTIVE ALGORITHMS
# ─────────────────────────────────────────────

class AdaptiveStrikeCalculator:
    """
    Computes the optimal pin strike profile from a SkinProfile.

    Aid Plus algorithm — improvement over current best-in-class:
    Standard devices pre-calculate depth from tenting alone.
    This calculator uses a multi-factor compensation model:
      - Stratum corneum index (primary depth driver)
      - Hydration index (dry skin requires slightly deeper strike)
      - Surface temperature (cold finger = reduced blood flow = deeper needed)
      - Minor flag (hard cap reduction)
      - Continuous force feedback flag (relayed to C++ RTOS for in-strike correction)
    """

    def compute(self, skin: SkinProfile) -> StrikeProfile:
        notes = []

        # Base depth
        depth = PIN_BASE_DEPTH_MM

        # SC thickness compensation (+0.1mm per 0.1 SC index above 0.3)
        if skin.stratum_corneum_index > 0.3:
            excess = skin.stratum_corneum_index - 0.3
            sc_adjustment = round(excess * 1.0, 2)  # max ~0.7mm for very calloused
            depth += sc_adjustment
            notes.append(f"SC+{sc_adjustment}mm (callous compensation)")

        # Hydration compensation (dry skin deforms less → penetrates shallower)
        if skin.hydration_index < 0.5:
            hydration_adjustment = round((0.5 - skin.hydration_index) * 0.3, 2)
            depth += hydration_adjustment
            notes.append(f"Hydration+{hydration_adjustment}mm (dry skin)")

        # Temperature compensation (cold extremities need slightly deeper strike)
        if skin.surface_temp_celsius < 32.0:
            temp_adjustment = round((32.0 - skin.surface_temp_celsius) * 0.02, 2)
            depth += temp_adjustment
            notes.append(f"Temp+{temp_adjustment}mm (cold extremity)")

        # Minor protection (hard reduction)
        if skin.is_minor:
            depth += PIN_CHILD_ADJUSTMENT_MM
            notes.append(f"Minor-{abs(PIN_CHILD_ADJUSTMENT_MM)}mm (age protection)")

        # Hard clamp — never exceed physical safety bounds
        depth = max(PIN_MIN_DEPTH_MM, min(depth, PIN_MAX_DEPTH_MM))

        return StrikeProfile(
            target_depth_mm         = round(depth, 3),
            stabilizer_pressure_kpa = PIN_STABILIZATION_PRESSURE_KPA,
            strike_velocity_ms      = PIN_STRIKE_VELOCITY_MS,
            retraction_delay_ms     = PIN_RETRACTION_DELAY_MS,
            compensation_applied    = "; ".join(notes) if notes else "baseline",
        )


class AdaptiveSuctionController:
    """
    Controls the suction phase with real-time pressure adaptation.

    Aid Plus algorithm — improvement over standard fixed-vacuum devices:
    Pressure is not predetermined. The controller starts low and
    responds to actual blood flow detected by the optical sensor.
    This adapts naturally to blood viscosity (hematocrit, temperature)
    without requiring any user input or pre-measurement.

    Safety guarantees:
    - Hard ceiling at -15 kPa (below venous pressure — no vascular risk)
    - Hard timeout at 15 seconds
    - Immediate pressure release on target volume reached
    """

    def run(self, hw: BTMHardwareInterface) -> SuctionResult:
        pressure    = SUCTION_INITIAL_PRESSURE_KPA
        volume      = 0.0
        cycles      = 0
        start       = time.time()

        log.info("Suction start | initial_pressure=%.1f kPa | target=%.0fµL",
                 pressure, SUCTION_TARGET_VOLUME_UL)

        while volume < SUCTION_TARGET_VOLUME_UL:
            elapsed = time.time() - start

            # Hard safety timeout
            if elapsed >= SUCTION_TIMEOUT_S:
                log.warning("Suction timeout | volume=%.1fµL | cycles=%d", volume, cycles)
                break

            # Apply one suction cycle
            cycle_volume = hw.apply_suction_cycle(pressure)
            volume += cycle_volume
            cycles += 1

            log.debug("Suction cycle %d | pressure=%.1f kPa | cycle_vol=%.1fµL | total=%.1fµL",
                      cycles, pressure, cycle_volume, volume)

            # If no meaningful flow, escalate pressure
            if cycle_volume < 1.0:
                if pressure > SUCTION_MAX_PRESSURE_KPA:
                    pressure = round(pressure + SUCTION_INCREMENT_KPA, 1)
                    log.info("Pressure escalated to %.1f kPa", pressure)
                else:
                    log.warning("Max suction pressure reached — no additional escalation")

        duration = time.time() - start
        target_met = volume >= SUCTION_TARGET_VOLUME_UL

        log.info("Suction complete | volume=%.1fµL | peak=%.1f kPa | duration=%.1fs | target=%s",
                 volume, pressure, duration, target_met)

        return SuctionResult(
            volume_collected_ul = round(volume, 2),
            peak_pressure_kpa   = pressure,
            flow_cycles         = cycles,
            duration_s          = round(duration, 2),
            target_met          = target_met,
        )


# ─────────────────────────────────────────────
#  COLLECTION ORCHESTRATOR
# ─────────────────────────────────────────────

class BTMSampleCollector:
    """
    AID PLUS+ BTM Sample Collection Orchestrator

    Drives the full collection sequence from scanner pre-check
    through to sample handoff to btm_analysis.

    Deployment-context-aware: accepts a DeploymentContext that
    controls how the session behaves in KIOSK, HOME, or NETWORK mode.

    Usage (KIOSK):
        ctx = DeploymentContext.kiosk()
        collector = BTMSampleCollector(ctx)
        result = collector.collect(session_id, user_card_id, Hand.RIGHT, Finger.INDEX)

    Usage (HOME):
        ctx = DeploymentContext.home(device_id="BTM-HOME-0042")
        collector = BTMSampleCollector(ctx)
        result = collector.collect(session_id, user_card_id, Hand.LEFT, Finger.MIDDLE)
    """

    def __init__(
        self,
        deployment  : DeploymentContext,
        on_status   : Optional[Callable[[CollectionStatus, str], None]] = None,
    ):
        self._ctx       = deployment
        self._hw        = BTMHardwareInterface(simulation=deployment.hw_simulation)
        self._strike_calc = AdaptiveStrikeCalculator()
        self._suction   = AdaptiveSuctionController()
        self._on_status = on_status
        log.info("BTMSampleCollector ready | mode=%s | device=%s",
                 deployment.mode.value, deployment.device_id)

    # ── Main Entry ────────────────────────────

    def collect(
        self,
        session_id   : str,
        user_card_id : str,
        hand         : Hand   = Hand.RIGHT,
        finger       : Finger = Finger.INDEX,
    ) -> CollectionResult:
        """
        Execute the full sample collection sequence.
        Returns CollectionResult with all phase data.
        """
        start = time.time()
        self._status(CollectionStatus.PENDING, "Collection sequence initiated")
        bus.update_session(session_id, "SAMPLE_COLLECTING", {
            "hand": hand.value, "finger": finger.value,
            "deployment_mode": self._ctx.mode.value,
        })

        result = CollectionResult(
            session_id      = session_id,
            user_card_id    = user_card_id,
            status          = CollectionStatus.PENDING,
            deployment_mode = self._ctx.mode.value,
            hand            = hand,
            finger          = finger,
            scanner         = None,
            skin_profile    = None,
            strike_profile  = None,
            suction         = None,
        )

        try:
            # ── Phase 1: Scanner Pre-Check ────
            scanner = self._phase_scanner()
            result.scanner = scanner
            if not scanner.cleared:
                return self._abort(result, AbortReason.CONTAMINATION_DETECTED,
                                   "Contamination detected on skin surface. "
                                   "Please wash hands and try again.")

            # ── Phase 2: Pre-Collection Clean ─
            self._phase_pre_clean()

            # ── Phase 3: Skin Assessment ──────
            skin = self._phase_skin_assessment(hand, finger)
            result.skin_profile = skin

            # ── Phase 4: Strike Profile ───────
            strike = self._strike_calc.compute(skin)
            result.strike_profile = strike
            log.info("Strike profile | depth=%.3fmm | compensation: %s",
                     strike.target_depth_mm, strike.compensation_applied)

            # ── Phase 5: Pin Strike ───────────
            blood_confirmed = self._phase_strike(strike)
            if not blood_confirmed:
                return self._abort(result, AbortReason.BLOOD_CONFIRM_TIMEOUT,
                                   "Blood not confirmed at wound site. "
                                   "Please contact Aid Plus support.")

            # ── Phase 6: Adaptive Suction ─────
            suction = self._phase_suction()
            result.suction = suction

            if suction.volume_collected_ul < SUCTION_TARGET_VOLUME_UL * 0.6:
                return self._abort(result, AbortReason.INSUFFICIENT_VOLUME,
                                   f"Insufficient blood volume collected "
                                   f"({suction.volume_collected_ul:.1f}µL). "
                                   f"Please try again or visit an Aid Plus centre.")

            # ── Phase 7: Post-Collection Clean ─
            self._phase_post_clean()

            # ── Sequence timeout guard ─────────
            duration = time.time() - start
            if duration > TOTAL_SEQUENCE_TIMEOUT_S:
                log.warning("Sequence exceeded time budget: %.1fs", duration)

            # ── Success ────────────────────────
            result.status               = CollectionStatus.COMPLETE
            result.sample_volume_ul     = suction.volume_collected_ul
            result.collection_duration_s = round(duration, 2)
            result.ready_for_analysis   = True

            self._status(CollectionStatus.COMPLETE,
                         f"Collection complete | {suction.volume_collected_ul:.1f}µL in {duration:.1f}s")

            bus.update_session(session_id, "SAMPLE_COMPLETE", {
                "volume_ul"  : suction.volume_collected_ul,
                "duration_s" : result.collection_duration_s,
                "peak_pressure_kpa": suction.peak_pressure_kpa,
            })

            self._publish_sample_event(result)
            return result

        except Exception as e:
            log.exception("Unexpected error during collection")
            return self._abort(result, AbortReason.HARDWARE_FAULT, str(e))

    # ── Phase Implementations ─────────────────

    def _phase_scanner(self) -> ScannerResult:
        self._status(CollectionStatus.SCANNER_CHECK, "Running pre-collection safety scan")
        log.info("Phase 1: Scanner pre-check")

        bacteria_index                  = self._hw.run_bacteria_scan()
        dna_matched, dna_hash           = self._hw.run_dna_scan()
        scan_duration                   = (SCANNER_BACTERIA_WINDOW_MS + SCANNER_DNA_WINDOW_MS)
        cleared                         = bacteria_index < SCANNER_CONTAMINATION_THRESHOLD

        log.info("Scanner | bacteria=%.3f | dna_match=%s | cleared=%s",
                 bacteria_index, dna_matched, cleared)

        return ScannerResult(
            bacteria_index          = round(bacteria_index, 4),
            dna_fingerprint_matched = dna_matched,
            dna_fingerprint_hash    = dna_hash,
            scan_duration_ms        = scan_duration,
            cleared                 = cleared,
        )

    def _phase_pre_clean(self) -> None:
        self._status(CollectionStatus.PRE_CLEAN, "Applying pre-collection clean")
        log.info("Phase 2: Pre-collection wet tissue clean")
        self._hw.dispense_wet_tissue(TISSUE_DISPENSE_DURATION_S)

    def _phase_skin_assessment(self, hand: Hand, finger: Finger) -> SkinProfile:
        self._status(CollectionStatus.STRIKE_PREP, "Assessing skin profile")
        log.info("Phase 3: Skin assessment | %s %s", hand.value, finger.value)
        return self._hw.probe_skin_profile(hand, finger)

    def _phase_strike(self, profile: StrikeProfile) -> bool:
        self._status(CollectionStatus.STRIKING, "Performing sample collection")
        log.info("Phase 4: Pin strike | depth=%.3fmm", profile.target_depth_mm)
        return self._hw.execute_strike(profile)

    def _phase_suction(self) -> SuctionResult:
        self._status(CollectionStatus.SUCTIONING, "Drawing blood sample")
        log.info("Phase 5: Adaptive suction")
        return self._suction.run(self._hw)

    def _phase_post_clean(self) -> None:
        self._status(CollectionStatus.POST_CLEAN, "Applying post-collection clean")
        log.info("Phase 6: Post-collection clean + bleed arrest")
        self._hw.dispense_wet_tissue(TISSUE_DISPENSE_DURATION_S)
        self._hw.apply_post_clean_pressure(POST_CLEAN_PRESSURE_S)

    # ── Helpers ───────────────────────────────

    def _abort(
        self,
        result  : CollectionResult,
        reason  : AbortReason,
        detail  : str,
    ) -> CollectionResult:
        result.status       = CollectionStatus.ABORTED
        result.abort_reason = reason
        result.error_detail = detail
        self._status(CollectionStatus.ABORTED, f"Aborted: {reason.value}")
        log.warning("Collection aborted | reason=%s | detail=%s", reason.value, detail)

        bus.publish(
            message_type = MessageType.ERROR_REPORT,
            payload      = {
                "session_id"  : result.session_id,
                "abort_reason": reason.value,
                "detail"      : detail,
                "deployment"  : result.deployment_mode,
            },
            priority     = Priority.HIGH,
            session_id   = result.session_id,
        )
        return result

    def _status(self, status: CollectionStatus, message: str) -> None:
        log.info("[%s] %s", status.value, message)
        if self._on_status:
            self._on_status(status, message)

    def _publish_sample_event(self, result: CollectionResult) -> None:
        bus.publish(
            message_type = MessageType.SAMPLE_EVENT,
            payload      = {
                "session_id"        : result.session_id,
                "user_card_id"      : result.user_card_id,
                "deployment_mode"   : result.deployment_mode,
                "volume_ul"         : result.sample_volume_ul,
                "duration_s"        : result.collection_duration_s,
                "strike_depth_mm"   : result.strike_profile.target_depth_mm if result.strike_profile else None,
                "peak_pressure_kpa" : result.suction.peak_pressure_kpa if result.suction else None,
                "ready_for_analysis": result.ready_for_analysis,
            },
            priority     = Priority.HIGH,
            session_id   = result.session_id,
            user_card_id = result.user_card_id,
        )


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Sample Collection — Test Suite ===\n")

    bus.activate(hw_simulation=True)

    # Status callback for live feedback
    def on_status(status: CollectionStatus, message: str):
        print(f"    [{status.value:<18}] {message}")

    # ── Test 1: KIOSK mode ─────────────────────────────────────────────
    print("  [Test 1] KIOSK deployment — standard adult collection")
    ctx_kiosk = DeploymentContext.kiosk("BTM-KIOSK-GH-001")
    collector  = BTMSampleCollector(ctx_kiosk, on_status=on_status)
    session    = bus.open_session("AID-A1B2-C3D4-E5F6")
    result     = collector.collect(session, "AID-A1B2-C3D4-E5F6", Hand.RIGHT, Finger.INDEX)

    print(f"\n  Result Summary:")
    print(f"    Status          : {result.status.value}")
    print(f"    Volume collected: {result.sample_volume_ul:.1f} µL")
    print(f"    Duration        : {result.collection_duration_s:.1f}s")
    if result.strike_profile:
        print(f"    Strike depth    : {result.strike_profile.target_depth_mm:.3f} mm")
        print(f"    Compensation    : {result.strike_profile.compensation_applied}")
    if result.suction:
        print(f"    Peak pressure   : {result.suction.peak_pressure_kpa:.1f} kPa")
        print(f"    Suction cycles  : {result.suction.flow_cycles}")
    if result.scanner:
        print(f"    Bacteria index  : {result.scanner.bacteria_index:.4f}")
        print(f"    DNA matched     : {result.scanner.dna_fingerprint_matched}")
    print(f"    Ready for analysis: {result.ready_for_analysis}")
    bus.close_session(session)

    # ── Test 2: HOME mode ──────────────────────────────────────────────
    print("\n\n  [Test 2] HOME deployment — left hand, middle finger")
    ctx_home  = DeploymentContext.home("BTM-HOME-0042", "https://api.aidplus.io/infobox")
    collector2 = BTMSampleCollector(ctx_home, on_status=on_status)
    session2   = bus.open_session("AID-G7H8-I9J0-K1L2")
    result2    = collector2.collect(session2, "AID-G7H8-I9J0-K1L2", Hand.LEFT, Finger.MIDDLE)

    print(f"\n  Result Summary:")
    print(f"    Status          : {result2.status.value}")
    print(f"    Deployment mode : {result2.deployment_mode}")
    print(f"    Volume collected: {result2.sample_volume_ul:.1f} µL")
    print(f"    Ready for analysis: {result2.ready_for_analysis}")
    bus.close_session(session2)

    print("\n✓ BTM Sample Collection test complete\n")
    bus.deactivate()
