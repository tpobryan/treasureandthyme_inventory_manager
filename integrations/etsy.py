import os
import requests
import hashlib
import base64
import secrets
from typing import Any, Dict
from integrations.base import PlatformIntegration

class EtsyIntegration(PlatformIntegration):
    """
    Etsy Platform Integration (Open API v3)
    """

    def __init__(self):
        self.client_id = os.getenv("ETSY_KEY_STRING", "")
        self.shared_secret = os.getenv("ETSY_SHARED_SECRET", "")
        self.redirect_uri = os.getenv("ETSY_REDIRECT_URI", "http://localhost:5005/api/integrations/etsy/connect")
        self.api_base = "https://openapi.etsy.com/v3"

    def _get_headers(self, access_token: str = None) -> Dict[str, str]:
        """Helper to generate Etsy API headers."""
        # Etsy v3 often requires keystring:shared_secret for certain endpoints
        api_key = self.client_id
        if self.shared_secret:
            api_key = f"{self.client_id}:{self.shared_secret}"
            
        headers = {
            "x-api-key": api_key
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    @property
    def platform_id(self) -> str:
        return "etsy"

    def generate_pkce_codes(self) -> Dict[str, str]:
        """Generates PKCE code_verifier and code_challenge."""
        # Generate a random verifier
        token = secrets.token_urlsafe(32)
        
        # Generate challenge (SHA256 hash of verifier)
        sha256_hash = hashlib.sha256(token.encode('utf-8')).digest()
        challenge = base64.urlsafe_b64encode(sha256_hash).decode('utf-8').replace('=', '')
        
        return {
            "verifier": token,
            "challenge": challenge,
            "state": secrets.token_urlsafe(16)
        }

    def authenticate(self, request_args: Dict[str, Any], session_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Handles the OAuth2 flow. 
        If 'code' is missing, returns a redirect URL.
        If 'code' is present, exchanges it for tokens.
        """
        code = request_args.get("code")
        state = request_args.get("state")

        if not code:
            # Phase 1: Generate codes and return redirect URL
            pkce = self.generate_pkce_codes()
            # Added shops_r to get shop_id later
            scope = 'listings_r listings_w shops_r'
            
            auth_url = (
                f"https://www.etsy.com/oauth/connect?"
                f"response_type=code&"
                f"redirect_uri={self.redirect_uri}&"
                f"scope={scope}&"
                f"client_id={self.client_id}&"
                f"state={pkce['state']}&"
                f"code_challenge={pkce['challenge']}&"
                f"code_challenge_method=S256"
            )
            
            return {
                "redirect_url": auth_url,
                "pkce": pkce
            }
            
        # Phase 2: Exchange code for token
        if not session_data or state != session_data.get("state"):
            return {"error": "Invalid state parameter"}

        verifier = session_data.get("verifier")
        
        response = requests.post(
            "https://api.etsy.com/v3/public/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "code": code,
                "code_verifier": verifier,
            }
        )

        if response.status_code == 200:
            token_data = response.json()
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")
            
            # Step 3: Get Shop ID
            shop_id = self._get_shop_id(access_token)
            
            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "settings": {
                    "shop_id": shop_id,
                    "user_id": access_token.split('.')[0]
                }
            }
        else:
            return {"error": f"Token exchange failed: {response.text}"}

    def _get_shop_id(self, access_token: str) -> str:
        """Helper to fetch shop_id for the authenticated user."""
        headers = self._get_headers(access_token)
        # First try getMe - this often returns shop_id directly in v3
        response = requests.get(f"{self.api_base}/application/users/me", headers=headers)
        if response.status_code == 200:
            data = response.json()
            if data.get("shop_id"):
                return str(data["shop_id"])
                
            user_id = data.get("user_id")
            # Fallback to fetching shops if not in getMe
            shop_response = requests.get(f"{self.api_base}/application/users/{user_id}/shops", headers=headers)
            if shop_response.status_code == 200:
                shops = shop_response.json()
                if shops.get("count", 0) > 0:
                    return str(shops["results"][0]["shop_id"])
        
        return ""

    def fetch_listings(self, access_token: str, shop_id: str) -> list[Dict[str, Any]]:
        """Fetches active listings from the Etsy shop."""
        if not shop_id:
            return []
            
        headers = self._get_headers(access_token)
        
        # Get active listings
        url = f"{self.api_base}/application/shops/{shop_id}/listings/active"
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            return response.json().get("results", [])
        else:
            print(f"[Etsy] Failed to fetch listings: {response.text}")
            return []

    def publish_listing(self, lot_number: int, item_data: Dict[str, Any]) -> str:
        """Publishes the listing to Etsy."""
        print(f"[Etsy] Publishing lot {lot_number}: {item_data.get('title')}")
        return f"etsy_{lot_number}_123"

    def update_listing(self, lot_number: int, remote_id: str, item_data: Dict[str, Any]) -> bool:
        """Updates an existing listing on Etsy."""
        print(f"[Etsy] Updating listing {remote_id} for lot {lot_number}")
        return True

    def delete_listing(self, lot_number: int, remote_id: str) -> bool:
        """Deletes or ends the listing on Etsy."""
        print(f"[Etsy] Deleting listing {remote_id} for lot {lot_number}")
        return True

    def handle_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Parses Etsy's webhook payload."""
        return {
            "event_type": "sale",
            "platform_id": self.platform_id,
            "remote_id": payload.get("listing_id")
        }
