"""
btm_hw_interface.py — AID PLUS+ BTM Hardware Interface
=========================================================
The single contract between all BTM Python modules and the physical
(or simulated) hardware. This is the ONLY simulation file in the BTM
stack — every other module is production code regardless of whether
the device underneath is real or simulated.

Architecture:
    BTMHardwareBridge (ABC)         — the contract every module codes to
    SimulatedHardwareBridge         — HW_SIMULATION_MODE=True
    ProductionHardwareBridge        — HW_SIMULATION_MODE=False, routes to
                                       compiled C++/Rust firmware via a
                                       shared pybind11/cffi link

Usage (every other module):
    from btm_hw_interface import hw_bridge
    hw_bridge.pin.strike(depth_mm=1.8, velocity_ms=0.05)
    hw_bridge.helix.move_to_position(slot_index=3, lift_height_mm=42.0)

Subsystems (11 — one attribute each on the bridge):
    pin, suction, scanner, helix, spinner, hygiene_dispenser,
    vent, skin_probe, pressure_sensor, flow_sensor, bin_sensor

Simulation behaviour:
    Every subsystem returns mechanically/physiologically realistic
    values with sensor-appropriate measurement noise, and carries a
    small (0.5-2%) rare-failure probability per action call — real
    hardware fails occasionally, and every module downstream (btm_helix,
    btm_bin, etc.) needs to be built against that reality, not an
    idealised one.

Firmware layer (documented here, implemented in a separate repo):
    C        — RTOS core: motor control, timing, interrupts (medical-grade)
    Rust     — first-class, not a future consideration: memory-safe
               sensor modules, suction regulation, real-time pressure loop
    C++      — HAL wrapping C/Rust for the pybind11 Python bridge
    MicroPython — microcontroller prototyping ahead of firmware being written
    VHDL/Verilog — if the scanner ends up using an FPGA for optical signal
               processing

    ProductionHardwareBridge expects a compiled extension module
    (pybind11 for the C++ HAL, cffi for standalone Rust modules) to be
    importable at runtime. Until that module exists, every production
    call raises FirmwareNotWired with a clear message — it never
    silently falls back to simulated behaviour.

Author  : Aid Plus Engineering
Version : 1.0.0
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("btm_hw_interface")

# ─────────────────────────────────────────────
#  CONFIG (sourced from config.py)
# ─────────────────────────────────────────────

from config import N_SLOTS, HW_SIMULATION_MODE, HW_FAILURE_PROBABILITY as _FAILURE_PROB


# ─────────────────────────────────────────────
#  EXCEPTIONS
# ─────────────────────────────────────────────

class HardwareFault(RuntimeError):
    """Raised by a subsystem when a simulated or real fault occurs."""
    def __init__(self, component: str, detail: str):
        self.component = component
        self.detail = detail
        super().__init__(f"[{component}] {detail}")


class FirmwareNotWired(RuntimeError):
    """
    Raised by any ProductionHardwareBridge subsystem call when the
    compiled C++/Rust firmware bridge isn't importable. Never falls
    back to simulated values — a missing firmware link on a production
    device is an operational failure, not something to paper over.
    """
    def __init__(self, component: str):
        super().__init__(
            f"[{component}] Firmware bridge not available — compiled "
            f"pybind11/cffi extension not found. Production hardware "
            f"cannot be driven until the firmware bridge is built and "
            f"installed."
        )


def _maybe_fail(component: str) -> None:
    if random.random() < _FAILURE_PROB.get(component, 0.01):
        raise HardwareFault(component, "rare fault triggered during simulated operation")


# ─────────────────────────────────────────────
#  SUBSYSTEM CONTRACTS (ABCs)
# ─────────────────────────────────────────────

class PinController(ABC):
    @abstractmethod
    def strike(self, depth_mm: float, velocity_ms: float) -> bool:
        """Executes the pin strike. Returns True if blood confirmed at wound site."""

    @abstractmethod
    def retract(self) -> bool:
        """Retracts the pin. Returns True on confirmed retraction."""

    @abstractmethod
    def depth_calibrate(self) -> float:
        """Runs zero-point calibration. Returns the calibrated offset in mm."""


class SuctionRegulator(ABC):
    @abstractmethod
    def set_pressure(self, kpa: float) -> bool:
        """Sets suction pressure (negative kPa). Returns True on confirmed set."""

    @abstractmethod
    def read_flow_rate(self) -> float:
        """Returns current flow rate in µL/s."""

    @abstractmethod
    def release(self) -> bool:
        """Releases suction to 0 kPa. Returns True on confirmed release."""


class ScannerController(ABC):
    @abstractmethod
    def run_uv_scan(self) -> float:
        """Runs the UV bacteria scan. Returns contamination index 0.0-1.0."""

    @abstractmethod
    def run_nir_scan(self) -> Tuple[bool, Optional[str]]:
        """Runs the near-IR DNA fingerprint scan. Returns (matched, hash)."""

    @abstractmethod
    def get_bacteria_index(self) -> float:
        """Returns the last cached bacteria index without re-scanning."""


class HelixMotorDriver(ABC):
    @abstractmethod
    def move_to_position(self, slot_index: int, lift_height_mm: float) -> bool:
        """Moves the Helix to the given slot/height. Returns True if the
        move completed; False if a jam was detected mid-move (check
        detect_jam() for details — this is a normal, recoverable outcome,
        not an exception)."""

    @abstractmethod
    def get_position(self) -> Tuple[int, float]:
        """Returns current (slot_index, lift_height_mm)."""

    @abstractmethod
    def home(self) -> bool:
        """Returns the Helix to slot 0 / height 0. Returns True on success."""

    @abstractmethod
    def detect_jam(self) -> bool:
        """Returns True if the Helix is currently in a jammed state."""


class SpinnerController(ABC):
    @abstractmethod
    def rotate_to_slot(self, slot_index: int) -> bool:
        """Rotates the Collective Spinner to the given slot. Returns True on success."""

    @abstractmethod
    def confirm_slot(self) -> bool:
        """Confirms the Spinner is correctly aligned at its target slot."""

    @abstractmethod
    def get_slot_index(self) -> int:
        """Returns the Spinner's current slot index."""


class HygieneDispenser(ABC):
    @abstractmethod
    def dispense_tissue(self, duration_s: float) -> bool:
        """Dispenses and applies wet tissue for the given contact duration."""

    @abstractmethod
    def dispense_disinfectant(self, volume_ml: float) -> bool:
        """Dispenses disinfectant via the Test Bolt. Returns True on success."""

    @abstractmethod
    def dispense_water(self, volume_ml: float) -> bool:
        """Dispenses distilled water via the Test Bolt. Returns True on success."""


class VentController(ABC):
    @abstractmethod
    def set_fan_speed(self, speed: str) -> bool:
        """Sets fan speed to 'LOW' | 'MEDIUM' | 'HIGH'. Returns True on success."""

    @abstractmethod
    def read_temp(self) -> float:
        """Returns internal temperature in °C."""

    @abstractmethod
    def read_humidity(self) -> float:
        """Returns internal relative humidity in %."""


class SkinProbeArray(ABC):
    @abstractmethod
    def read_deflection(self) -> float:
        """Returns tenting displacement under probe pressure, in mm."""

    @abstractmethod
    def read_compliance(self) -> float:
        """Returns skin compliance reading (arbitrary sensor units)."""

    @abstractmethod
    def read_surface_temp(self) -> float:
        """Returns fingertip surface temperature in °C."""


class PressureSensor(ABC):
    @abstractmethod
    def read_pressure_kpa(self) -> float:
        """Returns current suction line pressure in kPa."""


class FlowSensor(ABC):
    @abstractmethod
    def read_flow_ul_per_s(self) -> float:
        """Returns current fluid flow rate in µL/s."""


class BinSensor(ABC):
    @abstractmethod
    def read_capacity_pct(self) -> float:
        """Returns overall Bin capacity used, 0-100%."""

    @abstractmethod
    def read_compartment_levels(self) -> Dict[str, float]:
        """Returns {'new_pct': ..., 'used_pct': ...} compartment fill levels."""


# ─────────────────────────────────────────────
#  SIMULATED IMPLEMENTATIONS
# ─────────────────────────────────────────────

class SimulatedPinController(PinController):
    def strike(self, depth_mm: float, velocity_ms: float) -> bool:
        _maybe_fail("pin")
        time.sleep(velocity_ms)
        # Confirmation probability rises slightly with well-calibrated depth
        confirm_p = 0.97 if 0.8 <= depth_mm <= 2.3 else 0.85
        return random.random() < confirm_p

    def retract(self) -> bool:
        _maybe_fail("pin")
        return True

    def depth_calibrate(self) -> float:
        _maybe_fail("pin")
        return round(random.gauss(0.0, 0.03), 3)


class SimulatedSuctionRegulator(SuctionRegulator):
    def __init__(self):
        self._pressure_kpa = 0.0

    def set_pressure(self, kpa: float) -> bool:
        _maybe_fail("suction")
        self._pressure_kpa = kpa
        return True

    def read_flow_rate(self) -> float:
        if self._pressure_kpa >= -4.0:
            return round(random.uniform(0.0, 1.5), 2)
        return round(random.uniform(3.0, 8.0), 2)

    def release(self) -> bool:
        _maybe_fail("suction")
        self._pressure_kpa = 0.0
        return True


class SimulatedScannerController(ScannerController):
    def __init__(self):
        self._last_bacteria_index = 0.0

    def run_uv_scan(self) -> float:
        _maybe_fail("scanner")
        time.sleep(0.3)
        idx = random.choices(
            [random.uniform(0.0, 0.05), random.uniform(0.05, 0.12), random.uniform(0.12, 0.4)],
            weights=[95, 4, 1],
        )[0]
        self._last_bacteria_index = round(idx, 4)
        return self._last_bacteria_index

    def run_nir_scan(self) -> Tuple[bool, Optional[str]]:
        _maybe_fail("scanner")
        time.sleep(0.5)
        matched = random.random() > 0.02
        return matched, (f"DNA-{random.randint(100000, 999999)}" if matched else None)

    def get_bacteria_index(self) -> float:
        return self._last_bacteria_index


class SimulatedHelixMotorDriver(HelixMotorDriver):
    """
    Maintains real position state — the Helix is a physical mechanical
    path, not a stateless sensor. Jam is a distinct, recoverable outcome
    (returns False) from a HardwareFault (raised exception) — callers
    (btm_helix.py) are expected to check detect_jam() after any False
    return and drive their own retry/escalation logic.
    """
    _JAM_PROBABILITY = 0.02

    def __init__(self, n_slots: int = N_SLOTS):
        self._n_slots = n_slots
        self._slot_index = 0
        self._lift_height_mm = 0.0
        self._jammed = False

    def move_to_position(self, slot_index: int, lift_height_mm: float) -> bool:
        _maybe_fail("helix")
        if not (0 <= slot_index < self._n_slots):
            raise HardwareFault("helix", f"slot_index {slot_index} out of range (0-{self._n_slots - 1})")

        if random.random() < self._JAM_PROBABILITY:
            self._jammed = True
            log.warning("Helix jam detected en route to slot=%d height=%.1fmm", slot_index, lift_height_mm)
            return False

        # Realistic move timing — proportional to distance travelled
        distance = abs(slot_index - self._slot_index) + abs(lift_height_mm - self._lift_height_mm) / 10.0
        time.sleep(min(0.05 * distance, 0.5))

        self._slot_index = slot_index
        self._lift_height_mm = round(lift_height_mm, 2)
        self._jammed = False
        return True

    def get_position(self) -> Tuple[int, float]:
        return self._slot_index, self._lift_height_mm

    def home(self) -> bool:
        _maybe_fail("helix")
        time.sleep(0.3)
        self._slot_index = 0
        self._lift_height_mm = 0.0
        self._jammed = False
        return True

    def detect_jam(self) -> bool:
        return self._jammed


class SimulatedSpinnerController(SpinnerController):
    def __init__(self):
        self._slot_index = 0

    def rotate_to_slot(self, slot_index: int) -> bool:
        _maybe_fail("spinner")
        time.sleep(0.1)
        self._slot_index = slot_index
        return True

    def confirm_slot(self) -> bool:
        _maybe_fail("spinner")
        return True

    def get_slot_index(self) -> int:
        return self._slot_index


class SimulatedHygieneDispenser(HygieneDispenser):
    def dispense_tissue(self, duration_s: float) -> bool:
        _maybe_fail("hygiene")
        time.sleep(duration_s)
        return True

    def dispense_disinfectant(self, volume_ml: float) -> bool:
        _maybe_fail("hygiene")
        time.sleep(0.2 + volume_ml * 0.05)
        return True

    def dispense_water(self, volume_ml: float) -> bool:
        _maybe_fail("hygiene")
        time.sleep(0.2 + volume_ml * 0.05)
        return True


class SimulatedVentController(VentController):
    _SPEEDS = {"LOW": 0.3, "MEDIUM": 0.6, "HIGH": 1.0}

    def __init__(self):
        self._fan_level = 0.3

    def set_fan_speed(self, speed: str) -> bool:
        _maybe_fail("vent")
        if speed not in self._SPEEDS:
            raise HardwareFault("vent", f"invalid fan speed '{speed}'")
        self._fan_level = self._SPEEDS[speed]
        return True

    def read_temp(self) -> float:
        # Higher fan level → slightly cooler internal temp
        base = 24.0 - (self._fan_level * 2.0)
        return round(random.gauss(base, 1.0), 1)

    def read_humidity(self) -> float:
        return round(random.gauss(45.0, 8.0), 1)


class SimulatedSkinProbeArray(SkinProbeArray):
    def read_deflection(self) -> float:
        _maybe_fail("skin_probe")
        return round(random.uniform(0.3, 1.2), 3)

    def read_compliance(self) -> float:
        _maybe_fail("skin_probe")
        return round(random.uniform(1.2, 2.4), 3)

    def read_surface_temp(self) -> float:
        _maybe_fail("skin_probe")
        return round(random.uniform(30.0, 36.0), 2)


class SimulatedPressureSensor(PressureSensor):
    def read_pressure_kpa(self) -> float:
        return round(random.uniform(-15.0, 0.0), 2)


class SimulatedFlowSensor(FlowSensor):
    def read_flow_ul_per_s(self) -> float:
        return round(random.uniform(0.0, 8.0), 2)


class SimulatedBinSensor(BinSensor):
    def __init__(self):
        self._new_pct = round(random.uniform(60.0, 100.0), 1)
        self._used_pct = round(random.uniform(0.0, 25.0), 1)

    def read_capacity_pct(self) -> float:
        _maybe_fail("bin_sensor")
        return round((self._new_pct + self._used_pct) / 2, 1)

    def read_compartment_levels(self) -> Dict[str, float]:
        _maybe_fail("bin_sensor")
        return {"new_pct": self._new_pct, "used_pct": self._used_pct}


# ─────────────────────────────────────────────
#  PRODUCTION FIRMWARE LINK
# ─────────────────────────────────────────────

class _FirmwareLink:
    """
    Lazily imports the compiled firmware bridge exactly once. Never
    raises at import time — a missing firmware module is a normal state
    during development; individual subsystem calls raise FirmwareNotWired
    when actually invoked without a link.
    """

    def __init__(self):
        self.cpp = None    # pybind11 C++ HAL module
        self.rust = None   # cffi Rust module
        self.available = False
        self._try_link()

    def _try_link(self) -> None:
        try:
            import aidplus_btm_hal as _cpp   # pybind11-compiled C++ HAL — not yet built
            self.cpp = _cpp
        except ImportError as e:
            log.error("Firmware link: C++ HAL module 'aidplus_btm_hal' not importable: %s", e)

        try:
            import aidplus_btm_rust as _rust  # cffi-compiled Rust modules — not yet built
            self.rust = _rust
        except ImportError as e:
            log.error("Firmware link: Rust module 'aidplus_btm_rust' not importable: %s", e)

        self.available = self.cpp is not None or self.rust is not None
        if not self.available:
            log.error("Firmware link: no compiled extension found — ProductionHardwareBridge "
                      "is unusable until firmware/ is built and installed.")


def _call_firmware(link: _FirmwareLink, component: str, fn_name: str, *args, **kwargs):
    if not link.available:
        raise FirmwareNotWired(component)
    module = link.cpp if link.cpp is not None else link.rust
    fn = getattr(module, fn_name, None)
    if fn is None:
        raise FirmwareNotWired(component)
    return fn(*args, **kwargs)


# ─────────────────────────────────────────────
#  PRODUCTION IMPLEMENTATIONS
# ─────────────────────────────────────────────

class ProductionPinController(PinController):
    def __init__(self, link: _FirmwareLink): self._link = link
    def strike(self, depth_mm, velocity_ms): return _call_firmware(self._link, "pin", "pin_strike", depth_mm, velocity_ms)
    def retract(self): return _call_firmware(self._link, "pin", "pin_retract")
    def depth_calibrate(self): return _call_firmware(self._link, "pin", "pin_depth_calibrate")


class ProductionSuctionRegulator(SuctionRegulator):
    def __init__(self, link: _FirmwareLink): self._link = link
    def set_pressure(self, kpa): return _call_firmware(self._link, "suction", "suction_set_pressure", kpa)
    def read_flow_rate(self): return _call_firmware(self._link, "suction", "suction_read_flow_rate")
    def release(self): return _call_firmware(self._link, "suction", "suction_release")


class ProductionScannerController(ScannerController):
    def __init__(self, link: _FirmwareLink): self._link = link
    def run_uv_scan(self): return _call_firmware(self._link, "scanner", "scanner_run_uv_scan")
    def run_nir_scan(self): return _call_firmware(self._link, "scanner", "scanner_run_nir_scan")
    def get_bacteria_index(self): return _call_firmware(self._link, "scanner", "scanner_get_bacteria_index")


class ProductionHelixMotorDriver(HelixMotorDriver):
    def __init__(self, link: _FirmwareLink): self._link = link
    def move_to_position(self, slot_index, lift_height_mm): return _call_firmware(self._link, "helix", "helix_move_to_position", slot_index, lift_height_mm)
    def get_position(self): return _call_firmware(self._link, "helix", "helix_get_position")
    def home(self): return _call_firmware(self._link, "helix", "helix_home")
    def detect_jam(self): return _call_firmware(self._link, "helix", "helix_detect_jam")


class ProductionSpinnerController(SpinnerController):
    def __init__(self, link: _FirmwareLink): self._link = link
    def rotate_to_slot(self, slot_index): return _call_firmware(self._link, "spinner", "spinner_rotate_to_slot", slot_index)
    def confirm_slot(self): return _call_firmware(self._link, "spinner", "spinner_confirm_slot")
    def get_slot_index(self): return _call_firmware(self._link, "spinner", "spinner_get_slot_index")


class ProductionHygieneDispenser(HygieneDispenser):
    def __init__(self, link: _FirmwareLink): self._link = link
    def dispense_tissue(self, duration_s): return _call_firmware(self._link, "hygiene", "hygiene_dispense_tissue", duration_s)
    def dispense_disinfectant(self, volume_ml): return _call_firmware(self._link, "hygiene", "hygiene_dispense_disinfectant", volume_ml)
    def dispense_water(self, volume_ml): return _call_firmware(self._link, "hygiene", "hygiene_dispense_water", volume_ml)


class ProductionVentController(VentController):
    def __init__(self, link: _FirmwareLink): self._link = link
    def set_fan_speed(self, speed): return _call_firmware(self._link, "vent", "vent_set_fan_speed", speed)
    def read_temp(self): return _call_firmware(self._link, "vent", "vent_read_temp")
    def read_humidity(self): return _call_firmware(self._link, "vent", "vent_read_humidity")


class ProductionSkinProbeArray(SkinProbeArray):
    def __init__(self, link: _FirmwareLink): self._link = link
    def read_deflection(self): return _call_firmware(self._link, "skin_probe", "skin_read_deflection")
    def read_compliance(self): return _call_firmware(self._link, "skin_probe", "skin_read_compliance")
    def read_surface_temp(self): return _call_firmware(self._link, "skin_probe", "skin_read_surface_temp")


class ProductionPressureSensor(PressureSensor):
    def __init__(self, link: _FirmwareLink): self._link = link
    def read_pressure_kpa(self): return _call_firmware(self._link, "pressure_sensor", "pressure_read_kpa")


class ProductionFlowSensor(FlowSensor):
    def __init__(self, link: _FirmwareLink): self._link = link
    def read_flow_ul_per_s(self): return _call_firmware(self._link, "flow_sensor", "flow_read_ul_per_s")


class ProductionBinSensor(BinSensor):
    def __init__(self, link: _FirmwareLink): self._link = link
    def read_capacity_pct(self): return _call_firmware(self._link, "bin_sensor", "bin_read_capacity_pct")
    def read_compartment_levels(self): return _call_firmware(self._link, "bin_sensor", "bin_read_compartment_levels")


# ─────────────────────────────────────────────
#  HARDWARE BRIDGE (top-level contract)
# ─────────────────────────────────────────────

class BTMHardwareBridge(ABC):
    """
    Every BTM module codes against this — never against Simulated* or
    Production* directly, and never branches on simulation vs production
    itself. Subsystems are plain attributes set by each concrete bridge.
    """
    pin               : PinController
    suction           : SuctionRegulator
    scanner           : ScannerController
    helix             : HelixMotorDriver
    spinner           : SpinnerController
    hygiene_dispenser : HygieneDispenser
    vent              : VentController
    skin_probe        : SkinProbeArray
    pressure_sensor   : PressureSensor
    flow_sensor       : FlowSensor
    bin_sensor        : BinSensor

    @abstractmethod
    def diagnostics(self) -> Dict[str, Any]:
        """Returns a snapshot of bridge mode and subsystem health."""


class SimulatedHardwareBridge(BTMHardwareBridge):
    def __init__(self, n_slots: int = N_SLOTS):
        self.pin               = SimulatedPinController()
        self.suction           = SimulatedSuctionRegulator()
        self.scanner           = SimulatedScannerController()
        self.helix             = SimulatedHelixMotorDriver(n_slots=n_slots)
        self.spinner            = SimulatedSpinnerController()
        self.hygiene_dispenser = SimulatedHygieneDispenser()
        self.vent               = SimulatedVentController()
        self.skin_probe         = SimulatedSkinProbeArray()
        self.pressure_sensor    = SimulatedPressureSensor()
        self.flow_sensor         = SimulatedFlowSensor()
        self.bin_sensor          = SimulatedBinSensor()
        log.info("SimulatedHardwareBridge ready | n_slots=%d", n_slots)

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "mode": "SIMULATION",
            "helix_position": self.helix.get_position(),
            "helix_jammed": self.helix.detect_jam(),
            "bin_levels": self.bin_sensor.read_compartment_levels(),
        }


class ProductionHardwareBridge(BTMHardwareBridge):
    def __init__(self):
        self._link             = _FirmwareLink()
        self.pin               = ProductionPinController(self._link)
        self.suction           = ProductionSuctionRegulator(self._link)
        self.scanner           = ProductionScannerController(self._link)
        self.helix             = ProductionHelixMotorDriver(self._link)
        self.spinner            = ProductionSpinnerController(self._link)
        self.hygiene_dispenser = ProductionHygieneDispenser(self._link)
        self.vent               = ProductionVentController(self._link)
        self.skin_probe         = ProductionSkinProbeArray(self._link)
        self.pressure_sensor    = ProductionPressureSensor(self._link)
        self.flow_sensor         = ProductionFlowSensor(self._link)
        self.bin_sensor          = ProductionBinSensor(self._link)
        log.info("ProductionHardwareBridge ready | firmware_linked=%s", self._link.available)

    def diagnostics(self) -> Dict[str, Any]:
        return {"mode": "PRODUCTION", "firmware_linked": self._link.available}


# ─────────────────────────────────────────────
#  SINGLETON
# ─────────────────────────────────────────────

def get_hardware_bridge(simulation: bool = HW_SIMULATION_MODE) -> BTMHardwareBridge:
    return SimulatedHardwareBridge() if simulation else ProductionHardwareBridge()


hw_bridge: BTMHardwareBridge = get_hardware_bridge()


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Hardware Interface — Test Suite ===\n")
    print(f"  HW_SIMULATION_MODE = {HW_SIMULATION_MODE}")
    print(f"  Bridge type        = {type(hw_bridge).__name__}\n")

    print("  [PinController]")
    print("    calibrate offset :", hw_bridge.pin.depth_calibrate())
    print("    strike (1.8mm)   :", hw_bridge.pin.strike(1.8, 0.05))
    print("    retract          :", hw_bridge.pin.retract())

    print("\n  [SuctionRegulator]")
    hw_bridge.suction.set_pressure(-5.0)
    print("    flow rate        :", hw_bridge.suction.read_flow_rate())
    print("    release          :", hw_bridge.suction.release())

    print("\n  [ScannerController]")
    print("    uv scan          :", hw_bridge.scanner.run_uv_scan())
    print("    nir scan         :", hw_bridge.scanner.run_nir_scan())

    print("\n  [HelixMotorDriver]")
    print("    home             :", hw_bridge.helix.home())
    moved = 0
    jammed = 0
    for slot in range(5):
        ok = hw_bridge.helix.move_to_position(slot, slot * 10.0)
        if ok:
            moved += 1
        elif hw_bridge.helix.detect_jam():
            jammed += 1
    print(f"    moves ok={moved} jammed={jammed} | position={hw_bridge.helix.get_position()}")

    print("\n  [SpinnerController]")
    hw_bridge.spinner.rotate_to_slot(3)
    print("    confirmed        :", hw_bridge.spinner.confirm_slot())
    print("    slot index       :", hw_bridge.spinner.get_slot_index())

    print("\n  [HygieneDispenser]")
    print("    tissue           :", hw_bridge.hygiene_dispenser.dispense_tissue(0.2))
    print("    disinfectant     :", hw_bridge.hygiene_dispenser.dispense_disinfectant(2.0))

    print("\n  [VentController]")
    hw_bridge.vent.set_fan_speed("MEDIUM")
    print("    temp / humidity  :", hw_bridge.vent.read_temp(), "/", hw_bridge.vent.read_humidity())

    print("\n  [SkinProbeArray / PressureSensor / FlowSensor]")
    print("    deflection       :", hw_bridge.skin_probe.read_deflection())
    print("    pressure kPa     :", hw_bridge.pressure_sensor.read_pressure_kpa())
    print("    flow uL/s        :", hw_bridge.flow_sensor.read_flow_ul_per_s())

    print("\n  [BinSensor]")
    print("    capacity pct     :", hw_bridge.bin_sensor.read_capacity_pct())
    print("    compartments     :", hw_bridge.bin_sensor.read_compartment_levels())

    print("\n  [Diagnostics]")
    print("   ", hw_bridge.diagnostics())

    # ── Rare-failure exercise (small n, just proving HardwareFault fires) ──
    faults = 0
    for _ in range(500):
        try:
            hw_bridge.pin.strike(1.8, 0.01)
        except HardwareFault:
            faults += 1
    print(f"\n  HardwareFault triggered {faults}/500 pin strikes (~{faults/500*100:.1f}%, target ~1.5%)")

    # ── Production bridge exercise — confirms clean, explicit failure ──
    print("\n  [ProductionHardwareBridge — expected to be unwired]")
    prod = ProductionHardwareBridge()
    try:
        prod.pin.strike(1.8, 0.05)
        print("    UNEXPECTED: production call succeeded without firmware")
    except FirmwareNotWired as e:
        print(f"    Correctly raised FirmwareNotWired: {e}")

    print("\n✓ BTM Hardware Interface test complete\n")
