import csv
import io
import os
import re
import secrets
import uuid
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import request, session, flash, current_app
from markupsafe import Markup
from werkzeug.utils import secure_filename

from image_processor import ALLOWED_EXTENSIONS, HEIF_SUPPORT_ENABLED, optimize_image
import database as db_module
from database import (
    DATA_DIR,
    EXPORTS_DIR,
    fetch_active_draft as db_fetch_active_draft,
    set_active_draft as db_set_active_draft,
    clear_active_draft as db_clear_active_draft,
    fetch_all_active_drafts as db_fetch_all_active_drafts,
)

UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

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

def auth_enabled() -> bool:
    return bool(os.getenv("APP_LOGIN_PASSWORD", "").strip())

def auth_username() -> str:
    return os.getenv("APP_LOGIN_USERNAME", "admin").strip() or "admin"

def auth_password() -> str:
    return os.getenv("APP_LOGIN_PASSWORD", "").strip()

def is_authenticated() -> bool:
    return bool(session.get("authenticated"))

def get_draft_owner_token() -> str:
    token = str(session.get("draft_owner_token", "")).strip()
    if token:
        return token
    token = secrets.token_urlsafe(24)
    session["draft_owner_token"] = token
    return token

def get_csrf_token() -> str:
    token = str(session.get("csrf_token", "")).strip()
    if token:
        return token
    token = secrets.token_urlsafe(24)
    session["csrf_token"] = token
    return token

def csrf_input() -> Markup:
    return Markup(
        f'<input type="hidden" name="csrf_token" value="{get_csrf_token()}">'
    )

def validate_csrf_token(submitted_token: str) -> bool:
    session_token = str(session.get("csrf_token", "")).strip()
    submitted = str(submitted_token or "").strip()
    return bool(session_token and submitted and secrets.compare_digest(session_token, submitted))

def is_safe_local_url(target: str) -> bool:
    if not target:
        return False
    parsed = urlsplit(target)
    return not parsed.scheme and not parsed.netloc and target.startswith("/")

def load_saved_files_for_temp_id(temp_id: str) -> list[Path]:
    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        return []
    return sorted([p for p in temp_dir.iterdir() if p.is_file()])

def get_active_draft() -> dict[str, Any] | None:
    temp_id = session.get("active_temp_id")
    if not temp_id:
        return None

    draft = db_fetch_active_draft(temp_id, owner_token=get_draft_owner_token())
    if not draft:
        session.pop("active_temp_id", None)
        return None

    temp_id = draft["temp_id"]
    saved_files = load_saved_files_for_temp_id(temp_id)
    if not saved_files:
        db_clear_active_draft(temp_id=temp_id, owner_token=get_draft_owner_token())
        session.pop("active_temp_id", None)
        return None

    draft["image_files"] = [p.name for p in saved_files]
    draft["image_count"] = len(saved_files)
    return draft

def get_orphaned_drafts() -> list[dict[str, Any]]:
    active_temp_id = session.get("active_temp_id")
    all_drafts = db_fetch_all_active_drafts(owner_token=get_draft_owner_token())
    
    orphans = []
    for draft in all_drafts:
        if draft["temp_id"] == active_temp_id:
            continue
            
        saved_files = load_saved_files_for_temp_id(draft["temp_id"])
        if not saved_files:
            db_clear_active_draft(draft["temp_id"], owner_token=get_draft_owner_token())
            continue
            
        draft["image_count"] = len(saved_files)
        
        title = draft.get("form", {}).get("Title", "").strip()
        if not title and draft.get("options"):
            title = draft["options"][0].get("title", "").strip()
        draft["display_title"] = title or "Untitled Draft"
        
        orphans.append(draft)
        
    return orphans

def set_active_draft(
    temp_id: str,
    seller_notes: str,
    options: list[dict],
    form: dict[str, str],
    revision_request: str = "",
) -> None:
    session["active_temp_id"] = temp_id
    db_set_active_draft(
        temp_id=temp_id,
        owner_token=get_draft_owner_token(),
        seller_notes=seller_notes,
        options=options,
        form=form,
        revision_request=revision_request,
    )

def clear_active_draft(temp_id: str | None = None) -> None:
    target_id = temp_id or session.get("active_temp_id")
    if target_id:
        db_clear_active_draft(temp_id=target_id, owner_token=get_draft_owner_token())
    
    if session.get("active_temp_id") == target_id or not temp_id:
        session.pop("active_temp_id", None)

def current_edit_context(
    temp_id: str,
    seller_notes: str,
) -> tuple[list[Path], list[dict], dict[str, str]]:
    saved_files = load_saved_files_for_temp_id(temp_id)
    options = options_from_request()
    form = form_from_request(seller_notes=seller_notes)
    return saved_files, options, form

def blank_form(seller_notes: str = "") -> dict[str, str]:
    return {
        "Identification": "",
        "Confidence Note": "",
        "Material Notes": "",
        "Mark Notes": "",
        "Title": "",
        "Description": "",
        "Condition Summary": "",
        "Keywords": "",
        "Category": "Other",
        "Low Estimate ($)": "",
        "High Estimate ($)": "",
        "Dimensions - Length": "",
        "Dimensions - Depth": "",
        "Dimensions - Height": "",
        "Reference #": "",
        "Item Notes": seller_notes,
        "Consigner #": "",
        "Shipping Available": "",
        "Listing Strategy": "auction",
        "eBay SEO Title": "",
        "eBay Category Suggestion": "",
        "eBay Item Specifics": "{}",
        "Etsy Tags": "",
        "Etsy Materials": "",
        "Etsy Taxonomy ID": "",
        "Etsy Who Made": "someone_else",
        "Etsy When Made": "2020_2026",
        "Etsy Is Supply": "no",
        "Price": "0.00",
        "Price Rationale": "",
        "Quantity": "1",
        "Publish to eBay": "no",
        "Publish to Etsy": "",
        "Item Weight": "",
        "Item Weight Unit": "lb",
    }

def options_from_request() -> list[dict]:
    options = []
    for i in range(1, 4):
        options.append(
            {
                "rank": i,
                "identification": request.form.get(f"option_{i}_identification", "").strip(),
                "confidence_note": request.form.get(f"option_{i}_confidence_note", "").strip(),
                "material_notes": request.form.get(f"option_{i}_material_notes", "").strip(),
                "mark_notes": request.form.get(f"option_{i}_mark_notes", "").strip(),
                "title": request.form.get(f"option_{i}_title", "").strip(),
                "description": request.form.get(f"option_{i}_description", "").strip(),
                "category": request.form.get(f"option_{i}_category", "").strip() or "Other",
                "condition_summary": request.form.get(f"option_{i}_condition_summary", "").strip(),
                "keywords": request.form.get(f"option_{i}_keywords", "").strip(),
            }
        )
    return options

def form_from_request(seller_notes: str = "") -> dict[str, str]:
    return {
        "Identification": request.form.get("Identification", "").strip(),
        "Confidence Note": request.form.get("Confidence Note", "").strip(),
        "Material Notes": request.form.get("Material Notes", "").strip(),
        "Mark Notes": request.form.get("Mark Notes", "").strip(),
        "Title": request.form.get("Title", "").strip(),
        "Description": request.form.get("Description", "").strip(),
        "Condition Summary": request.form.get("Condition Summary", "").strip(),
        "Keywords": request.form.get("Keywords", "").strip(),
        "Category": request.form.get("Category", "").strip() or "Other",
        "Low Estimate ($)": request.form.get("Low Estimate ($)", "").strip(),
        "High Estimate ($)": request.form.get("High Estimate ($)", "").strip(),
        "Dimensions - Length": request.form.get("Dimensions - Length", "").strip(),
        "Dimensions - Depth": request.form.get("Dimensions - Depth", "").strip(),
        "Dimensions - Height": request.form.get("Dimensions - Height", "").strip(),
        "Reference #": request.form.get("Reference #", "").strip(),
        "Item Notes": request.form.get("Item Notes", seller_notes).strip(),
        "Consigner #": request.form.get("Consigner #", "").strip(),
        "Shipping Available": request.form.get("Shipping Available", "").strip(),
        "Listing Strategy": request.form.get("Listing Strategy", "auction").strip(),
        "eBay SEO Title": request.form.get("eBay SEO Title", "").strip(),
        "eBay Category Suggestion": request.form.get("eBay Category Suggestion", "").strip(),
        "eBay Item Specifics": request.form.get("eBay Item Specifics", "{}").strip(),
        "Etsy Tags": request.form.get("Etsy Tags", "").strip(),
        "Etsy Materials": request.form.get("Etsy Materials", "").strip(),
        "Etsy Taxonomy ID": request.form.get("Etsy Taxonomy ID", "").strip(),
        "Etsy Shipping Profile ID": request.form.get("Etsy Shipping Profile ID", "").strip(),
        "Publish to eBay": request.form.get("Publish to eBay", "").strip(),
        "Publish to Etsy": request.form.get("Publish to Etsy", "").strip(),
        "Item Weight": request.form.get("Item Weight", "").strip(),
        "Item Weight Unit": request.form.get("Item Weight Unit", "lb").strip(),
    }

def form_from_option(option: dict, seller_notes: str = "", strategy: str = "auction") -> dict[str, str]:
    form = blank_form(seller_notes=seller_notes)
    form["Listing Strategy"] = strategy
    form["Identification"] = str(option.get("identification", "")).strip()
    form["Confidence Note"] = str(option.get("confidence_note", "")).strip()
    form["Material Notes"] = str(option.get("material_notes", "")).strip()
    form["Mark Notes"] = str(option.get("mark_notes", "")).strip()
    form["Title"] = str(option.get("title", "")).strip()
    form["Description"] = str(option.get("description", "")).strip()
    form["Condition Summary"] = str(option.get("condition_summary", "")).strip()
    form["Keywords"] = str(option.get("keywords", "")).strip()
    form["Category"] = str(option.get("category", "Other")).strip() or "Other"
    form["Item Notes"] = seller_notes
    
    platform_data = option.get("platform_data", {})
    if platform_data:
        ebay = platform_data.get("ebay", {})
        form["eBay SEO Title"] = str(ebay.get("seo_title", "")).strip()
        form["eBay Category Suggestion"] = str(ebay.get("category_suggestion", "")).strip()
        form["eBay Item Specifics"] = json.dumps(ebay.get("item_specifics", {}))
        
        etsy = platform_data.get("etsy", {})
        form["Etsy Tags"] = ", ".join(etsy.get("tags", [])) if isinstance(etsy.get("tags"), list) else ""
        form["Etsy Materials"] = ", ".join(etsy.get("materials", [])) if isinstance(etsy.get("materials"), list) else ""
        form["Etsy Taxonomy ID"] = str(etsy.get("taxonomy_id", "")).strip()
        form["Etsy Who Made"] = str(etsy.get("who_made", "someone_else")).strip()
        form["Etsy When Made"] = str(etsy.get("when_made", "2020_2026")).strip()
        form["Etsy Is Supply"] = "yes" if etsy.get("is_supply") else "no"
        form["Price"] = f"{float(etsy.get('suggested_price', 0.0)):.2f}"
        form["Price Rationale"] = str(etsy.get("price_rationale", "")).strip()
        form["Quantity"] = str(etsy.get("suggested_quantity", 1)).strip()
        
    return form

def form_from_saved_item(record: dict[str, str]) -> dict[str, str]:
    platform_data = {}
    try:
        platform_data_str = record.get("platform_data")
        if platform_data_str:
            platform_data = json.loads(platform_data_str)
    except Exception:
        pass
        
    return {
        "Title": record.get("title", ""),
        "Description": record.get("description", ""),
        "Condition Summary": record.get("condition_notes", ""),
        "Keywords": record.get("tags", ""),
        "Category": record.get("category", "") or "Other",
        "Low Estimate ($)": record.get("low_estimate", ""),
        "High Estimate ($)": record.get("high_estimate", ""),
        "Dimensions - Length": record.get("dimensions_length", ""),
        "Dimensions - Depth": record.get("dimensions_depth", ""),
        "Dimensions - Height": record.get("dimensions_height", ""),
        "Reference #": record.get("reference_number", ""),
        "Item Notes": record.get("item_notes", ""),
        "Consigner #": record.get("consigner_number", ""),
        "Shipping Available": record.get("shipping_available", "") or "No",
        "Listing Strategy": record.get("listing_strategy", "auction"),
        "eBay Category ID": platform_data.get("ebay_category_id", ""),
        "Etsy Taxonomy ID": platform_data.get("etsy_taxonomy_id", ""),
        "Publish to eBay": "yes" if platform_data.get("publish_to_ebay") else "",
        "Publish to Etsy": "yes" if platform_data.get("publish_to_etsy") else "",
    }

def parse_decimal_field(value: str) -> float | None:
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    return float(cleaned)

def validate_save_form(form: dict[str, str]) -> list[str]:
    errors: list[str] = []

    if not form.get("Title", "").strip():
        errors.append("Title is required before saving.")

    low_raw = form.get("Low Estimate ($)", "")
    high_raw = form.get("High Estimate ($)", "")

    try:
        low_value = parse_decimal_field(low_raw)
    except ValueError:
        errors.append("Low Estimate ($) must be a number if provided.")
        low_value = None

    try:
        high_value = parse_decimal_field(high_raw)
    except ValueError:
        errors.append("High Estimate ($) must be a number if provided.")
        high_value = None

    if low_value is not None and high_value is not None and low_value > high_value:
        errors.append("Low Estimate ($) cannot be greater than High Estimate ($).")

    return errors

def build_csv_text(rows: list[list[str]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADER)
    writer.writerows(rows)
    return output.getvalue()

def archive_export_csv(filename: str, csv_text: str) -> Path:
    export_dir = EXPORTS_DIR
    export_dir.mkdir(parents=True, exist_ok=True)
    archive_path = export_dir / filename
    archive_path.write_text(csv_text, encoding="utf-8", newline="")
    return archive_path

def slugify_title(title: str, max_length: int = 80) -> str:
    title = title.lower().strip()
    title = re.sub(r"[^a-z0-9]+", " ", title)
    title = re.sub(r"\s+", "-", title).strip("-")
    if len(title) > max_length:
        title = title[:max_length].rstrip("-")
    return title or "item"

def make_unique_dir(base_dir: Path, name: str) -> Path:
    target = base_dir / name
    counter = 1
    while target.exists():
        target = base_dir / f"{name}-{counter}"
        counter += 1
    return target

def save_uploaded_files_to_dir(uploaded_files, temp_dir: Path) -> list[Path]:
    saved_files: list[Path] = []

    for uploaded in uploaded_files:
        current_app.logger.info(
            "Processing upload: filename=%r content_type=%r",
            uploaded.filename,
            uploaded.content_type,
        )

        if not uploaded or not uploaded.filename:
            continue

        original_name = secure_filename(uploaded.filename)
        if not original_name:
            continue

        suffix = Path(original_name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            if suffix in {".heic", ".heif"} and not HEIF_SUPPORT_ENABLED:
                flash(
                    "HEIC/HEIF upload is not available on this server yet. "
                    "Install pi-heif (preferred on Raspberry Pi) or convert the photo to JPG first."
                )
            current_app.logger.warning("Unsupported extension skipped: %s", suffix)
            continue

        raw_destination = temp_dir / original_name
        counter = 1
        while raw_destination.exists():
            raw_destination = temp_dir / f"{Path(original_name).stem}_{counter}{suffix}"
            counter += 1

        uploaded.save(raw_destination)

        try:
            optimized_destination = raw_destination.with_suffix(".jpg")
            optimized_path = optimize_image(raw_destination, optimized_destination)

            if raw_destination != optimized_path and raw_destination.exists():
                raw_destination.unlink()

            saved_files.append(optimized_path)
            current_app.logger.info("Saved optimized image: %s", optimized_path)

        except Exception as exc:
            current_app.logger.exception("Image optimization failed for %s", raw_destination)
            flash(f"Image optimization failed for {original_name}: {exc}")

            # fallback: keep original if optimization fails
            saved_files.append(raw_destination)

    return saved_files

def save_uploaded_files(uploaded_files) -> tuple[str, list[Path]]:
    temp_id = uuid.uuid4().hex
    temp_dir = UPLOADS_DIR / temp_id
    temp_dir.mkdir(parents=True, exist_ok=True)

    saved_files = save_uploaded_files_to_dir(uploaded_files, temp_dir)
    return temp_id, saved_files
