"""Unit tests of Flight Studio's own Python code; none of them needs a built or running cFS."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
