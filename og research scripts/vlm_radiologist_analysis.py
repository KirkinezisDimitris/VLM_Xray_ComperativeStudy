
"""
============================================================
VLM-Radiologist Agreement Analysis
============================================================

Purpose
-------
This script analyzes how closely VLM predictions agree with human radiologist
annotations across the same chest X-ray patient-finding items.

IMPORTANT CONCEPTUAL POINT
--------------------------
This script does NOT assume that any single radiologist is absolute ground truth.
The chief radiologist, each doctor, and human consensus are used as AGREEMENT
CONTEXTS, not as unquestionable truth.

The analysis answers questions such as:

    1. Which VLM agrees most with individual human readers?
    2. Which VLM agrees most with the average doctor pattern?
    3. Which VLM agrees most with the chief radiologist?
    4. Which VLM agrees most with non-chief doctor majority consensus?
    5. Which VLM agrees most with all-human majority consensus?
    6. Do the VLMs agree with each other?
    7. Do VLMs produce contradictions involving "No Finding"?

Input files
-----------
1) Human annotations:
       PATIENT ANSWERS.xlsx

   Expected columns:
       patient_id, finding_id, answer_choice, updated_at, user_id, username, role

2) Patient ID mapping:
       patients with ID.xlsx

   Expected columns commonly look like:
       id, patient_code

3) VLM prediction CSV files:
       clip_raw_and_calibrated_patient_results.csv
       biovil_raw_and_calibrated_patient_results.csv
       chexagent_patient_results.csv

Human label encoding
--------------------
Your annotation system uses:

    answer_choice = 1  -> present
    answer_choice = 2  -> absent
    answer_choice = 3  -> uncertain

For analysis, this is normalized to:

    0 = absent
    1 = uncertain
    2 = present

Model label encoding
--------------------
The VLM CSV files are expected to use:

    answer_choice = 0 -> absent
    answer_choice = 1 -> uncertain
    answer_choice = 2 -> present

No Finding rule
---------------
For HUMAN annotations only:

    If a human reader marks "No Finding" as present for a patient,
    then all other findings for that same patient-reader pair are treated as absent.

This rule is applied only in memory and only to the analysis dataframe.
The original Excel file is NOT modified.

For VLMs:

    The No Finding rule is NOT forced.

Instead, this script calculates a VLM No Finding contradiction rate, for example:

    No Finding = present
    AND at least one other finding = uncertain or present

This is useful because model inconsistency is itself an important reliability result.

Main outputs
------------
The script writes many CSV files to the selected output folder, including:

    clean_human_annotations_used.csv
    clean_vlm_predictions_used.csv

    vlm_vs_each_human_overall.csv
    vlm_vs_each_human_by_finding.csv
    vlm_vs_each_human_confusion_matrices_long.csv

    vlm_vs_each_doctor_overall.csv
    vlm_vs_each_doctor_by_finding.csv

    vlm_vs_doctors_mean_overall.csv
    vlm_vs_doctors_mean_by_finding.csv

    vlm_vs_chief_overall.csv
    vlm_vs_chief_by_finding.csv

    nonchief_doctor_majority_consensus.csv
    all_human_majority_consensus.csv

    vlm_vs_nonchief_consensus_overall.csv
    vlm_vs_nonchief_consensus_by_finding.csv

    vlm_vs_all_human_consensus_overall.csv
    vlm_vs_all_human_consensus_by_finding.csv

    vlm_only_fleiss_overall.csv
    vlm_only_fleiss_by_finding.csv

    vlm_vs_vlm_pairwise_overall.csv
    vlm_vs_vlm_confusion_matrices_long.csv

    vlm_no_finding_contradictions_summary.csv
    vlm_no_finding_contradictions_detail.csv

    chexagent_parse_status_summary.csv
    summary_vlm_human_agreement_contexts.csv

Optional plots
--------------
If --make_plots is passed, the script also creates thesis-friendly PNG files.

How to run
----------
Example PowerShell command:

python .\vlm_radiologist_analysis.py `
  --patient_answers "outputs\PATIENT ANSWERS.xlsx" `
  --patients_with_id "outputs\patients with ID.xlsx" `
  --clip_results "outputs\clip_raw_and_calibrated_patient_results.csv" `
  --biovil_results "outputs\biovil_raw_and_calibrated_patient_results.csv" `
  --chexagent_results "outputs\chexagent_patient_results.csv" `
  --output_dir vlm_agreement_outputs `
  --make_plots
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from sklearn.metrics import cohen_kappa_score, confusion_matrix, f1_score

import matplotlib.pyplot as plt


# ============================================================
# Constants
# ============================================================

FINDINGS = [
    "No Finding",
    "Enlarged Cardiomediastinum",
    "Cardiomegaly",
    "Lung Opacity",
    "Lung Lesion",
    "Edema",
    "Consolidation",
    "Pneumonia",
    "Atelectasis",
    "Pneumothorax",
    "Pleural Effusion",
    "Pleural Other",
    "Fracture",
    "Support Devices",
]

LABELS = [0, 1, 2]
LABEL_NAMES = {
    0: "absent",
    1: "uncertain",
    2: "present",
}

# Human annotation mapping:
# Original system: 1=present, 2=absent, 3=uncertain
# Normalized system: 0=absent, 1=uncertain, 2=present
HUMAN_LABEL_MAP = {
    1: 2,
    2: 0,
    3: 1,
}

# Expected key human readers in your project.
# These names are used only for convenience and safety checks.
CHIEF_USERNAME = "chief_rad_01"
NONCHIEF_DOCTOR_USERNAMES = ["rad_01", "rad_02", "rad_03", "rad_04", "rad_06"]
ALL_HUMAN_USERNAMES = [CHIEF_USERNAME] + NONCHIEF_DOCTOR_USERNAMES


# ============================================================
# General helper functions
# ============================================================

def ensure_dir(path: str) -> None:
    """Create an output directory if it does not already exist."""
    os.makedirs(path, exist_ok=True)


def finding_id_to_name(finding_id: int) -> str:
    """Map finding_id 1..14 to the corresponding finding name."""
    finding_id = int(finding_id)
    if finding_id < 1 or finding_id > len(FINDINGS):
        raise ValueError(f"Unexpected finding_id: {finding_id}")
    return FINDINGS[finding_id - 1]


def normalize_human_answer(answer_choice) -> float:
    """
    Convert a human answer_choice into normalized 0/1/2 labels.

    Human/original:
        1 = present
        2 = absent
        3 = uncertain

    Normalized:
        0 = absent
        1 = uncertain
        2 = present
    """
    try:
        value = int(answer_choice)
    except Exception:
        return np.nan
    return HUMAN_LABEL_MAP.get(value, np.nan)


def normalize_model_answer(answer_choice) -> float:
    """
    Convert a model answer_choice into normalized 0/1/2 labels.

    Model files are expected to already use:
        0 = absent
        1 = uncertain
        2 = present
    """
    try:
        value = int(answer_choice)
    except Exception:
        return np.nan
    if value in LABELS:
        return value
    return np.nan


def agreement_level_kappa(kappa: float) -> str:
    """
    Common descriptive interpretation for kappa-like statistics.

    This is a useful reporting convention, not a law of nature.
    """
    if pd.isna(kappa):
        return "not available"
    if kappa < 0:
        return "poor"
    if kappa <= 0.20:
        return "slight"
    if kappa <= 0.40:
        return "fair"
    if kappa <= 0.60:
        return "moderate"
    if kappa <= 0.80:
        return "substantial"
    return "almost perfect"


def safe_cohen_kappa(a: Iterable[int], b: Iterable[int], weights: Optional[str] = None) -> float:
    """
    Compute Cohen's kappa safely.

    sklearn can return warnings or NaN in degenerate cases where one or both
    vectors contain only one class. This wrapper keeps the script robust.
    """
    a = np.asarray(list(a), dtype=int)
    b = np.asarray(list(b), dtype=int)

    if len(a) == 0:
        return np.nan

    # If both arrays are identical and contain only one class, agreement is perfect.
    if np.array_equal(a, b) and len(np.unique(a)) == 1:
        return 1.0

    try:
        return float(cohen_kappa_score(a, b, labels=LABELS, weights=weights))
    except Exception:
        return np.nan


def safe_macro_f1(reference: Iterable[int], prediction: Iterable[int]) -> float:
    """
    Compute 3-class macro F1 safely.

    NOTE: Macro F1 is directional because it treats one side as the reference.
    In this project we interpret it only as a descriptive agreement statistic
    relative to the selected comparison context, not as absolute truth.
    """
    reference = np.asarray(list(reference), dtype=int)
    prediction = np.asarray(list(prediction), dtype=int)
    if len(reference) == 0:
        return np.nan
    try:
        return float(f1_score(reference, prediction, labels=LABELS, average="macro", zero_division=0))
    except Exception:
        return np.nan


def compute_pairwise_agreement_metrics(
    df: pd.DataFrame,
    a_col: str,
    b_col: str,
    reference_name: str,
    prediction_name: str,
) -> Dict[str, float]:
    """
    Compute agreement metrics between two aligned label columns.

    The labels must already be normalized:
        0 = absent
        1 = uncertain
        2 = present

    The words "reference" and "prediction" are only column labels here.
    They do NOT mean that the reference is absolute truth.
    """
    data = df.dropna(subset=[a_col, b_col]).copy()

    if data.empty:
        return {
            "reference": reference_name,
            "comparison": prediction_name,
            "n_items": 0,
            "percent_agreement": np.nan,
            "cohen_kappa": np.nan,
            "quadratic_weighted_kappa": np.nan,
            "macro_f1_descriptive": np.nan,
            "mean_absolute_difference": np.nan,
            "severe_disagreement_percent": np.nan,
            "reference_absent_percent": np.nan,
            "reference_uncertain_percent": np.nan,
            "reference_present_percent": np.nan,
            "comparison_absent_percent": np.nan,
            "comparison_uncertain_percent": np.nan,
            "comparison_present_percent": np.nan,
        }

    a = data[a_col].astype(int).to_numpy()
    b = data[b_col].astype(int).to_numpy()

    exact = (a == b)
    abs_diff = np.abs(a - b)

    row = {
        "reference": reference_name,
        "comparison": prediction_name,
        "n_items": len(data),
        "percent_agreement": float(exact.mean() * 100),
        "cohen_kappa": safe_cohen_kappa(a, b, weights=None),
        "quadratic_weighted_kappa": safe_cohen_kappa(a, b, weights="quadratic"),
        "macro_f1_descriptive": safe_macro_f1(a, b),
        "mean_absolute_difference": float(abs_diff.mean()),
        # Severe disagreement means absent vs present, or present vs absent.
        # In normalized labels, this is absolute difference = 2.
        "severe_disagreement_percent": float((abs_diff == 2).mean() * 100),
    }

    for label in LABELS:
        row[f"reference_{LABEL_NAMES[label]}_percent"] = float((a == label).mean() * 100)
        row[f"comparison_{LABEL_NAMES[label]}_percent"] = float((b == label).mean() * 100)

    row["cohen_kappa_level"] = agreement_level_kappa(row["cohen_kappa"])
    row["qwk_level"] = agreement_level_kappa(row["quadratic_weighted_kappa"])

    return row


def compute_confusion_matrix_long(
    df: pd.DataFrame,
    a_col: str,
    b_col: str,
    reference_name: str,
    comparison_name: str,
    extra_cols: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """Return a long-format 3x3 confusion matrix between two label columns."""
    data = df.dropna(subset=[a_col, b_col]).copy()

    if data.empty:
        rows = []
    else:
        a = data[a_col].astype(int).to_numpy()
        b = data[b_col].astype(int).to_numpy()
        cm = confusion_matrix(a, b, labels=LABELS)
        rows = []
        for i, ref_label in enumerate(LABELS):
            for j, comp_label in enumerate(LABELS):
                rows.append({
                    "reference": reference_name,
                    "comparison": comparison_name,
                    "reference_label": ref_label,
                    "reference_label_name": LABEL_NAMES[ref_label],
                    "comparison_label": comp_label,
                    "comparison_label_name": LABEL_NAMES[comp_label],
                    "count": int(cm[i, j]),
                })

    out = pd.DataFrame(rows)
    if extra_cols:
        for key, value in extra_cols.items():
            out[key] = value
    return out


def majority_vote(labels: Iterable[int]) -> int:
    """
    Compute a majority vote over labels 0/1/2.

    Tie-breaking policy:
        present > absent > uncertain

    Why this policy?
    - Present findings are clinically important.
    - Uncertain is kept when it actually has the most votes, but it is not
      preferred in a tie unless the tie only contains uncertain.

    If your supervisor wants a different tie policy, change priority_order.
    """
    labels = [int(x) for x in labels if not pd.isna(x)]
    if not labels:
        return np.nan

    counts = Counter(labels)
    max_count = max(counts.values())
    candidates = [label for label, count in counts.items() if count == max_count]

    if len(candidates) == 1:
        return candidates[0]

    priority_order = [2, 0, 1]
    for label in priority_order:
        if label in candidates:
            return label

    return candidates[0]


# ============================================================
# Fleiss' kappa functions for VLM-only agreement
# ============================================================

def fleiss_kappa_from_count_matrix(counts: pd.DataFrame) -> Tuple[float, float, float]:
    """
    Compute Fleiss' kappa from an item x category count matrix.

    Each row = one patient-finding item.
    Each column = one label category.
    The values = number of raters/models choosing that label.
    """
    arr = counts.to_numpy(dtype=float)
    if arr.size == 0:
        return np.nan, np.nan, np.nan

    n_items = arr.shape[0]
    n_raters_per_item = arr.sum(axis=1)

    if len(np.unique(n_raters_per_item)) != 1:
        raise ValueError("Fleiss' kappa requires the same number of raters/models per item.")

    n_raters = n_raters_per_item[0]
    if n_raters <= 1:
        return np.nan, np.nan, np.nan

    observed_per_item = ((arr ** 2).sum(axis=1) - n_raters) / (n_raters * (n_raters - 1))
    observed_agreement = observed_per_item.mean()

    category_proportions = arr.sum(axis=0) / (n_items * n_raters)
    expected_agreement = (category_proportions ** 2).sum()

    if np.isclose(1 - expected_agreement, 0):
        kappa = np.nan
    else:
        kappa = (observed_agreement - expected_agreement) / (1 - expected_agreement)

    return float(kappa), float(observed_agreement), float(expected_agreement)


def build_fleiss_count_matrix(long_df: pd.DataFrame, item_cols: List[str], label_col: str) -> pd.DataFrame:
    """Build item x label count matrix for Fleiss' kappa."""
    counts = (
        long_df
        .groupby(item_cols + [label_col])
        .size()
        .unstack(fill_value=0)
    )
    for label in LABELS:
        if label not in counts.columns:
            counts[label] = 0
    return counts[LABELS]


# ============================================================
# Data loading
# ============================================================

def infer_patient_map_columns(patient_map: pd.DataFrame) -> Tuple[str, str]:
    """
    Infer the numeric patient id column and patient code column from the mapping file.

    In your project, the mapping file normally has:
        id, patient_code
    """
    lower_to_original = {str(c).strip().lower(): c for c in patient_map.columns}

    numeric_candidates = ["id", "patient_id", "patient_index", "numeric_patient_id"]
    code_candidates = ["patient_code", "patient_folder", "patient_name", "code"]

    numeric_col = None
    for candidate in numeric_candidates:
        if candidate in lower_to_original:
            numeric_col = lower_to_original[candidate]
            break

    code_col = None
    for candidate in code_candidates:
        if candidate in lower_to_original:
            code_col = lower_to_original[candidate]
            break

    if numeric_col is None:
        numeric_col = patient_map.columns[0]
    if code_col is None:
        if len(patient_map.columns) < 2:
            raise ValueError("Patient map needs at least two columns: numeric id and patient code.")
        code_col = patient_map.columns[1]

    return numeric_col, code_col


def load_human_annotations(patient_answers_path: str, patients_with_id_path: str) -> pd.DataFrame:
    """
    Load and clean human annotations.

    Steps:
        1. Read Excel.
        2. Keep expected human readers.
        3. Remove duplicate rows if any.
        4. Apply No Finding rule in memory.
        5. Normalize labels to 0/1/2.
        6. Map numeric patient id to patient code.
        7. Add finding names.
    """
    human = pd.read_excel(patient_answers_path)
    patient_map = pd.read_excel(patients_with_id_path)

    required_cols = ["patient_id", "finding_id", "answer_choice", "username", "role"]
    missing_cols = [c for c in required_cols if c not in human.columns]
    if missing_cols:
        raise ValueError(f"Human annotation file missing columns: {missing_cols}")

    # Keep human readers in the project.
    # This includes the chief and the five non-chief radiologists.
    human = human[human["username"].isin(ALL_HUMAN_USERNAMES)].copy()

    # Remove exact duplicate entries for the same human-reader item.
    # Keep the latest row if updated_at is available.
    duplicate_key = ["patient_id", "finding_id", "username"]
    if "updated_at" in human.columns:
        human = human.sort_values("updated_at")
    before = len(human)
    duplicates = human[human.duplicated(duplicate_key, keep="last")].copy()
    human = human.drop_duplicates(duplicate_key, keep="last").copy()
    removed = before - len(human)

    # Apply No Finding rule in memory only.
    human["adjusted_by_no_finding_rule"] = False
    for (patient_id, username), group in human.groupby(["patient_id", "username"]):
        no_finding_rows = group[group["finding_id"] == 1]
        if no_finding_rows.empty:
            continue
        no_finding_answer = no_finding_rows["answer_choice"].iloc[0]
        if int(no_finding_answer) == 1:
            other_mask = (
                (human["patient_id"] == patient_id)
                & (human["username"] == username)
                & (human["finding_id"] != 1)
            )
            changed_mask = other_mask & (human["answer_choice"] != 2)
            human.loc[other_mask, "answer_choice"] = 2
            human.loc[changed_mask, "adjusted_by_no_finding_rule"] = True

    human["answer_ordinal"] = human["answer_choice"].apply(normalize_human_answer)
    invalid = human["answer_ordinal"].isna().sum()
    if invalid:
        print(f"WARNING: dropping {invalid} human rows with invalid answer_choice.")
        human = human.dropna(subset=["answer_ordinal"]).copy()
    human["answer_ordinal"] = human["answer_ordinal"].astype(int)

    human["finding"] = human["finding_id"].apply(finding_id_to_name)

    numeric_col, code_col = infer_patient_map_columns(patient_map)
    patient_map_small = patient_map[[numeric_col, code_col]].copy()
    patient_map_small.columns = ["patient_id", "patient_code"]

    human = human.merge(patient_map_small, on="patient_id", how="left")

    if human["patient_code"].isna().any():
        missing = human[human["patient_code"].isna()]["patient_id"].drop_duplicates().head(10).tolist()
        raise ValueError(f"Some human patient_id values were not found in patient map. Examples: {missing}")

    human["reader_type"] = np.where(human["username"] == CHIEF_USERNAME, "chief", "doctor")

    # Keep metadata about duplicate removals and No Finding adjustments in attrs.
    human.attrs["duplicate_rows_removed"] = duplicates
    human.attrs["n_duplicate_rows_removed"] = removed

    return human


def normalize_clip_or_biovil(df: pd.DataFrame, model_name: str) -> pd.DataFrame:
    """
    Normalize CLIP or BioViL output table.

    Expected columns:
        patient_id, finding, answer_choice, mode

    If answer_choice is missing but prediction exists, prediction is used.
    """
    data = df.copy()

    if "answer_choice" not in data.columns and "prediction" in data.columns:
        data["answer_choice"] = data["prediction"]

    required = ["patient_id", "finding", "answer_choice", "mode"]
    missing = [c for c in required if c not in data.columns]
    if missing:
        raise ValueError(f"{model_name} file missing required columns: {missing}")

    out = data[["patient_id", "finding", "answer_choice", "mode"]].copy()
    out["answer_ordinal"] = out["answer_choice"].apply(normalize_model_answer)
    out = out.dropna(subset=["answer_ordinal"]).copy()
    out["answer_ordinal"] = out["answer_ordinal"].astype(int)

    out["model_family"] = model_name
    out["vlm_name"] = out["model_family"] + "_" + out["mode"].astype(str)

    return out


def normalize_chexagent(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize CheXagent output table.

    Expected columns:
        patient_id, finding, answer_choice

    If answer_choice is missing but prediction exists, prediction is used.
    parse_status is preserved if present.
    """
    data = df.copy()

    if "answer_choice" not in data.columns and "prediction" in data.columns:
        data["answer_choice"] = data["prediction"]

    required = ["patient_id", "finding", "answer_choice"]
    missing = [c for c in required if c not in data.columns]
    if missing:
        raise ValueError(f"CheXagent file missing required columns: {missing}")

    keep_cols = ["patient_id", "finding", "answer_choice"]
    for optional in ["parse_status", "answer_label", "raw_response", "mode"]:
        if optional in data.columns:
            keep_cols.append(optional)

    out = data[keep_cols].copy()
    out["answer_ordinal"] = out["answer_choice"].apply(normalize_model_answer)
    out = out.dropna(subset=["answer_ordinal"]).copy()
    out["answer_ordinal"] = out["answer_ordinal"].astype(int)

    out["model_family"] = "CheXagent"
    if "mode" in out.columns:
        out["vlm_name"] = "CheXagent_" + out["mode"].astype(str)
    else:
        out["mode"] = "one_finding"
        out["vlm_name"] = "CheXagent_one_finding"

    return out


def load_vlm_predictions(
    clip_results_path: str,
    biovil_results_path: str,
    chexagent_results_path: str,
    patients_with_id_path: str,
) -> pd.DataFrame:
    """Load and combine all VLM predictions into one normalized long table."""
    clip = pd.read_csv(clip_results_path)
    biovil = pd.read_csv(biovil_results_path)
    chexagent = pd.read_csv(chexagent_results_path)
    patient_map = pd.read_excel(patients_with_id_path)

    clip_norm = normalize_clip_or_biovil(clip, "CLIP")
    biovil_norm = normalize_clip_or_biovil(biovil, "BioViL-T")
    chex_norm = normalize_chexagent(chexagent)

    vlm = pd.concat([clip_norm, biovil_norm, chex_norm], ignore_index=True)

    # ------------------------------------------------------------------
    # Patient id harmonization
    # ------------------------------------------------------------------
    # Human annotations use numeric patient_id values from the database
    # (for example: 1, 2, 3), while the VLM CSV files usually use the
    # patient folder/code strings (for example: patient00032).
    #
    # The analysis merges VLMs and humans on BOTH:
    #     patient_id   = numeric database id
    #     patient_code = string folder code
    #
    # Therefore, for VLMs we must detect whether their patient_id column
    # currently contains numeric ids or patient-code strings, then create
    # both columns consistently.
    numeric_col, code_col = infer_patient_map_columns(patient_map)

    patient_map_numeric = patient_map[[numeric_col, code_col]].copy()
    patient_map_numeric.columns = ["patient_id", "patient_code"]

    # Normalized string versions used only for robust matching.
    patient_map_by_code = patient_map_numeric.copy()
    patient_map_by_code["patient_code_key"] = patient_map_by_code["patient_code"].astype(str).str.strip()

    vlm["patient_id_original"] = vlm["patient_id"]
    vlm_patient_as_str = vlm["patient_id"].astype(str).str.strip()

    # If the VLM patient IDs look like patient00032, treat them as patient codes.
    looks_like_patient_code = vlm_patient_as_str.str.lower().str.startswith("patient").mean() > 0.50

    if looks_like_patient_code:
        # VLM patient_id is really the string patient code.
        vlm["patient_code"] = vlm_patient_as_str
        vlm["patient_code_key"] = vlm["patient_code"].astype(str).str.strip()

        vlm = vlm.merge(
            patient_map_by_code[["patient_id", "patient_code_key"]],
            on="patient_code_key",
            how="left",
            suffixes=("", "_mapped"),
        )

        # After the merge, the original VLM column named patient_id still
        # contains the string code. The mapped numeric database id is in
        # patient_id_mapped. Replace patient_id with the numeric id so that
        # it matches the human annotation dataframe.
        if "patient_id_mapped" not in vlm.columns:
            raise ValueError("Internal error: patient_id_mapped was not created during code-based merge.")
        vlm["patient_id"] = vlm["patient_id_mapped"]
        vlm = vlm.drop(columns=["patient_code_key", "patient_id_mapped"])

    else:
        # VLM patient_id is already numeric or numeric-like.
        vlm["patient_id"] = pd.to_numeric(vlm["patient_id"], errors="coerce")
        vlm = vlm.merge(patient_map_numeric, on="patient_id", how="left")

    if vlm["patient_id"].isna().any() or vlm["patient_code"].isna().any():
        missing = (
            vlm[vlm["patient_id"].isna() | vlm["patient_code"].isna()]["patient_id_original"]
            .drop_duplicates()
            .head(10)
            .tolist()
        )
        raise ValueError(
            "Some VLM patient identifiers were not found in the patient map. "
            f"Examples: {missing}"
        )

    # Keep numeric ids as integers so they match the human dataframe.
    vlm["patient_id"] = vlm["patient_id"].astype(int)

    # Add finding_id for easier sorting.
    finding_to_id = {name: idx + 1 for idx, name in enumerate(FINDINGS)}
    vlm["finding_id"] = vlm["finding"].map(finding_to_id)

    if vlm["finding_id"].isna().any():
        unknown = vlm[vlm["finding_id"].isna()]["finding"].drop_duplicates().tolist()
        raise ValueError(f"Unknown finding names in VLM files: {unknown}")

    vlm["finding_id"] = vlm["finding_id"].astype(int)

    return vlm


# ============================================================
# Consensus and evaluation
# ============================================================

def build_human_consensus(human: pd.DataFrame, readers: List[str], consensus_name: str) -> pd.DataFrame:
    """
    Build a majority-vote consensus for selected human readers.

    This is not absolute truth. It is a group-level human agreement context.
    """
    subset = human[human["username"].isin(readers)].copy()
    if subset.empty:
        raise ValueError(f"No human rows found for consensus group: {consensus_name}")

    consensus = (
        subset
        .groupby(["patient_id", "patient_code", "finding_id", "finding"])["answer_ordinal"]
        .apply(majority_vote)
        .reset_index()
        .rename(columns={"answer_ordinal": "consensus_ordinal"})
    )
    consensus["consensus_name"] = consensus_name
    consensus["n_readers_in_consensus"] = len(readers)
    return consensus


def evaluate_vlm_against_human_rows(vlm: pd.DataFrame, human_subset: pd.DataFrame, reference_label: str) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Compare every VLM to each human reader in human_subset.

    Returns:
        overall_metrics
        by_finding_metrics
        confusion_matrices_long
    """
    all_overall = []
    all_by_finding = []
    all_confusions = []

    for vlm_name, vlm_group in vlm.groupby("vlm_name"):
        for username, human_group in human_subset.groupby("username"):
            merged = vlm_group.merge(
                human_group[["patient_id", "patient_code", "finding_id", "finding", "answer_ordinal", "reader_type"]],
                on=["patient_id", "patient_code", "finding_id", "finding"],
                how="inner",
                suffixes=("_vlm", "_human"),
            )

            merged = merged.rename(columns={
                "answer_ordinal_vlm": "vlm_label",
                "answer_ordinal_human": "human_label",
            })

            overall = compute_pairwise_agreement_metrics(
                merged,
                a_col="human_label",
                b_col="vlm_label",
                reference_name=username,
                prediction_name=vlm_name,
            )
            overall["reference_group"] = reference_label
            overall["human_reader"] = username
            overall["human_reader_type"] = human_group["reader_type"].iloc[0]
            overall["vlm_name"] = vlm_name
            all_overall.append(overall)

            for finding, fg in merged.groupby("finding"):
                by_f = compute_pairwise_agreement_metrics(
                    fg,
                    a_col="human_label",
                    b_col="vlm_label",
                    reference_name=username,
                    prediction_name=vlm_name,
                )
                by_f["reference_group"] = reference_label
                by_f["human_reader"] = username
                by_f["human_reader_type"] = human_group["reader_type"].iloc[0]
                by_f["vlm_name"] = vlm_name
                by_f["finding"] = finding
                by_f["finding_id"] = int(fg["finding_id"].iloc[0])
                all_by_finding.append(by_f)

            cm = compute_confusion_matrix_long(
                merged,
                a_col="human_label",
                b_col="vlm_label",
                reference_name=username,
                comparison_name=vlm_name,
                extra_cols={
                    "reference_group": reference_label,
                    "human_reader": username,
                    "vlm_name": vlm_name,
                },
            )
            all_confusions.append(cm)

    return (
        pd.DataFrame(all_overall),
        pd.DataFrame(all_by_finding),
        pd.concat(all_confusions, ignore_index=True) if all_confusions else pd.DataFrame(),
    )


def evaluate_vlm_against_consensus(vlm: pd.DataFrame, consensus: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Compare every VLM to a human majority-consensus context.

    The consensus is used as a comparison label, not as absolute ground truth.
    """
    all_overall = []
    all_by_finding = []
    all_confusions = []

    consensus_name = consensus["consensus_name"].iloc[0]

    for vlm_name, vlm_group in vlm.groupby("vlm_name"):
        merged = vlm_group.merge(
            consensus[["patient_id", "patient_code", "finding_id", "finding", "consensus_ordinal"]],
            on=["patient_id", "patient_code", "finding_id", "finding"],
            how="inner",
        )
        merged = merged.rename(columns={"answer_ordinal": "vlm_label"})

        overall = compute_pairwise_agreement_metrics(
            merged,
            a_col="consensus_ordinal",
            b_col="vlm_label",
            reference_name=consensus_name,
            prediction_name=vlm_name,
        )
        overall["consensus_name"] = consensus_name
        overall["vlm_name"] = vlm_name
        all_overall.append(overall)

        for finding, fg in merged.groupby("finding"):
            by_f = compute_pairwise_agreement_metrics(
                fg,
                a_col="consensus_ordinal",
                b_col="vlm_label",
                reference_name=consensus_name,
                prediction_name=vlm_name,
            )
            by_f["consensus_name"] = consensus_name
            by_f["vlm_name"] = vlm_name
            by_f["finding"] = finding
            by_f["finding_id"] = int(fg["finding_id"].iloc[0])
            all_by_finding.append(by_f)

        cm = compute_confusion_matrix_long(
            merged,
            a_col="consensus_ordinal",
            b_col="vlm_label",
            reference_name=consensus_name,
            comparison_name=vlm_name,
            extra_cols={
                "consensus_name": consensus_name,
                "vlm_name": vlm_name,
            },
        )
        all_confusions.append(cm)

    return (
        pd.DataFrame(all_overall),
        pd.DataFrame(all_by_finding),
        pd.concat(all_confusions, ignore_index=True) if all_confusions else pd.DataFrame(),
    )


def summarize_mean_across_doctors(vlm_vs_each_doctor_overall: pd.DataFrame, vlm_vs_each_doctor_by_finding: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Average VLM agreement metrics across the five non-chief doctors.

    This creates a compact result table:
        one row per VLM
    and:
        one row per VLM per finding
    """
    metric_cols = [
        "percent_agreement",
        "cohen_kappa",
        "quadratic_weighted_kappa",
        "macro_f1_descriptive",
        "mean_absolute_difference",
        "severe_disagreement_percent",
        "reference_absent_percent",
        "reference_uncertain_percent",
        "reference_present_percent",
        "comparison_absent_percent",
        "comparison_uncertain_percent",
        "comparison_present_percent",
    ]

    overall = (
        vlm_vs_each_doctor_overall
        .groupby("vlm_name")[metric_cols]
        .mean()
        .reset_index()
    )
    overall["qwk_level"] = overall["quadratic_weighted_kappa"].apply(agreement_level_kappa)
    overall = overall.sort_values("quadratic_weighted_kappa", ascending=False)

    by_finding = (
        vlm_vs_each_doctor_by_finding
        .groupby(["vlm_name", "finding_id", "finding"])[metric_cols]
        .mean()
        .reset_index()
    )
    by_finding["qwk_level"] = by_finding["quadratic_weighted_kappa"].apply(agreement_level_kappa)
    by_finding = by_finding.sort_values(["finding_id", "quadratic_weighted_kappa"], ascending=[True, False])

    return overall, by_finding


# ============================================================
# VLM-only agreement and contradictions
# ============================================================

def compute_vlm_only_fleiss(vlm: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Compute Fleiss' kappa among the VLM variants only."""
    # Each VLM variant contributes one label per patient/finding.
    item_cols = ["patient_id", "patient_code", "finding_id", "finding"]

    # Ensure one prediction per VLM per item.
    dedup = vlm.drop_duplicates(item_cols + ["vlm_name"], keep="last").copy()

    counts = build_fleiss_count_matrix(dedup, item_cols=item_cols, label_col="answer_ordinal")
    kappa, observed, expected = fleiss_kappa_from_count_matrix(counts)

    overall = pd.DataFrame([{
        "scope": "vlm_only_all_findings",
        "n_items": counts.shape[0],
        "n_vlms": int(counts.sum(axis=1).iloc[0]) if not counts.empty else np.nan,
        "fleiss_kappa": kappa,
        "observed_agreement": observed,
        "expected_chance_agreement": expected,
        "agreement_level": agreement_level_kappa(kappa),
    }])

    rows = []
    for finding, fg in dedup.groupby("finding"):
        f_counts = build_fleiss_count_matrix(fg, item_cols=item_cols, label_col="answer_ordinal")
        fk, fo, fe = fleiss_kappa_from_count_matrix(f_counts)
        rows.append({
            "scope": f"vlm_only_{finding}",
            "finding_id": int(fg["finding_id"].iloc[0]),
            "finding": finding,
            "n_items": f_counts.shape[0],
            "n_vlms": int(f_counts.sum(axis=1).iloc[0]) if not f_counts.empty else np.nan,
            "fleiss_kappa": fk,
            "observed_agreement": fo,
            "expected_chance_agreement": fe,
            "agreement_level": agreement_level_kappa(fk),
        })

    by_finding = pd.DataFrame(rows).sort_values("fleiss_kappa")

    return overall, by_finding


def compute_vlm_vs_vlm_pairwise(vlm: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Compute pairwise agreement between VLM variants."""
    wide = vlm.pivot_table(
        index=["patient_id", "patient_code", "finding_id", "finding"],
        columns="vlm_name",
        values="answer_ordinal",
        aggfunc="last",
    ).reset_index()

    vlm_names = sorted([c for c in wide.columns if c not in ["patient_id", "patient_code", "finding_id", "finding"]])

    rows = []
    cm_rows = []

    for a, b in itertools.combinations(vlm_names, 2):
        metrics = compute_pairwise_agreement_metrics(
            wide,
            a_col=a,
            b_col=b,
            reference_name=a,
            prediction_name=b,
        )
        metrics["vlm_a"] = a
        metrics["vlm_b"] = b
        rows.append(metrics)

        cm = compute_confusion_matrix_long(
            wide,
            a_col=a,
            b_col=b,
            reference_name=a,
            comparison_name=b,
            extra_cols={"vlm_a": a, "vlm_b": b},
        )
        cm_rows.append(cm)

    return pd.DataFrame(rows), pd.concat(cm_rows, ignore_index=True) if cm_rows else pd.DataFrame()


def compute_vlm_no_finding_contradictions(vlm: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Detect VLM cases where No Finding is present but some other finding is
    uncertain or present.

    This rule is NOT used to change the VLM predictions.
    It only measures internal contradiction.
    """
    detail_rows = []

    for (vlm_name, patient_id, patient_code), group in vlm.groupby(["vlm_name", "patient_id", "patient_code"]):
        no_finding = group[group["finding"] == "No Finding"]
        if no_finding.empty:
            continue

        nf_label = int(no_finding["answer_ordinal"].iloc[0])
        if nf_label != 2:
            continue

        other = group[group["finding"] != "No Finding"].copy()
        contradictory = other[other["answer_ordinal"].isin([1, 2])].copy()
        if contradictory.empty:
            continue

        detail_rows.append({
            "vlm_name": vlm_name,
            "patient_id": patient_id,
            "patient_code": patient_code,
            "no_finding_label": nf_label,
            "n_contradictory_other_findings": len(contradictory),
            "contradictory_findings": "; ".join(
                f"{row.finding}={LABEL_NAMES[int(row.answer_ordinal)]}"
                for row in contradictory.itertuples(index=False)
            ),
        })

    detail = pd.DataFrame(detail_rows)

    summary_rows = []
    for vlm_name, group in vlm.groupby("vlm_name"):
        total_patients = group["patient_id"].nunique()
        nf_present_patients = (
            group[(group["finding"] == "No Finding") & (group["answer_ordinal"] == 2)]
            ["patient_id"]
            .nunique()
        )
        contradictions = 0 if detail.empty else detail[detail["vlm_name"] == vlm_name]["patient_id"].nunique()
        contradiction_rate_all = contradictions / total_patients * 100 if total_patients else np.nan
        contradiction_rate_nf_present = contradictions / nf_present_patients * 100 if nf_present_patients else np.nan
        summary_rows.append({
            "vlm_name": vlm_name,
            "total_patients": total_patients,
            "no_finding_present_patients": nf_present_patients,
            "contradictory_patients": contradictions,
            "contradiction_rate_all_patients_percent": contradiction_rate_all,
            "contradiction_rate_among_no_finding_present_percent": contradiction_rate_nf_present,
        })

    summary = pd.DataFrame(summary_rows).sort_values("contradiction_rate_all_patients_percent", ascending=False)
    return summary, detail


def compute_chexagent_parse_summary(vlm: pd.DataFrame) -> pd.DataFrame:
    """Summarize CheXagent parse statuses if available."""
    chex = vlm[vlm["model_family"] == "CheXagent"].copy()
    if chex.empty or "parse_status" not in chex.columns:
        return pd.DataFrame()

    summary = (
        chex
        .groupby("parse_status")
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
    )
    summary["percent"] = summary["count"] / summary["count"].sum() * 100
    return summary


# ============================================================
# Plots
# ============================================================

def save_bar_plot(df: pd.DataFrame, x_col: str, y_col: str, title: str, xlabel: str, ylabel: str, output_path: str, sort_desc: bool = True) -> None:
    """Save a simple thesis-friendly horizontal bar plot."""
    if df.empty or x_col not in df.columns or y_col not in df.columns:
        return

    plot_df = df[[x_col, y_col]].dropna().copy()
    plot_df = plot_df.sort_values(y_col, ascending=not sort_desc)

    plt.figure(figsize=(10, max(4, 0.45 * len(plot_df))))
    plt.barh(plot_df[x_col].astype(str), plot_df[y_col])
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(axis="x", linestyle="--", alpha=0.4)
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()


def save_heatmap_from_pivot(pivot: pd.DataFrame, title: str, output_path: str) -> None:
    """Save a simple heatmap using matplotlib only."""
    if pivot.empty:
        return

    values = pivot.to_numpy(dtype=float)

    plt.figure(figsize=(max(8, 0.65 * len(pivot.columns)), max(5, 0.4 * len(pivot.index))))
    plt.imshow(values, aspect="auto")
    plt.colorbar(label="Quadratic weighted kappa")
    plt.xticks(range(len(pivot.columns)), pivot.columns, rotation=45, ha="right")
    plt.yticks(range(len(pivot.index)), pivot.index)
    plt.title(title)
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()


def make_plots(output_dir: str, tables: Dict[str, pd.DataFrame]) -> None:
    """Create a small set of high-value plots."""
    plots_dir = os.path.join(output_dir, "plots")
    ensure_dir(plots_dir)

    save_bar_plot(
        tables.get("vlm_vs_doctors_mean_overall", pd.DataFrame()),
        x_col="vlm_name",
        y_col="quadratic_weighted_kappa",
        title="VLM agreement with doctors, averaged across non-chief radiologists",
        xlabel="Mean quadratic weighted kappa",
        ylabel="VLM",
        output_path=os.path.join(plots_dir, "vlm_vs_doctors_mean_qwk_overall.png"),
    )

    save_bar_plot(
        tables.get("vlm_vs_chief_overall", pd.DataFrame()),
        x_col="vlm_name",
        y_col="quadratic_weighted_kappa",
        title="VLM agreement with chief radiologist",
        xlabel="Quadratic weighted kappa",
        ylabel="VLM",
        output_path=os.path.join(plots_dir, "vlm_vs_chief_qwk_overall.png"),
    )

    save_bar_plot(
        tables.get("vlm_vs_nonchief_consensus_overall", pd.DataFrame()),
        x_col="vlm_name",
        y_col="quadratic_weighted_kappa",
        title="VLM agreement with non-chief doctor majority consensus",
        xlabel="Quadratic weighted kappa",
        ylabel="VLM",
        output_path=os.path.join(plots_dir, "vlm_vs_nonchief_consensus_qwk_overall.png"),
    )

    save_bar_plot(
        tables.get("vlm_only_fleiss_by_finding", pd.DataFrame()),
        x_col="finding",
        y_col="fleiss_kappa",
        title="Agreement among VLM variants by finding",
        xlabel="Fleiss' kappa",
        ylabel="Finding",
        output_path=os.path.join(plots_dir, "vlm_only_fleiss_by_finding.png"),
        sort_desc=False,
    )

    contradictions = tables.get("vlm_no_finding_contradictions_summary", pd.DataFrame())
    save_bar_plot(
        contradictions,
        x_col="vlm_name",
        y_col="contradiction_rate_all_patients_percent",
        title="VLM No Finding contradiction rate",
        xlabel="Contradictory patients (%)",
        ylabel="VLM",
        output_path=os.path.join(plots_dir, "vlm_no_finding_contradiction_rate.png"),
    )

    # Heatmap: VLM vs doctors mean QWK by finding.
    by_f = tables.get("vlm_vs_doctors_mean_by_finding", pd.DataFrame())
    if not by_f.empty:
        pivot = by_f.pivot_table(
            index="finding",
            columns="vlm_name",
            values="quadratic_weighted_kappa",
            aggfunc="mean",
        )
        # Keep findings in canonical order if present.
        pivot = pivot.reindex([f for f in FINDINGS if f in pivot.index])
        save_heatmap_from_pivot(
            pivot,
            title="VLM agreement with doctors by finding",
            output_path=os.path.join(plots_dir, "vlm_vs_doctors_mean_qwk_by_finding_heatmap.png"),
        )


# ============================================================
# Main script
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze agreement between VLMs and radiologists.")

    parser.add_argument("--patient_answers", required=True, help="Path to PATIENT ANSWERS.xlsx")
    parser.add_argument("--patients_with_id", required=True, help="Path to patients with ID.xlsx")
    parser.add_argument("--clip_results", required=True, help="Path to CLIP result CSV")
    parser.add_argument("--biovil_results", required=True, help="Path to BioViL result CSV")
    parser.add_argument("--chexagent_results", required=True, help="Path to CheXagent result CSV")
    parser.add_argument("--output_dir", default="vlm_agreement_outputs", help="Output folder")
    parser.add_argument("--make_plots", action="store_true", help="Create PNG plots")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_dir(args.output_dir)

    print("\n============================================================")
    print("VLM-Radiologist Agreement Analysis")
    print("============================================================")
    print(f"Output directory: {args.output_dir}")
    print("No single radiologist is treated as absolute ground truth.")
    print("Chief, doctors, and consensus labels are agreement contexts.\n")

    # -------------------------
    # Load data
    # -------------------------
    human = load_human_annotations(args.patient_answers, args.patients_with_id)
    vlm = load_vlm_predictions(args.clip_results, args.biovil_results, args.chexagent_results, args.patients_with_id)

    print("Human annotations used")
    print("----------------------")
    print("Rows:", len(human))
    print("Patients:", human["patient_id"].nunique())
    print("Findings:", human["finding_id"].nunique())
    print("Readers:", sorted(human["username"].unique()))
    print("Rows adjusted by No Finding rule in memory:", int(human["adjusted_by_no_finding_rule"].sum()))

    print("\nVLM predictions used")
    print("--------------------")
    print("Rows:", len(vlm))
    print("Patients:", vlm["patient_id"].nunique())
    print("Findings:", vlm["finding_id"].nunique())
    print(vlm["vlm_name"].value_counts().sort_index())

    # Save cleaned data and audits.
    human.to_csv(os.path.join(args.output_dir, "clean_human_annotations_used.csv"), index=False)
    vlm.to_csv(os.path.join(args.output_dir, "clean_vlm_predictions_used.csv"), index=False)

    dup = human.attrs.get("duplicate_rows_removed", pd.DataFrame())
    if isinstance(dup, pd.DataFrame):
        dup.to_csv(os.path.join(args.output_dir, "duplicate_human_rows_removed.csv"), index=False)

    human[human["adjusted_by_no_finding_rule"]].to_csv(
        os.path.join(args.output_dir, "human_rows_adjusted_by_no_finding_rule_in_memory.csv"),
        index=False,
    )

    # -------------------------
    # Build human groups
    # -------------------------
    chief = human[human["username"] == CHIEF_USERNAME].copy()
    doctors = human[human["username"].isin(NONCHIEF_DOCTOR_USERNAMES)].copy()
    all_humans = human[human["username"].isin(ALL_HUMAN_USERNAMES)].copy()

    if chief.empty:
        print("WARNING: No chief rows found. Chief agreement outputs will be empty.")
    if doctors.empty:
        raise ValueError("No non-chief doctor rows found.")

    nonchief_consensus = build_human_consensus(human, NONCHIEF_DOCTOR_USERNAMES, "nonchief_doctor_majority_consensus")
    all_human_consensus = build_human_consensus(human, ALL_HUMAN_USERNAMES, "all_human_majority_consensus")

    nonchief_consensus.to_csv(os.path.join(args.output_dir, "nonchief_doctor_majority_consensus.csv"), index=False)
    all_human_consensus.to_csv(os.path.join(args.output_dir, "all_human_majority_consensus.csv"), index=False)

    # -------------------------
    # VLM vs each human reader
    # -------------------------
    vlm_vs_each_human_overall, vlm_vs_each_human_by_finding, vlm_vs_each_human_cm = evaluate_vlm_against_human_rows(
        vlm=vlm,
        human_subset=all_humans,
        reference_label="each_human_reader",
    )

    vlm_vs_each_doctor_overall = vlm_vs_each_human_overall[vlm_vs_each_human_overall["human_reader_type"] == "doctor"].copy()
    vlm_vs_each_doctor_by_finding = vlm_vs_each_human_by_finding[vlm_vs_each_human_by_finding["human_reader_type"] == "doctor"].copy()
    vlm_vs_each_doctor_cm = vlm_vs_each_human_cm[vlm_vs_each_human_cm["human_reader"].isin(NONCHIEF_DOCTOR_USERNAMES)].copy()

    vlm_vs_chief_overall = vlm_vs_each_human_overall[vlm_vs_each_human_overall["human_reader"] == CHIEF_USERNAME].copy()
    vlm_vs_chief_by_finding = vlm_vs_each_human_by_finding[vlm_vs_each_human_by_finding["human_reader"] == CHIEF_USERNAME].copy()
    vlm_vs_chief_cm = vlm_vs_each_human_cm[vlm_vs_each_human_cm["human_reader"] == CHIEF_USERNAME].copy()

    # Mean across non-chief doctors.
    vlm_vs_doctors_mean_overall, vlm_vs_doctors_mean_by_finding = summarize_mean_across_doctors(
        vlm_vs_each_doctor_overall,
        vlm_vs_each_doctor_by_finding,
    )

    # -------------------------
    # VLM vs consensus contexts
    # -------------------------
    vlm_vs_nonchief_consensus_overall, vlm_vs_nonchief_consensus_by_finding, vlm_vs_nonchief_consensus_cm = evaluate_vlm_against_consensus(
        vlm,
        nonchief_consensus,
    )
    vlm_vs_nonchief_consensus_overall = vlm_vs_nonchief_consensus_overall.sort_values("quadratic_weighted_kappa", ascending=False)

    vlm_vs_all_human_consensus_overall, vlm_vs_all_human_consensus_by_finding, vlm_vs_all_human_consensus_cm = evaluate_vlm_against_consensus(
        vlm,
        all_human_consensus,
    )
    vlm_vs_all_human_consensus_overall = vlm_vs_all_human_consensus_overall.sort_values("quadratic_weighted_kappa", ascending=False)

    # -------------------------
    # VLM-only agreement and contradictions
    # -------------------------
    vlm_only_fleiss_overall, vlm_only_fleiss_by_finding = compute_vlm_only_fleiss(vlm)
    vlm_vs_vlm_pairwise_overall, vlm_vs_vlm_pairwise_cm = compute_vlm_vs_vlm_pairwise(vlm)
    vlm_no_finding_contradictions_summary, vlm_no_finding_contradictions_detail = compute_vlm_no_finding_contradictions(vlm)
    chexagent_parse_status_summary = compute_chexagent_parse_summary(vlm)

    # -------------------------
    # Compact summary table for paper
    # -------------------------
    summary_tables = []

    tmp = vlm_vs_doctors_mean_overall.copy()
    tmp["context"] = "mean_agreement_with_nonchief_doctors"
    summary_tables.append(tmp)

    tmp = vlm_vs_chief_overall.copy()
    tmp["context"] = "agreement_with_chief"
    summary_tables.append(tmp)

    tmp = vlm_vs_nonchief_consensus_overall.copy()
    tmp["context"] = "agreement_with_nonchief_doctor_consensus"
    summary_tables.append(tmp)

    tmp = vlm_vs_all_human_consensus_overall.copy()
    tmp["context"] = "agreement_with_all_human_consensus"
    summary_tables.append(tmp)

    summary_vlm_human_agreement_contexts = pd.concat(summary_tables, ignore_index=True)

    # -------------------------
    # Save outputs
    # -------------------------
    outputs = {
        "vlm_vs_each_human_overall": vlm_vs_each_human_overall,
        "vlm_vs_each_human_by_finding": vlm_vs_each_human_by_finding,
        "vlm_vs_each_human_confusion_matrices_long": vlm_vs_each_human_cm,

        "vlm_vs_each_doctor_overall": vlm_vs_each_doctor_overall,
        "vlm_vs_each_doctor_by_finding": vlm_vs_each_doctor_by_finding,
        "vlm_vs_each_doctor_confusion_matrices_long": vlm_vs_each_doctor_cm,

        "vlm_vs_doctors_mean_overall": vlm_vs_doctors_mean_overall,
        "vlm_vs_doctors_mean_by_finding": vlm_vs_doctors_mean_by_finding,

        "vlm_vs_chief_overall": vlm_vs_chief_overall,
        "vlm_vs_chief_by_finding": vlm_vs_chief_by_finding,
        "vlm_vs_chief_confusion_matrices_long": vlm_vs_chief_cm,

        "vlm_vs_nonchief_consensus_overall": vlm_vs_nonchief_consensus_overall,
        "vlm_vs_nonchief_consensus_by_finding": vlm_vs_nonchief_consensus_by_finding,
        "vlm_vs_nonchief_consensus_confusion_matrices_long": vlm_vs_nonchief_consensus_cm,

        "vlm_vs_all_human_consensus_overall": vlm_vs_all_human_consensus_overall,
        "vlm_vs_all_human_consensus_by_finding": vlm_vs_all_human_consensus_by_finding,
        "vlm_vs_all_human_consensus_confusion_matrices_long": vlm_vs_all_human_consensus_cm,

        "vlm_only_fleiss_overall": vlm_only_fleiss_overall,
        "vlm_only_fleiss_by_finding": vlm_only_fleiss_by_finding,

        "vlm_vs_vlm_pairwise_overall": vlm_vs_vlm_pairwise_overall,
        "vlm_vs_vlm_confusion_matrices_long": vlm_vs_vlm_pairwise_cm,

        "vlm_no_finding_contradictions_summary": vlm_no_finding_contradictions_summary,
        "vlm_no_finding_contradictions_detail": vlm_no_finding_contradictions_detail,

        "chexagent_parse_status_summary": chexagent_parse_status_summary,
        "summary_vlm_human_agreement_contexts": summary_vlm_human_agreement_contexts,
    }

    for name, table in outputs.items():
        table.to_csv(os.path.join(args.output_dir, f"{name}.csv"), index=False)

    if args.make_plots:
        make_plots(args.output_dir, outputs)

    # Save a JSON validation summary.
    validation = {
        "human_rows": int(len(human)),
        "human_patients": int(human["patient_id"].nunique()),
        "human_findings": int(human["finding_id"].nunique()),
        "human_readers": sorted(human["username"].unique().tolist()),
        "rows_adjusted_by_no_finding_rule_in_memory": int(human["adjusted_by_no_finding_rule"].sum()),
        "vlm_rows": int(len(vlm)),
        "vlm_patients": int(vlm["patient_id"].nunique()),
        "vlm_findings": int(vlm["finding_id"].nunique()),
        "vlm_names": sorted(vlm["vlm_name"].unique().tolist()),
        "rows_per_vlm": {str(k): int(v) for k, v in vlm["vlm_name"].value_counts().sort_index().items()},
    }

    with open(os.path.join(args.output_dir, "validation_summary.json"), "w", encoding="utf-8") as f:
        json.dump(validation, f, indent=2)

    # Print the most important result tables to the console.
    print("\nMain result: VLM agreement with non-chief doctors, averaged")
    print("----------------------------------------------------------")
    if not vlm_vs_doctors_mean_overall.empty:
        cols = ["vlm_name", "percent_agreement", "quadratic_weighted_kappa", "cohen_kappa", "macro_f1_descriptive", "severe_disagreement_percent"]
        print(vlm_vs_doctors_mean_overall[cols].to_string(index=False))

    print("\nMain result: VLM agreement with non-chief doctor consensus")
    print("---------------------------------------------------------")
    if not vlm_vs_nonchief_consensus_overall.empty:
        cols = ["vlm_name", "percent_agreement", "quadratic_weighted_kappa", "cohen_kappa", "macro_f1_descriptive", "severe_disagreement_percent"]
        print(vlm_vs_nonchief_consensus_overall[cols].to_string(index=False))

    print("\nNo Finding contradiction summary")
    print("--------------------------------")
    if not vlm_no_finding_contradictions_summary.empty:
        print(vlm_no_finding_contradictions_summary.to_string(index=False))
    else:
        print("No contradictions found or no No Finding present cases detected.")

    print("\nSaved outputs to:")
    print(args.output_dir)
    print("\nDone.")


if __name__ == "__main__":
    main()
