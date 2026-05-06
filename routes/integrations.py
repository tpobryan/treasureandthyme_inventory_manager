from flask import Blueprint, jsonify, request, redirect, url_for
from database import connect_item_store, ensure_item_store_ready
import json
import logging
from integrations.etsy import EtsyIntegration

integrations_bp = Blueprint("integrations", __name__)
logger = logging.getLogger(__name__)

# This will map platform_id to the specific integration class instance
# e.g., PLATFORMS = {'etsy': EtsyIntegration(), 'ebay': EbayIntegration()}
PLATFORMS = {
    'etsy': EtsyIntegration()
}

@integrations_bp.route("/api/integrations", methods=["GET"])
def list_integrations():
    """List all available integrations and their connection status."""
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    
    if not connection:
        return jsonify({"error": "Database error"}), 500
        
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT platform_id FROM integrations")
        connected_platforms = {row["platform_id"] for row in cursor.fetchall()}
        
        results = []
        for platform_id in PLATFORMS.keys():
            results.append({
                "platform_id": platform_id,
                "is_connected": platform_id in connected_platforms
            })
            
        return jsonify(results)
    finally:
        connection.close()

@integrations_bp.route("/api/integrations/<platform_id>/connect", methods=["GET", "POST"])
def connect_integration(platform_id):
    """Start or handle the OAuth flow for a platform."""
    if platform_id not in PLATFORMS:
        return jsonify({"error": f"Platform {platform_id} not supported"}), 404
        
    integration = PLATFORMS[platform_id]
    
    # Normally this would redirect to the platform's OAuth URL, or handle the callback
    # For now, we will just call the authenticate method which might return a URL
    # or handle the callback args.
    
    auth_result = integration.authenticate(request.args)
    
    if "redirect_url" in auth_result:
        return redirect(auth_result["redirect_url"])
        
    if "error" in auth_result:
        return jsonify({"error": auth_result["error"]}), 400
        
    # If successful, save to database
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    try:
        cursor = connection.cursor()
        settings_json = json.dumps(auth_result.get("settings", {}))
        
        if dialect == "sqlite":
            cursor.execute(
                """
                INSERT INTO integrations (platform_id, access_token, refresh_token, settings_json, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(platform_id) DO UPDATE SET
                    access_token=excluded.access_token,
                    refresh_token=excluded.refresh_token,
                    settings_json=excluded.settings_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (platform_id, auth_result.get("access_token"), auth_result.get("refresh_token"), settings_json)
            )
        else:
            cursor.execute(
                """
                INSERT INTO integrations (platform_id, access_token, refresh_token, settings_json)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    access_token=VALUES(access_token),
                    refresh_token=VALUES(refresh_token),
                    settings_json=VALUES(settings_json)
                """,
                (platform_id, auth_result.get("access_token"), auth_result.get("refresh_token"), settings_json)
            )
        connection.commit()
        return jsonify({"status": "success", "message": f"Connected to {platform_id}"})
    finally:
        connection.close()
