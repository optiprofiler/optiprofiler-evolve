"""Keep numerical regression tests independent of a desktop display server."""

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("MPLBACKEND", "Agg")

_REAL_CHMOD = os.chmod


def _protected_directories() -> frozenset[Path]:
    """Directories a test must never re-permission: the temp root, the checkout, their ancestors."""
    anchors = (Path(tempfile.gettempdir()), Path(__file__).parent.parent, Path.home())
    protected: set[Path] = set()
    for anchor in anchors:
        anchor = anchor.resolve()
        protected.update((anchor, *anchor.parents))
    return frozenset(protected)


@pytest.fixture(autouse=True, scope="session")
def _refuse_chmod_outside_test_directories():
    """A root test run once set ``/`` and ``/tmp`` to 0700 through a trace helper."""
    protected = _protected_directories()

    def chmod(path, mode, *args, **kwargs):
        if not isinstance(path, int) and Path(os.fspath(path)).resolve() in protected:
            # Not an OSError: the helpers under test deliberately swallow those.
            raise AssertionError(f"test attempted to chmod protected directory {path!r}")
        return _REAL_CHMOD(path, mode, *args, **kwargs)

    os.chmod = chmod  # Path.chmod delegates here
    try:
        yield
    finally:
        os.chmod = _REAL_CHMOD
