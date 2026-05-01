"""Pre-commit hook: refuse to commit a notebook whose JSON contains
high-entropy secrets in cell source or outputs.

Patterns mirrored from .claude/hooks/scan-sensitive-on-write.sh.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = [
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

WHITELIST_PATHS = (
    "fixtures/keys/telnyx_test_ed25519_priv.pem",
)


def main() -> int:
    failed = 0
    for arg in sys.argv[1:]:
        p = Path(arg)
        if any(w in str(p) for w in WHITELIST_PATHS):
            continue
        body = p.read_text()
        for pat in PATTERNS:
            m = pat.search(body)
            if m:
                print(f"❌ {p}: matches {pat.pattern!r} at offset {m.start()}")
                failed = 1
    return failed


if __name__ == "__main__":
    sys.exit(main())
