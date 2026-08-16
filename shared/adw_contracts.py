"""
shared/adw_contracts.py — ADW Service Bus Contracts Registry
==================================================================
Registry of every ADW (Aid Plus device/service bus) contract ID and
variant across Aid Plus products. Identifiers only — no logic. BTM is
the first product registered here; the Aid System kiosk's contract
details will be added when the kiosk is refactored to use this
registry instead of whatever it currently registers internally.

Source of truth for the BTM entry: btm_bus.py's existing
BTM_CONTRACT_ID / ADW_VARIANT constants — extracted here unchanged.
btm_bus.py now imports from here rather than defining its own copies.

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

from typing import Dict, Optional, TypedDict


class ADWContract(TypedDict):
    contract_id : str
    adw_variant : str
    product     : str
    description : str


ADW_CONTRACTS: Dict[str, ADWContract] = {
    "BTM-v1": {
        "contract_id": "BTM-v1",
        "adw_variant": "ADW_VARIANT_BT",
        "product"    : "BTM",
        "description": "AID PLUS+ Blood Testing Machine service bus contract",
    },
    # Aid System kiosk contract to be added here when the kiosk is
    # refactored to use this registry — do not guess its contract_id
    # or adw_variant ahead of that; the kiosk currently has its own
    # internal registration, not yet extracted to shared/.
}


def get_contract(contract_id: str) -> Optional[ADWContract]:
    return ADW_CONTRACTS.get(contract_id)
