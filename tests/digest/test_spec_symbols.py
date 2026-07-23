"""Guard: every shipped-method reference in the design specs must resolve.

The digest design spec names methods like ``airtable.update_after_send`` and
``crm_lookup.find_by_email``. Nothing stopped those from drifting out of sync
with the real ``digest/`` package -- issue #27 was exactly that (phantom names
that no longer existed in the code). This test extracts each ``module.method``
reference from the specs and checks the method actually exists in the mapped
source file, so the spec stays auditable against the code by the build, not by
memory.

Scope is deliberately narrow. Only the prefixes in ``SYMBOL_SOURCES`` are
checked. References whose prefix is not mapped fall through untouched: local
variables (``row.slug``), stdlib calls (``traceback.format_exc``), URLs
(``api.airtable.com``), and modules that live in other repos (``email_ledger.*``
is in houseofjawn-bot, which CI cannot see). A mapped prefix paired with a file
extension (``eventbrite_client.py``) is a filename, not a call, so those attrs
are ignored too.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = REPO_ROOT / "docs" / "superpowers" / "specs"

# spec prefix -> source file(s) in THIS repo that define its methods.
# email_ledger is intentionally absent: it lives in houseofjawn-bot, not here,
# so CI has nothing to check it against.
SYMBOL_SOURCES: dict[str, list[str]] = {
    "airtable": ["digest/airtable_client.py"],
    "cron": ["digest/cron.py"],
    "eventbrite_client": ["digest/eventbrite_client.py"],
    "crm_lookup": ["digest/crm_lookup.py"],
    "profile_builder": ["digest/profile_builder.py"],
    "send_engine": ["digest/send_engine.py"],
    "llm": ["digest/llm_subprocess.py"],
}

# Attrs that are file extensions or TLDs, not methods. A mapped prefix paired
# with one of these is a filename (``cron.timer``) or domain, not a call.
IGNORED_ATTRS = {
    "py", "md", "txt", "json", "sh", "html", "db", "log", "lock",
    "service", "timer", "yml", "yaml", "cfg", "toml", "png", "svg",
    "com", "org", "net", "io",
}

# ``prefix.attr``, not preceded by an identifier char or dot, so a dotted chain
# like ``a.b.c`` yields only ``a.b`` rather than spurious ``b.c`` fragments.
_REF = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)")


def _methods_in(rel_paths: list[str]) -> set[str]:
    """All function/method names defined anywhere in the given source files."""
    names: set[str] = set()
    for rel in rel_paths:
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names.add(node.name)
    return names


SYMBOLS: dict[str, set[str]] = {p: _methods_in(s) for p, s in SYMBOL_SOURCES.items()}


@dataclass(frozen=True)
class Reference:
    spec: str  # repo-relative path
    line: int
    prefix: str
    attr: str

    def __str__(self) -> str:
        return f"{self.spec}:{self.line} {self.prefix}.{self.attr}"


def _collect_references() -> list[Reference]:
    refs: list[Reference] = []
    for spec in sorted(SPEC_DIR.glob("*.md")):
        rel = spec.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(spec.read_text(encoding="utf-8").splitlines(), 1):
            for prefix, attr in _REF.findall(line):
                if prefix in SYMBOL_SOURCES and attr not in IGNORED_ATTRS:
                    refs.append(Reference(rel, lineno, prefix, attr))
    return refs


REFERENCES = _collect_references()


def test_symbol_sources_exist_and_parse():
    """Every mapped source file must exist and expose at least one method."""
    for prefix, paths in SYMBOL_SOURCES.items():
        for rel in paths:
            assert (REPO_ROOT / rel).is_file(), f"mapped source missing: {rel}"
        assert SYMBOLS[prefix], f"no methods parsed for prefix '{prefix}' ({paths})"


def test_extractor_finds_known_reference():
    """Guard the regex itself: the digest spec must yield a known-good ref.

    If a spec rewrite or a regex change made the extractor match nothing, the
    parametrized check below would silently pass on an empty set and the guard
    would be useless. This anchors it to a reference known to exist today.
    """
    pairs = {(r.prefix, r.attr) for r in REFERENCES}
    assert ("airtable", "list_active_records") in pairs, (
        "spec-symbol extractor found no `airtable.list_active_records` reference -- the "
        "spec changed shape or the regex broke; fix the extractor."
    )


@pytest.mark.parametrize("ref", REFERENCES, ids=str)
def test_spec_method_reference_resolves(ref: Reference):
    symbols = SYMBOLS[ref.prefix]
    if ref.attr not in symbols:
        near = difflib.get_close_matches(ref.attr, sorted(symbols), n=3)
        hint = f" did you mean: {', '.join(near)}?" if near else ""
        pytest.fail(
            f"{ref}: `{ref.prefix}.{ref.attr}` is not a method in "
            f"{SYMBOL_SOURCES[ref.prefix]}.{hint}\n"
            f"  available: {', '.join(sorted(symbols))}"
        )
