import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
EXPORTS_DIR = DATA_DIR / "exports"

DATA_DIR.mkdir(parents=True, exist_ok=True)
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

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

_DB_INITIALIZED = False
_DB_INITIALIZED_URL = ""


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{DATA_DIR / 'auction_items.db'}"
    return url


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
    global _DB_INITIALIZED, _DB_INITIALIZED_URL
    current_database_url = get_database_url()
    if _DB_INITIALIZED and _DB_INITIALIZED_URL == current_database_url:
        return

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
            _ensure_sqlite_column(cursor, "auction_items", "listing_strategy", "TEXT NOT NULL DEFAULT 'auction'")
            _ensure_sqlite_column(cursor, "auction_items", "platform_data", "TEXT")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS item_platform_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lot_number INTEGER NOT NULL,
                    platform_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    remote_id TEXT,
                    published_at TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(lot_number, platform_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS integrations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform_id TEXT NOT NULL UNIQUE,
                    access_token TEXT,
                    refresh_token TEXT,
                    settings_json TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
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
                    owner_token TEXT,
                    seller_notes TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    form_json TEXT NOT NULL,
                    revision_request TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            _ensure_sqlite_column(cursor, "active_drafts", "owner_token", "TEXT")
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
            _ensure_mysql_column(cursor, "auction_items", "listing_strategy", "VARCHAR(32) NOT NULL DEFAULT 'auction'")
            _ensure_mysql_column(cursor, "auction_items", "platform_data", "LONGTEXT NULL")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS item_platform_status (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    lot_number INT NOT NULL,
                    platform_id VARCHAR(64) NOT NULL,
                    status VARCHAR(64) NOT NULL,
                    remote_id VARCHAR(255) NULL,
                    published_at TIMESTAMP NULL DEFAULT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_item_platform (lot_number, platform_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS integrations (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    platform_id VARCHAR(64) NOT NULL UNIQUE,
                    access_token TEXT NULL,
                    refresh_token TEXT NULL,
                    settings_json LONGTEXT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )
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
                    owner_token VARCHAR(255) NULL,
                    seller_notes TEXT NOT NULL,
                    options_json LONGTEXT NOT NULL,
                    form_json LONGTEXT NOT NULL,
                    revision_request TEXT NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )
            _ensure_mysql_column(cursor, "active_drafts", "owner_token", "VARCHAR(255) NULL")
            _bootstrap_auction_rows(cursor, dialect)
            _backfill_auction_scope(cursor, dialect)
        connection.commit()
        _DB_INITIALIZED = True
        _DB_INITIALIZED_URL = current_database_url
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
        cursor.execute("UPDATE auction_items SET auction_id = ? WHERE auction_id IS NULL", (default_id,))
        cursor.execute("UPDATE export_batches SET auction_id = ? WHERE auction_id IS NULL", (default_id,))
        cursor.execute("UPDATE ftp_uploads SET auction_id = ? WHERE auction_id IS NULL", (default_id,))
    else:
        cursor.execute("UPDATE auction_items SET auction_id = %s WHERE auction_id IS NULL", (default_id,))
        cursor.execute("UPDATE export_batches SET auction_id = %s WHERE auction_id IS NULL", (default_id,))
        cursor.execute("UPDATE ftp_uploads SET auction_id = %s WHERE auction_id IS NULL", (default_id,))


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
                f"SELECT COUNT(*) AS export_count FROM export_batches WHERE auction_id = {placeholder}",
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
                f"UPDATE auctions SET is_current = 1, updated_at = CURRENT_TIMESTAMP WHERE id = {placeholder}",
                (auction_id,),
            )
        else:
            cursor.execute("UPDATE auctions SET is_current = 0")
            cursor.execute(
                f"UPDATE auctions SET is_current = 1 WHERE id = {placeholder}",
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
                f"UPDATE auctions SET status = {placeholders[0]}, updated_at = CURRENT_TIMESTAMP WHERE id = {placeholders[1]}",
                (status, auction_id),
            )
        else:
            cursor.execute(
                f"UPDATE auctions SET status = {placeholders[0]} WHERE id = {placeholders[1]}",
                (status, auction_id),
            )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def update_auction_last_lot_override(auction_id: int, last_lot: int) -> None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    
    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        if dialect == "sqlite":
            cursor.execute(
                f"UPDATE auctions SET last_lot_override = {placeholder}, updated_at = CURRENT_TIMESTAMP WHERE id = {placeholder}",
                (last_lot, auction_id)
            )
        else:
            cursor.execute(
                f"UPDATE auctions SET last_lot_override = {placeholder} WHERE id = {placeholder}",
                (last_lot, auction_id)
            )
        connection.commit()
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
                "UPDATE ftp_uploads SET auction_id = ? WHERE lot_number = ? AND auction_id = ?",
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
                "UPDATE ftp_uploads SET auction_id = %s WHERE lot_number = %s AND auction_id = %s",
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
        override_val = _extract_row_value(row_override, "last_lot_override", 0)
        if override_val is not None:
            override_lot = int(override_val)

        cursor.execute(f"SELECT MAX(lot_number) AS max_lot FROM auction_items WHERE auction_id = {placeholder}", (current_auction_id,))
        row_max = cursor.fetchone()
        max_lot = 0
        max_val = _extract_row_value(row_max, "max_lot", 0)
        if max_val is not None:
            max_lot = int(max_val)
    finally:
        connection.close()

    return max(override_lot, max_lot, DEFAULT_STARTING_LOT)


def get_last_lot() -> int:
    return fetch_last_lot_from_store()


def reserve_next_lot() -> int:
    candidate = fetch_last_lot_from_store() + 1
    while fetch_saved_item(candidate) is not None:
        candidate += 1
    return candidate


def get_next_lot_preview() -> int:
    candidate = get_last_lot() + 1
    while fetch_saved_item(candidate) is not None:
        candidate += 1
    return candidate


def current_auction_number_for_upload() -> str:
    return str(get_current_auction_id())


def combine_item_notes(form: dict[str, str]) -> str:
    return form.get("Item Notes", "").strip()


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
        "listing_strategy": form.get("Listing Strategy", "auction"),
        "platform_data": json.dumps({
            "ebay_category_id": form.get("eBay Category ID", ""),
            "ebay_seo_title": form.get("eBay SEO Title", ""),
            "ebay_category_suggestion": form.get("eBay Category Suggestion", ""),
            "ebay_item_specifics": form.get("eBay Item Specifics", "{}"),
            "etsy_taxonomy_id": form.get("Etsy Taxonomy ID", ""),
            "etsy_tags": form.get("Etsy Tags", ""),
            "etsy_materials": form.get("Etsy Materials", ""),
            "etsy_who_made": form.get("Etsy Who Made", "someone_else"),
            "etsy_when_made": form.get("Etsy When Made", "2020_2026"),
            "etsy_is_supply": form.get("Etsy Is Supply", "") == "yes",
            "etsy_price": form.get("Price", "0.00"),
            "etsy_price_rationale": form.get("Price Rationale", ""),
            "etsy_quantity": form.get("Quantity", "1"),
            "publish_to_ebay": form.get("Publish to eBay", "") == "yes",
            "publish_to_etsy": form.get("Publish to Etsy", "") == "yes",
        }) if form.get("Listing Strategy") != "auction" else "",
    }


def append_item_record(record: dict[str, str]) -> None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        auction_id = int(record.get("auction_id", get_current_auction_id()))
        placeholders = ", ".join(["?"] * 20) if dialect == "sqlite" else ", ".join(["%s"] * 20)
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
                image_folder,
                listing_strategy,
                platform_data
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
                record.get("listing_strategy", "auction"),
                record.get("platform_data", ""),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def initialize_platform_status(lot_number: int, platforms: list[str]) -> None:
    if not platforms:
        return
        
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        for platform_id in platforms:
            if dialect == "sqlite":
                cursor.execute(
                    """
                    INSERT INTO item_platform_status (lot_number, platform_id, status, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(lot_number, platform_id) DO UPDATE SET
                        status=excluded.status,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (lot_number, platform_id, "pending")
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO item_platform_status (lot_number, platform_id, status)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        status=VALUES(status)
                    """,
                    (lot_number, platform_id, "pending")
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
        if dialect == "sqlite":
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


def fetch_export_rows_for_lots(lot_numbers: list[int]) -> list[list[str]]:
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
                ai.lot_number,
                ai.title,
                ai.category,
                ai.shipping_available,
                ai.status,
                ai.image_folder,
                ai.created_at,
                ai.updated_at,
                ai.published_at,
                ai.last_export_batch,
                GROUP_CONCAT(ips.platform || ':' || ips.status) as platform_statuses
            FROM auction_items ai
            LEFT JOIN item_platform_status ips ON ai.lot_number = ips.lot_number
            WHERE ai.auction_id = {("?" if dialect == "sqlite" else "%s")}
              AND ai.status IN ({placeholders})
            GROUP BY ai.lot_number
            ORDER BY ai.lot_number
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
                published_at,
                listing_strategy
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
    
    # Fetch platform statuses for these items
    if items:
        lot_numbers = [int(item["lot_number"]) for item in items]
        platform_map = fetch_platform_statuses_for_lots(lot_numbers)
        for item in items:
            item["platform_statuses"] = platform_map.get(int(item["lot_number"]), [])
            
    return items


def fetch_platform_statuses_for_lots(lot_numbers: list[int]) -> dict[int, list[dict[str, str]]]:
    if not lot_numbers:
        return {}
        
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    
    try:
        cursor = connection.cursor()
        placeholders = ", ".join(["?"] * len(lot_numbers)) if dialect == "sqlite" else ", ".join(["%s"] * len(lot_numbers))
        cursor.execute(
            f"""
            SELECT lot_number, platform_id, status, remote_id, updated_at
            FROM item_platform_status
            WHERE lot_number IN ({placeholders})
            """,
            tuple(lot_numbers)
        )
        rows = cursor.fetchall()
        
        result: dict[int, list[dict[str, str]]] = {}
        for row in rows:
            lot_num = int(_extract_row_value(row, "lot_number", 0, 0))
            if lot_num not in result:
                result[lot_num] = []
            
            status_info = {
                "platform_id": str(_extract_row_value(row, "platform_id", 1, "")),
                "status": str(_extract_row_value(row, "status", 2, "")),
                "remote_id": str(_extract_row_value(row, "remote_id", 3, "")),
                "updated_at": str(_extract_row_value(row, "updated_at", 4, "")),
            }
            result[lot_num].append(status_info)
        return result
    finally:
        connection.close()


def fetch_recent_retail_items(limit: int = 10) -> list[dict[str, Any]]:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None
    
    try:
        cursor = connection.cursor()
        limit_placeholder = "?" if dialect == "sqlite" else "%s"
        cursor.execute(
            f"""
            SELECT lot_number, title, status, category, updated_at, listing_strategy, platform_data
            FROM auction_items
            WHERE listing_strategy = 'retail'
            ORDER BY updated_at DESC
            LIMIT {limit_placeholder}
            """,
            (limit,)
        )
        records = cursor.fetchall()
        
        items = []
        for record in records:
            if isinstance(record, sqlite3.Row):
                item = {key: record[key] for key in record.keys()}
            else:
                # Basic dict/tuple extraction fallback
                item = {
                    "lot_number": record[0],
                    "title": record[1],
                    "status": record[2],
                    "category": record[3],
                    "updated_at": record[4],
                    "listing_strategy": record[5],
                    "platform_data": record[6],
                }
            items.append(item)
            
        if items:
            lot_numbers = [int(item["lot_number"]) for item in items]
            platform_map = fetch_platform_statuses_for_lots(lot_numbers)
            for item in items:
                item["platform_statuses"] = platform_map.get(int(item["lot_number"]), [])
        
        return items
    finally:
        connection.close()


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
        "listing_strategy": form.get("Listing Strategy", "auction"),
        "platform_data": json.dumps({
            "ebay_category_id": form.get("eBay Category ID", ""),
            "etsy_taxonomy_id": form.get("Etsy Taxonomy ID", ""),
        }) if form.get("Listing Strategy") == "retail" else "",
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
        "listing_strategy",
        "platform_data",
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
                    listing_strategy = ?,
                    platform_data = ?,
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
                    updated_fields.get("listing_strategy", "auction"),
                    updated_fields.get("platform_data", ""),
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
                    status = %s,
                    listing_strategy = %s,
                    platform_data = %s
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
                    updated_fields.get("listing_strategy", "auction"),
                    updated_fields.get("platform_data", ""),
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


def get_next_auction_photo_index(auction_number: str) -> int:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        cursor.execute(
            f"SELECT last_index FROM auction_photo_counters WHERE auction_number = {placeholder}",
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
            f"SELECT last_index FROM auction_photo_counters WHERE auction_number = {placeholder}",
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
        cursor.execute(
            f"DELETE FROM ftp_uploads WHERE lot_number = {placeholder} AND auction_id = {placeholder}",
            (int(lot_number), current_auction_id),
        )
        connection.commit()
    finally:
        connection.close()


def set_active_draft(
    temp_id: str,
    seller_notes: str,
    options: list[dict],
    form: dict[str, str],
    owner_token: str = "",
    revision_request: str = "",
) -> None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    options_json = json.dumps(options)
    form_json = json.dumps(form)
    slot_name = temp_id

    try:
        cursor = connection.cursor()
        if dialect == "sqlite":
            cursor.execute(
                """
                INSERT OR REPLACE INTO active_drafts (
                    slot_name,
                    temp_id,
                    owner_token,
                    seller_notes,
                    options_json,
                    form_json,
                    revision_request,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (slot_name, temp_id, owner_token, seller_notes, options_json, form_json, revision_request),
            )
        else:
            cursor.execute(
                """
                INSERT INTO active_drafts (
                    slot_name,
                    temp_id,
                    owner_token,
                    seller_notes,
                    options_json,
                    form_json,
                    revision_request
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    temp_id = VALUES(temp_id),
                    owner_token = VALUES(owner_token),
                    seller_notes = VALUES(seller_notes),
                    options_json = VALUES(options_json),
                    form_json = VALUES(form_json),
                    revision_request = VALUES(revision_request)
                """,
                (slot_name, temp_id, owner_token, seller_notes, options_json, form_json, revision_request),
            )
        connection.commit()
    finally:
        connection.close()


def clear_active_draft(temp_id: str, owner_token: str | None = None) -> None:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    if not temp_id:
        return

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        if owner_token:
            cursor.execute(
                f"DELETE FROM active_drafts WHERE temp_id = {placeholder} AND owner_token = {placeholder}",
                (temp_id, owner_token),
            )
        else:
            cursor.execute(
                f"DELETE FROM active_drafts WHERE temp_id = {placeholder}",
                (temp_id,),
            )
        connection.commit()
    finally:
        connection.close()


def fetch_active_draft(temp_id: str, owner_token: str | None = None) -> dict[str, Any] | None:
    if not temp_id:
        return None

    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        if owner_token:
            cursor.execute(
                f"""
                SELECT temp_id, owner_token, seller_notes, options_json, form_json, revision_request
                FROM active_drafts
                WHERE temp_id = {placeholder} AND owner_token = {placeholder}
                """,
                (temp_id, owner_token),
            )
        else:
            cursor.execute(
                f"""
                SELECT temp_id, owner_token, seller_notes, options_json, form_json, revision_request
                FROM active_drafts
                WHERE temp_id = {placeholder}
                """,
                (temp_id,),
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

    return {
        "temp_id": temp_id,
        "owner_token": str(raw.get("owner_token", "")).strip(),
        "seller_notes": str(raw.get("seller_notes", "")).strip(),
        "options": options,
        "form": form,
        "revision_request": str(raw.get("revision_request", "")).strip(),
    }


def fetch_all_active_drafts(owner_token: str | None = None) -> list[dict[str, Any]]:
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    assert connection is not None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        if owner_token:
            cursor.execute(
                f"""
                SELECT temp_id, owner_token, seller_notes, options_json, form_json, revision_request, updated_at
                FROM active_drafts
                WHERE owner_token = {placeholder}
                ORDER BY updated_at DESC
                """,
                (owner_token,),
            )
        else:
            cursor.execute(
                """
                SELECT temp_id, owner_token, seller_notes, options_json, form_json, revision_request, updated_at
                FROM active_drafts
                ORDER BY updated_at DESC
                """
            )
        records = cursor.fetchall()
    finally:
        connection.close()

    drafts = []
    for record in records:
        if isinstance(record, sqlite3.Row):
            raw = {key: record[key] for key in record.keys()}
        elif isinstance(record, dict):
            raw = dict(record)
        else:
            continue

        temp_id = str(raw.get("temp_id", "")).strip()
        if not temp_id:
            continue

        try:
            options = json.loads(str(raw.get("options_json", "[]")))
            form = json.loads(str(raw.get("form_json", "{}")))
        except json.JSONDecodeError:
            continue

        if not isinstance(options, list) or not isinstance(form, dict):
            continue

        drafts.append({
            "temp_id": temp_id,
            "owner_token": str(raw.get("owner_token", "")).strip(),
            "seller_notes": str(raw.get("seller_notes", "")).strip(),
            "options": options,
            "form": form,
            "revision_request": str(raw.get("revision_request", "")).strip(),
            "updated_at": str(raw.get("updated_at", "")).strip(),
        })
    return drafts


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


def get_platform_credentials(platform_id: str) -> dict[str, Any] | None:
    """Retrieves credentials and settings for a given platform."""
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    if not connection:
        return None

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        cursor.execute(
            f"SELECT access_token, refresh_token, settings_json FROM integrations WHERE platform_id = {placeholder}",
            (platform_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None

        if isinstance(row, sqlite3.Row):
            data = dict(row)
        else:
            data = row

        return {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "settings": json.loads(data.get("settings_json") or "{}")
        }
    except Exception as exc:
        print(f"[Database] Error fetching credentials for {platform_id}: {exc}")
        return None
    finally:
        connection.close()


def update_platform_status(lot_number: int, platform_id: str, status: str, remote_id: str = None) -> bool:
    """Updates the publishing status of an item on a specific platform."""
    ensure_item_store_ready()
    connection, dialect = connect_item_store()
    if not connection:
        return False

    try:
        cursor = connection.cursor()
        placeholder = "?" if dialect == "sqlite" else "%s"
        
        if remote_id:
            cursor.execute(
                f"""
                UPDATE item_platform_status 
                SET status = {placeholder}, remote_id = {placeholder}, published_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE lot_number = {placeholder} AND platform_id = {placeholder}
                """,
                (status, remote_id, lot_number, platform_id)
            )
        else:
            cursor.execute(
                f"""
                UPDATE item_platform_status 
                SET status = {placeholder}, updated_at = CURRENT_TIMESTAMP
                WHERE lot_number = {placeholder} AND platform_id = {placeholder}
                """,
                (status, lot_number, platform_id)
            )
        connection.commit()
        return True
    except Exception as exc:
        print(f"[Database] Error updating platform status for lot {lot_number} on {platform_id}: {exc}")
        return False
    finally:
        connection.close()
