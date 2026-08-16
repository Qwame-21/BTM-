"""
shared/infobox.py — Aid Plus Infobox Delivery Schema
=========================================================
Shared schema for delivering product results to a user's Aid Plus
Infobox. BTM is the first product to formalise this — the Aid System
kiosk's own result delivery will be retrofitted to this schema later
(Roland Adams, confirmed decision, August 2026).

Source of truth for everything in this file: btm_results.py's existing
delivery payload and btm_bus.py's deliver_to_infobox() call shape —
extracted here unchanged, not redesigned. btm_results.py now builds an
InfoboxEntry and delivers its serialised form, rather than constructing
an ad-hoc dict at each call site.

Kiosk retrofit note: the kiosk's actual result format is not yet known
here. When the kiosk adopts this schema, ResultType should gain
kiosk-specific members (e.g. a dispensing-record type) rather than
overloading BLOOD_TEST — do not guess at what those should be named
until the kiosk's actual result concept is in hand.

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class ResultType(Enum):
    BLOOD_TEST = "BLOOD_TEST"      # BTM's current (only) result type
    # Kiosk-specific result types (e.g. a dispensing record) to be added
    # here when the Aid System kiosk is retrofitted to this schema.


class AccessibleVia(Enum):
    """Matches the accessible_via list already hardcoded in
    btm_bus.deliver_to_infobox()."""
    KIOSK      = "kiosk"
    MOBILE_APP = "mobile_app"
    AID_SYSTEM = "aid_system"
    WEB        = "web"


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class InfoboxEntry:
    """
    Canonical Infobox delivery envelope. Any product delivering a
    result to a user's Infobox (BTM today, kiosk on retrofit) should
    produce this exact shape before handing off to the delivery
    mechanism (btm_bus.deliver_to_infobox locally, CloudInfoboxClient
    for HOME/NETWORK).
    """
    user_card_id    : str
    result_type     : ResultType
    contract_id     : str                    # e.g. "BTM-v1" — see shared/adw_contracts.py
    results         : Dict                   # product-specific payload
    accessible_via  : List[AccessibleVia] = field(
        default_factory=lambda: [AccessibleVia.KIOSK, AccessibleVia.MOBILE_APP, AccessibleVia.AID_SYSTEM]
    )
    session_id      : Optional[str] = None
    delivered_at    : str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict:
        """Serialises to the same shape btm_bus.deliver_to_infobox() has
        always sent — this is the existing wire format, not a redesign."""
        return {
            "user_card_id"  : self.user_card_id,
            "result_type"   : self.result_type.value,
            "contract"      : self.contract_id,
            "results"       : self.results,
            "delivered_at"  : self.delivered_at,
            "accessible_via": [v.value for v in self.accessible_via],
            "session_id"    : self.session_id,
        }

    @classmethod
    def for_blood_test(cls, user_card_id: str, results: Dict,
                       session_id: Optional[str] = None,
                       contract_id: str = "BTM-v1") -> "InfoboxEntry":
        """Convenience factory matching BTM's current usage exactly."""
        return cls(
            user_card_id = user_card_id,
            result_type  = ResultType.BLOOD_TEST,
            contract_id  = contract_id,
            results      = results,
            session_id   = session_id,
        )
