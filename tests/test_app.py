import os
import sqlite3
from pathlib import Path

import pytest

import app as app_module


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    uploads_dir = data_dir / "uploads"
    exports_dir = data_dir / "exports"
    data_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    original_paths = {
        "DATA_DIR": app_module.DATA_DIR,
        "UPLOADS_DIR": app_module.UPLOADS_DIR,
        "EXPORTS_DIR": app_module.EXPORTS_DIR,
        "CSV_PATH": app_module.CSV_PATH,
        "LOT_STATE_PATH": app_module.LOT_STATE_PATH,
        "AUCTION_PHOTO_STATE_PATH": app_module.AUCTION_PHOTO_STATE_PATH,
        "FTP_UPLOAD_STATE_PATH": app_module.FTP_UPLOAD_STATE_PATH,
        "ACTIVE_DRAFT_STATE_PATH": app_module.ACTIVE_DRAFT_STATE_PATH,
        "LOT_LOCK_PATH": app_module.LOT_LOCK_PATH,
        "AUCTION_PHOTO_LOCK_PATH": app_module.AUCTION_PHOTO_LOCK_PATH,
        "FTP_UPLOAD_STATE_LOCK_PATH": app_module.FTP_UPLOAD_STATE_LOCK_PATH,
        "ACTIVE_DRAFT_STATE_LOCK_PATH": app_module.ACTIVE_DRAFT_STATE_LOCK_PATH,
    }

    app_module.DATA_DIR = data_dir
    app_module.UPLOADS_DIR = uploads_dir
    app_module.EXPORTS_DIR = exports_dir
    app_module.CSV_PATH = data_dir / "auction_items.csv"
    app_module.LOT_STATE_PATH = data_dir / "lot_state.json"
    app_module.AUCTION_PHOTO_STATE_PATH = data_dir / "auction_photo_state.json"
    app_module.FTP_UPLOAD_STATE_PATH = data_dir / "ftp_upload_state.json"
    app_module.ACTIVE_DRAFT_STATE_PATH = data_dir / "active_draft.json"
    app_module.LOT_LOCK_PATH = data_dir / "lot_state.lock"
    app_module.AUCTION_PHOTO_LOCK_PATH = data_dir / "auction_photo_state.lock"
    app_module.FTP_UPLOAD_STATE_LOCK_PATH = data_dir / "ftp_upload_state.lock"
    app_module.ACTIVE_DRAFT_STATE_LOCK_PATH = data_dir / "active_draft.lock"
    app_module.app.config["TESTING"] = True
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("AUCTION_NUMBER", "")
    monkeypatch.setenv("FTP_HOST", "")
    monkeypatch.setenv("FTP_PORT", "21")
    monkeypatch.setenv("FTP_USERNAME", "")
    monkeypatch.setenv("FTP_PASSWORD", "")
    monkeypatch.setenv("FTP_TLS", "false")

    yield {
        "client": app_module.app.test_client(),
        "uploads_dir": uploads_dir,
        "exports_dir": exports_dir,
    }

    for name, value in original_paths.items():
        setattr(app_module, name, value)


def test_reserve_next_lot_clears_stale_lock(test_env):
    app_module.LOT_LOCK_PATH.write_text("999999", encoding="utf-8")

    next_lot = app_module.reserve_next_lot()

    assert next_lot == 2000
    assert not app_module.LOT_LOCK_PATH.exists()
    assert app_module.get_last_lot() == 2000


def test_validate_save_form_rejects_blank_title_and_bad_estimates():
    errors = app_module.validate_save_form(
        {
            "Title": "   ",
            "Low Estimate ($)": "200",
            "High Estimate ($)": "100",
        }
    )

    assert "Title is required before saving." in errors
    assert "Low Estimate ($) cannot be greater than High Estimate ($)." in errors


def test_validate_save_form_rejects_non_numeric_estimates():
    errors = app_module.validate_save_form(
        {
            "Title": "Vase",
            "Low Estimate ($)": "abc",
            "High Estimate ($)": "$25",
        }
    )

    assert "Low Estimate ($) must be a number if provided." in errors
    assert "High Estimate ($) must be a number if provided." not in errors


def test_save_with_invalid_data_does_not_write_csv(test_env):
    draft_dir = test_env["uploads_dir"] / "draft123"
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "photo.jpg").write_bytes(b"fake image")

    response = test_env["client"].post(
        "/save",
        data={
            "temp_id": "draft123",
            "seller_notes": "From seller",
            "option_1_identification": "Option one",
            "option_1_confidence_note": "Likely",
            "option_1_material_notes": "Ceramic",
            "option_1_mark_notes": "Unmarked",
            "option_1_title": "Draft title",
            "option_1_description": "Draft description",
            "option_1_category": "Decorative Arts",
            "option_1_condition_summary": "Visible wear",
            "option_1_keywords": "vase, ceramic",
            "option_2_identification": "",
            "option_2_confidence_note": "",
            "option_2_material_notes": "",
            "option_2_mark_notes": "",
            "option_2_title": "",
            "option_2_description": "",
            "option_2_category": "",
            "option_2_condition_summary": "",
            "option_2_keywords": "",
            "option_3_identification": "",
            "option_3_confidence_note": "",
            "option_3_material_notes": "",
            "option_3_mark_notes": "",
            "option_3_title": "",
            "option_3_description": "",
            "option_3_category": "",
            "option_3_condition_summary": "",
            "option_3_keywords": "",
            "Identification": "Item",
            "Confidence Note": "Likely item",
            "Material Notes": "Ceramic",
            "Mark Notes": "Unmarked",
            "Title": "   ",
            "Description": "Desc",
            "Condition Summary": "Used",
            "Keywords": "decor",
            "Category": "Decorative Arts",
            "Low Estimate ($)": "10",
            "High Estimate ($)": "20",
            "Dimensions - Length": "",
            "Dimensions - Depth": "",
            "Dimensions - Height": "",
            "Reference #": "",
            "Item Notes": "notes",
            "Consigner #": "",
            "Shipping Available": "Yes",
        },
    )

    assert response.status_code == 200
    assert b"Title is required before saving." in response.data
    assert not app_module.CSV_PATH.exists()
    assert draft_dir.exists()


def test_record_and_delete_ftp_upload_record(test_env):
    app_module.record_ftp_upload(
        lot_number=2056,
        auction_number="4",
        auction_photo_index=5,
        remote_names=["5_1.jpg", "5_2.jpg"],
    )

    record = app_module.get_ftp_upload_record(2056)

    assert record is not None
    assert record["auction_number"] == "4"
    assert record["auction_photo_index"] == 5
    assert record["remote_names"] == ["5_1.jpg", "5_2.jpg"]

    app_module.delete_ftp_upload_record(2056)

    assert app_module.get_ftp_upload_record(2056) is None


def test_delete_remote_upload_without_record_flashes_message(test_env):
    response = test_env["client"].post(
        "/delete_remote_upload",
        data={"lot_number": "2056"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"No saved FTP upload record was found for lot 2056." in response.data


def test_index_shows_resume_panel_for_active_draft(test_env):
    draft_dir = test_env["uploads_dir"] / "draft123"
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "photo.jpg").write_bytes(b"fake image")

    app_module.set_active_draft(
        temp_id="draft123",
        seller_notes="Seller note",
        options=[{"rank": 1, "title": "Draft title"}],
        form={"Title": "Draft title"},
    )

    response = test_env["client"].get("/")

    assert response.status_code == 200
    assert b"Resume Last Draft" in response.data
    assert b"Draft photos: 1" in response.data


def test_discard_draft_removes_folder_and_state(test_env):
    draft_dir = test_env["uploads_dir"] / "draft123"
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "photo.jpg").write_bytes(b"fake image")

    app_module.set_active_draft(
        temp_id="draft123",
        seller_notes="Seller note",
        options=[{"rank": 1, "title": "Draft title"}],
        form={"Title": "Draft title"},
    )

    response = test_env["client"].post("/discard_draft", follow_redirects=True)

    assert response.status_code == 200
    assert b"Discarded the last unsaved draft." in response.data
    assert not draft_dir.exists()
    assert app_module.get_active_draft() is None


def test_save_uses_database_when_configured(test_env, tmp_path, monkeypatch):
    draft_dir = test_env["uploads_dir"] / "draft123"
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "photo.jpg").write_bytes(b"fake image")

    db_path = tmp_path / "auction_items.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    response = test_env["client"].post(
        "/save",
        data={
            "temp_id": "draft123",
            "seller_notes": "From seller",
            "option_1_identification": "Option one",
            "option_1_confidence_note": "Likely",
            "option_1_material_notes": "Ceramic",
            "option_1_mark_notes": "Unmarked",
            "option_1_title": "Draft title",
            "option_1_description": "Draft description",
            "option_1_category": "Decorative Arts",
            "option_1_condition_summary": "Visible wear",
            "option_1_keywords": "vase, ceramic",
            "option_2_identification": "",
            "option_2_confidence_note": "",
            "option_2_material_notes": "",
            "option_2_mark_notes": "",
            "option_2_title": "",
            "option_2_description": "",
            "option_2_category": "",
            "option_2_condition_summary": "",
            "option_2_keywords": "",
            "option_3_identification": "",
            "option_3_confidence_note": "",
            "option_3_material_notes": "",
            "option_3_mark_notes": "",
            "option_3_title": "",
            "option_3_description": "",
            "option_3_category": "",
            "option_3_condition_summary": "",
            "option_3_keywords": "",
            "Identification": "Item",
            "Confidence Note": "Likely item",
            "Material Notes": "Ceramic",
            "Mark Notes": "Unmarked",
            "Title": "Blue Vase",
            "Description": "Desc",
            "Condition Summary": "Used",
            "Keywords": "decor",
            "Category": "Decorative Arts",
            "Low Estimate ($)": "",
            "High Estimate ($)": "",
            "Dimensions - Length": "",
            "Dimensions - Depth": "",
            "Dimensions - Height": "",
            "Reference #": "",
            "Item Notes": "notes",
            "Consigner #": "",
            "Shipping Available": "No",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Saved lot 2000 to the database." in response.data
    assert not app_module.CSV_PATH.exists()

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT lot_number, title, item_notes, shipping_available, status FROM auction_items"
        ).fetchone()

    assert row == (2000, "Blue Vase", "notes", "No", "ready")


def test_export_csv_downloads_database_rows(test_env, tmp_path, monkeypatch):
    db_path = tmp_path / "auction_items.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    app_module.append_item_record(
        {
            "lot_number": "2005",
            "title": "Lamp",
            "description": "Brass lamp",
            "condition_notes": "Working",
            "low_estimate": "",
            "high_estimate": "",
            "dimensions_length": "",
            "dimensions_depth": "",
            "dimensions_height": "",
            "tags": "lamp, brass",
            "reference_number": "",
            "item_notes": "tested",
            "consigner_number": "",
            "shipping_available": "No",
            "category": "Decorative Arts",
            "status": "ready",
            "image_folder": "2005_lamp",
        }
    )

    response = test_env["client"].get("/export_csv")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    text = response.data.decode("utf-8")
    assert "Lot Number,Lead,Description" in text
    assert "2005,Lamp,Brass lamp,Working" in text
    export_files = list(test_env["exports_dir"].glob("auction_items_export_*.csv"))
    assert len(export_files) == 1
    assert "2005,Lamp,Brass lamp,Working" in export_files[0].read_text(encoding="utf-8")

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status, last_export_batch, published_at FROM auction_items WHERE lot_number = 2005"
        ).fetchone()

    assert row[0] == "published"
    assert row[1].startswith("auction_items_export_")
    assert row[2] is not None


def test_manage_items_requires_database(test_env):
    response = test_env["client"].get("/manage_items", follow_redirects=True)

    assert response.status_code == 200
    assert b"Batch item management is available when DATABASE_URL is configured." in response.data


def test_manage_items_and_export_selected_csv(test_env, tmp_path, monkeypatch):
    db_path = tmp_path / "auction_items.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    app_module.append_item_record(
        {
            "lot_number": "2005",
            "title": "Lamp",
            "description": "Brass lamp",
            "condition_notes": "Working",
            "low_estimate": "",
            "high_estimate": "",
            "dimensions_length": "",
            "dimensions_depth": "",
            "dimensions_height": "",
            "tags": "lamp, brass",
            "reference_number": "",
            "item_notes": "tested",
            "consigner_number": "",
            "shipping_available": "No",
            "category": "Decorative Arts",
            "status": "ready",
            "image_folder": "2005_lamp",
        }
    )
    app_module.append_item_record(
        {
            "lot_number": "2006",
            "title": "Chair",
            "description": "Wood chair",
            "condition_notes": "Vintage wear",
            "low_estimate": "",
            "high_estimate": "",
            "dimensions_length": "",
            "dimensions_depth": "",
            "dimensions_height": "",
            "tags": "chair, wood",
            "reference_number": "",
            "item_notes": "solid",
            "consigner_number": "",
            "shipping_available": "No",
            "category": "Furniture",
            "status": "ready",
            "image_folder": "2006_chair",
        }
    )

    manage_response = test_env["client"].get("/manage_items")
    assert manage_response.status_code == 200
    assert b"Manage Export Batches" in manage_response.data
    assert b"Lamp" in manage_response.data
    assert b"Chair" in manage_response.data

    export_response = test_env["client"].post(
        "/export_selected_csv",
        data={"lot_numbers": ["2006"]},
    )

    assert export_response.status_code == 200
    assert export_response.mimetype == "text/csv"
    text = export_response.data.decode("utf-8")
    assert "2006,Chair,Wood chair,Vintage wear" in text
    assert "2005,Lamp,Brass lamp,Working" not in text
    batch_files = list(test_env["exports_dir"].glob("auction_items_batch_2006-2006_*.csv"))
    assert len(batch_files) == 1
    assert "2006,Chair,Wood chair,Vintage wear" in batch_files[0].read_text(encoding="utf-8")

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT lot_number, status, last_export_batch FROM auction_items ORDER BY lot_number"
        ).fetchall()

    assert rows[0] == (2005, "ready", None)
    assert rows[1][0] == 2006
    assert rows[1][1] == "published"
    assert rows[1][2].startswith("auction_items_batch_2006-2006_")


def test_edit_saved_item_page_loads(test_env, tmp_path, monkeypatch):
    db_path = tmp_path / "auction_items.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    app_module.append_item_record(
        {
            "lot_number": "2010",
            "title": "Mirror",
            "description": "Wall mirror",
            "condition_notes": "Good",
            "low_estimate": "",
            "high_estimate": "",
            "dimensions_length": "",
            "dimensions_depth": "",
            "dimensions_height": "",
            "tags": "mirror",
            "reference_number": "",
            "item_notes": "hallway",
            "consigner_number": "",
            "shipping_available": "No",
            "category": "Decorative Arts",
            "status": "ready",
            "image_folder": "2010_mirror",
            "last_export_batch": "",
            "published_at": "",
        }
    )

    response = test_env["client"].get("/items/2010/edit")

    assert response.status_code == 200
    assert b"Edit Saved Lot 2010" in response.data
    assert b"Wall mirror" in response.data


def test_updating_published_item_marks_needs_update(test_env, tmp_path, monkeypatch):
    db_path = tmp_path / "auction_items.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    app_module.append_item_record(
        {
            "lot_number": "2011",
            "title": "Lamp",
            "description": "Brass lamp",
            "condition_notes": "Working",
            "low_estimate": "",
            "high_estimate": "",
            "dimensions_length": "",
            "dimensions_depth": "",
            "dimensions_height": "",
            "tags": "lamp",
            "reference_number": "",
            "item_notes": "tested",
            "consigner_number": "",
            "shipping_available": "No",
            "category": "Decorative Arts",
            "status": "ready",
            "image_folder": "2011_lamp",
            "last_export_batch": "",
            "published_at": "",
        }
    )

    test_env["client"].post("/export_selected_csv", data={"lot_numbers": ["2011"]})

    update_response = test_env["client"].post(
        "/items/2011/update",
        data={
            "Title": "Lamp",
            "Description": "Brass lamp with updated details",
            "Condition Summary": "Working",
            "Keywords": "lamp",
            "Category": "Decorative Arts",
            "Low Estimate ($)": "",
            "High Estimate ($)": "",
            "Dimensions - Length": "",
            "Dimensions - Depth": "",
            "Dimensions - Height": "",
            "Reference #": "",
            "Item Notes": "tested",
            "Consigner #": "",
            "Shipping Available": "No",
        },
        follow_redirects=True,
    )

    assert update_response.status_code == 200
    assert b"Status changed to needs_update" in update_response.data

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT description, status FROM auction_items WHERE lot_number = 2011"
        ).fetchone()

    assert row == ("Brass lamp with updated details", "needs_update")


def test_remove_saved_item_hides_it_from_manage_and_export(test_env, tmp_path, monkeypatch):
    db_path = tmp_path / "auction_items.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    app_module.append_item_record(
        {
            "lot_number": "2012",
            "title": "Clock",
            "description": "Mantel clock",
            "condition_notes": "Untested",
            "low_estimate": "",
            "high_estimate": "",
            "dimensions_length": "",
            "dimensions_depth": "",
            "dimensions_height": "",
            "tags": "clock",
            "reference_number": "",
            "item_notes": "heavy",
            "consigner_number": "",
            "shipping_available": "No",
            "category": "Decorative Arts",
            "status": "ready",
            "image_folder": "2012_clock",
            "last_export_batch": "",
            "published_at": "",
        }
    )

    remove_response = test_env["client"].post(
        "/items/2012/remove",
        follow_redirects=True,
    )

    assert remove_response.status_code == 200
    assert b"Removed lot 2012 from future exports." in remove_response.data
    assert b"Clock" not in remove_response.data

    export_response = test_env["client"].get("/export_csv", follow_redirects=True)
    assert export_response.status_code == 200
    assert b"There are no saved items to export yet." in export_response.data

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status, title FROM auction_items WHERE lot_number = 2012"
        ).fetchone()

    assert row == ("removed", "Clock")


def test_manage_items_status_filter_views_removed_and_ready(test_env, tmp_path, monkeypatch):
    db_path = tmp_path / "auction_items.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    app_module.append_item_record(
        {
            "lot_number": "2013",
            "title": "Plate",
            "description": "Blue plate",
            "condition_notes": "Good",
            "low_estimate": "",
            "high_estimate": "",
            "dimensions_length": "",
            "dimensions_depth": "",
            "dimensions_height": "",
            "tags": "plate",
            "reference_number": "",
            "item_notes": "",
            "consigner_number": "",
            "shipping_available": "No",
            "category": "Pottery & Glass",
            "status": "ready",
            "image_folder": "2013_plate",
            "last_export_batch": "",
            "published_at": "",
        }
    )
    app_module.append_item_record(
        {
            "lot_number": "2014",
            "title": "Bowl",
            "description": "Stoneware bowl",
            "condition_notes": "Used",
            "low_estimate": "",
            "high_estimate": "",
            "dimensions_length": "",
            "dimensions_depth": "",
            "dimensions_height": "",
            "tags": "bowl",
            "reference_number": "",
            "item_notes": "",
            "consigner_number": "",
            "shipping_available": "No",
            "category": "Pottery & Glass",
            "status": "ready",
            "image_folder": "2014_bowl",
            "last_export_batch": "",
            "published_at": "",
        }
    )

    test_env["client"].post("/items/2014/remove", follow_redirects=True)

    ready_response = test_env["client"].get("/manage_items?status=ready")
    assert ready_response.status_code == 200
    assert b"Plate" in ready_response.data
    assert b"Bowl" not in ready_response.data

    removed_response = test_env["client"].get("/manage_items?status=removed")
    assert removed_response.status_code == 200
    assert b"Bowl" in removed_response.data
    assert b"Plate" not in removed_response.data
    assert b"Ready (1)" in removed_response.data
    assert b"Removed (1)" in removed_response.data


def test_restore_removed_ready_item_returns_to_ready(test_env, tmp_path, monkeypatch):
    db_path = tmp_path / "auction_items.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    app_module.append_item_record(
        {
            "lot_number": "2015",
            "title": "Tray",
            "description": "Metal tray",
            "condition_notes": "Wear",
            "low_estimate": "",
            "high_estimate": "",
            "dimensions_length": "",
            "dimensions_depth": "",
            "dimensions_height": "",
            "tags": "tray",
            "reference_number": "",
            "item_notes": "",
            "consigner_number": "",
            "shipping_available": "No",
            "category": "Decorative Arts",
            "status": "ready",
            "image_folder": "2015_tray",
            "last_export_batch": "",
            "published_at": "",
        }
    )

    test_env["client"].post("/items/2015/remove", follow_redirects=True)
    restore_response = test_env["client"].post(
        "/items/2015/restore",
        data={"current_filter": "removed"},
        follow_redirects=True,
    )

    assert restore_response.status_code == 200
    assert b"Restored lot 2015 to ready." in restore_response.data
    assert b"No items matched this status filter." in restore_response.data

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status FROM auction_items WHERE lot_number = 2015"
        ).fetchone()

    assert row == ("ready",)


def test_restore_removed_published_item_returns_to_needs_update(test_env, tmp_path, monkeypatch):
    db_path = tmp_path / "auction_items.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    app_module.append_item_record(
        {
            "lot_number": "2016",
            "title": "Vase",
            "description": "Ceramic vase",
            "condition_notes": "Good",
            "low_estimate": "",
            "high_estimate": "",
            "dimensions_length": "",
            "dimensions_depth": "",
            "dimensions_height": "",
            "tags": "vase",
            "reference_number": "",
            "item_notes": "",
            "consigner_number": "",
            "shipping_available": "No",
            "category": "Decorative Arts",
            "status": "ready",
            "image_folder": "2016_vase",
            "last_export_batch": "",
            "published_at": "",
        }
    )

    test_env["client"].post("/export_selected_csv", data={"lot_numbers": ["2016"]})
    test_env["client"].post("/items/2016/remove", data={"current_filter": "published"}, follow_redirects=True)
    restore_response = test_env["client"].post(
        "/items/2016/restore",
        data={"current_filter": "removed"},
        follow_redirects=True,
    )

    assert restore_response.status_code == 200
    assert b"marked needs_update" in restore_response.data
    assert b"No items matched this status filter." in restore_response.data

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status, published_at FROM auction_items WHERE lot_number = 2016"
        ).fetchone()

    assert row[0] == "needs_update"
    assert row[1] is not None
