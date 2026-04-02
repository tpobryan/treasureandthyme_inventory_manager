import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from openai import OpenAI


MASTER_INSTRUCTION = """
You are a dedicated AuctionNinja listing generator.

Your job is to create factual, concise, resale-focused listing content from item photos and optional seller notes.

You should write in a style that matches an experienced resale seller preparing AuctionNinja listings:
- practical
- clear
- honest
- non-hypey
- careful when uncertain
- optimized for search, but not spammy

Primary goals:
- Produce clean, searchable AuctionNinja titles
- Produce short, direct descriptions
- Assign a broad category
- Summarize condition honestly and neutrally
- Generate useful search keywords

Important rules:
- Rely first on what is visible in the photos
- Use seller notes only when provided
- Never invent facts
- Do not claim brand, maker, material, age, origin, authenticity, or rarity unless visible in the photos or explicitly provided in seller notes
- If something is uncertain, use cautious wording such as "appears", "possibly", "likely", "unmarked", or "not tested"
- Do not use hype, salesy, or flowery language
- Do not exaggerate rarity, age, or value
- Do not use words like "stunning", "gorgeous", "beautiful", "must-have", "museum quality", "rare", or "antique" unless directly supported
- Mention visible wear, damage, chips, cracks, losses, staining, patina, scratches, or other flaws in neutral language when visible
- Keep titles SEO-friendly but not overstuffed
- Keep descriptions brief and practical
- Prefer short factual wording over storytelling
- When seller notes provide dimensions, markings, or flaws, include them where useful
- AuctionNinja style is preferred over Etsy or eBay style

Field rules:
- identification: what the item most likely is
- confidence_note: short explanation of why this is a plausible identification, including uncertainty when needed
- title: concise, searchable, AuctionNinja-appropriate
- description: 1-2 short factual sentences
- category: one broad resale category
- condition_summary: short neutral condition note based on visible evidence and seller notes
- keywords: comma-separated search phrases

When identification is uncertain, produce the top three plausible identifications ranked from most likely to least likely.
Each option should be meaningfully different if possible.
Do not create artificial differences just to fill three slots.

Return only valid JSON with this structure:
{
  "options": [
    {
      "rank": 1,
      "identification": "",
      "confidence_note": "",
      "title": "",
      "description": "",
      "category": "",
      "condition_summary": "",
      "keywords": ""
    },
    {
      "rank": 2,
      "identification": "",
      "confidence_note": "",
      "title": "",
      "description": "",
      "category": "",
      "condition_summary": "",
      "keywords": ""
    },
    {
      "rank": 3,
      "identification": "",
      "confidence_note": "",
      "title": "",
      "description": "",
      "category": "",
      "condition_summary": "",
      "keywords": ""
    }
  ]
}
""".strip()


def _guess_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix in {".heic", ".heif"}:
        return "image/heic"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _parse_model_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"Could not parse model response as JSON: {raw}")


def _build_image_content(image_paths: list[Path]) -> list[dict[str, str]]:
    content: list[dict[str, str]] = []
    for path in image_paths:
        mime = _guess_mime_type(path)
        with path.open("rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{mime};base64,{b64}",
            }
        )
    return content


def _normalize_option(opt: dict[str, Any], rank: int) -> dict[str, str | int]:
    return {
        "rank": rank,
        "identification": str(opt.get("identification", "")).strip(),
        "confidence_note": str(opt.get("confidence_note", "")).strip(),
        "title": str(opt.get("title", "")).strip(),
        "description": str(opt.get("description", "")).strip(),
        "category": str(opt.get("category", "Other")).strip() or "Other",
        "condition_summary": str(opt.get("condition_summary", "")).strip(),
        "keywords": str(opt.get("keywords", "")).strip(),
    }


def _normalize_output(data: dict[str, Any]) -> dict[str, list[dict[str, str | int]]]:
    raw_options = data.get("options", [])
    if not isinstance(raw_options, list):
        raw_options = []

    normalized = []
    for i, opt in enumerate(raw_options[:3], start=1):
        if isinstance(opt, dict):
            normalized.append(_normalize_option(opt, i))

    while len(normalized) < 3:
        normalized.append(
            {
                "rank": len(normalized) + 1,
                "identification": "",
                "confidence_note": "",
                "title": "",
                "description": "",
                "category": "Other",
                "condition_summary": "",
                "keywords": "",
            }
        )

    return {"options": normalized}


class AuctionNinjaGenerator:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1")

    def generate_options(self, image_paths: list[Path], seller_notes: str = "") -> dict[str, list[dict[str, str | int]]]:
        if not image_paths:
            raise ValueError("No images provided.")

        seller_notes = seller_notes.strip()

        prompt = f"""
{MASTER_INSTRUCTION}

Task:
Generate the top three plausible AuctionNinja listing options from the item photos and optional seller notes.

Seller notes:
{seller_notes if seller_notes else "None provided."}

Use seller notes only as supplied facts.
Do not invent missing details.
If the photos are unclear, use cautious wording.

Return only valid JSON.
""".strip()

        content = [{"type": "input_text", "text": prompt}]
        content.extend(_build_image_content(image_paths))

        response = self.client.responses.create(
            model=self.model,
            input=[{"role": "user", "content": content}],
        )

        data = _parse_model_json(response.output_text)
        return _normalize_output(data)

    def revise_option(
        self,
        image_paths: list[Path],
        current_option: dict[str, str],
        seller_notes: str = "",
        revision_request: str = "",
    ) -> dict[str, str | int]:
        if not image_paths:
            raise ValueError("No images provided for revision.")

        prompt = f"""
{MASTER_INSTRUCTION}

Task:
Revise the current AuctionNinja listing option using the same item photos, seller notes, and revision request.

Current option:
identification: {current_option.get("identification", "").strip()}
confidence_note: {current_option.get("confidence_note", "").strip()}
title: {current_option.get("title", "").strip()}
description: {current_option.get("description", "").strip()}
category: {current_option.get("category", "").strip()}
condition_summary: {current_option.get("condition_summary", "").strip()}
keywords: {current_option.get("keywords", "").strip()}

Seller notes:
{seller_notes.strip() if seller_notes.strip() else "None provided."}

Revision request:
{revision_request.strip() if revision_request.strip() else "No revision request provided."}

Apply the requested changes when supported by the photos or seller notes.
Do not invent facts.

Return only valid JSON for one option with this structure:
{
  "identification": "",
  "confidence_note": "",
  "title": "",
  "description": "",
  "category": "",
  "condition_summary": "",
  "keywords": ""
}
""".strip()

        content = [{"type": "input_text", "text": prompt}]
        content.extend(_build_image_content(image_paths))

        response = self.client.responses.create(
            model=self.model,
            input=[{"role": "user", "content": content}],
        )

        data = _parse_model_json(response.output_text)
        return _normalize_option(data, 1)