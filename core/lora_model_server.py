
from __future__ import annotations

import json
from pathlib import Path

from dataclasses import dataclass
from typing import Any


import torch
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig


# Hugging Face will download/cache this automatically on first run.
BASE_MODEL = "Qwen/Qwen3-4B"

# LoRA adapter bundled in this repo (core/qwen-email-cleaner-lora-v01).
ADAPTER_DIR = Path(__file__).resolve().parent / "qwen-email-cleaner-lora-v01"

HOST = "127.0.0.1"
PORT = 8008


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ClassifyRequest(BaseModel):
    sender: str = ""
    sender_domain: str = ""
    subject: str = ""
    snippet: str = ""
    gmail_labels: list[str] | None = None
    has_attachments: bool = False
    attachment_count: int = 0
    categories: list[dict[str, Any]]
    examples: dict[str, list[dict[str, Any]]] | None = None


class ClassificationResponse(BaseModel):
    category: str
    reason: str
    raw_output: str


@dataclass(frozen=True)
class Classification:
    category: str
    reason: str


class ModelServerError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# App globals
# ---------------------------------------------------------------------------

app = FastAPI(title="Local Qwen LoRA Email Classifier")

model = None
tokenizer = None



# ---------------------------------------------------------------------------
# Small helpers copied/recreated from your cleaner flow
# ---------------------------------------------------------------------------

def truncate_for_model(text: str, max_chars: int) -> str:
    text = text or ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def model_visible_snippet(snippet: str) -> str:
    snippet = snippet or ""
    snippet = " ".join(snippet.strip().split())
    return truncate_for_model(snippet, 700)


def _metadata_lines(
    *,
    sender: str,
    sender_domain: str = "",
    subject: str = "",
    snippet: str = "",
    gmail_labels: list[str] | None = None,
    has_attachments: bool = False,
    attachment_count: int = 0,
) -> str:
    sender = truncate_for_model((sender or "").strip(), 300)
    sender_domain = truncate_for_model((sender_domain or "").strip(), 120)
    subject = truncate_for_model((subject or "").strip(), 300)
    snippet = model_visible_snippet(snippet)

    labels = list(gmail_labels or [])
    labels_s = ", ".join(str(x) for x in labels) if labels else "(none)"
    labels_s = truncate_for_model(labels_s, 300)

    return (
        f"From: {sender}\n"
        f"Sender domain: {sender_domain or '(unknown)'}\n"
        f"Subject: {subject}\n"
        f"Snippet: {snippet}\n"
        f"Gmail labels: {labels_s}\n"
        f"Has attachments: {'yes' if has_attachments else 'no'}\n"
        f"Attachment count: {int(attachment_count)}\n"
    )


_FEW_SHOT_CHAR_BUDGET = 2000
_FEW_SHOT_PER_EXAMPLE_CAP = 250


def _build_few_shot_block(
    examples: dict[str, list[dict[str, Any]]],
    allowed_names: list[str],
) -> str:
    if not examples:
        return ""

    lines: list[str] = [
        "Below are real past emails the user already labeled. "
        "Use them as ground-truth examples of the user's preferences."
    ]

    used = 0
    rendered = 0

    for category, items in examples.items():
        if category not in allowed_names or not items:
            continue

        for ex in items:
            sender = (ex.get("sender") or "").strip()
            sd = (ex.get("sender_domain") or "").strip()
            subject = (ex.get("subject") or "").strip()
            snippet = (ex.get("snippet") or "").strip()
            snippet = truncate_for_model(snippet, _FEW_SHOT_PER_EXAMPLE_CAP)

            gl = ex.get("gmail_labels")
            if isinstance(gl, list):
                g_labels: list[str] | None = [str(x) for x in gl]
            else:
                g_labels = None

            ha = bool(ex.get("has_attachments", False))
            ac = int(ex.get("attachment_count") or 0)

            meta = _metadata_lines(
                sender=sender,
                sender_domain=sd,
                subject=subject,
                snippet=snippet,
                gmail_labels=g_labels,
                has_attachments=ha,
                attachment_count=ac,
            )

            meta_indented = "\n".join(
                f"  {ln}" for ln in meta.rstrip("\n").split("\n")
            )

            block = (
                "\nExample:\n"
                f"{meta_indented}\n"
                f'  Correct answer: {{"category":"{category}"}}'
            )

            if used + len(block) > _FEW_SHOT_CHAR_BUDGET:
                break

            lines.append(block)
            used += len(block)
            rendered += 1

        if used >= _FEW_SHOT_CHAR_BUDGET:
            break

    if rendered == 0:
        return ""

    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Prompt builder — same logic as your Ollama client, but used for LoRA
# ---------------------------------------------------------------------------

def _format_category_rule_label(cat: dict[str, Any]) -> str:
    action = (cat.get("action") or "KEEP").upper()
    important = bool(cat.get("important", False))
    if action == "IMPORTANT":
        action = "KEEP"
        important = True
    if action == "KEEP" and important:
        return "KEEP, star"
    return action


def build_messages(req: ClassifyRequest) -> tuple[list[dict[str, str]], list[str]]:
    category_list = list(req.categories)

    if not category_list:
        raise ModelServerError("No categories configured; check config/rules.json")

    allowed_names = [c["name"] for c in category_list]

    category_block = "\n".join(
        f'  - "{c["name"]}" [{_format_category_rule_label(c)}]: '
        f'{c.get("description", "").strip()}'
        for c in category_list
    )

    system_prompt = (
        "You are an email triage assistant. For ONE email, pick the single best "
        "matching category from the allowed list. Respond with STRICT JSON and "
        "nothing else.\n"
        "You only see metadata (headers, snippet, Gmail labels, attachment "
        "hints) - not the full message body.\n\n"
        "Required JSON schema:\n"
        "{\n"
        '  "category": "<one of the allowed category names, copied verbatim>",\n'
        '  "reason":   "<one short sentence explaining the choice>"\n'
        "}\n\n"
        "Category list format: \"Name [ACTION]\" or \"Name [KEEP, star]\" shows the "
        "user's configured handling. You only output the category name; the app "
        "applies the action from rules.\n\n"
        "When you are not sure which category fits, use category=\"Unknown\".\n"
        "Prefer a specific category over Unknown when there is a reasonable match.\n\n"
        "Hard rules:\n"
        "- category MUST be one of: "
        + ", ".join(f'"{n}"' for n in allowed_names)
        + ".\n"
        "- Do NOT output action or confidence. Do NOT invent categories.\n"
        "- Do NOT add extra fields. Do NOT wrap in markdown.\n"
        "- Keep the reason to one short sentence.\n"
    )

    few_shot_block = _build_few_shot_block(req.examples or {}, allowed_names)

    meta = _metadata_lines(
        sender=req.sender,
        sender_domain=req.sender_domain,
        subject=req.subject,
        snippet=req.snippet,
        gmail_labels=req.gmail_labels,
        has_attachments=req.has_attachments,
        attachment_count=req.attachment_count,
    )

    user_prompt = (
        "Allowed categories:\n"
        f"{category_block}\n\n"
        f"{few_shot_block}"
        "Now classify this email:\n"
        f"{meta}\n"
        "Return the JSON object now."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    return messages, allowed_names


# ---------------------------------------------------------------------------
# Parse model output — same behavior as your old client
# ---------------------------------------------------------------------------

def parse_classification(content: str, allowed: list[str]) -> Classification:
    raw = content.strip()

    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[-1] if raw.lower().startswith("json") else raw

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")

        if start == -1 or end == -1 or end <= start:
            raise ModelServerError(f"Could not parse JSON from model output: {content!r}")

        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as e:
            raise ModelServerError(f"Could not parse JSON from model output: {e}") from e

    category = str(parsed.get("category", "")).strip()
    reason = str(parsed.get("reason", "")).strip()

    if category not in allowed:
        lower_map = {n.lower(): n for n in allowed}
        if category.lower() in lower_map:
            category = lower_map[category.lower()]
        else:
            category = "Unknown"

    return Classification(
        category=category,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def load_model() -> None:
    global model, tokenizer

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available. Use your Python 3.11 CUDA venv.")

    print("Loading tokenizer from Hugging Face cache/download...")
    tokenizer = AutoTokenizer.from_pretrained(
        BASE_MODEL,
        trust_remote_code=True,
    )

    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    print("Loading base model in 4-bit from Hugging Face cache/download...")
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_use_double_quant=True,
    )

    base_model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        quantization_config=bnb_config,
        torch_dtype=torch.float16,
        device_map={"": 0},
        trust_remote_code=True,
    )

    print("Loading LoRA adapter...")
    model = PeftModel.from_pretrained(
        base_model,
        ADAPTER_DIR,
        local_files_only=True,
    )

    model.eval()

    print("Model loaded.")
    print("GPU:", torch.cuda.get_device_name(0))


@app.on_event("startup")
def startup_event() -> None:
    load_model()


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, Any]:
    loaded = model is not None and tokenizer is not None
    return {
        "status": "ok" if loaded else "loading",
        "model_loaded": loaded,
        "base_model": BASE_MODEL,
        "adapter_dir": str(ADAPTER_DIR),
        "cuda": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    }






@app.post("/classify", response_model=ClassificationResponse)
def classify(req: ClassifyRequest) -> ClassificationResponse:
    if model is None or tokenizer is None:
        raise RuntimeError("Model is not loaded yet.")

    messages, allowed_names = build_messages(req)

    prompt = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    )

    inputs = tokenizer(
        prompt,
        return_tensors="pt",
    ).to("cuda")

    with torch.no_grad():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=256,
            do_sample=True,
            temperature=0.1,
            top_p=1.0,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )

    prompt_len = inputs["input_ids"].shape[-1]
    new_tokens = output_ids[0][prompt_len:]
    output_text = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()

    if not output_text:
        raise ModelServerError("Model returned empty output.")

    parsed = parse_classification(output_text, allowed_names)

    return ClassificationResponse(
        category=parsed.category,
        reason=parsed.reason,
        raw_output=output_text,
    )


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)