from __future__ import annotations

import csv
import errno
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename
from auctionninja_generator import AuctionNinjaGenerator
from ftp_client import (
    delete_lot_photos_from_auctionninja,
    upload_lot_photos_to_auctionninja,
)
from image_processor import ALLOWED_EXTENSIONS, HEIF_SUPPORT_ENABLED, optimize_image

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
EXPORTS_DIR = DATA_DIR / "exports"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

generator = AuctionNinjaGenerator()

CSV_HEADER = [
    "Lot Number",
    "Lead",
    "Description",
    "Condition notes",
    "Low Estimate ($)",
    "High Estimate ($)",
    "Dimensions - Length",
    "Dimensions - Depth",
    "Dimensions - Height",
    "Tags",
    "Reference #",
    "Item Notes",
    "Consigner #",
    "Shipping Available",
    "Category",
]
DEFAULT_STARTING_LOT = 0
DEFAULT_AUCTION_ID = 4
ITEM_STATUS_READY = "ready"
ITEM_STATUS_PUBLISHED = "published"
ITEM_STATUS_NEEDS_UPDATE = "needs_update"
ITEM_STATUS_REMOVED = "removed"
AUCTION_STATUS_PREPARING = "preparing"
AUCTION_STATUS_ACTIVE = "active"
AUCTION_STATUS_COMPLETED = "completed"
AUCTION_STATUSES = {
    AUCTION_STATUS_PREPARING,
    AUCTION_STATUS_ACTIVE,
    AUCTION_STATUS_COMPLETED,
}
EXPORTABLE_STATUSES = {ITEM_STATUS_READY, ITEM_STATUS_NEEDS_UPDATE, ITEM_STATUS_PUBLISHED}
MANAGE_ITEM_FILTERS = {
    "active": {ITEM_STATUS_READY, ITEM_STATUS_PUBLISHED, ITEM_STATUS_NEEDS_UPDATE},
    ITEM_STATUS_READY: {ITEM_STATUS_READY},
    ITEM_STATUS_PUBLISHED: {ITEM_STATUS_PUBLISHED},
    ITEM_STATUS_NEEDS_UPDATE: {ITEM_STATUS_NEEDS_UPDATE},
    ITEM_STATUS_REMOVED: {ITEM_STATUS_REMOVED},
    "all": {
        ITEM_STATUS_READY,
        ITEM_STATUS_PUBLISHED,
        ITEM_STATUS_NEEDS_UPDATE,
        ITEM_STATUS_REMOVED,
    },
}

DEFAULT_CATEGORIES = [
    "Jewelry",
    "Art",
    "Decorative Arts",
    "Pottery & Glass",
    "Collectibles",
    "Fashion Accessories",
    "Books & Ephemera",
    "Toys",
    "Religious",
    "Household",
    "Furniture",
    "Electronics",
    "Tools",
    "Other",
]

def render_edit_page(
    temp_id: str,
    saved_files: list[Path],
    seller_notes: str,
    options: list[dict],
    form: dict[str, str],
    revision_request: str = "",
):
    set_active_draft(
        temp_id=temp_id,
        seller_notes=seller_notes,
        options=options,
        form=form,
        revision_request=revision_request,
    )
    return render_template(
        "edit.html",
        temp_id=temp_id,
        image_files=[p.name for p in saved_files],
        image_url_prefix=f"/uploads/{temp_id}/",
        next_lot=get_next_lot_preview(),
        categories=DEFAULT_CATEGORIES,
        seller_notes=seller_notes,
        revision_request=revision_request,
        options=options,
        form=form,
    )


def set_active_draft(
    temp_id: str,
    seller_notes: str,
    options: list[dict],
    form: dict[str, str],
    revision_request: str = "",
) -> None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    options_json = json.dumps(options)
    form_json = json.dumps(form)
    slot_name = f"auction:{get_current_auction_id()}"

    try:
        cursor = connection.cursor()
        if dialect == "sqlite":
            cursor.execute(
                """
                INSERT OR REPLACE INTO active_drafts (
                    slot_name,
                    temp_id,
                    seller_notes,
                    options_json,
                    form_json,
                    revision_request,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (slot_name, temp_id, seller_notes, options_json, form_json, revision_request),
            )
        else:
            cursor.execute(
                """
                INSERT INTO active_drafts (
                    slot_name,
                    temp_id,
                    seller_notes,
                    options_json,
                    form_json,
                    revision_request
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    temp_id = VALUES(temp_id),
                    seller_notes = VALUES(seller_notes),
                    options_json = VALUES(options_json),
                    form_json = VALUES(form_json),
                    revision_request = VALUES(revision_request)
                """,
                (slot_name, temp_id, seller_notes, options_json, form_json, revision_request),
            )
        connection.commit()
    finally:
        connection.close()


def clear_active_draft(temp_id: str | None = None) -> None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    slot_name = f"auction:{get_current_auction_id()}"

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        if temp_id:
            cursor.execute(
                f"SELECT temp_id FROM active_drafts WHERE slot_name = {placeholder}",
                (slot_name,),
            )
            record = cursor.fetchone()
            current_temp_id = ""
            if record:
                if isinstance(record, sqlite3.Row):
                    current_temp_id = str(record["temp_id"])
                elif isinstance(record, dict):
                    current_temp_id = str(record.get("temp_id", ""))
                else:
                    current_temp_id = str(record[0])
            if current_temp_id != temp_id:
                return
        cursor.execute(
            f"DELETE FROM active_drafts WHERE slot_name = {placeholder}",
            (slot_name,),
        )
        connection.commit()
    finally:
        connection.close()


def get_active_draft() -> dict[str, Any] | None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    slot_name = f"auction:{get_current_auction_id()}"

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        cursor.execute(
            f"""
            SELECT temp_id, seller_notes, options_json, form_json, revision_request
            FROM active_drafts
            WHERE slot_name = {placeholder}
            """,
            (slot_name,),
        )
        record = cursor.fetchone()
    finally:
        connection.close()

    if not record:
        return None

    if isinstance(record, sqlite3.Row):
        raw = {key: record[key] for key in record.keys()}
    elif isinstance(record, dict):
        raw = dict(record)
    else:
        return None

    temp_id = str(raw.get("temp_id", "")).strip()
    if not temp_id:
        return None

    try:
        options = json.loads(str(raw.get("options_json", "[]")))
        form = json.loads(str(raw.get("form_json", "{}")))
    except json.JSONDecodeError:
        clear_active_draft(temp_id=temp_id)
        return None

    if not isinstance(options, list) or not isinstance(form, dict):
        clear_active_draft(temp_id=temp_id)
        return None

    saved_files = load_saved_files_for_temp_id(temp_id)
    if not saved_files:
        clear_active_draft(temp_id=temp_id)
        return None

    return {
        "temp_id": temp_id,
        "seller_notes": str(raw.get("seller_notes", "")).strip(),
        "options": options,
        "form": form,
        "revision_request": str(raw.get("revision_request", "")).strip(),
        "image_files": [p.name for p in saved_files],
        "image_count": len(saved_files),
    }


def current_edit_context(
    temp_id: str,
    seller_notes: str,
) -> tuple[list[Path], list[dict], dict[str, str]]:
    saved_files = load_saved_files_for_temp_id(temp_id)
    options = options_from_request()
    form = form_from_request(seller_notes=seller_notes)
    return saved_files, options, form


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{DATA_DIR / 'auction_items.db'}"
    return url


def auth_enabled() -> bool:
    return bool(os.getenv("APP_LOGIN_PASSWORD", "").strip())


def auth_username() -> str:
    return os.getenv("APP_LOGIN_USERNAME", "admin").strip() or "admin"


def auth_password() -> str:
    return os.getenv("APP_LOGIN_PASSWORD", "").strip()


def is_authenticated() -> bool:
    return bool(session.get("authenticated"))


def default_auction_id() -> int:
    raw_value = os.getenv("CURRENT_AUCTION_ID", "").strip() or os.getenv("AUCTION_NUMBER", "").strip()
    if raw_value.isdigit():
        return int(raw_value)
    return DEFAULT_AUCTION_ID


def _extract_row_value(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, sqlite3.Row):
        return row[key]
    if isinstance(row, dict):
        return row.get(key, default)
    if isinstance(row, (list, tuple)) and len(row) > index:
        return row[index]
    return default


def connect_item_store():
    database_url = get_database_url()
    if not database_url:
        return None, "csv"

    parsed = urlparse(database_url)
    scheme = parsed.scheme.lower()

    if scheme.startswith("sqlite"):
        if database_url.startswith("sqlite:///"):
            db_path = unquote(database_url[10:])
        else:
            db_path = unquote(parsed.netloc + parsed.path)

        if not db_path:
            raise ValueError("DATABASE_URL sqlite path is missing.")

        db_file = Path(db_path)
        if db_file.parent and str(db_file.parent) != ".":
            db_file.parent.mkdir(parents=True, exist_ok=True)

        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection, "sqlite"

    if scheme in {"mysql", "mysql+pymysql"}:
        try:
            import pymysql
        except ImportError as exc:
            raise RuntimeError(
                "PyMySQL is required for MySQL storage. Install requirements.txt again."
            ) from exc

        query = parse_qs(parsed.query)
        connection = pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=(parsed.path or "").lstrip("/"),
            charset=query.get("charset", ["utf8mb4"])[0],
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
        return connection, "mysql"

    raise ValueError("DATABASE_URL must use sqlite:/// or mysql:// syntax.")


def ensure_item_store_ready() -> None:
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        if dialect == "sqlite":
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auctions (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 0,
                    last_lot_override INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            _ensure_sqlite_column(cursor, "auctions", "last_lot_override", "INTEGER")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auction_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    auction_id INTEGER,
                    lot_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    condition_notes TEXT NOT NULL,
                    low_estimate TEXT NOT NULL,
                    high_estimate TEXT NOT NULL,
                    dimensions_length TEXT NOT NULL,
                    dimensions_depth TEXT NOT NULL,
                    dimensions_height TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    reference_number TEXT NOT NULL,
                    item_notes TEXT NOT NULL,
                    consigner_number TEXT NOT NULL,
                    shipping_available TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready',
                    image_folder TEXT NOT NULL,
                    last_export_batch TEXT,
                    published_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(auction_id, lot_number)
                )
                """
            )
            _ensure_sqlite_column(cursor, "auction_items", "auction_id", "INTEGER")
            _ensure_sqlite_column(cursor, "auction_items", "last_export_batch", "TEXT")
            _ensure_sqlite_column(cursor, "auction_items", "published_at", "TEXT")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS export_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    auction_id INTEGER,
                    filename TEXT NOT NULL UNIQUE,
                    export_type TEXT NOT NULL,
                    lot_numbers TEXT NOT NULL,
                    lot_count INTEGER NOT NULL,
                    archive_path TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            _ensure_sqlite_column(cursor, "export_batches", "auction_id", "INTEGER")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ftp_uploads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lot_number INTEGER NOT NULL,
                    auction_id INTEGER,
                    auction_number TEXT NOT NULL,
                    auction_photo_index INTEGER NOT NULL,
                    remote_names TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(auction_id, lot_number)
                )
                """
            )
            _ensure_sqlite_column(cursor, "ftp_uploads", "auction_id", "INTEGER")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auction_photo_counters (
                    auction_number TEXT PRIMARY KEY,
                    last_index INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS active_drafts (
                    slot_name TEXT PRIMARY KEY,
                    temp_id TEXT NOT NULL,
                    seller_notes TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    form_json TEXT NOT NULL,
                    revision_request TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            _bootstrap_auction_rows(cursor, dialect)
            _backfill_auction_scope(cursor, dialect)
        else:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auctions (
                    id INT PRIMARY KEY,
                    status VARCHAR(32) NOT NULL,
                    is_current TINYINT(1) NOT NULL DEFAULT 0,
                    last_lot_override INT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )
            _ensure_mysql_column(cursor, "auctions", "last_lot_override", "INT NULL")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auction_items (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    auction_id INT NULL,
                    lot_number INT NOT NULL,
                    title TEXT NOT NULL,
                    description LONGTEXT NOT NULL,
                    condition_notes TEXT NOT NULL,
                    low_estimate VARCHAR(255) NOT NULL,
                    high_estimate VARCHAR(255) NOT NULL,
                    dimensions_length VARCHAR(255) NOT NULL,
                    dimensions_depth VARCHAR(255) NOT NULL,
                    dimensions_height VARCHAR(255) NOT NULL,
                    tags TEXT NOT NULL,
                    reference_number VARCHAR(255) NOT NULL,
                    item_notes LONGTEXT NOT NULL,
                    consigner_number VARCHAR(255) NOT NULL,
                    shipping_available VARCHAR(32) NOT NULL,
                    category VARCHAR(255) NOT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'ready',
                    image_folder VARCHAR(255) NOT NULL,
                    last_export_batch VARCHAR(255) NULL,
                    published_at TIMESTAMP NULL DEFAULT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_auction_lot (auction_id, lot_number)
                )
                """
            )
            _ensure_mysql_column(cursor, "auction_items", "auction_id", "INT NULL")
            _ensure_mysql_column(cursor, "auction_items", "last_export_batch", "VARCHAR(255) NULL")
            _ensure_mysql_column(cursor, "auction_items", "published_at", "TIMESTAMP NULL DEFAULT NULL")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS export_batches (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    auction_id INT NULL,
                    filename VARCHAR(255) NOT NULL UNIQUE,
                    export_type VARCHAR(64) NOT NULL,
                    lot_numbers TEXT NOT NULL,
                    lot_count INT NOT NULL,
                    archive_path VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            _ensure_mysql_column(cursor, "export_batches", "auction_id", "INT NULL")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ftp_uploads (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    lot_number INT NOT NULL,
                    auction_id INT NULL,
                    auction_number VARCHAR(255) NOT NULL,
                    auction_photo_index INT NOT NULL,
                    remote_names TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_auction_ftp_lot (auction_id, lot_number)
                )
                """
            )
            _ensure_mysql_column(cursor, "ftp_uploads", "auction_id", "INT NULL")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auction_photo_counters (
                    auction_number VARCHAR(255) PRIMARY KEY,
                    last_index INT NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS active_drafts (
                    slot_name VARCHAR(64) PRIMARY KEY,
                    temp_id VARCHAR(255) NOT NULL,
                    seller_notes TEXT NOT NULL,
                    options_json LONGTEXT NOT NULL,
                    form_json LONGTEXT NOT NULL,
                    revision_request TEXT NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )
            _bootstrap_auction_rows(cursor, dialect)
            _backfill_auction_scope(cursor, dialect)
        connection.commit()
    finally:
        connection.close()


def _ensure_sqlite_column(cursor, table_name: str, column_name: str, definition: str) -> None:
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = {row[1] for row in cursor.fetchall()}
    if column_name not in columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _ensure_mysql_column(cursor, table_name: str, column_name: str, definition: str) -> None:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    if cursor.fetchone() is None:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _bootstrap_auction_rows(cursor, dialect: str) -> None:
    cursor.execute("SELECT COUNT(*) AS auction_count FROM auctions")
    auction_count = int(_extract_row_value(cursor.fetchone(), "auction_count", 0, 0) or 0)
    if auction_count == 0:
        starting_auction_id = default_auction_id()
        if dialect == "sqlite":
            cursor.execute(
                """
                INSERT INTO auctions (id, status, is_current, created_at, updated_at)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (starting_auction_id, AUCTION_STATUS_ACTIVE),
            )
        else:
            cursor.execute(
                """
                INSERT INTO auctions (id, status, is_current)
                VALUES (%s, %s, 1)
                """,
                (starting_auction_id, AUCTION_STATUS_ACTIVE),
            )
        return

    cursor.execute("SELECT COUNT(*) AS current_count FROM auctions WHERE is_current = 1")
    current_count = int(_extract_row_value(cursor.fetchone(), "current_count", 0, 0) or 0)
    if current_count > 0:
        return

    cursor.execute("SELECT MAX(id) AS latest_id FROM auctions")
    latest_id = int(_extract_row_value(cursor.fetchone(), "latest_id", 0, default_auction_id()) or default_auction_id())
    placeholder = "?" if dialect == "sqlite" else "%s"
    if dialect == "sqlite":
        cursor.execute("UPDATE auctions SET is_current = 0")
        cursor.execute(
            f"""
            UPDATE auctions
            SET is_current = 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
            """,
            (latest_id,),
        )
    else:
        cursor.execute("UPDATE auctions SET is_current = 0")
        cursor.execute(
            f"""
            UPDATE auctions
            SET is_current = 1
            WHERE id = {placeholder}
            """,
            (latest_id,),
        )


def _backfill_auction_scope(cursor, dialect: str) -> None:
    default_id = default_auction_id()
    if dialect == "sqlite":
        cursor.execute(
            """
            UPDATE auction_items
            SET auction_id = ?
            WHERE auction_id IS NULL
            """,
            (default_id,),
        )
        cursor.execute(
            """
            UPDATE export_batches
            SET auction_id = ?
            WHERE auction_id IS NULL
            """,
            (default_id,),
        )
        cursor.execute(
            """
            UPDATE ftp_uploads
            SET auction_id = ?
            WHERE auction_id IS NULL
            """,
            (default_id,),
        )
    else:
        cursor.execute(
            """
            UPDATE auction_items
            SET auction_id = %s
            WHERE auction_id IS NULL
            """,
            (default_id,),
        )
        cursor.execute(
            """
            UPDATE export_batches
            SET auction_id = %s
            WHERE auction_id IS NULL
            """,
            (default_id,),
        )
        cursor.execute(
            """
            UPDATE ftp_uploads
            SET auction_id = %s
            WHERE auction_id IS NULL
            """,
            (default_id,),
        )


def get_current_auction() -> dict[str, str] | None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT id, status, is_current, created_at, updated_at
            FROM auctions
            WHERE is_current = 1
            ORDER BY id DESC
            LIMIT 1
            """
        )
        record = cursor.fetchone()
        if not record:
            cursor.execute(
                """
                SELECT id, status, is_current, created_at, updated_at
                FROM auctions
                ORDER BY id DESC
                LIMIT 1
                """
            )
            record = cursor.fetchone()
    finally:
        connection.close()

    if not record:
        return None

    if isinstance(record, sqlite3.Row):
        return {key: "" if record[key] is None else str(record[key]) for key in record.keys()}
    if isinstance(record, dict):
        return {key: "" if value is None else str(value) for key, value in record.items()}
    return None


def get_current_auction_id() -> int:
    auction = get_current_auction()
    if not auction:
        return default_auction_id()
    return int(auction.get("id", default_auction_id()))


def list_auctions() -> list[dict[str, str]]:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT id, status, is_current, created_at, updated_at
            FROM auctions
            ORDER BY id DESC
            """
        )
        records = cursor.fetchall()
    finally:
        connection.close()

    auctions: list[dict[str, str]] = []
    for record in records:
        if isinstance(record, sqlite3.Row):
            auctions.append({key: "" if record[key] is None else str(record[key]) for key in record.keys()})
        elif isinstance(record, dict):
            auctions.append({key: "" if value is None else str(value) for key, value in record.items()})
    return auctions


def fetch_auction_summaries() -> list[dict[str, str]]:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT id, status, is_current, created_at, updated_at
            FROM auctions
            ORDER BY id DESC
            """
        )
        auctions = cursor.fetchall()

        summaries: list[dict[str, str]] = []
        placeholder = "?" if dialect == "sqlite" else "%s"

        for auction in auctions:
            if isinstance(auction, sqlite3.Row):
                row = {key: "" if auction[key] is None else str(auction[key]) for key in auction.keys()}
            elif isinstance(auction, dict):
                row = {key: "" if value is None else str(value) for key, value in auction.items()}
            else:
                continue

            auction_id = int(row["id"])
            cursor.execute(
                f"""
                SELECT status, COUNT(*) AS item_count
                FROM auction_items
                WHERE auction_id = {placeholder}
                GROUP BY status
                """,
                (auction_id,),
            )
            item_counts = {
                ITEM_STATUS_READY: 0,
                ITEM_STATUS_PUBLISHED: 0,
                ITEM_STATUS_NEEDS_UPDATE: 0,
                ITEM_STATUS_REMOVED: 0,
            }
            for item_row in cursor.fetchall():
                status = str(_extract_row_value(item_row, "status", 0, ""))
                count = int(_extract_row_value(item_row, "item_count", 1, 0) or 0)
                if status in item_counts:
                    item_counts[status] = count

            cursor.execute(
                f"""
                SELECT COUNT(*) AS export_count
                FROM export_batches
                WHERE auction_id = {placeholder}
                """,
                (auction_id,),
            )
            export_count = int(_extract_row_value(cursor.fetchone(), "export_count", 0, 0) or 0)

            summaries.append(
                {
                    **row,
                    "ready_count": str(item_counts[ITEM_STATUS_READY]),
                    "published_count": str(item_counts[ITEM_STATUS_PUBLISHED]),
                    "needs_update_count": str(item_counts[ITEM_STATUS_NEEDS_UPDATE]),
                    "removed_count": str(item_counts[ITEM_STATUS_REMOVED]),
                    "total_count": str(sum(item_counts.values())),
                    "export_count": str(export_count),
                }
            )

        return summaries
    finally:
        connection.close()


def create_next_auction() -> dict[str, str]:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        cursor.execute("SELECT MAX(id) AS latest_id FROM auctions")
        latest_id = int(_extract_row_value(cursor.fetchone(), "latest_id", 0, default_auction_id()) or default_auction_id())
        next_id = latest_id + 1
        if dialect == "sqlite":
            cursor.execute("UPDATE auctions SET is_current = 0")
            cursor.execute(
                """
                INSERT INTO auctions (id, status, is_current, created_at, updated_at)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (next_id, AUCTION_STATUS_PREPARING),
            )
        else:
            cursor.execute("UPDATE auctions SET is_current = 0")
            cursor.execute(
                """
                INSERT INTO auctions (id, status, is_current)
                VALUES (%s, %s, 1)
                """,
                (next_id, AUCTION_STATUS_PREPARING),
            )
        connection.commit()
    finally:
        connection.close()

    auction = get_current_auction()
    if not auction:
        raise RuntimeError("Failed to create the next auction.")
    return auction


def switch_current_auction(auction_id: int) -> bool:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        cursor.execute(f"SELECT id FROM auctions WHERE id = {placeholder}", (auction_id,))
        if not cursor.fetchone():
            return False
        if dialect == "sqlite":
            cursor.execute("UPDATE auctions SET is_current = 0")
            cursor.execute(
                f"""
                UPDATE auctions
                SET is_current = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = {placeholder}
                """,
                (auction_id,),
            )
        else:
            cursor.execute("UPDATE auctions SET is_current = 0")
            cursor.execute(
                f"""
                UPDATE auctions
                SET is_current = 1
                WHERE id = {placeholder}
                """,
                (auction_id,),
            )
        connection.commit()
        return True
    finally:
        connection.close()


def update_auction_status(auction_id: int, status: str) -> bool:
    if status not in AUCTION_STATUSES:
        return False

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholders = ("?", "?") if dialect == "sqlite" else ("%s", "%s")
        if dialect == "sqlite":
            cursor.execute(
                f"""
                UPDATE auctions
                SET status = {placeholders[0]}, updated_at = CURRENT_TIMESTAMP
                WHERE id = {placeholders[1]}
                """,
                (status, auction_id),
            )
        else:
            cursor.execute(
                f"""
                UPDATE auctions
                SET status = {placeholders[0]}
                WHERE id = {placeholders[1]}
                """,
                (status, auction_id),
            )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def move_item_to_auction(lot_number: int, target_auction_id: int) -> bool:
    existing_item = fetch_saved_item(lot_number)
    if not existing_item:
        return False
    if str(existing_item.get("auction_id", "")).isdigit() and int(existing_item["auction_id"]) == target_auction_id:
        return True

    current_auction_id = get_current_auction_id()
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        cursor.execute(f"SELECT id FROM auctions WHERE id = {placeholder}", (target_auction_id,))
        if not cursor.fetchone():
            return False

        if dialect == "sqlite":
            cursor.execute(
                """
                UPDATE auction_items
                SET
                    auction_id = ?,
                    status = ?,
                    last_export_batch = '',
                    published_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE lot_number = ? AND auction_id = ?
                """,
                (target_auction_id, ITEM_STATUS_READY, lot_number, current_auction_id),
            )
            cursor.execute(
                """
                UPDATE ftp_uploads
                SET auction_id = ?
                WHERE lot_number = ? AND auction_id = ?
                """,
                (target_auction_id, lot_number, current_auction_id),
            )
        else:
            cursor.execute(
                """
                UPDATE auction_items
                SET
                    auction_id = %s,
                    status = %s,
                    last_export_batch = '',
                    published_at = NULL
                WHERE lot_number = %s AND auction_id = %s
                """,
                (target_auction_id, ITEM_STATUS_READY, lot_number, current_auction_id),
            )
            cursor.execute(
                """
                UPDATE ftp_uploads
                SET auction_id = %s
                WHERE lot_number = %s AND auction_id = %s
                """,
                (target_auction_id, lot_number, current_auction_id),
            )
        connection.commit()
        return True
    finally:
        connection.close()


def fetch_last_lot_from_store() -> int:
    current_auction_id = get_current_auction_id()
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"

        cursor.execute(f"SELECT last_lot_override FROM auctions WHERE id = {placeholder}", (current_auction_id,))
        row_override = cursor.fetchone()
        override_lot = 0
        if row_override and row_override[0] is not None:
            override_lot = int(row_override[0])

        cursor.execute(f"SELECT MAX(lot_number) AS max_lot FROM auction_items WHERE auction_id = {placeholder}", (current_auction_id,))
        row_max = cursor.fetchone()
        max_lot = 0
        if row_max and row_max[0] is not None:
            max_lot = int(row_max[0])
    finally:
        connection.close()

    return max(override_lot, max_lot, DEFAULT_STARTING_LOT)


def item_record_from_form(lot_number: int, form: dict[str, str], image_folder: str) -> dict[str, str]:
    return {
        "auction_id": str(get_current_auction_id()),
        "lot_number": str(lot_number),
        "title": form["Title"],
        "description": form["Description"],
        "condition_notes": form["Condition Summary"],
        "low_estimate": form["Low Estimate ($)"],
        "high_estimate": form["High Estimate ($)"],
        "dimensions_length": form["Dimensions - Length"],
        "dimensions_depth": form["Dimensions - Depth"],
        "dimensions_height": form["Dimensions - Height"],
        "tags": form["Keywords"],
        "reference_number": form["Reference #"],
        "item_notes": combine_item_notes(form),
        "consigner_number": form["Consigner #"],
        "shipping_available": form["Shipping Available"],
        "category": form["Category"],
        "status": ITEM_STATUS_READY,
        "image_folder": image_folder,
        "last_export_batch": "",
        "published_at": "",
    }


def append_item_record(record: dict[str, str]) -> None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        auction_id = int(record.get("auction_id", get_current_auction_id()))
        placeholders = ", ".join(["?"] * 18) if dialect == "sqlite" else ", ".join(["%s"] * 18)
        cursor.execute(
            f"""
            INSERT INTO auction_items (
                auction_id,
                lot_number,
                title,
                description,
                condition_notes,
                low_estimate,
                high_estimate,
                dimensions_length,
                dimensions_depth,
                dimensions_height,
                tags,
                reference_number,
                item_notes,
                consigner_number,
                shipping_available,
                category,
                status,
                image_folder
            ) VALUES ({placeholders})
            """,
            (
                auction_id,
                int(record["lot_number"]),
                record["title"],
                record["description"],
                record["condition_notes"],
                record["low_estimate"],
                record["high_estimate"],
                record["dimensions_length"],
                record["dimensions_depth"],
                record["dimensions_height"],
                record["tags"],
                record["reference_number"],
                record["item_notes"],
                record["consigner_number"],
                record["shipping_available"],
                record["category"],
                record["status"],
                record["image_folder"],
            ),
        )
        connection.commit()
    finally:
        connection.close()


def mark_lots_as_published(lot_numbers: list[int], export_batch_name: str) -> None:
    if not lot_numbers:
        return

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    current_auction_id = get_current_auction_id()

    try:
        cursor = connection.cursor()
        placeholders = ", ".join(["?"] * len(lot_numbers)) if dialect == "sqlite" else ", ".join(["%s"] * len(lot_numbers))
        params: tuple[object, ...]
        if dialect == "sqlite":
            params = (
                ITEM_STATUS_PUBLISHED,
                export_batch_name,
                *lot_numbers,
            )
            cursor.execute(
                f"""
                UPDATE auction_items
                SET
                    status = ?,
                    last_export_batch = ?,
                    published_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE auction_id = ?
                  AND lot_number IN ({placeholders})
                """,
                (ITEM_STATUS_PUBLISHED, export_batch_name, current_auction_id, *lot_numbers),
            )
        else:
            cursor.execute(
                f"""
                UPDATE auction_items
                SET
                    status = %s,
                    last_export_batch = %s,
                    published_at = CURRENT_TIMESTAMP
                WHERE auction_id = %s
                  AND lot_number IN ({placeholders})
                """,
                (ITEM_STATUS_PUBLISHED, export_batch_name, current_auction_id, *lot_numbers),
            )
        connection.commit()
    finally:
        connection.close()


def fetch_export_rows() -> list[list[str]]:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    current_auction_id = get_current_auction_id()

    try:
        cursor = connection.cursor()
        cursor.execute(
            f"""
            SELECT
                lot_number,
                title,
                description,
                condition_notes,
                low_estimate,
                high_estimate,
                dimensions_length,
                dimensions_depth,
                dimensions_height,
                tags,
                reference_number,
                item_notes,
                consigner_number,
                shipping_available,
                category
            FROM auction_items
            WHERE auction_id = {("?" if dialect == "sqlite" else "%s")}
              AND status != 'removed'
            ORDER BY lot_number
            """,
            (current_auction_id,),
        )
        records = cursor.fetchall()
    finally:
        connection.close()

    rows: list[list[str]] = []
    for record in records:
        if isinstance(record, sqlite3.Row):
            values = [record[key] for key in record.keys()]
        elif isinstance(record, dict):
            values = [record[key] for key in record.keys()]
        else:
            values = list(record)
        rows.append(["" if value is None else str(value) for value in values])
    return rows


def normalize_manage_filter(raw_filter: str) -> str:
    value = (raw_filter or "").strip().lower()
    if value in MANAGE_ITEM_FILTERS:
        return value
    return "active"


def fetch_manage_items(status_filter: str = "active") -> list[dict[str, str]]:
    normalized_filter = normalize_manage_filter(status_filter)
    statuses = MANAGE_ITEM_FILTERS[normalized_filter]

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    current_auction_id = get_current_auction_id()

    try:
        cursor = connection.cursor()
        placeholders = ", ".join(["?"] * len(statuses)) if dialect == "sqlite" else ", ".join(["%s"] * len(statuses))
        cursor.execute(
            f"""
            SELECT
                lot_number,
                title,
                category,
                shipping_available,
                status,
                image_folder,
                created_at,
                updated_at,
                published_at,
                last_export_batch
            FROM auction_items
            WHERE auction_id = {("?" if dialect == "sqlite" else "%s")}
              AND status IN ({placeholders})
            ORDER BY lot_number
            """,
            (current_auction_id, *tuple(statuses)),
        )
        records = cursor.fetchall()
    finally:
        connection.close()

    items: list[dict[str, str]] = []
    for record in records:
        if isinstance(record, sqlite3.Row):
            item = {key: "" if record[key] is None else str(record[key]) for key in record.keys()}
        elif isinstance(record, dict):
            item = {key: "" if value is None else str(value) for key, value in record.items()}
        else:
            continue
        items.append(item)
    return items


def fetch_manage_item_counts() -> dict[str, int]:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    current_auction_id = get_current_auction_id()

    try:
        cursor = connection.cursor()
        cursor.execute(
            f"""
            SELECT status, COUNT(*) AS item_count
            FROM auction_items
            WHERE auction_id = {("?" if dialect == "sqlite" else "%s")}
            GROUP BY status
            """,
            (current_auction_id,),
        )
        records = cursor.fetchall()
    finally:
        connection.close()

    counts = {
        ITEM_STATUS_READY: 0,
        ITEM_STATUS_PUBLISHED: 0,
        ITEM_STATUS_NEEDS_UPDATE: 0,
        ITEM_STATUS_REMOVED: 0,
    }
    for record in records:
        if isinstance(record, sqlite3.Row):
            status = str(record["status"])
            count = int(record["item_count"])
        elif isinstance(record, dict):
            status = str(record.get("status", ""))
            count = int(record.get("item_count", 0))
        else:
            status = str(record[0])
            count = int(record[1])
        if status in counts:
            counts[status] = count

    return {
        "active": counts[ITEM_STATUS_READY] + counts[ITEM_STATUS_PUBLISHED] + counts[ITEM_STATUS_NEEDS_UPDATE],
        ITEM_STATUS_READY: counts[ITEM_STATUS_READY],
        ITEM_STATUS_PUBLISHED: counts[ITEM_STATUS_PUBLISHED],
        ITEM_STATUS_NEEDS_UPDATE: counts[ITEM_STATUS_NEEDS_UPDATE],
        ITEM_STATUS_REMOVED: counts[ITEM_STATUS_REMOVED],
        "all": sum(counts.values()),
    }


def fetch_dashboard_items(statuses: list[str], limit: int = 5) -> list[dict[str, str]]:
    if not statuses:
        return []

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    current_auction_id = get_current_auction_id()

    try:
        cursor = connection.cursor()
        placeholders = ", ".join(["?"] * len(statuses)) if dialect == "sqlite" else ", ".join(["%s"] * len(statuses))
        limit_placeholder = "?" if dialect == "sqlite" else "%s"
        cursor.execute(
            f"""
            SELECT
                lot_number,
                title,
                status,
                category,
                updated_at,
                published_at
            FROM auction_items
            WHERE auction_id = {("?" if dialect == "sqlite" else "%s")}
              AND status IN ({placeholders})
            ORDER BY updated_at DESC, lot_number DESC
            LIMIT {limit_placeholder}
            """,
            (current_auction_id, *tuple(statuses), limit),
        )
        records = cursor.fetchall()
    finally:
        connection.close()

    items: list[dict[str, str]] = []
    for record in records:
        if isinstance(record, sqlite3.Row):
            item = {key: "" if record[key] is None else str(record[key]) for key in record.keys()}
        elif isinstance(record, dict):
            item = {key: "" if value is None else str(value) for key, value in record.items()}
        else:
            continue
        items.append(item)
    return items


def fetch_saved_item(lot_number: int) -> dict[str, str] | None:
    current_auction_id = get_current_auction_id()

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        cursor.execute(
            f"""
            SELECT
                lot_number,
                title,
                description,
                condition_notes,
                low_estimate,
                high_estimate,
                dimensions_length,
                dimensions_depth,
                dimensions_height,
                tags,
                reference_number,
                item_notes,
                consigner_number,
                shipping_available,
                category,
                status,
                image_folder,
                created_at,
                updated_at,
                published_at,
                last_export_batch
            FROM auction_items
            WHERE lot_number = {placeholder} 
              AND auction_id = {placeholder}
            """,
            (lot_number, current_auction_id),
        )
        record = cursor.fetchone()
    finally:
        connection.close()

    if not record:
        return None

    if isinstance(record, sqlite3.Row):
        return {key: "" if record[key] is None else str(record[key]) for key in record.keys()}
    if isinstance(record, dict):
        return {key: "" if value is None else str(value) for key, value in record.items()}
    return None


def form_from_saved_item(record: dict[str, str]) -> dict[str, str]:
    return {
        "Title": record.get("title", ""),
        "Description": record.get("description", ""),
        "Condition Summary": record.get("condition_notes", ""),
        "Keywords": record.get("tags", ""),
        "Category": record.get("category", "") or "Other",
        "Low Estimate ($)": record.get("low_estimate", ""),
        "High Estimate ($)": record.get("high_estimate", ""),
        "Dimensions - Length": record.get("dimensions_length", ""),
        "Dimensions - Depth": record.get("dimensions_depth", ""),
        "Dimensions - Height": record.get("dimensions_height", ""),
        "Reference #": record.get("reference_number", ""),
        "Item Notes": record.get("item_notes", ""),
        "Consigner #": record.get("consigner_number", ""),
        "Shipping Available": record.get("shipping_available", "") or "No",
    }


def saved_item_fields_from_form(form: dict[str, str]) -> dict[str, str]:
    return {
        "title": form["Title"],
        "description": form["Description"],
        "condition_notes": form["Condition Summary"],
        "low_estimate": form["Low Estimate ($)"],
        "high_estimate": form["High Estimate ($)"],
        "dimensions_length": form["Dimensions - Length"],
        "dimensions_depth": form["Dimensions - Depth"],
        "dimensions_height": form["Dimensions - Height"],
        "tags": form["Keywords"],
        "reference_number": form["Reference #"],
        "item_notes": combine_item_notes(form),
        "consigner_number": form["Consigner #"],
        "shipping_available": form["Shipping Available"],
        "category": form["Category"],
    }


def determine_updated_status(existing_record: dict[str, str], updated_fields: dict[str, str]) -> str:
    current_status = existing_record.get("status", ITEM_STATUS_READY)
    tracked_fields = [
        "title",
        "description",
        "condition_notes",
        "low_estimate",
        "high_estimate",
        "dimensions_length",
        "dimensions_depth",
        "dimensions_height",
        "tags",
        "reference_number",
        "item_notes",
        "consigner_number",
        "shipping_available",
        "category",
    ]
    changed = any((existing_record.get(field, "") or "") != updated_fields.get(field, "") for field in tracked_fields)

    if current_status == ITEM_STATUS_PUBLISHED and changed:
        return ITEM_STATUS_NEEDS_UPDATE
    return current_status


def update_saved_item_record(lot_number: int, form: dict[str, str]) -> str:
    existing_record = fetch_saved_item(lot_number)
    if not existing_record:
        raise ValueError(f"Lot {lot_number} was not found.")

    updated_fields = saved_item_fields_from_form(form)
    new_status = determine_updated_status(existing_record, updated_fields)

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        if dialect == "sqlite":
            cursor.execute(
                """
                UPDATE auction_items
                SET
                    title = ?,
                    description = ?,
                    condition_notes = ?,
                    low_estimate = ?,
                    high_estimate = ?,
                    dimensions_length = ?,
                    dimensions_depth = ?,
                    dimensions_height = ?,
                    tags = ?,
                    reference_number = ?,
                    item_notes = ?,
                    consigner_number = ?,
                    shipping_available = ?,
                    category = ?,
                    status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE lot_number = ? AND auction_id = ?
                """,
                (
                    updated_fields["title"],
                    updated_fields["description"],
                    updated_fields["condition_notes"],
                    updated_fields["low_estimate"],
                    updated_fields["high_estimate"],
                    updated_fields["dimensions_length"],
                    updated_fields["dimensions_depth"],
                    updated_fields["dimensions_height"],
                    updated_fields["tags"],
                    updated_fields["reference_number"],
                    updated_fields["item_notes"],
                    updated_fields["consigner_number"],
                    updated_fields["shipping_available"],
                    updated_fields["category"],
                    new_status,
                    lot_number,
                    get_current_auction_id(),
                ),
            )
        else:
            cursor.execute(
                """
                UPDATE auction_items
                SET
                    title = %s,
                    description = %s,
                    condition_notes = %s,
                    low_estimate = %s,
                    high_estimate = %s,
                    dimensions_length = %s,
                    dimensions_depth = %s,
                    dimensions_height = %s,
                    tags = %s,
                    reference_number = %s,
                    item_notes = %s,
                    consigner_number = %s,
                    shipping_available = %s,
                    category = %s,
                    status = %s
                WHERE lot_number = %s AND auction_id = %s
                """,
                (
                    updated_fields["title"],
                    updated_fields["description"],
                    updated_fields["condition_notes"],
                    updated_fields["low_estimate"],
                    updated_fields["high_estimate"],
                    updated_fields["dimensions_length"],
                    updated_fields["dimensions_depth"],
                    updated_fields["dimensions_height"],
                    updated_fields["tags"],
                    updated_fields["reference_number"],
                    updated_fields["item_notes"],
                    updated_fields["consigner_number"],
                    updated_fields["shipping_available"],
                    updated_fields["category"],
                    new_status,
                    lot_number,
                    get_current_auction_id(),
                ),
            )
        connection.commit()
    finally:
        connection.close()

    return new_status


def mark_item_removed(lot_number: int) -> bool:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        status_placeholder = "?" if dialect == "sqlite" else "%s"
        current_auction_id = get_current_auction_id()
        if dialect == "sqlite":
            cursor.execute(
                f"""
                UPDATE auction_items
                SET
                    status = {status_placeholder},
                    updated_at = CURRENT_TIMESTAMP
                WHERE lot_number = {placeholder} AND auction_id = {placeholder}
                """,
                (ITEM_STATUS_REMOVED, lot_number, current_auction_id),
            )
        else:
            cursor.execute(
                f"""
                UPDATE auction_items
                SET
                    status = {status_placeholder}
                WHERE lot_number = {placeholder} AND auction_id = {placeholder}
                """,
                (ITEM_STATUS_REMOVED, lot_number, current_auction_id),
            )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def restored_status_for_item(item: dict[str, str]) -> str:
    if item.get("published_at", "").strip():
        return ITEM_STATUS_NEEDS_UPDATE
    return ITEM_STATUS_READY


def restore_removed_item(lot_number: int) -> str | None:
    item = fetch_saved_item(lot_number)
    if not item or item.get("status") != ITEM_STATUS_REMOVED:
        return None

    restored_status = restored_status_for_item(item)

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        status_placeholder = "?" if dialect == "sqlite" else "%s"
        current_auction_id = get_current_auction_id()
        if dialect == "sqlite":
            cursor.execute(
                f"""
                UPDATE auction_items
                SET
                    status = {status_placeholder},
                    updated_at = CURRENT_TIMESTAMP
                WHERE lot_number = {placeholder} AND auction_id = {placeholder}
                """,
                (restored_status, lot_number, current_auction_id),
            )
        else:
            cursor.execute(
                f"""
                UPDATE auction_items
                SET
                    status = {status_placeholder}
                WHERE lot_number = {placeholder} AND auction_id = {placeholder}
                """,
                (restored_status, lot_number, current_auction_id),
            )
        connection.commit()
    finally:
        connection.close()

    return restored_status


def set_items_status(lot_numbers: list[int], target_status: str) -> int:
    if not lot_numbers:
        return 0

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholders = ", ".join(["?"] * len(lot_numbers)) if dialect == "sqlite" else ", ".join(["%s"] * len(lot_numbers))
        current_auction_id = get_current_auction_id()
        if dialect == "sqlite":
            cursor.execute(
                f"""
                UPDATE auction_items
                SET
                    status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE lot_number IN ({placeholders}) AND auction_id = ?
                """,
                (target_status, *lot_numbers, current_auction_id),
            )
        else:
            cursor.execute(
                f"""
                UPDATE auction_items
                SET
                    status = %s
                WHERE lot_number IN ({placeholders}) AND auction_id = %s
                """,
                (target_status, *lot_numbers, current_auction_id),
            )
        connection.commit()
        return int(cursor.rowcount)
    finally:
        connection.close()


def bulk_restore_items(lot_numbers: list[int]) -> int:
    restored = 0
    for lot_number in lot_numbers:
        if restore_removed_item(lot_number):
            restored += 1
    return restored


def fetch_export_rows_for_lots(lot_numbers: list[int]) -> list[list[str]]:
    if not lot_numbers:
        return []

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    current_auction_id = get_current_auction_id()

    try:
        cursor = connection.cursor()
        placeholders = ", ".join(["?"] * len(lot_numbers)) if dialect == "sqlite" else ", ".join(["%s"] * len(lot_numbers))
        cursor.execute(
            f"""
            SELECT
                lot_number,
                title,
                description,
                condition_notes,
                low_estimate,
                high_estimate,
                dimensions_length,
                dimensions_depth,
                dimensions_height,
                tags,
                reference_number,
                item_notes,
                consigner_number,
                shipping_available,
                category
            FROM auction_items
            WHERE auction_id = {("?" if dialect == "sqlite" else "%s")}
              AND status != 'removed'
              AND lot_number IN ({placeholders})
            ORDER BY lot_number
            """,
            (current_auction_id, *tuple(lot_numbers)),
        )
        records = cursor.fetchall()
    finally:
        connection.close()

    rows: list[list[str]] = []
    for record in records:
        if isinstance(record, sqlite3.Row):
            values = [record[key] for key in record.keys()]
        elif isinstance(record, dict):
            values = [record[key] for key in record.keys()]
        else:
            values = list(record)
        rows.append(["" if value is None else str(value) for value in values])
    return rows


def lot_numbers_from_rows(rows: list[list[str]]) -> list[int]:
    lot_numbers: list[int] = []
    for row in rows:
        if row and str(row[0]).isdigit():
            lot_numbers.append(int(row[0]))
    return lot_numbers

def get_last_lot() -> int:
    return fetch_last_lot_from_store()


def get_next_lot_preview() -> int:
    candidate = get_last_lot() + 1
    while fetch_saved_item(candidate) is not None:
        candidate += 1
    return candidate


def reserve_next_lot() -> int:
    candidate = fetch_last_lot_from_store() + 1
    while fetch_saved_item(candidate) is not None:
        candidate += 1
    return candidate


def slugify_title(title: str, max_length: int = 80) -> str:
    title = title.lower().strip()
    title = re.sub(r"[^a-z0-9]+", " ", title)
    title = re.sub(r"\s+", "-", title).strip("-")
    if len(title) > max_length:
        title = title[:max_length].rstrip("-")
    return title or "item"


def make_unique_dir(base_dir: Path, name: str) -> Path:
    target = base_dir / name
    counter = 1
    while target.exists():
        target = base_dir / f"{name}-{counter}"
        counter += 1
    return target


def save_uploaded_files(uploaded_files) -> tuple[str, list[Path]]:
    temp_id = uuid.uuid4().hex
    temp_dir = UPLOADS_DIR / temp_id
    temp_dir.mkdir(parents=True, exist_ok=True)

    saved_files = save_uploaded_files_to_dir(uploaded_files, temp_dir)
    return temp_id, saved_files


def save_uploaded_files_to_dir(uploaded_files, temp_dir: Path) -> list[Path]:
    saved_files: list[Path] = []

    for uploaded in uploaded_files:
        app.logger.info(
            "Processing upload: filename=%r content_type=%r",
            uploaded.filename,
            uploaded.content_type,
        )

        if not uploaded or not uploaded.filename:
            continue

        original_name = secure_filename(uploaded.filename)
        if not original_name:
            continue

        suffix = Path(original_name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            if suffix in {".heic", ".heif"} and not HEIF_SUPPORT_ENABLED:
                flash(
                    "HEIC/HEIF upload is not available on this server yet. "
                    "Install pi-heif (preferred on Raspberry Pi) or convert the photo to JPG first."
                )
            app.logger.warning("Unsupported extension skipped: %s", suffix)
            continue

        raw_destination = temp_dir / original_name
        counter = 1
        while raw_destination.exists():
            raw_destination = temp_dir / f"{Path(original_name).stem}_{counter}{suffix}"
            counter += 1

        uploaded.save(raw_destination)

        try:
            optimized_destination = raw_destination.with_suffix(".jpg")
            optimized_path = optimize_image(raw_destination, optimized_destination)

            if raw_destination != optimized_path and raw_destination.exists():
                raw_destination.unlink()

            saved_files.append(optimized_path)
            app.logger.info("Saved optimized image: %s", optimized_path)

        except Exception as exc:
            app.logger.exception("Image optimization failed for %s", raw_destination)
            flash(f"Image optimization failed for {original_name}: {exc}")

            # fallback: keep original if optimization fails
            saved_files.append(raw_destination)

    return saved_files


def get_next_auction_photo_index(auction_number: str) -> int:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        cursor.execute(
            f"""
            SELECT last_index
            FROM auction_photo_counters
            WHERE auction_number = {placeholder}
            """,
            (str(auction_number),),
        )
        row = cursor.fetchone()
    finally:
        connection.close()

    if not row:
        return 1
    if isinstance(row, sqlite3.Row):
        current = int(row["last_index"])
    elif isinstance(row, dict):
        current = int(row.get("last_index", 0))
    else:
        current = int(row[0])
    return current + 1


def reserve_next_auction_photo_index(auction_number: str) -> int:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        cursor.execute(
            f"""
            SELECT last_index
            FROM auction_photo_counters
            WHERE auction_number = {placeholder}
            """,
            (str(auction_number),),
        )
        row = cursor.fetchone()
        current = 0
        if row:
            if isinstance(row, sqlite3.Row):
                current = int(row["last_index"])
            elif isinstance(row, dict):
                current = int(row.get("last_index", 0))
            else:
                current = int(row[0])
        next_index = current + 1

        if dialect == "sqlite":
            cursor.execute(
                """
                INSERT OR REPLACE INTO auction_photo_counters (
                    auction_number,
                    last_index,
                    updated_at
                ) VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (str(auction_number), next_index),
            )
        else:
            cursor.execute(
                """
                INSERT INTO auction_photo_counters (
                    auction_number,
                    last_index
                ) VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE
                    last_index = VALUES(last_index)
                """,
                (str(auction_number), next_index),
            )
        connection.commit()
        return next_index
    finally:
        connection.close()


def get_ftp_upload_record(lot_number: int | str) -> dict[str, object] | None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        current_auction_id = get_current_auction_id()
        cursor.execute(
            f"""
            SELECT lot_number, auction_id, auction_number, auction_photo_index, remote_names
            FROM ftp_uploads
            WHERE lot_number = {placeholder} AND auction_id = {placeholder}
            """,
            (int(lot_number), current_auction_id),
        )
        record = cursor.fetchone()
    finally:
        connection.close()

    if not record:
        return None

    if isinstance(record, sqlite3.Row):
        row = {key: record[key] for key in record.keys()}
    elif isinstance(record, dict):
        row = dict(record)
    else:
        return None

    remote_names = row.get("remote_names", "")
    if isinstance(remote_names, str):
        remote_names_list = [name for name in remote_names.split(",") if name]
    else:
        remote_names_list = list(remote_names)

    return {
        "lot_number": int(row.get("lot_number", lot_number)),
        "auction_id": int(row.get("auction_id", 0) or 0),
        "auction_number": str(row.get("auction_number", "")),
        "auction_photo_index": int(row.get("auction_photo_index", 0)),
        "remote_names": remote_names_list,
    }


def record_ftp_upload(
    lot_number: int,
    auction_number: str,
    auction_photo_index: int,
    remote_names: list[str],
) -> None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    serialized_remote_names = ",".join(remote_names)
    current_auction_id = get_current_auction_id()

    try:
        cursor = connection.cursor()
        if dialect == "sqlite":
            cursor.execute(
                """
                INSERT OR REPLACE INTO ftp_uploads (
                    lot_number,
                    auction_id,
                    auction_number,
                    auction_photo_index,
                    remote_names
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    lot_number,
                    current_auction_id,
                    str(auction_number),
                    int(auction_photo_index),
                    serialized_remote_names,
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO ftp_uploads (
                    lot_number,
                    auction_id,
                    auction_number,
                    auction_photo_index,
                    remote_names
                ) VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    auction_id = VALUES(auction_id),
                    auction_number = VALUES(auction_number),
                    auction_photo_index = VALUES(auction_photo_index),
                    remote_names = VALUES(remote_names)
                """,
                (
                    lot_number,
                    current_auction_id,
                    str(auction_number),
                    int(auction_photo_index),
                    serialized_remote_names,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def delete_ftp_upload_record(lot_number: int | str) -> None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    current_auction_id = get_current_auction_id()

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        current_auction_id = get_current_auction_id()
        cursor.execute(
            f"DELETE FROM ftp_uploads WHERE lot_number = {placeholder} AND auction_id = {placeholder}",
            (int(lot_number), current_auction_id),
        )
        connection.commit()
    finally:
        connection.close()


def load_saved_files_for_temp_id(temp_id: str) -> list[Path]:
    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        return []
    return sorted([p for p in temp_dir.iterdir() if p.is_file()])


def blank_form(seller_notes: str = "") -> dict[str, str]:
    return {
        "Identification": "",
        "Confidence Note": "",
        "Material Notes": "",
        "Mark Notes": "",
        "Title": "",
        "Description": "",
        "Condition Summary": "",
        "Keywords": "",
        "Category": "Other",
        "Low Estimate ($)": "",
        "High Estimate ($)": "",
        "Dimensions - Length": "",
        "Dimensions - Depth": "",
        "Dimensions - Height": "",
        "Reference #": "",
        "Item Notes": seller_notes,
        "Consigner #": "",
        "Shipping Available": "",
    }


def options_from_request() -> list[dict]:
    options = []
    for i in range(1, 4):
        options.append(
            {
                "rank": i,
                "identification": request.form.get(f"option_{i}_identification", "").strip(),
                "confidence_note": request.form.get(f"option_{i}_confidence_note", "").strip(),
                "material_notes": request.form.get(f"option_{i}_material_notes", "").strip(),
                "mark_notes": request.form.get(f"option_{i}_mark_notes", "").strip(),
                "title": request.form.get(f"option_{i}_title", "").strip(),
                "description": request.form.get(f"option_{i}_description", "").strip(),
                "category": request.form.get(f"option_{i}_category", "").strip() or "Other",
                "condition_summary": request.form.get(f"option_{i}_condition_summary", "").strip(),
                "keywords": request.form.get(f"option_{i}_keywords", "").strip(),
            }
        )
    return options


def form_from_request(seller_notes: str = "") -> dict[str, str]:
    return {
        "Identification": request.form.get("Identification", "").strip(),
        "Confidence Note": request.form.get("Confidence Note", "").strip(),
        "Material Notes": request.form.get("Material Notes", "").strip(),
        "Mark Notes": request.form.get("Mark Notes", "").strip(),
        "Title": request.form.get("Title", "").strip(),
        "Description": request.form.get("Description", "").strip(),
        "Condition Summary": request.form.get("Condition Summary", "").strip(),
        "Keywords": request.form.get("Keywords", "").strip(),
        "Category": request.form.get("Category", "").strip() or "Other",
        "Low Estimate ($)": request.form.get("Low Estimate ($)", "").strip(),
        "High Estimate ($)": request.form.get("High Estimate ($)", "").strip(),
        "Dimensions - Length": request.form.get("Dimensions - Length", "").strip(),
        "Dimensions - Depth": request.form.get("Dimensions - Depth", "").strip(),
        "Dimensions - Height": request.form.get("Dimensions - Height", "").strip(),
        "Reference #": request.form.get("Reference #", "").strip(),
        "Item Notes": request.form.get("Item Notes", seller_notes).strip(),
        "Consigner #": request.form.get("Consigner #", "").strip(),
        "Shipping Available": request.form.get("Shipping Available", "").strip(),
    }


def form_from_option(option: dict, seller_notes: str = "") -> dict[str, str]:
    form = blank_form(seller_notes=seller_notes)
    form["Identification"] = str(option.get("identification", "")).strip()
    form["Confidence Note"] = str(option.get("confidence_note", "")).strip()
    form["Material Notes"] = str(option.get("material_notes", "")).strip()
    form["Mark Notes"] = str(option.get("mark_notes", "")).strip()
    form["Title"] = str(option.get("title", "")).strip()
    form["Description"] = str(option.get("description", "")).strip()
    form["Condition Summary"] = str(option.get("condition_summary", "")).strip()
    form["Keywords"] = str(option.get("keywords", "")).strip()
    form["Category"] = str(option.get("category", "Other")).strip() or "Other"
    form["Item Notes"] = seller_notes
    return form


def parse_decimal_field(value: str) -> float | None:
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    return float(cleaned)


def validate_save_form(form: dict[str, str]) -> list[str]:
    errors: list[str] = []

    if not form.get("Title", "").strip():
        errors.append("Title is required before saving.")

    low_raw = form.get("Low Estimate ($)", "")
    high_raw = form.get("High Estimate ($)", "")

    try:
        low_value = parse_decimal_field(low_raw)
    except ValueError:
        errors.append("Low Estimate ($) must be a number if provided.")
        low_value = None

    try:
        high_value = parse_decimal_field(high_raw)
    except ValueError:
        errors.append("High Estimate ($) must be a number if provided.")
        high_value = None

    if low_value is not None and high_value is not None and low_value > high_value:
        errors.append("Low Estimate ($) cannot be greater than High Estimate ($).")

    return errors


def combine_item_notes(form: dict[str, str]) -> str:
    return form.get("Item Notes", "").strip()


def build_csv_text(rows: list[list[str]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADER)
    writer.writerows(rows)
    return output.getvalue()


def archive_export_csv(filename: str, csv_text: str) -> Path:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = EXPORTS_DIR / filename
    archive_path.write_text(csv_text, encoding="utf-8", newline="")
    return archive_path


def record_export_batch(
    filename: str,
    export_type: str,
    lot_numbers: list[int],
    archive_path: Path,
) -> None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    serialized_lots = ",".join(str(lot_number) for lot_number in lot_numbers)
    current_auction_id = get_current_auction_id()

    try:
        cursor = connection.cursor()
        if dialect == "sqlite":
            cursor.execute(
                """
                INSERT OR REPLACE INTO export_batches (
                    auction_id,
                    filename,
                    export_type,
                    lot_numbers,
                    lot_count,
                    archive_path
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    current_auction_id,
                    filename,
                    export_type,
                    serialized_lots,
                    len(lot_numbers),
                    archive_path.name,
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO export_batches (
                    auction_id,
                    filename,
                    export_type,
                    lot_numbers,
                    lot_count,
                    archive_path
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    auction_id = VALUES(auction_id),
                    export_type = VALUES(export_type),
                    lot_numbers = VALUES(lot_numbers),
                    lot_count = VALUES(lot_count),
                    archive_path = VALUES(archive_path)
                """,
                (
                    current_auction_id,
                    filename,
                    export_type,
                    serialized_lots,
                    len(lot_numbers),
                    archive_path.name,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def list_export_archives() -> list[dict[str, str]]:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    current_auction_id = get_current_auction_id()

    try:
        cursor = connection.cursor()
        cursor.execute(
            f"""
            SELECT auction_id, filename, export_type, lot_numbers, lot_count, archive_path, created_at
            FROM export_batches
            WHERE auction_id = {("?" if dialect == "sqlite" else "%s")}
            ORDER BY created_at DESC, id DESC
            """,
            (current_auction_id,),
        )
        records = cursor.fetchall()
    finally:
        connection.close()

    archives: list[dict[str, str]] = []
    for record in records:
        if isinstance(record, sqlite3.Row):
            row = {key: "" if record[key] is None else str(record[key]) for key in record.keys()}
        elif isinstance(record, dict):
            row = {key: "" if value is None else str(value) for key, value in record.items()}
        else:
            continue
        archive_file = EXPORTS_DIR / row["archive_path"]
        size_bytes = archive_file.stat().st_size if archive_file.exists() else 0
        archives.append(
            {
                "filename": row["filename"],
                "auction_id": row["auction_id"],
                "export_type": row["export_type"],
                "lot_numbers": row["lot_numbers"],
                "lot_count": row["lot_count"],
                "modified_at": row["created_at"],
                "size_bytes": str(size_bytes),
            }
        )
    return archives


def fetch_export_batch(filename: str) -> dict[str, str] | None:
    current_auction_id = get_current_auction_id()
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        cursor.execute(
            f"""
            SELECT filename, export_type, lot_numbers, lot_count, archive_path, created_at
            FROM export_batches
            WHERE auction_id = {placeholder}
              AND filename = {placeholder}
            """,
            (current_auction_id, filename),
        )
        record = cursor.fetchone()
    finally:
        connection.close()

    if not record:
        return None

    if isinstance(record, sqlite3.Row):
        return {key: "" if record[key] is None else str(record[key]) for key in record.keys()}
    if isinstance(record, dict):
        return {key: "" if value is None else str(value) for key, value in record.items()}
    return None


def fetch_items_for_lot_numbers(lot_numbers: list[int]) -> list[dict[str, str]]:
    if not lot_numbers:
        return []

    current_auction_id = get_current_auction_id()
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholders = ", ".join(["?"] * len(lot_numbers)) if dialect == "sqlite" else ", ".join(["%s"] * len(lot_numbers))
        cursor.execute(
            f"""
            SELECT auction_id, lot_number, title, status, category, last_export_batch, updated_at, published_at
            FROM auction_items
            WHERE auction_id = {("?" if dialect == "sqlite" else "%s")}
              AND lot_number IN ({placeholders})
            ORDER BY lot_number
            """,
            (current_auction_id, *tuple(lot_numbers)),
        )
        records = cursor.fetchall()
    finally:
        connection.close()

    items: list[dict[str, str]] = []
    for record in records:
        if isinstance(record, sqlite3.Row):
            row = {key: "" if record[key] is None else str(record[key]) for key in record.keys()}
        elif isinstance(record, dict):
            row = {key: "" if value is None else str(value) for key, value in record.items()}
        else:
            continue
        items.append(row)
    return items


def current_auction_number_for_upload() -> str:
    return str(get_current_auction_id())


@app.before_request
def require_login_when_configured():
    if not auth_enabled():
        return None

    allowed_endpoints = {
        "healthz",
        "login",
        "logout",
        "static",
    }
    if request.endpoint in allowed_endpoints:
        return None

    if is_authenticated():
        return None

    return redirect(url_for("login", next=request.path))


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


@app.route("/auctions/create_next", methods=["POST"])
def create_auction_route():
    auction = create_next_auction()
    flash(f"Created auction {auction['id']} and switched to it.")
    return redirect(request.form.get("return_to") or url_for("dashboard"))


@app.route("/auctions/switch", methods=["POST"])
def switch_auction_route():
    auction_id = request.form.get("auction_id", "").strip()
    if not auction_id.isdigit() or not switch_current_auction(int(auction_id)):
        flash("Choose a valid auction to switch to.")
        return redirect(request.form.get("return_to") or url_for("dashboard"))

    flash(f"Now working in auction {auction_id}.")
    return redirect(request.form.get("return_to") or url_for("dashboard"))


@app.route("/auctions/status", methods=["POST"])
def update_auction_status_route():
    auction_id = request.form.get("auction_id", "").strip()
    status = request.form.get("status", "").strip().lower()
    if not auction_id.isdigit() or not update_auction_status(int(auction_id), status):
        flash("Choose a valid auction and status.")
        return redirect(request.form.get("return_to") or url_for("dashboard"))

    flash(f"Auction {auction_id} is now marked {status}.")
    return redirect(request.form.get("return_to") or url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth_enabled():
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        next_url = request.form.get("next", "").strip()
        if username == auth_username() and password == auth_password():
            session["authenticated"] = True
            flash("Signed in.")
            return redirect(next_url or url_for("index"))
        flash("Login failed. Check the username and password.")

    next_url = request.values.get("next", "").strip()
    return render_template("login.html", next_url=next_url, username=auth_username())


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("authenticated", None)
    flash("Signed out.")
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
def index():
    active_draft = get_active_draft()
    return render_template(
        "index.html",
        next_lot=get_next_lot_preview(),
        active_draft=active_draft,
    )


@app.route("/dashboard", methods=["GET"])
def dashboard():
    return render_template(
        "dashboard.html",
        counts=fetch_manage_item_counts(),
        recent_exports=list_export_archives()[:5],
        needs_update_items=fetch_dashboard_items([ITEM_STATUS_NEEDS_UPDATE], limit=5),
        ready_items=fetch_dashboard_items([ITEM_STATUS_READY], limit=5),
    )


@app.route("/auctions", methods=["GET"])
def auctions_overview():
    return render_template(
        "auctions.html",
        auctions=fetch_auction_summaries(),
    )


@app.route("/export_csv", methods=["GET"])
def export_csv():
    rows = fetch_export_rows()
    if not rows:
        flash("There are no saved items to export yet.")
        return redirect(url_for("index"))

    filename = f"auction_{get_current_auction_id()}_items_export_{time.strftime('%Y%m%d')}.csv"
    csv_text = build_csv_text(rows)
    lot_numbers = lot_numbers_from_rows(rows)
    archive_path = archive_export_csv(filename, csv_text)
    record_export_batch(
        filename=filename,
        export_type="full",
        lot_numbers=lot_numbers,
        archive_path=archive_path,
    )
    mark_lots_as_published(
        lot_numbers=lot_numbers,
        export_batch_name=filename,
    )
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/manage_items", methods=["GET"])
def manage_items():
    current_filter = normalize_manage_filter(request.args.get("status", "active"))
    items = fetch_manage_items(current_filter)
    filter_counts = fetch_manage_item_counts()
    return render_template(
        "manage_items.html",
        items=items,
        current_filter=current_filter,
        filter_counts=filter_counts,
    )


@app.route("/exports", methods=["GET"])
def export_history():
    return render_template(
        "export_history.html",
        archives=list_export_archives(),
    )


@app.route("/exports/<path:filename>/details", methods=["GET"])
def export_batch_details(filename: str):
    safe_name = Path(filename).name
    batch = fetch_export_batch(safe_name)
    if not batch:
        flash("That export batch was not found.")
        return redirect(url_for("export_history"))

    lot_numbers = [int(value) for value in batch.get("lot_numbers", "").split(",") if value.isdigit()]
    items = fetch_items_for_lot_numbers(lot_numbers)
    return render_template(
        "export_batch_details.html",
        batch=batch,
        items=items,
    )


@app.route("/exports/<path:filename>", methods=["GET"])
def download_export_archive(filename: str):
    safe_name = Path(filename).name
    target = EXPORTS_DIR / safe_name
    if not target.exists() or not target.is_file():
        flash("That export file was not found.")
        return redirect(url_for("export_history"))
    return send_from_directory(EXPORTS_DIR, safe_name, as_attachment=True)


@app.route("/items/<int:lot_number>/edit", methods=["GET"])
def edit_saved_item(lot_number: int):
    item = fetch_saved_item(lot_number)
    if not item or item.get("status") == ITEM_STATUS_REMOVED:
        flash(f"Lot {lot_number} was not found.")
        return redirect(url_for("manage_items", status=normalize_manage_filter(request.args.get("status", "active"))))

    image_folder = item.get("image_folder", "")
    saved_files = load_saved_files_for_temp_id(image_folder)
    return render_template(
        "saved_item_edit.html",
        item=item,
        form=form_from_saved_item(item),
        categories=DEFAULT_CATEGORIES,
        image_files=[p.name for p in saved_files],
        image_url_prefix=f"/uploads/{image_folder}/" if image_folder else "",
        current_filter=normalize_manage_filter(request.args.get("status", "active")),
    )


@app.route("/items/<int:lot_number>/update", methods=["POST"])
def update_saved_item(lot_number: int):
    current_filter = normalize_manage_filter(request.form.get("current_filter", "active"))
    item = fetch_saved_item(lot_number)
    if not item or item.get("status") == ITEM_STATUS_REMOVED:
        flash(f"Lot {lot_number} was not found.")
        return redirect(url_for("manage_items", status=current_filter))

    form = {
        "Title": request.form.get("Title", "").strip(),
        "Description": request.form.get("Description", "").strip(),
        "Condition Summary": request.form.get("Condition Summary", "").strip(),
        "Keywords": request.form.get("Keywords", "").strip(),
        "Category": request.form.get("Category", "").strip() or "Other",
        "Low Estimate ($)": request.form.get("Low Estimate ($)", "").strip(),
        "High Estimate ($)": request.form.get("High Estimate ($)", "").strip(),
        "Dimensions - Length": request.form.get("Dimensions - Length", "").strip(),
        "Dimensions - Depth": request.form.get("Dimensions - Depth", "").strip(),
        "Dimensions - Height": request.form.get("Dimensions - Height", "").strip(),
        "Reference #": request.form.get("Reference #", "").strip(),
        "Item Notes": request.form.get("Item Notes", "").strip(),
        "Consigner #": request.form.get("Consigner #", "").strip(),
        "Shipping Available": request.form.get("Shipping Available", "").strip() or "No",
    }

    validation_errors = validate_save_form(form)
    if validation_errors:
        for error in validation_errors:
            flash(error)
        image_folder = item.get("image_folder", "")
        saved_files = load_saved_files_for_temp_id(image_folder)
        return render_template(
            "saved_item_edit.html",
            item=item,
            form=form,
            categories=DEFAULT_CATEGORIES,
            image_files=[p.name for p in saved_files],
            image_url_prefix=f"/uploads/{image_folder}/" if image_folder else "",
            current_filter=current_filter,
        )

    new_status = update_saved_item_record(lot_number, form)
    if new_status == ITEM_STATUS_NEEDS_UPDATE:
        flash(f"Updated lot {lot_number}. Status changed to needs_update so it can be re-exported.")
    else:
        flash(f"Updated lot {lot_number}.")
    return redirect(url_for("manage_items", status=current_filter))


@app.route("/items/<int:lot_number>/remove", methods=["POST"])
def remove_saved_item(lot_number: int):
    current_filter = normalize_manage_filter(request.form.get("current_filter", "active"))
    item = fetch_saved_item(lot_number)
    if not item or item.get("status") == ITEM_STATUS_REMOVED:
        flash(f"Lot {lot_number} was not found.")
        return redirect(url_for("manage_items", status=current_filter))

    if mark_item_removed(lot_number):
        flash(f"Removed lot {lot_number} from future exports.")
    else:
        flash(f"Lot {lot_number} could not be removed.")
    return redirect(url_for("manage_items", status=current_filter))


@app.route("/items/<int:lot_number>/move", methods=["POST"])
def move_saved_item(lot_number: int):
    current_filter = normalize_manage_filter(request.form.get("current_filter", "active"))
    target_auction_id = request.form.get("auction_id", "").strip()
    if not target_auction_id.isdigit():
        flash("Choose a valid auction to move this lot.")
        return redirect(url_for("edit_saved_item", lot_number=lot_number, status=current_filter))

    if move_item_to_auction(lot_number, int(target_auction_id)):
        flash(
            f"Moved lot {lot_number} to auction {target_auction_id}. "
            "Its publish state was reset so it can be reviewed and exported there."
        )
    else:
        flash(f"Lot {lot_number} could not be moved.")
    return redirect(url_for("manage_items", status=current_filter))


@app.route("/items/<int:lot_number>/restore", methods=["POST"])
def restore_saved_item(lot_number: int):
    current_filter = normalize_manage_filter(request.form.get("current_filter", "removed"))
    restored_status = restore_removed_item(lot_number)
    if restored_status == ITEM_STATUS_NEEDS_UPDATE:
        flash(f"Restored lot {lot_number}. It is marked needs_update because it had already been published before removal.")
    elif restored_status == ITEM_STATUS_READY:
        flash(f"Restored lot {lot_number} to ready.")
    else:
        flash(f"Lot {lot_number} could not be restored.")
    return redirect(url_for("manage_items", status=current_filter))


@app.route("/items/bulk_action", methods=["POST"])
def bulk_update_items():
    current_filter = normalize_manage_filter(request.form.get("current_filter", "active"))
    selected_lots = sorted(
        {
            int(value)
            for value in request.form.getlist("lot_numbers")
            if str(value).isdigit()
        }
    )
    action = request.form.get("bulk_action", "").strip().lower()

    if not selected_lots:
        flash("Select at least one lot for a bulk action.")
        return redirect(url_for("manage_items", status=current_filter))

    if action == "remove":
        changed = 0
        for lot_number in selected_lots:
            changed += 1 if mark_item_removed(lot_number) else 0
        flash(f"Removed {changed} selected lot(s) from future exports.")
        return redirect(url_for("manage_items", status=current_filter))

    if action == "restore":
        restored = bulk_restore_items(selected_lots)
        flash(f"Restored {restored} selected lot(s).")
        return redirect(url_for("manage_items", status=current_filter))

    if action == "mark_ready":
        changed = set_items_status(selected_lots, ITEM_STATUS_READY)
        flash(f"Marked {changed} selected lot(s) as ready.")
        return redirect(url_for("manage_items", status=current_filter))

    if action == "move":
        target_auction_id = request.form.get("target_auction_id", "").strip()
        if not target_auction_id.isdigit():
            flash("Choose a destination auction for the move action.")
            return redirect(url_for("manage_items", status=current_filter))

        moved = 0
        for lot_number in selected_lots:
            moved += 1 if move_item_to_auction(lot_number, int(target_auction_id)) else 0
        flash(
            f"Moved {moved} selected lot(s) to auction {target_auction_id}. "
            "Their publish state was reset for review in the new auction."
        )
        return redirect(url_for("manage_items", status=current_filter))

    if action == "upload_ftp":
        uploaded_count = 0
        for lot_number in selected_lots:
            item = fetch_saved_item(lot_number)
            if not item:
                continue
            image_folder = item.get("image_folder")
            if not image_folder:
                continue
            final_dir = UPLOADS_DIR / image_folder
            if not final_dir.exists():
                continue
            local_jpgs = sorted([p for p in final_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"])
            if not local_jpgs:
                continue
            auction_number = str(item.get("auction_id", current_auction_number_for_upload()))
            if not auction_number:
                continue
            try:
                auction_photo_index = reserve_next_auction_photo_index(auction_number)
                uploaded_names = upload_lot_photos_to_auctionninja(
                    local_files=local_jpgs,
                    auction_number=auction_number,
                    lot_number=lot_number,
                )
                if uploaded_names:
                    record_ftp_upload(lot_number, auction_number, auction_photo_index, uploaded_names)
                    uploaded_count += 1
            except Exception as exc:
                app.logger.exception("Bulk FTP upload failed for lot %s", lot_number)
                flash(f"FTP upload failed for lot {lot_number}: {exc}")
        flash(f"Successfully uploaded photos to FTP for {uploaded_count} selected lot(s).")
        return redirect(url_for("manage_items", status=current_filter))

    flash("Choose a valid bulk action.")
    return redirect(url_for("manage_items", status=current_filter))


@app.route("/export_selected_csv", methods=["POST"])
def export_selected_csv():
    current_filter = normalize_manage_filter(request.form.get("current_filter", "active"))
    selected_lots = sorted(
        {
            int(value)
            for value in request.form.getlist("lot_numbers")
            if str(value).isdigit()
        }
    )

    if not selected_lots:
        flash("Select at least one lot to export.")
        return redirect(url_for("manage_items", status=current_filter))

    rows = fetch_export_rows_for_lots(selected_lots)
    if not rows:
        flash("The selected lots could not be exported.")
        return redirect(url_for("manage_items", status=current_filter))

    first_lot = selected_lots[0]
    last_lot = selected_lots[-1]
    filename = f"auction_{get_current_auction_id()}_batch_{first_lot}-{last_lot}_{time.strftime('%Y%m%d')}.csv"

    mark_lots_as_published(
        lot_numbers=selected_lots,
        export_batch_name=filename,
    )
    csv_text = build_csv_text(rows)
    archive_path = archive_export_csv(filename, csv_text)
    record_export_batch(
        filename=filename,
        export_type="selected",
        lot_numbers=selected_lots,
        archive_path=archive_path,
    )

    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/analyze", methods=["POST"])
def analyze():
    app.logger.info("Entered /analyze")

    uploaded_files = request.files.getlist("photos")
    seller_notes = request.form.get("seller_notes", "").strip()

    temp_id, saved_files = save_uploaded_files(uploaded_files)

    if not saved_files:
        flash("Please choose at least one valid image.")
        return redirect(url_for("index"))

    try:
        ai_data = generator.generate_options(saved_files, seller_notes=seller_notes)
        options = ai_data.get("options", [])
        if not options:
            raise ValueError("No listing options were returned.")
        selected = options[0]
        form = form_from_option(selected, seller_notes=seller_notes)
        app.logger.info("Generated %s options", len(options))
    except Exception as exc:
        app.logger.exception("AI analysis failed")
        flash(f"AI analysis failed: {exc}")
        return redirect(url_for("index"))

    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
        options=options,
        form=form,
    )


@app.route("/choose_option", methods=["POST"])
def choose_option():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()

    saved_files = load_saved_files_for_temp_id(temp_id)
    if not saved_files:
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    options = options_from_request()
    chosen_rank = request.form.get("chosen_rank", "").strip()

    selected_option = None
    for option in options:
        if str(option.get("rank")) == chosen_rank:
            selected_option = option
            break

    if selected_option is None:
        flash("Could not determine which option was selected.")
        form = form_from_request(seller_notes=seller_notes)
    else:
        form = form_from_option(selected_option, seller_notes=seller_notes)

        current_form = form_from_request(seller_notes=seller_notes)
        for key in [
            "Low Estimate ($)",
            "High Estimate ($)",
            "Dimensions - Length",
            "Dimensions - Depth",
            "Dimensions - Height",
            "Reference #",
            "Item Notes",
            "Consigner #",
            "Shipping Available",
        ]:
            form[key] = current_form.get(key, "")

    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
        options=options,
        form=form,
    )


@app.route("/add_draft_photos", methods=["POST"])
def add_draft_photos():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()
    saved_files, options, form = current_edit_context(temp_id, seller_notes)

    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    uploaded_files = request.files.getlist("photos")
    added_files = save_uploaded_files_to_dir(uploaded_files, temp_dir)

    if added_files:
        flash(f"Added {len(added_files)} photo(s) to this draft.")
    else:
        flash("No new valid photos were added to this draft.")

    saved_files = load_saved_files_for_temp_id(temp_id)
    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
        options=options,
        form=form,
    )


@app.route("/remove_draft_photo", methods=["POST"])
def remove_draft_photo():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()
    filename = secure_filename(request.form.get("filename", "").strip())
    saved_files, options, form = current_edit_context(temp_id, seller_notes)

    temp_dir = UPLOADS_DIR / temp_id
    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    target = temp_dir / filename
    if not filename or not target.exists() or not target.is_file():
        flash("Could not find that photo in this draft.")
    else:
        target.unlink()
        flash(f"Removed photo: {filename}")

    saved_files = load_saved_files_for_temp_id(temp_id)
    if not saved_files:
        flash("This draft has no photos left. Please add at least one photo before revising or saving.")

    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
        options=options,
        form=form,
    )


@app.route("/revise", methods=["POST"])
def revise():
    temp_id = request.form.get("temp_id", "").strip()
    seller_notes = request.form.get("seller_notes", "").strip()
    revision_request = request.form.get("revision_request", "").strip()

    saved_files = load_saved_files_for_temp_id(temp_id)
    if not saved_files:
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    options = options_from_request()
    form = form_from_request(seller_notes=seller_notes)

    current_option = {
        "identification": form["Identification"],
        "confidence_note": form["Confidence Note"],
        "material_notes": form["Material Notes"],
        "mark_notes": form["Mark Notes"],
        "title": form["Title"],
        "description": form["Description"],
        "category": form["Category"],
        "condition_summary": form["Condition Summary"],
        "keywords": form["Keywords"],
    }

    try:
        revised = generator.revise_option(
            saved_files,
            current_option=current_option,
            seller_notes=seller_notes,
            revision_request=revision_request,
        )
        form["Identification"] = str(revised.get("identification", form["Identification"])).strip()
        form["Confidence Note"] = str(revised.get("confidence_note", form["Confidence Note"])).strip()
        form["Material Notes"] = str(revised.get("material_notes", form["Material Notes"])).strip()
        form["Mark Notes"] = str(revised.get("mark_notes", form["Mark Notes"])).strip()
        form["Title"] = str(revised.get("title", form["Title"])).strip()
        form["Description"] = str(revised.get("description", form["Description"])).strip()
        form["Condition Summary"] = str(revised.get("condition_summary", form["Condition Summary"])).strip()
        form["Keywords"] = str(revised.get("keywords", form["Keywords"])).strip()
        form["Category"] = str(revised.get("category", form["Category"])).strip() or "Other"
    except Exception as exc:
        app.logger.exception("AI revision failed")
        flash(f"AI revision failed: {exc}")

    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=seller_notes,
        options=options,
        form=form,
    )


@app.route("/save", methods=["POST"])
def save():
    temp_id = request.form.get("temp_id", "").strip()
    temp_dir = UPLOADS_DIR / temp_id
    seller_notes = request.form.get("seller_notes", "").strip()

    if not temp_id or not temp_dir.exists():
        flash("Could not find uploaded images for this draft.")
        return redirect(url_for("index"))

    saved_files = load_saved_files_for_temp_id(temp_id)
    form = form_from_request(seller_notes=seller_notes)
    options = options_from_request()
    title = form["Title"]

    validation_errors = validate_save_form(form)
    if validation_errors:
        for error in validation_errors:
            flash(error)
        return render_edit_page(
            temp_id=temp_id,
            saved_files=saved_files,
            seller_notes=seller_notes,
            options=options,
            form=form,
        )

    csv_lot_number = reserve_next_lot()

    safe_title = slugify_title(title)
    folder_name = f"{csv_lot_number}_{safe_title}"
    final_dir = make_unique_dir(UPLOADS_DIR, folder_name)

    try:
        temp_dir.rename(final_dir)
    except Exception as exc:
        app.logger.exception("Failed to rename image folder")
        flash(f"Warning: saved listing but could not rename image folder: {exc}")
        final_dir = temp_dir

    record = item_record_from_form(
        lot_number=csv_lot_number,
        form=form,
        image_folder=final_dir.name,
    )
    append_item_record(record)
    clear_active_draft(temp_id=temp_id)

    auction_number = current_auction_number_for_upload()
    uploaded_names = []
    auction_photo_index = 0

    if auction_number:
        try:
            auction_photo_index = reserve_next_auction_photo_index(auction_number)
            local_jpgs = sorted([p for p in final_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"])
            uploaded_names = upload_lot_photos_to_auctionninja(
                local_files=local_jpgs,
                auction_number=auction_number,
                lot_number=csv_lot_number,
            )
        except Exception as exc:
            app.logger.exception("FTP upload failed")
            flash(f"Lot saved locally, but FTP upload failed: {exc}")

    if uploaded_names:
        record_ftp_upload(
            lot_number=csv_lot_number,
            auction_number=auction_number,
            auction_photo_index=auction_photo_index,
            remote_names=uploaded_names,
        )
        flash(
            f"Saved lot {csv_lot_number}. Uploaded to auction {auction_number} as: "
            + ", ".join(uploaded_names)
        )
    else:
        flash(
            f"Saved lot {csv_lot_number} to the database. "
            f"Images stored in: {final_dir.name}"
        )

    return redirect(url_for("index"))


@app.route("/uploads/<temp_id>/<filename>")
def uploaded_file(temp_id: str, filename: str):
    return send_from_directory(UPLOADS_DIR / temp_id, filename)


@app.route("/set_next_lot", methods=["POST"])
def set_next_lot():
    next_lot_str = request.form.get("next_lot", "").strip()
    if not next_lot_str.isdigit():
        flash("Next lot must be a valid number.")
        return redirect(request.referrer or url_for("index"))

    next_lot = int(next_lot_str)
    last_lot = next_lot - 1

    current_auction_id = get_current_auction_id()
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    
    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        if dialect == "sqlite":
            cursor.execute(
                f"UPDATE auctions SET last_lot_override = {placeholder}, updated_at = CURRENT_TIMESTAMP WHERE id = {placeholder}",
                (last_lot, current_auction_id)
            )
        else:
            cursor.execute(
                f"UPDATE auctions SET last_lot_override = {placeholder} WHERE id = {placeholder}",
                (last_lot, current_auction_id)
            )
        connection.commit()
    finally:
        connection.close()

    flash(f"Next lot number successfully set to {next_lot}.")
    return redirect(request.referrer or url_for("index"))


@app.route("/reset", methods=["POST"])
def reset():
    if UPLOADS_DIR.exists():
        shutil.rmtree(UPLOADS_DIR)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    clear_active_draft()
    flash("Temporary uploads cleared.")
    return redirect(url_for("index"))


@app.route("/resume_draft", methods=["GET"])
def resume_draft():
    active_draft = get_active_draft()
    if not active_draft:
        flash("No resumable draft was found.")
        return redirect(url_for("index"))

    temp_id = str(active_draft["temp_id"])
    saved_files = load_saved_files_for_temp_id(temp_id)
    return render_edit_page(
        temp_id=temp_id,
        saved_files=saved_files,
        seller_notes=str(active_draft["seller_notes"]),
        options=active_draft["options"],
        form=active_draft["form"],
        revision_request=str(active_draft["revision_request"]),
    )


@app.route("/discard_draft", methods=["POST"])
def discard_draft():
    active_draft = get_active_draft()
    if not active_draft:
        flash("No resumable draft was found.")
        return redirect(url_for("index"))

    temp_id = str(active_draft["temp_id"])
    temp_dir = UPLOADS_DIR / temp_id
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    clear_active_draft(temp_id=temp_id)
    flash("Discarded the last unsaved draft.")
    return redirect(url_for("index"))


@app.route("/delete_remote_upload", methods=["POST"])
def delete_remote_upload():
    lot_number = request.form.get("lot_number", "").strip()

    if not lot_number.isdigit():
        flash("Enter a valid lot number to delete FTP photos.")
        return redirect(url_for("index"))

    record = get_ftp_upload_record(lot_number)
    if not record:
        flash(f"No saved FTP upload record was found for lot {lot_number}.")
        return redirect(url_for("index"))

    auction_number = str(record.get("auction_number", "")).strip()
    remote_names = record.get("remote_names", [])

    if not auction_number or not isinstance(remote_names, list):
        flash(f"FTP upload record for lot {lot_number} is incomplete.")
        return redirect(url_for("index"))

    try:
        deleted_names, missing_names = delete_lot_photos_from_auctionninja(
            auction_number=auction_number,
            remote_names=[str(name) for name in remote_names],
        )
        delete_ftp_upload_record(lot_number)
    except Exception as exc:
        app.logger.exception("FTP delete failed for lot %s", lot_number)
        flash(f"FTP delete failed for lot {lot_number}: {exc}")
        return redirect(url_for("index"))

    if deleted_names and missing_names:
        flash(
            f"Deleted FTP photos for lot {lot_number}: {', '.join(deleted_names)}. "
            f"Already missing: {', '.join(missing_names)}."
        )
    elif deleted_names:
        flash(f"Deleted FTP photos for lot {lot_number}: {', '.join(deleted_names)}.")
    elif missing_names:
        flash(
            f"FTP photos for lot {lot_number} were already missing remotely: "
            + ", ".join(missing_names)
        )
    else:
        flash(f"No FTP photos were recorded for lot {lot_number}.")

    return redirect(url_for("index"))


@app.route("/upload_remote_ftp", methods=["POST"])
def upload_remote_ftp():
    lot_number_str = request.form.get("lot_number", "").strip()
    if not lot_number_str.isdigit():
        flash("Enter a valid lot number to upload FTP photos.")
        return redirect(url_for("index"))

    lot_number = int(lot_number_str)
    image_folder = None
    auction_number = current_auction_number_for_upload()

    item = fetch_saved_item(lot_number)
    if item:
        image_folder = item.get("image_folder")
        auction_number = str(item.get("auction_id", auction_number))

    if not image_folder:
        flash(f"No image folder found for lot {lot_number}.")
        return redirect(url_for("index"))

    final_dir = UPLOADS_DIR / image_folder
    if not final_dir.exists():
        flash(f"Image folder {final_dir.name} does not exist.")
        return redirect(url_for("index"))

    local_jpgs = sorted([p for p in final_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"])
    if not local_jpgs:
        flash(f"No JPG photos found in {final_dir.name}.")
        return redirect(url_for("index"))

    if not auction_number:
        flash("No auction number configured or associated with this lot.")
        return redirect(url_for("index"))

    try:
        auction_photo_index = reserve_next_auction_photo_index(auction_number)
        uploaded_names = upload_lot_photos_to_auctionninja(
            local_files=local_jpgs,
            auction_number=auction_number,
            lot_number=lot_number,
        )
        if uploaded_names:
            record_ftp_upload(lot_number, auction_number, auction_photo_index, uploaded_names)
            flash(f"Successfully uploaded {len(uploaded_names)} photos for lot {lot_number} to FTP.")
        else:
            flash(f"Failed to upload photos for lot {lot_number}.")
    except Exception as exc:
        app.logger.exception("FTP upload failed for lot %s", lot_number)
        flash(f"FTP upload failed for lot {lot_number}: {exc}")

    return redirect(url_for("index"))


@app.route("/ftp_preview", methods=["GET"])
def ftp_preview():
    auction_number = current_auction_number_for_upload()
    if not auction_number:
        flash("You must set an active AUCTION_NUMBER to preview photos.")
        return redirect(url_for("index"))

    missing_lots = []

    if UPLOADS_DIR.exists():
        for final_dir in sorted(UPLOADS_DIR.iterdir(), key=lambda d: d.name):
            if not final_dir.is_dir():
                continue

            parts = final_dir.name.split('_', 1)
            if not parts[0].isdigit():
                continue

            lot_number = int(parts[0])
            if get_ftp_upload_record(lot_number):
                continue

            local_jpgs = sorted([p for p in final_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"])
            if not local_jpgs:
                continue
            
            current_lot_auction = auction_number
            item_title = ""
            item_description = ""
            item = fetch_saved_item(lot_number)
            if item:
                if item.get("auction_id"):
                    current_lot_auction = str(item["auction_id"])
                item_title = item.get("title", "")
                item_description = item.get("description", "")

            files_info = []
            for i, p in enumerate(local_jpgs, start=1):
                files_info.append({
                    "original_name": p.name,
                    "remote_name": f"{lot_number}_{i}.jpg"
                })

            missing_lots.append({
                "lot_number": lot_number,
                "auction_number": current_lot_auction,
                "title": item_title,
                "description": item_description,
                "folder": final_dir.name,
                "files": files_info
            })

    return render_template("ftp_preview.html", missing_lots=missing_lots)


@app.route("/upload_selected_ftp", methods=["POST"])
def upload_selected_ftp():
    auction_number = current_auction_number_for_upload()
    if not auction_number:
        flash("You must set an active AUCTION_NUMBER to upload photos.")
        return redirect(url_for("index"))

    lots_to_upload = {}
    for key, value in request.form.items():
        if key.startswith("lot_") and "_file_" in key:
            parts = key.split("_", 3)
            lot_number = int(parts[1])
            original_name = parts[3]

            if lot_number not in lots_to_upload:
                lots_to_upload[lot_number] = {
                    "auction_number": request.form.get(f"lot_{lot_number}_auction"),
                    "folder": request.form.get(f"lot_{lot_number}_folder"),
                    "files": []
                }
            
            remote_name = request.form.get(f"lot_{lot_number}_name_{original_name}", original_name).strip()
            if not remote_name:
                remote_name = original_name

            lots_to_upload[lot_number]["files"].append((original_name, remote_name))

    uploaded_count = 0
    
    for lot_number, data in lots_to_upload.items():
        folder = data["folder"]
        current_lot_auction = data["auction_number"]
        if not folder or not current_lot_auction:
            continue
            
        final_dir = UPLOADS_DIR / folder
        if not final_dir.exists():
            continue

        files_to_upload = []
        for orig_name, remote_name in data["files"]:
            local_path = final_dir / orig_name
            if local_path.exists():
                files_to_upload.append((local_path, remote_name))

        if not files_to_upload:
            continue

        try:
            auction_photo_index = reserve_next_auction_photo_index(current_lot_auction)
            uploaded_names = upload_lot_photos_to_auctionninja(files_to_upload, current_lot_auction, lot_number)
            if uploaded_names:
                record_ftp_upload(lot_number, current_lot_auction, auction_photo_index, uploaded_names)
                uploaded_count += 1
        except Exception as exc:
            app.logger.exception("FTP upload failed for lot %s", lot_number)
            flash(f"FTP upload failed for lot {lot_number}: {exc}")

    if uploaded_count > 0:
        flash(f"Successfully uploaded photos to FTP for {uploaded_count} lot(s).")
    else:
        flash("No new lot photos were uploaded.")

    return redirect(url_for("index"))


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    app.run(host=host, port=port, debug=debug)
