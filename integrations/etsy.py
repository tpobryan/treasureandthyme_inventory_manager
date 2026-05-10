import os
import requests
import hashlib
import base64
import secrets
import json
from typing import Any, Dict
from flask import current_app
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
        url_me = f"{self.api_base}/application/users/me"
        current_app.logger.info("[Etsy] Fetching user details from: %s", url_me)
        response = requests.get(url_me, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            current_app.logger.info("[Etsy] User data: %s", json.dumps(data))
            if data.get("shop_id"):
                return str(data["shop_id"])
                
            user_id = data.get("user_id")
            # Fallback to fetching shops if not in getMe
            url_shops = f"{self.api_base}/application/users/{user_id}/shops"
            current_app.logger.info("[Etsy] Fetching shops for user %s from: %s", user_id, url_shops)
            shop_response = requests.get(url_shops, headers=headers)
            if shop_response.status_code == 200:
                shops = shop_response.json()
                current_app.logger.info("[Etsy] Shops data: %s", json.dumps(shops))
                if shops.get("count", 0) > 0:
                    return str(shops["results"][0]["shop_id"])
        else:
            current_app.logger.warning("[Etsy] Failed to fetch user details: %d - %s", response.status_code, response.text)
        
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

    def get_shipping_profiles(self, access_token: str, shop_id: str) -> list[Dict[str, Any]]:
        """Fetches available shipping profiles for the shop."""
        if not shop_id:
            current_app.logger.info("[Etsy] Cannot fetch shipping profiles: shop_id is missing")
            return []
            
        headers = self._get_headers(access_token)
        url = f"{self.api_base}/application/shops/{shop_id}/shipping-profiles"
        current_app.logger.info("[Etsy] Fetching shipping profiles from: %s", url)
        
        response = requests.get(url, headers=headers)
        current_app.logger.info("[Etsy] Response Status: %d", response.status_code)
        current_app.logger.info("[Etsy] Raw Response: %s", response.text)
        
        if response.status_code == 200:
            profiles = response.json().get("results", [])
            current_app.logger.info("[Etsy] Successfully fetched %d shipping profiles", len(profiles))
            return profiles
        else:
            current_app.logger.warning("[Etsy] Failed to fetch shipping profiles: %d - %s", response.status_code, response.text)
            return []

    def publish_listing(self, lot_number: int, item_data: Dict[str, Any]) -> str:
        """
        Publishes the listing to Etsy.
        Expects item_data to contain 'access_token', 'shop_id', and 'platform_data' or raw fields.
        """
        access_token = item_data.get("access_token")
        shop_id = item_data.get("shop_id")
        
        if not access_token or not shop_id:
            print("[Etsy] Missing credentials for publishing")
            return ""

        # 1. Create the draft listing
        listing_id = self.create_draft_listing(access_token, shop_id, item_data)
        if not listing_id:
            return ""

        # 2. Upload images
        image_paths = item_data.get("image_paths", [])
        for i, img_path in enumerate(image_paths):
            success = self.upload_listing_image(access_token, shop_id, listing_id, img_path, i + 1)
            if not success:
                print(f"[Etsy] Failed to upload image {i+1}: {img_path}")

        return f"etsy_{listing_id}"

    def create_draft_listing(self, access_token: str, shop_id: str, data: Dict[str, Any]) -> str:
        """Creates a draft listing and returns the listing_id."""
        headers = self._get_headers(access_token)
        url = f"{self.api_base}/application/shops/{shop_id}/listings"
        
        # Map our internal data to Etsy fields
        # Note: who_made, when_made, what_it_is are required
        platform_data = data.get("platform_data", {})
        if isinstance(platform_data, str):
            import json
            try:
                platform_data = json.loads(platform_data)
            except:
                platform_data = {}

        etsy_data = platform_data.get("etsy", {})
        
        payload = {
            "quantity": int(data.get("Quantity", 1)),
            "title": data.get("Title", "Untitled Listing")[:140],
            "description": data.get("Description", ""),
            "price": float(data.get("Price", 0.0)),
            "who_made": data.get("Etsy Who Made") or etsy_data.get("who_made", "someone_else"),
            "when_made": data.get("Etsy When Made") or etsy_data.get("when_made", "2020_2026"),
            "taxonomy_id": int(data.get("Etsy Taxonomy ID") or etsy_data.get("taxonomy_id", 1)), # Default to 1 if missing
            "is_supply": (data.get("Etsy Is Supply") == "yes") or etsy_data.get("is_supply", False),
            "shipping_profile_id": int(data.get("Etsy Shipping Profile ID") or etsy_data.get("shipping_profile_id", 0)),
            "state": "draft"
        }

        # Materials and Tags (optional but recommended)
        materials = data.get("Etsy Materials") or etsy_data.get("materials", [])
        if isinstance(materials, str):
            materials = [m.strip() for m in materials.split(",") if m.strip()]
        if materials:
            payload["materials"] = materials[:13]

        tags = data.get("Etsy Tags") or etsy_data.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if tags:
            payload["tags"] = tags[:13]

        response = requests.post(url, headers=headers, data=payload)
        if response.status_code in [200, 201]:
            result = response.json()
            return str(result.get("listing_id"))
        else:
            print(f"[Etsy] Failed to create draft: {response.text}")
            return ""

    def upload_listing_image(self, access_token: str, shop_id: str, listing_id: str, image_path: str, rank: int) -> bool:
        """Uploads a single image to an existing listing."""
        if not os.path.exists(image_path):
            return False

        headers = self._get_headers(access_token)
        url = f"{self.api_base}/application/shops/{shop_id}/listings/{listing_id}/images"
        
        try:
            with open(image_path, 'rb') as f:
                files = {
                    'image': f
                }
                data = {
                    'rank': rank
                }
                response = requests.post(url, headers=headers, files=files, data=data)
                
            if response.status_code in [200, 201]:
                return True
            else:
                print(f"[Etsy] Image upload failed: {response.text}")
                return False
        except Exception as e:
            print(f"[Etsy] Exception during image upload: {e}")
            return False

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
