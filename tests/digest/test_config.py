import pytest

from digest.config import ConfigError, load_config


def test_load_config_reads_env(env):
    env.setenv("EVENTBRITE_PRIVATE_TOKEN", "tok123")
    env.setenv("AIRTABLE_PAT", "pat123")
    env.setenv("AIRTABLE_BASE_ID", "appABC")
    env.setenv("DASHBOARD_API_KEY", "dash123")
    env.setenv("SMTP_PASSWORD", "smtp123")
    cfg = load_config()
    assert cfg.eventbrite_token == "tok123"
    assert cfg.airtable_pat == "pat123"
    assert cfg.airtable_base_id == "appABC"
    assert cfg.smtp_password == "smtp123"


def test_load_config_raises_on_missing_required(env):
    env.delenv("EVENTBRITE_PRIVATE_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="EVENTBRITE_PRIVATE_TOKEN"):
        load_config()


def test_load_config_defaults(env):
    env.setenv("EVENTBRITE_PRIVATE_TOKEN", "x")
    env.setenv("AIRTABLE_PAT", "x")
    env.setenv("AIRTABLE_BASE_ID", "x")
    env.setenv("DASHBOARD_API_KEY", "x")
    env.setenv("SMTP_PASSWORD", "x")
    cfg = load_config()
    assert cfg.smtp_host == "smtp.gmail.com"
    assert cfg.smtp_port == 465
    assert cfg.airtable_table_name == "Events"
    assert cfg.gemini_bin == "gemini"


def test_load_config_parses_bcc_list(env):
    env.setenv("EVENTBRITE_PRIVATE_TOKEN", "x")
    env.setenv("AIRTABLE_PAT", "x")
    env.setenv("AIRTABLE_BASE_ID", "x")
    env.setenv("DASHBOARD_API_KEY", "x")
    env.setenv("SMTP_PASSWORD", "x")
    env.setenv("BCC_ALWAYS", "a@b.com, c@d.com ,  ")
    cfg = load_config()
    assert cfg.bcc_always == ("a@b.com", "c@d.com")
