from flask import Blueprint, render_template, request, flash, redirect, url_for

from database import (
    create_next_auction,
    switch_current_auction,
    update_auction_status,
    fetch_auction_summaries,
)

auctions_bp = Blueprint("auctions", __name__)

@auctions_bp.route("/auctions", methods=["GET"])
def auctions_overview():
    return render_template(
        "auctions.html",
        auctions=fetch_auction_summaries(),
    )

@auctions_bp.route("/auctions/create_next", methods=["POST"])
def create_auction_route():
    auction = create_next_auction()
    flash(f"Created auction {auction['id']} and switched to it.")
    return redirect(request.form.get("return_to") or url_for("main.dashboard"))

@auctions_bp.route("/auctions/switch", methods=["POST"])
def switch_auction_route():
    auction_id = request.form.get("auction_id", "").strip()
    if not auction_id.isdigit() or not switch_current_auction(int(auction_id)):
        flash("Choose a valid auction to switch to.")
        return redirect(request.form.get("return_to") or url_for("main.dashboard"))

    flash(f"Now working in auction {auction_id}.")
    return redirect(request.form.get("return_to") or url_for("main.dashboard"))

@auctions_bp.route("/auctions/status", methods=["POST"])
def update_auction_status_route():
    auction_id = request.form.get("auction_id", "").strip()
    status = request.form.get("status", "").strip().lower()
    if not auction_id.isdigit() or not update_auction_status(int(auction_id), status):
        flash("Choose a valid auction and status.")
        return redirect(request.form.get("return_to") or url_for("main.dashboard"))

    flash(f"Auction {auction_id} is now marked {status}.")
    return redirect(request.form.get("return_to") or url_for("main.dashboard"))