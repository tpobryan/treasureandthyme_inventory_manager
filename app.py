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
    url_for,
)
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
register_heif_opener()
from ftplib import FTP, FTP_TLS, error_perm
from auctionninja_generator import AuctionNinjaGenerator

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
CSV_PATH = DATA_DIR / "auction_items.csv"
LOT_STATE_PATH = DATA_DIR / "lot_state.json"
AUCTION_PHOTO_STATE_PATH = DATA_DIR / "auction_photo_state.json"
FTP_UPLOAD_STATE_PATH = DATA_DIR / "ftp_upload_state.json"
ACTIVE_DRAFT_STATE_PATH = DATA_DIR / "active_draft.json"
LOT_LOCK_PATH = DATA_DIR / "lot_state.lock"
AUCTION_PHOTO_LOCK_PATH = DATA_DIR / "auction_photo_state.lock"
FTP_UPLOAD_STATE_LOCK_PATH = DATA_DIR / "ftp_upload_state.lock"
ACTIVE_DRAFT_STATE_LOCK_PATH = DATA_DIR / "active_draft.lock"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

generator = AuctionNinjaGenerator()

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

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
DEFAULT_STARTING_LOT = 1999
ITEM_STATUS_READY = "ready"
ITEM_STATUS_PUBLISHED = "published"
ITEM_STATUS_NEEDS_UPDATE = "needs_update"
ITEM_STATUS_REMOVED = "removed"
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
MAX_IMAGE_DIMENSION = 1800
JPEG_QUALITY = 85
LOCK_TIMEOUT_SECONDS = 10
LOCK_POLL_INTERVAL_SECONDS = 0.1


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


def ensure_active_draft_state() -> None:
    if not ACTIVE_DRAFT_STATE_PATH.exists():
        ACTIVE_DRAFT_STATE_PATH.write_text("{}", encoding="utf-8")


def set_active_draft(
    temp_id: str,
    seller_notes: str,
    options: list[dict],
    form: dict[str, str],
    revision_request: str = "",
) -> None:
    with state_lock(ACTIVE_DRAFT_STATE_LOCK_PATH):
        ensure_active_draft_state()
        payload = {
            "temp_id": temp_id,
            "seller_notes": seller_notes,
            "options": options,
            "form": form,
            "revision_request": revision_request,
        }
        ACTIVE_DRAFT_STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clear_active_draft(temp_id: str | None = None) -> None:
    with state_lock(ACTIVE_DRAFT_STATE_LOCK_PATH):
        ensure_active_draft_state()
        try:
            current = json.loads(ACTIVE_DRAFT_STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = {}

        if temp_id and str(current.get("temp_id", "")).strip() != temp_id:
            return

        ACTIVE_DRAFT_STATE_PATH.write_text("{}", encoding="utf-8")


def get_active_draft() -> dict[str, Any] | None:
    ensure_active_draft_state()
    try:
        data = json.loads(ACTIVE_DRAFT_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        clear_active_draft()
        return None

    if not isinstance(data, dict):
        clear_active_draft()
        return None

    temp_id = str(data.get("temp_id", "")).strip()
    if not temp_id:
        return None

    saved_files = load_saved_files_for_temp_id(temp_id)
    if not saved_files:
        clear_active_draft(temp_id=temp_id)
        return None

    options = data.get("options", [])
    form = data.get("form", {})

    if not isinstance(options, list) or not isinstance(form, dict):
        clear_active_draft(temp_id=temp_id)
        return None

    return {
        "temp_id": temp_id,
        "seller_notes": str(data.get("seller_notes", "")).strip(),
        "options": options,
        "form": form,
        "revision_request": str(data.get("revision_request", "")).strip(),
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


def _read_lock_pid(lock_path: Path) -> int | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None

    if not raw.isdigit():
        return None
    return int(raw)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _clear_stale_lock(lock_path: Path) -> bool:
    pid = _read_lock_pid(lock_path)

    if pid is not None and _pid_is_running(pid):
        return False

    try:
        lock_path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


@contextmanager
def state_lock(lock_path: Path, timeout_seconds: float = LOCK_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout_seconds
    lock_fd = None

    while True:
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            if _clear_stale_lock(lock_path):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock: {lock_path.name}")
            time.sleep(LOCK_POLL_INTERVAL_SECONDS)

    try:
        os.write(lock_fd, str(os.getpid()).encode("utf-8"))
        yield
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def get_database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def database_enabled() -> bool:
    return bool(get_database_url())


def database_label() -> str:
    database_url = get_database_url()
    if not database_url:
        return "Local CSV file"

    scheme = urlparse(database_url).scheme.lower()
    if scheme.startswith("mysql"):
        return "MySQL database"
    if scheme.startswith("sqlite"):
        return "SQLite database"
    return "Database"


def connect_item_store():
    database_url = get_database_url()
    if not database_url:
        return None, "csv"

    parsed = urlparse(database_url)
    scheme = parsed.scheme.lower()

    if scheme.startswith("sqlite"):
        db_path = unquote(parsed.path or "")
        if not db_path:
            raise ValueError("DATABASE_URL sqlite path is missing.")
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
    if not database_enabled():
        return

    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        if dialect == "sqlite":
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auction_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lot_number INTEGER NOT NULL UNIQUE,
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
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            _ensure_sqlite_column(cursor, "auction_items", "last_export_batch", "TEXT")
            _ensure_sqlite_column(cursor, "auction_items", "published_at", "TEXT")
        else:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auction_items (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    lot_number INT NOT NULL UNIQUE,
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
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )
            _ensure_mysql_column(cursor, "auction_items", "last_export_batch", "VARCHAR(255) NULL")
            _ensure_mysql_column(cursor, "auction_items", "published_at", "TIMESTAMP NULL DEFAULT NULL")
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


def fetch_last_lot_from_store() -> int:
    if not database_enabled():
        ensure_lot_state()
        data = json.loads(LOT_STATE_PATH.read_text(encoding="utf-8"))
        return int(data.get("last_lot", DEFAULT_STARTING_LOT))

    ensure_item_store_ready()
    connection, _dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        cursor.execute("SELECT MAX(lot_number) AS max_lot FROM auction_items")
        row = cursor.fetchone()
    finally:
        connection.close()

    if isinstance(row, sqlite3.Row):
        max_lot = row["max_lot"]
    elif isinstance(row, dict):
        max_lot = row.get("max_lot")
    else:
        max_lot = row[0] if row else None

    return int(max_lot or DEFAULT_STARTING_LOT)


def item_record_from_form(lot_number: int, form: dict[str, str], image_folder: str) -> dict[str, str]:
    return {
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
    if not database_enabled():
        row = [
            record["lot_number"],
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
        ]
        append_csv_row(row)
        return

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholders = ", ".join(["?"] * 17) if dialect == "sqlite" else ", ".join(["%s"] * 17)
        cursor.execute(
            f"""
            INSERT INTO auction_items (
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
    if not lot_numbers or not database_enabled():
        return

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

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
                WHERE lot_number IN ({placeholders})
                """,
                params,
            )
        else:
            params = (
                ITEM_STATUS_PUBLISHED,
                export_batch_name,
                *lot_numbers,
            )
            cursor.execute(
                f"""
                UPDATE auction_items
                SET
                    status = %s,
                    last_export_batch = %s,
                    published_at = CURRENT_TIMESTAMP
                WHERE lot_number IN ({placeholders})
                """,
                params,
            )
        connection.commit()
    finally:
        connection.close()


def fetch_export_rows() -> list[list[str]]:
    if not database_enabled():
        ensure_csv_exists()
        with CSV_PATH.open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        return rows[1:]

    ensure_item_store_ready()
    connection, _dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        cursor.execute(
            """
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
            WHERE status != 'removed'
            ORDER BY lot_number
            """
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
    if not database_enabled():
        return []

    normalized_filter = normalize_manage_filter(status_filter)
    statuses = MANAGE_ITEM_FILTERS[normalized_filter]

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

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
            WHERE status IN ({placeholders})
            ORDER BY lot_number
            """,
            tuple(statuses),
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
    if not database_enabled():
        return None

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
            """,
            (lot_number,),
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
                WHERE lot_number = ?
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
                WHERE lot_number = %s
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
                ),
            )
        connection.commit()
    finally:
        connection.close()

    return new_status


def mark_item_removed(lot_number: int) -> bool:
    if not database_enabled():
        return False

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        status_placeholder = "?" if dialect == "sqlite" else "%s"
        if dialect == "sqlite":
            cursor.execute(
                f"""
                UPDATE auction_items
                SET
                    status = {status_placeholder},
                    updated_at = CURRENT_TIMESTAMP
                WHERE lot_number = {placeholder}
                """,
                (ITEM_STATUS_REMOVED, lot_number),
            )
        else:
            cursor.execute(
                f"""
                UPDATE auction_items
                SET
                    status = {status_placeholder}
                WHERE lot_number = {placeholder}
                """,
                (ITEM_STATUS_REMOVED, lot_number),
            )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def fetch_export_rows_for_lots(lot_numbers: list[int]) -> list[list[str]]:
    if not lot_numbers:
        return []

    if not database_enabled():
        ensure_csv_exists()
        wanted = {str(lot_number) for lot_number in lot_numbers}
        with CSV_PATH.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            return [row for row in reader if row and row[0] in wanted]

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

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
            WHERE status != 'removed'
              AND lot_number IN ({placeholders})
            ORDER BY lot_number
            """,
            tuple(lot_numbers),
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

def ensure_lot_state() -> None:
    if not LOT_STATE_PATH.exists():
        LOT_STATE_PATH.write_text(
            json.dumps({"last_lot": DEFAULT_STARTING_LOT}, indent=2),
            encoding="utf-8",
        )


def get_last_lot() -> int:
    return fetch_last_lot_from_store()


def get_next_lot_preview() -> int:
    return get_last_lot() + 1


def reserve_next_lot() -> int:
    with state_lock(LOT_LOCK_PATH):
        if database_enabled():
            return fetch_last_lot_from_store() + 1

        ensure_lot_state()
        data = json.loads(LOT_STATE_PATH.read_text(encoding="utf-8"))
        next_lot = int(data.get("last_lot", DEFAULT_STARTING_LOT)) + 1
        data["last_lot"] = next_lot
        LOT_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return next_lot


def ensure_csv_exists() -> None:
    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)


def append_csv_row(row: list[str]) -> None:
    ensure_csv_exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row)


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

def optimize_image(source_path: Path, destination_path: Path) -> Path:
    """
    Open an uploaded image, auto-rotate it, convert to RGB if needed,
    resize to a sane max dimension, and save as optimized JPEG.
    Returns the final saved path.
    """
    with Image.open(source_path) as img:
        img = ImageOps.exif_transpose(img)

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")

        width, height = img.size
        longest_side = max(width, height)

        if longest_side > MAX_IMAGE_DIMENSION:
            scale = MAX_IMAGE_DIMENSION / float(longest_side)
            new_size = (int(width * scale), int(height * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        final_path = destination_path.with_suffix(".jpg")
        img.save(
            final_path,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=True,
        )

    return final_path

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

def ensure_auction_photo_state() -> None:
    if not AUCTION_PHOTO_STATE_PATH.exists():
        AUCTION_PHOTO_STATE_PATH.write_text("{}", encoding="utf-8")


def get_next_auction_photo_index(auction_number: str) -> int:
    ensure_auction_photo_state()
    data = json.loads(AUCTION_PHOTO_STATE_PATH.read_text(encoding="utf-8"))
    current = int(data.get(str(auction_number), 0))
    return current + 1


def reserve_next_auction_photo_index(auction_number: str) -> int:
    with state_lock(AUCTION_PHOTO_LOCK_PATH):
        ensure_auction_photo_state()
        data = json.loads(AUCTION_PHOTO_STATE_PATH.read_text(encoding="utf-8"))
        current = int(data.get(str(auction_number), 0))
        next_index = current + 1
        data[str(auction_number)] = next_index
        AUCTION_PHOTO_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return next_index


def ensure_ftp_upload_state() -> None:
    if not FTP_UPLOAD_STATE_PATH.exists():
        FTP_UPLOAD_STATE_PATH.write_text("{}", encoding="utf-8")


def get_ftp_upload_record(lot_number: int | str) -> dict[str, object] | None:
    ensure_ftp_upload_state()
    data = json.loads(FTP_UPLOAD_STATE_PATH.read_text(encoding="utf-8"))
    record = data.get(str(lot_number))
    return record if isinstance(record, dict) else None


def record_ftp_upload(
    lot_number: int,
    auction_number: str,
    auction_photo_index: int,
    remote_names: list[str],
) -> None:
    with state_lock(FTP_UPLOAD_STATE_LOCK_PATH):
        ensure_ftp_upload_state()
        data = json.loads(FTP_UPLOAD_STATE_PATH.read_text(encoding="utf-8"))
        data[str(lot_number)] = {
            "auction_number": str(auction_number),
            "auction_photo_index": int(auction_photo_index),
            "remote_names": list(remote_names),
        }
        FTP_UPLOAD_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def delete_ftp_upload_record(lot_number: int | str) -> None:
    with state_lock(FTP_UPLOAD_STATE_LOCK_PATH):
        ensure_ftp_upload_state()
        data = json.loads(FTP_UPLOAD_STATE_PATH.read_text(encoding="utf-8"))
        data.pop(str(lot_number), None)
        FTP_UPLOAD_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def connect_ftp():
    host = os.getenv("FTP_HOST", "").strip()
    username = os.getenv("FTP_USERNAME", "").strip()
    password = os.getenv("FTP_PASSWORD", "").strip()
    port = int(os.getenv("FTP_PORT", "21"))
    use_tls = os.getenv("FTP_TLS", "false").lower() == "true"

    if not host or not username or not password:
        raise ValueError("FTP credentials are missing in .env")

    if use_tls:
        ftp = FTP_TLS()
        ftp.connect(host, port, timeout=30)
        ftp.login(username, password)
        ftp.prot_p()
    else:
        ftp = FTP()
        ftp.connect(host, port, timeout=30)
        ftp.login(username, password)

    return ftp


def ensure_remote_dir(ftp, remote_dir: str) -> None:
    try:
        ftp.cwd(remote_dir)
        return
    except error_perm:
        pass

    ftp.mkd(remote_dir)
    ftp.cwd(remote_dir)


def upload_lot_photos_to_auctionninja(
    local_files: list[Path],
    auction_number: str,
    auction_photo_index: int,
) -> list[str]:
    """
    Upload files to AuctionNinja naming format:
    folder: auction_number
    files: {auction_photo_index}_1.jpg, {auction_photo_index}_2.jpg, ...
    """
    if not local_files:
        return []

    uploaded_names: list[str] = []
    ftp = connect_ftp()

    try:
        ensure_remote_dir(ftp, str(auction_number))

        for i, local_file in enumerate(sorted(local_files), start=1):
            remote_name = f"{auction_photo_index}_{i}.jpg"
            with local_file.open("rb") as f:
                ftp.storbinary(f"STOR {remote_name}", f)
            uploaded_names.append(remote_name)
            app.logger.info("Uploaded %s as %s/%s", local_file, auction_number, remote_name)

    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    return uploaded_names


def delete_lot_photos_from_auctionninja(
    auction_number: str,
    remote_names: list[str],
) -> tuple[list[str], list[str]]:
    if not remote_names:
        return [], []

    deleted_names: list[str] = []
    missing_names: list[str] = []
    ftp = connect_ftp()

    try:
        ftp.cwd(str(auction_number))

        for remote_name in remote_names:
            try:
                ftp.delete(remote_name)
                deleted_names.append(remote_name)
                app.logger.info("Deleted remote file %s/%s", auction_number, remote_name)
            except error_perm as exc:
                if str(exc).startswith("550"):
                    missing_names.append(remote_name)
                    app.logger.warning(
                        "Remote file missing during delete: %s/%s",
                        auction_number,
                        remote_name,
                    )
                else:
                    raise
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    return deleted_names, missing_names

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


@app.route("/", methods=["GET"])
def index():
    active_draft = get_active_draft()
    return render_template(
        "index.html",
        next_lot=get_next_lot_preview(),
        csv_path=CSV_PATH.name,
        active_draft=active_draft,
        database_enabled=database_enabled(),
        storage_label=database_label(),
    )


@app.route("/export_csv", methods=["GET"])
def export_csv():
    rows = fetch_export_rows()
    if not rows:
        flash("There are no saved items to export yet.")
        return redirect(url_for("index"))

    if not database_enabled() and CSV_PATH.exists():
        return send_file(CSV_PATH, as_attachment=True, download_name=CSV_PATH.name)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADER)
    writer.writerows(rows)

    filename = f"auction_items_export_{time.strftime('%Y%m%d')}.csv"
    mark_lots_as_published(
        lot_numbers=lot_numbers_from_rows(rows),
        export_batch_name=filename,
    )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/manage_items", methods=["GET"])
def manage_items():
    if not database_enabled():
        flash("Batch item management is available when DATABASE_URL is configured.")
        return redirect(url_for("index"))

    current_filter = normalize_manage_filter(request.args.get("status", "active"))
    items = fetch_manage_items(current_filter)
    return render_template(
        "manage_items.html",
        items=items,
        current_filter=current_filter,
    )


@app.route("/items/<int:lot_number>/edit", methods=["GET"])
def edit_saved_item(lot_number: int):
    if not database_enabled():
        flash("Saved item editing is available when DATABASE_URL is configured.")
        return redirect(url_for("index"))

    item = fetch_saved_item(lot_number)
    if not item or item.get("status") == ITEM_STATUS_REMOVED:
        flash(f"Lot {lot_number} was not found.")
        return redirect(url_for("manage_items"))

    image_folder = item.get("image_folder", "")
    saved_files = load_saved_files_for_temp_id(image_folder)
    return render_template(
        "saved_item_edit.html",
        item=item,
        form=form_from_saved_item(item),
        categories=DEFAULT_CATEGORIES,
        image_files=[p.name for p in saved_files],
        image_url_prefix=f"/uploads/{image_folder}/" if image_folder else "",
    )


@app.route("/items/<int:lot_number>/update", methods=["POST"])
def update_saved_item(lot_number: int):
    if not database_enabled():
        flash("Saved item editing is available when DATABASE_URL is configured.")
        return redirect(url_for("index"))

    item = fetch_saved_item(lot_number)
    if not item or item.get("status") == ITEM_STATUS_REMOVED:
        flash(f"Lot {lot_number} was not found.")
        return redirect(url_for("manage_items"))

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
        )

    new_status = update_saved_item_record(lot_number, form)
    if new_status == ITEM_STATUS_NEEDS_UPDATE:
        flash(f"Updated lot {lot_number}. Status changed to needs_update so it can be re-exported.")
    else:
        flash(f"Updated lot {lot_number}.")
    return redirect(url_for("manage_items"))


@app.route("/items/<int:lot_number>/remove", methods=["POST"])
def remove_saved_item(lot_number: int):
    if not database_enabled():
        flash("Saved item management is available when DATABASE_URL is configured.")
        return redirect(url_for("index"))

    item = fetch_saved_item(lot_number)
    if not item or item.get("status") == ITEM_STATUS_REMOVED:
        flash(f"Lot {lot_number} was not found.")
        return redirect(url_for("manage_items"))

    if mark_item_removed(lot_number):
        flash(f"Removed lot {lot_number} from future exports.")
    else:
        flash(f"Lot {lot_number} could not be removed.")
    return redirect(url_for("manage_items"))


@app.route("/export_selected_csv", methods=["POST"])
def export_selected_csv():
    selected_lots = sorted(
        {
            int(value)
            for value in request.form.getlist("lot_numbers")
            if str(value).isdigit()
        }
    )

    if not selected_lots:
        flash("Select at least one lot to export.")
        return redirect(url_for("manage_items"))

    rows = fetch_export_rows_for_lots(selected_lots)
    if not rows:
        flash("The selected lots could not be exported.")
        return redirect(url_for("manage_items"))

    first_lot = selected_lots[0]
    last_lot = selected_lots[-1]
    filename = f"auction_items_batch_{first_lot}-{last_lot}_{time.strftime('%Y%m%d')}.csv"

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADER)
    writer.writerows(rows)
    mark_lots_as_published(
        lot_numbers=selected_lots,
        export_batch_name=filename,
    )

    return Response(
        output.getvalue(),
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

    auction_number = os.getenv("AUCTION_NUMBER", "").strip()
    uploaded_names = []

    if auction_number:
        try:
            auction_photo_index = reserve_next_auction_photo_index(auction_number)
            local_jpgs = sorted([p for p in final_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"])
            uploaded_names = upload_lot_photos_to_auctionninja(
                local_files=local_jpgs,
                auction_number=auction_number,
                auction_photo_index=auction_photo_index,
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
        if database_enabled():
            flash(
                f"Saved lot {csv_lot_number} to the database. "
                f"Download the AuctionNinja CSV from the home page when ready. "
                f"Images stored in: {final_dir.name}"
            )
        else:
            flash(f"Saved lot {csv_lot_number}. Images stored in: {final_dir.name}")

    return redirect(url_for("index"))


@app.route("/uploads/<temp_id>/<filename>")
def uploaded_file(temp_id: str, filename: str):
    return send_from_directory(UPLOADS_DIR / temp_id, filename)


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


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    app.run(host=host, port=port, debug=debug)
