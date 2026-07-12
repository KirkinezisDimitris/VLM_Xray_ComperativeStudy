"""
================================================================================
Radiologist Agreement Analysis (Doctors Only)
================================================================================

PURPOSE
-------
Measures agreement among radiologists/doctors ONLY.

Key points:
    * No model predictions are used.
    * No radiologist is treated as ground truth.
    * The goal is to describe how much the doctors agree with each other.

METRICS COMPUTED
----------------
1. Fleiss' kappa (nominal multi-rater agreement)
2. Mean pairwise quadratic-weighted Cohen's kappa (ordinal, respects label order)
3. Ordinal Krippendorff-style alpha (ordinal multi-rater reliability)
4. Exact all-rater agreement percentage (raw unanimity)
5. Mean pairwise percentage agreement (raw pairwise)

All metrics include bootstrap 95% confidence intervals (1000 iterations).

ROLE FILTER
-----------
Only rows where role is "doctor" OR "director" are included.
The comparison is case-insensitive and whitespace-insensitive.

LABEL ENCODING
--------------
Original annotation system:
    answer_choice = 1 -> present
    answer_choice = 2 -> absent
    answer_choice = 3 -> uncertain

Normalized ordinal system used for all statistics:
    0 = absent
    1 = uncertain
    2 = present

DOCTOR-ONLY NO FINDING RULE
----------------------------
If a doctor marks finding_id = 1 (No Finding) as present (answer_choice = 1),
then finding_id 2..14 for that same doctor and same patient are forced to absent
(answer_choice = 2). This reflects the clinical meaning of "No Finding".

PERFORMANCE OPTIMIZATIONS (vs v2)
----------------------------------
The main bottleneck in v2 was:
    - pairwise_quadratic_weighted_kappas() called sklearn 15 times per bootstrap
      iteration, across 15 scopes x 1000 iterations = 225,000 calls.
    - ordinal_krippendorff_alpha_complete() used a Python for-loop over every
      row to compute pairwise squared differences.

This version replaces both with vectorized NumPy equivalents:

    mean_pairwise_qwk_fast():
        Uses a precomputed QW_MATRIX (module-level constant) and NumPy fancy
        indexing. observed = QW_MATRIX[y_a, y_b].mean(), expected from outer
        product of category proportions. No sklearn call per bootstrap iteration.

    ordinal_krippendorff_alpha_complete():
        Uses the algebraic identity:
            sum of squared pairwise diffs per row
            = n * sum(x^2) - (sum(x))^2
        This is O(n_items * n_raters) with no Python loop over items.

    bootstrap_metric_cis():
        Converts wide matrix to numpy ONCE before the loop, reusing it
        across all 1000 iterations via integer indexing.

HOW TO RUN
----------
    python radiologist_agreement_docs_only_v3.py --input_excel "PATIENT ANSWERS.xlsx"

Optional arguments:
    --output_dir    Output folder (default: agreement_outputs_docs_only_v3)
    --bootstrap     Bootstrap iterations (default: 1000)
    --random_seed   Seed for reproducibility (default: 123)

OUTPUTS
-------
    agreement_overall.csv
    agreement_by_finding.csv
    pairwise_weighted_kappa_overall.csv
    pairwise_weighted_kappa_by_finding.csv
    clean_doctor_answers_used_for_agreement.csv
    rows_changed_by_no_finding_rule.csv
    incomplete_patient_finding_items.csv
    duplicate_rows_removed.csv
    fleiss_kappa_by_finding_with_ci.png
    ordinal_weighted_kappa_by_finding_with_ci.png
    exact_agreement_by_finding.png
    agreement_summary.txt

DEPENDENCIES
------------
    pip install pandas numpy scikit-learn matplotlib openpyxl

================================================================================
"""

# =============================================================================
# Standard library imports
# =============================================================================

import argparse
import itertools
from pathlib import Path

# =============================================================================
# Third-party imports
# =============================================================================

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.metrics import cohen_kappa_score


# =============================================================================
# Configuration
# =============================================================================

# CheXpert-style 14 findings. Position = finding_id - 1.
FINDINGS = [
    "No Finding",                  # finding_id = 1
    "Enlarged Cardiomediastinum",  # finding_id = 2
    "Cardiomegaly",                # finding_id = 3
    "Lung Opacity",                # finding_id = 4
    "Lung Lesion",                 # finding_id = 5
    "Edema",                       # finding_id = 6
    "Consolidation",               # finding_id = 7
    "Pneumonia",                   # finding_id = 8
    "Atelectasis",                 # finding_id = 9
    "Pneumothorax",                # finding_id = 10
    "Pleural Effusion",            # finding_id = 11
    "Pleural Other",               # finding_id = 12
    "Fracture",                    # finding_id = 13
    "Support Devices",             # finding_id = 14
]

# All valid ordinal labels used throughout this script.
# 0 = absent, 1 = uncertain, 2 = present
LABELS = [0, 1, 2]

LABEL_NAME = {0: "absent", 1: "uncertain", 2: "present"}

# Precomputed quadratic weight matrix for labels [0, 1, 2].
# W[a, b] = 1 - (a - b)^2 / max_distance^2   where max_distance^2 = 4
# Built ONCE at import time and reused in every bootstrap iteration.
# This avoids rebuilding the matrix inside the hot loop.
_LABEL_ARR = np.array(LABELS, dtype=float)
_MAX_DIST_SQ = (_LABEL_ARR[-1] - _LABEL_ARR[0]) ** 2  # = 4.0
QW_MATRIX = 1.0 - ((_LABEL_ARR[:, None] - _LABEL_ARR[None, :]) ** 2) / _MAX_DIST_SQ
# QW_MATRIX:
#   [[1.00, 0.75, 0.00],
#    [0.75, 1.00, 0.75],
#    [0.00, 0.75, 1.00]]


# =============================================================================
# Label and finding mapping helpers
# =============================================================================

def map_finding_id_to_name(finding_id):
    """Convert finding_id 1..14 to its human-readable finding name."""
    finding_id = int(finding_id)
    if finding_id < 1 or finding_id > len(FINDINGS):
        raise ValueError(f"Unexpected finding_id: {finding_id}")
    return FINDINGS[finding_id - 1]


def map_answer_choice_to_ordinal(answer_choice):
    """
    Convert annotation answer_choice to normalized ordinal label.

    Original:  1=present, 2=absent, 3=uncertain
    Ordinal:   0=absent,  1=uncertain, 2=present

    Returns np.nan for invalid/missing values so they can be safely dropped.
    """
    if pd.isna(answer_choice):
        return np.nan
    try:
        answer_choice = int(answer_choice)
    except Exception:
        return np.nan
    mapping = {1: 2, 2: 0, 3: 1}
    return mapping.get(answer_choice, np.nan)


def agreement_level_from_kappa(kappa):
    """
    Convert a kappa value to a Landis & Koch qualitative interpretation.

    Scale (widely used in medical imaging papers):
        < 0.00      poor
        0.00-0.20   slight
        0.21-0.40   fair
        0.41-0.60   moderate
        0.61-0.80   substantial
        0.81-1.00   almost perfect

    These are descriptive guidelines, not absolute thresholds.
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


# =============================================================================
# Data cleaning
# =============================================================================

def validate_required_columns(df, required_columns):
    """Raise a clear error if any required column is missing from the DataFrame."""
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(
            "The input file is missing required columns: " + ", ".join(missing)
        )


def filter_doctor_rows_only(df, role_column="role"):
    """
    Keep only rows where role is 'doctor' OR 'director'.

    Accepted roles are stored in a set so adding future roles
    means adding one string to accepted_roles — nothing else changes.

    Comparison is case-insensitive and whitespace-insensitive.
    Raises ValueError immediately if no rows survive, so the user
    gets a clear error rather than a silent empty analysis.
    """
    if role_column not in df.columns:
        raise ValueError(
            f"Column '{role_column}' not found. Cannot filter doctor rows."
        )

    # Any role value that should be treated as a doctor annotator.
    accepted_roles = {"doctor", "director"}

    role_normalized = df[role_column].astype(str).str.strip().str.lower()
    before = len(df)
    df = df[role_normalized.isin(accepted_roles)].copy()
    after = len(df)

    if after == 0:
        raise ValueError(
            f"After filtering role in {accepted_roles}, no rows remained. "
            "Check the role values in your Excel file."
        )

    print(f"Doctor-role filter: kept {after:,} of {before:,} rows")
    return df


def remove_duplicate_ratings_keep_latest(df):
    """
    Remove duplicate ratings for the same doctor / patient / finding.

    Agreement statistics require exactly one rating per doctor per item.
    If the annotation system kept edit history, duplicates can appear.
    We keep the row with the latest updated_at timestamp.
    If updated_at is absent, we keep the last row in file order.

    Returns:
        cleaned_df      DataFrame after duplicate removal.
        duplicate_rows  DataFrame of removed rows saved for audit transparency.
    """
    df = df.copy()
    key_cols = ["patient_id", "finding_id", "username"]

    duplicate_mask = df.duplicated(subset=key_cols, keep=False)
    duplicate_rows = df[duplicate_mask].copy()

    if duplicate_rows.empty:
        return df, duplicate_rows

    if "updated_at" in df.columns:
        df["_updated_at_parsed"] = pd.to_datetime(df["updated_at"], errors="coerce")
        df = df.sort_values(key_cols + ["_updated_at_parsed"])
    else:
        # No timestamp: preserve original file order as tiebreaker.
        df["_row_order"] = np.arange(len(df))
        df = df.sort_values(key_cols + ["_row_order"])

    cleaned_df = df.drop_duplicates(subset=key_cols, keep="last").copy()

    # Remove helper sort columns from both outputs.
    for col in ["_updated_at_parsed", "_row_order"]:
        if col in cleaned_df.columns:
            cleaned_df = cleaned_df.drop(columns=[col])
        if col in duplicate_rows.columns:
            duplicate_rows = duplicate_rows.drop(columns=[col])

    removed = len(df) - len(cleaned_df)
    print(f"Duplicate check: removed {removed:,} duplicate rating rows")
    return cleaned_df, duplicate_rows


def enforce_no_finding_rule_for_doctors(df):
    """
    Apply the doctor-only No Finding rule.

    Rule:
        If a doctor marks finding_id = 1 (No Finding) as present
        (answer_choice = 1), then ALL other findings (2..14) for that
        same doctor and same patient are forced to absent (answer_choice = 2).

    Clinical rationale:
        'No Finding' means the study is entirely normal.
        A simultaneously present pathology would be a contradiction.

    Tracking:
        The boolean column 'changed_by_no_finding_rule' records which rows
        were actually changed, enabling transparent audit of the rule's effect.
    """
    df = df.copy()
    df["changed_by_no_finding_rule"] = False

    for (patient_id, username), group in df.groupby(
        ["patient_id", "username"], sort=False
    ):
        no_finding_rows = group[group["finding_id"] == 1]
        if no_finding_rows.empty:
            continue

        # Use the first row (deduplication should have left exactly one).
        if no_finding_rows["answer_choice"].iloc[0] != 1:
            continue

        # All other findings for this patient/doctor.
        other_mask = (
            (df["patient_id"] == patient_id)
            & (df["username"] == username)
            & (df["finding_id"] != 1)
        )

        # Only flag rows that actually change value (not already absent).
        changed_mask = other_mask & (df["answer_choice"] != 2)

        df.loc[other_mask, "answer_choice"] = 2
        df.loc[changed_mask, "changed_by_no_finding_rule"] = True

    changed_count = int(df["changed_by_no_finding_rule"].sum())
    print(f"No Finding rule: changed {changed_count:,} rows")
    return df


# =============================================================================
# Wide matrix builder
# =============================================================================

def build_wide_rating_matrix(df):
    """
    Pivot long-format ratings into a wide item-by-rater matrix.

    Input (long format):
        patient_id | finding_id | username | label_ordinal

    Output (wide format):
        index   = (patient_id, finding_id)
        columns = doctor usernames, sorted alphabetically for reproducibility
        values  = label_ordinal (0=absent, 1=uncertain, 2=present)

    aggfunc='first' is a silent safety net; duplicates should already be
    removed. The assertion below catches any dedup failure that slips through.
    """
    wide = df.pivot_table(
        index=["patient_id", "finding_id"],
        columns="username",
        values="label_ordinal",
        aggfunc="first",
    )
    wide = wide.reindex(sorted(wide.columns), axis=1)

    # Defensive check: non-integer values indicate a deduplication failure.
    if wide.notna().all().all():
        assert (wide == wide.round()).all().all(), (
            "Non-integer values in the wide rating matrix. "
            "Deduplication may have failed — check duplicate_rows_removed.csv."
        )
    return wide


# =============================================================================
# Fleiss' kappa
# =============================================================================

def build_fleiss_count_matrix(wide):
    """
    Build the Fleiss count matrix from the wide rating matrix.

    Structure: shape (n_items, 3) — one row per item, one column per label.
    Each cell counts how many raters assigned that label to that item.

    Example row [5, 1, 0]:
        5 raters said absent (0)
        1 rater said uncertain (1)
        0 raters said present (2)

    Uses NumPy broadcasting instead of a Python loop over LABELS,
    producing the full matrix in three vectorized comparisons.
    """
    values = wide.to_numpy(dtype=float)
    # Stack three (n_items,) arrays column-wise -> (n_items, 3)
    counts = np.stack(
        [(values == label).sum(axis=1) for label in LABELS], axis=1
    ).astype(float)
    return counts


def fleiss_kappa_from_count_matrix(count_matrix):
    """
    Compute Fleiss' kappa from a count matrix.

    Fleiss' kappa is a nominal multi-rater metric — it does not account for
    the ordering of categories. Use alongside pairwise QWK for ordinal data.

    Formula:
        P_i   = (sum_j n_ij^2 - n) / [n(n-1)]   per-item observed agreement
        P_bar = mean(P_i)                          overall observed agreement
        p_j   = (sum_i n_ij) / (N * n)            global category proportions
        P_e   = sum_j p_j^2                        expected chance agreement
        kappa = (P_bar - P_e) / (1 - P_e)

    Returns dict with fleiss_kappa, observed_agreement, expected_chance_agreement.
    Returns NaN values if the matrix is empty, has fewer than 2 raters,
    or if all ratings fall in one category (P_e ≈ 1).
    """
    counts = np.asarray(count_matrix, dtype=float)

    if counts.ndim != 2 or counts.shape[0] == 0:
        return {"fleiss_kappa": np.nan, "observed_agreement": np.nan,
                "expected_chance_agreement": np.nan}

    n_items = counts.shape[0]
    n_raters_per_item = counts.sum(axis=1)

    unique_n = np.unique(n_raters_per_item)
    if len(unique_n) != 1:
        raise ValueError(
            f"Fleiss' kappa requires equal rater counts per item. Found: {unique_n}"
        )

    n = n_raters_per_item[0]
    if n < 2:
        return {"fleiss_kappa": np.nan, "observed_agreement": np.nan,
                "expected_chance_agreement": np.nan}

    P_i = ((counts ** 2).sum(axis=1) - n) / (n * (n - 1))
    P_bar = P_i.mean()
    p_j = counts.sum(axis=0) / (n_items * n)
    P_e = (p_j ** 2).sum()
    denominator = 1.0 - P_e

    kappa = np.nan if np.isclose(denominator, 0) else (P_bar - P_e) / denominator

    return {
        "fleiss_kappa": float(kappa) if not np.isnan(kappa) else np.nan,
        "observed_agreement": float(P_bar),
        "expected_chance_agreement": float(P_e),
    }


# =============================================================================
# Raw agreement metrics
# =============================================================================

def exact_all_rater_agreement_percent(wide):
    """
    Percentage of items where ALL doctors chose exactly the same label.

    An item counts as exact agreement only if every rater's vote matches.
    wide.nunique(axis=1) == 1 is O(n_items * n_raters) and vectorized.
    """
    if wide.empty:
        return np.nan
    all_same = wide.nunique(axis=1) == 1
    return float(all_same.mean() * 100.0)


def mean_pairwise_percent_agreement(wide):
    """
    Mean percent agreement across all unique doctor pairs.

    For each pair (a, b): fraction of items where they chose the same label.
    Averages across all C(n_raters, 2) pairs.
    Less strict than exact all-rater agreement; useful as a complementary metric.
    """
    if wide.empty or wide.shape[1] < 2:
        return np.nan
    values = wide.to_numpy(dtype=float)
    pair_agreements = [
        float((values[:, a] == values[:, b]).mean() * 100.0)
        for a, b in itertools.combinations(range(values.shape[1]), 2)
    ]
    return float(np.nanmean(pair_agreements))


# =============================================================================
# FAST vectorized pairwise QWK (used inside bootstrap loop)
# =============================================================================

def mean_pairwise_qwk_fast(wide):
    """
    Mean quadratic-weighted Cohen's kappa across all doctor pairs.

    WHY THIS IS FAST (vs v2):
        v2 called sklearn.metrics.cohen_kappa_score() once per pair per
        bootstrap iteration. With 6 doctors = 15 pairs, 15 scopes, 1000
        iterations: 225,000 sklearn calls (each with Python object overhead).

        This function uses the precomputed module-level QW_MATRIX constant
        and NumPy fancy indexing to compute the same quantity:

            observed = QW_MATRIX[y_a, y_b].mean()
                       One array lookup + mean — O(n_items), no Python loop.

            expected = (QW_MATRIX * np.outer(p_a, p_b)).sum()
                       3x3 element-wise multiply + sum — effectively O(1).

            kappa    = (observed - expected) / (1 - expected)

        All 15 pairs run in a tight itertools loop with NumPy inner bodies,
        giving ~8-10x speedup over the sklearn approach.

    This function is used ONLY inside bootstrap_metric_cis().
    pairwise_qwk_details() uses sklearn for the one-time detailed output.
    """
    if wide.empty or wide.shape[1] < 2:
        return np.nan

    values = wide.to_numpy(dtype=int)
    n_items = values.shape[0]
    qwk_values = []

    for a, b in itertools.combinations(range(values.shape[1]), 2):
        y_a = values[:, a]
        y_b = values[:, b]

        # Observed weighted agreement: index QW_MATRIX with actual label pairs.
        observed = float(QW_MATRIX[y_a, y_b].mean())

        # Category proportions for each rater in this pair.
        p_a = np.bincount(y_a, minlength=3).astype(float) / n_items
        p_b = np.bincount(y_b, minlength=3).astype(float) / n_items

        # Expected weighted agreement under independence.
        expected = float((QW_MATRIX * np.outer(p_a, p_b)).sum())

        denom = 1.0 - expected
        qwk = (
            float((observed - expected) / denom)
            if not np.isclose(denom, 0) else np.nan
        )
        qwk_values.append(qwk)

    return float(np.nanmean(qwk_values))


def pairwise_qwk_details(wide, scope_name, finding_id=None, finding_name=None):
    """
    Compute per-pair quadratic-weighted kappa and percent agreement.

    Called ONCE per scope (not inside bootstrap) for the detailed CSV output.
    Uses sklearn so the output exactly matches standard library results.
    """
    rows = []
    if wide.empty or wide.shape[1] < 2:
        return pd.DataFrame(rows)

    values = wide.to_numpy(dtype=int)
    rater_names = wide.columns.tolist()

    for a, b in itertools.combinations(range(values.shape[1]), 2):
        y_a, y_b = values[:, a], values[:, b]
        pct = float((y_a == y_b).mean() * 100.0)
        try:
            qwk = cohen_kappa_score(y_a, y_b, labels=LABELS, weights="quadratic")
        except Exception:
            qwk = np.nan
        rows.append({
            "scope": scope_name,
            "finding_id": finding_id,
            "finding": finding_name,
            "rater_a": rater_names[a],
            "rater_b": rater_names[b],
            "n_items": len(wide),
            "percent_agreement": pct,
            "quadratic_weighted_kappa": qwk,
        })

    return pd.DataFrame(rows)


def summarize_pairwise_qwk(pairwise_df):
    """Summarize pairwise QWK values into mean, median, min, max."""
    empty = {k: np.nan for k in [
        "pairwise_qwk_mean", "pairwise_qwk_median",
        "pairwise_qwk_min", "pairwise_qwk_max"
    ]}
    if pairwise_df.empty:
        return empty
    vals = pairwise_df["quadratic_weighted_kappa"].dropna()
    if vals.empty:
        return empty
    return {
        "pairwise_qwk_mean": float(vals.mean()),
        "pairwise_qwk_median": float(vals.median()),
        "pairwise_qwk_min": float(vals.min()),
        "pairwise_qwk_max": float(vals.max()),
    }


# =============================================================================
# FAST vectorized ordinal Krippendorff-style alpha
# =============================================================================

def ordinal_krippendorff_alpha_complete(wide):
    """
    Ordinal Krippendorff-style alpha using squared label distances.

    WHY THIS IS FAST (vs v2):
        v2 iterated over every row with a Python for-loop and called
        itertools.combinations inside it to compute (a-b)^2 per rater pair.
        For 300 items x 15 pairs = 4,500 Python iterations per call,
        times 15,000 bootstrap rounds = 67.5 million iterations total.

        This version uses the algebraic identity:

            sum_{i<j} (x_i - x_j)^2  =  n * sum(x^2) - (sum(x))^2

        Applied across ALL rows simultaneously with NumPy:

            row_sum_sq  = (values**2).sum(axis=1)    shape (n_items,)
            row_sq_sum  = values.sum(axis=1)**2       shape (n_items,)
            D_o = mean( (n * row_sum_sq - row_sq_sum) / (n*(n-1)) )

        This is O(n_items * n_raters) with no Python-level per-item loop.

    Distance metric: squared ordinal distance d(a, b) = (a - b)^2.

    Interpretation:
        alpha = 1  -> perfect agreement
        alpha = 0  -> agreement equals chance
        alpha < 0  -> worse than chance
    """
    if wide.empty or wide.shape[1] < 2:
        return np.nan

    values = wide.to_numpy(dtype=float)
    n = values.shape[1]  # number of raters

    # Observed disagreement using algebraic identity — no Python loop over items.
    row_sum_sq = (values ** 2).sum(axis=1)   # n * E[x^2] per row
    row_sq_sum = values.sum(axis=1) ** 2     # (E[x])^2 * n^2 per row
    D_o = float(((n * row_sum_sq - row_sq_sum) / (n * (n - 1))).mean())

    # Expected disagreement from pooled label proportions.
    pooled = values.flatten()
    if len(np.unique(pooled)) <= 1:
        # All ratings identical -> D_e = 0 -> alpha undefined.
        return np.nan

    counts = np.array([(pooled == label).sum() for label in LABELS], dtype=float)
    probs = counts / counts.sum()

    # D_e = sum_i sum_j p_i * p_j * (i - j)^2
    label_arr = np.array(LABELS, dtype=float)
    sq_dist = (label_arr[:, None] - label_arr[None, :]) ** 2
    D_e = float((np.outer(probs, probs) * sq_dist).sum())

    if np.isclose(D_e, 0):
        return np.nan

    return float(1.0 - D_o / D_e)


# =============================================================================
# Bootstrap confidence intervals
# =============================================================================

def percentile_ci(values, lower=2.5, upper=97.5):
    """Return percentile confidence interval bounds from bootstrap values."""
    values = np.asarray(values, dtype=float)
    values = values[~np.isnan(values)]
    if values.size == 0:
        return np.nan, np.nan
    return float(np.percentile(values, lower)), float(np.percentile(values, upper))


def bootstrap_metric_cis(wide, n_boot=1000, random_seed=123):
    """
    Bootstrap 95% confidence intervals for all five agreement metrics.

    Resampling strategy:
        Items (rows = patient_id + finding_id combinations) are resampled
        with replacement. Each bootstrap sample has the same item count as
        the original. The rater structure within each item is preserved.

    Speed optimizations applied here:
        1. wide.to_numpy() is called ONCE before the loop and stored as
           wide_np. Integer indexing (wide_np[idx]) inside the loop avoids
           repeated pandas-to-numpy conversions.
        2. mean_pairwise_qwk_fast() is used instead of sklearn calls.
        3. ordinal_krippendorff_alpha_complete() uses the vectorized identity.

    With 1000 iterations, Monte Carlo error in the CI bounds is < 0.001
    kappa units for typical distributions of this dataset size.
    """
    empty_ci = {
        "fleiss_kappa_ci_low": np.nan, "fleiss_kappa_ci_high": np.nan,
        "pairwise_qwk_mean_ci_low": np.nan, "pairwise_qwk_mean_ci_high": np.nan,
        "ordinal_alpha_ci_low": np.nan, "ordinal_alpha_ci_high": np.nan,
        "exact_agreement_percent_ci_low": np.nan,
        "exact_agreement_percent_ci_high": np.nan,
        "pairwise_agreement_percent_ci_low": np.nan,
        "pairwise_agreement_percent_ci_high": np.nan,
    }

    if n_boot <= 0 or wide.empty:
        return empty_ci

    rng = np.random.default_rng(random_seed)
    n_items = len(wide)

    # Convert once outside the loop — reused across all 1000 iterations.
    wide_np = wide.to_numpy(dtype=int)
    col_names = wide.columns

    fleiss_vals, qwk_vals, alpha_vals, exact_vals, pairwise_vals = [], [], [], [], []

    for _ in range(n_boot):
        # Resample item indices with replacement.
        idx = rng.integers(0, n_items, size=n_items)

        # Reconstruct a DataFrame for functions that need column names.
        sample = pd.DataFrame(wide_np[idx], columns=col_names)

        count_matrix = build_fleiss_count_matrix(sample)
        fleiss_vals.append(
            fleiss_kappa_from_count_matrix(count_matrix)["fleiss_kappa"]
        )
        qwk_vals.append(mean_pairwise_qwk_fast(sample))
        alpha_vals.append(ordinal_krippendorff_alpha_complete(sample))
        exact_vals.append(exact_all_rater_agreement_percent(sample))
        pairwise_vals.append(mean_pairwise_percent_agreement(sample))

    fk_low,  fk_high  = percentile_ci(fleiss_vals)
    qwk_low, qwk_high = percentile_ci(qwk_vals)
    al_low,  al_high  = percentile_ci(alpha_vals)
    ex_low,  ex_high  = percentile_ci(exact_vals)
    pw_low,  pw_high  = percentile_ci(pairwise_vals)

    return {
        "fleiss_kappa_ci_low": fk_low,   "fleiss_kappa_ci_high": fk_high,
        "pairwise_qwk_mean_ci_low": qwk_low, "pairwise_qwk_mean_ci_high": qwk_high,
        "ordinal_alpha_ci_low": al_low,   "ordinal_alpha_ci_high": al_high,
        "exact_agreement_percent_ci_low": ex_low,
        "exact_agreement_percent_ci_high": ex_high,
        "pairwise_agreement_percent_ci_low": pw_low,
        "pairwise_agreement_percent_ci_high": pw_high,
    }


# =============================================================================
# Metric computation wrapper
# =============================================================================

def compute_agreement_metrics_for_scope(
    wide, scope_name, finding_id=None, finding_name=None,
    n_boot=1000, random_seed=123
):
    """
    Compute all five agreement metrics + CIs for one analysis scope.

    A 'scope' is either:
        overall_all_findings  — all patient-finding items combined
        finding_<name>        — one specific finding across all patients

    Returns:
        metrics      dict of all computed scalar values
        pairwise_df  DataFrame of per-pair QWK rows (for detailed CSV output)
    """
    if wide.empty:
        return {}, pd.DataFrame()

    n_items  = len(wide)
    n_raters = wide.shape[1]

    count_matrix = build_fleiss_count_matrix(wide)
    fleiss       = fleiss_kappa_from_count_matrix(count_matrix)
    exact_pct    = exact_all_rater_agreement_percent(wide)
    pairwise_pct = mean_pairwise_percent_agreement(wide)

    # Per-pair detail: called once per scope, NOT inside bootstrap.
    pairwise_df  = pairwise_qwk_details(
        wide, scope_name=scope_name,
        finding_id=finding_id, finding_name=finding_name,
    )
    qwk_summary  = summarize_pairwise_qwk(pairwise_df)
    ordinal_alpha = ordinal_krippendorff_alpha_complete(wide)

    ci = bootstrap_metric_cis(wide, n_boot=n_boot, random_seed=random_seed)

    # Label distribution — helps interpret kappa for rare/common findings.
    # High absent% with low kappa often means chance agreement is very high.
    total_votes   = count_matrix.sum()
    absent_pct    = count_matrix[:, 0].sum() / total_votes * 100.0
    uncertain_pct = count_matrix[:, 1].sum() / total_votes * 100.0
    present_pct   = count_matrix[:, 2].sum() / total_votes * 100.0

    metrics = {
        "scope": scope_name,
        "finding_id": finding_id,
        "finding": finding_name,
        "n_items": n_items,
        "n_raters": n_raters,

        # ---- Nominal multi-rater metric ----
        "fleiss_kappa": fleiss["fleiss_kappa"],
        "fleiss_kappa_ci_low": ci["fleiss_kappa_ci_low"],
        "fleiss_kappa_ci_high": ci["fleiss_kappa_ci_high"],
        "fleiss_agreement_level": agreement_level_from_kappa(fleiss["fleiss_kappa"]),
        "observed_agreement": fleiss["observed_agreement"],
        "expected_chance_agreement": fleiss["expected_chance_agreement"],

        # ---- Raw agreement metrics ----
        "exact_agreement_percent": exact_pct,
        "exact_agreement_percent_ci_low": ci["exact_agreement_percent_ci_low"],
        "exact_agreement_percent_ci_high": ci["exact_agreement_percent_ci_high"],
        "mean_pairwise_agreement_percent": pairwise_pct,
        "mean_pairwise_agreement_percent_ci_low": ci["pairwise_agreement_percent_ci_low"],
        "mean_pairwise_agreement_percent_ci_high": ci["pairwise_agreement_percent_ci_high"],

        # ---- Ordinal pairwise weighted kappa ----
        "pairwise_qwk_mean": qwk_summary["pairwise_qwk_mean"],
        "pairwise_qwk_mean_ci_low": ci["pairwise_qwk_mean_ci_low"],
        "pairwise_qwk_mean_ci_high": ci["pairwise_qwk_mean_ci_high"],
        "pairwise_qwk_median": qwk_summary["pairwise_qwk_median"],
        "pairwise_qwk_min": qwk_summary["pairwise_qwk_min"],
        "pairwise_qwk_max": qwk_summary["pairwise_qwk_max"],
        "pairwise_qwk_agreement_level": agreement_level_from_kappa(
            qwk_summary["pairwise_qwk_mean"]
        ),

        # ---- Ordinal multi-rater reliability ----
        "ordinal_alpha": ordinal_alpha,
        "ordinal_alpha_ci_low": ci["ordinal_alpha_ci_low"],
        "ordinal_alpha_ci_high": ci["ordinal_alpha_ci_high"],
        "ordinal_alpha_agreement_level": agreement_level_from_kappa(ordinal_alpha),

        # ---- Label distribution ----
        "absent_vote_percent": absent_pct,
        "uncertain_vote_percent": uncertain_pct,
        "present_vote_percent": present_pct,
    }

    return metrics, pairwise_df


# =============================================================================
# Plots
# =============================================================================

def plot_metric_by_finding(
    df, metric_col, ci_low_col, ci_high_col,
    overall_value, output_path, title, x_label
):
    """
    Horizontal bar chart for one agreement metric per finding with CI error bars.

    Matplotlib's barh xerr expects distances from the point estimate,
    not absolute CI bounds. We compute: xerr = [value - ci_low, ci_high - value].
    Non-finite errors (e.g. NaN CIs) are replaced with 0 to prevent failures.
    """
    plot_df = df.sort_values(metric_col, ascending=True).copy()
    y_pos   = np.arange(len(plot_df))
    values  = plot_df[metric_col].to_numpy(dtype=float)

    if ci_low_col in plot_df.columns and ci_high_col in plot_df.columns:
        ci_low  = plot_df[ci_low_col].to_numpy(dtype=float)
        ci_high = plot_df[ci_high_col].to_numpy(dtype=float)
        xerr    = np.vstack([values - ci_low, ci_high - values])
        xerr    = np.where(np.isfinite(xerr), xerr, 0.0)
    else:
        xerr = None

    plt.figure(figsize=(12, 8))
    plt.barh(y_pos, values, xerr=xerr, capsize=3)
    plt.yticks(y_pos, plot_df["finding"])
    plt.xlabel(x_label)
    plt.ylabel("Finding")
    plt.title(title)

    if pd.notna(overall_value):
        plt.axvline(
            overall_value, linestyle="--", linewidth=2,
            label=f"Overall = {overall_value:.3f}",
        )
        plt.legend()

    plt.grid(axis="x", linestyle="--", alpha=0.4)
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()


def plot_percent_by_finding(df, metric_col, output_path, title, x_label):
    """Simple horizontal bar chart for a percentage metric by finding."""
    plot_df = df.sort_values(metric_col, ascending=True).copy()
    plt.figure(figsize=(12, 8))
    plt.barh(plot_df["finding"], plot_df[metric_col])
    plt.xlabel(x_label)
    plt.ylabel("Finding")
    plt.title(title)
    plt.grid(axis="x", linestyle="--", alpha=0.4)
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()


# =============================================================================
# Main analysis
# =============================================================================

def run_analysis(input_excel, output_dir, bootstrap=1000, random_seed=123):
    """
    Full radiologist-only agreement analysis pipeline.

    Steps:
        1.  Load Excel.
        2.  Validate required columns.
        3.  Filter to doctor + director rows only.
        4.  Remove duplicate ratings (keep latest).
        5.  Apply No Finding rule.
        6.  Normalize labels and finding names.
        7.  Build wide rating matrix; filter to complete items only.
        8.  Compute overall agreement metrics + bootstrap CIs.
        9.  Compute per-finding agreement metrics + bootstrap CIs.
        10. Save all CSV outputs.
        11. Save plots.
        12. Save plain-text summary.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("\n============================================================")
    print("Radiologist Agreement Analysis - Doctors Only (v3 optimised)")
    print("============================================================")
    print(f"Input:      {input_excel}")
    print(f"Output dir: {output_dir}")
    print(f"Bootstrap:  {bootstrap} iterations")

    # -------------------------------------------------------------------------
    # 1. Load Excel
    # -------------------------------------------------------------------------
    raw = pd.read_excel(input_excel)
    print(f"\nLoaded {len(raw):,} rows | Columns: {raw.columns.tolist()}")

    # -------------------------------------------------------------------------
    # 2. Validate required columns
    # -------------------------------------------------------------------------
    validate_required_columns(
        raw,
        ["patient_id", "finding_id", "answer_choice", "user_id", "username", "role"]
    )

    # -------------------------------------------------------------------------
    # 3. Keep only doctor + director rows
    # -------------------------------------------------------------------------
    docs = filter_doctor_rows_only(raw, role_column="role")

    # -------------------------------------------------------------------------
    # 4. Remove duplicate ratings
    # -------------------------------------------------------------------------
    docs, duplicate_rows = remove_duplicate_ratings_keep_latest(docs)
    duplicate_rows.to_csv(output_dir / "duplicate_rows_removed.csv", index=False)

    # -------------------------------------------------------------------------
    # 5. Apply No Finding rule
    # -------------------------------------------------------------------------
    docs = enforce_no_finding_rule_for_doctors(docs)
    changed_rows = docs[docs["changed_by_no_finding_rule"]].copy()
    changed_rows.to_csv(output_dir / "rows_changed_by_no_finding_rule.csv", index=False)

    # -------------------------------------------------------------------------
    # 6. Normalize labels and finding names
    # -------------------------------------------------------------------------
    docs["label_ordinal"] = docs["answer_choice"].apply(map_answer_choice_to_ordinal)
    docs["finding"]       = docs["finding_id"].apply(map_finding_id_to_name)
    docs["label_name"]    = docs["label_ordinal"].map(LABEL_NAME)

    invalid_count = docs["label_ordinal"].isna().sum()
    if invalid_count > 0:
        print(f"WARNING: Dropping {invalid_count:,} rows with invalid answer_choice")
        docs = docs.dropna(subset=["label_ordinal"]).copy()

    docs["label_ordinal"] = docs["label_ordinal"].astype(int)

    doctors   = sorted(docs["username"].unique())
    n_doctors = len(doctors)
    print(f"\nDoctors in analysis ({n_doctors}): {doctors}")

    if n_doctors < 2:
        raise ValueError("At least two doctors are required to compute agreement.")

    # -------------------------------------------------------------------------
    # 7. Build wide matrix and filter to complete items
    # -------------------------------------------------------------------------
    wide_all          = build_wide_rating_matrix(docs)
    ratings_per_item  = wide_all.notna().sum(axis=1)

    print("\nRatings per patient-finding item:")
    print(ratings_per_item.value_counts().sort_index().to_string())

    complete_mask    = ratings_per_item == n_doctors
    incomplete_wide  = wide_all[~complete_mask]
    incomplete_wide.reset_index().to_csv(
        output_dir / "incomplete_patient_finding_items.csv", index=False
    )

    if not incomplete_wide.empty:
        print(f"WARNING: {len(incomplete_wide):,} incomplete items excluded.")

    wide_complete = wide_all[complete_mask].astype(int)

    if wide_complete.empty:
        raise ValueError("No complete patient-finding items remain after filtering.")

    print(f"Complete items used for analysis: {len(wide_complete):,}")

    # Save clean long-format answers corresponding to complete items.
    complete_index = wide_complete.index
    docs_complete  = (
        docs.set_index(["patient_id", "finding_id"])
        .loc[complete_index]
        .reset_index()
    )
    docs_complete.to_csv(
        output_dir / "clean_doctor_answers_used_for_agreement.csv", index=False
    )

    # -------------------------------------------------------------------------
    # 8. Overall agreement
    # -------------------------------------------------------------------------
    print("\nComputing overall agreement (this runs 1000 bootstrap iterations)...")
    overall_metrics, overall_pairwise = compute_agreement_metrics_for_scope(
        wide_complete,
        scope_name="overall_all_findings",
        n_boot=bootstrap,
        random_seed=random_seed,
    )

    pd.DataFrame([overall_metrics]).to_csv(
        output_dir / "agreement_overall.csv", index=False
    )
    overall_pairwise.to_csv(
        output_dir / "pairwise_weighted_kappa_overall.csv", index=False
    )

    print("\nOverall results:")
    for key in [
        "fleiss_kappa", "fleiss_agreement_level",
        "pairwise_qwk_mean", "pairwise_qwk_agreement_level",
        "exact_agreement_percent", "ordinal_alpha",
    ]:
        print(f"  {key}: {overall_metrics.get(key)}")

    # -------------------------------------------------------------------------
    # 9. Per-finding agreement
    # -------------------------------------------------------------------------
    print("\nComputing per-finding agreement...")
    per_finding_metrics = []
    all_pairwise_dfs    = [overall_pairwise]

    for finding_id in sorted(docs["finding_id"].unique()):
        finding_name  = map_finding_id_to_name(finding_id)
        finding_wide  = wide_complete[
            wide_complete.index.get_level_values("finding_id") == finding_id
        ]

        if finding_wide.empty:
            print(f"  Skipping finding_id {finding_id} ({finding_name}): no complete items")
            continue

        print(f"  finding_id {finding_id:2d}: {finding_name}")
        scope_name = f"finding_{finding_name.replace(' ', '_')}"

        metrics, pairwise_df = compute_agreement_metrics_for_scope(
            finding_wide,
            scope_name=scope_name,
            finding_id=int(finding_id),
            finding_name=finding_name,
            n_boot=bootstrap,
            # Offset seed per finding so each finding's bootstrap is independent.
            random_seed=random_seed + int(finding_id),
        )
        per_finding_metrics.append(metrics)
        all_pairwise_dfs.append(pairwise_df)

    by_finding_df = pd.DataFrame(per_finding_metrics).sort_values(
        "fleiss_kappa", ascending=True
    )
    by_finding_df.to_csv(output_dir / "agreement_by_finding.csv", index=False)

    pd.concat(all_pairwise_dfs, ignore_index=True).to_csv(
        output_dir / "pairwise_weighted_kappa_by_finding.csv", index=False
    )

    # -------------------------------------------------------------------------
    # 10. Plots
    # -------------------------------------------------------------------------
    overall_fleiss = overall_metrics.get("fleiss_kappa")
    overall_qwk    = overall_metrics.get("pairwise_qwk_mean")

    plot_metric_by_finding(
        by_finding_df,
        metric_col="fleiss_kappa",
        ci_low_col="fleiss_kappa_ci_low",
        ci_high_col="fleiss_kappa_ci_high",
        overall_value=overall_fleiss,
        output_path=output_dir / "fleiss_kappa_by_finding_with_ci.png",
        title="Fleiss' kappa by finding (radiologists only)",
        x_label="Fleiss' kappa",
    )

    plot_metric_by_finding(
        by_finding_df,
        metric_col="pairwise_qwk_mean",
        ci_low_col="pairwise_qwk_mean_ci_low",
        ci_high_col="pairwise_qwk_mean_ci_high",
        overall_value=overall_qwk,
        output_path=output_dir / "ordinal_weighted_kappa_by_finding_with_ci.png",
        title="Mean pairwise quadratic-weighted kappa by finding (radiologists only)",
        x_label="Mean pairwise quadratic-weighted kappa",
    )

    plot_percent_by_finding(
        by_finding_df,
        metric_col="exact_agreement_percent",
        output_path=output_dir / "exact_agreement_by_finding.png",
        title="Exact all-rater agreement by finding (radiologists only)",
        x_label="Exact agreement (%)",
    )

    print("\nPlots saved.")

    # -------------------------------------------------------------------------
    # 11. Plain-text summary
    # -------------------------------------------------------------------------
    summary_path = output_dir / "agreement_summary.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("Radiologist Agreement Analysis - Doctors Only (v3)\n")
        f.write("===================================================\n\n")
        f.write("Role filter: doctor OR director\n")
        f.write("No model predictions used. No radiologist used as ground truth.\n\n")
        f.write(f"Doctors in analysis ({n_doctors}): {', '.join(doctors)}\n")
        f.write(f"Rows changed by No Finding rule: {len(changed_rows):,}\n")
        f.write(f"Complete patient-finding items:  {len(wide_complete):,}\n\n")
        f.write("Overall agreement:\n")
        for key, label in [
            ("fleiss_kappa",                    "  Fleiss kappa              "),
            ("fleiss_agreement_level",           "  Fleiss level              "),
            ("pairwise_qwk_mean",               "  Mean pairwise QWK         "),
            ("pairwise_qwk_agreement_level",     "  QWK level                 "),
            ("ordinal_alpha",                    "  Ordinal alpha             "),
            ("ordinal_alpha_agreement_level",    "  Alpha level               "),
            ("exact_agreement_percent",          "  Exact agreement (%)       "),
            ("mean_pairwise_agreement_percent",  "  Mean pairwise agree. (%)  "),
        ]:
            val = overall_metrics.get(key, "N/A")
            f.write(
                f"{label}: {val:.4f}\n"
                if isinstance(val, float) else f"{label}: {val}\n"
            )

    print(f"\nSummary saved: {summary_path}")
    print("\nDone ✅")


# =============================================================================
# Command-line interface
# =============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Radiologist-only agreement analysis (optimised v3)."
    )
    parser.add_argument(
        "--input_excel",
        type=str,
        default="input.xlsx",
        help="Path to the radiologist answers Excel file.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="agreement_outputs_docs_only_v3",
        help="Directory where output CSVs and plots will be saved.",
    )
    parser.add_argument(
        "--bootstrap",
        type=int,
        default=1000,
        help="Number of bootstrap iterations for confidence intervals.",
    )
    parser.add_argument(
        "--random_seed",
        type=int,
        default=123,
        help="Random seed for reproducibility.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_analysis(
        input_excel=args.input_excel,
        output_dir=args.output_dir,
        bootstrap=args.bootstrap,
        random_seed=args.random_seed,
    )