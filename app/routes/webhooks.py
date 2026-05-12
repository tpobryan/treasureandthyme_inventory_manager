import json
import logging
from flask import Blueprint, request, jsonify, current_app
from ..database import connect_item_store, ensure_item_store_ready
from .integrations import PLATFORMS

webhooks_bp = Blueprint("webhooks", __name__)
logger = logging.getLogger(__name__)

def cancel_cross_platform_listings(sold_lot_number: int, sold_platform_id: str):
    """
    Cancel listings on other platforms when an item sells.
    """
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    
    if not connection:
        return
        
    try:
        cursor = connection.cursor()
        
        # Find all other platforms where this item is active
        if dialect == "sqlite":
            cursor.execute(
                """
                SELECT platform_id, remote_id 
                FROM item_platform_status 
                WHERE lot_number = ? AND platform_id != ? AND status != 'cancelled'
                """,
                (sold_lot_number, sold_platform_id)
            )
        else:
            cursor.execute(
                """
                SELECT platform_id, remote_id 
                FROM item_platform_status 
                WHERE lot_number = %s AND platform_id != %s AND status != 'cancelled'
                """,
                (sold_lot_number, sold_platform_id)
            )
            
        other_platforms = cursor.fetchall()
        
        for row in other_platforms:
            platform_id = row["platform_id"]
            remote_id = row["remote_id"]
            
            integration = PLATFORMS.get(platform_id)
            if not integration:
                continue
                
            try:
                success = integration.delete_listing(sold_lot_number, remote_id)
                new_status = "cancelled" if success else "cancel_failed"
                
                # Update status
                if dialect == "sqlite":
                    cursor.execute(
                        "UPDATE item_platform_status SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE lot_number = ? AND platform_id = ?",
                        (new_status, sold_lot_number, platform_id)
                    )
                else:
                    cursor.execute(
                        "UPDATE item_platform_status SET status = %s WHERE lot_number = %s AND platform_id = %s",
                        (new_status, sold_lot_number, platform_id)
                    )
                connection.commit()
                
            except Exception as e:
                logger.error(f"Failed to cancel listing on {platform_id} for lot {sold_lot_number}: {e}")
                
    finally:
        connection.close()


@webhooks_bp.route("/api/webhooks/etsy", methods=["POST"])
def etsy_webhook():
    """Receive Etsy webhooks (e.g. shop_receipt for sales)."""
    payload = request.json or {}
    
    integration = PLATFORMS.get("etsy")
    if not integration:
        return jsonify({"error": "Etsy integration not configured"}), 500
        
    try:
        event = integration.handle_webhook(payload)
        
        if event.get("event_type") == "sale":
            remote_id = event.get("remote_id")
            
            # Find the lot number for this remote_id
            ensure_item_store_ready()
            connection, dialect = connect_item_store()
            if connection:
                try:
                    cursor = connection.cursor()
                    placeholder = "?" if dialect == "sqlite" else "%s"
                    cursor.execute(
                        f"SELECT lot_number FROM item_platform_status WHERE platform_id = 'etsy' AND remote_id = {placeholder}",
                        (remote_id,)
                    )
                    row = cursor.fetchone()
                    if row:
                        lot_number = row["lot_number"]
                        logger.info(f"Etsy sale detected for lot {lot_number}. Initiating cross-platform cancellation.")
                        
                        # Mark Etsy as sold
                        cursor.execute(
                            f"UPDATE item_platform_status SET status = 'sold' WHERE lot_number = {placeholder} AND platform_id = 'etsy'",
                            (lot_number,)
                        )
                        connection.commit()
                        
                        # Cancel on other platforms
                        cancel_cross_platform_listings(lot_number, "etsy")
                finally:
                    connection.close()
                    
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f"Error handling Etsy webhook: {e}")
        return jsonify({"error": str(e)}), 500
