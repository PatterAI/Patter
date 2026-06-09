#!/usr/bin/env python
"""Run the Patter Hermes preflight checks (wraps `patter hermes doctor`)."""

import subprocess
import sys

raise SystemExit(subprocess.call(["patter", "hermes", "doctor", *sys.argv[1:]]))
