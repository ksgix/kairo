"""Kairo's offline test suite.

Test modules share helpers by importing sibling modules directly
(``from test_work import create``). ``python3 -m unittest discover -s tests`` puts
``tests/`` on ``sys.path``, so that works there. This package init does the same for
the dotted form (``python3 -m unittest tests.test_deploy``).
"""
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
