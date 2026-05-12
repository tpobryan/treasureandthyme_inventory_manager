import json
import pytest
from pathlib import Path
from app.integrations.taxonomy import flatten_taxonomy, get_taxonomy_name, search_taxonomy

@pytest.fixture
def mock_taxonomy_data(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    
    # Create a small dummy taxonomy
    dummy_data = {
        "results": [
            {
                "id": 1,
                "name": "Accessories",
                "children": [
                    {
                        "id": 2,
                        "name": "Belts",
                        "children": []
                    }
                ]
            },
            {
                "id": 66,
                "name": "Art",
                "children": []
            }
        ]
    }
    
    taxonomy_file = data_dir / "etsy_taxonomy.json"
    with open(taxonomy_file, "w") as f:
        json.dump(dummy_data, f)
        
    # Monkeypatch the DATA_DIR in taxonomy.py
    import app.integrations.taxonomy as taxonomy_module
    monkeypatch.setattr(taxonomy_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(taxonomy_module, "TAXONOMY_FILE", taxonomy_file)
    monkeypatch.setattr(taxonomy_module, "FLAT_TAXONOMY_FILE", data_dir / "etsy_taxonomy_flat.json")
    
    return data_dir

def test_flatten_taxonomy(mock_taxonomy_data):
    flat = flatten_taxonomy()
    assert flat["1"] == "Accessories"
    assert flat["2"] == "Accessories > Belts"
    assert flat["66"] == "Art"
    assert mock_taxonomy_data.joinpath("etsy_taxonomy_flat.json").exists()

def test_get_taxonomy_name(mock_taxonomy_data):
    # Should flatten first if missing
    name = get_taxonomy_name(2)
    assert name == "Accessories > Belts"
    
    # Test unknown ID
    assert "Unknown (999)" in get_taxonomy_name(999)

def test_search_taxonomy(mock_taxonomy_data):
    results = search_taxonomy("belt")
    assert len(results) == 1
    assert results[0]["id"] == "2"
    assert results[0]["name"] == "Accessories > Belts"
    
    # Case insensitive
    results = search_taxonomy("ART")
    assert len(results) == 1
    assert results[0]["id"] == "66"
