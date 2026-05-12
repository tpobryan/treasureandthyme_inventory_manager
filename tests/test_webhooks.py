import os
# DATABASE_URL set via settings in fixture

import pytest
from app.app import app
from app.database import connect_item_store, ensure_item_store_ready
from app.config import settings
from app.routes.integrations import PLATFORMS

class MockEbayIntegration:
    def delete_listing(self, lot_number, remote_id):
        return True

PLATFORMS["ebay"] = MockEbayIntegration()

@pytest.fixture
def client():
    # Override settings for testing
    settings.DATABASE_URL = "sqlite:///test.db"
    app.config["SQLALCHEMY_DATABASE_URI"] = settings.effective_database_url
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with app.app_context():
            # Create a test lot and statuses
            ensure_item_store_ready()
            conn, dialect = connect_item_store()
            if conn:
                try:
                    cursor = conn.cursor()
                    
                    # Add item platform statuses for testing cross-cancel
                    cursor.execute(
                        "INSERT INTO item_platform_status (lot_number, platform_id, remote_id, status) VALUES (?, ?, ?, ?)",
                        (9999, "etsy", "etsy_listing_9999", "active")
                    )
                    cursor.execute(
                        "INSERT INTO item_platform_status (lot_number, platform_id, remote_id, status) VALUES (?, ?, ?, ?)",
                        (9999, "ebay", "ebay_listing_9999", "active")
                    )
                    conn.commit()
                except Exception:
                    pass
                finally:
                    conn.close()
        
        yield client
        
        # Cleanup
        if os.path.exists("test.db"):
            os.remove("test.db")

def test_etsy_webhook_sale_triggers_cancellation(client):
    # Simulate a webhook for etsy_listing_9999 selling
    payload = {
        "event_type": "shop_receipt",
        "listing_id": "etsy_listing_9999"
    }
    
    response = client.post("/api/webhooks/etsy", json=payload)
    assert response.status_code == 200
    
    # Check that the ebay listing was cancelled
    conn, dialect = connect_item_store()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM item_platform_status WHERE lot_number = 9999 AND platform_id = 'ebay'")
        row = cursor.fetchone()
        assert row is not None
        assert row["status"] in ["cancelled", "cancel_failed"]
        
        cursor.execute("SELECT status FROM item_platform_status WHERE lot_number = 9999 AND platform_id = 'etsy'")
        row = cursor.fetchone()
        assert row is not None
        assert row["status"] == "sold"
    finally:
        conn.close()
