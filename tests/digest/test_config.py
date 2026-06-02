import pytest

from digest.config import ConfigError, load_config

# All env vars in config.REQUIRED. Centralized here so adding a required var
# is a one-line change in both the source tuple and this fixture, not a
# scavenger hunt across every success-path test.
_REQUIRED_ENV = {
    "EVENTBRITE_PRIVATE_TOKEN": "tok123",
    "AIRTABLE_PAT": "pat123",
    "AIRTABLE_BASE_ID": "appABC",
    "DASHBOARD_API_KEY": "dash123",
    "SMTP_PASSWORD": "smtp123",
    "SMTP_USER": "sender@ccm.example",
    "SMTP_FROM_EMAIL": "digest@ccm.example",
}


def _set_required(env):
    for key, value in _REQUIRED_ENV.items():
        env.setenv(key, value)


def test_load_config_reads_env(env):
    _set_required(env)
    cfg = load_config()
    assert cfg.eventbrite_token == "tok123"
    assert cfg.airtable_pat == "pat123"
    assert cfg.airtable_base_id == "appABC"
    assert cfg.smtp_password == "smtp123"
    assert cfg.smtp_user == "sender@ccm.example"
    assert cfg.smtp_from_email == "digest@ccm.example"


def test_load_config_raises_on_missing_required(env):
    env.delenv("EVENTBRITE_PRIVATE_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="EVENTBRITE_PRIVATE_TOKEN"):
        load_config()


@pytest.mark.parametrize("missing_key", ["SMTP_USER", "SMTP_FROM_EMAIL"])
def test_load_config_requires_smtp_identity(env, missing_key):
    """SMTP_USER and SMTP_FROM_EMAIL must fail fast at load, not at send time:
    a placeholder would break SMTP auth or the List-Unsubscribe mailto target.
    """
    _set_required(env)
    env.delenv(missing_key, raising=False)
    with pytest.raises(ConfigError, match=missing_key):
        load_config()


def test_load_config_defaults(env):
    _set_required(env)
    cfg = load_config()
    assert cfg.smtp_host == "smtp.gmail.com"
    assert cfg.smtp_port == 465
    assert cfg.airtable_table_name == "Events"
    assert cfg.gemini_bin == "gemini"
    assert cfg.logo_url == (
        "https://summit.collaborativejournalism.org/ccm-logo.png"
    )


def test_load_config_logo_url_override(env):
    _set_required(env)
    env.setenv("LOGO_URL", "https://example.org/custom-logo.png")
    cfg = load_config()
    assert cfg.logo_url == "https://example.org/custom-logo.png"


def test_load_config_raises_config_error_on_invalid_smtp_port(env):
    _set_required(env)
    env.setenv("SMTP_PORT", "abc")
    with pytest.raises(ConfigError, match="SMTP_PORT"):
        load_config()


def test_load_config_parses_bcc_list(env):
    _set_required(env)
    env.setenv("BCC_ALWAYS", "a@b.com, c@d.com ,  ")
    cfg = load_config()
    assert cfg.bcc_always == ("a@b.com", "c@d.com")


def test_load_config_bcc_default_includes_standing_recipients(env):
    """With BCC_ALWAYS unset, the standing org recipients are copied on every
    digest: Joe, Cassandra, and advinculaa."""
    _set_required(env)
    env.delenv("BCC_ALWAYS", raising=False)
    cfg = load_config()
    assert cfg.bcc_always == (
        "jamditis@gmail.com",
        "etiennec@montclair.edu",
        "advinculaa@montclair.edu",
    )


def test_load_config_parses_cc_list(env):
    _set_required(env)
    env.setenv("CC_ALWAYS", "x@y.com, z@w.com ,  ")
    cfg = load_config()
    assert cfg.cc_always == ("x@y.com", "z@w.com")


def test_load_config_cc_default_copies_ccm_inbox(env):
    """With CC_ALWAYS unset, the CCM org inbox is the standing visible copy."""
    _set_required(env)
    env.delenv("CC_ALWAYS", raising=False)
    cfg = load_config()
    assert cfg.cc_always == ("info@centerforcooperativemedia.org",)
