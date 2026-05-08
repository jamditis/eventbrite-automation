from unittest.mock import MagicMock

import pytest

from digest.llm_subprocess import (
    LLMOutputInvalid,
    LLMRunner,
    strip_polite_prefix,
    validate_one_liner,
)


def test_validate_accepts_clean_one_liner():
    s = "Sarah Smith covers municipal government for North Jersey Journal."
    assert validate_one_liner(s) == s


def test_validate_rejects_markdown():
    with pytest.raises(LLMOutputInvalid, match="markdown"):
        validate_one_liner("**Sarah Smith** covers government.")


def test_validate_rejects_multi_sentence():
    with pytest.raises(LLMOutputInvalid, match="single sentence"):
        validate_one_liner("Sarah is an editor. She covers government.")


def test_validate_rejects_too_long():
    with pytest.raises(LLMOutputInvalid, match="length"):
        validate_one_liner("x " * 200)


def test_validate_rejects_empty():
    with pytest.raises(LLMOutputInvalid, match="empty"):
        validate_one_liner("   ")


def test_strip_polite_prefix_removes_known_starters():
    assert strip_polite_prefix("Sure, Sarah is an editor.") == "Sarah is an editor."
    assert strip_polite_prefix("Here's a one-liner: Sarah is an editor.") == "Sarah is an editor."
    assert strip_polite_prefix("Here is the briefing — Sarah is an editor.") == "Sarah is an editor."


def test_strip_polite_prefix_leaves_clean_input_alone():
    assert strip_polite_prefix("Sarah is an editor.") == "Sarah is an editor."


def test_runner_calls_gemini_first(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return MagicMock(stdout=" Sarah is an editor.\n", stderr="", returncode=0)

    monkeypatch.setattr("digest.llm_subprocess.subprocess.run", fake_run)
    runner = LLMRunner(gemini_bin="gemini-test", codex_bin="codex-test", codex_model="gpt-5.4-low")
    out = runner.run_blurb("Make a one-liner.")
    assert out == "Sarah is an editor."
    assert calls[0][0] == "gemini-test"


def test_runner_falls_back_to_codex_on_gemini_failure(monkeypatch):
    call_log = []

    def fake_run(args, **kw):
        call_log.append(args[0])
        if args[0] == "gemini-test":
            return MagicMock(stdout="", stderr="failure", returncode=1)
        return MagicMock(stdout="Codex result.\n", stderr="", returncode=0)

    monkeypatch.setattr("digest.llm_subprocess.subprocess.run", fake_run)
    runner = LLMRunner(gemini_bin="gemini-test", codex_bin="codex-test", codex_model="gpt-5.4-low")
    out = runner.run_blurb("Make a one-liner.")
    assert out == "Codex result."
    assert call_log == ["gemini-test", "codex-test"]


def test_runner_returns_none_when_both_fail(monkeypatch):
    def fake_run(args, **kw):
        return MagicMock(stdout="", stderr="boom", returncode=1)

    monkeypatch.setattr("digest.llm_subprocess.subprocess.run", fake_run)
    runner = LLMRunner(gemini_bin="g", codex_bin="c", codex_model="gpt-5.4-low")
    assert runner.run_blurb("x") is None


def test_runner_falls_back_when_gemini_returns_invalid(monkeypatch):
    def fake_run(args, **kw):
        if args[0] == "gemini-test":
            return MagicMock(stdout="**Bold sarah**\n", stderr="", returncode=0)
        return MagicMock(stdout="Sarah is an editor.\n", stderr="", returncode=0)

    monkeypatch.setattr("digest.llm_subprocess.subprocess.run", fake_run)
    runner = LLMRunner(gemini_bin="gemini-test", codex_bin="codex-test", codex_model="gpt-5.4-low")
    assert runner.run_blurb("x") == "Sarah is an editor."


def test_runner_scrubs_all_provider_auth_vars_from_subprocess_env(monkeypatch):
    """Defense-in-depth: any exported provider auth var (across OpenAI / Anthropic /
    Google aliases) must not leak into the codex/gemini subprocess. Forces OAuth
    (chatgpt / gemini Pro) auth at zero marginal cost. If a CLI ships a new
    auth alias, add it to _PROVIDER_AUTH_VARS and extend this assertion.
    """
    captured = {}

    def fake_run(args, **kw):
        captured.update(kw.get("env") or {})
        return MagicMock(stdout="Sarah is an editor.\n", stderr="", returncode=0)

    leaked = {
        "OPENAI_API_KEY": "leak1",
        "OPENAI_BASE_URL": "https://leak.example",
        "OPENAI_PROJECT": "proj-leak",
        "OPENAI_ORG_ID": "org-leak",
        "OPENAI_ORGANIZATION": "org-leak2",
        "ANTHROPIC_API_KEY": "leak2",
        "ANTHROPIC_BASE_URL": "https://leak2.example",
        "GOOGLE_API_KEY": "leak3",
        "GEMINI_API_KEY": "leak4",
    }
    for key, val in leaked.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setattr("digest.llm_subprocess.subprocess.run", fake_run)
    runner = LLMRunner(gemini_bin="g", codex_bin="c", codex_model="x")
    runner.run_blurb("test")
    for key in leaked:
        assert key not in captured, f"{key} leaked into subprocess env"


def test_runner_handles_missing_binary(monkeypatch):
    def fake_run(args, **kw):
        raise FileNotFoundError(f"no such binary: {args[0]}")

    monkeypatch.setattr("digest.llm_subprocess.subprocess.run", fake_run)
    runner = LLMRunner(gemini_bin="nonexistent-gemini", codex_bin="nonexistent-codex", codex_model="x")
    assert runner.run_blurb("x") is None
