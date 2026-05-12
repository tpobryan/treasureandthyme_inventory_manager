import pytest

from app.inventory_manager_generator import _parse_model_json


def test_parse_model_json_accepts_valid_json():
    parsed = _parse_model_json('{"options": [{"rank": 1, "title": "Vase"}]}')

    assert parsed["options"][0]["title"] == "Vase"


def test_parse_model_json_repairs_unquoted_keys_and_trailing_commas():
    raw = """
    {
      options: [
        {
          rank: 1,
          identification: "Ceramic vase",
          title: "Blue Vase",
        }
      ],
    }
    """

    parsed = _parse_model_json(raw)

    assert parsed["options"][0]["rank"] == 1
    assert parsed["options"][0]["identification"] == "Ceramic vase"
    assert parsed["options"][0]["title"] == "Blue Vase"


def test_parse_model_json_raises_for_non_json_text():
    with pytest.raises(ValueError):
        _parse_model_json("Sorry, I could not determine the item.")
