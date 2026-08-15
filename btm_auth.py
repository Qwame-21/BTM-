"""
btm_auth.py — AID PLUS+ BTM Authentication Module
===================================================
Handles user validation via AID CARD (swipe/scan) or phone NFC/QR.
Initiates a verified BTM session on the service bus upon success.

Validation Flow:
    1. Card/phone presented at touchscreen
    2. Card ID extracted and format-validated
    3. Identity verified against AidPlusOS user registry
    4. Membership & BTM access entitlement checked
    5. Session opened on BTMServiceBus
    6. User fingerprint/DNA pre-scan clearance issued to btm_sample

Auth Contract  : BTM-v1 / ADW_VARIANT_BT
Author         : Aid Plus Engineering
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, Dict, Callable

from btm_bus import (
    BTMServiceBus,
    BTMMessage,
    MessageType,
    Priority,
    bus,
)
from shared.aid_card import (
    CARD_ID_PATTERN, PHONE_TOKEN_PATTERN,
    EntitlementStatus, AIDCardProfile,
    normalise_credential, is_valid_card_id, is_valid_phone_token,
    check_entitlement as shared_check_entitlement,
    mask_card_id,
)

log = logging.getLogger("btm_auth")


# ─────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────

AUTH_TIMEOUT_SECONDS    = 30        # max wait for card presentation
SESSION_TTL_SECONDS     = 600       # 10 min max session lifetime
MAX_AUTH_ATTEMPTS       = 3         # lockout after N failures
LOCKOUT_DURATION        = 300       # 5 min lockout on max failures
# CARD_ID_PATTERN / PHONE_TOKEN_PATTERN now sourced from shared.aid_card


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class AuthMethod(Enum):
    AID_CARD    = "AID_CARD"
    PHONE_NFC   = "PHONE_NFC"
    PHONE_QR    = "PHONE_QR"


class AuthStatus(Enum):
    PENDING     = "PENDING"
    SCANNING    = "SCANNING"
    VALIDATING  = "VALIDATING"
    APPROVED    = "APPROVED"
    REJECTED    = "REJECTED"
    LOCKED_OUT  = "LOCKED_OUT"
    TIMEOUT     = "TIMEOUT"
    ERROR       = "ERROR"


# EntitlementStatus now sourced from shared.aid_card


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

# AIDCardProfile now sourced from shared.aid_card


@dataclass
class AuthResult:
    """Result envelope returned by BTMAuthManager.authenticate()"""
    status          : AuthStatus
    method          : Optional[AuthMethod]  = None
    session_id      : Optional[str]         = None
    card_id         : Optional[str]         = None
    profile         : Optional[AIDCardProfile] = None
    failure_reason  : Optional[str]         = None
    attempts_left   : Optional[int]         = None
    authenticated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ─────────────────────────────────────────────
#  SIMULATION REGISTRY
# ─────────────────────────────────────────────

_SIMULATED_REGISTRY: Dict[str, Dict] = {
    "AID-A1B2-C3D4-E5F6": {
        "user_id"         : "USR-00001",
        "display_name"    : "Kwame Asante",
        "membership_tier" : "PREMIUM",
        "btm_entitlement" : EntitlementStatus.ACTIVE,
        "tests_remaining" : None,
        "last_test_at"    : None,
        "registered_at"   : "2024-01-15T08:00:00+00:00",
    },
    "AID-G7H8-I9J0-K1L2": {
        "user_id"         : "USR-00002",
        "display_name"    : "Ama Boateng",
        "membership_tier" : "STANDARD",
        "btm_entitlement" : EntitlementStatus.ACTIVE,
        "tests_remaining" : 5,
        "last_test_at"    : "2025-07-20T14:30:00+00:00",
        "registered_at"   : "2024-03-10T09:00:00+00:00",
    },
    "AID-M3N4-O5P6-Q7R8": {
        "user_id"         : "USR-00003",
        "display_name"    : "Kofi Mensah",
        "membership_tier" : "BASIC",
        "btm_entitlement" : EntitlementStatus.EXPIRED,
        "tests_remaining" : 0,
        "last_test_at"    : "2025-01-05T11:00:00+00:00",
        "registered_at"   : "2023-11-01T07:00:00+00:00",
    },
}


# ─────────────────────────────────────────────
#  CORE AUTH MANAGER
# ─────────────────────────────────────────────

class BTMAuthManager:
    """
    AID PLUS+ BTM Authentication Manager

    Validates users via AID CARD swipe/scan or phone NFC/QR,
    checks entitlements, and opens a verified session on the
    BTM service bus.

    Usage:
        auth = BTMAuthManager()
        result = auth.authenticate("AID-A1B2-C3D4-E5F6", AuthMethod.AID_CARD)
        if result.status == AuthStatus.APPROVED:
            # proceed to btm_sample with result.session_id
    """

    def __init__(self, hw_simulation: bool = True):
        self._hw_simulation = hw_simulation
        self._attempt_log   : Dict[str, Dict] = {}   # card_id → attempt tracking
        self._active_session: Optional[str] = None
        self._on_approved   : Optional[Callable[[AuthResult], None]] = None
        self._on_rejected   : Optional[Callable[[AuthResult], None]] = None

        # Subscribe to any external auth commands from AidPlusOS
        bus.subscribe(MessageType.AUTH_REQUEST, self._handle_external_auth_request)
        log.info("BTMAuthManager initialised | simulation=%s", hw_simulation)

    # ── Public API ────────────────────────────

    def authenticate(
        self,
        raw_credential  : str,
        method          : AuthMethod = AuthMethod.AID_CARD,
    ) -> AuthResult:
        """
        Primary authentication entry point.

        Args:
            raw_credential: Card ID string or phone token from scanner
            method:         AuthMethod.AID_CARD | PHONE_NFC | PHONE_QR

        Returns:
            AuthResult with status, session_id on approval, or failure detail
        """
        log.info("Auth attempt | method=%s | credential=%s", method.value, self._mask(raw_credential))

        # ── 1. Lockout check ──────────────────
        lockout = self._check_lockout(raw_credential)
        if lockout:
            return self._publish_result(AuthResult(
                status         = AuthStatus.LOCKED_OUT,
                method         = method,
                card_id        = raw_credential,
                failure_reason = f"Too many failed attempts. Locked for {LOCKOUT_DURATION // 60} minutes.",
                attempts_left  = 0,
            ))

        # ── 2. Format validation ──────────────
        card_id = self._extract_card_id(raw_credential, method)
        if not card_id:
            return self._publish_result(self._fail(
                raw_credential, method,
                "Invalid credential format. Please use your AID CARD or registered phone."
            ))

        # ── 3. Registry lookup ────────────────
        profile = self._lookup_profile(card_id)
        if not profile:
            return self._publish_result(self._fail(
                card_id, method,
                "AID CARD not recognised. Please visit an Aid Plus centre to register."
            ))

        # ── 4. Entitlement check ──────────────
        entitlement_result = self._check_entitlement(profile)
        if entitlement_result:
            return self._publish_result(self._fail(
                card_id, method, entitlement_result, profile=profile
            ))

        # ── 5. Open session ───────────────────
        session_id = bus.open_session(card_id)
        self._active_session = session_id
        self._clear_attempts(card_id)

        result = AuthResult(
            status       = AuthStatus.APPROVED,
            method       = method,
            session_id   = session_id,
            card_id      = card_id,
            profile      = profile,
            attempts_left= MAX_AUTH_ATTEMPTS,
        )

        log.info(
            "Auth APPROVED | user=%s | tier=%s | session=%s",
            profile.display_name, profile.membership_tier, session_id
        )

        if self._on_approved:
            self._on_approved(result)

        return self._publish_result(result)

    def on_approved(self, handler: Callable[[AuthResult], None]) -> None:
        """Register a callback for successful authentication."""
        self._on_approved = handler

    def on_rejected(self, handler: Callable[[AuthResult], None]) -> None:
        """Register a callback for rejected authentication."""
        self._on_rejected = handler

    def end_session(self, session_id: str) -> Optional[Dict]:
        """Close the active session after test completion."""
        data = bus.close_session(session_id)
        if self._active_session == session_id:
            self._active_session = None
        log.info("Session ended | session_id=%s", session_id)
        return data

    # ── Credential Extraction ─────────────────

    def _extract_card_id(self, raw: str, method: AuthMethod) -> Optional[str]:
        """
        Extract and normalise a card ID from the raw credential string.
        Handles AID CARD direct IDs and phone token formats. Format
        checks delegate to shared.aid_card; phone token resolution
        stays here since it's BTM/simulation-specific.
        """
        raw = normalise_credential(raw)

        if method == AuthMethod.AID_CARD:
            if is_valid_card_id(raw):
                return raw

        elif method in (AuthMethod.PHONE_NFC, AuthMethod.PHONE_QR):
            # Phone tokens encode the card ID — decode the mapping
            if is_valid_phone_token(raw):
                return self._resolve_phone_token(raw)
            # Phone may also present the card ID directly (digital card)
            if is_valid_card_id(raw):
                return raw

        return None

    def _resolve_phone_token(self, token: str) -> Optional[str]:
        """
        Resolve a phone NFC/QR token back to its AID CARD ID.
        In production: queries AidPlusOS token registry.
        In simulation: deterministic hash-based resolution.
        """
        if self._hw_simulation:
            # Simulate: map known tokens to known cards for testing
            _token_map = {
                "PHN-AA112233-4455": "AID-A1B2-C3D4-E5F6",
                "PHN-BB223344-5566": "AID-G7H8-I9J0-K1L2",
            }
            return _token_map.get(token)
        # Production: call AidPlusOS token resolver
        raise NotImplementedError("Production token resolver not yet wired.")

    # ── Registry Lookup ───────────────────────

    def _lookup_profile(self, card_id: str) -> Optional[AIDCardProfile]:
        """
        Fetch user profile from AidPlusOS registry.
        In simulation: returns from in-memory registry.
        In production: queries the AidPlusOS user service.
        """
        if self._hw_simulation:
            data = _SIMULATED_REGISTRY.get(card_id)
            if not data:
                return None
            return AIDCardProfile(
                card_id          = card_id,
                user_id          = data["user_id"],
                display_name     = data["display_name"],
                membership_tier  = data["membership_tier"],
                btm_entitlement  = data["btm_entitlement"],
                tests_remaining  = data["tests_remaining"],
                last_test_at     = data["last_test_at"],
                registered_at    = data["registered_at"],
            )
        raise NotImplementedError("Production registry lookup not yet wired.")

    # ── Entitlement Check ─────────────────────

    def _check_entitlement(self, profile: AIDCardProfile) -> Optional[str]:
        """
        Verify the user is entitled to use the BTM.
        Returns an error string if denied, None if approved.
        Delegates to shared.aid_card.check_entitlement — the shared
        version is the single source of truth for this logic.
        """
        return shared_check_entitlement(profile)

    # ── Attempt Tracking ──────────────────────

    def _check_lockout(self, card_id: str) -> bool:
        record = self._attempt_log.get(card_id)
        if not record:
            return False
        if record.get("locked_until") and time.time() < record["locked_until"]:
            return True
        if record.get("locked_until") and time.time() >= record["locked_until"]:
            del self._attempt_log[card_id]
        return False

    def _record_failure(self, card_id: str) -> int:
        record = self._attempt_log.setdefault(card_id, {"attempts": 0})
        record["attempts"] += 1
        remaining = MAX_AUTH_ATTEMPTS - record["attempts"]
        if remaining <= 0:
            record["locked_until"] = time.time() + LOCKOUT_DURATION
            log.warning("Card locked out | card=%s", self._mask(card_id))
        return max(remaining, 0)

    def _clear_attempts(self, card_id: str) -> None:
        self._attempt_log.pop(card_id, None)

    # ── Helpers ───────────────────────────────

    def _fail(
        self,
        card_id : str,
        method  : AuthMethod,
        reason  : str,
        profile : Optional[AIDCardProfile] = None,
    ) -> AuthResult:
        attempts_left = self._record_failure(card_id)
        log.warning("Auth REJECTED | card=%s | reason=%s | attempts_left=%d",
                    self._mask(card_id), reason, attempts_left)
        result = AuthResult(
            status         = AuthStatus.REJECTED,
            method         = method,
            card_id        = card_id,
            profile        = profile,
            failure_reason = reason,
            attempts_left  = attempts_left,
        )
        if self._on_rejected:
            self._on_rejected(result)
        return result

    def _publish_result(self, result: AuthResult) -> AuthResult:
        """Publish auth result onto the service bus."""
        bus.publish(
            message_type = MessageType.AUTH_RESPONSE,
            payload      = {
                "status"        : result.status.value,
                "method"        : result.method.value if result.method else None,
                "session_id"    : result.session_id,
                "card_id"       : self._mask(result.card_id) if result.card_id else None,
                "display_name"  : result.profile.display_name if result.profile else None,
                "membership_tier": result.profile.membership_tier if result.profile else None,
                "failure_reason": result.failure_reason,
            },
            priority     = Priority.HIGH,
            session_id   = result.session_id,
            user_card_id = result.card_id,
        )
        return result

    def _handle_external_auth_request(self, msg: BTMMessage) -> None:
        """Handle auth requests sent from AidPlusOS or external systems."""
        credential = msg.payload.get("credential")
        method_str = msg.payload.get("method", AuthMethod.AID_CARD.value)
        if credential:
            try:
                method = AuthMethod(method_str)
            except ValueError:
                method = AuthMethod.AID_CARD
            self.authenticate(credential, method)

    @staticmethod
    def _mask(card_id: Optional[str]) -> str:
        """Mask card ID for safe logging — delegates to shared.aid_card."""
        return mask_card_id(card_id)


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Auth Module — Test Suite ===\n")

    bus.activate(hw_simulation=True)
    auth = BTMAuthManager(hw_simulation=True)

    auth.on_approved(lambda r: print(f"  ✓ CALLBACK | Approved: {r.profile.display_name} | Session: {r.session_id}"))
    auth.on_rejected(lambda r: print(f"  ✗ CALLBACK | Rejected: {r.failure_reason}"))

    tests = [
        ("Valid PREMIUM card",     "AID-A1B2-C3D4-E5F6", AuthMethod.AID_CARD),
        ("Valid STANDARD card",    "AID-G7H8-I9J0-K1L2", AuthMethod.AID_CARD),
        ("Expired BASIC card",     "AID-M3N4-O5P6-Q7R8", AuthMethod.AID_CARD),
        ("Invalid format",         "BADCARD123",          AuthMethod.AID_CARD),
        ("Unknown card",           "AID-ZZZZ-ZZZZ-ZZZZ", AuthMethod.AID_CARD),
        ("Phone NFC token",        "PHN-AA112233-4455",   AuthMethod.PHONE_NFC),
        ("Phone QR token",         "PHN-BB223344-5566",   AuthMethod.PHONE_QR),
    ]

    for label, credential, method in tests:
        print(f"\n  [{label}]")
        result = auth.authenticate(credential, method)
        print(f"  Status       : {result.status.value}")
        if result.profile:
            print(f"  User         : {result.profile.display_name}")
            print(f"  Tier         : {result.profile.membership_tier}")
            print(f"  Tests left   : {result.profile.tests_remaining or 'Unlimited'}")
        if result.session_id:
            print(f"  Session      : {result.session_id}")
            auth.end_session(result.session_id)
        if result.failure_reason:
            print(f"  Reason       : {result.failure_reason}")
        if result.attempts_left is not None and result.status == AuthStatus.REJECTED:
            print(f"  Attempts left: {result.attempts_left}")

    print("\n✓ BTM Auth module test complete\n")
    bus.deactivate()
