"""Secret detection and redaction.

Kata stores tool arguments and results so it can learn from them. Anything that looks
like a credential is masked before it touches disk, and a routine whose *constant*
arguments contain a secret is never compiled: a generated tool must not embed one.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_SECRET_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|apikey|auth(orization)?|bearer|cookie|"
    r"session[_-]?key|private[_-]?key|credential|signature|otp|pin)$",
    re.IGNORECASE,
)

_SECRET_VALUE = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),                       # OpenAI / Anthropic style
    re.compile(r"(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),                 # Slack
    re.compile(r"AKIA[0-9A-Z]{16}"),                              # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),                        # Google API key
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)(password|passwd|token|api[_-]?key|secret)=[^\s&'\"]{6,}"),
]


def looks_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return any(p.search(value) for p in _SECRET_VALUE)


def redact_text(text: str) -> str:
    out = text
    for p in _SECRET_VALUE:
        out = p.sub(REDACTED, out)
    return out


def redact(value: Any, key: str = "") -> Any:
    """Return a deep copy of ``value`` with secret-looking keys and values masked."""
    if key and _SECRET_KEY.search(key) and value not in (None, "", False):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def contains_redaction(value: Any) -> bool:
    if isinstance(value, str):
        return REDACTED in value
    if isinstance(value, dict):
        return any(contains_redaction(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_redaction(v) for v in value)
    return False
