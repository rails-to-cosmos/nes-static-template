#!/usr/bin/env -S uv run --managed-python --script
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = []
# ///
"""Run the template regression suite from any working directory."""
from pathlib import Path
import unittest

suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parents[1] / 'tests'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
