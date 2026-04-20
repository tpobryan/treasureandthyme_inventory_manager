from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename
from auctionninja_generator import AuctionNinjaGenerator
from ftp_client import (
    delete_lot_photos_from_auctionninja,
    upload_lot_photos_to_auctionninja,
)
import database as db_module
from database import (
    DEFAULT_STARTING_LOT,
    DEFAULT_AUCTION_ID,
    ITEM_STATUS_READY,
    ITEM_STATUS_PUBLISHED,
    ITEM_STATUS_NEEDS_UPDATE,
    ITEM_STATUS_REMOVED,
    AUCTION_STATUS_PREPARING,
    AUCTION_STATUS_ACTIVE,
    AUCTION_STATUS_COMPLETED,
    AUCTION_STATUSES,
    EXPORTABLE_STATUSES,
    MANAGE_ITEM_FILTERS,
    DATA_DIR,
    EXPORTS_DIR,
    get_current_auction_id,
    get_current_auction,
    list_auctions,
    fetch_auction_summaries,
    create_next_auction,
    switch_current_auction,
    update_auction_status,
    move_item_to_auction,
    fetch_last_lot_from_store,
    append_item_record,
    mark_lots_as_published,
    fetch_export_rows,
    normalize_manage_filter,
    fetch_manage_items,
    fetch_manage_item_counts,
    fetch_dashboard_items,
    fetch_saved_item,
    update_saved_item_record,
    mark_item_removed,
    restore_removed_item,
    set_items_status,
    bulk_restore_items,
    fetch_export_rows_for_lots,
    lot_numbers_from_rows,
    get_last_lot,
    get_next_lot_preview,
    reserve_next_lot,
    get_next_auction_photo_index,
    reserve_next_auction_photo_index,
    get_ftp_upload_record,
    record_ftp_upload,
    delete_ftp_upload_record,
    record_export_batch,
    list_export_archives,
    fetch_export_batch,
    fetch_items_for_lot_numbers,
    item_record_from_form,
    combine_item_notes,
    update_auction_last_lot_override,
    current_auction_number_for_upload,
)

from utils import (
    UPLOADS_DIR,
    CSV_HEADER,
    DEFAULT_CATEGORIES,
    auth_enabled,
    auth_username,
    auth_password,
    is_authenticated,
    load_saved_files_for_temp_id,
    get_active_draft,
    set_active_draft,
    clear_active_draft,
    current_edit_context,
    blank_form,
    options_from_request,
    form_from_request,
    form_from_option,
    form_from_saved_item,
    parse_decimal_field,
    validate_save_form,
    build_csv_text,
    archive_export_csv,
    slugify_title,
    make_unique_dir,
    save_uploaded_files,
    save_uploaded_files_to_dir,
)

from routes.items import items_bp

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
EXPORTS_DIR = DATA_DIR / "exports"

DATA_DIR.mkdir(parents=True, exist_ok=True)
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

app.register_blueprint(items_bp)

generator = AuctionNinjaGenerator()

def render_edit_page(
    temp_id: str,
    saved_files: list[Path],
    seller_notes: str,
    options: list[dict],
    form: dict[str, str],
    revision_request: str = "",
):
    set_active_draft(
        temp_id=temp_id,
        seller_notes=seller_notes,
        options=options,
        form=form,
        revision_request=revision_request,
    )
    return render_template(
        "edit.html",
        temp_id=temp_id,
        image_files=[p.name for p in saved_files],
        image_url_prefix=f"/uploads/{temp_id}/",
        next_lot=get_next_lot_preview(),
        categories=DEFAULT_CATEGORIES,
        seller_notes=seller_notes,
        revision_request=revision_request,
        options=options,
        form=form,
    )


@app.before_request
def require_login_when_configured():
    if not auth_enabled():
        return None

    allowed_endpoints = {
        "healthz",
        "login",
        "logout",
        "static",
    }
    if request.endpoint in allowed_endpoints:
        return None

    if is_authenticated():
        return None

    return redirect(url_for("login", next=request.path))


@app.route("/healthz", methods=["GET"])
def healthz():
    return Response("ok\n", mimetype="text/plain")


@app.context_processor
def inject_auction_context() -> dict[str, Any]:
    return {
        "current_auction": get_current_auction(),
        "auction_list": list_auctions(),
        "auction_statuses": [
            AUCTION_STATUS_PREPARING,
            AUCTION_STATUS_ACTIVE,
            AUCTION_STATUS_COMPLETED,
        ],
        "auth_enabled": auth_enabled(),
        "is_authenticated": is_authenticated(),
    }


@app.route("/auctions/create_next", methods=["POST"])
def create_auction_route():
    auction = create_next_auction()
    flash(f"Created auction {auction['id']} and switched to it.")
    return redirect(request.form.get("return_to") or url_for("dashboard"))


@app.route("/auctions/switch", methods=["POST"])
def switch_auction_route():
    auction_id = request.form.get("auction_id", "").strip()
    if not auction_id.isdigit() or not switch_current_auction(int(auction_id)):
        flash("Choose a valid auction to switch to.")
        return redirect(request.form.get("return_to") or url_for("dashboard"))

    flash(f"Now working in auction {auction_id}.")
    return redirect(request.form.get("return_to") or url_for("dashboard"))


@app.route("/auctions/status", methods=["POST"])
def update_auction_status_route():
    auction_id = request.form.get("auction_id", "").strip()
    status = request.form.get("status", "").strip().lower()
    if not auction_id.isdigit() or not update_auction_status(int(auction_id), status):
        flash("Choose a valid auction and status.")
        return redirect(request.form.get("return_to") or url_for("dashboard"))

    flash(f"Auction {auction_id} is now marked {status}.")
    return redirect(request.form.get("return_to") or url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth_enabled():
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        next_url = request.form.get("next", "").strip()
        if username == auth_username() and password == auth_password():
            session["authenticated"] = True
            flash("Signed in.")
            return redirect(next_url or url_for("index"))
        flash("Login failed. Check the username and password.")

    next_url = request.values.get("next", "").strip()
    return render_template("login.html", next_url=next_url, username=auth_username())


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("authenticated", None)
    flash("Signed out.")
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
def index():
    active_draft = get_active_draft()
    return render_template(
        "index.html",
        next_lot=get_next_lot_preview(),
        active_draft=active_draft,
    )


@app.route("/dashboard", methods=["GET"])
def dashboard():
    return render_template(
        "dashboard.html",
        counts=fetch_manage_item_counts(),
        recent_exports=list_export_archives()[:5],
        needs_update_items=fetch_dashboard_items([ITEM_STATUS_NEEDS_UPDATE], limit=5),
        ready_items=fetch_dashboard_items([ITEM_STATUS_READY], limit=5),
    )


@app.route("/auctions", methods=["GET"])
def auctions_overview():
    return render_template(
        "auctions.html",
        auctions=fetch_auction_summaries(),
    )


@app.route("/export_csv", methods=["GET"])
def export_csv():
    rows = fetch_export_rows()
    if not rows:
        flash("There are no saved items to export yet.")
        return redirect(url_for("index"))

    filename = f"auction_{get_current_auction_id()}_items_export_{time.strftime('%Y%m%d')}.csv"
    csv_text = build_csv_text(rows)
    lot_numbers = lot_numbers_from_rows(rows)
    archive_path = archive_export_csv(filename, csv_text)
    record_export_batch(
        filename=filename,
        export_type="full",
        lot_numbers=lot_numbers,
        archive_path=archive_path,
    )
    mark_lots_as_published(
        lot_numbers=lot_numbers,
        export_batch_name=filename,
    )
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/exports", methods=["GET"])
def export_history():
    return render_template(
        "export_history.html",
        archives=list_export_archives(),
    )


@app.route("/admin", methods=["GET"])
def admin():
    return render_template("admin.html")


@app.route("/exports/<path:filename>/details", methods=["GET"])
def export_batch_details(filename: str):
    safe_name = Path(filename).name
    batch = fetch_export_batch(safe_name)
    if not batch:
        flash("That export batch was not found.")
        return redirect(url_for("export_history"))

    lot_numbers = [int(value) for value in batch.get("lot_numbers", "").split(",") if value.isdigit()]
    items = fetch_items_for_lot_numbers(lot_numbers)
    return render_template(
        "export_batch_details.html",
        batch=batch,
        items=items,
    )


@app.route("/exports/<path:filename>", methods=["GET"])
def download_export_archive(filename: str):
    safe_name = Path(filename).name
    export_dir = db_module.EXPORTS_DIR
    target = export_dir / safe_name
    if not target.exists() or not target.is_file():
        flash("That export file was not found.")
        return redirect(url_for("export_history"))
    return send_from_directory(export_dir, safe_name, as_attachment=True)


@app.route("/export_selected_csv", methods=["POST"])
def export_selected_csv():
    current_filter = normalize_manage_filter(request.form.get("current_filter", "active"))
    selected_lots = sorted(
        {
            int(value)
            for value in request.form.getlist("lot_numbers")
            if str(value).isdigit()
        }
    )

    if not selected_lots:
        flash("Select at least one lot to export.")
        return redirect(url_for("items.manage_items", status=current_filter))

    rows = fetch_export_rows_for_lots(selected_lots)
    if not rows:
        flash("The selected lots could not be exported.")
        return redirect(url_for("items.manage_items", status=current_filter))

    first_lot = selected_lots[0]
    last_lot = selected_lots[-1]
    filename = f"auction_{get_current_auction_id()}_batch_{first_lot}-{last_lot}_{time.strftime('%Y%m%d')}.csv"

    mark_lots_as_published(
        lot_numbers=selected_lots,
        export_batch_name=filename,
    )
    csv_text = build_csv_text(rows)
    archive_path = archive_export_csv(filename, csv_text)
    record_export_batch(
        filename=filename,
        export_type="selected",
        lot_numbers=selected_lots,
        archive_path=archive_path,
    )

    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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

    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
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

    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
        options=options,
        form=form,
    )


@app.route("/add_draft_photos", methods=["POST"])
def add_draft_photos():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()
    saved_files, options, form = current_edit_context(temp_id, seller_notes)

    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    uploaded_files = request.files.getlist("photos")
    added_files = save_uploaded_files_to_dir(uploaded_files, temp_dir)

    if added_files:
        flash(f"Added {len(added_files)} photo(s) to this draft.")
    else:
        flash("No new valid photos were added to this draft.")

    saved_files = load_saved_files_for_temp_id(temp_id)
    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
        options=options,
        form=form,
    )


@app.route("/remove_draft_photo", methods=["POST"])
def remove_draft_photo():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()
    filename = secure_filename(request.form.get("filename", "").strip())
    saved_files, options, form = current_edit_context(temp_id, seller_notes)

    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    target = temp_dir / filename
    if not filename or not target.exists() or not target.is_file():
        flash("Could not find that photo in this draft.")
    else:
        target.unlink()
        flash(f"Removed photo: {filename}")

    saved_files = load_saved_files_for_temp_id(temp_id)
    if not saved_files:
        flash("This draft has no photos left. Please add at least one photo before revising or saving.")

    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
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
        "material_notes": form["Material Notes"],
        "mark_notes": form["Mark Notes"],
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
        form["Material Notes"] = str(revised.get("material_notes", form["Material Notes"])).strip()
        form["Mark Notes"] = str(revised.get("mark_notes", form["Mark Notes"])).strip()
        form["Title"] = str(revised.get("title", form["Title"])).strip()
        form["Description"] = str(revised.get("description", form["Description"])).strip()
        form["Condition Summary"] = str(revised.get("condition_summary", form["Condition Summary"])).strip()
        form["Keywords"] = str(revised.get("keywords", form["Keywords"])).strip()
        form["Category"] = str(revised.get("category", form["Category"])).strip() or "Other"
    except Exception as exc:
        app.logger.exception("AI revision failed")
        flash(f"AI revision failed: {exc}")

    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
        options=options,
        form=form,
    )


@app.route("/save", methods=["POST"])
def save():
    temp_id = request.form.get("temp_id", "").strip()
    temp_dir = UPLOADS_DIR / temp_id
    seller_notes = request.form.get("seller_notes", "").strip()

    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    saved_files = load_saved_files_for_temp_id(temp_id)
    form = form_from_request(seller_notes=seller_notes)
    options = options_from_request()
    title = form["Title"]

    validation_errors = validate_save_form(form)
    if validation_errors:
        for error in validation_errors:
            flash(error)
        return render_edit_page(
            temp_id=temp_id,
            saved_files=saved_files,
            seller_notes=seller_notes,
            options=options,
            form=form,
        )

    csv_lot_number = reserve_next_lot()

    safe_title = slugify_title(title)
    folder_name = f"{csv_lot_number}_{safe_title}"
    final_dir = make_unique_dir(UPLOADS_DIR, folder_name)

    try:
        temp_dir.rename(final_dir)
    except Exception as exc:
        app.logger.exception("Failed to rename image folder")
        flash(f"Warning: saved listing but could not rename image folder: {exc}")
        final_dir = temp_dir

    record = item_record_from_form(
        lot_number=csv_lot_number,
        form=form,
        image_folder=final_dir.name,
    )
    append_item_record(record)
    clear_active_draft(temp_id=temp_id)

    auction_number = current_auction_number_for_upload()
    uploaded_names = []
    auction_photo_index = 0

    if auction_number:
        try:
            auction_photo_index = reserve_next_auction_photo_index(auction_number)
            local_jpgs = sorted([p for p in final_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"])
            uploaded_names = upload_lot_photos_to_auctionninja(
                local_files=local_jpgs,
                auction_number=auction_number,
                lot_number=csv_lot_number,
            )
        except Exception as exc:
            app.logger.exception("FTP upload failed")
            flash(f"Lot saved locally, but FTP upload failed: {exc}")

    if uploaded_names:
        record_ftp_upload(
            lot_number=csv_lot_number,
            auction_number=auction_number,
            auction_photo_index=auction_photo_index,
            remote_names=uploaded_names,
        )
        flash(
            f"Saved lot {csv_lot_number}. Uploaded to auction {auction_number} as: "
            + ", ".join(uploaded_names)
        )
    else:
        flash(
            f"Saved lot {csv_lot_number} to the database. "
            f"Images stored in: {final_dir.name}"
        )

    return redirect(url_for("index"))


@app.route("/uploads/<temp_id>/<filename>")
def uploaded_file(temp_id: str, filename: str):
    return send_from_directory(UPLOADS_DIR / temp_id, filename)


@app.route("/set_next_lot", methods=["POST"])
def set_next_lot():
    next_lot_str = request.form.get("next_lot", "").strip()
    if not next_lot_str.isdigit():
        flash("Next lot must be a valid number.")
        return redirect(request.referrer or url_for("index"))

    next_lot = int(next_lot_str)
    last_lot = next_lot - 1

    update_auction_last_lot_override(get_current_auction_id(), last_lot)

    flash(f"Next lot number successfully set to {next_lot}.")
    return redirect(request.referrer or url_for("index"))


@app.route("/reset", methods=["POST"])
def reset():
    if UPLOADS_DIR.exists():
        shutil.rmtree(UPLOADS_DIR)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    clear_active_draft()
    flash("Temporary uploads cleared.")
    return redirect(url_for("index"))


@app.route("/resume_draft", methods=["GET"])
def resume_draft():
    active_draft = get_active_draft()
    if not active_draft:
        flash("No resumable draft was found.")
        return redirect(url_for("index"))

    temp_id = str(active_draft["temp_id"])
    saved_files = load_saved_files_for_temp_id(temp_id)
    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=str(active_draft["seller_notes"]),
        options=active_draft["options"],
        form=active_draft["form"],
        revision_request=str(active_draft["revision_request"]),
    )


@app.route("/discard_draft", methods=["POST"])
def discard_draft():
    active_draft = get_active_draft()
    if not active_draft:
        flash("No resumable draft was found.")
        return redirect(url_for("index"))

    temp_id = str(active_draft["temp_id"])
    temp_dir = UPLOADS_DIR / temp_id
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    clear_active_draft(temp_id=temp_id)
    flash("Discarded the last unsaved draft.")
    return redirect(url_for("index"))


@app.route("/delete_remote_upload", methods=["POST"])
def delete_remote_upload():
    lot_number = request.form.get("lot_number", "").strip()

    if not lot_number.isdigit():
        flash("Enter a valid lot number to delete FTP photos.")
        return redirect(url_for("admin"))

    record = get_ftp_upload_record(lot_number)
    if not record:
        flash(f"No saved FTP upload record was found for lot {lot_number}.")
        return redirect(url_for("admin"))

    auction_number = str(record.get("auction_number", "")).strip()
    remote_names = record.get("remote_names", [])

    if not auction_number or not isinstance(remote_names, list):
        flash(f"FTP upload record for lot {lot_number} is incomplete.")
        return redirect(url_for("admin"))

    try:
        deleted_names, missing_names = delete_lot_photos_from_auctionninja(
            auction_number=auction_number,
            remote_names=[str(name) for name in remote_names],
        )
        delete_ftp_upload_record(lot_number)
    except Exception as exc:
        app.logger.exception("FTP delete failed for lot %s", lot_number)
        flash(f"FTP delete failed for lot {lot_number}: {exc}")
        return redirect(url_for("admin"))

    if deleted_names and missing_names:
        flash(
            f"Deleted FTP photos for lot {lot_number}: {', '.join(deleted_names)}. "
            f"Already missing: {', '.join(missing_names)}."
        )
    elif deleted_names:
        flash(f"Deleted FTP photos for lot {lot_number}: {', '.join(deleted_names)}.")
    elif missing_names:
        flash(
            f"FTP photos for lot {lot_number} were already missing remotely: "
            + ", ".join(missing_names)
        )
    else:
        flash(f"No FTP photos were recorded for lot {lot_number}.")

    return redirect(url_for("admin"))


@app.route("/upload_remote_ftp", methods=["POST"])
def upload_remote_ftp():
    lot_number_str = request.form.get("lot_number", "").strip()
    if not lot_number_str.isdigit():
        flash("Enter a valid lot number to upload FTP photos.")
        return redirect(url_for("admin"))

    lot_number = int(lot_number_str)
    image_folder = None
    auction_number = current_auction_number_for_upload()

    item = fetch_saved_item(lot_number)
    if item:
        image_folder = item.get("image_folder")
        auction_number = str(item.get("auction_id", auction_number))

    if not image_folder:
        flash(f"No image folder found for lot {lot_number}.")
        return redirect(url_for("admin"))

    final_dir = UPLOADS_DIR / image_folder
    if not final_dir.exists():
        flash(f"Image folder {final_dir.name} does not exist.")
        return redirect(url_for("admin"))

    local_jpgs = sorted([p for p in final_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"])
    if not local_jpgs:
        flash(f"No JPG photos found in {final_dir.name}.")
        return redirect(url_for("admin"))

    if not auction_number:
        flash("No auction number configured or associated with this lot.")
        return redirect(url_for("admin"))

    try:
        auction_photo_index = reserve_next_auction_photo_index(auction_number)
        uploaded_names = upload_lot_photos_to_auctionninja(
            local_files=local_jpgs,
            auction_number=auction_number,
            lot_number=lot_number,
        )
        if uploaded_names:
            record_ftp_upload(lot_number, auction_number, auction_photo_index, uploaded_names)
            flash(f"Successfully uploaded {len(uploaded_names)} photos for lot {lot_number} to FTP.")
        else:
            flash(f"Failed to upload photos for lot {lot_number}.")
    except Exception as exc:
        app.logger.exception("FTP upload failed for lot %s", lot_number)
        flash(f"FTP upload failed for lot {lot_number}: {exc}")

    return redirect(url_for("admin"))


@app.route("/ftp_preview", methods=["GET"])
def ftp_preview():
    auction_number = current_auction_number_for_upload()
    if not auction_number:
        flash("You must set an active AUCTION_NUMBER to preview photos.")
        return redirect(url_for("admin"))

    missing_lots = []

    if UPLOADS_DIR.exists():
        for final_dir in sorted(UPLOADS_DIR.iterdir(), key=lambda d: d.name):
            if not final_dir.is_dir():
                continue

            parts = final_dir.name.split('_', 1)
            if not parts[0].isdigit():
                continue

            lot_number = int(parts[0])
            if get_ftp_upload_record(lot_number):
                continue

            local_jpgs = sorted([p for p in final_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"])
            if not local_jpgs:
                continue
            
            current_lot_auction = auction_number
            item_title = ""
            item_description = ""
            item = fetch_saved_item(lot_number)
            if item:
                if item.get("auction_id"):
                    current_lot_auction = str(item["auction_id"])
                item_title = item.get("title", "")
                item_description = item.get("description", "")

            files_info = []
            for i, p in enumerate(local_jpgs, start=1):
                files_info.append({
                    "original_name": p.name,
                    "remote_name": f"{lot_number}_{i}.jpg"
                })

            missing_lots.append({
                "lot_number": lot_number,
                "auction_number": current_lot_auction,
                "title": item_title,
                "description": item_description,
                "folder": final_dir.name,
                "files": files_info
            })

    return render_template("ftp_preview.html", missing_lots=missing_lots)


@app.route("/upload_selected_ftp", methods=["POST"])
def upload_selected_ftp():
    auction_number = current_auction_number_for_upload()
    if not auction_number:
        flash("You must set an active AUCTION_NUMBER to upload photos.")
        return redirect(url_for("admin"))

    lots_to_upload = {}
    for key, value in request.form.items():
        if key.startswith("lot_") and "_file_" in key:
            parts = key.split("_", 3)
            lot_number = int(parts[1])
            original_name = parts[3]

            if lot_number not in lots_to_upload:
                lots_to_upload[lot_number] = {
                    "auction_number": request.form.get(f"lot_{lot_number}_auction"),
                    "folder": request.form.get(f"lot_{lot_number}_folder"),
                    "files": []
                }
            
            remote_name = request.form.get(f"lot_{lot_number}_name_{original_name}", original_name).strip()
            if not remote_name:
                remote_name = original_name

            lots_to_upload[lot_number]["files"].append((original_name, remote_name))

    uploaded_count = 0
    
    for lot_number, data in lots_to_upload.items():
        folder = data["folder"]
        current_lot_auction = data["auction_number"]
        if not folder or not current_lot_auction:
            continue
            
        final_dir = UPLOADS_DIR / folder
        if not final_dir.exists():
            continue

        files_to_upload = []
        for orig_name, remote_name in data["files"]:
            local_path = final_dir / orig_name
            if local_path.exists():
                files_to_upload.append((local_path, remote_name))

        if not files_to_upload:
            continue

        try:
            auction_photo_index = reserve_next_auction_photo_index(current_lot_auction)
            uploaded_names = upload_lot_photos_to_auctionninja(files_to_upload, current_lot_auction, lot_number)
            if uploaded_names:
                record_ftp_upload(lot_number, current_lot_auction, auction_photo_index, uploaded_names)
                uploaded_count += 1
        except Exception as exc:
            app.logger.exception("FTP upload failed for lot %s", lot_number)
            flash(f"FTP upload failed for lot {lot_number}: {exc}")

    if uploaded_count > 0:
        flash(f"Successfully uploaded photos to FTP for {uploaded_count} lot(s).")
    else:
        flash("No new lot photos were uploaded.")

    return redirect(url_for("admin"))


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    app.run(host=host, port=port, debug=debug)
