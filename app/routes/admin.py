from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app

from ..integrations.ftp_client import (
    delete_lot_photos_from_inventory_manager,
    upload_lot_photos_to_inventory_manager,
)
from ..database import (
    fetch_saved_item,
    reserve_next_auction_photo_index,
    get_ftp_upload_record,
    record_ftp_upload,
    delete_ftp_upload_record,
    current_auction_number_for_upload,
    connect_item_store,
    ensure_item_store_ready
)
from ..utils import UPLOADS_DIR

admin_bp = Blueprint("admin", __name__)

@admin_bp.route("/admin", methods=["GET"])
def admin():
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    etsy_connected = False
    ebay_connected = False
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT platform_id FROM integrations WHERE platform_id IN ('etsy', 'ebay')")
            connected_platforms = {row["platform_id"] for row in cursor.fetchall()}
            etsy_connected = 'etsy' in connected_platforms
            ebay_connected = 'ebay' in connected_platforms
        finally:
            connection.close()
            
    return render_template("admin.html", etsy_connected=etsy_connected, ebay_connected=ebay_connected)

@admin_bp.route("/delete_remote_upload", methods=["POST"])
def delete_remote_upload():
    lot_number = request.form.get("lot_number", "").strip()

    if not lot_number.isdigit():
        flash("Enter a valid lot number to delete FTP photos.")
        return redirect(url_for("admin.admin"))

    record = get_ftp_upload_record(lot_number)
    if not record:
        flash(f"No saved FTP upload record was found for lot {lot_number}.")
        return redirect(url_for("admin.admin"))

    auction_number = str(record.get("auction_number", "")).strip()
    remote_names = record.get("remote_names", [])

    if not auction_number or not isinstance(remote_names, list):
        flash(f"FTP upload record for lot {lot_number} is incomplete.")
        return redirect(url_for("admin.admin"))

    try:
        deleted_names, missing_names = delete_lot_photos_from_inventory_manager(
            auction_number=auction_number,
            remote_names=[str(name) for name in remote_names],
        )
        delete_ftp_upload_record(lot_number)
    except Exception as exc:
        current_app.logger.exception("FTP delete failed for lot %s", lot_number)
        flash(f"FTP delete failed for lot {lot_number}: {exc}")
        return redirect(url_for("admin.admin"))

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

    return redirect(url_for("admin.admin"))

@admin_bp.route("/upload_remote_ftp", methods=["POST"])
def upload_remote_ftp():
    lot_number_str = request.form.get("lot_number", "").strip()
    if not lot_number_str.isdigit():
        flash("Enter a valid lot number to upload FTP photos.")
        return redirect(url_for("admin.admin"))

    lot_number = int(lot_number_str)
    image_folder = None
    auction_number = current_auction_number_for_upload()

    item = fetch_saved_item(lot_number)
    if item:
        image_folder = item.get("image_folder")
        auction_number = str(item.get("auction_id", auction_number))

    if not image_folder:
        flash(f"No image folder found for lot {lot_number}.")
        return redirect(url_for("admin.admin"))

    final_dir = UPLOADS_DIR / image_folder
    if not final_dir.exists():
        flash(f"Image folder {final_dir.name} does not exist.")
        return redirect(url_for("admin.admin"))

    local_jpgs = sorted([p for p in final_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"])
    if not local_jpgs:
        flash(f"No JPG photos found in {final_dir.name}.")
        return redirect(url_for("admin.admin"))

    if not auction_number:
        flash("No auction number configured or associated with this lot.")
        return redirect(url_for("admin.admin"))

    try:
        auction_photo_index = reserve_next_auction_photo_index(auction_number)
        uploaded_names = upload_lot_photos_to_inventory_manager(
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
        current_app.logger.exception("FTP upload failed for lot %s", lot_number)
        flash(f"FTP upload failed for lot {lot_number}: {exc}")

    return redirect(url_for("admin.admin"))

@admin_bp.route("/ftp_preview", methods=["GET"])
def ftp_preview():
    auction_number = current_auction_number_for_upload()
    if not auction_number:
        flash("You must set an active AUCTION_NUMBER to preview photos.")
        return redirect(url_for("admin.admin"))

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

@admin_bp.route("/upload_selected_ftp", methods=["POST"])
def upload_selected_ftp():
    auction_number = current_auction_number_for_upload()
    if not auction_number:
        flash("You must set an active AUCTION_NUMBER to upload photos.")
        return redirect(url_for("admin.admin"))

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
            uploaded_names = upload_lot_photos_to_inventory_manager(files_to_upload, current_lot_auction, lot_number)
            if uploaded_names:
                record_ftp_upload(lot_number, current_lot_auction, auction_photo_index, uploaded_names)
                uploaded_count += 1
        except Exception as exc:
            current_app.logger.exception("FTP upload failed for lot %s", lot_number)
            flash(f"FTP upload failed for lot {lot_number}: {exc}")

    if uploaded_count > 0:
        flash(f"Successfully uploaded photos to FTP for {uploaded_count} lot(s).")
    else:
        flash("No new lot photos were uploaded.")

    return redirect(url_for("admin.admin"))