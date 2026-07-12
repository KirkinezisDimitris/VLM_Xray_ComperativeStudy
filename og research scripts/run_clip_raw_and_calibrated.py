from pathlib import Path
import argparse

import numpy as np
import pandas as pd
import torch
from PIL import Image
from tqdm import tqdm
import open_clip


# ============================================================
# ΒΑΣΙΚΗ ΠΕΡΙΓΡΑΦΗ
# ============================================================
# Αυτό το script τρέχει δύο εκδοχές του CLIP:
#
#   1. Raw CLIP:
#      Χρησιμοποιεί ένα prompt ανά κλάση:
#        0 = absent
#        1 = unsure
#        2 = present
#
#   2. Calibrated CLIP:
#      Χρησιμοποιεί πολλαπλά ισοδύναμα prompts ανά κλάση.
#      Τα scores των prompts κάθε κλάσης γίνονται average.
#
# Για κάθε patient χρησιμοποιούνται και οι δύο διαθέσιμες ακτινογραφίες:
#
#   view1_frontal
#   view2_lateral
#
# Οι δύο εικόνες ΔΕΝ εξετάζονται ως ξεχωριστά περιστατικά.
# Συνδυάζονται σε ένα ενιαίο patient-level embedding.
#
# Τελικό αποτέλεσμα:
#
#   1 patient + 2 X-rays + 1 finding = 1 prediction
#
# Άρα για 300 patients και 14 findings:
#
#   300 x 14 = 4200 rows για raw CLIP
#   300 x 14 = 4200 rows για calibrated CLIP
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

IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".bmp", ".webp"]


# ============================================================
# RAW PROMPTS
# ============================================================
# Στο raw CLIP χρησιμοποιούμε ένα prompt ανά κατηγορία.
#
# Για κάθε finding έχουμε:
#
#   0 = absent
#   1 = unsure
#   2 = present
#
# Το μοντέλο επιλέγει την κατηγορία με το μεγαλύτερο CLIP score.
# ============================================================


def build_raw_prompts(finding: str):
    finding_lower = finding.lower()

    if finding == "No Finding":
        return {
            0: ["This chest X-ray shows abnormal findings."],
            1: ["It is uncertain whether this chest X-ray shows abnormal findings."],
            2: ["This chest X-ray shows no abnormal findings."],
        }

    return {
        0: [f"This chest X-ray shows no evidence of {finding_lower}."],
        1: [f"It is uncertain whether this chest X-ray shows {finding_lower}."],
        2: [f"This chest X-ray shows evidence of {finding_lower}."],
    }


# ============================================================
# CALIBRATED PROMPTS
# ============================================================
# Στο calibrated CLIP χρησιμοποιούμε prompt ensembling.
#
# Δηλαδή, αντί για ένα prompt ανά κλάση, χρησιμοποιούμε πολλά
# κλινικά ισοδύναμα prompts.
#
# Παράδειγμα για Cardiomegaly:
#
#   present:
#     - "Cardiomegaly is present."
#     - "There is cardiomegaly."
#     - "The chest X-ray shows cardiomegaly."
#
#   absent:
#     - "Cardiomegaly is absent."
#     - "There is no cardiomegaly."
#     - "No evidence of cardiomegaly is seen."
#
#   unsure:
#     - "It is uncertain whether cardiomegaly is present."
#     - "Cardiomegaly cannot be determined."
#     - "The presence of cardiomegaly is unclear."
#
# Για κάθε κλάση παίρνουμε τον μέσο όρο των scores.
# Αυτό μειώνει την εξάρτηση του CLIP από μία μόνο διατύπωση.
# ============================================================


def build_calibrated_prompts(finding: str):
    finding_lower = finding.lower()

    if finding == "No Finding":
        return {
            0: [
                "This chest X-ray shows abnormal findings.",
                "Abnormal findings are present on this chest X-ray.",
                "There is evidence of disease on this chest X-ray.",
                "This chest radiograph is not normal.",
            ],
            1: [
                "It is uncertain whether this chest X-ray shows abnormal findings.",
                "It is unclear whether abnormal findings are present.",
                "The presence of abnormal findings cannot be determined.",
                "This chest X-ray is indeterminate for abnormal findings.",
            ],
            2: [
                "This chest X-ray shows no abnormal findings.",
                "No abnormal findings are present on this chest X-ray.",
                "This chest radiograph appears normal.",
                "There is no evidence of disease on this chest X-ray.",
            ],
        }

    return {
        0: [
            f"{finding} is absent.",
            f"There is no {finding_lower}.",
            f"This chest X-ray shows no {finding_lower}.",
            f"No evidence of {finding_lower} is seen.",
            f"The chest radiograph is negative for {finding_lower}.",
        ],
        1: [
            f"It is uncertain whether {finding_lower} is present.",
            f"{finding} cannot be determined from this chest X-ray.",
            f"The presence of {finding_lower} is unclear.",
            f"This chest X-ray is indeterminate for {finding_lower}.",
            f"It is unclear whether this patient has {finding_lower}.",
        ],
        2: [
            f"{finding} is present.",
            f"There is {finding_lower}.",
            f"This chest X-ray shows {finding_lower}.",
            f"There is evidence of {finding_lower}.",
            f"The chest radiograph is positive for {finding_lower}.",
        ],
    }


# ============================================================
# ΕΥΡΕΣΗ ΕΙΚΟΝΩΝ ΑΣΘΕΝΗ
# ============================================================
# Αναμενόμενη δομή:
#
#   E:\Final Xray Collection\
#       patient00032\
#           study1\
#               view1_frontal.png
#               view2_lateral.png
#
# Το script ψάχνει μέσα στο study1 για εικόνες.
# Αν βρει frontal/lateral ή view1/view2 στο filename, τις χρησιμοποιεί.
# Αν όχι, χρησιμοποιεί όλες τις εικόνες του study1.
# ============================================================


def find_patient_images(patient_dir: Path):
    study_dir = patient_dir / "study1"

    if not study_dir.exists():
        return []

    all_images = [
        p for p in study_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    ]

    selected_images = []

    for image_path in all_images:
        name = image_path.stem.lower()

        if (
            "frontal" in name
            or "lateral" in name
            or "view1" in name
            or "view2" in name
        ):
            selected_images.append(image_path)

    if selected_images:
        return sorted(selected_images)

    return sorted(all_images)


# ============================================================
# ΣΥΝΔΥΑΣΜΟΣ ΤΩΝ 2 X-RAYS
# ============================================================
# Το CLIP δέχεται μία εικόνα κάθε φορά.
# Επειδή κάθε patient έχει δύο views, κάνουμε:
#
#   frontal image -> CLIP image embedding
#   lateral image -> CLIP image embedding
#
# Μετά:
#
#   1. Κανονικοποιούμε κάθε image embedding.
#   2. Υπολογίζουμε τον μέσο όρο των embeddings.
#   3. Κανονικοποιούμε ξανά το τελικό embedding.
#
# Έτσι δημιουργείται ένα ενιαίο patient-level embedding.
# Αυτό συγκρίνεται με τα text prompts.
#
# Άρα η απόφαση βασίζεται και στις δύο ακτινογραφίες μαζί.
# ============================================================


def encode_patient_images(image_paths, preprocess, model, device):
    image_tensors = []

    for image_path in image_paths:
        image = Image.open(image_path).convert("RGB")
        image_tensor = preprocess(image)
        image_tensors.append(image_tensor)

    image_batch = torch.stack(image_tensors).to(device)

    image_features = model.encode_image(image_batch)
    image_features = image_features / image_features.norm(dim=-1, keepdim=True)

    patient_embedding = image_features.mean(dim=0, keepdim=True)
    patient_embedding = patient_embedding / patient_embedding.norm(dim=-1, keepdim=True)

    return patient_embedding


# ============================================================
# TEXT ENCODING
# ============================================================


def encode_text_prompts(prompts, tokenizer, model, device):
    text_tokens = tokenizer(prompts).to(device)

    text_features = model.encode_text(text_tokens)
    text_features = text_features / text_features.norm(dim=-1, keepdim=True)

    return text_features


# ============================================================
# SOFTMAX
# ============================================================


def softmax_np(values):
    values = np.array(values, dtype=np.float64)
    values = values - np.max(values)
    exp_values = np.exp(values)
    return exp_values / exp_values.sum()


# ============================================================
# ΥΠΟΛΟΓΙΣΜΟΣ CLASS SCORES
# ============================================================
# Αυτή η συνάρτηση μπορεί να δουλέψει και για raw και για calibrated.
#
# raw:
#   Κάθε κλάση έχει 1 prompt.
#
# calibrated:
#   Κάθε κλάση έχει πολλά prompts.
#
# Βήματα:
#
#   1. Υπολογίζουμε CLIP score για όλα τα prompts.
#   2. Για κάθε κλάση παίρνουμε τον μέσο όρο των prompt scores.
#   3. Επιλέγουμε την κλάση με το μεγαλύτερο average score.
#
# Αυτό δίνει:
#
#   prediction = 0 / 1 / 2
# ============================================================


def compute_class_scores(
    patient_embedding,
    prompt_map,
    tokenizer,
    model,
    device,
):
    labels = [0, 1, 2]

    class_logits = {}
    class_probabilities = {}
    prompt_details = {}

    logit_scale = model.logit_scale.exp()

    for label in labels:
        prompts = prompt_map[label]

        text_features = encode_text_prompts(
            prompts=prompts,
            tokenizer=tokenizer,
            model=model,
            device=device,
        )

        similarities = (patient_embedding @ text_features.T).squeeze(0)
        logits = similarities * logit_scale

        logits_np = logits.detach().cpu().numpy()

        class_logits[label] = float(np.mean(logits_np))

        prompt_details[label] = {
            "prompts": prompts,
            "prompt_logits": [float(x) for x in logits_np],
        }

    logits_for_softmax = [
        class_logits[0],
        class_logits[1],
        class_logits[2],
    ]

    probabilities = softmax_np(logits_for_softmax)

    class_probabilities[0] = float(probabilities[0])
    class_probabilities[1] = float(probabilities[1])
    class_probabilities[2] = float(probabilities[2])

    prediction = int(max(class_logits, key=class_logits.get))

    return {
        "negative_logit": class_logits[0],
        "uncertain_logit": class_logits[1],
        "positive_logit": class_logits[2],

        "negative_probability": class_probabilities[0],
        "uncertain_probability": class_probabilities[1],
        "positive_probability": class_probabilities[2],

        "prediction": prediction,
        "answer_choice": prediction,

        "prompt_details": prompt_details,
    }


# ============================================================
# MAIN
# ============================================================


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--dataset_dir",
        type=str,
        default=r"E:\Final Xray Collection",
        help="Path στο folder που περιέχει τα patient folders.",
    )

    parser.add_argument(
        "--output_dir",
        type=str,
        default="outputs",
        help="Folder εξόδου για τα CSV.",
    )

    parser.add_argument(
        "--model_name",
        type=str,
        default="ViT-B-32",
        help="CLIP model από open_clip.",
    )

    parser.add_argument(
        "--pretrained",
        type=str,
        default="openai",
        help="Pretrained weights.",
    )

    args = parser.parse_args()

    dataset_dir = Path(args.dataset_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)

    raw_output_csv = output_dir / "clip_raw_patient_results.csv"
    calibrated_output_csv = output_dir / "clip_calibrated_prompt_ensemble_patient_results.csv"
    combined_output_csv = output_dir / "clip_raw_and_calibrated_patient_results.csv"

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")

    print("Loading CLIP...")
    model, _, preprocess = open_clip.create_model_and_transforms(
        args.model_name,
        pretrained=args.pretrained,
    )
    tokenizer = open_clip.get_tokenizer(args.model_name)

    model = model.to(device)
    model.eval()

    patient_dirs = sorted([
        p for p in dataset_dir.iterdir()
        if p.is_dir() and p.name.lower().startswith("patient")
    ])

    print(f"Found patient folders: {len(patient_dirs)}")

    raw_rows = []
    calibrated_rows = []
    combined_rows = []

    with torch.no_grad():
        for patient_dir in tqdm(patient_dirs, desc="Running CLIP raw + calibrated"):
            patient_id = patient_dir.name
            image_paths = find_patient_images(patient_dir)

            if len(image_paths) == 0:
                print(f"Warning: no images found for {patient_id}")
                continue

            patient_embedding = encode_patient_images(
                image_paths=image_paths,
                preprocess=preprocess,
                model=model,
                device=device,
            )

            image_paths_text = " | ".join(str(p) for p in image_paths)

            for finding in FINDINGS:
                # ------------------------------
                # Raw CLIP
                # ------------------------------
                raw_prompt_map = build_raw_prompts(finding)

                raw_scores = compute_class_scores(
                    patient_embedding=patient_embedding,
                    prompt_map=raw_prompt_map,
                    tokenizer=tokenizer,
                    model=model,
                    device=device,
                )

                raw_row = {
                    "patient_id": patient_id,
                    "image_paths": image_paths_text,
                    "num_images_used": len(image_paths),
                    "finding": finding,

                    "negative_logit": raw_scores["negative_logit"],
                    "uncertain_logit": raw_scores["uncertain_logit"],
                    "positive_logit": raw_scores["positive_logit"],

                    "negative_probability": raw_scores["negative_probability"],
                    "uncertain_probability": raw_scores["uncertain_probability"],
                    "positive_probability": raw_scores["positive_probability"],

                    "prediction": raw_scores["prediction"],
                    "answer_choice": raw_scores["answer_choice"],
                    "model_name": "CLIP",
                    "mode": "raw_single_prompt",
                }

                # ------------------------------
                # Calibrated CLIP
                # ------------------------------
                calibrated_prompt_map = build_calibrated_prompts(finding)

                calibrated_scores = compute_class_scores(
                    patient_embedding=patient_embedding,
                    prompt_map=calibrated_prompt_map,
                    tokenizer=tokenizer,
                    model=model,
                    device=device,
                )

                calibrated_row = {
                    "patient_id": patient_id,
                    "image_paths": image_paths_text,
                    "num_images_used": len(image_paths),
                    "finding": finding,

                    "negative_logit": calibrated_scores["negative_logit"],
                    "uncertain_logit": calibrated_scores["uncertain_logit"],
                    "positive_logit": calibrated_scores["positive_logit"],

                    "negative_probability": calibrated_scores["negative_probability"],
                    "uncertain_probability": calibrated_scores["uncertain_probability"],
                    "positive_probability": calibrated_scores["positive_probability"],

                    "prediction": calibrated_scores["prediction"],
                    "answer_choice": calibrated_scores["answer_choice"],
                    "model_name": "CLIP",
                    "mode": "calibrated_prompt_ensemble",
                }

                raw_rows.append(raw_row)
                calibrated_rows.append(calibrated_row)

                combined_rows.append(raw_row)
                combined_rows.append(calibrated_row)

    raw_df = pd.DataFrame(raw_rows)
    calibrated_df = pd.DataFrame(calibrated_rows)
    combined_df = pd.DataFrame(combined_rows)

    raw_df.to_csv(raw_output_csv, index=False, encoding="utf-8-sig")
    calibrated_df.to_csv(calibrated_output_csv, index=False, encoding="utf-8-sig")
    combined_df.to_csv(combined_output_csv, index=False, encoding="utf-8-sig")

    print()
    print(f"Saved raw CLIP results to: {raw_output_csv}")
    print(f"Saved calibrated CLIP results to: {calibrated_output_csv}")
    print(f"Saved combined results to: {combined_output_csv}")
    print()
    print("Raw shape:", raw_df.shape)
    print("Calibrated shape:", calibrated_df.shape)
    print("Combined shape:", combined_df.shape)


if __name__ == "__main__":
    main()