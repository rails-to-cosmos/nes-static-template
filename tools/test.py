#!/usr/bin/env -S uv run
"""Run the template regression suite."""
from pathlib import Path
import unittest

suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parents[1] / 'tests'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
