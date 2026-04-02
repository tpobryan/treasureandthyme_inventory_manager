import csv
import json
import logging
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
from werkzeug.utils import secure_filename

from auctionninja_generator import AuctionNinjaGenerator

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
CSV_PATH = DATA_DIR / "auction_items.csv"
LOT_STATE_PATH = DATA_DIR / "lot_state.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

generator = AuctionNinjaGenerator()

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


def ensure_lot_state() -> None:
    if not LOT_STATE_PATH.exists():
        LOT_STATE_PATH.write_text(json.dumps({"last_lot": 1999}, indent=2), encoding="utf-8")


def get_last_lot() -> int:
    ensure_lot_state()
    data = json.loads(LOT_STATE_PATH.read_text(encoding="utf-8"))
    return int(data.get("last_lot", 1999))


def get_next_lot_preview() -> int:
    return get_last_lot() + 1


def reserve_next_lot() -> int:
    ensure_lot_state()
    data = json.loads(LOT_STATE_PATH.read_text(encoding="utf-8"))
    next_lot = int(data.get("last_lot", 1999)) + 1
    data["last_lot"] = next_lot
    LOT_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
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


def save_uploaded_files(uploaded_files) -> tuple[str, list[Path]]:
    temp_id = uuid.uuid4().hex
    temp_dir = UPLOADS_DIR / temp_id
    temp_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[Path] = []

    for uploaded in uploaded_files:
        app.logger.info(
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
            app.logger.warning("Unsupported extension skipped: %s", suffix)
            continue

        destination = temp_dir / original_name
        counter = 1
        while destination.exists():
            destination = temp_dir / f"{Path(original_name).stem}_{counter}{suffix}"
            counter += 1

        uploaded.save(destination)
        saved_files.append(destination)

    return temp_id, saved_files


def load_saved_files_for_temp_id(temp_id: str) -> list[Path]:
    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        return []
    return sorted([p for p in temp_dir.iterdir() if p.is_file()])


def blank_form(seller_notes: str = "") -> dict[str, str]:
    return {
        "Identification": "",
        "Confidence Note": "",
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
    }


def options_from_request() -> list[dict]:
    options = []
    for i in range(1, 4):
        options.append(
            {
                "rank": i,
                "identification": request.form.get(f"option_{i}_identification", "").strip(),
                "confidence_note": request.form.get(f"option_{i}_confidence_note", "").strip(),
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
    }


def form_from_option(option: dict, seller_notes: str = "") -> dict[str, str]:
    form = blank_form(seller_notes=seller_notes)
    form["Identification"] = str(option.get("identification", "")).strip()
    form["Confidence Note"] = str(option.get("confidence_note", "")).strip()
    form["Title"] = str(option.get("title", "")).strip()
    form["Description"] = str(option.get("description", "")).strip()
    form["Condition Summary"] = str(option.get("condition_summary", "")).strip()
    form["Keywords"] = str(option.get("keywords", "")).strip()
    form["Category"] = str(option.get("category", "Other")).strip() or "Other"
    form["Item Notes"] = seller_notes
    return form


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        next_lot=get_next_lot_preview(),
        csv_path=CSV_PATH.name,
    )


@app.route("/analyze", methods=["POST"])
def analyze():
    app.logger.info("Entered /analyze")

    uploaded_files = request.files.getlist("photos")
    seller_notes = request.form.get("seller_notes", "").strip()

    temp_id, saved_files = save_uploaded_files(uploaded_files)

    if not saved_files:
        flash("Please choose at least one valid image.")
        return redirect(url_for("index"))

    try:
        ai_data = generator.generate_options(saved_files, seller_notes=seller_notes)
        options = ai_data.get("options", [])
        if not options:
            raise ValueError("No listing options were returned.")
        selected = options[0]
        form = form_from_option(selected, seller_notes=seller_notes)
        app.logger.info("Generated %s options", len(options))
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
        seller_notes=seller_notes,
        revision_request="",
        options=options,
        form=form,
    )


@app.route("/choose_option", methods=["POST"])
def choose_option():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()

    saved_files = load_saved_files_for_temp_id(temp_id)
    if not saved_files:
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    options = options_from_request()
    chosen_rank = request.form.get("chosen_rank", "").strip()

    selected_option = None
    for option in options:
        if str(option.get("rank")) == chosen_rank:
            selected_option = option
            break

    if selected_option is None:
        flash("Could not determine which option was selected.")
        form = form_from_request(seller_notes=seller_notes)
    else:
        form = form_from_option(selected_option, seller_notes=seller_notes)

        current_form = form_from_request(seller_notes=seller_notes)
        for key in [
            "Low Estimate ($)",
            "High Estimate ($)",
            "Dimensions - Length",
            "Dimensions - Depth",
            "Dimensions - Height",
            "Reference #",
            "Item Notes",
            "Consigner #",
            "Shipping Available",
        ]:
            form[key] = current_form.get(key, "")

    return render_template(
        "edit.html",
        temp_id=temp_id,
        image_files=[p.name for p in saved_files],
        image_url_prefix=f"/uploads/{temp_id}/",
        next_lot=get_next_lot_preview(),
        categories=DEFAULT_CATEGORIES,
        seller_notes=seller_notes,
        revision_request="",
        options=options,
        form=form,
    )


@app.route("/revise", methods=["POST"])
def revise():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()
    revision_request = request.form.get("revision_request", "").strip()

    saved_files = load_saved_files_for_temp_id(temp_id)
    if not saved_files:
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    options = options_from_request()
    form = form_from_request(seller_notes=seller_notes)

    current_option = {
        "identification": form["Identification"],
        "confidence_note": form["Confidence Note"],
        "title": form["Title"],
        "description": form["Description"],
        "category": form["Category"],
        "condition_summary": form["Condition Summary"],
        "keywords": form["Keywords"],
    }

    try:
        revised = generator.revise_option(
            saved_files,
            current_option=current_option,
            seller_notes=seller_notes,
            revision_request=revision_request,
        )
        form["Identification"] = str(revised.get("identification", form["Identification"])).strip()
        form["Confidence Note"] = str(revised.get("confidence_note", form["Confidence Note"])).strip()
        form["Title"] = str(revised.get("title", form["Title"])).strip()
        form["Description"] = str(revised.get("description", form["Description"])).strip()
        form["Condition Summary"] = str(revised.get("condition_summary", form["Condition Summary"])).strip()
        form["Keywords"] = str(revised.get("keywords", form["Keywords"])).strip()
        form["Category"] = str(revised.get("category", form["Category"])).strip() or "Other"
    except Exception as exc:
        app.logger.exception("AI revision failed")
        flash(f"AI revision failed: {exc}")

    return render_template(
        "edit.html",
        temp_id=temp_id,
        image_files=[p.name for p in saved_files],
        image_url_prefix=f"/uploads/{temp_id}/",
        next_lot=get_next_lot_preview(),
        categories=DEFAULT_CATEGORIES,
        seller_notes=seller_notes,
        revision_request="",
        options=options,
        form=form,
    )


@app.route("/save", methods=["POST"])
def save():
    temp_id = request.form.get("temp_id", "").strip()
    temp_dir = UPLOADS_DIR / temp_id

    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    form = form_from_request(seller_notes=request.form.get("seller_notes", "").strip())
    title = form["Title"]

    lot_number = reserve_next_lot()

    safe_title = slugify_title(title)
    folder_name = f"{lot_number}_{safe_title}"
    final_dir = make_unique_dir(UPLOADS_DIR, folder_name)

    try:
        temp_dir.rename(final_dir)
    except Exception as exc:
        app.logger.exception("Failed to rename image folder")
        flash(f"Warning: saved listing but could not rename image folder: {exc}")

    row = [
        str(lot_number),
        form["Title"],
        form["Description"],
        form["Condition Summary"],
        form["Low Estimate ($)"],
        form["High Estimate ($)"],
        form["Dimensions - Length"],
        form["Dimensions - Depth"],
        form["Dimensions - Height"],
        form["Keywords"],
        form["Reference #"],
        form["Item Notes"],
        form["Consigner #"],
        form["Shipping Available"],
        form["Category"],
    ]

    append_csv_row(row)

    flash(f"Saved lot {lot_number}. Images stored in: {final_dir.name}")
    return redirect(url_for("index"))


@app.route("/uploads/<temp_id>/<filename>")
def uploaded_file(temp_id: str, filename: str):
    return send_from_directory(UPLOADS_DIR / temp_id, filename)


@app.route("/reset", methods=["POST"])
def reset():
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