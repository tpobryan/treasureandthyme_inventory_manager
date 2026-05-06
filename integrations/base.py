from abc import ABC, abstractmethod
from typing import Any, Dict

class PlatformIntegration(ABC):
    """
    Abstract base class for all platform integrations (eBay, Etsy, etc.)
    """
    
    @property
    @abstractmethod
    def platform_id(self) -> str:
        """Returns the unique identifier for the platform (e.g., 'ebay', 'etsy')."""
        pass

    @abstractmethod
    def authenticate(self, *args, **kwargs) -> Dict[str, Any]:
        """
        Handles OAuth or API key authentication and returns updated tokens/settings.
        """
        pass

    @abstractmethod
    def publish_listing(self, lot_number: int, item_data: Dict[str, Any]) -> str:
        """
        Publishes a listing to the platform and returns the remote listing ID.
        """
        pass

    @abstractmethod
    def update_listing(self, lot_number: int, remote_id: str, item_data: Dict[str, Any]) -> bool:
        """
        Updates an existing listing on the platform. Returns True on success.
        """
        pass

    @abstractmethod
    def delete_listing(self, lot_number: int, remote_id: str) -> bool:
        """
        Deletes or ends a listing on the platform. Returns True on success.
        """
        pass

    @abstractmethod
    def handle_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses an incoming webhook payload and returns a normalized dictionary 
        containing event details (e.g., 'event_type': 'sale', 'remote_id': '12345').
        """
        pass
