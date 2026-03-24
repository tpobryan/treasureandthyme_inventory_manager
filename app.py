import base64
import csv
import json
import mimetypes
import os
import re
import shutil
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from openai import OpenAI
from werkzeug.utils import secure_filename

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
CSV_PATH = DATA_DIR / "auction_items.csv"
LOT_STATE_PATH = DATA_DIR / "lot_state.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
import logging

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

CSV_HEADER = [
    "Lot Number",
    "Lead",
    "Description",
    "Condition notes",
    "Low Estimate ($)",
    "High Estimate ($)",
    "Dimensions - Length",
    "Dimensions - Depth",
    "Dimensions - Height",
    "Tags",
    "Reference #",
    "Item Notes",
    "Consigner #",
    "Shipping Available",
    "Category",
]

DEFAULT_CATEGORIES = [
    "Jewelry",
    "Art",
    "Decorative Arts",
    "Pottery & Glass",
    "Collectibles",
    "Fashion Accessories",
    "Books & Ephemera",
    "Toys",
    "Religious",
    "Household",
    "Furniture",
    "Electronics",
    "Tools",
    "Other",
]


def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def ensure_lot_state() -> None:
    if not LOT_STATE_PATH.exists():
        LOT_STATE_PATH.write_text(json.dumps({"last_lot": 1999}, indent=2))


def get_last_lot() -> int:
    ensure_lot_state()
    data = json.loads(LOT_STATE_PATH.read_text())
    return int(data.get("last_lot", 1999))


def get_next_lot_preview() -> int:
    return get_last_lot() + 1


def reserve_next_lot() -> int:
    ensure_lot_state()
    data = json.loads(LOT_STATE_PATH.read_text())
    next_lot = int(data.get("last_lot", 1999)) + 1
    data["last_lot"] = next_lot
    LOT_STATE_PATH.write_text(json.dumps(data, indent=2))
    return next_lot


def ensure_csv_exists() -> None:
    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)


def append_csv_row(row: list[str]) -> None:
    ensure_csv_exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row)


def parse_model_json(text: str) -> dict:
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


def guess_mime_type(path: Path) -> str:
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


def save_uploaded_files(uploaded_files) -> tuple[str, list[Path]]:
    temp_id = uuid.uuid4().hex
    temp_dir = UPLOADS_DIR / temp_id
    temp_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[Path] = []

    for uploaded in uploaded_files:
        app.logger.info("Processing upload: filename=%r content_type=%r", uploaded.filename, uploaded.content_type)

        if not uploaded or not uploaded.filename:
            app.logger.warning("Skipped empty upload")
            continue

        original_name = secure_filename(uploaded.filename)
        app.logger.info("Secure filename: %r", original_name)

        if not original_name:
            app.logger.warning("Skipped file with empty secure filename")
            continue

        suffix = Path(original_name).suffix.lower()
        app.logger.info("Detected suffix: %r", suffix)

        if suffix not in ALLOWED_EXTENSIONS:
            app.logger.warning("Skipped unsupported extension: %r", suffix)
            continue

        filename = original_name
        destination = temp_dir / filename

        counter = 1
        while destination.exists():
            filename = f"{Path(original_name).stem}_{counter}{suffix}"
            destination = temp_dir / filename
            counter += 1

        uploaded.save(destination)
        app.logger.info("Saved upload to: %s", destination)
        saved_files.append(destination)

    return temp_id, saved_files


def analyze_images(saved_files: list[Path], additional_info: str = "") -> dict:
    if not saved_files:
        raise ValueError("No images were uploaded.")

    extra_text = additional_info.strip()

    prompt = f"""
Identify the item in the photos and generate an auctionninja listing.

User-supplied additional info:
{extra_text if extra_text else "None provided."}

Use the additional info when it helps clarify materials, markings, dimensions, condition, attribution, or other details not obvious from the photos.
Do not invent facts. If the additional info conflicts with the photos, prefer cautious wording.

Return ONLY valid JSON.
Do not use markdown.
Do not use code fences.
Do not add commentary.

Required keys:
title
description
tags
category

Rules:
- Title should be SEO-friendly and concise
- Include material, item type, and notable feature if visible or supplied
- Avoid marketing words like beautiful, stunning, gorgeous, lovely, elegant
- Description must be simple, direct, and factual
- Description should be 1–2 short sentences maximum
- Mention markings, dimensions, materials, or condition if visible or provided
- Tags should be comma-separated SEO phrases
- Category should be one broad auction category from this style:
  Jewelry, Art, Decorative Arts, Pottery & Glass, Collectibles, Fashion Accessories,
  Books & Ephemera, Toys, Religious, Household, Furniture, Electronics, Tools, Other
""".strip()

    content = [{"type": "input_text", "text": prompt}]

    for path in saved_files:
        mime = guess_mime_type(path)
        with path.open("rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{mime};base64,{b64}",
            }
        )

    response = client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4.1"),
        input=[{"role": "user", "content": content}],
    )

    data = parse_model_json(response.output_text)

    return {
        "title": str(data.get("title", "")).strip(),
        "description": str(data.get("description", "")).strip(),
        "tags": str(data.get("tags", "")).strip(),
        "category": str(data.get("category", "Other")).strip() or "Other",
    }

def revise_listing_with_ai(
    saved_files: list[Path],
    current_form: dict,
    revision_request: str,
    additional_info: str = "",
) -> dict:
    if not saved_files:
        raise ValueError("No images available for revision.")

    prompt = f"""
You are revising an auction listing draft based on item photos, the current draft, user-supplied additional info, and a revision request.

Return ONLY valid JSON.
Do not use markdown.
Do not use code fences.
Do not add commentary.

Required keys:
title
description
tags
category

Current draft:
Title: {current_form.get("Lead", "").strip()}
Description: {current_form.get("Description", "").strip()}
Tags: {current_form.get("Tags", "").strip()}
Category: {current_form.get("Category", "").strip()}

Additional info from user:
{additional_info.strip() if additional_info.strip() else "None provided."}

Revision request:
{revision_request.strip() if revision_request.strip() else "No revision request provided."}

Rules:
- Apply the requested changes if they are supported by the photos or user-supplied info
- Do not invent facts
- Title should be SEO-friendly and concise
- Description must be simple, direct, and factual
- Description should be 1–2 short sentences maximum
- Avoid marketing language
- Tags should be comma-separated SEO phrases
- Category should be one broad auction category
""".strip()

    content = [{"type": "input_text", "text": prompt}]

    for path in saved_files:
        mime = guess_mime_type(path)
        with path.open("rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{mime};base64,{b64}",
            }
        )

    response = client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4.1"),
        input=[{"role": "user", "content": content}],
    )

    data = parse_model_json(response.output_text)

    return {
        "title": str(data.get("title", "")).strip(),
        "description": str(data.get("description", "")).strip(),
        "tags": str(data.get("tags", "")).strip(),
        "category": str(data.get("category", "Other")).strip() or "Other",
    }

@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        next_lot=get_next_lot_preview(),
        csv_path=str(CSV_PATH.name),
    )


@app.route("/analyze", methods=["POST"])
def analyze():
    app.logger.info("Entered /analyze")

    uploaded_files = request.files.getlist("photos")
    additional_info = request.form.get("additional_info", "").strip()

    app.logger.info("Uploaded file count: %s", len(uploaded_files))
    app.logger.info("Additional info: %r", additional_info)

    temp_id, saved_files = save_uploaded_files(uploaded_files)
    app.logger.info("Temp ID: %s", temp_id)
    app.logger.info("Saved files: %s", [str(p) for p in saved_files])

    if not saved_files:
        app.logger.warning("No valid files were saved")
        flash("Please choose at least one JPG, PNG, WebP, or HEIC image.")
        return redirect(url_for("index"))

    try:
        ai_data = analyze_images(saved_files, additional_info=additional_info)
        app.logger.info("AI data: %s", ai_data)
    except Exception as exc:
        app.logger.exception("AI analysis failed")
        flash(f"AI analysis failed: {exc}")
        return redirect(url_for("index"))

    return render_template(
        "edit.html",
        temp_id=temp_id,
        image_files=[p.name for p in saved_files],
        image_url_prefix=f"/uploads/{temp_id}/",
        next_lot=get_next_lot_preview(),
        categories=DEFAULT_CATEGORIES,
        additional_info=additional_info,
        revision_request="",
        form={
            "Lead": ai_data.get("title", ""),
            "Description": ai_data.get("description", ""),
            "Condition notes": "",
            "Low Estimate ($)": "",
            "High Estimate ($)": "",
            "Dimensions - Length": "",
            "Dimensions - Depth": "",
            "Dimensions - Height": "",
            "Tags": ai_data.get("tags", ""),
            "Reference #": "",
            "Item Notes": additional_info,
            "Consigner #": "",
            "Shipping Available": "",
            "Category": ai_data.get("category", "Other") or "Other",
        },
    )

@app.route("/revise", methods=["POST"])
def revise():
    temp_id = request.form.get("temp_id", "").strip()
    additional_info = request.form.get("additional_info", "").strip()
    revision_request = request.form.get("revision_request", "").strip()

    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    saved_files = sorted([p for p in temp_dir.iterdir() if p.is_file()])

    current_form = {
        "Lead": request.form.get("Lead", "").strip(),
        "Description": request.form.get("Description", "").strip(),
        "Condition notes": request.form.get("Condition notes", "").strip(),
        "Low Estimate ($)": request.form.get("Low Estimate ($)", "").strip(),
        "High Estimate ($)": request.form.get("High Estimate ($)", "").strip(),
        "Dimensions - Length": request.form.get("Dimensions - Length", "").strip(),
        "Dimensions - Depth": request.form.get("Dimensions - Depth", "").strip(),
        "Dimensions - Height": request.form.get("Dimensions - Height", "").strip(),
        "Tags": request.form.get("Tags", "").strip(),
        "Reference #": request.form.get("Reference #", "").strip(),
        "Item Notes": request.form.get("Item Notes", "").strip(),
        "Consigner #": request.form.get("Consigner #", "").strip(),
        "Shipping Available": request.form.get("Shipping Available", "").strip(),
        "Category": request.form.get("Category", "").strip(),
    }

    try:
        ai_data = revise_listing_with_ai(
            saved_files=saved_files,
            current_form=current_form,
            revision_request=revision_request,
            additional_info=additional_info,
        )
    except Exception as exc:
        flash(f"AI revision failed: {exc}")
        return render_template(
            "edit.html",
            temp_id=temp_id,
            image_files=[p.name for p in saved_files],
            image_url_prefix=f"/uploads/{temp_id}/",
            next_lot=get_next_lot_preview(),
            categories=DEFAULT_CATEGORIES,
            additional_info=additional_info,
            revision_request=revision_request,
            form=current_form,
        )

    current_form["Lead"] = ai_data.get("title", current_form["Lead"])
    current_form["Description"] = ai_data.get("description", current_form["Description"])
    current_form["Tags"] = ai_data.get("tags", current_form["Tags"])
    current_form["Category"] = ai_data.get("category", current_form["Category"] or "Other")

    return render_template(
        "edit.html",
        temp_id=temp_id,
        image_files=[p.name for p in saved_files],
        image_url_prefix=f"/uploads/{temp_id}/",
        next_lot=get_next_lot_preview(),
        categories=DEFAULT_CATEGORIES,
        additional_info=additional_info,
        revision_request="",
        form=current_form,
    )

@app.route("/save", methods=["POST"])
def save():
    temp_id = request.form.get("temp_id", "").strip()
    temp_dir = UPLOADS_DIR / temp_id

    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    lot_number = reserve_next_lot()

    row = [
        str(lot_number),
        request.form.get("Lead", "").strip(),
        request.form.get("Description", "").strip(),
        request.form.get("Condition notes", "").strip(),
        request.form.get("Low Estimate ($)", "").strip(),
        request.form.get("High Estimate ($)", "").strip(),
        request.form.get("Dimensions - Length", "").strip(),
        request.form.get("Dimensions - Depth", "").strip(),
        request.form.get("Dimensions - Height", "").strip(),
        request.form.get("Tags", "").strip(),
        request.form.get("Reference #", "").strip(),
        request.form.get("Item Notes", "").strip(),
        request.form.get("Consigner #", "").strip(),
        request.form.get("Shipping Available", "").strip(),
        request.form.get("Category", "").strip(),
    ]

    append_csv_row(row)

    flash(f"Saved lot {lot_number} to {CSV_PATH.name}.")
    return redirect(url_for("index"))


@app.route("/uploads/<temp_id>/<filename>")
def uploaded_file(temp_id: str, filename: str):
    return send_from_directory(UPLOADS_DIR / temp_id, filename)


@app.route("/reset", methods=["POST"])
def reset():
    """
    Optional helper route to clear uploads during testing.
    """
    if UPLOADS_DIR.exists():
        shutil.rmtree(UPLOADS_DIR)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    flash("Temporary uploads cleared.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    app.run(host=host, port=port, debug=debug)
