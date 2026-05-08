import re
import shutil
import uuid
import base64
import tempfile
from pathlib import Path
from flask import Blueprint, render_template, request, flash, redirect, url_for, send_from_directory, current_app, session
from werkzeug.utils import secure_filename

from image_processor import apply_auto_enhance

from inventory_manager_generator import InventoryManagerGenerator
from ftp_client import upload_lot_photos_to_inventory_manager
from database import (
    ITEM_STATUS_READY,
    ITEM_STATUS_NEEDS_UPDATE,
    get_current_auction_id,
    fetch_manage_item_counts,
    fetch_dashboard_items,
    get_next_lot_preview,
    reserve_next_lot,
    reserve_next_auction_photo_index,
    record_ftp_upload,
    list_export_archives,
    item_record_from_form,
    update_auction_last_lot_override,
    current_auction_number_for_upload,
    append_item_record,
    fetch_active_draft,
    initialize_platform_status,
    fetch_recent_retail_items,
)
from utils import (
    UPLOADS_DIR,
    DEFAULT_CATEGORIES,
    get_draft_owner_token,
    load_saved_files_for_temp_id,
    get_active_draft,
    get_orphaned_drafts,
    set_active_draft,
    clear_active_draft,
    current_edit_context,
    options_from_request,
    form_from_request,
    form_from_option,
    validate_save_form,
    slugify_title,
    make_unique_dir,
    save_uploaded_files,
    save_uploaded_files_to_dir,
)
from database import fetch_active_draft as db_fetch_active_draft

main_bp = Blueprint("main", __name__)
generator = InventoryManagerGenerator()

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

@main_bp.route("/", methods=["GET"])
def index():
    active_draft = get_active_draft()
    return render_template(
        "index.html",
        next_lot=get_next_lot_preview(),
        active_draft=active_draft,
        orphaned_drafts=get_orphaned_drafts(),
    )

@main_bp.route("/dashboard", methods=["GET"])
def dashboard():
    return render_template(
        "dashboard.html",
        counts=fetch_manage_item_counts(),
        recent_exports=list_export_archives()[:5],
        needs_update_items=fetch_dashboard_items([ITEM_STATUS_NEEDS_UPDATE], limit=5),
        ready_items=fetch_dashboard_items([ITEM_STATUS_READY], limit=5),
        retail_items=fetch_recent_retail_items(limit=10),
    )

@main_bp.route("/analyze", methods=["POST"])
def analyze():
    current_app.logger.info("Entered /analyze")

    uploaded_files = request.files.getlist("photos")
    seller_notes = request.form.get("seller_notes", "").strip()
    strategy = request.form.get("strategy", "auction")

    temp_id, saved_files = save_uploaded_files(uploaded_files)

    if not saved_files:
        flash("Please choose at least one valid image.")
        return redirect(url_for("main.index"))

    try:
        ai_data = generator.generate_options(saved_files, seller_notes=seller_notes, strategy=strategy)
        options = ai_data.get("options", [])
        if not options:
            raise ValueError("No listing options were returned.")
        selected = options[0]
        current_app.logger.info("AI Selected Option Strategy=%s: %s", strategy, json.dumps(selected, indent=2))
        form = form_from_option(selected, seller_notes=seller_notes, strategy=strategy)
        current_app.logger.info("Generated %s options", len(options))
    except Exception as exc:
        current_app.logger.exception("AI analysis failed")
        flash(f"AI analysis failed: {exc}")
        return redirect(url_for("main.index"))

    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
        options=options,
        form=form,
    )

@main_bp.route("/choose_option", methods=["POST"])
def choose_option():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()

    saved_files = load_saved_files_for_temp_id(temp_id)
    if not saved_files:
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("main.index"))

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

@main_bp.route("/add_draft_photos", methods=["POST"])
def add_draft_photos():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()
    saved_files, options, form = current_edit_context(temp_id, seller_notes)

    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("main.index"))

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

@main_bp.route("/remove_draft_photo", methods=["POST"])
def remove_draft_photo():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()
    filename = secure_filename(request.form.get("filename", "").strip())
    saved_files, options, form = current_edit_context(temp_id, seller_notes)

    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("main.index"))

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

@main_bp.route("/reorder_draft_photos", methods=["POST"])
def reorder_draft_photos():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()
    ordered_files = request.form.getlist("ordered_files")
    saved_files, options, form = current_edit_context(temp_id, seller_notes)

    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("main.index"))

    valid_files = []
    for filename in ordered_files:
        safe_name = secure_filename(filename)
        target = temp_dir / safe_name
        if target.exists() and target.is_file():
            valid_files.append(target)

    if valid_files:
        temp_mappings = []
        for target in valid_files:
            temp_path = target.with_name(f".tmp_{uuid.uuid4().hex}_{target.name}")
            target.rename(temp_path)
            temp_mappings.append((temp_path, target.name))

        for index, (temp_path, original_name) in enumerate(temp_mappings, start=1):
            clean_name = re.sub(r"^\d{2,3}_", "", original_name)
            new_name = f"{index:02d}_{clean_name}"
            final_path = temp_dir / new_name

            counter = 1
            while final_path.exists():
                final_path = temp_dir / f"{index:02d}_{counter}_{clean_name}"
                counter += 1

            temp_path.rename(final_path)

        flash("Photos reordered successfully.")

    saved_files = load_saved_files_for_temp_id(temp_id)
    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
        options=options,
        form=form,
    )

@main_bp.route("/revise", methods=["POST"])
def revise():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()
    revision_request = request.form.get("revision_request", "").strip()

    saved_files = load_saved_files_for_temp_id(temp_id)
    if not saved_files:
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("main.index"))

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
        current_app.logger.exception("AI revision failed")
        flash(f"AI revision failed: {exc}")

    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
        options=options,
        form=form,
    )

@main_bp.route("/save", methods=["POST"])
def save():
    temp_id = request.form.get("temp_id", "").strip()
    temp_dir = UPLOADS_DIR / temp_id
    seller_notes = request.form.get("seller_notes", "").strip()

    if not temp_id or not temp_dir.exists():
        # If the folder is missing, it might be a double-submit. 
        # Only flash an error if the draft is still active in the database.
        draft = get_active_draft()
        if draft and draft.get("temp_id") == temp_id:
            flash("Could not find uploaded images for this draft.")
        return redirect(url_for("main.index"))

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
        current_app.logger.exception("Failed to rename image folder")
        flash(f"Warning: saved listing but could not rename image folder: {exc}")
        final_dir = temp_dir

    record = item_record_from_form(
        lot_number=csv_lot_number,
        form=form,
        image_folder=final_dir.name,
    )
    append_item_record(record)
    
    platforms_to_publish = []
    if form.get("Listing Strategy") == "retail":
        if form.get("Publish to eBay") == "yes":
            platforms_to_publish.append("ebay")
        if form.get("Publish to Etsy") == "yes":
            platforms_to_publish.append("etsy")
            
    if platforms_to_publish:
        initialize_platform_status(csv_lot_number, platforms_to_publish)
        
    clear_active_draft(temp_id=temp_id)

    auction_number = current_auction_number_for_upload()
    uploaded_names = []
    auction_photo_index = 0

    if auction_number:
        try:
            auction_photo_index = reserve_next_auction_photo_index(auction_number)
            local_jpgs = sorted([p for p in final_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"])
            uploaded_names = upload_lot_photos_to_inventory_manager(
                local_files=local_jpgs,
                auction_number=auction_number,
                lot_number=csv_lot_number,
            )
        except Exception as exc:
            current_app.logger.exception("FTP upload failed")
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

    return redirect(url_for("main.index"))

@main_bp.route("/uploads/<temp_id>/<filename>")
def uploaded_file(temp_id: str, filename: str):
    return send_from_directory(UPLOADS_DIR / temp_id, filename)

@main_bp.route("/set_next_lot", methods=["POST"])
def set_next_lot():
    next_lot_str = request.form.get("next_lot", "").strip()
    if not next_lot_str.isdigit():
        flash("Next lot must be a valid number.")
        return redirect(request.referrer or url_for("main.index"))

    next_lot = int(next_lot_str)
    last_lot = next_lot - 1

    update_auction_last_lot_override(get_current_auction_id(), last_lot)

    flash(f"Next lot number successfully set to {next_lot}.")
    return redirect(request.referrer or url_for("main.index"))

@main_bp.route("/reset", methods=["POST"])
def reset():
    draft = get_active_draft()
    if draft:
        temp_id = draft["temp_id"]
        temp_dir = UPLOADS_DIR / temp_id
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        clear_active_draft(temp_id)
    else:
        clear_active_draft()
    flash("Temporary uploads cleared.")
    return redirect(url_for("main.index"))

@main_bp.route("/resume_draft", methods=["GET"])
def resume_draft():
    active_draft = get_active_draft()
    if not active_draft:
        flash("No resumable draft was found.")
        return redirect(url_for("main.index"))

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

@main_bp.route("/discard_draft", methods=["POST"])
def discard_draft():
    active_draft = get_active_draft()
    if not active_draft:
        flash("No resumable draft was found.")
        return redirect(url_for("main.index"))

    temp_id = str(active_draft["temp_id"])
    temp_dir = UPLOADS_DIR / temp_id
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    clear_active_draft(temp_id=temp_id)
    flash("Discarded the last unsaved draft.")
    return redirect(url_for("main.index"))

@main_bp.route("/recover_draft/<temp_id>", methods=["POST"])
def recover_draft(temp_id: str):
    draft = db_fetch_active_draft(temp_id, owner_token=get_draft_owner_token())
    if not draft:
        flash("That draft is not available in this browser session.")
        return redirect(url_for("main.index"))

    session["active_temp_id"] = temp_id
    flash("Draft recovered successfully!")
    return redirect(url_for("main.resume_draft"))

@main_bp.route("/api/edit_draft_photo", methods=["POST"])
def edit_draft_photo():
    temp_id = request.form.get("temp_id", "").strip()
    filename = secure_filename(request.form.get("filename", "").strip())
    auto_enhance = request.form.get("auto_enhance") == "true"
    image_data = request.form.get("image_data", "").strip()
    
    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        return {"success": False, "error": "Draft not found"}, 404

    target = temp_dir / filename
    if not filename or not target.exists() or not target.is_file():
        return {"success": False, "error": "File not found"}, 404

    if not image_data.startswith("data:image/"):
        return {"success": False, "error": "Invalid image data"}, 400

    try:
        header, encoded = image_data.split(",", 1)
        data = base64.b64decode(encoded)
        target.write_bytes(data)
        
        if auto_enhance:
            apply_auto_enhance(target, target)
            
        return {"success": True}
    except Exception as exc:
        current_app.logger.exception("Failed to edit photo")
        return {"success": False, "error": str(exc)}, 500

@main_bp.route("/api/auto_enhance_preview", methods=["POST"])
def auto_enhance_preview():
    image_data = request.form.get("image_data", "").strip()
    
    if not image_data.startswith("data:image/"):
        return {"success": False, "error": "Invalid image data"}, 400

    try:
        header, encoded = image_data.split(",", 1)
        data = base64.b64decode(encoded)
        
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tf.write(data)
            temp_path = Path(tf.name)
            
        try:
            apply_auto_enhance(temp_path, temp_path)
            enhanced_bytes = temp_path.read_bytes()
            enhanced_b64 = base64.b64encode(enhanced_bytes).decode("utf-8")
            return {"success": True, "image_data": f"data:image/jpeg;base64,{enhanced_b64}"}
        finally:
            if temp_path.exists():
                temp_path.unlink()
                
    except Exception as exc:
        current_app.logger.exception("Failed to auto enhance preview")
        return {"success": False, "error": str(exc)}, 500
