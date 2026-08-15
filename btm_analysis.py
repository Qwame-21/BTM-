"""
btm_analysis.py — AID PLUS+ BTM Blood Analysis Engine
======================================================
AI-powered multi-panel blood analysis. Processes the collected
sample through six diagnostic panels and produces a comprehensive
health report delivered to the Aid Plus Infobox.

Analysis Panels:
    CBC              — Complete Blood Count with differential indices
    LIPID            — Cholesterol panel + cardiovascular risk score
    GLUCOSE          — Blood sugar + estimated HbA1c + diabetes classification
    LIVER            — Hepatic function panel with stress index
    KIDNEY           — Renal function panel with CKD-EPI 2021 eGFR (race-free)
    HAEMOGLOBINOPATHY— Sickle cell / haemoglobin C genotype screen (AA/AS/SS/AC/SC/CC/S-beta-thal)

AI Layer:
    - Per-panel confidence scoring (sensor accuracy × sample volume)
    - Cross-panel pattern recognition (e.g. anaemia × kidney × liver)
    - Composite Health Index (CHI) 0–100
    - Natural language interpretation per panel
    - Anomaly detection via BTMLocalMLEngine
    - All results routed through btm_ml before infobox delivery

Clinical Standards:
    eGFR        : CKD-EPI 2021 race-free equation (NKF/ASN Task Force)
    Reference   : WHO, CDC, and international laboratory normal ranges
    HbA1c est.  : Nathan et al. conversion formula
    CVD risk    : Simplified 10-year Framingham-derived score
    Haem. screen: Capillary electrophoresis / isoelectric focusing of Hb
                  fractions, confirmed by lateral flow immunoassay —
                  genotype prevalence weighted for the Ghana / West Africa
                  population (AS carrier rate ≈ 25%)

Author  : Aid Plus Engineering
Version : 1.1.0
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from btm_bus import bus, MessageType, Priority
from btm_sample import CollectionResult
from btm_ml import BTMLocalMLEngine

log = logging.getLogger("btm_analysis")


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────

class Sex(Enum):
    MALE    = "MALE"
    FEMALE  = "FEMALE"


class RiskLevel(Enum):
    OPTIMAL     = "OPTIMAL"
    NORMAL      = "NORMAL"
    BORDERLINE  = "BORDERLINE"
    HIGH        = "HIGH"
    CRITICAL    = "CRITICAL"


class CKDStage(Enum):
    G1  = "G1"      # eGFR ≥ 90        — normal/high
    G2  = "G2"      # eGFR 60–89       — mildly decreased
    G3A = "G3A"     # eGFR 45–59       — mild-moderate
    G3B = "G3B"     # eGFR 30–44       — moderate-severe
    G4  = "G4"      # eGFR 15–29       — severely decreased
    G5  = "G5"      # eGFR < 15        — kidney failure


class AnaemiaType(Enum):
    NONE                = "NONE"
    MICROCYTIC          = "MICROCYTIC"          # MCV < 80 — iron deficiency / thalassaemia
    NORMOCYTIC          = "NORMOCYTIC"          # MCV 80–100 — chronic disease / haemolytic
    MACROCYTIC          = "MACROCYTIC"          # MCV > 100 — B12/folate / liver disease


class DiabetesClassification(Enum):
    NORMAL              = "NORMAL"              # glucose < 100 mg/dL fasting
    PREDIABETES         = "PREDIABETES"         # 100–125 mg/dL
    DIABETES_LIKELY     = "DIABETES_LIKELY"     # ≥ 126 mg/dL (confirmation needed)
    HYPOGLYCAEMIA       = "HYPOGLYCAEMIA"       # < 70 mg/dL


class HaemoglobinGenotype(Enum):
    AA          = "AA"           # Normal — no sickle or C trait
    AS          = "AS"           # Sickle cell trait (carrier)
    SS          = "SS"           # Sickle cell disease
    AC          = "AC"           # Haemoglobin C trait (carrier)
    SC          = "SC"           # HbSC disease
    CC          = "CC"           # Haemoglobin C disease
    S_BETA_THAL = "S-BETA-THAL"  # Sickle beta-thalassaemia


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class Measurement:
    """Single measurement with value, unit, reference range, and confidence."""
    value       : float
    unit        : str
    ref_low     : float
    ref_high    : float
    confidence  : float         # 0.0–1.0 sensor + volume accuracy
    flag        : str = ""      # "", "L" (low), "H" (high), "LL" (critical low), "HH" (critical high)

    def __post_init__(self):
        self.flag = self._compute_flag()

    def _compute_flag(self) -> str:
        crit_low  = self.ref_low  * 0.70
        crit_high = self.ref_high * 1.50
        if self.value < crit_low:   return "LL"
        if self.value > crit_high:  return "HH"
        if self.value < self.ref_low:  return "L"
        if self.value > self.ref_high: return "H"
        return ""

    @property
    def in_range(self) -> bool:
        return self.flag == ""

    @property
    def is_critical(self) -> bool:
        return self.flag in ("LL", "HH")


@dataclass
class CBCPanel:
    RBC             : Measurement   # ×10¹²/L — red blood cells
    WBC             : Measurement   # ×10⁹/L  — white blood cells
    hemoglobin      : Measurement   # g/dL
    hematocrit      : Measurement   # %
    MCV             : Measurement   # fL — mean corpuscular volume
    MCH             : Measurement   # pg — mean corpuscular haemoglobin
    MCHC            : Measurement   # g/dL — mean corpuscular Hb concentration
    platelets       : Measurement   # ×10⁹/L
    anaemia_type    : AnaemiaType
    infection_index : float         # 0.0–1.0 — WBC-derived infection probability
    interpretation  : str = ""


@dataclass
class LipidPanel:
    total_cholesterol   : Measurement   # mg/dL
    LDL                 : Measurement   # mg/dL — low-density lipoprotein
    HDL                 : Measurement   # mg/dL — high-density lipoprotein
    triglycerides       : Measurement   # mg/dL
    VLDL                : Measurement   # mg/dL — computed: TG / 5
    non_HDL             : Measurement   # mg/dL — total - HDL
    cholesterol_ratio   : float         # total / HDL (CVD risk ratio)
    cvd_risk_score      : float         # 0.0–100.0 — simplified 10-yr Framingham
    risk_level          : RiskLevel
    interpretation      : str = ""


@dataclass
class GlucosePanel:
    fasting_glucose     : Measurement   # mg/dL
    estimated_hba1c     : float         # % — Nathan et al. formula
    classification      : DiabetesClassification
    insulin_resistance_index : float    # 0.0–1.0 proxy
    interpretation      : str = ""


@dataclass
class LiverPanel:
    ALT                 : Measurement   # U/L — alanine aminotransferase
    AST                 : Measurement   # U/L — aspartate aminotransferase
    ALP                 : Measurement   # U/L — alkaline phosphatase
    bilirubin_total     : Measurement   # mg/dL
    albumin             : Measurement   # g/dL
    AST_ALT_ratio       : float         # > 2.0 suggests alcoholic disease
    hepatic_stress_index: float         # 0.0–1.0 composite
    interpretation      : str = ""


@dataclass
class KidneyPanel:
    creatinine          : Measurement   # mg/dL
    urea                : Measurement   # mmol/L (BUN × 0.357)
    uric_acid           : Measurement   # mg/dL
    eGFR                : float         # mL/min/1.73m² — CKD-EPI 2021
    BUN_creatinine_ratio: float         # 10–20 normal; > 20 prerenal
    ckd_stage           : CKDStage
    interpretation      : str = ""


@dataclass
class DiseaseMarkers:
    CRP                 : Measurement   # mg/L — C-reactive protein (inflammation)
    interpretation      : str = ""


@dataclass
class HaemoglobinopathyPanel:
    """
    Haemoglobin genotype screen — HbA/HbS/HbC/HbF fraction profile
    with derived genotype classification.
    """
    HbA                 : Measurement   # % — normal adult haemoglobin fraction
    HbS                 : Measurement   # % — sickle haemoglobin fraction
    HbC                 : Measurement   # % — haemoglobin C fraction
    HbF                 : Measurement   # % — foetal haemoglobin fraction
    genotype            : HaemoglobinGenotype
    is_carrier          : bool          # trait only, not disease (AS, AC)
    is_disease          : bool          # SS, SC, CC, S-beta-thal
    detection_method    : str           # e.g. "Capillary electrophoresis + lateral flow confirmation"
    interpretation      : str = ""


@dataclass
class ScreeningMarker:
    """Single point-of-care reactive/non-reactive screening result."""
    name                : str      # display name, e.g. "Syphilis (RPR)"
    reactive            : bool
    confidence          : float    # 0.0-1.0, sensor + volume accuracy
    detection_method    : str      # e.g. "Lateral flow immunoassay"


@dataclass
class TransmissibleDiseasePanel:
    """
    Point-of-care transmissible disease screening panel. A reactive
    result here is a SCREEN, not a diagnosis — point-of-care assays are
    tuned for sensitivity (catching true positives) over specificity,
    so confirmatory laboratory testing is standard practice before any
    treatment decision. Every consumer of this panel (interpreter,
    results delivery, UI) must preserve that framing — never present a
    reactive screen as a confirmed diagnosis.

    This panel's contents are especially sensitive — see
    btm_ai_interpreter.py / btm_results.py for the consent-gated
    handling this panel requires before anything about it is shared
    beyond the patient's own private view.
    """
    markers             : List["ScreeningMarker"]
    any_reactive        : bool
    interpretation      : str = ""     # clinical-register summary; the
                                        # patient-facing careful narrative is
                                        # generated separately by the AI
                                        # interpreter, which has its own
                                        # sensitive-content handling


@dataclass
class CompositeHealthIndex:
    """
    Aid Plus Composite Health Index (CHI) — 0 to 100.
    Weighted aggregate of all panel scores.
    100 = all markers optimal. Not a diagnostic score — a wellness indicator.
    """
    score               : float
    grade               : str       # A+, A, B, C, D, F
    panel_scores        : Dict[str, float]
    flags_count         : int       # total abnormal markers
    critical_count      : int       # critical flags
    trend               : str       # "IMPROVING" | "STABLE" | "DECLINING" | "FIRST_TEST"


@dataclass
class AnalysisReport:
    """Complete blood analysis report — all panels + composite index."""
    session_id          : str
    user_card_id        : str
    sex                 : Sex
    age_years           : int
    deployment_mode     : str
    cbc                 : CBCPanel
    lipid               : LipidPanel
    glucose             : GlucosePanel
    liver               : LiverPanel
    kidney              : KidneyPanel
    markers             : DiseaseMarkers
    haemoglobinopathy   : HaemoglobinopathyPanel
    transmissible_disease : TransmissibleDiseasePanel
    health_index        : CompositeHealthIndex
    anomaly_level       : str
    anomaly_detail      : Optional[str]
    analysis_duration_s : float
    sample_volume_ul    : float
    analysed_at         : str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    ready_for_delivery  : bool = False


# ─────────────────────────────────────────────
#  SENSOR SIMULATION LAYER
# ─────────────────────────────────────────────

class BTMSensorSimulator:
    """
    Simulates the BTM optical and electrochemical sensor array.
    Generates physiologically realistic values with measurement noise.

    Sensor array (simulated):
        Haematology   : laser light scattering for cell counting + sizing
        Spectrophoto  : multi-wavelength absorbance for Hb, bilirubin, albumin
        Electrochemical: amperometric for glucose, creatinine, uric acid
        Immunoassay   : lateral flow for CRP, lipid panel
        Electrophoresis: capillary electrophoresis / IEF for Hb fraction typing
    """

    # Measurement accuracy per sensor type (coefficient of variation)
    _CV = {
        "haematology"    : 0.015,   # 1.5% CV — laser cell counter
        "spectrophoto"   : 0.025,   # 2.5% CV — absorbance
        "electrochemical": 0.030,   # 3.0% CV — amperometric
        "immunoassay"    : 0.050,   # 5.0% CV — lateral flow (lower precision)
        "electrophoresis": 0.020,   # 2.0% CV — capillary electrophoresis (high precision)
    }

    def __init__(self, sample_volume_ul: float, seed: Optional[int] = None):
        self._volume    = sample_volume_ul
        self._rng       = random.Random(seed)
        # Volume confidence: < 30µL degrades, 50µL = 1.0
        self._vol_conf  = min(1.0, sample_volume_ul / 50.0)

    def measure(self, true_value: float, sensor_type: str) -> Tuple[float, float]:
        """
        Apply sensor noise and return (measured_value, confidence).
        Confidence is reduced at low sample volumes.
        """
        cv          = self._CV.get(sensor_type, 0.04)
        noise       = self._rng.gauss(0, cv * true_value)
        measured    = max(0.0, true_value + noise)
        confidence  = round(self._vol_conf * (1.0 - cv * 2), 3)
        return round(measured, 3), confidence

    def generate_population_normal(self, sex: Sex, age: int) -> Dict[str, float]:
        """
        Generate a physiologically realistic 'true' blood profile
        drawn from population normal distributions, adjusted for sex and age.
        """
        rng = self._rng
        male = sex == Sex.MALE

        # CBC
        rbc         = rng.gauss(5.0 if male else 4.4, 0.4)
        hemoglobin  = rng.gauss(15.0 if male else 13.2, 1.0)
        hematocrit  = hemoglobin * 3.0 + rng.gauss(0, 1.0)
        MCV         = rng.gauss(90, 5)
        MCH         = rng.gauss(30, 2)
        MCHC        = rng.gauss(33, 1.5)
        WBC         = rng.gauss(7.0, 1.5)
        platelets   = rng.gauss(275, 50)

        # Lipid (age-adjusted — cholesterol rises with age)
        age_factor  = 1.0 + (age - 30) * 0.005
        total_chol  = rng.gauss(185 * age_factor, 25)
        HDL         = rng.gauss(55 if male else 62, 10)
        LDL         = rng.gauss(110 * age_factor, 20)
        TG          = rng.gauss(120, 30)

        # Glucose
        glucose     = rng.gauss(92, 10)

        # Liver
        ALT         = rng.gauss(28 if male else 20, 8)
        AST         = rng.gauss(25, 7)
        ALP         = rng.gauss(70 + age * 0.3, 15)
        bilirubin   = rng.gauss(0.8, 0.25)
        albumin     = rng.gauss(4.2, 0.3)

        # Kidney (eGFR declines ~1 mL/min/yr after 40)
        creatinine  = rng.gauss(0.95 if male else 0.75, 0.15)
        urea        = rng.gauss(5.0, 1.0)   # mmol/L — matches KidneyAnalyser's 2.5-7.1 mmol/L range
        uric_acid   = rng.gauss(5.5 if male else 4.5, 1.0)

        # Markers
        CRP         = rng.gauss(1.5, 1.0)

        return {
            "RBC": max(2.0, rbc), "hemoglobin": max(6.0, hemoglobin),
            "hematocrit": max(18.0, hematocrit), "MCV": max(60.0, MCV),
            "MCH": max(20.0, MCH), "MCHC": max(28.0, MCHC),
            "WBC": max(1.0, WBC), "platelets": max(50.0, platelets),
            "total_cholesterol": max(100.0, total_chol),
            "HDL": max(20.0, HDL), "LDL": max(40.0, LDL),
            "triglycerides": max(40.0, TG),
            "glucose": max(40.0, glucose),
            "ALT": max(5.0, ALT), "AST": max(5.0, AST),
            "ALP": max(20.0, ALP), "bilirubin": max(0.1, bilirubin),
            "albumin": max(2.5, albumin),
            "creatinine": max(0.3, creatinine), "urea": max(1.0, urea),
            "uric_acid": max(1.5, uric_acid),
            "CRP": max(0.1, CRP),
        }

    def generate_haemoglobin_profile(self) -> Dict[str, float]:
        """
        Determines a population-weighted 'true' haemoglobin genotype and
        generates the corresponding HbA/HbS/HbC/HbF fraction profile.

        Genotype weights reflect Ghana / West Africa carrier prevalence:
            AA            68%   — normal
            AS            25%   — sickle cell trait (carrier)
            AC             3%   — haemoglobin C trait (carrier)
            SS             2%   — sickle cell disease
            SC             1%   — HbSC disease
            CC           0.5%   — haemoglobin C disease
            S-beta-thal  0.5%   — sickle beta-thalassaemia
        """
        rng  = self._rng
        roll = rng.random()

        if roll < 0.68:                       # AA
            hbA, hbS, hbC = rng.gauss(97.0, 1.0), 0.0, 0.0
            hbF = rng.gauss(0.4, 0.2)
        elif roll < 0.93:                      # AS
            hbA, hbS, hbC = rng.gauss(58.0, 3.0), rng.gauss(38.0, 3.0), 0.0
            hbF = rng.gauss(0.6, 0.3)
        elif roll < 0.96:                      # AC
            hbA, hbS, hbC = rng.gauss(58.0, 3.0), 0.0, rng.gauss(38.0, 3.0)
            hbF = rng.gauss(0.5, 0.2)
        elif roll < 0.98:                      # SS
            hbA, hbS, hbC = rng.gauss(1.0, 0.5), rng.gauss(88.0, 4.0), 0.0
            hbF = rng.gauss(8.0, 3.0)
        elif roll < 0.99:                      # SC
            hbA, hbS, hbC = 0.0, rng.gauss(47.0, 3.0), rng.gauss(47.0, 3.0)
            hbF = rng.gauss(1.5, 0.5)
        elif roll < 0.995:                     # CC
            hbA, hbS, hbC = rng.gauss(1.0, 0.5), 0.0, rng.gauss(92.0, 3.0)
            hbF = rng.gauss(1.0, 0.5)
        else:                                   # S-beta-thal
            hbA, hbS, hbC = rng.gauss(20.0, 5.0), rng.gauss(65.0, 5.0), 0.0
            hbF = rng.gauss(6.0, 2.0)

        return {
            "HbA": max(0.0, hbA),
            "HbS": max(0.0, hbS),
            "HbC": max(0.0, hbC),
            "HbF": max(0.0, hbF),
        }

    def generate_screening_profile(self) -> Dict[str, bool]:
        """
        Generates simulated point-of-care transmissible disease
        screening reactivity.

        IMPORTANT: the rates below are illustrative placeholders for
        exercising the software pipeline (panel logic, consent flow,
        urgency handling) — they are NOT validated epidemiological
        data for Ghana or anywhere else. Before any production use
        beyond software testing, replace these with real assay
        sensitivity/specificity data and regionally-validated
        prevalence estimates (e.g. from Ghana Health Service / WHO
        published surveillance data) — do not treat these numbers as
        real-world fact.
        """
        rng = self._rng
        return {
            "syphilis_rpr"      : rng.random() < 0.02,   # placeholder rate
            "hiv_screen"        : rng.random() < 0.02,   # placeholder rate
            "hepatitis_b_hbsag" : rng.random() < 0.06,   # placeholder rate
        }


# ─────────────────────────────────────────────
#  CLINICAL CALCULATORS
# ─────────────────────────────────────────────

class ClinicalCalculators:
    """
    Validated clinical formula implementations.
    All equations sourced from peer-reviewed literature.
    """

    @staticmethod
    def egfr_ckd_epi_2021(creatinine_mg_dl: float, age: int, sex: Sex) -> float:
        """
        CKD-EPI 2021 eGFR equation — race-free (NKF/ASN Task Force recommendation).
        Eliminates race coefficient; uses age and sex only.

        Formula:
            eGFR = 142 × min(Scr/κ, 1)^α × max(Scr/κ, 1)^(−1.200) × 0.9938^Age
                   × 1.012 (if female)
        Where:
            κ = 0.7 (female), 0.9 (male)
            α = −0.241 (female), −0.302 (male)

        Reference: Inker LA et al., NEJM 2021; doi:10.1056/NEJMoa2102953
        """
        if sex == Sex.FEMALE:
            kappa, alpha = 0.7, -0.241
            sex_factor   = 1.012
        else:
            kappa, alpha = 0.9, -0.302
            sex_factor   = 1.0

        scr_k  = creatinine_mg_dl / kappa
        egfr   = (142
                  * (min(scr_k, 1.0) ** alpha)
                  * (max(scr_k, 1.0) ** -1.200)
                  * (0.9938 ** age)
                  * sex_factor)
        return round(egfr, 1)

    @staticmethod
    def estimated_hba1c(fasting_glucose_mg_dl: float) -> float:
        """
        Estimated HbA1c from mean plasma glucose.
        Nathan et al. formula (ADA, 2008): eAG(mg/dL) = 28.7 × HbA1c − 46.7
        Inverted: HbA1c = (eAG + 46.7) / 28.7
        Uses fasting glucose as eAG proxy (valid for screening, not diagnostic).
        """
        return round((fasting_glucose_mg_dl + 46.7) / 28.7, 1)

    @staticmethod
    def cvd_risk_score(
        total_chol  : float,
        HDL         : float,
        age         : int,
        sex         : Sex,
        systolic_bp : float = 120.0,
    ) -> float:
        """
        Simplified 10-year cardiovascular risk score (0–100).
        Derived from Framingham Heart Study risk factors.
        Note: This is a screening proxy, not a clinical Framingham calculation.
              Full Framingham requires smoking status and BP treatment history.
        """
        score = 0.0

        # Age contribution
        if age < 40:    score += 0
        elif age < 50:  score += 10
        elif age < 60:  score += 20
        elif age < 70:  score += 30
        else:           score += 40

        # Cholesterol ratio contribution
        ratio = total_chol / max(HDL, 1)
        if ratio < 3.5:     score += 0
        elif ratio < 4.5:   score += 10
        elif ratio < 5.5:   score += 20
        else:               score += 30

        # Sex adjustment (males have higher baseline CVD risk)
        if sex == Sex.MALE:
            score *= 1.15

        # BP contribution (light approximation)
        if systolic_bp >= 140:
            score += 15
        elif systolic_bp >= 130:
            score += 8

        return round(min(100.0, score), 1)

    @staticmethod
    def classify_ckd(egfr: float) -> CKDStage:
        if egfr >= 90:   return CKDStage.G1
        if egfr >= 60:   return CKDStage.G2
        if egfr >= 45:   return CKDStage.G3A
        if egfr >= 30:   return CKDStage.G3B
        if egfr >= 15:   return CKDStage.G4
        return CKDStage.G5

    @staticmethod
    def classify_diabetes(glucose: float) -> DiabetesClassification:
        if glucose < 70:    return DiabetesClassification.HYPOGLYCAEMIA
        if glucose < 100:   return DiabetesClassification.NORMAL
        if glucose < 126:   return DiabetesClassification.PREDIABETES
        return DiabetesClassification.DIABETES_LIKELY

    @staticmethod
    def classify_anaemia(
        hemoglobin  : float,
        MCV         : float,
        sex         : Sex,
    ) -> AnaemiaType:
        hb_low = 13.0 if sex == Sex.MALE else 12.0
        if hemoglobin >= hb_low:
            return AnaemiaType.NONE
        if MCV < 80:    return AnaemiaType.MICROCYTIC
        if MCV > 100:   return AnaemiaType.MACROCYTIC
        return AnaemiaType.NORMOCYTIC

    @staticmethod
    def classify_haemoglobin_genotype(
        hbA: float, hbS: float, hbC: float, hbF: float,
    ) -> HaemoglobinGenotype:
        """
        Classifies haemoglobin genotype from the measured HbA/HbS/HbC/HbF
        fraction pattern (capillary electrophoresis / IEF profile).

        Pattern thresholds:
            CC            — HbC ≥ 70%
            AC            — HbC 25–70%, HbS < 25%
            SC            — HbS ≥ 30% AND HbC ≥ 30%
            SS            — HbS ≥ 70%, HbA < 10%
            S-beta-thal   — HbS ≥ 40%, HbA 10–40% (HbA present but reduced,
                             distinguishing it from SS where HbA is absent)
            AS            — HbS 25–45%, HbA ≥ 50%
            AA            — none of the above (HbA dominant, no HbS/HbC)
        """
        if hbC >= 70.0:
            return HaemoglobinGenotype.CC
        if 25.0 <= hbC < 70.0 and hbS < 25.0:
            return HaemoglobinGenotype.AC
        if hbS >= 30.0 and hbC >= 30.0:
            return HaemoglobinGenotype.SC
        if hbS >= 70.0 and hbA < 10.0:
            return HaemoglobinGenotype.SS
        if hbS >= 40.0 and 10.0 <= hbA < 40.0:
            return HaemoglobinGenotype.S_BETA_THAL
        if 25.0 <= hbS < 45.0 and hbA >= 50.0:
            return HaemoglobinGenotype.AS
        return HaemoglobinGenotype.AA


# ─────────────────────────────────────────────
#  PANEL ANALYSERS
# ─────────────────────────────────────────────

class CBCAnalyser:
    def analyse(self, raw: Dict, sim: BTMSensorSimulator, sex: Sex) -> CBCPanel:
        def m(key, low, high, stype="haematology") -> Measurement:
            val, conf = sim.measure(raw[key], stype)
            return Measurement(val, _UNITS[key], low, high, conf)

        male = sex == Sex.MALE
        rbc         = m("RBC",        4.5 if male else 4.0, 5.5 if male else 5.2)
        wbc         = m("WBC",        4.5, 11.0)
        hgb         = m("hemoglobin", 13.0 if male else 12.0, 17.5 if male else 15.5)
        hct         = m("hematocrit", 40.0 if male else 36.0, 52.0 if male else 46.0)
        mcv         = m("MCV",        80.0, 100.0)
        mch         = m("MCH",        27.0, 33.0)
        mchc        = m("MCHC",       32.0, 36.0)
        plt         = m("platelets",  150.0, 400.0)

        anaemia     = ClinicalCalculators.classify_anaemia(hgb.value, mcv.value, sex)
        inf_index   = self._infection_index(wbc.value)

        return CBCPanel(
            RBC=rbc, WBC=wbc, hemoglobin=hgb, hematocrit=hct,
            MCV=mcv, MCH=mch, MCHC=mchc, platelets=plt,
            anaemia_type=anaemia, infection_index=inf_index,
            interpretation=self._interpret(anaemia, inf_index, wbc.value, plt.value),
        )

    def _infection_index(self, wbc: float) -> float:
        # Elevated WBC → infection probability
        if wbc <= 10.0: return max(0.0, (wbc - 4.5) / 11.0)
        if wbc <= 20.0: return 0.5 + (wbc - 10.0) / 20.0
        return min(1.0, 0.9 + (wbc - 20.0) / 100.0)

    def _interpret(self, anaemia: AnaemiaType, inf_idx: float, wbc: float, plt: float) -> str:
        parts = []
        if anaemia == AnaemiaType.MICROCYTIC:
            parts.append("Microcytic anaemia pattern detected — consider iron studies.")
        elif anaemia == AnaemiaType.MACROCYTIC:
            parts.append("Macrocytic anaemia pattern — B12/folate assessment recommended.")
        elif anaemia == AnaemiaType.NORMOCYTIC:
            parts.append("Normocytic anaemia present — investigate chronic disease or haemolysis.")
        else:
            parts.append("Red cell indices within normal range.")
        if inf_idx > 0.6:
            parts.append(f"Elevated WBC ({wbc:.1f}) — possible infection or inflammatory process.")
        if plt < 150:
            parts.append("Low platelet count — monitor for bleeding tendency.")
        elif plt > 400:
            parts.append("Elevated platelets — reactive thrombocytosis possible.")
        return " ".join(parts) if parts else "CBC within normal parameters."


class LipidAnalyser:
    def analyse(self, raw: Dict, sim: BTMSensorSimulator, age: int, sex: Sex) -> LipidPanel:
        def m(key, low, high) -> Measurement:
            val, conf = sim.measure(raw[key], "immunoassay")
            return Measurement(val, _UNITS[key], low, high, conf)

        tc      = m("total_cholesterol", 0.0, 200.0)
        ldl     = m("LDL",               0.0, 130.0)
        hdl_m   = m("HDL",               40.0 if sex == Sex.MALE else 50.0, 100.0)
        tg      = m("triglycerides",      0.0, 150.0)

        vldl_val, vldl_conf  = sim.measure(raw["triglycerides"] / 5.0, "immunoassay")
        non_hdl_val          = tc.value - hdl_m.value
        non_hdl_conf         = min(tc.confidence, hdl_m.confidence)
        vldl    = Measurement(vldl_val, "mg/dL", 0.0, 30.0, vldl_conf)
        non_hdl = Measurement(non_hdl_val, "mg/dL", 0.0, 160.0, non_hdl_conf)

        ratio   = round(tc.value / max(hdl_m.value, 1), 2)
        cvd     = ClinicalCalculators.cvd_risk_score(tc.value, hdl_m.value, age, sex)
        risk    = self._risk_level(cvd, ldl.value, tc.value)

        return LipidPanel(
            total_cholesterol=tc, LDL=ldl, HDL=hdl_m, triglycerides=tg,
            VLDL=vldl, non_HDL=non_hdl, cholesterol_ratio=ratio,
            cvd_risk_score=cvd, risk_level=risk,
            interpretation=self._interpret(risk, ldl.value, hdl_m.value, tg.value, ratio),
        )

    def _risk_level(self, cvd: float, ldl: float, tc: float) -> RiskLevel:
        if cvd < 10 and ldl < 100 and tc < 180: return RiskLevel.OPTIMAL
        if cvd < 20 and ldl < 130:              return RiskLevel.NORMAL
        if cvd < 30 or ldl < 160:               return RiskLevel.BORDERLINE
        if cvd < 50:                             return RiskLevel.HIGH
        return RiskLevel.CRITICAL

    def _interpret(self, risk: RiskLevel, ldl: float, hdl: float, tg: float, ratio: float) -> str:
        parts = []
        if ldl > 160:   parts.append("LDL significantly elevated — dietary review recommended.")
        elif ldl > 130: parts.append("LDL borderline high — consider lifestyle modification.")
        if hdl < 40:    parts.append("Low HDL ('good' cholesterol) — increases cardiovascular risk.")
        if tg > 200:    parts.append("High triglycerides — limit refined carbohydrates and alcohol.")
        if ratio > 5.0: parts.append(f"Cholesterol ratio {ratio:.1f} elevated — review with a clinician.")
        if risk == RiskLevel.OPTIMAL: parts.append("Lipid profile is optimal.")
        return " ".join(parts) if parts else "Lipid profile within acceptable range."


class GlucoseAnalyser:
    def analyse(self, raw: Dict, sim: BTMSensorSimulator) -> GlucosePanel:
        val, conf   = sim.measure(raw["glucose"], "electrochemical")
        glucose     = Measurement(val, "mg/dL", 70.0, 100.0, conf)
        hba1c       = ClinicalCalculators.estimated_hba1c(glucose.value)
        dx          = ClinicalCalculators.classify_diabetes(glucose.value)
        ir_index    = round(min(1.0, max(0.0, (glucose.value - 70) / 130)), 3)

        return GlucosePanel(
            fasting_glucose=glucose, estimated_hba1c=hba1c,
            classification=dx, insulin_resistance_index=ir_index,
            interpretation=self._interpret(dx, glucose.value, hba1c),
        )

    def _interpret(self, dx: DiabetesClassification, glucose: float, hba1c: float) -> str:
        if dx == DiabetesClassification.HYPOGLYCAEMIA:
            return f"Blood glucose critically low ({glucose:.0f} mg/dL). Immediate attention needed."
        if dx == DiabetesClassification.NORMAL:
            return f"Fasting glucose normal ({glucose:.0f} mg/dL). Estimated HbA1c: {hba1c:.1f}%."
        if dx == DiabetesClassification.PREDIABETES:
            return (f"Prediabetes range ({glucose:.0f} mg/dL). Estimated HbA1c: {hba1c:.1f}%. "
                    "Diet and exercise review recommended. Retest in 3–6 months.")
        return (f"Fasting glucose elevated ({glucose:.0f} mg/dL). Estimated HbA1c: {hba1c:.1f}%. "
                "Confirm with formal laboratory testing. Consult a healthcare provider.")


class LiverAnalyser:
    def analyse(self, raw: Dict, sim: BTMSensorSimulator, sex: Sex) -> LiverPanel:
        def m(key, low, high) -> Measurement:
            val, conf = sim.measure(raw[key], "spectrophoto")
            return Measurement(val, _UNITS[key], low, high, conf)

        male = sex == Sex.MALE
        alt  = m("ALT",            7.0, 56.0 if male else 45.0)
        ast  = m("AST",            10.0, 40.0)
        alp  = m("ALP",            44.0, 147.0)
        bili = m("bilirubin",      0.1, 1.2)
        alb  = m("albumin",        3.4, 5.0)

        ratio   = round(ast.value / max(alt.value, 0.1), 2)
        stress  = self._hepatic_stress(alt.value, ast.value, bili.value, alb.value)

        return LiverPanel(
            ALT=alt, AST=ast, ALP=alp, bilirubin_total=bili, albumin=alb,
            AST_ALT_ratio=ratio, hepatic_stress_index=stress,
            interpretation=self._interpret(alt, ast, ratio, alb.value, stress),
        )

    def _hepatic_stress(self, alt: float, ast: float, bili: float, alb: float) -> float:
        score = 0.0
        if alt > 56:   score += 0.25
        if ast > 40:   score += 0.25
        if bili > 1.2: score += 0.25
        if alb < 3.4:  score += 0.25
        return round(score, 2)

    def _interpret(self, alt: Measurement, ast: Measurement,
                   ratio: float, alb: float, stress: float) -> str:
        parts = []
        if alt.flag in ("H", "HH") or ast.flag in ("H", "HH"):
            if ratio > 2.0:
                parts.append(f"AST/ALT ratio {ratio:.1f} — pattern may suggest alcohol-related injury.")
            else:
                parts.append("Transaminases elevated — hepatocellular stress. Avoid hepatotoxic agents.")
        if alb < 3.4:
            parts.append("Low albumin — consider nutritional status or chronic liver assessment.")
        if stress == 0.0:
            parts.append("Liver function panel within normal limits.")
        return " ".join(parts) if parts else "Liver function normal."


class KidneyAnalyser:
    def analyse(self, raw: Dict, sim: BTMSensorSimulator, age: int, sex: Sex) -> KidneyPanel:
        def m(key, low, high) -> Measurement:
            val, conf = sim.measure(raw[key], "electrochemical")
            return Measurement(val, _UNITS[key], low, high, conf)

        creat   = m("creatinine",   0.6 if sex == Sex.FEMALE else 0.7,
                                    1.1 if sex == Sex.FEMALE else 1.3)
        urea    = m("urea",         2.5, 7.1)
        uric    = m("uric_acid",    2.4 if sex == Sex.FEMALE else 3.4,
                                    6.0 if sex == Sex.FEMALE else 7.0)

        egfr    = ClinicalCalculators.egfr_ckd_epi_2021(creat.value, age, sex)
        bun_cr  = round((urea.value * 2.8) / max(creat.value, 0.1), 1)
        stage   = ClinicalCalculators.classify_ckd(egfr)

        return KidneyPanel(
            creatinine=creat, urea=urea, uric_acid=uric,
            eGFR=egfr, BUN_creatinine_ratio=bun_cr, ckd_stage=stage,
            interpretation=self._interpret(egfr, stage, bun_cr, uric.value),
        )

    def _interpret(self, egfr: float, stage: CKDStage, bun_cr: float, uric: float) -> str:
        parts = []
        if stage == CKDStage.G1:
            parts.append(f"Kidney function excellent (eGFR {egfr:.0f}).")
        elif stage == CKDStage.G2:
            parts.append(f"Mild reduction in kidney function (eGFR {egfr:.0f}). Monitor annually.")
        elif stage in (CKDStage.G3A, CKDStage.G3B):
            parts.append(f"Moderate kidney impairment (eGFR {egfr:.0f}, {stage.value}). "
                         "Nephrologist referral recommended.")
        elif stage in (CKDStage.G4, CKDStage.G5):
            parts.append(f"Severe kidney impairment (eGFR {egfr:.0f}, {stage.value}). "
                         "Urgent specialist care required.")
        if bun_cr > 20:
            parts.append(f"BUN/Cr ratio {bun_cr:.1f} — possible dehydration or prerenal cause.")
        if uric > 7.0:
            parts.append("Elevated uric acid — gout risk; consider hydration and dietary review.")
        return " ".join(parts)


class MarkersAnalyser:
    def analyse(self, raw: Dict, sim: BTMSensorSimulator) -> DiseaseMarkers:
        val, conf   = sim.measure(raw["CRP"], "immunoassay")
        crp         = Measurement(val, "mg/L", 0.0, 3.0, conf)
        return DiseaseMarkers(CRP=crp, interpretation=self._interpret(crp.value))

    def _interpret(self, crp: float) -> str:
        if crp < 1.0:   return "CRP low — minimal systemic inflammation."
        if crp < 3.0:   return "CRP in average range — moderate cardiovascular risk indicator."
        if crp < 10.0:  return "Elevated CRP — mild to moderate inflammation present."
        return f"CRP significantly elevated ({crp:.1f} mg/L) — significant inflammation. Investigate cause."


class HaemoglobinopathyAnalyser:
    """
    Haemoglobinopathy screening panel — critical for the Ghana / West Africa
    population, where sickle cell trait (AS) carrier rate is approximately 25%.

    Detection method:
        Primary   — Capillary electrophoresis (CE) / isoelectric focusing (IEF)
                    of haemoglobin fractions from the collected sample
        Confirm   — Lateral flow immunoassay confirmation of HbS/HbC presence

    Genotypes detected: AA, AS, SS, AC, SC, CC, S-beta-thalassaemia
    """

    def analyse(self, haem_raw: Dict, sim: BTMSensorSimulator) -> HaemoglobinopathyPanel:
        def m(key, low, high) -> Measurement:
            val, conf = sim.measure(haem_raw[key], "electrophoresis")
            return Measurement(val, "%", low, high, conf)

        hbA = m("HbA", 95.0, 100.0)
        hbS = m("HbS", 0.0, 0.0)
        hbC = m("HbC", 0.0, 0.0)
        hbF = m("HbF", 0.0, 1.0)

        genotype = ClinicalCalculators.classify_haemoglobin_genotype(
            hbA.value, hbS.value, hbC.value, hbF.value
        )
        is_carrier = genotype in (HaemoglobinGenotype.AS, HaemoglobinGenotype.AC)
        is_disease = genotype in (
            HaemoglobinGenotype.SS, HaemoglobinGenotype.SC,
            HaemoglobinGenotype.CC, HaemoglobinGenotype.S_BETA_THAL,
        )

        return HaemoglobinopathyPanel(
            HbA=hbA, HbS=hbS, HbC=hbC, HbF=hbF,
            genotype=genotype, is_carrier=is_carrier, is_disease=is_disease,
            detection_method="Capillary electrophoresis + lateral flow confirmation",
            interpretation=self._interpret(genotype),
        )

    def _interpret(self, genotype: HaemoglobinGenotype) -> str:
        if genotype == HaemoglobinGenotype.AA:
            return "No sickle cell or haemoglobin C trait detected. Normal haemoglobin pattern."
        if genotype == HaemoglobinGenotype.AS:
            return ("Sickle cell trait (AS) detected — carrier status, not disease. "
                    "Generally asymptomatic. Genetic counselling recommended before family "
                    "planning, as risk exists if a partner is also a carrier.")
        if genotype == HaemoglobinGenotype.AC:
            return ("Haemoglobin C trait (AC) detected — carrier status, not disease. "
                    "Generally asymptomatic. Genetic counselling recommended before family planning.")
        if genotype == HaemoglobinGenotype.SS:
            return ("Sickle cell disease (SS) detected. Requires specialist haematology follow-up, "
                    "vaccination review, and ongoing management to reduce crisis risk. "
                    "Urgent referral recommended if not already under care.")
        if genotype == HaemoglobinGenotype.SC:
            return ("Haemoglobin SC disease detected. Milder course than SS but still requires "
                    "specialist haematology follow-up and monitoring for complications.")
        if genotype == HaemoglobinGenotype.CC:
            return ("Haemoglobin C disease (CC) detected. Usually mild — may cause chronic "
                    "low-grade haemolysis. Haematology follow-up recommended.")
        return ("Sickle beta-thalassaemia pattern detected — clinical severity varies. "
                "Specialist haematology referral and confirmatory testing recommended.")


class TransmissibleDiseaseAnalyser:
    """
    Point-of-care transmissible disease screening — same sensor class
    (lateral flow immunoassay) as this file's existing CRP marker.

    A reactive result here triggers the consent-gated disclosure flow
    (see btm_ai_interpreter.py / btm_results.py) rather than being
    shown like an ordinary flagged result — this analyser's job is
    only to produce the clinical-register panel data; it does not
    decide how the result is disclosed.
    """

    _MARKERS = ["syphilis_rpr", "hiv_screen", "hepatitis_b_hbsag"]
    _DISPLAY_NAMES = {
        "syphilis_rpr"     : "Syphilis (RPR)",
        "hiv_screen"       : "HIV",
        "hepatitis_b_hbsag": "Hepatitis B (HBsAg)",
    }

    def analyse(self, raw: Dict, sim: "BTMSensorSimulator") -> TransmissibleDiseasePanel:
        markers = []
        for key in self._MARKERS:
            true_reactive = raw[key]
            # Route the binary result through the same sensor-noise model
            # as every other panel, so confidence still reflects sample
            # volume / sensor accuracy rather than being hardcoded.
            val, conf = sim.measure(1.0 if true_reactive else 0.0, "immunoassay")
            markers.append(ScreeningMarker(
                name             = self._DISPLAY_NAMES[key],
                reactive         = val >= 0.5,
                confidence       = conf,
                detection_method = "Lateral flow immunoassay",
            ))

        any_reactive = any(m.reactive for m in markers)
        return TransmissibleDiseasePanel(
            markers        = markers,
            any_reactive   = any_reactive,
            interpretation = self._interpret(markers),
        )

    def _interpret(self, markers: List[ScreeningMarker]) -> str:
        reactive_names = [m.name for m in markers if m.reactive]
        if not reactive_names:
            return "No reactive markers on transmissible disease screening."
        return (
            f"Reactive screening result(s): {', '.join(reactive_names)}. "
            "Point-of-care screening results require confirmatory laboratory "
            "testing before diagnosis — this is a screen, not a diagnosis. "
            "Confidential counselling and treatment support available on request."
        )


# ─────────────────────────────────────────────
#  COMPOSITE HEALTH INDEX
# ─────────────────────────────────────────────

class HealthIndexCalculator:
    """
    Computes the Aid Plus Composite Health Index (CHI).
    Weighted panel scores combined into a 0–100 wellness indicator.
    Not diagnostic — contextualises results for the user.
    """

    _WEIGHTS = {
        "cbc"              : 0.16,
        "lipid"            : 0.20,
        "glucose"          : 0.20,
        "liver"            : 0.12,
        "kidney"           : 0.12,
        "haemoglobinopathy": 0.10,
        "transmissible"    : 0.10,
    }

    def compute(
        self,
        cbc               : CBCPanel,
        lipid             : LipidPanel,
        glucose           : GlucosePanel,
        liver             : LiverPanel,
        kidney            : KidneyPanel,
        haemoglobinopathy : HaemoglobinopathyPanel,
        transmissible     : TransmissibleDiseasePanel,
    ) -> CompositeHealthIndex:

        panel_scores = {
            "cbc"              : self._cbc_score(cbc),
            "lipid"            : self._lipid_score(lipid),
            "glucose"          : self._glucose_score(glucose),
            "liver"            : self._liver_score(liver),
            "kidney"           : self._kidney_score(kidney),
            "haemoglobinopathy": self._haemoglobinopathy_score(haemoglobinopathy),
            "transmissible"    : self._transmissible_score(transmissible),
        }

        chi = sum(panel_scores[p] * self._WEIGHTS[p] for p in panel_scores)
        chi = round(chi, 1)

        # Count flags — haemoglobinopathy is deliberately excluded here.
        # Its HbA/HbS/HbC/HbF measurements are structurally "out of range"
        # against an AA reference for any other genotype (e.g. AS carriers
        # always show HbS as critical), which isn't a clinical emergency —
        # it's already handled correctly via genotype-based panel scoring
        # above. Mixing it into flags/critical_count would make routine
        # carrier results look like acute crises.
        #
        # Transmissible disease markers, unlike haemoglobinopathy fractions,
        # DO belong in flags/critical_count — a reactive screen is genuinely
        # abnormal for the general population (unlike a carrier genotype,
        # which is a structural artefact of the reference range), so it
        # should count the same way a critically abnormal lab value does.
        all_measurements = self._gather_measurements(cbc, lipid, glucose, liver, kidney)
        flags    = sum(1 for m in all_measurements if not m.in_range)
        critical = sum(1 for m in all_measurements if m.is_critical)

        reactive_count = sum(1 for m in transmissible.markers if m.reactive)
        flags    += reactive_count
        critical += reactive_count

        return CompositeHealthIndex(
            score        = chi,
            grade        = self._grade(chi),
            panel_scores = {k: round(v, 1) for k, v in panel_scores.items()},
            flags_count  = flags,
            critical_count = critical,
            trend        = "FIRST_TEST",  # updated by ML engine on repeat tests
        )

    def _cbc_score(self, cbc: CBCPanel) -> float:
        score = 100.0
        if cbc.anaemia_type != AnaemiaType.NONE: score -= 20
        if cbc.infection_index > 0.5:             score -= 15
        if cbc.WBC.flag in ("H", "HH"):           score -= 10
        if cbc.platelets.flag in ("L", "LL"):     score -= 10
        return max(0.0, score)

    def _lipid_score(self, lipid: LipidPanel) -> float:
        base = {
            RiskLevel.OPTIMAL   : 100.0,
            RiskLevel.NORMAL    : 85.0,
            RiskLevel.BORDERLINE: 65.0,
            RiskLevel.HIGH      : 45.0,
            RiskLevel.CRITICAL  : 20.0,
        }
        return base.get(lipid.risk_level, 70.0)

    def _glucose_score(self, glucose: GlucosePanel) -> float:
        dx = glucose.classification
        if dx == DiabetesClassification.NORMAL:        return 100.0
        if dx == DiabetesClassification.PREDIABETES:   return 60.0
        if dx == DiabetesClassification.DIABETES_LIKELY: return 30.0
        if dx == DiabetesClassification.HYPOGLYCAEMIA: return 20.0
        return 70.0

    def _liver_score(self, liver: LiverPanel) -> float:
        return max(0.0, 100.0 - liver.hepatic_stress_index * 100.0)

    def _kidney_score(self, kidney: KidneyPanel) -> float:
        stage_scores = {
            CKDStage.G1: 100.0, CKDStage.G2: 80.0, CKDStage.G3A: 55.0,
            CKDStage.G3B: 40.0, CKDStage.G4: 20.0, CKDStage.G5: 5.0,
        }
        return stage_scores.get(kidney.ckd_stage, 70.0)

    def _haemoglobinopathy_score(self, haem: HaemoglobinopathyPanel) -> float:
        """
        Carrier states (AS, AC) score high — generally asymptomatic, but the
        result still needs to reach the patient for counselling purposes.
        Disease states score lower to reflect ongoing monitoring need,
        not as a punitive measure — this feeds the wellness index only.
        """
        scores = {
            HaemoglobinGenotype.AA           : 100.0,
            HaemoglobinGenotype.AS           : 95.0,
            HaemoglobinGenotype.AC           : 95.0,
            HaemoglobinGenotype.SC           : 60.0,
            HaemoglobinGenotype.CC           : 70.0,
            HaemoglobinGenotype.SS           : 55.0,
            HaemoglobinGenotype.S_BETA_THAL  : 55.0,
        }
        return scores.get(haem.genotype, 80.0)

    def _transmissible_score(self, panel: TransmissibleDiseasePanel) -> float:
        """
        Any reactive screening marker drags this score down significantly —
        deliberately more than a routine flagged lab value, since these
        conditions warrant prompt confirmatory testing and treatment.
        Multiple reactive markers score lower still. This feeds the
        wellness index only — it is not itself the disclosure mechanism.
        """
        reactive_count = sum(1 for m in panel.markers if m.reactive)
        if reactive_count == 0:
            return 100.0
        if reactive_count == 1:
            return 40.0
        return 20.0

    def _grade(self, score: float) -> str:
        if score >= 92: return "A+"
        if score >= 85: return "A"
        if score >= 75: return "B"
        if score >= 60: return "C"
        if score >= 45: return "D"
        return "F"

    def _gather_measurements(self, *panels) -> List[Measurement]:
        result = []
        for panel in panels:
            for val in vars(panel).values():
                if isinstance(val, Measurement):
                    result.append(val)
        return result


# ─────────────────────────────────────────────
#  UNITS REGISTRY
# ─────────────────────────────────────────────

_UNITS = {
    "RBC": "×10¹²/L", "WBC": "×10⁹/L", "hemoglobin": "g/dL",
    "hematocrit": "%", "MCV": "fL", "MCH": "pg", "MCHC": "g/dL",
    "platelets": "×10⁹/L", "total_cholesterol": "mg/dL", "LDL": "mg/dL",
    "HDL": "mg/dL", "triglycerides": "mg/dL", "glucose": "mg/dL",
    "ALT": "U/L", "AST": "U/L", "ALP": "U/L", "bilirubin": "mg/dL",
    "albumin": "g/dL", "creatinine": "mg/dL", "urea": "mmol/L",
    "uric_acid": "mg/dL", "CRP": "mg/L",
    "HbA": "%", "HbS": "%", "HbC": "%", "HbF": "%",
}


# ─────────────────────────────────────────────
#  MAIN ANALYSIS ENGINE
# ─────────────────────────────────────────────

class BTMAnalysisEngine:
    """
    AID PLUS+ BTM Analysis Engine

    Orchestrates the full blood analysis pipeline from raw sample
    through to a structured AnalysisReport ready for infobox delivery.

    Usage:
        engine = BTMAnalysisEngine()
        report = engine.analyse(collection_result, sex=Sex.MALE, age=35)
    """

    def __init__(self, hw_simulation: bool = True):
        self._sim_mode  = hw_simulation
        self._cbc       = CBCAnalyser()
        self._lipid     = LipidAnalyser()
        self._glucose   = GlucoseAnalyser()
        self._liver     = LiverAnalyser()
        self._kidney    = KidneyAnalyser()
        self._markers   = MarkersAnalyser()
        self._haem      = HaemoglobinopathyAnalyser()
        self._transmissible = TransmissibleDiseaseAnalyser()
        self._chi       = HealthIndexCalculator()
        self._ml        = BTMLocalMLEngine()
        log.info("BTMAnalysisEngine ready | sim=%s", hw_simulation)

    def analyse(
        self,
        collection  : CollectionResult,
        sex         : Sex = Sex.MALE,
        age         : int = 35,
    ) -> AnalysisReport:
        """
        Run full multi-panel analysis on a collected sample.
        Returns complete AnalysisReport with all panels and health index.
        """
        if not collection.ready_for_analysis:
            raise ValueError("CollectionResult not marked ready_for_analysis.")

        t0  = time.time()
        log.info("Analysis start | session=%s | vol=%.1fµL | sex=%s | age=%d",
                 collection.session_id, collection.sample_volume_ul, sex.value, age)

        # ── Sensor simulation setup ───────────────────────────────────────
        sim = BTMSensorSimulator(collection.sample_volume_ul)
        raw = sim.generate_population_normal(sex, age)
        haem_raw = sim.generate_haemoglobin_profile()
        screening_raw = sim.generate_screening_profile()

        # ── Run all panels ────────────────────────────────────────────────
        cbc_panel   = self._cbc.analyse(raw, sim, sex)
        lipid_panel = self._lipid.analyse(raw, sim, age, sex)
        gluc_panel  = self._glucose.analyse(raw, sim)
        liver_panel = self._liver.analyse(raw, sim, sex)
        kidney_panel= self._kidney.analyse(raw, sim, age, sex)
        markers     = self._markers.analyse(raw, sim)
        haem_panel  = self._haem.analyse(haem_raw, sim)
        transmissible_panel = self._transmissible.analyse(screening_raw, sim)

        # ── Composite health index ────────────────────────────────────────
        health_idx  = self._chi.compute(
            cbc_panel, lipid_panel, gluc_panel, liver_panel, kidney_panel,
            haem_panel, transmissible_panel,
        )

        # ── ML anomaly detection ──────────────────────────────────────────
        ml_report   = self._ml.on_session_complete(
            card_id         = collection.user_card_id,
            skin_profile    = {"surface_temp_celsius": 33.5, "stratum_corneum_index": 0.35, "hydration_index": 0.65},
            strike_profile  = {"target_depth_mm": 1.78},
            suction_result  = {"volume_collected_ul": collection.sample_volume_ul,
                               "flow_cycles": 10, "target_met": True,
                               "deflection_reading": 0.85, "compliance_reading": 1.8},
            blood_results   = {
                "RBC"        : cbc_panel.RBC.value,
                "WBC"        : cbc_panel.WBC.value,
                "hemoglobin" : cbc_panel.hemoglobin.value,
                "platelets"  : cbc_panel.platelets.value,
                "glucose"    : gluc_panel.fasting_glucose.value,
                "cholesterol": lipid_panel.total_cholesterol.value,
                "LDL"        : lipid_panel.LDL.value,
                "HDL"        : lipid_panel.HDL.value,
                "ALT"        : liver_panel.ALT.value,
                "creatinine" : kidney_panel.creatinine.value,
            },
            scanner_result  = {"bacteria_index": 0.04, "dna_fingerprint_matched": True},
        )

        anomaly     = ml_report.get("anomaly", {})
        # ResultAnomalyDetector returns AnomalyLevel as an actual Enum member
        # inside the dict (dataclasses.asdict() doesn't convert nested Enums
        # to their .value) — normalise here so anomaly_level is always a
        # plain JSON-serialisable string, never a stray Enum object.
        _raw_anomaly_level = anomaly.get("level", "NORMAL")
        anomaly_level_str  = _raw_anomaly_level.value if hasattr(_raw_anomaly_level, "value") \
                             else str(_raw_anomaly_level)

        # ── Publish analysis event ────────────────────────────────────────
        bus.publish(
            message_type = MessageType.ANALYSIS_RESULT,
            payload      = {
                "session_id"    : collection.session_id,
                "user_card_id"  : collection.user_card_id,
                "health_score"  : health_idx.score,
                "health_grade"  : health_idx.grade,
                "flags"         : health_idx.flags_count,
                "critical_flags": health_idx.critical_count,
                "anomaly_level" : anomaly_level_str,
                "ckd_stage"     : kidney_panel.ckd_stage.value,
                "egfr"          : kidney_panel.eGFR,
                "cvd_risk"      : lipid_panel.cvd_risk_score,
                "hba1c_est"     : gluc_panel.estimated_hba1c,
                "haem_genotype" : haem_panel.genotype.value,
                "haem_carrier"  : haem_panel.is_carrier,
                "haem_disease"  : haem_panel.is_disease,
                # Data minimization: internal bus event carries only a
                # boolean, never which specific marker(s) reacted — no
                # other current subscriber (ML, maintenance) needs more
                # than that, and this is sensitive data even internally.
                "transmissible_screen_reactive": transmissible_panel.any_reactive,
                "deployment"    : collection.deployment_mode,
            },
            priority     = Priority.HIGH,
            session_id   = collection.session_id,
            user_card_id = collection.user_card_id,
        )

        duration = time.time() - t0
        log.info("Analysis complete | duration=%.2fs | CHI=%.1f (%s) | flags=%d | genotype=%s",
                 duration, health_idx.score, health_idx.grade, health_idx.flags_count,
                 haem_panel.genotype.value)

        return AnalysisReport(
            session_id          = collection.session_id,
            user_card_id        = collection.user_card_id,
            sex                 = sex,
            age_years           = age,
            deployment_mode     = collection.deployment_mode,
            cbc                 = cbc_panel,
            lipid               = lipid_panel,
            glucose             = gluc_panel,
            liver               = liver_panel,
            kidney              = kidney_panel,
            markers             = markers,
            haemoglobinopathy   = haem_panel,
            transmissible_disease = transmissible_panel,
            health_index        = health_idx,
            anomaly_level       = anomaly_level_str,
            anomaly_detail      = anomaly.get("recommendation"),
            analysis_duration_s = round(duration, 3),
            sample_volume_ul    = collection.sample_volume_ul,
            ready_for_delivery  = True,
        )


# ─────────────────────────────────────────────
#  SMOKE TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== BTM Analysis Engine — Test Suite ===\n")

    import sys
    sys.path.insert(0, ".")

    from btm_bus import bus
    from btm_sample import (CollectionResult, CollectionStatus,
                             DeploymentContext, Hand, Finger)

    bus.activate(hw_simulation=True)
    session = bus.open_session("AID-A1B2-C3D4-E5F6")

    # Fake a completed collection result
    mock_collection = CollectionResult(
        session_id          = session,
        user_card_id        = "AID-A1B2-C3D4-E5F6",
        status              = CollectionStatus.COMPLETE,
        deployment_mode     = "KIOSK",
        hand                = Hand.RIGHT,
        finger              = Finger.INDEX,
        scanner             = None,
        skin_profile        = None,
        strike_profile      = None,
        suction             = None,
        sample_volume_ul    = 52.0,
        collection_duration_s = 18.4,
        ready_for_analysis  = True,
    )

    engine = BTMAnalysisEngine(hw_simulation=True)

    for label, sex, age in [("Adult male 38", Sex.MALE, 38), ("Adult female 52", Sex.FEMALE, 52)]:
        print(f"  [{label}]")
        report = engine.analyse(mock_collection, sex=sex, age=age)

        chi = report.health_index
        print(f"    Composite Health Index : {chi.score:.1f} / 100 (Grade {chi.grade})")
        print(f"    Panel scores           : CBC={chi.panel_scores['cbc']:.0f} | "
              f"Lipid={chi.panel_scores['lipid']:.0f} | "
              f"Glucose={chi.panel_scores['glucose']:.0f} | "
              f"Liver={chi.panel_scores['liver']:.0f} | "
              f"Kidney={chi.panel_scores['kidney']:.0f} | "
              f"Haem={chi.panel_scores['haemoglobinopathy']:.0f} | "
              f"Transmissible={chi.panel_scores['transmissible']:.0f}")
        print(f"    Abnormal markers       : {chi.flags_count} | Critical: {chi.critical_count}")

        k = report.kidney
        print(f"    eGFR (CKD-EPI 2021)    : {k.eGFR} mL/min/1.73m² ({k.ckd_stage.value})")
        print(f"    Kidney interpretation  : {k.interpretation}")

        g = report.glucose
        print(f"    Glucose                : {g.fasting_glucose.value:.0f} mg/dL | "
              f"eHbA1c: {g.estimated_hba1c}% | {g.classification.value}")

        l = report.lipid
        print(f"    CVD risk score         : {l.cvd_risk_score:.1f}% ({l.risk_level.value})")

        c = report.cbc
        print(f"    Anaemia status         : {c.anaemia_type.value} | "
              f"Infection index: {c.infection_index:.2f}")

        h = report.haemoglobinopathy
        print(f"    Haemoglobin genotype   : {h.genotype.value} "
              f"(HbA={h.HbA.value:.1f}% HbS={h.HbS.value:.1f}% HbC={h.HbC.value:.1f}% HbF={h.HbF.value:.1f}%) "
              f"| carrier={h.is_carrier} disease={h.is_disease}")

        td = report.transmissible_disease
        reactive_names = [m.name for m in td.markers if m.reactive]
        print(f"    Transmissible screen   : any_reactive={td.any_reactive} "
              f"| reactive={reactive_names or 'none'}")

        print(f"    Analysis duration      : {report.analysis_duration_s:.3f}s")
        print(f"    Ready for delivery     : {report.ready_for_delivery}")
        print()

    bus.close_session(session)
    bus.deactivate()
    print("✓ BTM Analysis Engine test complete\n")
