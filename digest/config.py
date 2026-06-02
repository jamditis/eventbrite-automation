"""Env-driven config. Required keys must be set or load_config raises ConfigError.

Loads `.env.digest` at module import via python-dotenv. The digest uses
its own env file — the repo's `.env` belongs to the webhook server and
sets a different `AIRTABLE_BASE_ID`, so the two cannot share it. systemd
reads `.env.digest` directly via `EnvironmentFile=`, so the cron path
doesn't need this load — but the manual operator commands documented in
the runbook (e.g. `venv/bin/python -m digest.cron --dry-run`) run from
the repo root and silently fail without it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# config.py lives in digest/; the digest env file sits at the repo root.
load_dotenv(Path(__file__).resolve().parent.parent / ".env.digest")


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
    logo_url: str
    bcc_always: tuple[str, ...]
    cc_always: tuple[str, ...]
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
    # SMTP_USER is the auth identity; SMTP_FROM_EMAIL is the From header and
    # the List-Unsubscribe mailto target. Both must be real — a placeholder
    # would fail SMTP login or send unsubscribe replies into a void. Required
    # so a blank in .env.digest fails at load time, not mid-send.
    "SMTP_USER",
    "SMTP_FROM_EMAIL",
)


def load_config() -> Config:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise ConfigError(f"missing required env vars: {', '.join(missing)}")

    # bcc_always = hidden standing recipients copied on every digest (Joe,
    # Cassandra, advinculaa). cc_always = the visible standing copy (the CCM
    # org inbox), which all recipients see. Both are overridable per-deploy via
    # the env vars; the defaults encode the current standing intent.
    bcc_raw = os.environ.get(
        "BCC_ALWAYS",
        "jamditis@gmail.com,etiennec@montclair.edu,advinculaa@montclair.edu",
    )
    bcc_always = tuple(addr.strip() for addr in bcc_raw.split(",") if addr.strip())

    cc_raw = os.environ.get("CC_ALWAYS", "info@centerforcooperativemedia.org")
    cc_always = tuple(addr.strip() for addr in cc_raw.split(",") if addr.strip())

    smtp_port_raw = os.environ.get("SMTP_PORT", "465")
    try:
        smtp_port = int(smtp_port_raw)
    except ValueError as e:
        raise ConfigError(f"SMTP_PORT must be an integer; got {smtp_port_raw!r}") from e

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
        smtp_port=smtp_port,
        smtp_user=os.environ["SMTP_USER"],
        smtp_password=os.environ["SMTP_PASSWORD"],
        smtp_from_name=os.environ.get(
            "SMTP_FROM_NAME", "Center for Cooperative Media"
        ),
        smtp_from_email=os.environ["SMTP_FROM_EMAIL"],
        logo_url=os.environ.get(
            "LOGO_URL", "https://summit.collaborativejournalism.org/ccm-logo.png"
        ),
        bcc_always=bcc_always,
        cc_always=cc_always,
        gemini_bin=os.environ.get("GEMINI_BIN", "gemini"),
        codex_bin=os.environ.get("CODEX_BIN", "codex"),
        codex_model=os.environ.get("CODEX_MODEL", "gpt-5.4-low"),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
