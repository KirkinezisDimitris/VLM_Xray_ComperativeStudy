from pathlib import Path
import argparse
import json
import re
import time

import numpy as np
import pandas as pd
import torch
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer
from transformers.utils.logging import set_verbosity_error


# ============================================================
# ΒΑΣΙΚΗ ΠΕΡΙΓΡΑΦΗ
# ============================================================
# VERSION: v11
#
# v8 change:
#   low_cpu_mem_usage=True αφαιρέθηκε, γιατί μπορεί να αφήσει
#   meta tensors στο CheXagent/XraySigLIP vision encoder.
#   Με 32 GB RAM προτιμάμε κανονικό loading και μετά model.to(device).
# ============================================================
# Αυτό το script τρέχει το CheXagent σε patient-level μορφή,
# ώστε να έχουμε output συγκρίσιμο με τα scripts του CLIP και
# του BioViL.
#
# Το CheXagent ΔΕΝ είναι κλασικός classifier με logits όπως το
# CLIP/BioViL. Είναι vision-language model που απαντά σε prompts.
#
# Για κάθε patient χρησιμοποιούνται και οι δύο διαθέσιμες ακτινογραφίες:
#
#   view1_frontal
#   view2_lateral
#
# Οι δύο εικόνες ΔΕΝ εξετάζονται ως ξεχωριστά περιστατικά.
# Δίνονται μαζί στο CheXagent ως εικόνες του ίδιου patient/study.
#
# Default τρόπος λειτουργίας:
#
#   1 patient + έως 2 X-rays + 1 finding -> 1 forced key-value answer
#
# Μετά το JSON μετατρέπεται σε long CSV:
#
#   1 patient + 1 finding = 1 prediction
#
# Άρα για 300 patients και 14 findings:
#
#   300 x 14 = 4200 rows
#
# Κλάσεις:
#
#   0 = absent
#   1 = unsure / uncertain
#   2 = present
#
# ΣΗΜΑΝΤΙΚΟ:
# Το CheXagent δεν δίνει πραγματικά logits/probabilities.
# Για να παραμείνει συμβατό το CSV με τα CLIP/BioViL outputs:
#
#   - τα logit columns μένουν NaN
#   - τα probability columns είναι one-hot compatibility values
#
# Δηλαδή δεν πρέπει να ερμηνευτούν ως calibrated probabilities.
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

ANSWER_TO_CHOICE = {
    "absent": 0,
    "negative": 0,
    "no": 0,
    "not present": 0,

    "uncertain": 1,
    "unsure": 1,
    "indeterminate": 1,
    "unclear": 1,
    "equivocal": 1,

    "present": 2,
    "positive": 2,
    "yes": 2,
}

CHOICE_TO_LABEL = {
    0: "absent",
    1: "uncertain",
    2: "present",
}


# ============================================================
# FINDING-AWARE PARSER ALIASES
# ============================================================
# CheXagent is generative and sometimes answers with clinical phrases
# instead of the forced "Finding: present/absent/uncertain" format.
# These aliases let the parser decide whether a phrase actually refers
# to the requested finding, instead of blindly treating every
# "no evidence of ..." sentence as absent.
# ============================================================

FINDING_ALIASES = {
    "Enlarged Cardiomediastinum": [
        "enlarged cardiomediastinum",
        "enlarged cardiomediastinal",
        "cardiomediastinal silhouette is enlarged",
        "cardiomediastinal silhouette enlarged",
        "cardiomediastinal silhouette remains enlarged",
        "enlarged cardiomediastinal silhouette",
    ],
    "Cardiomegaly": [
        "cardiomegaly",
        "enlarged heart",
        "heart is enlarged",
        "cardiac silhouette is enlarged",
        "enlarged cardiac silhouette",
    ],
    "Lung Opacity": [
        "lung opacity",
        "pulmonary opacity",
        "airspace opacity",
        "opacity",
        "opacities",
        "basilar opacity",
    ],
    "Lung Lesion": [
        "lung lesion",
        "pulmonary lesion",
        "nodule",
        "mass",
        "metastatic disease",
        "metastasis",
    ],
    "Edema": [
        "edema",
        "pulmonary edema",
        "interstitial edema",
    ],
    "Consolidation": [
        "consolidation",
        "consolidative opacity",
    ],
    "Pneumonia": [
        "pneumonia",
    ],
    "Atelectasis": [
        "atelectasis",
    ],
    "Pneumothorax": [
        "pneumothorax",
    ],
    "Pleural Effusion": [
        "pleural effusion",
        "effusion",
    ],
    "Pleural Other": [
        "pleural other",
        "pleural thickening",
        "pleural abnormality",
        "pleural scarring",
    ],
    "Fracture": [
        "fracture",
        "rib fracture",
        "osseous fracture",
    ],
    "Support Devices": [
        "support device",
        "support devices",
        "line",
        "tube",
        "catheter",
        "pacemaker",
        "port",
        "picc",
        "endotracheal",
    ],
}


NEGATIVE_CUES = (
    r"\b(no evidence of|without|negative for|absent|not present|free of|"
    r"resolved|has resolved|resolution of)\b"
)

UNCERTAIN_CUES = (
    r"\b(uncertain|unsure|unclear|indeterminate|equivocal|"
    r"cannot determine|cannot be determined|limited)\b"
)

POSITIVE_CUES = (
    r"\b(present|positive|evidence of|seen|noted|identified|demonstrates|"
    r"shows|there is|there are|unchanged|improved|decreased|increased|"
    r"enlarged|persistent|residual)\b"
)


def response_mentions_requested_finding(text_lower: str, finding: str) -> bool:
    aliases = FINDING_ALIASES.get(finding, [finding.lower()])
    for alias in aliases:
        if re.search(r"\b" + re.escape(alias.lower()) + r"\b", text_lower):
            return True
    return False



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
#
# Με default κρατάμε μέχρι 2 εικόνες ανά patient, επειδή το πείραμα
# είναι patient-level με δύο views.
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
        images = sorted(selected_images)
    else:
        images = sorted(all_images)

    if max_images_per_patient is not None and max_images_per_patient > 0:
        images = images[:max_images_per_patient]

    return images


# ============================================================
# PROMPT ΓΙΑ ΟΛΑ ΤΑ 14 FINDINGS ΜΑΖΙ
# ============================================================
# Το CheXagent είναι generative model.
# Αν κάναμε ξεχωριστό generation για κάθε finding, θα είχαμε:
#
#   300 patients x 14 findings = 4200 generations
#
# Αυτό είναι πολύ αργό.
#
# Γι' αυτό το default prompt ζητάει ΟΛΑ τα findings μαζί σε ένα
# αυστηρό JSON object. Έτσι έχουμε:
#
#   300 patients = 300 generations
#
# και μετά μετατρέπουμε το JSON σε 4200 rows.
# ============================================================


def build_all_findings_prompt():
    """
    JSON-first prompt for the 14 CheXpert observations.

    Earlier line-based prompts made CheXagent collapse the whole answer into
    a single text response like "No Finding". For patient-level 14-label
    inference, the most defensible format is a strict JSON object with all
    14 exact keys.
    """

    findings_json = json.dumps(FINDINGS, ensure_ascii=False, indent=2)

    return f"""
You are given one or two chest X-ray images from the same patient and the same study.
Use all provided images together as one patient-level case.

Classify each of the following 14 CheXpert observations using exactly one lowercase label:
- absent
- uncertain
- present

Important rule for "No Finding":
- "present" means no abnormal finding is present.
- "absent" means the study is not normal / some abnormal finding is present.

Return ONLY a valid JSON object.
Do not include markdown.
Do not include explanations.
Do not include extra keys.
Do not omit any key.

The keys must be exactly these strings:
{findings_json}

The values must be only "absent", "uncertain", or "present".

Example value format:
{{
  "No Finding": "absent",
  "Enlarged Cardiomediastinum": "uncertain",
  "Cardiomegaly": "present"
}}
""".strip()


# ============================================================
# PROMPT ΓΙΑ ΕΝΑ FINDING
# ============================================================
# Αυτός ο τρόπος είναι πιο κοντά στο CLIP/BioViL γιατί εξετάζει
# κάθε finding ξεχωριστά.
#
# Όμως είναι πολύ πιο αργός:
#
#   300 patients x 14 findings = 4200 generations
#
# Χρησιμοποιείται μόνο αν δώσεις:
#
#   --prompt_mode one_finding
# ============================================================


def build_one_finding_prompt(finding: str):
    return f"""
You are given one or two chest X-ray images from the same patient and the same study.
Use all provided images together as one patient-level case.

Classify ONLY this CheXpert observation:
{finding}

Allowed labels:
- absent
- uncertain
- present

Important rule for "No Finding":
- present means no abnormal finding is present.
- absent means the study is not normal / some abnormal finding is present.

Return exactly ONE line and nothing else.
The line must use this exact format:
{finding}: absent

or:
{finding}: uncertain

or:
{finding}: present

Rules:
- The key before the colon must be exactly: {finding}
- The value after the colon must be exactly one of: absent, uncertain, present
- Do not output another disease name.
- Do not explain.
- Do not use markdown.
""".strip()


# ============================================================
# ΦΟΡΤΩΣΗ CHEXAGENT
# ============================================================
# Χρησιμοποιούμε το official Hugging Face μοντέλο:
#
#   StanfordAIMI/CheXagent-2-3b
#
# Το repo του CheXagent δείχνει ότι το model δέχεται λίστα από
# image paths μαζί με text prompt μέσω tokenizer.from_list_format.
# ============================================================




def patch_module_input_dtype(model):
    """
    CheXagent-2-3b can mix float32 activations from its image preprocessing /
    multimodal bridge with float16 CUDA weights.

    On CUDA this can crash inside Conv2d or Linear with errors like:

        Input type torch.cuda.FloatTensor and weight type torch.cuda.HalfTensor
        should be the same

        expected mat1 and mat2 to have the same dtype, but got: float != Half

    These hooks make Conv2d and Linear receive floating input tensors in the
    same dtype/device as their own weights. This does not change the model
    architecture; it only fixes dtype compatibility during inference.
    """

    patched = 0

    def _dtype_pre_hook(module, inputs):
        if not inputs:
            return inputs

        x = inputs[0]

        if torch.is_tensor(x):
            target_dtype = module.weight.dtype
            target_device = module.weight.device

            if x.dtype != target_dtype or x.device != target_device:
                x = x.to(device=target_device, dtype=target_dtype)
                return (x, *inputs[1:])

        return inputs

    target_modules = (torch.nn.Conv2d, torch.nn.Linear)

    for module in model.modules():
        if isinstance(module, target_modules):
            module.register_forward_pre_hook(_dtype_pre_hook)
            patched += 1

    print(f"Patched Conv2d/Linear input dtype compatibility hooks: {patched}")

def resolve_dtype(dtype_arg: str):
    if dtype_arg == "auto":
        if torch.cuda.is_available():
            return torch.float16
        return torch.float32

    if dtype_arg == "float16":
        return torch.float16

    if dtype_arg == "bfloat16":
        return torch.bfloat16

    if dtype_arg == "float32":
        return torch.float32

    raise ValueError(f"Unsupported dtype: {dtype_arg}")


def resolve_device(device_arg: str):
    if device_arg == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if device_arg == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError("--device cuda was requested, but CUDA is not available.")
        return torch.device("cuda")

    if device_arg == "cpu":
        return torch.device("cpu")

    raise ValueError(f"Unsupported device: {device_arg}")


def load_chexagent(model_name: str, dtype_arg: str, device_arg: str):
    set_verbosity_error()

    dtype = resolve_dtype(dtype_arg)
    device = resolve_device(device_arg)

    print(f"Loading CheXagent model: {model_name}")
    print(f"Using dtype: {dtype}")
    print(f"Using single device: {device}")
    print("low_cpu_mem_usage: disabled to avoid meta-parameter loading warnings")

    tokenizer = AutoTokenizer.from_pretrained(
        model_name,
        trust_remote_code=True,
    )

    # Important: do NOT use device_map="auto" here.
    # Important: do NOT use low_cpu_mem_usage=True here either.
    #
    # Why:
    #   CheXagent uses custom remote-code modules for the language model and
    #   the XraySigLIP vision encoder. With low_cpu_mem_usage=True, some
    #   Transformers/custom-model combinations can leave parameters on the
    #   "meta" device during loading. That produces warnings like:
    #
    #       copying from a non-meta parameter in the checkpoint to a meta
    #       parameter in the current model, which is a no-op
    #
    #   If that happens, the vision encoder may not be correctly loaded and
    #   the model can collapse to useless generic answers such as "No Finding".
    #
    # With 32 GB RAM, the safer approach is to load normally on CPU first,
    # then move the whole model to one device.
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        trust_remote_code=True,
        torch_dtype=dtype,
    )

    model.to(device)
    model.eval()

    # Patch dtype mismatch between float32 activations and float16 CUDA weights.
    # We keep both Conv2d and Linear because CheXagent/XraySigLIP can hit both.
    patch_module_input_dtype(model)

    return tokenizer, model


# ============================================================
# CHEXAGENT GENERATION
# ============================================================
# Εδώ φτιάχνουμε το multimodal prompt:
#
#   image path 1
#   image path 2
#   text prompt
#
# και παίρνουμε την απάντηση του CheXagent.
# ============================================================


def get_first_model_device(model):
    try:
        return next(model.parameters()).device
    except StopIteration:
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def chexagent_generate(
    tokenizer,
    model,
    image_paths,
    prompt: str,
    max_new_tokens: int,
):
    image_paths = [str(p) for p in image_paths]

    query = tokenizer.from_list_format(
        [
            *[{"image": image_path} for image_path in image_paths],
            {"text": prompt},
        ]
    )

    conversation = [
        {"from": "system", "value": "You are a helpful assistant."},
        {"from": "human", "value": query},
    ]

    encoded = tokenizer.apply_chat_template(
        conversation,
        add_generation_prompt=True,
        return_tensors="pt",
    )

    # Since the model is on one device, move the encoded tensors to that device.
    # Do not manually cast prompt/image tensors to the model dtype here.
    # The Conv2d/Linear hooks handle float32 image activations safely.
    device = get_first_model_device(model)

    if isinstance(encoded, dict):
        encoded = {
            k: (v.to(device) if torch.is_tensor(v) else v)
            for k, v in encoded.items()
        }
    else:
        encoded = encoded.to(device)

    with torch.no_grad():
        if isinstance(encoded, dict):
            input_length = encoded["input_ids"].shape[-1]
            output = model.generate(
                **encoded,
                do_sample=False,
                num_beams=1,
                temperature=1.0,
                top_p=1.0,
                use_cache=True,
                max_new_tokens=max_new_tokens,
            )[0]
        else:
            input_ids = encoded
            input_length = input_ids.shape[-1]
            output = model.generate(
                input_ids,
                do_sample=False,
                num_beams=1,
                temperature=1.0,
                top_p=1.0,
                use_cache=True,
                max_new_tokens=max_new_tokens,
            )[0]

    response = tokenizer.decode(
        output[input_length:],
        skip_special_tokens=True,
    )

    return response.strip()


# ============================================================
# PARSING JSON ΑΠΑΝΤΗΣΗΣ
# ============================================================
# Ζητάμε από το CheXagent αυστηρό JSON, αλλά επειδή είναι generative
# model μπορεί να βάλει έξτρα κείμενο ή markdown.
#
# Η συνάρτηση αυτή προσπαθεί να βρει JSON object μέσα στην απάντηση.
# Αν αποτύχει, γίνεται fallback parsing γραμμών.
# ============================================================


def clean_response_text(text: str):
    text = str(text).strip()
    text = text.replace("```json", "")
    text = text.replace("```", "")
    text = text.replace("**", "")
    text = text.replace("`", "")

    # Remove CheXagent section tags such as:
    #   [Breathing: Lungs]
    # These are not the answer label and can confuse the parser.
    text = re.sub(r"\[[^\]]+\]", " ", text)

    # Remove leading markdown bullets.
    text = re.sub(r"^\s*[-*•]\s*", "", text)

    # Normalize whitespace.
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def normalize_answer(value):
    if value is None:
        return None

    value = str(value).strip().lower()
    value = re.sub(r"^\s*[-*•]\s*", "", value)
    value = value.replace(".", "")
    value = value.replace(":", "")
    value = value.replace("'", "")
    value = value.replace('"', "")
    value = re.sub(r"\s+", " ", value).strip()

    if value in ANSWER_TO_CHOICE:
        return ANSWER_TO_CHOICE[value]

    if value in {"0", "1", "2"}:
        return int(value)

    # Fallback για απαντήσεις τύπου "present with ..." ή "likely absent".
    if re.search(r"\b(uncertain|unsure|unclear|indeterminate|equivocal)\b", value):
        return 1

    if re.search(r"\b(absent|negative)\b", value) or "not present" in value or value.startswith("no "):
        return 0

    if re.search(r"\b(present|positive)\b", value) or value.startswith("yes"):
        return 2

    return None


def extract_label_from_text(value: str):
    """
    Extract an allowed answer from the part of a line that should contain
    only the label. We keep this stricter than normalize_answer because
    full lines may contain terms like "No Finding", which should not be
    interpreted as the answer "no".
    """
    if value is None:
        return None

    text = str(value).strip().lower()
    text = text.replace("`", "")
    text = text.replace("**", "")
    text = text.replace(".", "")
    text = text.replace(",", "")
    text = text.replace("\"", "")
    text = text.replace("'", "")
    text = text.strip()

    # Most reliable: exact labels / common direct synonyms.
    exact_map = {
        "absent": 0,
        "negative": 0,
        "not present": 0,
        "uncertain": 1,
        "unsure": 1,
        "indeterminate": 1,
        "unclear": 1,
        "equivocal": 1,
        "present": 2,
        "positive": 2,
    }

    if text in exact_map:
        return exact_map[text]

    # Token-level / phrase-level fallback on the answer part only.
    if re.search(r"\b(uncertain|unsure|indeterminate|unclear|equivocal)\b", text):
        return 1

    if re.search(r"\b(absent|negative)\b", text) or "not present" in text:
        return 0

    if re.search(r"\b(present|positive)\b", text):
        return 2

    return None


def parse_line_based_response(cleaned: str):
    """
    Parse responses like:

        Cardiomegaly = PRESENT
        Cardiomegaly: present
        | Cardiomegaly | present |
        3. Cardiomegaly - absent

    This is intentionally finding-aware so that "No Finding" does not get
    misread as the answer "no".
    """

    parsed = {}
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]

    # First pass: parse one line at a time.
    for raw_line in lines:
        line = raw_line.strip()
        line = re.sub(r"^[-*•]\s*", "", line)
        line = re.sub(r"^\d+[.)]\s*", "", line)
        line = line.strip()

        # Handle markdown table rows: | Finding | PRESENT |
        if "|" in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2:
                for finding in FINDINGS:
                    for i, cell in enumerate(cells[:-1]):
                        if cell.lower() == finding.lower():
                            choice = extract_label_from_text(cells[i + 1])
                            if choice is not None:
                                parsed[finding] = CHOICE_TO_LABEL[choice]

        # Handle delimiter rows: Finding = PRESENT, Finding: PRESENT, Finding - PRESENT
        for finding in FINDINGS:
            if finding in parsed:
                continue

            pattern = re.compile(
                r"(?:^|\b)" + re.escape(finding) + r"\s*(?:=|:|\-|–|—)\s*([^|;\n]+)",
                flags=re.IGNORECASE,
            )
            match = pattern.search(line)
            if match:
                answer_part = match.group(1).strip()
                choice = extract_label_from_text(answer_part)
                if choice is not None:
                    parsed[finding] = CHOICE_TO_LABEL[choice]

    # Second pass: whole-response regex, useful if CheXagent returns everything
    # in one paragraph rather than one line per finding.
    for finding in FINDINGS:
        if finding in parsed:
            continue

        pattern = re.compile(
            re.escape(finding) + r"\s*(?:=|:|\-|–|—)\s*"
            r"(ABSENT|UNCERTAIN|PRESENT|absent|uncertain|present|negative|positive|not present|indeterminate|unclear|unsure)",
            flags=re.IGNORECASE,
        )
        match = pattern.search(cleaned)
        if match:
            choice = extract_label_from_text(match.group(1))
            if choice is not None:
                parsed[finding] = CHOICE_TO_LABEL[choice]

    return parsed


def parse_all_findings_response(response_text: str):
    cleaned = clean_response_text(response_text)

    parsed = {}
    parse_status = "ok"
    parse_error = ""

    # Try JSON first in case the model still returns valid JSON.
    try:
        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start != -1 and end != -1 and end > start:
            json_text = cleaned[start:end + 1]
            json_obj = json.loads(json_text)
            if isinstance(json_obj, dict):
                parsed = json_obj
                parse_status = "json_parse"
            else:
                raise ValueError("JSON was not an object")
        else:
            raise ValueError("No JSON object found in response")

    except Exception as exc:
        parse_status = "line_parse"
        parse_error = str(exc)
        parsed = parse_line_based_response(cleaned)

    choices = {}

    for finding in FINDINGS:
        raw_value = parsed.get(finding)
        choice = normalize_answer(raw_value)
        choices[finding] = choice

    missing = [finding for finding, choice in choices.items() if choice is None]

    # Special fallback:
    # CheXagent sometimes answers only "No Finding" for a globally normal study.
    # In that case, interpret it as:
    #   No Finding = PRESENT
    #   all other observations = ABSENT
    # This is only applied when nothing else was parsed.
    cleaned_lower = cleaned.lower().strip()
    if len(missing) == len(FINDINGS):
        compact = re.sub(r"[^a-z ]+", " ", cleaned_lower)
        compact = re.sub(r"\s+", " ", compact).strip()
        if compact in {"no finding", "normal", "normal chest x ray", "normal chest radiograph"}:
            for finding in FINDINGS:
                choices[finding] = 0
            choices["No Finding"] = 2
            parse_status = "global_no_finding_fallback"
            parse_error = ""
            missing = []

    if missing:
        if parse_status in {"ok", "json_parse", "line_parse"}:
            parse_status = "partial_parse"
        parse_error = (parse_error + " " if parse_error else "") + f"Missing/unparsed findings: {missing}"

    return choices, parse_status, parse_error.strip()


# ============================================================
# PARSING ΑΠΑΝΤΗΣΗΣ ΓΙΑ ΕΝΑ FINDING
# ============================================================


def parse_one_finding_response(response_text: str, finding: str):
    cleaned = clean_response_text(response_text)
    lower = cleaned.lower().strip()
    finding_lower = finding.lower().strip()

    # v11 preferred format:
    #   Finding Name: absent / uncertain / present
    # This is the cleanest format for CSV generation.
    key_value_pattern = re.compile(
        r"^\s*" + re.escape(finding) + r"\s*[:=\-–—]\s*"
        r"(absent|uncertain|present|negative|positive|not present|unclear|indeterminate|unsure|0|1|2)\s*\.?\s*$",
        flags=re.IGNORECASE,
    )
    kv_match = key_value_pattern.search(cleaned)
    if kv_match:
        label_text = kv_match.group(1).lower().strip()
        choice = normalize_answer(label_text)
        if choice is not None:
            return choice, "key_value_parse", ""

    # Legacy explicit format.
    match = re.search(r"final[_\s-]*label\s*[:=]\s*(absent|uncertain|present|0|1|2)", lower)
    if match:
        label = match.group(1)
        if label in {"0", "1", "2"}:
            return int(label), "final_label_numeric_parse", ""
        return ANSWER_TO_CHOICE[label], "final_label_parse", ""

    if lower in {"0", "1", "2"}:
        return int(lower), "numeric_token_parse", ""

    # Special handling for the "No Finding" observation.
    if finding == "No Finding":
        if lower in {"no finding", "normal", "normal chest x-ray", "normal chest radiograph"}:
            return 2, "no_finding_token_parse", ""

        if re.search(r"\b(no finding|normal|no acute cardiopulmonary abnormality|no acute cardiopulmonary disease)\b", lower):
            return 2, "no_finding_phrase_parse", ""

        # If the No Finding question gets a sentence about only one negative pathology
        # (e.g. "There is no evidence of pneumonia"), this does NOT prove the full
        # study has no abnormal findings. Map conservatively to UNCERTAIN.
        if re.search(NEGATIVE_CUES, lower):
            return 1, "no_finding_specific_negative_to_uncertain", (
                "Specific negative pathology does not establish global No Finding."
            )

        # Any clearly abnormal term means No Finding is absent.
        if re.search(
            r"\b(abnormal|opacity|pneumonia|effusion|atelectasis|pneumothorax|"
            r"cardiomegaly|edema|fracture|consolidation|lesion|support device|line|tube)\b",
            lower,
        ):
            return 0, "no_finding_abnormal_phrase_parse", ""

    # Non-"No Finding" observations.
    else:
        # If the model outputs only the requested finding name, interpret it as PRESENT.
        compact_response = re.sub(r"[^a-z0-9]+", " ", lower).strip()
        compact_finding = re.sub(r"[^a-z0-9]+", " ", finding_lower).strip()
        if compact_response == compact_finding:
            return 2, "finding_name_as_present_parse", ""

        # If the model says "No Finding" / "normal" for a specific disease question,
        # treat the requested disease as ABSENT.
        if lower in {
            "no finding",
            "no findings",
            "normal",
            "normal chest x-ray",
            "normal chest radiograph",
            "no acute cardiopulmonary abnormality",
            "no acute cardiopulmonary disease",
        }:
            return 0, "no_finding_as_absent_parse", ""

        # Finding-aware clinical phrase parsing.
        # This fixes cases like:
        #   finding = Enlarged Cardiomediastinum
        #   response = "The cardiomediastinal silhouette is enlarged."
        mentions_requested_finding = response_mentions_requested_finding(lower, finding)

        if mentions_requested_finding:
            # Uncertainty wins.
            if re.search(UNCERTAIN_CUES, lower):
                return 1, "finding_aware_uncertain_phrase_parse", ""

            # "Resolved" means currently absent.
            if re.search(r"\b(resolved|has resolved|resolution of)\b", lower):
                return 0, "finding_aware_resolved_as_absent_parse", ""

            # Negative cue referring to the requested finding.
            if re.search(NEGATIVE_CUES, lower) or "not present" in lower:
                return 0, "finding_aware_negative_phrase_parse", ""

            # Improved/decreased/unchanged/persistent usually means the finding is still present.
            if re.search(POSITIVE_CUES, lower):
                return 2, "finding_aware_positive_phrase_parse", ""

            # If the requested finding/alias is present with no negative/uncertain cue,
            # count it as PRESENT.
            return 2, "finding_aware_alias_as_present_parse", ""

        # Token-level direct answer parsing.
        choice = normalize_answer(cleaned)
        if choice is not None:
            return choice, "token_parse", ""

        # A negative statement about another disease should NOT automatically become
        # absent for the requested finding.
        if re.search(NEGATIVE_CUES, lower):
            return 1, "other_finding_negative_to_uncertain", (
                "Negative statement did not clearly refer to requested finding."
            )

        if re.search(UNCERTAIN_CUES, lower):
            return 1, "phrase_uncertain_parse", ""

        if re.search(POSITIVE_CUES, lower):
            return 1, "other_finding_positive_to_uncertain", (
                "Positive statement did not clearly refer to requested finding."
            )

    # Final safety fallback for thesis CSV compatibility:
    # an unparseable generative answer is treated as UNCERTAIN, while the raw
    # response and parse_status preserve that this was not a clean model token.
    return 1, "unparsed_to_uncertain", f"Could not confidently parse response, mapped to uncertain: {cleaned}"


# ============================================================
# ΜΕΤΑΤΡΟΠΗ PREDICTION ΣΕ ROW
# ============================================================
# Για compatibility με τα CLIP/BioViL CSVs, κρατάμε ίδια βασικά columns.
#
# Επειδή το CheXagent δεν παράγει logits/probabilities, τα logits είναι NaN.
# Οι probabilities είναι one-hot μόνο για πρακτική συμβατότητα.
# ============================================================


def build_result_row(
    patient_id: str,
    image_paths,
    finding: str,
    choice,
    mode: str,
    raw_response: str,
    parse_status: str,
    parse_error: str,
):
    negative_probability = 1.0 if choice == 0 else 0.0
    uncertain_probability = 1.0 if choice == 1 else 0.0
    positive_probability = 1.0 if choice == 2 else 0.0

    if choice is None:
        prediction = np.nan
        answer_choice = np.nan
        answer_label = "parse_failed"
        negative_probability = np.nan
        uncertain_probability = np.nan
        positive_probability = np.nan
    else:
        prediction = int(choice)
        answer_choice = int(choice)
        answer_label = CHOICE_TO_LABEL[int(choice)]

    return {
        "patient_id": patient_id,
        "image_paths": " | ".join(str(p) for p in image_paths),
        "num_images_used": len(image_paths),
        "finding": finding,

        "negative_logit": np.nan,
        "uncertain_logit": np.nan,
        "positive_logit": np.nan,

        "negative_probability": negative_probability,
        "uncertain_probability": uncertain_probability,
        "positive_probability": positive_probability,

        "prediction": prediction,
        "answer_choice": answer_choice,
        "answer_label": answer_label,

        "model_name": "CheXagent",
        "mode": mode,

        "raw_response": raw_response,
        "parse_status": parse_status,
        "parse_error": parse_error,
    }


# ============================================================
# CHECKPOINT SAVE
# ============================================================
# Επειδή το CheXagent είναι βαρύ generative model, γράφουμε ενδιάμεσα
# αποτελέσματα κάθε N patients. Έτσι αν διακοπεί το run, δεν χάνονται όλα.
# ============================================================


def save_outputs(rows, raw_response_rows, output_dir: Path):
    results_df = pd.DataFrame(rows)
    raw_df = pd.DataFrame(raw_response_rows)

    results_csv = output_dir / "chexagent_patient_results.csv"
    raw_responses_csv = output_dir / "chexagent_raw_responses.csv"
    wide_predictions_csv = output_dir / "chexagent_patient_wide_predictions.csv"

    results_df.to_csv(results_csv, index=False, encoding="utf-8-sig")
    raw_df.to_csv(raw_responses_csv, index=False, encoding="utf-8-sig")

    if not results_df.empty:
        wide_df = results_df.pivot_table(
            index="patient_id",
            columns="finding",
            values="answer_choice",
            aggfunc="first",
        ).reset_index()

        wide_df.to_csv(wide_predictions_csv, index=False, encoding="utf-8-sig")

    return results_df, raw_df


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
        default="StanfordAIMI/CheXagent-2-3b",
        help="Hugging Face model name για CheXagent.",
    )

    parser.add_argument(
        "--dtype",
        type=str,
        default="auto",
        choices=["auto", "float16", "bfloat16", "float32"],
        help="Model dtype. Για RTX 3070 προτείνεται auto ή float16.",
    )

    parser.add_argument(
        "--device",
        type=str,
        default="auto",
        choices=["auto", "cuda", "cpu"],
        help="Single device για το CheXagent. Default auto = cuda αν υπάρχει, αλλιώς cpu.",
    )

    parser.add_argument(
        "--prompt_mode",
        type=str,
        default="one_finding",
        choices=["all_findings", "one_finding"],
        help="one_finding = reliable forced key-value prompt, 14 generations per patient. all_findings = faster JSON prompt, but CheXagent may ignore it.",
    )

    parser.add_argument(
        "--max_new_tokens",
        type=int,
        default=32,
        help="Maximum νέα tokens για κάθε CheXagent απάντηση. Use 32-64 for one_finding; use 768 for --prompt_mode all_findings.",
    )

    parser.add_argument(
        "--max_images_per_patient",
        type=int,
        default=2,
        help="Μέγιστος αριθμός εικόνων ανά patient.",
    )

    parser.add_argument(
        "--max_patients",
        type=int,
        default=None,
        help="Για γρήγορο test. Π.χ. --max_patients 3",
    )

    parser.add_argument(
        "--checkpoint_every",
        type=int,
        default=10,
        help="Αποθήκευση προσωρινών CSV κάθε N patients.",
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        help="Αν υπάρχουν προηγούμενα outputs, συνέχισε από patients που λείπουν.",
    )

    args = parser.parse_args()

    dataset_dir = Path(args.dataset_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)

    results_csv = output_dir / "chexagent_patient_results.csv"
    raw_responses_csv = output_dir / "chexagent_raw_responses.csv"
    run_config_json = output_dir / "chexagent_run_config.json"

    run_config = {
        "dataset_dir": str(dataset_dir),
        "output_dir": str(output_dir),
        "model_name": args.model_name,
        "dtype": args.dtype,
        "device": args.device,
        "prompt_mode": args.prompt_mode,
        "max_new_tokens": args.max_new_tokens,
        "max_images_per_patient": args.max_images_per_patient,
        "max_patients": args.max_patients,
        "checkpoint_every": args.checkpoint_every,
        "answer_mapping": CHOICE_TO_LABEL,
        "note": "CheXagent does not return calibrated logits/probabilities. Probability columns are one-hot compatibility values. v10 default uses one_finding key-value prompting for reliable parsing.",
    }

    with open(run_config_json, "w", encoding="utf-8") as f:
        json.dump(run_config, f, indent=2, ensure_ascii=False)

    device_text = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device_text}")

    tokenizer, model = load_chexagent(
        model_name=args.model_name,
        dtype_arg=args.dtype,
        device_arg=args.device,
    )

    patient_dirs = sorted([
        p for p in dataset_dir.iterdir()
        if p.is_dir() and p.name.lower().startswith("patient")
    ])

    if args.max_patients is not None:
        patient_dirs = patient_dirs[:args.max_patients]

    print(f"Found patient folders: {len(patient_dirs)}")

    rows = []
    raw_response_rows = []
    completed_patients = set()

    if args.resume and results_csv.exists():
        old_df = pd.read_csv(results_csv)
        rows = old_df.to_dict("records")
        completed_patients = set(old_df["patient_id"].astype(str).unique())
        print(f"Resume enabled. Already completed patients: {len(completed_patients)}")

        if raw_responses_csv.exists():
            old_raw_df = pd.read_csv(raw_responses_csv)
            raw_response_rows = old_raw_df.to_dict("records")

    start_time = time.time()

    for patient_index, patient_dir in enumerate(
        tqdm(patient_dirs, desc="Running CheXagent patient-level"),
        start=1,
    ):
        patient_id = patient_dir.name

        if args.resume and patient_id in completed_patients:
            continue

        image_paths = find_patient_images(
            patient_dir=patient_dir,
            max_images_per_patient=args.max_images_per_patient,
        )

        if len(image_paths) == 0:
            print(f"Warning: no images found for {patient_id}")
            continue

        if args.prompt_mode == "all_findings":
            prompt = build_all_findings_prompt()

            response = chexagent_generate(
                tokenizer=tokenizer,
                model=model,
                image_paths=image_paths,
                prompt=prompt,
                max_new_tokens=args.max_new_tokens,
            )

            choices, parse_status, parse_error = parse_all_findings_response(response)

            raw_response_rows.append({
                "patient_id": patient_id,
                "image_paths": " | ".join(str(p) for p in image_paths),
                "num_images_used": len(image_paths),
                "mode": "strict_json_all_findings",
                "raw_response": response,
                "parse_status": parse_status,
                "parse_error": parse_error,
            })

            for finding in FINDINGS:
                row = build_result_row(
                    patient_id=patient_id,
                    image_paths=image_paths,
                    finding=finding,
                    choice=choices[finding],
                    mode="strict_json_all_findings",
                    raw_response=response,
                    parse_status=parse_status,
                    parse_error=parse_error,
                )
                rows.append(row)

        else:
            for finding in FINDINGS:
                prompt = build_one_finding_prompt(finding)

                response = chexagent_generate(
                    tokenizer=tokenizer,
                    model=model,
                    image_paths=image_paths,
                    prompt=prompt,
                    max_new_tokens=min(args.max_new_tokens, 64),
                )

                choice, parse_status, parse_error = parse_one_finding_response(response, finding)

                raw_response_rows.append({
                    "patient_id": patient_id,
                    "image_paths": " | ".join(str(p) for p in image_paths),
                    "num_images_used": len(image_paths),
                    "finding": finding,
                    "mode": "one_finding_key_value",
                    "raw_response": response,
                    "parse_status": parse_status,
                    "parse_error": parse_error,
                })

                row = build_result_row(
                    patient_id=patient_id,
                    image_paths=image_paths,
                    finding=finding,
                    choice=choice,
                    mode="one_finding_key_value",
                    raw_response=response,
                    parse_status=parse_status,
                    parse_error=parse_error,
                )
                rows.append(row)

        if args.checkpoint_every > 0 and patient_index % args.checkpoint_every == 0:
            save_outputs(rows, raw_response_rows, output_dir)
            print(f"Checkpoint saved after {patient_index} patients.")

    results_df, raw_df = save_outputs(rows, raw_response_rows, output_dir)

    elapsed_minutes = (time.time() - start_time) / 60.0

    print()
    print(f"Saved CheXagent results to: {results_csv}")
    print(f"Saved CheXagent raw responses to: {raw_responses_csv}")
    print(f"Saved CheXagent wide predictions to: {output_dir / 'chexagent_patient_wide_predictions.csv'}")
    print(f"Saved run config to: {run_config_json}")
    print()
    print("Results shape:", results_df.shape)
    print("Raw responses shape:", raw_df.shape)
    print(f"Elapsed minutes: {elapsed_minutes:.2f}")

    if not results_df.empty:
        print()
        print("Mode counts:")
        print(results_df["mode"].value_counts())
        print()
        print("Prediction counts:")
        print(results_df["answer_choice"].value_counts(dropna=False).sort_index())
        print()
        print("Images used counts:")
        print(results_df["num_images_used"].value_counts(dropna=False).sort_index())
        print()
        print("Parse status counts:")
        print(results_df["parse_status"].value_counts(dropna=False))


if __name__ == "__main__":
    main()
