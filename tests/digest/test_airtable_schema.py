import runpy
from pathlib import Path


def test_new_airtable_bases_include_send_weekdays_field():
    script = Path(__file__).parents[2] / "deploy" / "create-airtable-base.py"
    fields = runpy.run_path(str(script))["EVENTS_FIELDS"]
    weekday = next(field for field in fields if field["name"] == "Send weekdays")
    assert weekday["type"] == "singleLineText"
    assert "Blank" in weekday["description"]
