"""Env-driven config. Required keys must be set or load_config raises ConfigError."""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    eventbrite_token: str
    airtable_pat: str
    airtable_base_id: str
    airtable_table_name: str
    dashboard_api_base: str
    dashboard_api_key: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from_name: str
    smtp_from_email: str
    bcc_always: tuple[str, ...]
    gemini_bin: str
    codex_bin: str
    codex_model: str
    telegram_bot_token: str
    telegram_chat_id: str


REQUIRED = (
    "EVENTBRITE_PRIVATE_TOKEN",
    "AIRTABLE_PAT",
    "AIRTABLE_BASE_ID",
    "DASHBOARD_API_KEY",
    "SMTP_PASSWORD",
)


def load_config() -> Config:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise ConfigError(f"missing required env vars: {', '.join(missing)}")

    bcc_raw = os.environ.get(
        "BCC_ALWAYS", "jamditis@gmail.com,etiennec@montclair.edu"
    )
    bcc_always = tuple(addr.strip() for addr in bcc_raw.split(",") if addr.strip())

    return Config(
        eventbrite_token=os.environ["EVENTBRITE_PRIVATE_TOKEN"],
        airtable_pat=os.environ["AIRTABLE_PAT"],
        airtable_base_id=os.environ["AIRTABLE_BASE_ID"],
        airtable_table_name=os.environ.get("AIRTABLE_TABLE_NAME", "Events"),
        dashboard_api_base=os.environ.get(
            "DASHBOARD_API_BASE", "http://localhost:8081/api"
        ),
        dashboard_api_key=os.environ["DASHBOARD_API_KEY"],
        smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.environ.get("SMTP_PORT", "465")),
        smtp_user=os.environ.get("SMTP_USER", "njnewscommons@gmail.com"),
        smtp_password=os.environ["SMTP_PASSWORD"],
        smtp_from_name=os.environ.get(
            "SMTP_FROM_NAME", "Center for Cooperative Media"
        ),
        smtp_from_email=os.environ.get(
            "SMTP_FROM_EMAIL", "njnewscommons@gmail.com"
        ),
        bcc_always=bcc_always,
        gemini_bin=os.environ.get("GEMINI_BIN", "gemini"),
        codex_bin=os.environ.get("CODEX_BIN", "codex"),
        codex_model=os.environ.get("CODEX_MODEL", "gpt-5.4-low"),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
