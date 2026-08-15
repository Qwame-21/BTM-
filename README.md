# AID PLUS+ BTM — Firmware Stack

This document describes the firmware layer that sits behind
`btm_hw_interface.py`. It is a separate repository/build target from
the Python application layer — nothing here runs on the same process
as `run_btm.py`. The Python side only ever sees it through
`ProductionHardwareBridge`'s compiled extension modules
(`aidplus_btm_hal` for C++, `aidplus_btm_rust` for standalone Rust
modules) — see `_FirmwareLink` in `btm_hw_interface.py`.

**Status: not yet built.** `ProductionHardwareBridge` is fully wired
on the Python side and will raise `FirmwareNotWired` cleanly on every
call until these compiled modules exist and are importable. That's
intentional — it never silently falls back to simulated values.

---

## 1. Why five languages, not one

Each subsystem in the BTM has different real-time, safety, and
development-speed requirements. Using one language everywhere would
mean compromising on at least one of those axes for every subsystem.
The five languages below aren't a wishlist — each is the right tool
for a specific part of the stack, and Rust is treated as a first-class
citizen alongside C, not a future nice-to-have.

| Language | Role | Why this one |
|---|---|---|
| **C** | RTOS firmware core — motor control, timing, interrupts | Most universal for embedded/RTOS work; proven in FDA-cleared medical devices; direct hardware register access with predictable timing |
| **Rust** | Memory-safe sensor modules, suction regulation, real-time pressure loop | No buffer overflows or use-after-free by construction — critical for anything reading live sensor data continuously; growing adoption in medical device firmware for exactly this reason; first-class here, not experimental |
| **C++** | Hardware Abstraction Layer (HAL), pybind11 bridge to Python | Wraps the C/Rust modules into a coherent object-oriented interface; pybind11 is the most mature C++↔Python binding tool, giving `btm_hw_interface.py` a clean, typed surface to call |
| **MicroPython** | Rapid prototyping on microcontroller before firmware is written | Lets a new subsystem (e.g. a redesigned pin mechanism) be prototyped and iterated on hardware in hours instead of days, before committing to a C/Rust implementation |
| **VHDL / Verilog** | Only if the Scanner ends up using an FPGA for optical signal processing | Not committed to yet — the UV/near-IR scanner's signal processing needs are still being evaluated against whether an MCU-based approach (C/Rust) is sufficient before reaching for FPGA-level parallelism |

---

## 2. Subsystem → language mapping

This maps directly to the 11 subsystem contracts already defined in
`btm_hw_interface.py`'s `BTMHardwareBridge`:

| Subsystem (Python contract) | Firmware owner | Language |
|---|---|---|
| `PinController` | Pin strike RTOS module | C (timing-critical actuation) |
| `SuctionRegulator` | Real-time pressure control loop | Rust |
| `ScannerController` | UV/NIR scan sequencing | C, with VHDL under evaluation for the optical signal path |
| `HelixMotorDriver` | Stepper/servo motor control | C |
| `SpinnerController` | Rotation control, slot confirmation | C |
| `HygieneDispenser` | Fluid pump/valve control | C |
| `VentController` | Fan PWM, thermal sensor polling | C |
| `SkinProbeArray` | Deflection/compliance/temp sensor reads | Rust (continuous sensor stream — memory safety matters most here) |
| `PressureSensor` | Suction line pressure ADC read | Rust |
| `FlowSensor` | Fluid flow ADC read | Rust |
| `BinSensor` | Capacity/compartment level sensing | C |

The **C++ HAL** wraps all of the above into the single
`aidplus_btm_hal` pybind11 module. Standalone Rust modules that don't
need to go through the HAL (the continuous sensor-stream ones) are
exposed directly via `aidplus_btm_rust` (cffi).

---

## 3. The Python↔firmware bridge contract

`btm_hw_interface.py`'s `_FirmwareLink` expects exactly two importable
extension modules at runtime:

```
aidplus_btm_hal    — pybind11-compiled C++ HAL
aidplus_btm_rust   — cffi-compiled Rust modules
```

Every `Production*Controller` class calls a specific function name on
whichever module is available (e.g. `pin_strike`, `suction_set_pressure`,
`helix_move_to_position` — see `_call_firmware()` and each
`Production*Controller.__init__` in `btm_hw_interface.py` for the
exact function names expected). Firmware build targets should expose
functions under those exact names — that's the contract, not a
suggestion, since the Python side has zero fallback if a name doesn't
match.

If only one of the two modules is available (e.g. C++ HAL built but
Rust modules not yet ready), `_FirmwareLink.available` is still `True`
and calls route through whichever module responds — but a missing
specific function still raises `FirmwareNotWired` for that call. Build
incrementally; each subsystem coming online is independently visible
to the Python layer without needing the whole firmware stack finished
at once.

---

## 4. Development sequence (recommended, not yet started)

1. **MicroPython prototype** of any subsystem whose mechanical design
   isn't fully locked yet (fingertip stabiliser interaction with pin
   strike is the most likely candidate, given how much that design
   evolved during the Python-side adaptive strike calculator work).
2. **C core** for the timing-critical actuators (pin, helix, spinner,
   vent, hygiene, bin) once mechanical design is locked.
3. **Rust modules** for the continuous sensor-read paths (skin probe,
   pressure, flow) — these benefit most from Rust's safety guarantees
   since they're polled constantly during collection.
4. **C++ HAL + pybind11 bridge** wrapping both, exposing the function
   names `_FirmwareLink` expects.
5. **cffi bridge** for the Rust modules, if kept separate from the C++
   HAL rather than wrapped into it.
6. Flip `HW_SIMULATION_MODE` (via `config.py` / `BTM_HW_SIMULATION` env
   var) to `false` on a test unit and validate every subsystem against
   `btm_hw_interface.py`'s existing contract — no Python-side changes
   should be needed if the firmware honours the function-name contract
   above.
7. **VHDL/FPGA evaluation** for the scanner's optical path, only if
   step 6 shows the MCU-based approach isn't fast/precise enough.

---

## 5. Security & certification notes (carried over from the software layer)

- Firmware should validate all commands from the Python layer defensively
  — the Python side is production code, but firmware is the last line
  of defense against a malformed command reaching an actuator.
- If pursuing IEC 62304 medical device software certification, **Ada**
  becomes relevant for the highest-criticality control loops (this was
  flagged in the original architecture notes but is not yet scoped —
  revisit once certification strategy is decided).
- OTA firmware updates should go through the same HMAC-SHA512 signing
  and rollback-on-failure pattern already implemented for the software
  layer in `btm_ml.py`'s `OTAReceiver` — firmware updates carry
  strictly higher risk than a Python module hot-swap, so this is a
  floor, not a ceiling, for firmware update security.

---

*This document should be updated as soon as firmware development
begins — it currently describes the intended architecture, not a
built system. Update the "Status" line at the top of this file the
moment `aidplus_btm_hal` or `aidplus_btm_rust` first becomes
importable.*
