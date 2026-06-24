"""Run backend unit tests with local dependency paths configured."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TEST_DEPS = ROOT / ".testdeps"

if TEST_DEPS.exists():
    sys.path.insert(0, str(TEST_DEPS))
sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
