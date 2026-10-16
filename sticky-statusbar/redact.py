"""Redaction at the chat boundary.

An agent's own reply passes through Hermes's redactor on its way to the platform.
A message this plugin posts does not — it goes straight out through the platform
client — so the board applies its own pass before anything is sent. That matters
because the board echoes the first characters of a tool's command, which is
exactly where an inline credential sits.

Kept self-contained on purpose: Hermes's own redactor lives in a private core
module, and a plugin that imports one takes on a coupling the catalog explicitly
warns about. These patterns are the credential shapes that can realistically
appear in a one-line argument hint, and a false mask on a status line is a
cosmetic bug while a missed token is not — so the rules lean toward masking.
"""

from __future__ import annotations

import re

MASK = "***"

# Prefix-matched credentials: vendor shapes that are unambiguous.
_PREFIXED = (
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{12,}"),          # OpenAI-style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),                # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),              # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                        # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),                   # Google API key
)

# Credential-shaped assignments and headers. The value must be long enough that
# a real word ("token: none") is not swallowed.
#
# The leading guard is ``(?<![A-Za-z0-9])`` rather than ``\b``: an underscore is a
# word character, so ``\bapi`` does not match inside ``ANTHROPIC_API_KEY=`` — the
# most common shape this rule exists for.
_ASSIGNED = (
    re.compile(
        r"(?i)(?<![A-Za-z0-9])(?:bearer|token|api[_\- ]?key|apikey|access[_\- ]?key|secret|"
        r"password|passwd|client[_\- ]?secret)\b\s*[:=]?\s*[\"']?([A-Za-z0-9._\-/+]{16,})"
    ),
)

# URL userinfo: ``scheme://user:password@host``.
_URL_CREDS = (
    re.compile(r"://[^\s/:@]+:[^\s/:@]+@"),
)

# A long hex/base64-looking blob: at least 32 chars with at least one letter
# (so a pure-digit id survives) — API keys and session tokens look like this.
_OPAQUE = (
    re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)


def _mask(value: str) -> str:
    """Keep a hint of the shape, never the value."""
    head = value.split("-", 1)[0][:6] if "-" in value else ""
    return f"{head}{MASK}" if head else MASK


def _mask_assignment(match: re.Match) -> str:
    """Keep ``token=`` and drop the value."""
    whole = match.group(0)
    value = match.group(1)
    return whole[: whole.rindex(value)] + MASK


def scrub(text: str) -> str:
    """The text that is safe to put in front of a chat.

    Never raises: a redaction failure must not take the publisher down, and it
    must not be silently skipped either — every pass is independent, so one
    pattern failing leaves the others applied.
    """
    if not isinstance(text, str) or not text:
        return text
    scrubbed = text
    for pattern in _PREFIXED:
        scrubbed = pattern.sub(lambda match: _mask(match.group(0)), scrubbed)
    scrubbed = _ASSIGNED[0].sub(_mask_assignment, scrubbed)
    scrubbed = _URL_CREDS[0].sub("://" + MASK + "@", scrubbed)
    for pattern in _OPAQUE:
        scrubbed = pattern.sub(MASK, scrubbed)
    return scrubbed
