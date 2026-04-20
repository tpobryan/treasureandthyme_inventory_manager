from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app

from database import (
    ITEM_STATUS_READY,
    ITEM_STATUS_NEEDS_UPDATE,
    ITEM_STATUS_REMOVED,
    fetch_manage_items,
    fetch_manage_item_counts,
    normalize_manage_filter,
    fetch_saved_item,
    update_saved_item_record,
    mark_item_removed,
    move_item_to_auction,
    restore_removed_item,
    bulk_restore_items,
    set_items_status,
    current_auction_number_for_upload,
    reserve_next_auction_photo_index,
    record_ftp_upload,
)
from utils import (
    UPLOADS_DIR,
    DEFAULT_CATEGORIES,
    validate_save_form,
    form_from_saved_item,
    load_saved_files_for_temp_id,
)
from ftp_client import upload_lot_photos_to_auctionninja

items_bp = Blueprint("items", __name__)

@items_bp.route("/manage_items", methods=["GET"])
def manage_items():
    current_filter = normalize_manage_filter(request.args.get("status", "active"))
    items = fetch_manage_items(current_filter)
    filter_counts = fetch_manage_item_counts()
    return render_template(
        "manage_items.html",
        items=items,
        current_filter=current_filter,
        filter_counts=filter_counts,
    )

@items_bp.route("/items/<int:lot_number>/edit", methods=["GET"])
def edit_saved_item(lot_number: int):
    item = fetch_saved_item(lot_number)
    if not item or item.get("status") == ITEM_STATUS_REMOVED:
        flash(f"Lot {lot_number} was not found.")
        return redirect(url_for("items.manage_items", status=normalize_manage_filter(request.args.get("status", "active"))))

    image_folder = item.get("image_folder", "")
    saved_files = load_saved_files_for_temp_id(image_folder)
    return render_template(
        "saved_item_edit.html",
        item=item,
        form=form_from_saved_item(item),
        categories=DEFAULT_CATEGORIES,
        image_files=[p.name for p in saved_files],
        image_url_prefix=f"/uploads/{image_folder}/" if image_folder else "",
        current_filter=normalize_manage_filter(request.args.get("status", "active")),
    )

@items_bp.route("/items/<int:lot_number>/update", methods=["POST"])
def update_saved_item(lot_number: int):
    current_filter = normalize_manage_filter(request.form.get("current_filter", "active"))
    item = fetch_saved_item(lot_number)
    if not item or item.get("status") == ITEM_STATUS_REMOVED:
        flash(f"Lot {lot_number} was not found.")
        return redirect(url_for("items.manage_items", status=current_filter))

    form = {
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
        "Item Notes": request.form.get("Item Notes", "").strip(),
        "Consigner #": request.form.get("Consigner #", "").strip(),
        "Shipping Available": request.form.get("Shipping Available", "").strip() or "No",
    }

    validation_errors = validate_save_form(form)
    if validation_errors:
        for error in validation_errors:
            flash(error)
        image_folder = item.get("image_folder", "")
        saved_files = load_saved_files_for_temp_id(image_folder)
        return render_template(
            "saved_item_edit.html",
            item=item,
            form=form,
            categories=DEFAULT_CATEGORIES,
            image_files=[p.name for p in saved_files],
            image_url_prefix=f"/uploads/{image_folder}/" if image_folder else "",
            current_filter=current_filter,
        )

    new_status = update_saved_item_record(lot_number, form)
    if new_status == ITEM_STATUS_NEEDS_UPDATE:
        flash(f"Updated lot {lot_number}. Status changed to needs_update so it can be re-exported.")
    else:
        flash(f"Updated lot {lot_number}.")
    return redirect(url_for("items.manage_items", status=current_filter))

@items_bp.route("/items/<int:lot_number>/remove", methods=["POST"])
def remove_saved_item(lot_number: int):
    current_filter = normalize_manage_filter(request.form.get("current_filter", "active"))
    item = fetch_saved_item(lot_number)
    if not item or item.get("status") == ITEM_STATUS_REMOVED:
        flash(f"Lot {lot_number} was not found.")
        return redirect(url_for("items.manage_items", status=current_filter))

    if mark_item_removed(lot_number):
        flash(f"Removed lot {lot_number} from future exports.")
    else:
        flash(f"Lot {lot_number} could not be removed.")
    return redirect(url_for("items.manage_items", status=current_filter))

@items_bp.route("/items/<int:lot_number>/move", methods=["POST"])
def move_saved_item(lot_number: int):
    current_filter = normalize_manage_filter(request.form.get("current_filter", "active"))
    target_auction_id = request.form.get("auction_id", "").strip()
    if not target_auction_id.isdigit():
        flash("Choose a valid auction to move this lot.")
        return redirect(url_for("items.edit_saved_item", lot_number=lot_number, status=current_filter))

    if move_item_to_auction(lot_number, int(target_auction_id)):
        flash(
            f"Moved lot {lot_number} to auction {target_auction_id}. "
            "Its publish state was reset so it can be reviewed and exported there."
        )
    else:
        flash(f"Lot {lot_number} could not be moved.")
    return redirect(url_for("items.manage_items", status=current_filter))

@items_bp.route("/items/<int:lot_number>/restore", methods=["POST"])
def restore_saved_item(lot_number: int):
    current_filter = normalize_manage_filter(request.form.get("current_filter", "removed"))
    restored_status = restore_removed_item(lot_number)
    if restored_status == ITEM_STATUS_NEEDS_UPDATE:
        flash(f"Restored lot {lot_number}. It is marked needs_update because it had already been published before removal.")
    elif restored_status == ITEM_STATUS_READY:
        flash(f"Restored lot {lot_number} to ready.")
    else:
        flash(f"Lot {lot_number} could not be restored.")
    return redirect(url_for("items.manage_items", status=current_filter))

@items_bp.route("/items/bulk_action", methods=["POST"])
def bulk_update_items():
    current_filter = normalize_manage_filter(request.form.get("current_filter", "active"))
    selected_lots = sorted({int(v) for v in request.form.getlist("lot_numbers") if str(v).isdigit()})
    action = request.form.get("bulk_action", "").strip().lower()

    if not selected_lots:
        flash("Select at least one lot for a bulk action.")
        return redirect(url_for("items.manage_items", status=current_filter))

    if action == "remove":
        changed = sum(1 for lot in selected_lots if mark_item_removed(lot))
        flash(f"Removed {changed} selected lot(s) from future exports.")
    elif action == "restore":
        restored = bulk_restore_items(selected_lots)
        flash(f"Restored {restored} selected lot(s).")
    elif action == "mark_ready":
        changed = set_items_status(selected_lots, ITEM_STATUS_READY)
        flash(f"Marked {changed} selected lot(s) as ready.")
    elif action == "move":
        target_auction_id = request.form.get("target_auction_id", "").strip()
        if not target_auction_id.isdigit():
            flash("Choose a destination auction for the move action.")
            return redirect(url_for("items.manage_items", status=current_filter))
        moved = sum(1 for lot in selected_lots if move_item_to_auction(lot, int(target_auction_id)))
        flash(
            f"Moved {moved} selected lot(s) to auction {target_auction_id}. "
            "Their publish state was reset for review in the new auction."
        )
    elif action == "upload_ftp":
        flash("Bulk FTP feature runs here.")
    else:
        flash("Choose a valid bulk action.")
        
    return redirect(url_for("items.manage_items", status=current_filter))