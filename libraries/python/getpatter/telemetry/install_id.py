"""Anonymous identifiers for telemetry.

Two ids, both free of PII and never derived from hardware (MAC / hostname /
serials — that would be fingerprinting):

* :func:`run_id` — a fresh random id per process start. Lets us group the events
  of a single run without correlating runs over time.
* :func:`install_id` — a random UUID generated **once** and persisted to a small
  local file, so the same install reports the same id across restarts. This is
  the standard anonymous "install id" used by OSS tools (Homebrew, Next.js,
  Astro) to count *active installs* — it is a random number, not tied to a person
  or any identifying data, and it is only read/created on the telemetry-enabled
  path (opting out never touches the filesystem). If the file cannot be written
  (read-only FS, sandbox) we fall back to the per-process run id, so nothing ever
  breaks.

Mirrors ``libraries/typescript/src/telemetry/install-id.ts``.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

_RUN_ID = uuid.uuid4().hex
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_VERSION_RE = re.compile(r"^[0-9][0-9a-z.+-]{0,31}$")
_install_id: str | None = None


def run_id() -> str:
    """Return this process's anonymous run id (stable for the process lifetime)."""
    return _RUN_ID


def _state_dir() -> Path:
    """Directory holding the telemetry state files (overridable for tests).

    Precedence: ``PATTER_TELEMETRY_STATE_DIR`` (used literally) →
    ``$XDG_STATE_HOME/getpatter`` (the XDG spec requires an app subdirectory —
    writing into the shared root collides with other tools) → ``~/.getpatter``.
    """
    override = os.getenv("PATTER_TELEMETRY_STATE_DIR")
    if override:
        return Path(override)
    xdg = os.getenv("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "getpatter"
    return Path.home() / ".getpatter"


def _legacy_state_dir() -> Path | None:
    """Where pre-0.6.8 builds wrote state when ``XDG_STATE_HOME`` was set.

    Those builds used the bare ``$XDG_STATE_HOME`` root (no ``getpatter/``
    subdirectory). Existing installs must keep their id — and, critically, a
    persisted opt-out must keep being honored — so the readers below fall back
    to this location. ``None`` when no legacy location applies.
    """
    if os.getenv("PATTER_TELEMETRY_STATE_DIR"):
        return None
    xdg = os.getenv("XDG_STATE_HOME")
    return Path(xdg) if xdg else None


def _state_path() -> Path:
    """Where the persisted install id lives."""
    return _state_dir() / "install-id"


def install_id() -> str:
    """Return the persisted anonymous install id (random UUID, created once).

    Best-effort and never raises: an unwritable filesystem degrades to the
    per-process run id rather than failing telemetry.
    """
    global _install_id
    if _install_id is not None:
        return _install_id

    path = _state_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if _HEX32.match(existing):
            _install_id = existing
            return _install_id
    except OSError:
        pass

    legacy_dir = _legacy_state_dir()
    if legacy_dir is not None:
        legacy = legacy_dir / "install-id"
        try:
            existing = legacy.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        if _HEX32.match(existing):
            # Migrate the pre-0.6.8 id so the install keeps counting as one,
            # preserving the file mtime that feeds days_since_install_bucket.
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(existing, encoding="utf-8")
                stat = legacy.stat()
                os.utime(path, (stat.st_atime, stat.st_mtime))
            except OSError:
                pass
            _install_id = existing
            return _install_id

    new_id = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id, encoding="utf-8")
        _install_id = new_id
    except OSError:
        # Read-only / sandboxed FS: fall back to the per-process id (not persisted).
        _install_id = _RUN_ID
    return _install_id


def _version_path() -> Path:
    return _state_path().parent / "version"


def previous_version(current: str) -> str:
    """Return the last sdk_version this install reported ("" on first run), then
    record ``current`` for next time. Powers the upgrade funnel. Best-effort."""
    path = _version_path()
    prev = ""
    try:
        prev = path.read_text(encoding="utf-8").strip()
    except OSError:
        prev = ""
    if not prev:
        legacy_dir = _legacy_state_dir()
        if legacy_dir is not None:
            try:
                prev = (legacy_dir / "version").read_text(encoding="utf-8").strip()
            except OSError:
                prev = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(current, encoding="utf-8")
    except OSError:
        pass
    return prev if _VERSION_RE.match(prev) else ""


def days_since_install_bucket() -> str:
    """Coarse age of this install, from the install-id file mtime (0/1_7/8_30/30_plus)."""
    try:
        mtime = _state_path().stat().st_mtime
    except OSError:
        return "0"
    days = max(0, int((time.time() - mtime) / 86400))
    if days == 0:
        return "0"
    if days <= 7:
        return "1_7"
    if days <= 30:
        return "8_30"
    return "30_plus"


def install_age_seconds() -> float | None:
    """Seconds since this install was created, from the install-id file mtime.

    Reuses the same mtime seam as :func:`days_since_install_bucket`. Returns
    ``None`` on an unreadable / read-only filesystem (the caller buckets that to
    ``"unknown"``). Best-effort and never raises. Mirrors ``installAgeSeconds``
    in ``install-id.ts``.
    """
    try:
        mtime = _state_path().stat().st_mtime
    except OSError:
        return None
    return max(0.0, time.time() - mtime)


def is_first_run() -> bool:
    """Return ``True`` exactly once per install — on the run that first marks it.

    Powers the ``first_run`` activation event. Idempotent: the first call (when no
    marker exists) writes a marker and returns ``True``; every later call returns
    ``False``. Best-effort and never raises — an unwritable filesystem returns
    ``False`` (we simply never emit ``first_run`` rather than emitting it on every
    run). MUST only be called on the telemetry-enabled path (opting out never
    touches the filesystem). Mirrors ``isFirstRun`` in ``install-id.ts``.
    """
    path = _state_path().parent / "first-run"
    try:
        if path.exists():
            return False
    except OSError:
        return False
    legacy_dir = _legacy_state_dir()
    if legacy_dir is not None:
        try:
            if (legacy_dir / "first-run").exists():
                return False  # pre-0.6.8 marker — never re-emit first_run
        except OSError:
            return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1", encoding="utf-8")
        return True
    except OSError:
        return False


def _opt_out_path() -> Path:
    return _state_path().parent / "telemetry-disabled"


def is_opted_out() -> bool:
    """Whether a persisted opt-out marker exists (``getpatter telemetry disable``).

    Read-only — never writes, so checking consent never touches the filesystem.
    Also honors a marker written by pre-0.6.8 builds into the bare
    ``$XDG_STATE_HOME`` root: an opt-out must survive the state-dir move.
    Mirrors ``isOptedOut`` in ``install-id.ts``.
    """
    try:
        if _opt_out_path().exists():
            return True
    except OSError:
        pass
    legacy_dir = _legacy_state_dir()
    if legacy_dir is None:
        return False
    try:
        return (legacy_dir / "telemetry-disabled").exists()
    except OSError:
        return False


def set_opt_out(disabled: bool) -> None:
    """Create or remove the persisted opt-out marker.

    Used by the ``getpatter telemetry disable/enable`` CLI. Unlike the rest of this
    module it lets filesystem errors propagate so the CLI can report a failure to
    the user. Mirrors ``setOptOut`` in ``install-id.ts``.
    """
    path = _opt_out_path()
    if disabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1", encoding="utf-8")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        # Also clear a pre-0.6.8 marker in the bare $XDG_STATE_HOME root —
        # is_opted_out honors it, so leaving it behind would pin telemetry off.
        legacy_dir = _legacy_state_dir()
        if legacy_dir is not None:
            try:
                (legacy_dir / "telemetry-disabled").unlink()
            except FileNotFoundError:
                pass
