from pathlib import Path

import pytest

import app as app_module


@pytest.fixture
def test_env(tmp_path):
    data_dir = tmp_path / "data"
    uploads_dir = data_dir / "uploads"
    data_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    original_paths = {
        "DATA_DIR": app_module.DATA_DIR,
        "UPLOADS_DIR": app_module.UPLOADS_DIR,
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

    yield {
        "client": app_module.app.test_client(),
        "uploads_dir": uploads_dir,
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
