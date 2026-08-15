# AID PLUS+ BTM — Handoff & Resource Request
**From:** Continuation session (Claude)
**To:** Next agent / session picking this up
**Signed by:** Roland Adams, Founder & CEO, Aid Plus (Ghana)
**Date:** August 2026

---

## 1. What this document is

This is a handoff note for whichever agent or session continues the AID PLUS+ BTM (Blood Testing Machine) build. It states exactly what's built and verified, what's blocking the next phase, and what resources/access are needed to keep moving without guessing at interfaces that already exist elsewhere in the Aid Plus codebase.

The pattern worth internalizing from this build so far: **every time a file was built against a guessed interface instead of real source, it produced a real integration bug that only surfaced once the actual source arrived.** Two concrete examples, both caught and fixed:

- `btm_maintenance.py` was first built against an assumed `get_maintenance_predictions()` dict interface for the ML engine. Once the real `btm_ml.py` arrived, it turned out `MaintenancePredictor.evaluate()` returns a `List[MaintenanceAlert]` with a completely different shape (`MaintenanceUrgency` enum: ROUTINE/SOON/URGENT/IMMEDIATE). The guessed version would have silently returned zero maintenance alerts forever in production — no crash, just quietly useless.
- `run_btm.py`'s session loop originally ignored `transport_new_material()`'s return value. A forced-fault test showed a session could report `success: True` and deliver a full health narrative to a patient even though the Helix never actually delivered a pin. Fixed to abort cleanly on hardware delivery failure.

**The ask of this handoff: please supply the Aid System kiosk's actual source code (or explicit confirmation that no such contract exists yet) before the next phase, rather than let another agent guess at shared interfaces the way earlier sessions had to.**

---

## 2. Full build status — what exists and is verified

All files below are complete, syntax-checked, and have passed their own smoke test (most run inline in this session against real dependent modules, not mocks, once those modules' real source was available).

| File | Status | Core contents |
|---|---|---|
| `btm_bus.py` | ✓ Built (pre-existing) | Service bus, BTM-v1 / ADW_VARIANT_BT, BTMMessage envelope, pub/sub, session mgmt |
| `btm_auth.py` | ✓ Built (pre-existing) | AID CARD / phone NFC/QR auth, entitlement checks, lockout logic |
| `btm_sample.py` | ✓ Built (pre-existing, recreated in outputs this session) | Full collection sequence, adaptive strike/suction algorithms, DeploymentContext |
| `btm_ml.py` | ✓ Built (pre-existing) | Viscosity ensemble, skin learner, anomaly detector, MaintenancePredictor, federated learning, OTA |
| `btm_analysis.py` | ✓ Built + haemoglobinopathy panel added this session | 6 diagnostic panels incl. AA/AS/SS/AC/SC/CC/S-beta-thal genotype screen |
| `btm_ai_interpreter.py` | ✓ Built this session | Provider-agnostic LLM interface, deployment-aware tone, deterministic urgency safety-rail, template fallback |
| `btm_results.py` | ✓ Built this session | Kiosk/phone/web view formatting, cloud delivery, offline buffer |
| `btm_hw_interface.py` | ✓ Built this session | THE only sim/production boundary — 11 subsystem contracts, HardwareFault/FirmwareNotWired |
| `btm_helix.py` | ✓ Built this session | Full state machine, multi-step moves, jam retry + escalation |
| `btm_bin.py` | ✓ Built this session | Consumable inventory, auto new/used separation, threshold alerts |
| `btm_maintenance.py` | ✓ Built this session, fixed against real ML interface | Technician alert dispatch, service records, time-based escalation |
| `btm_hygiene.py` | ✓ Built this session | Pre/post-test/deep/emergency cleaning cycles via Test Bolts |
| `btm_vent.py` | ✓ Built this session | Adaptive fan control, thermal alerting, threaded monitor loop |
| `config.py` | ✓ Built this session | Genuine single source of truth — all 10 prior files refactored to import from it |
| `run_btm.py` | ✓ Built this session | Standalone entry point, full session loop, background loops, graceful shutdown |

**Two real bugs found and fixed via testing (not just written and assumed correct):**
1. Haemoglobinopathy `Measurement` flags were inflating `flags_count`/`critical_count` for any non-AA genotype (AS carriers showed as "critical" purely from structural HbS/HbA fraction differences vs. an AA reference). Fixed to feed weighted panel scores only.
2. `urea` was generated in mg/dL-scale values but checked against an mmol/L reference range, causing near-universal false "critical" kidney flags. Fixed to generate directly in mmol/L, consistent with the existing ×2.8 BUN conversion already in the code.

---

## 3. What's next per the build blueprint

```
shared/
  ├── infobox.py       — shared result-delivery schema (BTM + kiosk + future mobile app)
  ├── aid_card.py       — AID CARD auth contract (BTM + kiosk)
  └── adw_contracts.py  — registry of all ADW service bus contracts

btm_ui.py               — Flask/HTML touchscreen interface, calls run_btm.py's session loop

firmware/README.md      — documents the C/Rust/C++/MicroPython/VHDL firmware stack
```

---

## 4. The specific blocker — resource request

`shared/` is, by definition, shared with the **Aid System kiosk** (`aidplus/` in the existing repo layout). Two of its three files need to match code that already exists and is presumably live:

- **`shared/aid_card.py`** — `btm_auth.py` already has its own AID CARD ID regex (`^AID-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$`), phone token pattern, and entitlement-check logic embedded directly in the file. If the kiosk has its own independent version of this same logic, extracting `shared/aid_card.py` needs to reconcile both, not invent a third version neither system actually uses.
- **`shared/infobox.py`** — `btm_results.py` currently builds its own kiosk/phone/web view dicts inline and calls `bus.deliver_to_infobox()`. If the kiosk has an existing Infobox delivery format (Build 29, 21 Python modules per earlier notes), the shared schema needs to match what the kiosk already writes, or nothing downstream (mobile app, web) will be able to read both products' results uniformly.

**What I need, in order of preference:**

1. **Best:** The kiosk's actual auth module and infobox/result-delivery module source (however many files that spans). I'll extract the real shared contract from both, the same way `btm_auth.py`/`btm_ml.py`'s real source fixed two integration bugs this session.
2. **Acceptable:** Explicit confirmation that the kiosk does *not* yet have a formalized infobox/auth contract of its own — i.e., BTM will be the first product to define `shared/`, and the kiosk will be retrofitted to it later. In that case I can design `shared/` cleanly from BTM's existing needs alone, but Roland should know that's what's happening (a fresh contract, not an extraction of something already live).
3. **If neither is available right now:** I can proceed with `btm_ui.py` and `firmware/README.md` first (both are BTM-only, no kiosk dependency) and come back to `shared/` once kiosk source or explicit confirmation arrives — this avoids blocking all forward progress on one open question.

---

## 5. Standing engineering principles for whoever continues this

These aren't new — they're what's already been holding across this build — but worth restating for continuity:

- **Complete drop-in file replacements only.** No patches or diffs when handing back a file.
- **Verify with real execution, not just syntax checks.** Every file in the table above was actually run against its real dependencies where those existed. Two real bugs were only caught this way.
- **`btm_hw_interface.py` is the only sim/production boundary.** Every other file is production code regardless of whether real hardware is attached.
- **`config.py` is the actual single source of truth.** New constants go there, not as module-level locals.
- **When a module's real interface isn't available, say so and either wait or build the smallest defensible bridge — don't silently assume a shape.** This is exactly what went wrong (twice) and got caught by testing rather than guesswork holding up cleanly.

---

*End of handoff. Ready to continue with `btm_ui.py` / `firmware/README.md` immediately, or with `shared/` as soon as kiosk source (or confirmation there isn't one yet) is available.*
