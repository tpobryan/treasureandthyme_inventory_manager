from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
)
from database import (
    AUCTION_STATUS_PREPARING,
    AUCTION_STATUS_ACTIVE,
    AUCTION_STATUS_COMPLETED,
    get_current_auction,
    list_auctions,
)

from utils import (
    auth_enabled,
    is_authenticated,
)

load_dotenv()

from routes.items import items_bp
from routes.auctions import auctions_bp
from routes.exports import exports_bp
from routes.main import main_bp
from routes.admin import admin_bp
from routes.auth import auth_bp

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

app.register_blueprint(items_bp)
app.register_blueprint(auctions_bp)
app.register_blueprint(exports_bp)
app.register_blueprint(main_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(auth_bp)


@app.route("/healthz", methods=["GET"])
def healthz():
    return Response("ok\n", mimetype="text/plain")


@app.context_processor
def inject_auction_context() -> dict[str, Any]:
    return {
        "current_auction": get_current_auction(),
        "auction_list": list_auctions(),
        "auction_statuses": [
            AUCTION_STATUS_PREPARING,
            AUCTION_STATUS_ACTIVE,
            AUCTION_STATUS_COMPLETED,
        ],
        "auth_enabled": auth_enabled(),
        "is_authenticated": is_authenticated(),
    }


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    app.run(host=host, port=port, debug=debug)
