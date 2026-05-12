import os
import json
from pathlib import Path
from typing import Any, Dict, List
from flask import current_app
from ..database import get_platform_credentials, update_platform_status
from .etsy import EtsyIntegration
from .ebay import EbayIntegration

# Registry of platform integrations
PLATFORMS = {
    "etsy": EtsyIntegration(),
    "ebay": EbayIntegration()
}

def process_platform_publishing(lot_number: int, form: Dict[str, Any], image_folder: str):
    """
    Main entry point for publishing a lot to various platforms.
    Should be called after the item is saved to the local database.
    """
    platforms_to_publish = []
    if form.get("Listing Strategy") == "retail":
        if form.get("Publish to eBay") == "yes":
            platforms_to_publish.append("ebay")
        if form.get("Publish to Etsy") == "yes":
            platforms_to_publish.append("etsy")

    if not platforms_to_publish:
        return

    for platform_id in platforms_to_publish:
        if platform_id not in PLATFORMS:
            current_app.logger.warning("[Publisher] Platform %s not supported for direct publishing yet.", platform_id)
            continue

        try:
            publish_to_platform(platform_id, lot_number, form, image_folder)
        except Exception as exc:
            current_app.logger.exception(f"Failed to publish lot {lot_number} to {platform_id}")

def publish_to_platform(platform_id: str, lot_number: int, form: Dict[str, Any], image_folder: str):
    """Orchestrates the publishing for a specific platform."""
    integration = PLATFORMS[platform_id]
    
    # 1. Get credentials
    creds = get_platform_credentials(platform_id)
    if not creds or not creds.get("access_token"):
        current_app.logger.warning("[Publisher] %s is not connected. Cannot publish.", platform_id)
        return

    # 2. Prepare item data
    # We need full image paths
    from ..config import settings
    uploads_dir = settings.UPLOADS_DIR
    final_dir = uploads_dir / image_folder
    image_paths = sorted([str(p) for p in final_dir.iterdir() if p.is_file() and p.suffix.lower() in [".jpg", ".jpeg", ".png"]])

    item_data = {
        **form,
        "access_token": creds["access_token"],
        "shop_id": creds["settings"].get("shop_id"),
        "image_paths": image_paths
    }

    # 3. Call integration
    current_app.logger.info("[Publisher] Publishing lot %s to %s...", lot_number, platform_id)
    remote_id = integration.publish_listing(lot_number, item_data)
    
    if remote_id:
        current_app.logger.info("[Publisher] Successfully published lot %s to %s. Remote ID: %s", lot_number, platform_id, remote_id)
        # 4. Update status in database
        update_platform_status(lot_number, platform_id, "published", remote_id=remote_id)
    else:
        current_app.logger.warning("[Publisher] Failed to publish lot %s to %s.", lot_number, platform_id)
        update_platform_status(lot_number, platform_id, "failed")
