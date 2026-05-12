import time
from pathlib import Path
from flask import Blueprint, Response, flash, redirect, render_template, request, send_from_directory, url_for

from ..database import (
    EXPORTS_DIR,
    get_current_auction_id,
    fetch_export_rows,
    lot_numbers_from_rows,
    mark_lots_as_published,
    record_export_batch,
    list_export_archives,
    fetch_export_batch,
    fetch_items_for_lot_numbers,
    normalize_manage_filter,
    fetch_export_rows_for_lots,
)
from ..utils import (
    build_csv_text,
    archive_export_csv,
)

exports_bp = Blueprint("exports", __name__)

@exports_bp.route("/export_csv", methods=["GET"])
def export_csv():
    rows = fetch_export_rows()
    if not rows:
        flash("There are no saved items to export yet.")
        return redirect(url_for("main.index"))

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

@exports_bp.route("/exports", methods=["GET"])
def export_history():
    return render_template(
        "export_history.html",
        archives=list_export_archives(),
    )

@exports_bp.route("/exports/<path:filename>/details", methods=["GET"])
def export_batch_details(filename: str):
    safe_name = Path(filename).name
    batch = fetch_export_batch(safe_name)
    if not batch:
        flash("That export batch was not found.")
        return redirect(url_for("exports.export_history"))

    lot_numbers = [int(value) for value in batch.get("lot_numbers", "").split(",") if value.isdigit()]
    items = fetch_items_for_lot_numbers(lot_numbers)
    return render_template(
        "export_batch_details.html",
        batch=batch,
        items=items,
    )

@exports_bp.route("/exports/<path:filename>", methods=["GET"])
def download_export_archive(filename: str):
    safe_name = Path(filename).name
    target = EXPORTS_DIR / safe_name
    if not target.exists() or not target.is_file():
        flash("That export file was not found.")
        return redirect(url_for("exports.export_history"))
    return send_from_directory(EXPORTS_DIR, safe_name, as_attachment=True)

@exports_bp.route("/export_selected_csv", methods=["POST"])
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