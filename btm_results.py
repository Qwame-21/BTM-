"""
btm_results.py — AID PLUS+ BTM Results Packaging & Delivery
==============================================================
Packages the AnalysisReport + AI HealthNarrative into surface-specific
views (kiosk / phone / web) and routes delivery to the Aid Plus Infobox
according to the active DeploymentContext.

Responsibilities:
    - Format results for three consumption surfaces without re-deriving
      any clinical logic — this module packages, it never analyses.
    - Route delivery: local bus (KIOSK), cloud upload with offline
      fallback (HOME), cloud upload with maintenance alerting on failure
      (NETWORK).
    - Durable offline buffering for HOME-mode devices that lose
      connectivity — queued locally, flushed by sync_pending() once
      connectivity returns.

Design principles:
    Never blocks     — delivery failure never raises past this module;
                       it degrades to buffering (or a maintenance alert)
                       and always returns a ResultPackage describing what
                       actually happened.
    Production code   — CloudInfoboxClient makes real HTTP calls. There
                       is no simulation flag here; btm_hw_interface.py
                       remains the only file where sim/production differ.
                       Against a placeholder endpoint this will simply
                       fail closed and exercise the buffering path, which
                       is the correct production behaviour for lost
                       connectivity anyway.
    Deployment-aware   — every routing decision reads DeploymentContext;
                       nothing here assumes KIOSK.

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from btm_bus import bus, MessageType, Priority
from btm_sample import DeploymentContext, DeploymentMode
from btm_analysis import AnalysisReport, HaemoglobinGenotype
from btm_ai_interpreter import HealthNarrative
from config import RESULTS_OFFLINE_BUFFER_DIR
from shared.infobox import InfoboxEntry

log = logging.getLogger("btm_results")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class DeliveryStatus(Enum):
    PENDING   = "PENDING"
    DELIVERED = "DELIVERED"
    BUFFERED  = "BUFFERED"
    FAILED    = "FAILED"


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class ResultPackage:
    """Complete packaged result — all three views + delivery outcome."""
    session_id            : str
    user_card_id          : str
    deployment_mode        : str
    kiosk_view             : Dict
    phone_view             : Dict
    web_view               : Dict
    delivery_status         : DeliveryStatus = DeliveryStatus.PENDING
    delivery_destination     : str            = ""
    delivered_at             : Optional[str]  = None
    buffered_at               : Optional[str] = None
    error_detail               : Optional[str] = None
    packaged_at                 : str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict:
        return {
            "session_id"           : self.session_id,
            "user_card_id"         : self.user_card_id,
            "deployment_mode"      : self.deployment_mode,
            "kiosk_view"           : self.kiosk_view,
            "phone_view"           : self.phone_view,
            "web_view"             : self.web_view,
            "delivery_status"      : self.delivery_status.value,
            "delivery_destination" : self.delivery_destination,
            "delivered_at"         : self.delivered_at,
            "buffered_at"          : self.buffered_at,
            "error_detail"         : self.error_detail,
            "packaged_at"          : self.packaged_at,
        }


# ─────────────────────────────────────────────
#  FORMATTERS
# ─────────────────────────────────────────────

class BTMResultFormatter:
    """
    Builds the three delivery views from an already-computed
    AnalysisReport + HealthNarrative. No clinical logic lives here —
    only packaging decisions about what each surface shows.
    """

    def format_kiosk(self, report: AnalysisReport, narrative: HealthNarrative) -> Dict:
        """Minimal — what fits on a public kiosk screen in a few seconds.

        The BTM is used one patient at a time, so this view is private to
        whoever is at the device right now — that's what makes it safe to
        carry the consent-flow fields at all. requires_consent_flow /
        reactive_markers / sensitive_narrative are never folded into
        headline/summary/disclaimer above; they're kept as their own
        fields so btm_ui.py can render them as a separate, deliberately
        shown private card rather than mixed into the general screen."""
        chi = report.health_index
        return {
            "headline"          : narrative.headline,
            "urgency"           : narrative.urgency.value,
            "chi_score"         : chi.score,
            "chi_grade"         : chi.grade,
            "summary"           : self._truncate_sentences(narrative.narrative, 2),
            "follow_up_actions" : narrative.follow_up_actions[:3],
            "genotype_note"     : self._genotype_kiosk_note(report.haemoglobinopathy),
            "disclaimer"        : narrative.disclaimer,
            "requires_consent_flow" : narrative.requires_consent_flow,
            "reactive_markers"      : narrative.reactive_markers,
            "sensitive_narrative"   : narrative.sensitive_narrative,
        }

    def format_phone(self, report: AnalysisReport, narrative: HealthNarrative) -> Dict:
        """Full narrative + per-panel summaries — the primary patient view."""
        chi = report.health_index
        return {
            "headline"           : narrative.headline,
            "urgency"            : narrative.urgency.value,
            "narrative"          : narrative.narrative,
            "follow_up_actions"  : narrative.follow_up_actions,
            "health_index"       : {
                "score"          : chi.score,
                "grade"          : chi.grade,
                "panel_scores"   : chi.panel_scores,
                "flags_count"    : chi.flags_count,
                "critical_count" : chi.critical_count,
                "trend"          : chi.trend,
            },
            "panels"             : self._panel_summaries(report),
            "narrative_source"   : narrative.source.value,
            "model_used"         : narrative.model_used,
            "generated_at"       : narrative.generated_at,
            "disclaimer"         : narrative.disclaimer,
            "requires_consent_flow" : narrative.requires_consent_flow,
            "reactive_markers"      : narrative.reactive_markers,
            "sensitive_narrative"   : narrative.sensitive_narrative,
        }

    def format_web(self, report: AnalysisReport, narrative: HealthNarrative) -> Dict:
        """Everything phone has, plus the full raw measurement table —
        for a printable / clinical-facing dashboard view."""
        web_view = self.format_phone(report, narrative)
        web_view["raw_measurements"] = self._all_measurements(report)
        web_view["sample_meta"] = {
            "sex"                  : report.sex.value,
            "age_years"            : report.age_years,
            "sample_volume_ul"     : report.sample_volume_ul,
            "analysis_duration_s"  : report.analysis_duration_s,
            "analysed_at"          : report.analysed_at,
            "anomaly_level"        : report.anomaly_level,
            "anomaly_detail"       : report.anomaly_detail,
        }
        return web_view

    # ── Internals ──────────────────────────────

    def _panel_summaries(self, report: AnalysisReport) -> Dict:
        haem = report.haemoglobinopathy
        return {
            "cbc"    : {"anaemia_type": report.cbc.anaemia_type.value,
                        "infection_index": report.cbc.infection_index,
                        "interpretation": report.cbc.interpretation},
            "lipid"  : {"risk_level": report.lipid.risk_level.value,
                        "cvd_risk_score": report.lipid.cvd_risk_score,
                        "interpretation": report.lipid.interpretation},
            "glucose": {"classification": report.glucose.classification.value,
                        "estimated_hba1c": report.glucose.estimated_hba1c,
                        "interpretation": report.glucose.interpretation},
            "liver"  : {"hepatic_stress_index": report.liver.hepatic_stress_index,
                        "interpretation": report.liver.interpretation},
            "kidney" : {"egfr": report.kidney.eGFR, "ckd_stage": report.kidney.ckd_stage.value,
                        "interpretation": report.kidney.interpretation},
            "markers": {"interpretation": report.markers.interpretation},
            "haemoglobinopathy": {"genotype": haem.genotype.value,
                                   "is_carrier": haem.is_carrier, "is_disease": haem.is_disease,
                                   "detection_method": haem.detection_method,
                                   "interpretation": haem.interpretation},
        }

    def _all_measurements(self, report: AnalysisReport) -> Dict:
        panels = {
            "cbc": report.cbc, "lipid": report.lipid, "glucose": report.glucose,
            "liver": report.liver, "kidney": report.kidney, "markers": report.markers,
            "haemoglobinopathy": report.haemoglobinopathy,
        }
        out: Dict[str, Dict] = {}
        for panel_name, panel in panels.items():
            out[panel_name] = {}
            for attr, val in vars(panel).items():
                if hasattr(val, "value") and hasattr(val, "flag") and hasattr(val, "unit"):
                    out[panel_name][attr] = {
                        "value": val.value, "unit": val.unit,
                        "ref_low": val.ref_low, "ref_high": val.ref_high,
                        "flag": val.flag, "confidence": val.confidence,
                    }
        return out

    def _genotype_kiosk_note(self, haem) -> Optional[str]:
        if haem.genotype == HaemoglobinGenotype.AA:
            return None
        qualifier = "carrier" if haem.is_carrier else "disease"
        return f"Haemoglobin type: {haem.genotype.value} ({qualifier}) — see full report for details."

    def _truncate_sentences(self, text: str, n: int) -> str:
        if not text:
            return text
        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        return " ".join(sentences[:n])


# ─────────────────────────────────────────────
#  CLOUD DELIVERY
# ─────────────────────────────────────────────

class CloudInfoboxClient:
    """
    Production HTTP client for HOME/NETWORK cloud infobox delivery.
    No simulation branch — this is real networking code. Against a
    placeholder endpoint (no live Aid Plus cloud yet) it fails closed
    with ConnectionError, which is exactly the signal the caller needs
    to fall back to offline buffering.
    """

    def __init__(self, timeout_s: float = 8.0):
        self._timeout_s = timeout_s

    def deliver(self, payload: Dict, endpoint: str) -> bool:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                if 200 <= resp.status < 300:
                    return True
                raise ConnectionError(f"Cloud infobox responded with status {resp.status}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            raise ConnectionError(f"Cloud infobox delivery failed: {e}") from e


# ─────────────────────────────────────────────
#  OFFLINE BUFFER
# ─────────────────────────────────────────────

class BTMOfflineBuffer:
    """
    Local durable queue for results that couldn't reach the cloud
    infobox immediately. One queue file per device. Flushed by
    BTMResultsEngine.sync_pending() — intended to be called from the
    device's periodic connectivity check (wired up in run_btm.py).

    Storage path is a sensible default for now; config.py will make
    this configurable once built.
    """
    _DEFAULT_DIR = RESULTS_OFFLINE_BUFFER_DIR

    def __init__(self, device_id: str, directory: Optional[str] = None):
        self._device_id = device_id
        self._dir = directory or self._DEFAULT_DIR
        try:
            os.makedirs(self._dir, exist_ok=True)
        except OSError as e:
            log.error("Could not create offline buffer directory %s: %s", self._dir, e)
        self._path = os.path.join(self._dir, f"{device_id}.jsonl")

    def enqueue(self, package: ResultPackage) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(package.to_dict()) + "\n")
            log.info("Buffered result | session=%s | queue=%s", package.session_id, self._path)
        except OSError as e:
            log.error("Failed to write to offline buffer %s: %s", self._path, e)

    def pending(self) -> List[Dict]:
        if not os.path.exists(self._path):
            return []
        entries = []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except OSError as e:
            log.error("Failed to read offline buffer %s: %s", self._path, e)
        return entries

    def pending_count(self) -> int:
        return len(self.pending())

    def rewrite(self, remaining: List[Dict]) -> None:
        """Overwrites the queue with only the entries that still need syncing."""
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                for entry in remaining:
                    f.write(json.dumps(entry) + "\n")
        except OSError as e:
            log.error("Failed to rewrite offline buffer %s: %s", self._path, e)

    def clear(self) -> None:
        try:
            if os.path.exists(self._path):
                os.remove(self._path)
        except OSError as e:
            log.error("Failed to clear offline buffer %s: %s", self._path, e)


# ─────────────────────────────────────────────
#  MAIN ENGINE
# ─────────────────────────────────────────────

class BTMResultsEngine:
    """
    AID PLUS+ BTM Results Packaging & Delivery Engine

    Usage:
        ctx    = DeploymentContext.kiosk()
        engine = BTMResultsEngine(ctx)
        package = engine.deliver(report, narrative)

    Periodic sync (HOME/NETWORK devices, called from run_btm.py's
    connectivity loop once built):
        engine.sync_pending()
    """

    def __init__(self, deployment: DeploymentContext,
                 cloud_client: Optional[CloudInfoboxClient] = None,
                 buffer: Optional[BTMOfflineBuffer] = None):
        self._ctx        = deployment
        self._formatter   = BTMResultFormatter()
        self._cloud       = cloud_client or CloudInfoboxClient()
        self._buffer      = buffer or BTMOfflineBuffer(device_id=deployment.device_id)
        log.info("BTMResultsEngine ready | mode=%s | device=%s",
                 deployment.mode.value, deployment.device_id)

    def _infobox_payload(self, package: ResultPackage) -> Dict:
        """Wraps the package's web_view in the shared Infobox schema —
        this is what actually gets sent to bus.deliver_to_infobox() and
        CloudInfoboxClient.deliver(), not the raw view dict."""
        return InfoboxEntry.for_blood_test(
            user_card_id = package.user_card_id,
            results      = package.web_view,
            session_id   = package.session_id,
        ).to_dict()

    def deliver(self, report: AnalysisReport, narrative: HealthNarrative) -> ResultPackage:
        """
        Packages the report + narrative into all three views and routes
        delivery according to the active DeploymentContext. Always
        returns a ResultPackage describing the outcome — never raises.
        """
        package = ResultPackage(
            session_id       = report.session_id,
            user_card_id     = report.user_card_id,
            deployment_mode  = self._ctx.mode.value,
            kiosk_view       = self._formatter.format_kiosk(report, narrative),
            phone_view       = self._formatter.format_phone(report, narrative),
            web_view         = self._formatter.format_web(report, narrative),
        )

        if self._ctx.mode == DeploymentMode.KIOSK:
            self._deliver_local(package)
        elif self._ctx.mode == DeploymentMode.HOME:
            self._deliver_home(package)
        elif self._ctx.mode == DeploymentMode.NETWORK:
            self._deliver_network(package)
        else:
            log.warning("Unknown deployment mode %s — defaulting to local delivery", self._ctx.mode)
            self._deliver_local(package)

        log.info("Result delivery complete | session=%s | status=%s | destination=%s",
                 package.session_id, package.delivery_status.value, package.delivery_destination)
        return package

    def sync_pending(self) -> int:
        """
        Attempts to flush the offline buffer to the cloud infobox.
        Returns the number of entries successfully synced. Safe to call
        repeatedly (e.g. from a periodic connectivity check) — it's a
        no-op when there's nothing buffered or no cloud_endpoint configured.
        """
        if not self._ctx.cloud_endpoint:
            return 0
        pending = self._buffer.pending()
        if not pending:
            return 0

        remaining: List[Dict] = []
        synced = 0
        for entry in pending:
            try:
                infobox_payload = InfoboxEntry.for_blood_test(
                    user_card_id = entry["user_card_id"],
                    results      = entry["web_view"],
                    session_id   = entry["session_id"],
                ).to_dict()
                self._cloud.deliver(infobox_payload, self._ctx.cloud_endpoint)
                bus.deliver_to_infobox(
                    user_card_id = entry["user_card_id"],
                    result_data  = infobox_payload,
                    session_id   = entry["session_id"],
                )
                synced += 1
                log.info("Synced buffered result | session=%s", entry["session_id"])
            except Exception as e:
                log.warning("Buffered result still undeliverable | session=%s | %s",
                           entry.get("session_id"), e)
                remaining.append(entry)

        self._buffer.rewrite(remaining)
        if synced:
            try:
                bus.publish(
                    message_type = MessageType.STATUS_UPDATE,
                    payload      = {"event": "OFFLINE_BUFFER_SYNCED", "synced_count": synced,
                                    "remaining_count": len(remaining), "device_id": self._ctx.device_id},
                    priority     = Priority.NORMAL,
                )
            except Exception as e:
                log.error("Could not report buffer sync to bus: %s", e)
        return synced

    # ── Routing per deployment mode ───────────

    def _deliver_local(self, package: ResultPackage) -> None:
        try:
            bus.deliver_to_infobox(
                user_card_id = package.user_card_id,
                result_data  = self._infobox_payload(package),
                session_id   = package.session_id,
            )
            package.delivery_status      = DeliveryStatus.DELIVERED
            package.delivery_destination = "local_infobox"
            package.delivered_at         = _now_iso()
        except Exception as e:
            log.error("Local infobox delivery failed | session=%s | %s", package.session_id, e)
            package.delivery_status = DeliveryStatus.FAILED
            package.error_detail    = str(e)
            self._report_failure(package)

    def _deliver_home(self, package: ResultPackage) -> None:
        connected = self._ctx.wifi_available or self._ctx.ble_available
        if connected and self._ctx.cloud_endpoint:
            try:
                self._cloud.deliver(self._infobox_payload(package), self._ctx.cloud_endpoint)
                bus.deliver_to_infobox(
                    user_card_id = package.user_card_id,
                    result_data  = self._infobox_payload(package),
                    session_id   = package.session_id,
                )
                package.delivery_status      = DeliveryStatus.DELIVERED
                package.delivery_destination = f"cloud:{self._ctx.cloud_endpoint}"
                package.delivered_at         = _now_iso()
                return
            except Exception as e:
                log.warning("HOME cloud delivery failed | session=%s | %s — buffering.",
                           package.session_id, e)
                package.error_detail = str(e)

        if self._ctx.offline_buffer:
            self._buffer.enqueue(package)
            package.delivery_status      = DeliveryStatus.BUFFERED
            package.delivery_destination = "offline_buffer"
            package.buffered_at          = _now_iso()
            self._report_buffered(package)
        else:
            package.delivery_status = DeliveryStatus.FAILED
            self._report_failure(package)

    def _deliver_network(self, package: ResultPackage) -> None:
        if not self._ctx.cloud_endpoint:
            log.error("NETWORK deployment missing cloud_endpoint | device=%s", self._ctx.device_id)
            package.delivery_status = DeliveryStatus.FAILED
            package.error_detail    = "NETWORK mode requires a cloud_endpoint"
            self._report_failure(package)
            return

        try:
            self._cloud.deliver(self._infobox_payload(package), self._ctx.cloud_endpoint)
            bus.deliver_to_infobox(
                user_card_id = package.user_card_id,
                result_data  = self._infobox_payload(package),
                session_id   = package.session_id,
            )
            package.delivery_status      = DeliveryStatus.DELIVERED
            package.delivery_destination = f"cloud:{self._ctx.cloud_endpoint}"
            package.delivered_at         = _now_iso()
            return
        except Exception as e:
            log.warning("NETWORK cloud delivery failed | session=%s | %s — buffering + alerting.",
                       package.session_id, e)
            package.error_detail = str(e)

        if self._ctx.offline_buffer:
            self._buffer.enqueue(package)
            package.delivery_status      = DeliveryStatus.BUFFERED
            package.delivery_destination = "offline_buffer"
            package.buffered_at          = _now_iso()
        else:
            package.delivery_status = DeliveryStatus.FAILED

        # NETWORK mode = clinic/pharmacy, multi-unit, cloud-managed centrally.
        # Connectivity loss there is operationally significant in a way a
        # single HOME device going offline isn't — alert maintenance now
        # rather than waiting for the next sync cycle.
        try:
            bus.send_maintenance_alert(
                alert_type = "NETWORK_CLOUD_UNREACHABLE",
                details    = {"device_id": self._ctx.device_id, "session_id": package.session_id,
                              "error": package.error_detail},
                critical   = True,
            )
        except Exception as e:
            log.error("Could not send maintenance alert to bus: %s", e)

    # ── Bus reporting helpers ─────────────────

    def _report_buffered(self, package: ResultPackage) -> None:
        try:
            bus.publish(
                message_type = MessageType.STATUS_UPDATE,
                payload      = {"event": "RESULT_BUFFERED", "session_id": package.session_id,
                                "pending_count": self._buffer.pending_count(),
                                "device_id": self._ctx.device_id},
                priority     = Priority.NORMAL,
                session_id   = package.session_id,
                user_card_id = package.user_card_id,
            )
        except Exception as e:
            log.error("Could not report buffered status to bus: %s", e)

    def _report_failure(self, package: ResultPackage) -> None:
        try:
            bus.send_maintenance_alert(
                alert_type = "RESULT_DELIVERY_FAILED",
                details    = {"device_id": self._ctx.device_id, "session_id": package.session_id,
                              "error": package.error_detail},
                critical   = True,
            )
        except Exception as e:
            log.error("Could not report delivery failure to bus: %s", e)


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Results Engine — Test Suite ===\n")

    import sys
    sys.path.insert(0, ".")

    from btm_bus import bus as _bus
    from btm_sample import DeploymentContext, DeploymentMode
    from btm_analysis import BTMAnalysisEngine, Sex
    from btm_ai_interpreter import BTMAIInterpreter

    _bus.activate(hw_simulation=True)

    class _FakeCollection:
        session_id             = "sess-results-test"
        user_card_id           = "AID-TEST-9999"
        deployment_mode        = "KIOSK"
        sample_volume_ul       = 52.0
        ready_for_analysis     = True

    engine      = BTMAnalysisEngine(hw_simulation=True)
    interpreter = BTMAIInterpreter(use_default_provider=False)  # deterministic fallback for the test

    contexts = [
        ("KIOSK", DeploymentContext.kiosk("BTM-KIOSK-TEST")),
        ("HOME — offline", DeploymentContext(
            mode=DeploymentMode.HOME, device_id="BTM-HOME-TEST",
            cloud_endpoint="https://api.aidplus.io/infobox",
            wifi_available=False, ble_available=False,
        )),
        ("NETWORK", DeploymentContext.network("BTM-NET-TEST", "https://clinic.aidplus.io/infobox")),
    ]

    for label, ctx in contexts:
        print(f"  [{label}]")
        _FakeCollection.deployment_mode = ctx.mode.value
        report    = engine.analyse(_FakeCollection(), sex=Sex.MALE, age=40)
        narrative = interpreter.interpret(report)

        results_engine = BTMResultsEngine(ctx)
        package = results_engine.deliver(report, narrative)

        print(f"    Delivery status : {package.delivery_status.value}")
        print(f"    Destination     : {package.delivery_destination}")
        print(f"    Kiosk headline  : {package.kiosk_view['headline']}")
        print(f"    Web view keys   : {list(package.web_view.keys())}")
        print()

    # Confirm buffered entry is queryable + syncable (will fail-closed again,
    # since there's still no live cloud endpoint — that's expected here)
    home_ctx = contexts[1][1]
    home_engine = BTMResultsEngine(home_ctx)
    print(f"  Buffered pending count (HOME): {home_engine._buffer.pending_count()}")

    interpreter.shutdown()
    _bus.deactivate()
    print("\n✓ BTM Results Engine test complete\n")
