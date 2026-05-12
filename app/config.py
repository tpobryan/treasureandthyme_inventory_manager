from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    BASE_DIR: Path = BASE_DIR
    
    # Flask settings
    SECRET_KEY: str = "dev-secret-change-me"
    SESSION_COOKIE_SECURE: bool = False
    MAX_CONTENT_LENGTH: int = 50 * 1024 * 1024
    MAX_FORM_MEMORY_SIZE: int = 50 * 1024 * 1024

    # Database settings
    DATABASE_URL: Optional[str] = None
    CURRENT_AUCTION_ID: Optional[int] = None
    AUCTION_NUMBER: Optional[int] = None  # Legacy support

    # Auth settings
    APP_LOGIN_USERNAME: str = "admin"
    APP_LOGIN_PASSWORD: str = ""

    # FTP settings
    FTP_HOST: str = ""
    FTP_USERNAME: str = ""
    FTP_PASSWORD: str = ""
    FTP_PORT: int = 21
    FTP_TLS: bool = False
    FTP_RENAME_DIR: str = ""

    # AI settings
    OPENAI_API_KEY: str = "dummy-key-if-missing"
    OPENAI_MODEL: str = "gpt-4o"

    # Etsy settings
    ETSY_KEY_STRING: str = ""
    ETSY_SHARED_SECRET: str = ""
    ETSY_REDIRECT_URI: str = "http://localhost:5005/api/integrations/etsy/connect"

    # eBay settings
    EBAY_CLIENT_ID: str = ""
    EBAY_CLIENT_SECRET: str = ""
    EBAY_RUNAME: str = ""
    EBAY_REDIRECT_URI: str = ""

    # Path settings
    DATA_DIR: Path = BASE_DIR / "data"
    UPLOADS_DIR: Path = BASE_DIR / "data" / "uploads"
    EXPORTS_DIR: Path = BASE_DIR / "data" / "exports"

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def effective_database_url(self) -> str:
        url = self.DATABASE_URL
        if not url:
            return f"sqlite:///{self.DATA_DIR / 'auction_items.db'}"
        if url.startswith("mysql://"):
            return url.replace("mysql://", "mysql+pymysql://", 1)
        return url

    @property
    def effective_auction_id(self) -> int:
        if self.CURRENT_AUCTION_ID is not None:
            return self.CURRENT_AUCTION_ID
        if self.AUCTION_NUMBER is not None:
            return self.AUCTION_NUMBER
        return 4 # Default

settings = Settings()
