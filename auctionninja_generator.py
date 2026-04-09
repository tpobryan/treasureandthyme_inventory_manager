from __future__ import annotations

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
- Identify plausible materials and visible marks carefully
- Surface uncertainty instead of guessing

Important rules:
- Rely first on what is visible in the photos
- Use seller notes only when provided
- Never invent facts
- Do not claim brand, maker, material, age, origin, authenticity, or rarity unless visible in the photos or explicitly provided in seller notes
- If something is uncertain, use cautious wording such as "appears", "possibly", "likely", "unmarked", "not tested", or "illegible mark"
- Do not use hype, salesy, or flowery language
- Do not exaggerate rarity, age, or value
- Do not use words like "stunning", "gorgeous", "beautiful", "must-have", "museum quality", "rare", or "antique" unless directly supported
- Mention visible wear, damage, chips, cracks, losses, staining, patina, scratches, dents, or other flaws in neutral language when visible
- Keep titles SEO-friendly but not overstuffed
- Keep descriptions brief and practical
- Prefer short factual wording over storytelling
- AuctionNinja style is preferred over Etsy or eBay style

Material and mark handling:
- Look carefully for visible clues about material, such as sterling marks, karat marks, glaze, clay body, molded glass, metal tone, wood grain, stone texture, etc.
- Look carefully for visible signatures, maker's marks, hallmarks, stamps, tags, labels, inscriptions, or purity marks
- If a mark is clearly visible, you may state it
- If a mark is only partly visible, use cautious wording such as "appears marked" or "possibly signed"
- If no mark is visible, do not invent one
- Prefer "gold tone" over "gold" when unsupported
- Prefer "silver tone" over "sterling silver" when unsupported
- Prefer "appears to be glass" or "possibly ceramic" when uncertain
- Do not claim gemstones unless clearly supported by the photos or seller notes

Keyword rules:
- Generate 10 to 15 useful comma-separated search phrases
- Include material, form, style, use, and maker related keywords when supported
- Do not include unsupported maker or material keywords
- Keep keywords practical for resale search

Lot handling rules:
- If the seller notes say the listing is a lot, group, set, collection, bundle, or multiple related items, analyze it as a multi-item lot rather than forcing a single-item identification
- Titles and descriptions for lots should make clear that the listing includes multiple items
- If the seller notes include details about specific individual items within the lot, incorporate those details into the description in a concise, factual way
- Do not collapse a lot into one representative item when the seller notes indicate the listing should cover multiple pieces

Field rules:
- identification: what the item most likely is
- confidence_note: short explanation of why this is plausible, including uncertainty when needed
- material_notes: short note about visible material clues and level of certainty
- mark_notes: short note about visible marks, signatures, tags, or lack of marks
- title: concise, searchable, AuctionNinja-appropriate
- description: 1-2 short factual sentences
- category: one broad resale category
- condition_summary: short neutral condition note based on visible evidence and seller notes
- keywords: 10-15 comma-separated search phrases

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
      "material_notes": "",
      "mark_notes": "",
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
      "material_notes": "",
      "mark_notes": "",
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
      "material_notes": "",
      "mark_notes": "",
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
    def try_load(candidate: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def repair_json(candidate: str) -> str:
        repaired = candidate
        repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
        repaired = re.sub(
            r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)',
            r'\1"\2"\3',
            repaired,
        )
        return repaired

    raw = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    for candidate in (cleaned, repair_json(cleaned)):
        parsed = try_load(candidate)
        if parsed is not None:
            return parsed

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        extracted = match.group(0)
        for candidate in (extracted, repair_json(extracted)):
            parsed = try_load(candidate)
            if parsed is not None:
                return parsed

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
        "material_notes": str(opt.get("material_notes", "")).strip(),
        "mark_notes": str(opt.get("mark_notes", "")).strip(),
        "title": str(opt.get("title", "")).strip(),
        "description": str(opt.get("description", "")).strip(),
        "category": str(opt.get("category", "Other")).strip() or "Other",
        "condition_summary": str(opt.get("condition_summary", "")).strip(),
        "keywords": str(opt.get("keywords", "")).strip(),
    }


def _blank_option(rank: int) -> dict[str, str | int]:
    return {
        "rank": rank,
        "identification": "",
        "confidence_note": "",
        "material_notes": "",
        "mark_notes": "",
        "title": "",
        "description": "",
        "category": "Other",
        "condition_summary": "",
        "keywords": "",
    }


def _normalize_output(data: dict[str, Any]) -> dict[str, list[dict[str, str | int]]]:
    raw_options = data.get("options", [])
    if not isinstance(raw_options, list):
        raw_options = []

    normalized: list[dict[str, str | int]] = []
    for i, opt in enumerate(raw_options[:3], start=1):
        if isinstance(opt, dict):
            normalized.append(_normalize_option(opt, i))

    while len(normalized) < 3:
        normalized.append(_blank_option(len(normalized) + 1))

    return {"options": normalized}


class AuctionNinjaGenerator:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1")

    def generate_options(
        self,
        image_paths: list[Path],
        seller_notes: str = "",
    ) -> dict[str, list[dict[str, str | int]]]:
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
Prefer practical resale phrasing.

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

        seller_notes = seller_notes.strip()
        revision_request = revision_request.strip()

        prompt = f"""
    {MASTER_INSTRUCTION}

    Task:
    Revise the current AuctionNinja listing option using the same item photos, seller notes, and revision request.

    Important revision behavior:
    - Preserve the current option's overall identification and direction unless the revision request clearly asks to change it
    - Make the smallest reasonable changes needed to satisfy the revision request
    - Do not rewrite everything from scratch unless necessary
    - Keep the same general title approach, category, and identification unless the user asks otherwise or the current version is clearly unsupported
    - Preserve cautious wording where appropriate
    - Do not become more certain unless the photos or seller notes support it
    - If the revision request conflicts with the photos or seller notes, keep the safer factual version

    Current option:
    identification: {current_option.get("identification", "").strip()}
    confidence_note: {current_option.get("confidence_note", "").strip()}
    material_notes: {current_option.get("material_notes", "").strip()}
    mark_notes: {current_option.get("mark_notes", "").strip()}
    title: {current_option.get("title", "").strip()}
    description: {current_option.get("description", "").strip()}
    category: {current_option.get("category", "").strip()}
    condition_summary: {current_option.get("condition_summary", "").strip()}
    keywords: {current_option.get("keywords", "").strip()}

    Seller notes:
    {seller_notes if seller_notes else "None provided."}

    Revision request:
    {revision_request if revision_request else "No revision request provided."}

    Output requirements:
    - Return only valid JSON
    - Return one revised option only
    - Keep the same field structure
    - Stay concise and AuctionNinja-appropriate

    Return JSON with these keys:
    identification
    confidence_note
    material_notes
    mark_notes
    title
    description
    category
    condition_summary
    keywords
    """.strip()

        content = [{"type": "input_text", "text": prompt}]
        content.extend(_build_image_content(image_paths))

        response = self.client.responses.create(
            model=self.model,
            input=[{"role": "user", "content": content}],
        )

        data = _parse_model_json(response.output_text)
        revised = _normalize_option(data, 1)

        # Soft fallback: preserve key fields if the model returns them blank
        for key, fallback in {
            "identification": current_option.get("identification", ""),
            "confidence_note": current_option.get("confidence_note", ""),
            "material_notes": current_option.get("material_notes", ""),
            "mark_notes": current_option.get("mark_notes", ""),
            "title": current_option.get("title", ""),
            "description": current_option.get("description", ""),
            "category": current_option.get("category", "Other"),
            "condition_summary": current_option.get("condition_summary", ""),
            "keywords": current_option.get("keywords", ""),
        }.items():
            if not str(revised.get(key, "")).strip():
                revised[key] = fallback.strip() if isinstance(fallback, str) else fallback

        return revised
