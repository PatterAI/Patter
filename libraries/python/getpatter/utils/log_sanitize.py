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

# Anything not in this safe set is folded to ``_`` when building a single
# filesystem path segment from an untrusted value.  Crucially this folds BOTH
# path separators (POSIX ``/`` and Windows ``\``) plus drive-letter ``:`` so the
# result can never contain a separator and therefore can never traverse.
_PATH_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


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


def safe_path_segment(value: object, max_len: int = 64) -> str:
    """Return a filesystem-safe SINGLE path segment from untrusted *value*.

    Used where an attacker-influenceable id (e.g. a carrier-supplied call id
    from an unauthenticated media-WebSocket ``start`` frame) becomes a directory
    or file name. Folds every path separator — POSIX ``/`` AND Windows ``\\`` —
    and any other unusual character to ``_``, so the result is guaranteed to be
    a single component that cannot traverse, then neutralises a bare ``..`` by
    stripping leading/trailing dots and caps the length. Never returns ``""``,
    ``"."`` or ``".."``.
    """
    cleaned = _CONTROL_RE.sub("", str(value or ""))
    cleaned = _PATH_UNSAFE_RE.sub("_", cleaned)[:max_len].strip(".")
    return cleaned or "unknown"
