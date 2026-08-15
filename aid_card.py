"""
shared/aid_card.py — AID CARD Auth Contract
================================================
Shared AID CARD identity and entitlement contract between Aid Plus
products. BTM is the first product to formalise this — the Aid System
kiosk (Build 29) currently has its own embedded auth logic and will be
retrofitted to this contract when its turn comes (Roland Adams,
confirmed decision, August 2026).

Source of truth for everything in this file: btm_auth.py's existing
regex patterns, EntitlementStatus enum, AIDCardProfile dataclass, and
entitlement-check logic — extracted here unchanged, not redesigned.
btm_auth.py now imports from here rather than defining its own copies.

Kiosk retrofit note: when the kiosk adopts this contract, its own AID
CARD validation/entitlement logic should be reconciled against what's
here — if the kiosk's rules differ, that's a real product discrepancy
to resolve deliberately, not to paper over silently.

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# ─────────────────────────────────────────────
#  FORMAT PATTERNS
# ─────────────────────────────────────────────

CARD_ID_PATTERN     = re.compile(r"^AID-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")
PHONE_TOKEN_PATTERN = re.compile(r"^PHN-[A-Z0-9]{8}-[A-Z0-9]{4}$")


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class EntitlementStatus(Enum):
    ACTIVE      = "ACTIVE"
    EXPIRED     = "EXPIRED"
    SUSPENDED   = "SUSPENDED"
    NOT_FOUND   = "NOT_FOUND"


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class AIDCardProfile:
    """
    Canonical AID CARD user profile shape. Any product reading a user's
    AID CARD (BTM today, kiosk on retrofit, mobile app in future) should
    produce/consume this exact shape.
    """
    card_id         : str
    user_id         : str
    display_name    : str
    membership_tier : str                   # BASIC | STANDARD | PREMIUM
    btm_entitlement : EntitlementStatus      # field name kept from btm_auth.py's
                                              # original — represents general Aid
                                              # Plus membership entitlement, not
                                              # BTM-specific; kiosk retrofit should
                                              # reuse this field as-is rather than
                                              # adding a parallel one
    tests_remaining : Optional[int]         # None = unlimited (PREMIUM)
    last_test_at    : Optional[str]
    registered_at   : str
    fingerprint_hash: Optional[str] = None


# ─────────────────────────────────────────────
#  VALIDATION UTILITIES
# ─────────────────────────────────────────────

def normalise_credential(raw: str) -> str:
    """Whitespace/case normalisation applied before any pattern match."""
    return raw.strip().upper()


def is_valid_card_id(raw: str) -> bool:
    """Format check only — does not verify the card exists or is entitled."""
    return bool(CARD_ID_PATTERN.match(normalise_credential(raw)))


def is_valid_phone_token(raw: str) -> bool:
    """Format check only for a phone NFC/QR token (pre-resolution to a card ID)."""
    return bool(PHONE_TOKEN_PATTERN.match(normalise_credential(raw)))


def check_entitlement(profile: AIDCardProfile) -> Optional[str]:
    """
    Verifies a profile is entitled to use the service. Returns a
    user-facing denial message if not entitled, None if approved.
    Wording is copied verbatim from btm_auth.py's original
    _check_entitlement — this is an extraction, not a rewrite.
    """
    if profile.btm_entitlement == EntitlementStatus.EXPIRED:
        return (
            "Your AID CARD membership has expired. "
            "Please renew at any Aid Plus centre or the Aid Plus app."
        )
    if profile.btm_entitlement == EntitlementStatus.SUSPENDED:
        return (
            "Your account has been suspended. "
            "Please contact Aid Plus support."
        )
    if profile.btm_entitlement == EntitlementStatus.NOT_FOUND:
        return "BTM access not found on your membership. Please upgrade your AID CARD."

    if (
        profile.membership_tier in ("BASIC", "STANDARD")
        and profile.tests_remaining is not None
        and profile.tests_remaining <= 0
    ):
        return (
            "You have used all your available BTM tests for this period. "
            "Please top up via the Aid Plus app or upgrade your membership."
        )

    return None


def mask_card_id(card_id: Optional[str]) -> str:
    """Mask card ID for safe logging — show only last 4 chars."""
    if not card_id:
        return "UNKNOWN"
    return f"****-****-****-{card_id[-4:]}" if len(card_id) > 4 else "****"
