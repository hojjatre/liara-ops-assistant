"""Mask secrets before any text reaches the LLM or the UI."""
from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # scheme://user:password@host
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]+:)([^@\s]+)(@)"), r"\1***\3"),
    # JWTs
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "***JWT***"),
    # Bearer tokens
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1***"),
    # key=value / key: value for sensitive keys
    (re.compile(r"(?i)\b([A-Z0-9_]*(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*)"
                r"(\s*[=:]\s*)(['\"]?)[^\s'\",;]+"), r"\1\3\4***"),
    # common provider keys
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "***KEY***"),
    (re.compile(r"\bbox-ai-[A-Za-z0-9]{16,}"), "***KEY***"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact_obj(obj):
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    return obj
