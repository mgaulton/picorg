"""Keep repository modules importable under all supported pytest launch modes."""

import sys
from pathlib import Path


REPO_ROOT = str(Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
