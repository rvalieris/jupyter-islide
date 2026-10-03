"""Shared pytest fixtures for the islide test suite."""
from __future__ import annotations

import pytest

from util import SLIDE_PATH, SLIDE_URL, ensure_slide


@pytest.fixture(scope="session")
def slide_path() -> str:
    """Path to the real test slide, ``data/CMU-1.tiff``.

    Downloaded at test time from openslide-testdata if missing (see
    ``util.ensure_slide``); tests depending on this fixture are skipped
    when the file is absent and the download fails.
    """
    if not ensure_slide():
        pytest.skip(f"test slide {SLIDE_PATH.name} missing and not "
                    f"downloadable from {SLIDE_URL}")
    return str(SLIDE_PATH)
