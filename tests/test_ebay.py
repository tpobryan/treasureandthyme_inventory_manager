import pytest
from unittest.mock import MagicMock, patch
from app.integrations.ebay import EbayIntegration

@pytest.fixture
def ebay():
    return EbayIntegration()

def test_ebay_headers(ebay):
    # No token
    headers = ebay._get_headers()
    assert headers["Content-Type"] == "application/json"
    assert "Authorization" not in headers
    
    # With token
    headers = ebay._get_headers("fake-ebay-token")
    assert headers["Authorization"] == "Bearer fake-ebay-token"

def test_ebay_auth_redirect_url(ebay):
    # Phase 1: No code in request
    result = ebay.authenticate({})
    assert "redirect_url" in result
    assert "client_id" in result["redirect_url"]
    assert "pkce" in result
    assert "state" in result["pkce"]

@patch("requests.post")
def test_ebay_auth_token_exchange(mock_post, ebay):
    # Phase 2: Code in request
    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {
        "access_token": "abc",
        "refresh_token": "def",
        "expires_in": 7200
    }
    mock_post.return_value = mock_res
    
    result = ebay.authenticate({"code": "my-code", "state": "my-state"})
    
    assert result["access_token"] == "abc"
    assert result["refresh_token"] == "def"
    assert result["settings"]["expires_in"] == 7200
    
    # Verify mock post call
    args, kwargs = mock_post.call_args
    assert "Authorization" in kwargs["headers"]
    assert kwargs["headers"]["Authorization"].startswith("Basic ")
    assert kwargs["data"]["code"] == "my-code"

def test_ebay_placeholders(ebay):
    # Test placeholders don't crash
    assert ebay.publish_listing(1, {"Title": "Item"}) == "ebay_1_draft"
    assert ebay.update_listing(1, "rem1", {}) is True
    assert ebay.delete_listing(1, "rem1") is True
