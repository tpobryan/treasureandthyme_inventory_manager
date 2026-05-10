import os
import requests
import base64
import json
from typing import Any, Dict
from integrations.base import PlatformIntegration

class EbayIntegration(PlatformIntegration):
    """
    eBay Platform Integration (RESTful Inventory API)
    """

    def __init__(self):
        self.client_id = os.getenv("EBAY_CLIENT_ID", "")
        self.client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
        self.runame = os.getenv("EBAY_RUNAME", "")
        self.redirect_uri = os.getenv("EBAY_REDIRECT_URI", "")
        self.api_base = "https://api.ebay.com/sell/inventory/v1"
        self.auth_base = "https://auth.ebay.com/oauth2/authorize"
        self.token_url = "https://api.ebay.com/identity/v1/oauth2/token"

    @property
    def platform_id(self) -> str:
        return "ebay"

    def _get_headers(self, access_token: str = None) -> Dict[str, str]:
        """Helper to generate eBay API headers."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def authenticate(self, request_args: Dict[str, Any], session_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Handles eBay OAuth2 flow.
        """
        code = request_args.get("code")
        state = request_args.get("state")

        if not code:
            # Phase 1: Redirect to eBay for consent
            import secrets
            state = secrets.token_urlsafe(16)
            
            # Scopes for Inventory API
            scopes = [
                "https://api.ebay.com/oauth/api_scope/sell.inventory",
                "https://api.ebay.com/oauth/api_scope/sell.marketing",
                "https://api.ebay.com/oauth/api_scope/sell.account"
            ]
            
            auth_url = (
                f"{self.auth_base}?"
                f"client_id={self.client_id}&"
                f"redirect_uri={self.runame}&"
                f"response_type=code&"
                f"state={state}&"
                f"scope={' '.join(scopes)}"
            )
            
            return {
                "redirect_url": auth_url,
                "pkce": {"state": state} # We use this to verify the state on return
            }

        # Phase 2: Exchange code for token
        # Note: eBay requires Basic Auth with Base64(client_id:client_secret)
        auth_str = f"{self.client_id}:{self.client_secret}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {b64_auth}"
        }
        
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.runame
        }
        
        response = requests.post(self.token_url, headers=headers, data=data)
        
        if response.status_code == 200:
            token_data = response.json()
            return {
                "access_token": token_data.get("access_token"),
                "refresh_token": token_data.get("refresh_token"),
                "settings": {
                    "expires_in": token_data.get("expires_in"),
                    "refresh_token_expires_in": token_data.get("refresh_token_expires_in")
                }
            }
        else:
            return {"error": f"eBay token exchange failed: {response.text}"}

    def publish_listing(self, lot_number: int, item_data: Dict[str, Any]) -> str:
        """
        Publishes the listing to eBay.
        Note: eBay uses a different flow (Create Inventory Item -> Create Offer -> Publish Offer).
        For now, this is a placeholder.
        """
        print(f"[eBay] Publishing lot {lot_number}: {item_data.get('Title')}")
        return f"ebay_{lot_number}_draft"

    def update_listing(self, lot_number: int, remote_id: str, item_data: Dict[str, Any]) -> bool:
        return True

    def delete_listing(self, lot_number: int, remote_id: str) -> bool:
        return True

    def handle_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "event_type": "sale",
            "platform_id": self.platform_id,
            "remote_id": payload.get("listing_id")
        }
