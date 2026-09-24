"""Slide backends.

``SlideMeta`` lives in :mod:`islide.viewport` (pure); this module provides
:class:`OpenSlideBackend`, the local-file backend over openslide-python's
object-oriented API (``openslide.open_slide``).

The ``SlideBackend`` interface is the seam for future remote backends
(openslide-server HTTP, S3, ...) — see DESIGN.md §7.1.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .viewport import SlideMeta


@runtime_checkable
class SlideBackend(Protocol):
    meta: SlideMeta

    def read_region(self, location: tuple[int, int], level: int, size: tuple[int, int]):
        """Read a region. ``location`` in level-0 px, ``size`` in level px.

        Returns a PIL image of exactly ``size`` (vendor fills out-of-bounds).
        """
        ...

    def thumbnail(self, size: tuple[int, int]):
        ...

    def close(self) -> None:
        ...


def _parse_mpp(props: dict[str, str]) -> float | None:
    raw = props.get("openslide.mpp-x")
    if raw is None:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v > 0 else None


class OpenSlideBackend:
    """Local WSI file backed by libopenslide (via openslide-python >= 1.4)."""

    def __init__(self, path: str):
        import openslide  # imported here so pure modules stay import-light

        self.path = str(path)
        self._os = openslide.open_slide(self.path)
        props = dict(self._os.properties)
        self.meta = SlideMeta(
            dimensions=tuple(self._os.dimensions),
            level_count=int(self._os.level_count),
            level_downsamples=tuple(self._os.level_downsamples),
            level_dimensions=tuple(self._os.level_dimensions),
            mpp=_parse_mpp(props),
            vendor=props.get("openslide.vendor"),
        )

    def read_region(self, location: tuple[int, int], level: int, size: tuple[int, int]):
        return self._os.read_region(tuple(location), int(level), tuple(size))

    def thumbnail(self, size: tuple[int, int]):
        return self._os.get_thumbnail(tuple(size))

    def close(self) -> None:
        if self._os is None:
            return
        try:
            self._os.close()
        finally:
            self._os = None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        w, h = self.meta.dimensions
        return f"OpenSlideBackend({self.path!r}, {w}x{h}, {self.meta.level_count} levels)"
