from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from flask import (
    abort,
    Flask,
    Response,
    request,
)
from .config import settings
from .database import (
    AUCTION_STATUS_PREPARING,
    AUCTION_STATUS_ACTIVE,
    AUCTION_STATUS_COMPLETED,
    get_current_auction,
    list_auctions,
)

from .utils import (
    auth_enabled,
    csrf_input,
    get_csrf_token,
    is_authenticated,
    validate_csrf_token,
)

# load_dotenv() # Handled by pydantic-settings

from .extensions import limiter, db, migrate

from .routes.items import items_bp
from .routes.auctions import auctions_bp
from .routes.exports import exports_bp
from .routes.main import main_bp
from .routes.admin import admin_bp
from .routes.auth import auth_bp
from .routes.integrations import integrations_bp
from .routes.webhooks import webhooks_bp

from . import models

app = Flask(__name__, template_folder=str(settings.BASE_DIR / "templates"))
app.config["SECRET_KEY"] = settings.SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"] = settings.effective_database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
app.config["SESSION_COOKIE_SECURE"] = settings.SESSION_COOKIE_SECURE

@app.context_processor
def inject_taxonomy_helpers():
    from .integrations.taxonomy import get_taxonomy_name
    return dict(get_taxonomy_name=get_taxonomy_name)
app.config["MAX_CONTENT_LENGTH"] = settings.MAX_CONTENT_LENGTH
app.config["MAX_FORM_MEMORY_SIZE"] = settings.MAX_FORM_MEMORY_SIZE

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

db.init_app(app)
migrate.init_app(app, db)
limiter.init_app(app)

app.register_blueprint(items_bp)
app.register_blueprint(auctions_bp)
app.register_blueprint(exports_bp)
app.register_blueprint(main_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(integrations_bp)
app.register_blueprint(webhooks_bp)


@app.route("/healthz", methods=["GET"])
def healthz():
    return Response("ok\n", mimetype="text/plain")


@app.before_request
def protect_csrf():
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if not app.config.get("WTF_CSRF_ENABLED", True):
        return None
    if request.endpoint == "healthz" or request.path.startswith("/api/"):
        return None
    if validate_csrf_token(request.form.get("csrf_token", "")):
        return None
    abort(400, description="Missing or invalid CSRF token.")


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
        "csrf_token": get_csrf_token(),
        "csrf_input": csrf_input,
    }


@app.route("/api/etsy/taxonomy/search")
def api_etsy_taxonomy_search():
    from .integrations.taxonomy import search_taxonomy
    query = request.args.get("q", "")
    if len(query) < 2:
        return {"results": []}
    return {"results": search_taxonomy(query)}

@app.route("/api/etsy/taxonomy/resolve/<int:taxonomy_id>")
def api_etsy_taxonomy_resolve(taxonomy_id):
    from .integrations.taxonomy import get_taxonomy_name
    return {"name": get_taxonomy_name(taxonomy_id)}

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    app.run(host=host, port=port, debug=debug)
