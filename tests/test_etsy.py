import json
import pytest
from unittest.mock import MagicMock, patch
from integrations.etsy import EtsyIntegration

@pytest.fixture
def etsy():
    return EtsyIntegration()

@pytest.fixture
def mock_app(monkeypatch):
    mock = MagicMock()
    # Mock current_app.logger
    import integrations.etsy
    monkeypatch.setattr(integrations.etsy, "current_app", mock)
    return mock

def test_etsy_headers(etsy):
    # No token
    headers = etsy._get_headers()
    assert "x-api-key" in headers
    assert "Authorization" not in headers
    
    # With token
    headers = etsy._get_headers("fake-token")
    assert headers["Authorization"] == "Bearer fake-token"

def test_etsy_pkce_generation(etsy):
    pkce = etsy.generate_pkce_codes()
    assert "verifier" in pkce
    assert "challenge" in pkce
    assert "state" in pkce
    assert len(pkce["verifier"]) > 0
    assert len(pkce["challenge"]) > 0

@patch("requests.get")
def test_etsy_get_shop_id(mock_get, etsy, mock_app):
    # Mock /users/me
    mock_me = MagicMock()
    mock_me.status_code = 200
    mock_me.json.return_value = {"user_id": 123, "shop_id": 456}
    
    # Mock /users/123/shops
    mock_shops = MagicMock()
    mock_shops.status_code = 200
    mock_shops.json.return_value = {"count": 1, "results": [{"shop_id": 789}]}
    
    mock_get.side_effect = [mock_me, mock_shops, MagicMock(status_code=404)] # diagnostics call fails
    
    shop_id = etsy._get_shop_id("fake-token")
    assert shop_id == "789" # From the shops list

@patch("requests.post")
@patch("requests.get")
def test_etsy_create_draft_listing(mock_get, mock_post, etsy, mock_app):
    # Mock readiness fetch
    mock_readiness = MagicMock()
    mock_readiness.status_code = 200
    mock_readiness.json.return_value = {"count": 1, "results": [{"readiness_state_id": 111}]}
    mock_get.return_value = mock_readiness
    
    # Mock listing creation
    mock_create = MagicMock()
    mock_create.status_code = 201
    mock_create.json.return_value = {"listing_id": 12345}
    mock_post.return_value = mock_create
    
    item_data = {
        "Title": "Test Item",
        "Description": "Test Desc",
        "Price": "25.00",
        "Quantity": "2",
        "Etsy Taxonomy ID": "1179",
        "Etsy Materials": "Silver, Gold",
        "Etsy Tags": "Jewelry, Gift",
    }
    
    listing_id = etsy.create_draft_listing("fake-token", "fake-shop", item_data)
    
    assert listing_id == "12345"
    
    # Verify the payload sent to Etsy
    args, kwargs = mock_post.call_args
    payload = kwargs["data"]
    assert payload["title"] == "Test Item"
    assert payload["price"] == 25.0
    assert payload["quantity"] == 2
    assert payload["taxonomy_id"] == 1179
    assert "Silver" in payload["materials"]
    assert "Jewelry" in payload["tags"]
    assert payload["readiness_state_id"] == 111

def test_etsy_create_draft_listing_mapping_from_platform_data(etsy, mock_app):
    # Test mapping when data is in platform_data JSON
    item_data = {
        "platform_data": {
            "etsy": {
                "taxonomy_id": "1217",
                "price": 30.0,
                "tags": ["necklace", "blue"]
            }
        },
        "Title": "Necklace"
    }
    
    with patch("requests.get") as mock_get, patch("requests.post") as mock_post:
        mock_get.return_value.status_code = 404 # skip readiness
        mock_post.return_value.status_code = 201
        mock_post.return_value.json.return_value = {"listing_id": 999}
        
        etsy.create_draft_listing("token", "shop", item_data)
        
        args, kwargs = mock_post.call_args
        payload = kwargs["data"]
        assert payload["taxonomy_id"] == 1217
        assert "necklace" in payload["tags"]
