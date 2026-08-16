"""
shared/referral.py — Aid Plus Referral Code Contract
=========================================================
Shared referral-code schema for any product that needs to hand a
patient a verifiable path to further care without exposing what
triggered it. BTM is the first product to need this (consent-gated
transmissible-disease screening); any future product with a similar
"private, consent-gated next step" need should produce/consume this
same shape rather than inventing a parallel one — same precedent as
shared/infobox.py and shared/aid_card.py.

Design constraints this schema is built around (BTM's original
design, generalised for ecosystem-wide reuse):
    - A code is only ever generated at the moment of explicit patient
      consent — never speculatively, never before "yes". See
      ReferralCode.issue(), the only constructor path products should
      use.
    - The code, and which condition(s) it relates to, are only ever
      visible in the patient's own private record (delivered as a
      REFERRAL-type shared/infobox.py entry). This schema does not
      define or imply any public/shared visibility.
    - Facility routing: Aid Plus does not yet operate a facility
      directory or live verification endpoint. Every code currently
      resolves to Aid Plus's own clinic (DEFAULT_FACILITY, confirmed
      with Roland Adams, August 2026 — Aid Plus's own facility, not a
      generic multi-facility network). When a real facility directory
      exists, `facility` becomes a lookup result instead of a
      constant — this schema's shape doesn't need to change for that,
      only how `facility` gets populated.
    - Delivery channel: today every referral is delivered the same way
      any other Infobox entry is (local bus / cloud). `channel` is
      modelled as a field with room for a routed value once products
      have a real cross-product communication path (see AidPlusOS in
      the Aid System / Code-6 codebase, which today only handles boot,
      OTA updates, and power/connectivity — nothing facility- or
      referral-shaped yet). OS_ROUTED is reserved, not wired to
      anything — do not implement OS_ROUTED delivery logic until
      AidPlusOS actually exposes something concrete to route to.

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List

DEFAULT_FACILITY = "Aid Plus Health Centre"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class ReferralChannel(Enum):
    """How this referral reaches wherever it needs to go. Only
    PATIENT_INFOBOX is implemented today — the patient carries the
    code themselves and presents it on request. OS_ROUTED is reserved
    for when a real cross-product routing path exists — its presence
    in this enum is not evidence that routing exists; it doesn't yet."""
    PATIENT_INFOBOX = "PATIENT_INFOBOX"
    OS_ROUTED       = "OS_ROUTED"   # reserved — not wired to anything yet


class ReferralStatus(Enum):
    ISSUED  = "ISSUED"
    EXPIRED = "EXPIRED"


# ─────────────────────────────────────────────
#  CODE GENERATION
# ─────────────────────────────────────────────

class ReferralCodeGenerator:
    """
    Generates short, human-presentable referral codes. Uses `secrets`
    rather than `random` deliberately — unlike the sensor-noise
    simulation elsewhere in this codebase, this is a real credential a
    stranger could try to guess or enumerate, so cryptographic
    randomness matters here.
    """
    # No 0/O/1/I — avoids misread characters when a code is handwritten
    # or read aloud at a facility desk.
    _ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    def generate(self) -> str:
        block = lambda n: "".join(secrets.choice(self._ALPHABET) for _ in range(n))
        return f"AID-REF-{block(4)}-{block(4)}"


# ─────────────────────────────────────────────
#  DATA STRUCTURE
# ─────────────────────────────────────────────

@dataclass
class ReferralCode:
    """
    A single referral code issued to a patient. Construct only via
    issue() — never build one directly with a caller-supplied code.
    """
    code            : str
    user_card_id    : str
    session_id      : str
    markers         : List[str]                 # which reactive marker(s) this is for
    facility        : str = DEFAULT_FACILITY
    channel         : ReferralChannel = ReferralChannel.PATIENT_INFOBOX
    status          : ReferralStatus = ReferralStatus.ISSUED
    issued_at       : str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict:
        return {
            "code"        : self.code,
            "user_card_id": self.user_card_id,
            "session_id"  : self.session_id,
            "markers"     : self.markers,
            "facility"    : self.facility,
            "channel"     : self.channel.value,
            "status"      : self.status.value,
            "issued_at"   : self.issued_at,
        }

    @classmethod
    def issue(cls, user_card_id: str, session_id: str, markers: List[str]) -> "ReferralCode":
        """The only constructor path products should use — always
        generates a fresh code via ReferralCodeGenerator rather than
        letting a caller supply or predict one."""
        return cls(
            code         = ReferralCodeGenerator().generate(),
            user_card_id = user_card_id,
            session_id   = session_id,
            markers      = list(markers),
        )


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== shared/referral.py — Test Suite ===\n")

    gen = ReferralCodeGenerator()
    codes = {gen.generate() for _ in range(2000)}
    print(f"  Generated 2000 codes | unique: {len(codes)} | collisions: {2000 - len(codes)}")
    assert len(codes) == 2000, "Referral codes must not collide at this volume"

    sample = next(iter(codes))
    print(f"  Sample code format: {sample}")
    parts = sample.split("-")
    assert len(parts) == 4 and parts[0] == "AID" and parts[1] == "REF"
    random_blocks = "".join(parts[2:])   # exclude the fixed "AID-REF-" prefix,
                                          # which legitimately contains 'I'
    assert all(c not in random_blocks for c in "01OI"), \
        "Ambiguous characters (0/O/1/I) must never appear in the generated blocks"

    ref = ReferralCode.issue(user_card_id="AID-TEST-9999", session_id="sess-1", markers=["HIV"])
    print(f"\n  Issued: {ref.code} | facility={ref.facility} | channel={ref.channel.value} "
          f"| status={ref.status.value}")
    d = ref.to_dict()
    assert d["code"] == ref.code and d["markers"] == ["HIV"] and d["facility"] == DEFAULT_FACILITY
    assert d["channel"] == "PATIENT_INFOBOX"

    print("\n✓ shared/referral.py test complete\n")
