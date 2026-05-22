import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(monkeypatch):
    """Scrub project-prefixed env vars so tests start clean, then return monkeypatch."""
    for key in list(os.environ):
        if key.startswith(
            (
                "EVENTBRITE_",
                "AIRTABLE_",
                "DASHBOARD_",
                "SMTP_",
                "BCC_",
                "GEMINI_",
                "CODEX_",
                "TELEGRAM_",
                "LOGO_URL",
            )
        ):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch
