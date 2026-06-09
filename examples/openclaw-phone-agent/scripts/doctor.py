#!/usr/bin/env python
"""Run the Patter OpenClaw preflight checks (wraps `patter openclaw doctor`)."""

import subprocess
import sys

raise SystemExit(subprocess.call(["patter", "openclaw", "doctor", *sys.argv[1:]]))
