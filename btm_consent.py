"""
btm_consent.py — AID PLUS+ BTM Consent-Gated Referral Capture
==================================================================
Captures a patient's accept/decline decision on the "would you like
confidential support?" prompt shown only when
HealthNarrative.requires_consent_flow is True, and — only on
acceptance — issues a shared/referral.py ReferralCode and delivers it
to the patient's own Infobox.

Design constraints (see btm_analysis.py's TransmissibleDiseasePanel
docstring and btm_ai_interpreter.py's sensitive_narrative for the
fuller design rationale this module implements one piece of):
    - The code is generated at the moment of "yes" — never before,
      never speculatively. See ReferralCode.issue() in
      shared/referral.py, the only call site in this module.
    - A "no" is close to a no-op — the decision is logged (as a
      boolean, on the bus) but nothing identifiable is created beyond
      what the patient already had (their own private result,
      already delivered by btm_results.py).
    - Universal crisis/support resources are a UI-layer concern
      (btm_ui.py, shown to every patient regardless of result) — this
      module has nothing to do with those; it only handles the
      code-issuing side of an explicit "yes".
    - Data minimisation: the bus event this module publishes carries
      only a boolean (referral_issued) — never the reactive marker
      names or the code itself — matching the same principle
      btm_analysis.py already applies to its own bus event.

Delivery: deliberately mirrors BTMResultsEngine's KIOSK/HOME/NETWORK
routing (local bus / cloud+buffer / cloud+buffer+alert) rather than
sharing its routing code directly. This is a reasonable amount of
duplication for a first caller — worth extracting into a shared
delivery helper once a second consumer actually needs the same
routing tree (the Code-6 kiosk, once retrofitted to shared/, is the
natural forcing function for that — premature to abstract for one
caller today).

IMPORTANT — offline buffer isolation: this module's BTMOfflineBuffer
uses a device_id suffixed with "-referrals", deliberately different
from BTMResultsEngine's own buffer file. Reusing the same device_id
would put differently-shaped entries (web_view-wrapped blood-test
results vs. plain referral payloads) in the same .jsonl queue file —
BTMResultsEngine.sync_pending() would KeyError the first time it tried
to re-sync a buffered referral. Do not remove this suffix.

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from btm_bus import bus, MessageType, Priority
from btm_sample import DeploymentContext, DeploymentMode
from btm_results import CloudInfoboxClient, BTMOfflineBuffer, DeliveryStatus
from btm_ai_interpreter import HealthNarrative
from shared.infobox import InfoboxEntry
from shared.referral import ReferralCode

log = logging.getLogger("btm_consent")


# ─────────────────────────────────────────────
#  ENUMS / DATA STRUCTURES
# ─────────────────────────────────────────────

class ConsentDecision(Enum):
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"


@dataclass
class ConsentOutcome:
    """Result of a capture() call — always returned, never raises."""
    session_id       : str
    user_card_id     : str
    decision          : ConsentDecision
    referral_code      : Optional[str] = None   # only set on ACCEPTED + successful issue
    delivery_status      : Optional[str] = None  # only set on ACCEPTED


# ─────────────────────────────────────────────
#  MAIN ENGINE
# ─────────────────────────────────────────────

class ConsentCaptureEngine:
    """
    Usage:
        engine  = ConsentCaptureEngine(deployment_context)
        outcome = engine.capture(narrative, ConsentDecision.ACCEPTED)

    Periodic sync for buffered referrals (HOME/NETWORK devices —
    intended to be called from run_btm.py's connectivity loop
    alongside BTMResultsEngine.sync_pending(), once wired up):
        engine.sync_pending()
    """

    def __init__(self, deployment: DeploymentContext,
                 cloud_client: Optional[CloudInfoboxClient] = None,
                 buffer: Optional[BTMOfflineBuffer] = None):
        self._ctx    = deployment
        self._cloud  = cloud_client or CloudInfoboxClient()
        # See module docstring — "-referrals" suffix is deliberate and
        # must not be removed.
        self._buffer = buffer or BTMOfflineBuffer(device_id=f"{deployment.device_id}-referrals")
        log.info("ConsentCaptureEngine ready | mode=%s | device=%s",
                 deployment.mode.value, deployment.device_id)

    def capture(self, narrative: HealthNarrative, decision: ConsentDecision) -> ConsentOutcome:
        """
        Records the patient's decision. On ACCEPTED, issues a referral
        code and delivers it to the Infobox. Never raises — delivery
        failure degrades to buffering/alerting exactly like
        BTMResultsEngine, reflected in the returned outcome rather
        than an exception.
        """
        if not narrative.requires_consent_flow:
            log.warning("capture() called for a narrative that doesn't require "
                       "consent — no-op. session=%s", narrative.session_id)
            return ConsentOutcome(
                session_id=narrative.session_id, user_card_id=narrative.user_card_id,
                decision=decision,
            )

        if decision == ConsentDecision.DECLINED:
            log.info("Consent declined | session=%s", narrative.session_id)
            self._report_decision(narrative, decision, referral_issued=False)
            return ConsentOutcome(
                session_id=narrative.session_id, user_card_id=narrative.user_card_id,
                decision=decision,
            )

        referral = ReferralCode.issue(
            user_card_id = narrative.user_card_id,
            session_id   = narrative.session_id,
            markers      = narrative.reactive_markers,
        )
        entry = InfoboxEntry.for_referral(
            user_card_id = narrative.user_card_id,
            referral     = referral.to_dict(),
            session_id   = narrative.session_id,
        )

        delivery_status = self._deliver(entry, narrative.session_id, narrative.user_card_id)
        self._report_decision(narrative, decision, referral_issued=True)

        log.info("Referral issued | session=%s | status=%s",
                 narrative.session_id, delivery_status.value)
        return ConsentOutcome(
            session_id      = narrative.session_id,
            user_card_id    = narrative.user_card_id,
            decision        = decision,
            referral_code   = referral.code,
            delivery_status = delivery_status.value,
        )

    def sync_pending(self) -> int:
        """Flushes buffered referrals to the cloud infobox. Mirrors
        BTMResultsEngine.sync_pending()'s shape but operates on plain
        InfoboxEntry payload dicts — no web_view unwrapping needed,
        since a referral entry IS the payload already."""
        if not self._ctx.cloud_endpoint:
            return 0
        pending = self._buffer.pending()
        if not pending:
            return 0

        remaining: List[Dict] = []
        synced = 0
        for entry in pending:
            try:
                self._cloud.deliver(entry, self._ctx.cloud_endpoint)
                bus.deliver_to_infobox(
                    user_card_id = entry["user_card_id"],
                    result_data  = entry,
                    session_id   = entry.get("session_id"),
                )
                synced += 1
                log.info("Synced buffered referral | session=%s", entry.get("session_id"))
            except Exception as e:
                log.warning("Buffered referral still undeliverable | session=%s | %s",
                           entry.get("session_id"), e)
                remaining.append(entry)

        self._buffer.rewrite(remaining)
        return synced

    # ── Delivery (mirrors BTMResultsEngine's routing — see module docstring) ──

    def _deliver(self, entry: InfoboxEntry, session_id: str, user_card_id: str) -> DeliveryStatus:
        payload = entry.to_dict()

        if self._ctx.mode == DeploymentMode.KIOSK:
            try:
                bus.deliver_to_infobox(user_card_id=user_card_id, result_data=payload, session_id=session_id)
                return DeliveryStatus.DELIVERED
            except Exception as e:
                log.error("Local infobox delivery failed for referral | session=%s | %s", session_id, e)
                self._alert_failure(session_id, str(e))
                return DeliveryStatus.FAILED

        # HOME and NETWORK both attempt cloud delivery, falling back to the
        # offline buffer — NETWORK additionally alerts maintenance, the same
        # distinction BTMResultsEngine draws (single HOME device going
        # offline isn't operationally urgent; a NETWORK unit going dark is).
        connected = (self._ctx.mode == DeploymentMode.NETWORK
                    or self._ctx.wifi_available or self._ctx.ble_available)
        if connected and self._ctx.cloud_endpoint:
            try:
                self._cloud.deliver(payload, self._ctx.cloud_endpoint)
                bus.deliver_to_infobox(user_card_id=user_card_id, result_data=payload, session_id=session_id)
                return DeliveryStatus.DELIVERED
            except Exception as e:
                log.warning("Cloud referral delivery failed | session=%s | %s — buffering.", session_id, e)
                if self._ctx.mode == DeploymentMode.NETWORK:
                    self._alert_failure(session_id, str(e))

        if self._ctx.offline_buffer:
            self._buffer.enqueue(entry)   # duck-types on to_dict() — see BTMOfflineBuffer
            return DeliveryStatus.BUFFERED
        return DeliveryStatus.FAILED

    # ── Bus reporting helpers ─────────────────

    def _alert_failure(self, session_id: str, error: str) -> None:
        try:
            bus.send_maintenance_alert(
                alert_type = "REFERRAL_DELIVERY_FAILED",
                details    = {"device_id": self._ctx.device_id, "session_id": session_id, "error": error},
                critical   = True,
            )
        except Exception as e:
            log.error("Could not send maintenance alert to bus: %s", e)

    def _report_decision(self, narrative: HealthNarrative, decision: ConsentDecision,
                         referral_issued: bool) -> None:
        """Data minimisation: never publishes marker names or the
        referral code itself — only that a decision was made and
        whether a referral resulted. See btm_analysis.py's own bus
        event for the same principle applied to the reactive screen."""
        try:
            bus.publish(
                message_type = MessageType.STATUS_UPDATE,
                payload      = {
                    "event"          : "CONSENT_DECISION_RECORDED",
                    "decision"       : decision.value,
                    "referral_issued": referral_issued,
                    "device_id"      : self._ctx.device_id,
                },
                priority     = Priority.NORMAL,
                session_id   = narrative.session_id,
                user_card_id = narrative.user_card_id,
            )
        except Exception as e:
            log.error("Could not report consent decision to bus: %s", e)


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Consent Capture — Test Suite ===\n")

    import sys
    sys.path.insert(0, ".")

    from btm_bus import bus as _bus
    from btm_sample import DeploymentContext, DeploymentMode
    from btm_analysis import BTMAnalysisEngine, Sex, BTMSensorSimulator
    from btm_ai_interpreter import BTMAIInterpreter, Urgency
    from btm_sample import CollectionResult, CollectionStatus, Hand, Finger

    _bus.activate(hw_simulation=True)

    # Force a reactive screening result deterministically, same
    # technique used to verify btm_results.py's wiring earlier.
    _orig = BTMSensorSimulator.generate_screening_profile
    BTMSensorSimulator.generate_screening_profile = lambda self: {
        "syphilis_rpr": False, "hiv_screen": True, "hepatitis_b_hbsag": False,
    }

    def _make_reactive_narrative(session_id: str):
        session = _bus.open_session(f"AID-CONSENT-{session_id}")
        collection = CollectionResult(
            session_id=session, user_card_id=f"AID-CONSENT-{session_id}",
            status=CollectionStatus.COMPLETE, deployment_mode="HOME",
            hand=Hand.RIGHT, finger=Finger.INDEX,
            scanner=None, skin_profile=None, strike_profile=None, suction=None,
            sample_volume_ul=52.0, collection_duration_s=18.4, ready_for_analysis=True,
        )
        engine = BTMAnalysisEngine(hw_simulation=True)
        report = engine.analyse(collection, sex=Sex.FEMALE, age=29)
        interpreter = BTMAIInterpreter(use_default_provider=False)
        narrative = interpreter.interpret(report)
        interpreter.shutdown()
        _bus.close_session(session)
        return narrative

    contexts = [
        ("KIOSK", DeploymentContext.kiosk("BTM-KIOSK-CONSENT-TEST")),
        ("HOME — offline", DeploymentContext(
            mode=DeploymentMode.HOME, device_id="BTM-HOME-CONSENT-TEST",
            cloud_endpoint="https://api.aidplus.io/infobox",
            wifi_available=False, ble_available=False,
        )),
        ("NETWORK", DeploymentContext.network("BTM-NET-CONSENT-TEST", "https://clinic.aidplus.io/infobox")),
    ]

    for label, ctx in contexts:
        print(f"  [{label} — ACCEPTED]")
        narrative = _make_reactive_narrative(label.replace(" ", "-").replace("—", ""))
        assert narrative.requires_consent_flow, "Test setup failed — narrative should require consent"
        assert narrative.reactive_markers == ["HIV"]

        engine = ConsentCaptureEngine(ctx)
        outcome = engine.capture(narrative, ConsentDecision.ACCEPTED)

        print(f"    Referral code    : {outcome.referral_code}")
        print(f"    Delivery status  : {outcome.delivery_status}")
        assert outcome.referral_code is not None
        assert outcome.referral_code.startswith("AID-REF-")
        assert outcome.delivery_status in ("DELIVERED", "BUFFERED")
        print()

    # Decline path — must be near-no-op, no referral code
    print("  [KIOSK — DECLINED]")
    narrative = _make_reactive_narrative("decline-test")
    engine = ConsentCaptureEngine(contexts[0][1])
    outcome = engine.capture(narrative, ConsentDecision.DECLINED)
    print(f"    Referral code    : {outcome.referral_code}")
    print(f"    Delivery status  : {outcome.delivery_status}")
    assert outcome.referral_code is None
    assert outcome.delivery_status is None
    print()

    # Buffer isolation check — confirm the referral buffer file is
    # distinct from BTMResultsEngine's own buffer file for the same
    # device (the exact bug this module's docstring warns about).
    from btm_results import BTMOfflineBuffer as _RB
    home_ctx = contexts[1][1]
    results_buffer  = _RB(device_id=home_ctx.device_id)
    consent_engine  = ConsentCaptureEngine(home_ctx)
    print(f"  Buffer path check:")
    print(f"    Results buffer  : {results_buffer._path}")
    print(f"    Consent buffer  : {consent_engine._buffer._path}")
    assert results_buffer._path != consent_engine._buffer._path, \
        "Referral buffer must NOT share a file with the results buffer"
    print(f"    Pending referrals (HOME): {consent_engine._buffer.pending_count()}")

    # Non-consent-required narrative — should be a clean no-op
    print("\n  [Non-reactive narrative — capture() should no-op]")
    BTMSensorSimulator.generate_screening_profile = _orig
    narrative2 = _make_reactive_narrative("no-consent-needed")
    print(f"    requires_consent_flow: {narrative2.requires_consent_flow}")
    assert narrative2.requires_consent_flow is False
    outcome2 = ConsentCaptureEngine(contexts[0][1]).capture(narrative2, ConsentDecision.ACCEPTED)
    assert outcome2.referral_code is None
    print(f"    Outcome referral_code: {outcome2.referral_code} (correctly None)")

    _bus.deactivate()
    print("\n✓ BTM Consent Capture test complete\n")
