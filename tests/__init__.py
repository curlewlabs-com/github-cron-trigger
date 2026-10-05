"""Run from the repository root with `python3 -m unittest discover -s tests -t .`;
the package is imported from src/ without being installed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
