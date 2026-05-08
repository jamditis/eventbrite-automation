"""LLM CLI wrappers — gemini primary, codex fallback. OAuth subprocess only.

Per `~/.claude/CLAUDE.md`: never make direct LLM API calls. Always shell out
to the CLI binary so the existing OAuth (gemini Pro / ChatGPT Plus) sessions
are used at zero marginal cost. The runner also scrubs OPENAI_API_KEY and
ANTHROPIC_API_KEY from the subprocess env so a stray export can't silently
flip codex into API-key billing mode.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

MAX_BLURB_CHARS = 200

_POLITE_PREFIXES = (
    "Sure, ",
    "Sure! ",
    "Here's a one-liner: ",
    "Here's a one-liner — ",
    "Here is a one-liner: ",
    "Here is a one-liner — ",
    "Here's the briefing: ",
    "Here is the briefing: ",
    "Here's the briefing — ",
    "Here is the briefing — ",
)

_MARKDOWN_TOKENS = re.compile(r"(\*\*|__|`|\[.*?\]\(.*?\)|^#)", re.MULTILINE)


class LLMOutputInvalid(ValueError):
    pass


def strip_polite_prefix(s: str) -> str:
    s = s.strip()
    for prefix in _POLITE_PREFIXES:
        if s.startswith(prefix):
            return s[len(prefix) :].strip()
    return s


def validate_one_liner(s: str) -> str:
    """Raise LLMOutputInvalid if s violates one-liner constraints; return s on pass."""
    s = s.strip()
    if not s:
        raise LLMOutputInvalid("empty")
    if len(s) > MAX_BLURB_CHARS:
        raise LLMOutputInvalid(f"length over {MAX_BLURB_CHARS}: got {len(s)}")
    if _MARKDOWN_TOKENS.search(s):
        raise LLMOutputInvalid("markdown detected in output")
    sentence_breaks = re.findall(r"[.!?]\s+[A-Z]", s)
    if sentence_breaks:
        raise LLMOutputInvalid(f"single sentence required; got {len(sentence_breaks) + 1}")
    return s


def _clean_env() -> dict[str, str]:
    """Subprocess env with LLM API keys removed to force OAuth-only billing."""
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(key, None)
    return env


class LLMRunner:
    def __init__(self, gemini_bin: str, codex_bin: str, codex_model: str) -> None:
        self._gemini = gemini_bin
        self._codex = codex_bin
        self._codex_model = codex_model

    def run_blurb(self, prompt: str) -> str | None:
        """Try gemini, then codex. Return validated one-liner or None on full failure."""
        for runner in (self._run_gemini, self._run_codex):
            raw = runner(prompt)
            if raw is None:
                continue
            try:
                return validate_one_liner(strip_polite_prefix(raw))
            except LLMOutputInvalid as e:
                logger.warning("LLM output rejected (%s): %r", e, raw[:100])
        return None

    def _run_gemini(self, prompt: str) -> str | None:
        return self._run_cmd([self._gemini, "-p", prompt])

    def _run_codex(self, prompt: str) -> str | None:
        return self._run_cmd([self._codex, "-m", self._codex_model, "-p", prompt])

    @staticmethod
    def _run_cmd(args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=_clean_env(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("CLI %s failed: %s", args[0], e)
            return None
        if result.returncode != 0:
            logger.warning(
                "CLI %s exit %d: %s", args[0], result.returncode, (result.stderr or "")[:200]
            )
            return None
        return result.stdout
