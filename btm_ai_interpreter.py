"""
btm_ai_interpreter.py — AID PLUS+ BTM AI Health Narrative Engine
==================================================================
Converts a structured AnalysisReport into a personalised, plain-language
health narrative for the patient. This is the patient-facing layer —
raw panel data stays in AnalysisReport; this module produces the human
report that sits alongside it in the Aid Plus Infobox.

Design principles:
    Provider-agnostic  — LLMProvider is an interface. AnthropicProvider
                         is the default implementation, but swapping to
                         another provider means writing one new class,
                         not touching interpretation logic.
    Never blocks        — if the LLM is unreachable, times out, or
                         returns something unparseable, this module
                         falls back to a deterministic template narrative
                         built from the panels' own interpretation text.
                         Result delivery never stalls on the AI layer.
    Bus-integrated       — degradation (API failure, fallback triggered)
                         is reported via ERROR_REPORT on the BTM service
                         bus, so the maintenance/ops layer has visibility,
                         even though the patient still gets a usable
                         narrative immediately.
    Safety rail          — urgency is never taken purely on the model's
                         word. A deterministic minimum urgency is computed
                         from the report itself (critical flags, CKD stage,
                         disease-state haemoglobinopathy, etc.) and the
                         final urgency is max(model_urgency, computed_min).
                         The narrative layer can escalate concern; it can
                         never quietly downgrade it.
    Not diagnostic       — system prompt explicitly forbids diagnosis;
                         always frames findings as information to discuss
                         with a healthcare professional where warranted.

Deployment-context tone:
    KIOSK    — short headline + 2–3 key points, quick read at the machine
    HOME     — full narrative, more explanation of what each panel means
    NETWORK  — kiosk-style brevity (clinician has the raw report already)

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from btm_bus import bus, MessageType, Priority
from btm_analysis import (
    AnalysisReport, HaemoglobinGenotype, CKDStage,
    DiabetesClassification, RiskLevel, AnaemiaType,
)
from config import DEFAULT_MODEL, DEFAULT_TIMEOUT_S, DEFAULT_MAX_TOKENS, ANTHROPIC_API_KEY as _config_api_key

log = logging.getLogger("btm_ai_interpreter")

DISCLAIMER = ("This is an AI-generated summary to help you understand your results. "
              "It is not a medical diagnosis. Please consult a healthcare professional "
              "with any concerns.")


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class Urgency(Enum):
    ROUTINE = "ROUTINE"   # no action needed beyond normal awareness
    SOON    = "SOON"      # follow up with a clinician in the coming weeks
    URGENT  = "URGENT"    # seek care promptly


_URGENCY_ORDER = {Urgency.ROUTINE: 0, Urgency.SOON: 1, Urgency.URGENT: 2}


class NarrativeSource(Enum):
    AI_GENERATED      = "AI_GENERATED"
    TEMPLATE_FALLBACK = "TEMPLATE_FALLBACK"


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class HealthNarrative:
    """Patient-facing AI-generated (or fallback) health report."""
    session_id          : str
    user_card_id        : str
    deployment_mode      : str
    headline             : str
    narrative            : str
    follow_up_actions    : List[str]
    urgency              : Urgency
    source                : NarrativeSource
    model_used            : Optional[str]
    disclaimer            : str = DISCLAIMER
    # Consent-flow fields — set deterministically from the report, never
    # by the AI provider. Whether a patient sees the "would you like
    # confidential support?" prompt must never depend on the LLM
    # reliably mentioning it — same principle as the urgency safety-rail.
    requires_consent_flow : bool = False
    reactive_markers        : List[str] = field(default_factory=list)
    sensitive_narrative       : Optional[str] = None
    generated_at           : str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ─────────────────────────────────────────────
#  PROVIDER INTERFACE
# ─────────────────────────────────────────────

class LLMProvider(ABC):
    """
    Interface for any language model backend. Implementations must
    raise on failure (timeout, auth error, network error, etc.) — the
    interpreter handles fallback, providers should not swallow errors.
    """
    name: str = "base"

    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str,
                 max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        ...


class AnthropicProvider(LLMProvider):
    """
    Default provider — Anthropic Claude API.

    Requires the `anthropic` package and an API key (constructor arg or
    ANTHROPIC_API_KEY env var). If either is missing, the provider stays
    unconfigured and `generate()` raises — the interpreter will fall back
    to the template narrative rather than crash.
    """
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, api_key: Optional[str] = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S):
        self._model     = model
        self._timeout_s = timeout_s
        self._client    = None

        key = api_key or _config_api_key
        if not key:
            log.warning("AnthropicProvider: no API key found — provider unconfigured, "
                        "interpreter will use template fallback.")
            return
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=key)
        except ImportError:
            log.warning("AnthropicProvider: 'anthropic' package not installed — "
                        "provider unconfigured, interpreter will use template fallback.")

    def generate(self, system_prompt: str, user_prompt: str,
                 max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        if self._client is None:
            raise RuntimeError("AnthropicProvider not configured (missing package or API key).")

        response = self._client.messages.create(
            model      = self._model,
            max_tokens = max_tokens,
            system     = system_prompt,
            messages   = [{"role": "user", "content": user_prompt}],
        )
        return "".join(
            block.text for block in response.content
            if getattr(block, "type", "") == "text"
        )


# ─────────────────────────────────────────────
#  URGENCY DETERMINATION (deterministic safety rail)
# ─────────────────────────────────────────────

class UrgencyAssessor:
    """
    Computes a minimum required urgency directly from the AnalysisReport,
    independent of anything the LLM says. Final narrative urgency is never
    allowed to fall below this — see BTMAIInterpreter._resolve_urgency.
    """

    def minimum_urgency(self, report: AnalysisReport) -> Urgency:
        # health_index.critical_count includes reactive transmissible-disease
        # markers (correctly, for wellness-index display) but URGENT here
        # means "seek care promptly / near-emergency" — overstated for a
        # screening result whose real-world guidance is confirmatory testing
        # within days-to-weeks, not today. Isolate that contribution so it
        # gets its own (still real, just correctly-calibrated) SOON handling
        # below, instead of silently inheriting URGENT from the blanket rule.
        transmissible_reactive_count = sum(
            1 for m in report.transmissible_disease.markers if m.reactive
        )
        non_transmissible_critical = report.health_index.critical_count - transmissible_reactive_count

        if non_transmissible_critical > 0:
            return Urgency.URGENT

        if report.kidney.ckd_stage in (CKDStage.G4, CKDStage.G5):
            return Urgency.URGENT
        if report.glucose.classification in (
            DiabetesClassification.DIABETES_LIKELY, DiabetesClassification.HYPOGLYCAEMIA
        ):
            return Urgency.URGENT
        if report.haemoglobinopathy.genotype == HaemoglobinGenotype.SS:
            return Urgency.URGENT
        if report.markers.CRP.value > 10.0:
            return Urgency.URGENT

        soon_conditions = [
            report.kidney.ckd_stage in (CKDStage.G3A, CKDStage.G3B),
            report.glucose.classification == DiabetesClassification.PREDIABETES,
            report.lipid.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL),
            report.liver.hepatic_stress_index >= 0.5,
            report.haemoglobinopathy.is_disease,     # SC, CC, S-beta-thal (SS already caught above)
            report.haemoglobinopathy.is_carrier,      # AS/AC — not urgent, but counselling matters
            report.cbc.anaemia_type != AnaemiaType.NONE,
            report.health_index.grade in ("D", "F"),
            report.transmissible_disease.any_reactive,   # confirmatory testing needed, not an emergency
        ]
        if any(soon_conditions):
            return Urgency.SOON

        return Urgency.ROUTINE

    @staticmethod
    def max_urgency(a: Urgency, b: Urgency) -> Urgency:
        return a if _URGENCY_ORDER[a] >= _URGENCY_ORDER[b] else b


# ─────────────────────────────────────────────
#  TEMPLATE FALLBACK GENERATOR
# ─────────────────────────────────────────────

class TemplateNarrativeGenerator:
    """
    Deterministic, no-LLM narrative built directly from each panel's own
    `.interpretation` text. Used whenever the AI provider is unavailable,
    times out, or returns something that fails validation. Not as fluent
    as the AI narrative, but always available and always accurate to the
    underlying data.
    """

    _FOLLOWUP_KEYWORDS = ("recommend", "referral", "consult", "specialist",
                          "urgent", "follow-up", "monitor", "counselling")

    def build(self, report: AnalysisReport, urgency: Urgency) -> HealthNarrative:
        chi = report.health_index

        headline = self._headline(report, urgency)
        narrative = self._narrative(report)
        follow_ups = self._collect_follow_ups(report)
        reactive_markers = [m.name for m in report.transmissible_disease.markers if m.reactive]

        return HealthNarrative(
            session_id        = report.session_id,
            user_card_id      = report.user_card_id,
            deployment_mode   = report.deployment_mode,
            headline          = headline,
            narrative         = narrative,
            follow_up_actions = follow_ups,
            urgency           = urgency,
            source            = NarrativeSource.TEMPLATE_FALLBACK,
            model_used        = None,
            requires_consent_flow = report.transmissible_disease.any_reactive,
            reactive_markers        = reactive_markers,
            sensitive_narrative        = self.sensitive_narrative(report),
        )

    def sensitive_narrative(self, report: AnalysisReport) -> Optional[str]:
        """
        Deterministic, pre-reviewed copy for a reactive transmissible-
        disease screen — used regardless of AI provider availability
        (see BTMAIInterpreter._generate_with_ai, which calls this same
        method rather than letting the LLM generate this content). This
        text is never mixed into the general narrative — it's rendered
        as its own separate, consent-gated card by the results
        formatter / UI, matching the calm-disclosure design.
        """
        reactive = [m.name for m in report.transmissible_disease.markers if m.reactive]
        if not reactive:
            return None
        if len(reactive) == 1:
            names = reactive[0]
        elif len(reactive) == 2:
            names = f"{reactive[0]} and {reactive[1]}"
        else:
            names = ", ".join(reactive[:-1]) + f", and {reactive[-1]}"
        return (
            f"Your {names} screening came back reactive. This is common, it's "
            f"treatable, and this result is only visible to you right now — no "
            f"one else has seen it. A reactive screen isn't a final diagnosis on "
            f"its own — a confirmatory test and appropriate treatment usually "
            f"resolve it. Many people test positive at some point; it doesn't "
            f"reflect on you."
        )

    def _headline(self, report: AnalysisReport, urgency: Urgency) -> str:
        chi = report.health_index
        if urgency == Urgency.URGENT:
            return f"Your results need prompt attention (Health Index {chi.score:.0f}/100)."
        if urgency == Urgency.SOON:
            return f"Your results are mostly good, with a few things to follow up on (Health Index {chi.score:.0f}/100)."
        return f"Your results look good overall (Health Index {chi.score:.0f}/100, Grade {chi.grade})."

    def _narrative(self, report: AnalysisReport) -> str:
        sections = [
            ("Blood count", report.cbc.interpretation),
            ("Cholesterol", report.lipid.interpretation),
            ("Blood sugar", report.glucose.interpretation),
            ("Liver function", report.liver.interpretation),
            ("Kidney function", report.kidney.interpretation),
            ("Inflammation marker", report.markers.interpretation),
            ("Haemoglobin type", report.haemoglobinopathy.interpretation),
        ]
        lines = [text for _, text in sections if text]
        return " ".join(lines)

    def _collect_follow_ups(self, report: AnalysisReport) -> List[str]:
        texts = [
            report.cbc.interpretation, report.lipid.interpretation,
            report.glucose.interpretation, report.liver.interpretation,
            report.kidney.interpretation, report.markers.interpretation,
            report.haemoglobinopathy.interpretation,
        ]
        actions = []
        for text in texts:
            if not text:
                continue
            for sentence in re.split(r'(?<=[.!?])\s+', text):
                if any(kw in sentence.lower() for kw in self._FOLLOWUP_KEYWORDS):
                    actions.append(sentence.strip())
        if not actions:
            actions.append("No specific follow-up needed — maintain regular check-ups.")
        return actions[:6]


# ─────────────────────────────────────────────
#  AI PROMPT CONSTRUCTION
# ─────────────────────────────────────────────

class PromptBuilder:
    """Builds the system and user prompts sent to the LLM provider."""

    _SYSTEM_PROMPT = """You are the Aid Plus BTM Health Communicator. You turn structured \
blood test results into a short, warm, plain-language summary for a patient who just used a \
BTM blood testing kiosk or home device.

Rules you must always follow:
1. You are a health communicator, NOT a diagnostician. Never state a diagnosis. Describe \
   findings and their general meaning, and point to a healthcare professional for anything \
   that needs clinical judgement.
2. Use plain, everyday language. Avoid medical jargon where possible; briefly explain any \
   term you must use.
3. Be warm and reassuring in tone, but never minimise a finding that needs follow-up.
4. For haemoglobin genotype findings (sickle cell trait/disease, haemoglobin C), be especially \
   careful: state facts plainly and calmly, avoid alarming language, and always mention that \
   genetic counselling or specialist follow-up (as applicable) is a next step to discuss with \
   a health worker — never speculate about severity beyond what the data shows.
5. You will never receive transmissible-disease screening data — that content is deliberately \
   excluded from what you're given and is handled by a separate, pre-reviewed, non-AI-generated \
   pathway. If you notice its absence, that's expected; do not ask about it or reference it.
6. Respond with ONLY a JSON object, no markdown fences, no preamble, matching exactly this \
   schema:
   {
     "headline": "<one short sentence, under 20 words>",
     "narrative": "<the full plain-language summary — length appropriate to the requested tone>",
     "follow_up_actions": ["<short actionable item>", "..."],
     "urgency": "ROUTINE" | "SOON" | "URGENT"
   }
7. Choose urgency conservatively: URGENT for anything critical or requiring prompt care, SOON \
   for things worth a clinician visit in the coming weeks, ROUTINE otherwise."""

    def build(self, report: AnalysisReport, min_urgency: Urgency) -> tuple[str, str]:
        tone_instruction = self._tone_for_deployment(report.deployment_mode)
        summary = self._structured_summary(report)

        user_prompt = (
            f"{tone_instruction}\n\n"
            f"Patient context: {report.sex.value.title()}, age {report.age_years}.\n\n"
            f"Structured results (JSON):\n{summary}\n\n"
            f"Note: our internal safety check has already flagged this result as requiring "
            f"at least {min_urgency.value} urgency — your urgency field must be at least that "
            f"level, but you may escalate higher if the data warrants it.\n\n"
            f"Produce the JSON response now."
        )
        return self._SYSTEM_PROMPT, user_prompt

    def _tone_for_deployment(self, deployment_mode: str) -> str:
        if deployment_mode == "KIOSK":
            return ("Tone: KIOSK. The patient is standing at a public kiosk and will read this "
                    "in a few seconds. Keep the narrative to 2–3 short sentences and 2–3 "
                    "follow-up actions maximum.")
        if deployment_mode == "HOME":
            return ("Tone: HOME. The patient is at home and can read a fuller explanation. "
                    "Narrative may be a short paragraph (4–7 sentences) explaining what the "
                    "flagged panels mean and why they matter.")
        return ("Tone: NETWORK (clinic/pharmacy). Keep the narrative brief and practical, "
                "similar to KIOSK tone — clinical staff have the full raw report separately.")

    def _structured_summary(self, report: AnalysisReport) -> str:
        """Curated, flag-focused summary — keeps the prompt compact and keeps the
        model's attention on what's actually abnormal rather than restating normals.
        Deliberately excludes the transmissible-disease panel entirely (see
        _SYSTEM_PROMPT rule 5) — including its contribution to flags/critical_flags,
        so the counts shown here stay consistent with the panels the model can
        actually see, rather than showing an elevated count it can't explain."""
        chi = report.health_index
        transmissible_reactive_count = sum(
            1 for m in report.transmissible_disease.markers if m.reactive
        )
        summary = {
            "composite_health_index": {
                "score": chi.score, "grade": chi.grade,
                "flags": chi.flags_count - transmissible_reactive_count,
                "critical_flags": chi.critical_count - transmissible_reactive_count,
            },
            "cbc": {"anaemia_type": report.cbc.anaemia_type.value,
                    "infection_index": report.cbc.infection_index,
                    "flagged": self._flags(report.cbc)},
            "lipid": {"risk_level": report.lipid.risk_level.value,
                      "cvd_risk_score": report.lipid.cvd_risk_score,
                      "flagged": self._flags(report.lipid)},
            "glucose": {"classification": report.glucose.classification.value,
                        "estimated_hba1c": report.glucose.estimated_hba1c,
                        "flagged": self._flags(report.glucose)},
            "liver": {"hepatic_stress_index": report.liver.hepatic_stress_index,
                      "flagged": self._flags(report.liver)},
            "kidney": {"egfr": report.kidney.eGFR, "ckd_stage": report.kidney.ckd_stage.value,
                       "flagged": self._flags(report.kidney)},
            "markers": {"flagged": self._flags(report.markers)},
            "haemoglobinopathy": {"genotype": report.haemoglobinopathy.genotype.value,
                                   "is_carrier": report.haemoglobinopathy.is_carrier,
                                   "is_disease": report.haemoglobinopathy.is_disease},
        }
        return json.dumps(summary, indent=2)

    def _flags(self, panel) -> List[Dict]:
        """Pulls out only the abnormal Measurement fields from a panel."""
        out = []
        for attr_name, val in vars(panel).items():
            if hasattr(val, "flag") and hasattr(val, "in_range") and not val.in_range:
                out.append({
                    "measure": attr_name, "value": val.value, "unit": val.unit, "flag": val.flag,
                })
        return out


# ─────────────────────────────────────────────
#  MAIN INTERPRETER
# ─────────────────────────────────────────────

class BTMAIInterpreter:
    """
    AID PLUS+ BTM AI Health Narrative Engine

    Usage:
        interpreter = BTMAIInterpreter()   # uses AnthropicProvider by default
        narrative   = interpreter.interpret(report)

    Swap providers:
        interpreter = BTMAIInterpreter(provider=MyOtherProvider())

    Force fallback-only (e.g. offline testing):
        interpreter = BTMAIInterpreter(provider=None)
    """

    def __init__(self, provider: Optional[LLMProvider] = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 use_default_provider: bool = True):
        if provider is not None:
            self._provider = provider
        elif use_default_provider:
            self._provider = AnthropicProvider(timeout_s=timeout_s)
        else:
            self._provider = None

        self._timeout_s = timeout_s
        self._prompts   = PromptBuilder()
        self._urgency   = UrgencyAssessor()
        self._fallback  = TemplateNarrativeGenerator()
        self._executor  = ThreadPoolExecutor(max_workers=2, thread_name_prefix="btm-ai")

        log.info("BTMAIInterpreter ready | provider=%s",
                 self._provider.name if self._provider else "none (fallback-only)")

    def interpret(self, report: AnalysisReport) -> HealthNarrative:
        """
        Generate a patient-facing health narrative for the given report.
        Always returns a HealthNarrative — never raises. Falls back to a
        template narrative on any AI failure and reports the degradation
        to the BTM service bus.
        """
        min_urgency = self._urgency.minimum_urgency(report)

        if self._provider_available():
            try:
                narrative = self._generate_with_ai(report, min_urgency)
                log.info("AI narrative generated | session=%s | urgency=%s",
                         report.session_id, narrative.urgency.value)
                return narrative
            except Exception as e:
                log.error("AI interpretation failed for session=%s: %s — using fallback.",
                         report.session_id, e)
                self._report_degradation(report, str(e))

        fallback = self._fallback.build(report, min_urgency)
        log.info("Template fallback narrative used | session=%s | urgency=%s",
                 report.session_id, fallback.urgency.value)
        return fallback

    def _provider_available(self) -> bool:
        return self._provider is not None

    def _generate_with_ai(self, report: AnalysisReport, min_urgency: Urgency) -> HealthNarrative:
        system_prompt, user_prompt = self._prompts.build(report, min_urgency)

        future = self._executor.submit(
            self._provider.generate, system_prompt, user_prompt, DEFAULT_MAX_TOKENS
        )
        try:
            raw = future.result(timeout=self._timeout_s)
        except FutureTimeoutError:
            future.cancel()
            raise TimeoutError(f"LLM provider did not respond within {self._timeout_s}s")

        parsed = self._parse_output(raw)
        model_urgency = Urgency(parsed["urgency"])
        final_urgency = self._urgency.max_urgency(min_urgency, model_urgency)
        reactive_markers = [m.name for m in report.transmissible_disease.markers if m.reactive]

        return HealthNarrative(
            session_id        = report.session_id,
            user_card_id      = report.user_card_id,
            deployment_mode   = report.deployment_mode,
            headline          = parsed["headline"],
            narrative         = parsed["narrative"],
            follow_up_actions = parsed["follow_up_actions"],
            urgency           = final_urgency,
            source            = NarrativeSource.AI_GENERATED,
            model_used        = getattr(self._provider, "_model", self._provider.name),
            requires_consent_flow = report.transmissible_disease.any_reactive,
            reactive_markers        = reactive_markers,
            # Always the deterministic template copy, never AI-generated —
            # see TemplateNarrativeGenerator.sensitive_narrative() and
            # _SYSTEM_PROMPT rule 5. This is true even on the AI-generated
            # path — the LLM never sees this panel's data at all.
            sensitive_narrative        = self._fallback.sensitive_narrative(report),
        )

    def _parse_output(self, raw: str) -> Dict:
        cleaned = raw.strip()
        # Defensive: strip markdown fences if the model added them despite instructions
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', cleaned, flags=re.MULTILINE).strip()

        parsed = json.loads(cleaned)   # raises ValueError on bad JSON — triggers fallback

        required = {"headline", "narrative", "follow_up_actions", "urgency"}
        missing = required - parsed.keys()
        if missing:
            raise ValueError(f"LLM output missing required fields: {missing}")
        if parsed["urgency"] not in (u.value for u in Urgency):
            raise ValueError(f"LLM output has invalid urgency: {parsed['urgency']!r}")
        if not isinstance(parsed["follow_up_actions"], list):
            raise ValueError("LLM output follow_up_actions must be a list")

        return parsed

    def _report_degradation(self, report: AnalysisReport, error_detail: str) -> None:
        """Reports AI-layer failure to the BTM bus for ops visibility. Never
        lets a bus problem cascade into an interpreter failure."""
        try:
            bus.publish(
                message_type = MessageType.ERROR_REPORT,
                payload      = {
                    "component"     : "btm_ai_interpreter",
                    "session_id"    : report.session_id,
                    "error"         : error_detail,
                    "fallback_used" : "template_narrative",
                    "provider"      : self._provider.name if self._provider else "none",
                },
                priority     = Priority.NORMAL,
                session_id   = report.session_id,
                user_card_id = report.user_card_id,
            )
        except Exception as bus_err:
            log.error("Could not report AI degradation to bus (bus inactive?): %s", bus_err)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM AI Interpreter — Test Suite ===\n")

    import sys
    sys.path.insert(0, ".")

    from btm_bus import bus as _bus
    from btm_sample import (CollectionResult, CollectionStatus, Hand, Finger)
    from btm_analysis import BTMAnalysisEngine, Sex

    _bus.activate(hw_simulation=True)
    session = _bus.open_session("AID-A1B2-C3D4-E5F6")

    mock_collection = CollectionResult(
        session_id            = session,
        user_card_id          = "AID-A1B2-C3D4-E5F6",
        status                = CollectionStatus.COMPLETE,
        deployment_mode       = "HOME",
        hand                  = Hand.RIGHT,
        finger                = Finger.INDEX,
        scanner               = None,
        skin_profile          = None,
        strike_profile        = None,
        suction                = None,
        sample_volume_ul      = 52.0,
        collection_duration_s = 18.4,
        ready_for_analysis    = True,
    )

    engine = BTMAnalysisEngine(hw_simulation=True)

    # No ANTHROPIC_API_KEY expected in this test environment —
    # interpreter should cleanly fall back to the template narrative.
    interpreter = BTMAIInterpreter()

    for label, sex, age in [("Adult male 38", Sex.MALE, 38), ("Adult female 52", Sex.FEMALE, 52)]:
        print(f"  [{label}]")
        report = engine.analyse(mock_collection, sex=sex, age=age)
        narrative = interpreter.interpret(report)

        print(f"    Source        : {narrative.source.value}")
        print(f"    Urgency       : {narrative.urgency.value}")
        print(f"    Headline      : {narrative.headline}")
        print(f"    Narrative     : {narrative.narrative[:200]}{'...' if len(narrative.narrative) > 200 else ''}")
        print(f"    Follow-ups    : {narrative.follow_up_actions}")
        print(f"    Genotype      : {report.haemoglobinopathy.genotype.value}")
        print()

    interpreter.shutdown()
    _bus.close_session(session)
    _bus.deactivate()
    print("✓ BTM AI Interpreter test complete\n")
