"""Keep numerical regression tests independent of a desktop display server."""

import os

os.environ.setdefault("MPLBACKEND", "Agg")
