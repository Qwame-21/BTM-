"""
btm_ui.py — AID PLUS+ BTM Touchscreen Interface
====================================================
Flask/HTML kiosk UI that drives run_btm.py's BTMRuntime through a real
user interaction flow. One physical device serves one person at a
time, so state is a single global KioskSessionState — not per-browser-
session, since there's only ever one active user at the touchscreen.

Screens:
    WELCOME → CARD_SCAN → HAND_SELECTION → COLLECTING → PROCESSING →
    RESULTS → [CONSENT_PROMPT if result_summary.requires_consent_flow]
    → INFOBOX_CONFIRMATION → GOODBYE (→ back to WELCOME)
    (ERROR can be reached from CARD_SCAN or COLLECTING on failure)

CONSENT_PROMPT: a private, single-user screen (the device only ever
serves one person at a time, same assumption the rest of this UI
already makes) shown only when the delivered result's
requires_consent_flow is True — see btm_ai_interpreter.py /
btm_consent.py. Shows the pre-reviewed sensitive_narrative and
captures Yes/Not-now via runtime.submit_consent(), which only exists
for the ~15-minute window run_btm.py caches the narrative for (see
NARRATIVE_CACHE_TTL_S) — submit_consent() degrades gracefully
(consent_outcome with a null referral_code) rather than erroring if
that window has passed.
Universal, non-conditional support resources (a generic "confidential
support is always available" line) are shown on the RESULTS screen to
EVERY patient regardless of result — never only when
requires_consent_flow is True, which would itself leak signal about
what the result was. Do not make that line conditional.

Flow:
    1. Card scan submits a credential — calls runtime.authenticate().
       Approved → HAND_SELECTION. Rejected → stays on CARD_SCAN with
       the failure reason shown.
    2. Hand selection kicks off runtime.complete_session() in a
       background thread (so the Flask request returns immediately) —
       on_collection_status streams fine-grained collection phases,
       on_phase flips the screen through COLLECTING → PROCESSING →
       RESULTS_READY.
    3. The page polls GET /api/state every second and re-renders the
       current screen client-side — no full page reloads, appropriate
       for a touchscreen kiosk.

Card input (simulation mode only): btm_auth.py's AID CARD reader isn't
modelled in btm_hw_interface.py — the physical NFC/QR reader is a
separate hardware concern not yet built. In HW_SIMULATION_MODE, this
screen offers quick-select buttons for the three known simulated
registry cards plus a manual text entry field. When real card-reader
hardware is wired up, that becomes its own subsystem and this screen's
manual input stays as a fallback/support path, not the primary flow.

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional

from flask import Flask, jsonify, request, render_template_string

import config
from btm_auth import AuthMethod, AuthStatus
from btm_sample import Hand, Finger
from btm_consent import ConsentDecision
from run_btm import BTMRuntime

log = logging.getLogger("btm_ui")

GOODBYE_AUTO_RESET_S = 8.0   # how long the Goodbye screen stays up before auto-reset


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class KioskStage(Enum):
    WELCOME              = "WELCOME"
    CARD_SCAN            = "CARD_SCAN"
    HAND_SELECTION       = "HAND_SELECTION"
    COLLECTING           = "COLLECTING"
    PROCESSING           = "PROCESSING"
    RESULTS              = "RESULTS"
    CONSENT_PROMPT       = "CONSENT_PROMPT"
    INFOBOX_CONFIRMATION = "INFOBOX_CONFIRMATION"
    GOODBYE              = "GOODBYE"
    ERROR                = "ERROR"


# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────

@dataclass
class KioskSessionState:
    stage               : KioskStage = KioskStage.WELCOME
    display_name        : Optional[str] = None
    card_id             : Optional[str] = None
    session_id          : Optional[str] = None
    collection_status   : Optional[str] = None
    collection_message  : str = ""
    error_message        : Optional[str] = None
    result_summary        : Optional[Dict] = None
    consent_outcome         : Optional[Dict] = None   # set once submit_consent()
                                                        # has been called for this
                                                        # session — None means
                                                        # "not yet decided"
    updated_at              : str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict:
        return {
            "stage"              : self.stage.value,
            "display_name"       : self.display_name,
            "card_id"            : self.card_id,
            "session_id"         : self.session_id,
            "collection_status"  : self.collection_status,
            "collection_message" : self.collection_message,
            "error_message"      : self.error_message,
            "result_summary"     : self.result_summary,
            "consent_outcome"    : self.consent_outcome,
            "updated_at"         : self.updated_at,
            "simulation_mode"    : config.HW_SIMULATION_MODE,
        }


# ─────────────────────────────────────────────
#  CONTROLLER
# ─────────────────────────────────────────────

class BTMKioskController:
    """
    Bridges the Flask routes to BTMRuntime. Owns the single global
    KioskSessionState and the background thread that runs
    complete_session() so the HTTP layer never blocks on hardware time.
    """

    def __init__(self, runtime: BTMRuntime):
        self._runtime = runtime
        self._state = KioskSessionState()
        self._lock = threading.Lock()
        self._pending_auth = None   # AuthResult between scan_card() and select_hand()
        self._session_thread: Optional[threading.Thread] = None

    def get_state(self) -> Dict:
        with self._lock:
            return self._state.to_dict()

    def _update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self._state, k, v)
            self._state.updated_at = _now_iso()

    def start_card_scan(self) -> Dict:
        self._pending_auth = None
        self._update(stage=KioskStage.CARD_SCAN, error_message=None,
                     display_name=None, card_id=None, session_id=None,
                     result_summary=None, consent_outcome=None,
                     collection_status=None, collection_message="")
        return self.get_state()

    def scan_card(self, credential: str, method: AuthMethod = AuthMethod.AID_CARD) -> Dict:
        auth_result = self._runtime.authenticate(credential, method)

        if auth_result.status == AuthStatus.APPROVED:
            self._pending_auth = auth_result
            self._update(
                stage        = KioskStage.HAND_SELECTION,
                display_name = auth_result.profile.display_name if auth_result.profile else None,
                card_id      = auth_result.card_id,
                session_id   = auth_result.session_id,
                error_message = None,
            )
        else:
            self._pending_auth = None
            self._update(stage=KioskStage.CARD_SCAN, error_message=auth_result.failure_reason)

        return self.get_state()

    def select_hand(self, hand: Hand, finger: Finger) -> Dict:
        if self._pending_auth is None or self._pending_auth.status != AuthStatus.APPROVED:
            self._update(stage=KioskStage.CARD_SCAN,
                         error_message="Your session expired — please scan your card again.")
            return self.get_state()

        auth_result = self._pending_auth
        self._pending_auth = None   # one hand-selection per scan
        self._update(stage=KioskStage.COLLECTING, collection_status=None,
                     collection_message="Starting collection...")

        thread = threading.Thread(
            target=self._run_collection, args=(auth_result, hand, finger),
            daemon=True, name="BTM-UI-Session",
        )
        self._session_thread = thread
        thread.start()
        return self.get_state()

    def confirm_infobox(self) -> Dict:
        self._update(stage=KioskStage.GOODBYE)
        threading.Timer(GOODBYE_AUTO_RESET_S, self.start_card_scan).start()
        return self.get_state()

    def go_to_consent(self) -> Dict:
        """Called when the patient taps 'Done' on RESULTS. If this
        result didn't require the consent flow, there's nothing to
        show — proceed exactly as before rather than stranding the UI
        on an empty screen."""
        with self._lock:
            requires = bool(self._state.result_summary
                            and self._state.result_summary.get("requires_consent_flow"))
        if not requires:
            return self.confirm_infobox()
        self._update(stage=KioskStage.CONSENT_PROMPT, consent_outcome=None)
        return self.get_state()

    def submit_consent(self, decision_str: str) -> Dict:
        """Captures the patient's Yes/Not-now decision. Degrades
        gracefully (a consent_outcome with a null referral_code, not an
        exception) if the runtime's narrative cache has already expired
        for this session — see NARRATIVE_CACHE_TTL_S in run_btm.py."""
        try:
            decision = ConsentDecision(decision_str)
        except ValueError:
            log.warning("submit_consent got invalid decision=%r", decision_str)
            return self.get_state()

        with self._lock:
            session_id = self._state.session_id

        outcome = self._runtime.submit_consent(session_id, decision) if session_id else None

        if outcome is None:
            log.warning("Consent window expired or unknown session=%s — "
                       "degrading gracefully.", session_id)
            self._update(consent_outcome={
                "decision": decision.value, "referral_code": None, "delivery_status": None,
            })
        else:
            self._update(consent_outcome={
                "decision"       : outcome.decision.value,
                "referral_code"  : outcome.referral_code,
                "delivery_status": outcome.delivery_status,
            })
        return self.get_state()

    def go_home(self) -> Dict:
        self._pending_auth = None
        self._update(stage=KioskStage.WELCOME, error_message=None, display_name=None,
                     card_id=None, session_id=None, result_summary=None,
                     consent_outcome=None,
                     collection_status=None, collection_message="")
        return self.get_state()

    def shutdown(self) -> None:
        """
        Graceful teardown. If a session is mid-flight (physical
        collection or the helix-return/bin/hygiene cleanup that follows
        results delivery), wait for it rather than deactivating the bus
        underneath it — a session thread hitting a deactivated bus
        mid-cleanup would silently fail to return used material or log
        its hygiene cycle.
        """
        thread = self._session_thread
        if thread and thread.is_alive():
            log.info("Waiting for in-flight session to finish before shutdown...")
            thread.join(timeout=30)
            if thread.is_alive():
                log.warning("Session thread did not finish within 30s — "
                           "shutting down runtime anyway.")
        self._runtime.shutdown()

    # ── Background session runner ──────────────

    def _run_collection(self, auth_result, hand: Hand, finger: Finger) -> None:
        def on_collection_status(status, message):
            self._update(collection_status=status.value, collection_message=message)

        def on_phase(phase: str):
            if phase == "COLLECTING":
                self._update(stage=KioskStage.COLLECTING)
            elif phase == "PROCESSING":
                self._update(stage=KioskStage.PROCESSING,
                             collection_message="Analysing your results...")
            # RESULTS_READY is handled by on_results below, which carries
            # the actual kiosk_view content — no separate action needed here.

        def on_results(kiosk_view: dict):
            self._update(stage=KioskStage.RESULTS, result_summary=kiosk_view)

        try:
            success = self._runtime.complete_session(
                auth_result, hand, finger,
                on_collection_status=on_collection_status,
                on_phase=on_phase,
                on_results=on_results,
            )
        except Exception as e:
            log.exception("Unhandled error in background session")
            self._update(stage=KioskStage.ERROR,
                         error_message="Something went wrong. Please ask a technician for help.")
            return

        if not success:
            self._update(
                stage=KioskStage.ERROR,
                error_message="We couldn't complete your test. Please try again or "
                              "visit an Aid Plus centre.",
            )


# ─────────────────────────────────────────────
#  FLASK APP
# ─────────────────────────────────────────────

app = Flask(__name__)
_runtime = BTMRuntime()
_kiosk = BTMKioskController(_runtime)

_SIM_CARDS = [
    {"card_id": "AID-A1B2-C3D4-E5F6", "label": "Kwame Asante (PREMIUM)"},
    {"card_id": "AID-G7H8-I9J0-K1L2", "label": "Ama Boateng (STANDARD)"},
    {"card_id": "AID-M3N4-O5P6-Q7R8", "label": "Kofi Mensah (EXPIRED — test rejection)"},
]

_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>AID PLUS+ BTM</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #0d1b2a; color: #e0e6ed; height: 100vh; display: flex;
    align-items: center; justify-content: center;
  }
  .screen {
    width: 100%; max-width: 480px; padding: 32px; text-align: center;
  }
  h1 { font-size: 28px; margin-bottom: 8px; color: #4fd1c5; }
  h2 { font-size: 22px; margin-bottom: 16px; }
  p.sub { color: #9aa8b8; margin-bottom: 32px; font-size: 16px; }
  .btn {
    display: block; width: 100%; padding: 20px; margin: 12px 0;
    font-size: 18px; border: none; border-radius: 12px; cursor: pointer;
    background: #4fd1c5; color: #0d1b2a; font-weight: 600;
  }
  .btn:active { background: #38b2ac; }
  .btn.secondary { background: #1e3a52; color: #e0e6ed; }
  .btn.danger { background: #742a2a; color: #fff; }
  input[type=text] {
    width: 100%; padding: 16px; font-size: 18px; border-radius: 10px;
    border: 2px solid #2d4a63; background: #14273a; color: #e0e6ed;
    margin-bottom: 12px;
  }
  .error-box {
    background: #4a1e1e; border: 1px solid #742a2a; border-radius: 10px;
    padding: 16px; margin-bottom: 20px; color: #ffb4b4; font-size: 15px;
  }
  .spinner {
    width: 56px; height: 56px; border: 5px solid #1e3a52;
    border-top-color: #4fd1c5; border-radius: 50%;
    animation: spin 1s linear infinite; margin: 24px auto;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .status-line { color: #9aa8b8; font-size: 15px; margin-top: 12px; min-height: 20px; }
  .result-card {
    background: #14273a; border-radius: 14px; padding: 24px; margin-bottom: 20px;
  }
  .hand-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
</style>
</head>
<body>
<div class="screen" id="screen"></div>
<script>
async function post(url, body) {
  const res = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
                                 body: JSON.stringify(body || {})});
  return res.json();
}
async function getState() {
  const res = await fetch("/api/state");
  return res.json();
}

function render(state) {
  const el = document.getElementById("screen");
  const stage = state.stage;

  if (stage === "WELCOME") {
    el.innerHTML = `
      <h1>AID PLUS+</h1>
      <h2>Blood Testing Machine</h2>
      <p class="sub">Fast, private blood analysis. Tap below to begin.</p>
      <button class="btn" onclick="beginScan()">Start Test</button>`;

  } else if (stage === "CARD_SCAN") {
    let simCards = "";
    if (state.simulation_mode) {
      simCards = `<p class="sub" style="margin-bottom:8px;">Simulation — quick select:</p>` +
        SIM_CARDS.map(c => `<button class="btn secondary" onclick="scanCard('${c.card_id}')">${c.label}</button>`).join("");
    }
    el.innerHTML = `
      <h2>Scan your AID CARD</h2>
      <p class="sub">Or hold your phone to the reader.</p>
      ${state.error_message ? `<div class="error-box">${state.error_message}</div>` : ""}
      ${simCards}
      <input type="text" id="manualCard" placeholder="AID-XXXX-XXXX-XXXX">
      <button class="btn" onclick="scanCard(document.getElementById('manualCard').value)">Submit Card ID</button>
      <button class="btn secondary" onclick="goHome()">Cancel</button>`;

  } else if (stage === "HAND_SELECTION") {
    el.innerHTML = `
      <h2>Welcome, ${state.display_name || "there"}</h2>
      <p class="sub">Choose which hand and finger you'll use.</p>
      <div class="hand-grid">
        <button class="btn" onclick="selectHand('RIGHT','INDEX')">Right Index</button>
        <button class="btn" onclick="selectHand('LEFT','INDEX')">Left Index</button>
        <button class="btn secondary" onclick="selectHand('RIGHT','MIDDLE')">Right Middle</button>
        <button class="btn secondary" onclick="selectHand('LEFT','MIDDLE')">Left Middle</button>
      </div>`;

  } else if (stage === "COLLECTING") {
    el.innerHTML = `
      <h2>Collecting your sample</h2>
      <div class="spinner"></div>
      <p class="status-line">${state.collection_message || "Please keep your finger still."}</p>`;

  } else if (stage === "PROCESSING") {
    el.innerHTML = `
      <h2>Analysing your results</h2>
      <div class="spinner"></div>
      <p class="status-line">${state.collection_message || "Almost done..."}</p>`;

  } else if (stage === "RESULTS") {
    const s = state.result_summary || {};
    const urgencyColor = {ROUTINE: "#4fd1c5", SOON: "#e8b84f", URGENT: "#e85f4f"}[s.urgency] || "#4fd1c5";
    const followUps = (s.follow_up_actions || []).map(a => `<li>${a}</li>`).join("");
    el.innerHTML = `
      <h2>Test Complete</h2>
      <div class="result-card">
        <p style="font-size:19px; font-weight:600;">${s.headline || "Your results are ready."}</p>
        ${s.chi_score !== undefined ? `<p style="color:${urgencyColor}; font-size:15px;">Health Index: ${s.chi_score}/100 (Grade ${s.chi_grade})</p>` : ""}
        ${s.summary ? `<p class="sub" style="margin:12px 0;">${s.summary}</p>` : ""}
        ${followUps ? `<ul style="text-align:left; color:#9aa8b8; font-size:14px;">${followUps}</ul>` : ""}
        ${s.genotype_note ? `<p style="color:#e8b84f; font-size:14px; margin-top:12px;">${s.genotype_note}</p>` : ""}
        <p style="color:#5a6b7d; font-size:12px; margin-top:16px;">${s.disclaimer || ""}</p>
      </div>
      <p class="sub" style="font-size:13px; margin-bottom:16px;">Need someone to talk to? Confidential support is always available through the Aid Plus app.</p>
      <button class="btn" onclick="${s.requires_consent_flow ? "goToConsent()" : "confirmInfobox()"}">Done</button>`;

  } else if (stage === "CONSENT_PROMPT") {
    const s = state.result_summary || {};
    const outcome = state.consent_outcome;
    if (!outcome) {
      el.innerHTML = `
        <h2>A private note</h2>
        <div class="result-card">
          <p class="sub">${s.sensitive_narrative || ""}</p>
          <p style="margin-top:16px; font-weight:600;">Would you like confidential support?</p>
        </div>
        <button class="btn" onclick="submitConsent('ACCEPTED')">Yes, I'd like support</button>
        <button class="btn secondary" onclick="submitConsent('DECLINED')">Not right now</button>`;
    } else if (outcome.referral_code) {
      el.innerHTML = `
        <h2>Your referral code</h2>
        <div class="result-card">
          <p style="font-size:24px; font-weight:700; letter-spacing:2px;">${outcome.referral_code}</p>
          <p class="sub" style="margin-top:12px;">Present this at Aid Plus Health Centre for confidential follow-up. This code is private to you.</p>
        </div>
        <button class="btn" onclick="confirmInfobox()">Continue</button>`;
    } else if (outcome.decision === "DECLINED") {
      el.innerHTML = `
        <h2>That's okay</h2>
        <p class="sub">Support is always available whenever you're ready — through the Aid Plus app.</p>
        <button class="btn" onclick="confirmInfobox()">Continue</button>`;
    } else {
      el.innerHTML = `
        <h2>We couldn't process that just now</h2>
        <p class="sub">Please ask a technician for help, or check your Infobox shortly — your results are already saved.</p>
        <button class="btn" onclick="confirmInfobox()">Continue</button>`;
    }

  } else if (stage === "INFOBOX_CONFIRMATION") {
    el.innerHTML = `
      <h2>Saved to your Infobox</h2>
      <p class="sub">You can view full results anytime via the Aid Plus app.</p>
      <button class="btn" onclick="confirmInfobox()">Continue</button>`;

  } else if (stage === "GOODBYE") {
    el.innerHTML = `
      <h1>Thank you</h1>
      <p class="sub">Take care of your health. See you next time.</p>`;

  } else if (stage === "ERROR") {
    el.innerHTML = `
      <h2>We hit a problem</h2>
      <div class="error-box">${state.error_message || "Please try again."}</div>
      <button class="btn" onclick="goHome()">Return to Start</button>`;
  }
}

async function beginScan() { render(await post("/api/start_scan")); poll(); }
async function scanCard(cardId) { render(await post("/api/scan_card", {credential: cardId})); }
async function selectHand(hand, finger) { render(await post("/api/select_hand", {hand: hand, finger: finger})); }
async function confirmInfobox() { render(await post("/api/confirm_infobox")); }
async function goToConsent() { render(await post("/api/go_to_consent")); }
async function submitConsent(decision) { render(await post("/api/submit_consent", {decision: decision})); }
async function goHome() { render(await post("/api/go_home")); }

let lastStage = null;
async function poll() {
  const state = await getState();
  if (state.stage !== lastStage || state.stage === "COLLECTING" || state.stage === "PROCESSING") {
    render(state);
    lastStage = state.stage;
  }
  setTimeout(poll, 1000);
}

const SIM_CARDS = {{ sim_cards | tojson }};
poll();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(_PAGE_TEMPLATE, sim_cards=_SIM_CARDS)


@app.route("/api/state", methods=["GET"])
def api_state():
    return jsonify(_kiosk.get_state())


@app.route("/api/start_scan", methods=["POST"])
def api_start_scan():
    return jsonify(_kiosk.start_card_scan())


@app.route("/api/scan_card", methods=["POST"])
def api_scan_card():
    data = request.get_json(force=True) or {}
    credential = (data.get("credential") or "").strip()
    if not credential:
        return jsonify(_kiosk.get_state())
    method_str = data.get("method", "AID_CARD")
    try:
        method = AuthMethod(method_str)
    except ValueError:
        method = AuthMethod.AID_CARD
    return jsonify(_kiosk.scan_card(credential, method))


@app.route("/api/select_hand", methods=["POST"])
def api_select_hand():
    data = request.get_json(force=True) or {}
    try:
        hand = Hand(data.get("hand", "RIGHT"))
        finger = Finger(data.get("finger", "INDEX"))
    except ValueError:
        hand, finger = Hand.RIGHT, Finger.INDEX
    return jsonify(_kiosk.select_hand(hand, finger))


@app.route("/api/confirm_infobox", methods=["POST"])
def api_confirm_infobox():
    return jsonify(_kiosk.confirm_infobox())


@app.route("/api/go_to_consent", methods=["POST"])
def api_go_to_consent():
    return jsonify(_kiosk.go_to_consent())


@app.route("/api/submit_consent", methods=["POST"])
def api_submit_consent():
    data = request.get_json(force=True) or {}
    decision_str = (data.get("decision") or "").strip()
    return jsonify(_kiosk.submit_consent(decision_str))


@app.route("/api/go_home", methods=["POST"])
def api_go_home():
    return jsonify(_kiosk.go_home())


def _handle_signal(signum, frame) -> None:
    log.info("Received signal %s — shutting down.", signum)
    _kiosk.shutdown()
    sys.exit(0)


def main() -> None:
    """Real deployment entry point — starts the BTM runtime then the
    Flask dev server. Production should run this behind a proper WSGI
    server (gunicorn/waitress), not Flask's built-in dev server."""
    if not _runtime.startup():
        log.critical("BTM runtime startup failed — UI cannot serve sessions.")
        raise SystemExit(1)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
    finally:
        _kiosk.shutdown()


if __name__ == "__main__":
    main()
