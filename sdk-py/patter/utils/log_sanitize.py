"""Helpers for safely logging user-supplied text and PII.

Free-form transcripts and phone numbers flow through the SDK at INFO level
for debuggability, but logs are often shipped to third parties (stdout,
files, SaaS log aggregators).  These helpers strip control characters that
could tamper with log output and mask sensitive values such as full E.164
phone numbers.
"""

from __future__ import annotations

import re

# Matches C0/C1 control bytes (including \r\n \t) and DEL.  These would
# otherwise let a malicious transcript inject newlines or ANSI escape
# sequences into logs.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_log_value(value: object, max_len: int = 200) -> str:
    """Return a log-safe rendition of *value*.

    - ``None`` becomes an empty string.
    - Control characters are removed.
    - Values longer than *max_len* are truncated with an ellipsis suffix.
    """
    if value is None:
        return ""
    cleaned = _CONTROL_RE.sub("", str(value))
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "..."
    return cleaned


def mask_phone_number(number: object) -> str:
    """Mask an E.164 phone number for logging.

    Keeps only the last 4 characters to preserve enough context for
    correlation while avoiding PII leakage.  Returns an empty placeholder
    when *number* is falsy or too short to meaningfully mask.
    """
    if not number:
        return "***"
    text = str(number)
    if len(text) <= 4:
        return "***"
    return f"***{text[-4:]}"
