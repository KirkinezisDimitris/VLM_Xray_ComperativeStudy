from pathlib import Path
import argparse
from types import MethodType

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from tqdm import tqdm

from health_multimodal.image import ImageInferenceEngine
from health_multimodal.image.data.transforms import create_chest_xray_transform_for_inference
from health_multimodal.image.model.pretrained import get_biovil_t_image_encoder
from health_multimodal.text.utils import BertEncoderType, get_bert_inference
from health_multimodal.vlp.inference_engine import ImageTextInferenceEngine


# ============================================================
# ΒΑΣΙΚΗ ΠΕΡΙΓΡΑΦΗ
# ============================================================
# Αυτό το script τρέχει δύο εκδοχές του BioViL-T:
#
#   1. Raw BioViL-T:
#      Χρησιμοποιεί ένα prompt ανά κλάση:
#        0 = absent
#        1 = unsure
#        2 = present
#
#   2. Calibrated BioViL-T:
#      Χρησιμοποιεί πολλαπλά ισοδύναμα prompts ανά κλάση.
#      Τα scores των prompts κάθε κλάσης γίνονται average.
#
# Για κάθε patient χρησιμοποιούνται μέχρι δύο διαθέσιμες ακτινογραφίες:
#
#   view1_frontal
#   view2_lateral
#
# ΣΗΜΑΝΤΙΚΟ:
# Το public zero-shot API του HI-ML/BioViL-T δέχεται ένα image_path ανά κλήση.
# Άρα εδώ ΔΕΝ περνάμε δύο εικόνες σε μία native κλήση.
# Αντίθετα κάνουμε:
#
#   frontal image -> BioViL-T image embedding
#   lateral image -> BioViL-T image embedding
#
# Μετά:
#
#   1. Υπολογίζουμε similarity score κάθε εικόνας με τα text prompts.
#   2. Υπολογίζουμε average score ανά κλάση στις διαθέσιμες εικόνες.
#   3. Παράγουμε μία patient-level απόφαση.
#
# Τελικό αποτέλεσμα:
#
#   1 patient + μέχρι 2 X-rays + 1 finding = 1 prediction
#
# Άρα για 300 patients και 14 findings:
#
#   300 x 14 = 4200 rows για raw BioViL-T
#   300 x 14 = 4200 rows για calibrated BioViL-T
#
# Τα output columns κρατιούνται ίδια με το CLIP script, ώστε η ανάλυση
# να μπορεί να διαβάσει τα αρχεία με τον ίδιο τρόπο.
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

# BioViL-T inference transform settings used in the official HI-ML examples.
RESIZE = 512
CENTER_CROP_SIZE = 512


# ============================================================
# RAW PROMPTS
# ============================================================
# Στο raw BioViL-T χρησιμοποιούμε ένα prompt ανά κατηγορία.
#
# Για κάθε finding έχουμε:
#
#   0 = absent
#   1 = unsure
#   2 = present
#
# Το μοντέλο επιλέγει την κατηγορία με το μεγαλύτερο BioViL-T score.
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
# Στο calibrated BioViL-T χρησιμοποιούμε prompt ensembling.
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
# Αυτό μειώνει την εξάρτηση του BioViL-T από μία μόνο διατύπωση.
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
# Αναμενόμενη δομή, ίδια με το CLIP script:
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
#
# Για BioViL-T κρατάμε μέχρι 2 εικόνες ανά patient, ώστε να ταιριάζει
# με το πρωτόκολλο της εργασίας:
#
#   view1_frontal + view2_lateral
# ============================================================


def find_patient_images(patient_dir: Path, max_images_per_patient: int = 2):
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
        return sorted(selected_images)[:max_images_per_patient]

    return sorted(all_images)[:max_images_per_patient]


# ============================================================
# PATCH ΓΙΑ CXRBertTokenizer
# ============================================================
# Σε κάποιους συνδυασμούς hi-ml-multimodal + transformers, το HI-ML
# καλεί tokenizer.batch_encode_plus(...), αλλά ο CXRBertTokenizer που
# φορτώνεται δεν έχει αυτό το method.
#
# Το traceback μοιάζει με:
#
#   AttributeError: CXRBertTokenizer has no attribute batch_encode_plus
#
# Για να μη χρειάζεται να αλλάξουμε το site-packages, προσθέτουμε στο
# tokenizer ένα μικρό compatibility wrapper που καλεί το σύγχρονο
# tokenizer(...). Έτσι το υπόλοιπο BioViL-T inference engine μένει ίδιο.
# ============================================================


def patch_tokenizer_batch_encode_plus(tokenizer):
    if hasattr(tokenizer, "batch_encode_plus"):
        return False

    def batch_encode_plus_compat(
        self,
        batch_text_or_text_pairs=None,
        add_special_tokens=True,
        padding="longest",
        return_tensors=None,
        **kwargs,
    ):
        if batch_text_or_text_pairs is None:
            batch_text_or_text_pairs = kwargs.pop("text", None)

        if batch_text_or_text_pairs is None:
            raise TypeError("batch_text_or_text_pairs/text was not provided")

        return self(
            batch_text_or_text_pairs,
            add_special_tokens=add_special_tokens,
            padding=padding,
            return_tensors=return_tensors,
            **kwargs,
        )

    tokenizer.batch_encode_plus = MethodType(batch_encode_plus_compat, tokenizer)
    return True


# ============================================================
# ΦΟΡΤΩΣΗ BioViL-T
# ============================================================
# Το HI-ML multimodal package παρέχει έτοιμα components:
#
#   get_biovil_t_image_encoder()
#   create_chest_xray_transform_for_inference()
#   get_bert_inference(BertEncoderType.BIOVIL_T_BERT)
#   ImageTextInferenceEngine
#
# Αυτό είναι το επίσημο zero-shot μοτίβο χρήσης του BioViL-T.
#
# Σε αυτή την έκδοση φορτώνουμε το ImageTextInferenceEngine, αλλά για
# ταχύτητα ΔΕΝ καλούμε get_similarity_score_from_raw_data για κάθε prompt.
# Αν το κάναμε, θα ξαναφόρτωνε/ξανακωδικοποιούσε την ίδια εικόνα πολλές
# φορές. Αντίθετα:
#
#   1. Κωδικοποιούμε κάθε X-ray μία φορά ανά patient.
#   2. Κωδικοποιούμε κάθε prompt set μία φορά και το κάνουμε cache.
#   3. Υπολογίζουμε cosine similarities απευθείας.
# ============================================================


def get_biovil_t_inference_engine(device):
    image_inference = ImageInferenceEngine(
        image_model=get_biovil_t_image_encoder(),
        transform=create_chest_xray_transform_for_inference(
            resize=RESIZE,
            center_crop_size=CENTER_CROP_SIZE,
        ),
    )

    text_inference = get_bert_inference(BertEncoderType.BIOVIL_T_BERT)
    was_patched = patch_tokenizer_batch_encode_plus(text_inference.tokenizer)

    if was_patched:
        print("Patched CXRBertTokenizer.batch_encode_plus compatibility issue.")

    image_text_inference = ImageTextInferenceEngine(
        image_inference_engine=image_inference,
        text_inference_engine=text_inference,
    )

    image_text_inference.to(torch.device(device))
    return image_text_inference


# ============================================================
# SOFTMAX
# ============================================================


def softmax_np(values):
    values = np.array(values, dtype=np.float64)
    values = values - np.max(values)
    exp_values = np.exp(values)
    return exp_values / exp_values.sum()


# ============================================================
# TEXT EMBEDDING CACHE
# ============================================================
# Το calibrated mode έχει πολλά prompts και τα ίδια prompts θα
# χρησιμοποιηθούν για όλους τους patients.
#
# Για να μη γίνονται ξανά και ξανά tokenization + BERT forward pass,
# κρατάμε cache:
#
#   (mode, finding, label) -> text embedding
#
# Αν μία κλάση έχει πολλά prompts, το BioViL-T pattern είναι:
#
#   prompt embeddings -> mean -> L2 normalization
# ============================================================


def prompts_cache_key(mode: str, finding: str, label: int):
    return f"{mode}::{finding}::{label}"


def get_cached_text_embedding(
    cache,
    image_text_inference,
    mode: str,
    finding: str,
    label: int,
    prompts,
):
    key = prompts_cache_key(mode=mode, finding=finding, label=label)

    if key in cache:
        return cache[key]

    text_embedding = image_text_inference.text_inference_engine.get_embeddings_from_prompt(
        prompts=prompts,
        normalize=False,
    )

    # Αν υπάρχουν πολλά prompts για την ίδια κλάση, παίρνουμε mean.
    text_embedding = text_embedding.mean(dim=0)
    text_embedding = F.normalize(text_embedding, dim=0, p=2)

    cache[key] = text_embedding
    return text_embedding


# ============================================================
# IMAGE EMBEDDINGS ΑΝΑ PATIENT
# ============================================================
# Κάθε X-ray περνάει από το BioViL-T image encoder μία φορά.
# Το HI-ML image inference επιστρέφει L2-normalized global embedding.
#
# Για patient με 2 X-rays παίρνουμε δύο embeddings:
#
#   image_embeddings.shape = [2, embedding_dim]
#
# Για patient με 1 X-ray:
#
#   image_embeddings.shape = [1, embedding_dim]
# ============================================================


def encode_patient_images(image_paths, image_text_inference):
    image_embeddings = []

    for image_path in image_paths:
        image_embedding = image_text_inference.image_inference_engine.get_projected_global_embedding(
            image_path=Path(image_path)
        )
        image_embeddings.append(image_embedding)

    return torch.stack(image_embeddings, dim=0)


# ============================================================
# ΥΠΟΛΟΓΙΣΜΟΣ CLASS SCORES ΣΕ PATIENT LEVEL
# ============================================================
# Για κάθε finding και κάθε mode:
#
#   1. Έχουμε 1 ή 2 image embeddings για τον patient.
#   2. Έχουμε 3 text embeddings:
#        0 = absent
#        1 = unsure
#        2 = present
#   3. Υπολογίζουμε cosine similarity image-text.
#   4. Αν υπάρχουν 2 X-rays, κάνουμε average των image scores.
#   5. Η τελική κλάση είναι αυτή με το μεγαλύτερο patient-level score.
#
# Τα columns ονομάζονται negative_logit / uncertain_logit / positive_logit
# για να είναι ίδια με το CLIP output. Εδώ όμως είναι BioViL-T similarity
# scores, όχι CLIP logits.
# ============================================================


def compute_patient_class_scores(
    patient_image_embeddings,
    prompt_map,
    image_text_inference,
    text_embedding_cache,
    mode: str,
    finding: str,
):
    labels = [0, 1, 2]
    patient_scores = {}

    for label in labels:
        prompts = prompt_map[label]

        text_embedding = get_cached_text_embedding(
            cache=text_embedding_cache,
            image_text_inference=image_text_inference,
            mode=mode,
            finding=finding,
            label=label,
            prompts=prompts,
        )

        # image embeddings: [num_images, dim]
        # text embedding:   [dim]
        # similarities:     [num_images]
        similarities = patient_image_embeddings @ text_embedding
        patient_scores[label] = float(similarities.mean().detach().cpu().item())

    scores_for_softmax = [
        patient_scores[0],
        patient_scores[1],
        patient_scores[2],
    ]

    probabilities = softmax_np(scores_for_softmax)
    prediction = int(max(patient_scores, key=patient_scores.get))

    return {
        "negative_logit": patient_scores[0],
        "uncertain_logit": patient_scores[1],
        "positive_logit": patient_scores[2],

        "negative_probability": float(probabilities[0]),
        "uncertain_probability": float(probabilities[1]),
        "positive_probability": float(probabilities[2]),

        "prediction": prediction,
        "answer_choice": prediction,
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
        "--max_images_per_patient",
        type=int,
        default=2,
        help="Μέγιστος αριθμός X-rays ανά patient. Default = 2.",
    )

    args = parser.parse_args()

    dataset_dir = Path(args.dataset_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)

    raw_output_csv = output_dir / "biovil_raw_patient_results.csv"
    calibrated_output_csv = output_dir / "biovil_calibrated_prompt_ensemble_patient_results.csv"
    combined_output_csv = output_dir / "biovil_raw_and_calibrated_patient_results.csv"

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")

    print("Loading BioViL-T...")
    image_text_inference = get_biovil_t_inference_engine(device=device)

    patient_dirs = sorted([
        p for p in dataset_dir.iterdir()
        if p.is_dir() and p.name.lower().startswith("patient")
    ])

    print(f"Found patient folders: {len(patient_dirs)}")

    raw_rows = []
    calibrated_rows = []
    combined_rows = []

    text_embedding_cache = {}

    with torch.no_grad():
        for patient_dir in tqdm(patient_dirs, desc="Running BioViL-T raw + calibrated"):
            patient_id = patient_dir.name
            image_paths = find_patient_images(
                patient_dir=patient_dir,
                max_images_per_patient=args.max_images_per_patient,
            )

            if len(image_paths) == 0:
                print(f"Warning: no images found for {patient_id}")
                continue

            patient_image_embeddings = encode_patient_images(
                image_paths=image_paths,
                image_text_inference=image_text_inference,
            )

            image_paths_text = " | ".join(str(p) for p in image_paths)

            for finding in FINDINGS:
                # ------------------------------
                # Raw BioViL-T
                # ------------------------------
                raw_prompt_map = build_raw_prompts(finding)

                raw_scores = compute_patient_class_scores(
                    patient_image_embeddings=patient_image_embeddings,
                    prompt_map=raw_prompt_map,
                    image_text_inference=image_text_inference,
                    text_embedding_cache=text_embedding_cache,
                    mode="raw_single_prompt",
                    finding=finding,
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
                    "model_name": "BioViL-T",
                    "mode": "raw_single_prompt",
                }

                # ------------------------------
                # Calibrated BioViL-T
                # ------------------------------
                calibrated_prompt_map = build_calibrated_prompts(finding)

                calibrated_scores = compute_patient_class_scores(
                    patient_image_embeddings=patient_image_embeddings,
                    prompt_map=calibrated_prompt_map,
                    image_text_inference=image_text_inference,
                    text_embedding_cache=text_embedding_cache,
                    mode="calibrated_prompt_ensemble",
                    finding=finding,
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
                    "model_name": "BioViL-T",
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
    print(f"Saved raw BioViL-T results to: {raw_output_csv}")
    print(f"Saved calibrated BioViL-T results to: {calibrated_output_csv}")
    print(f"Saved combined results to: {combined_output_csv}")
    print()
    print("Raw shape:", raw_df.shape)
    print("Calibrated shape:", calibrated_df.shape)
    print("Combined shape:", combined_df.shape)


if __name__ == "__main__":
    main()
