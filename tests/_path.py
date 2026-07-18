"""Make src/ importable for tests (run via `python -m unittest discover tests`)."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))
