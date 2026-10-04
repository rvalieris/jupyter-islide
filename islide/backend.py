"""Slide backends.

``SlideMeta`` lives in :mod:`islide.viewport` (pure); this module provides
:class:`OpenSlideBackend`, the local-file backend over openslide-python's
object-oriented API (``openslide.open_slide``). It can also wrap an
*already-opened* slide object that speaks the same OO API
(:meth:`OpenSlideBackend.from_object`) — the seam for slide libraries that
mirror openslide's API over different slide types.

The ``SlideBackend`` interface is the seam for future remote backends
(openslide-server HTTP, S3, ...) — see docs/DESIGN.md §7.1.
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


def _object_path(slide) -> str:
    """Best-effort file path of an already-opened slide object (display only)."""
    p = getattr(slide, "path", None)
    if p is None:
        p = getattr(slide, "_filename", None)  # openslide-python OO objects
    return str(p or "")


class OpenSlideBackend:
    """WSI backed by an openslide object (openslide-python >= 1.4 OO API).

    Construct with a path (``OpenSlideBackend(path)`` — opened via
    ``openslide.open_slide``) or wrap an already-opened, openslide-compatible
    object with :meth:`from_object`.
    """

    #: Attributes an object must expose to be wrappable via ``from_object``
    #: (the openslide OO API surface islide uses — duck-typed, so any
    #: slide library mirroring that API works).
    REQUIRED_ATTRS = (
        "properties",
        "dimensions",
        "level_count",
        "level_downsamples",
        "level_dimensions",
        "read_region",
        "get_thumbnail",
        "close",
    )

    def __init__(self, path: str):
        import openslide  # imported here so pure modules stay import-light

        self.path = str(path)
        self._os = openslide.open_slide(self.path)
        self._init_meta()

    @classmethod
    def from_object(cls, slide) -> "OpenSlideBackend":
        """Wrap an already-opened, openslide-compatible slide object.

        ``slide`` is duck-typed: it must expose the openslide OO API surface
        islide uses (see :attr:`REQUIRED_ATTRS`). This is the seam for custom
        slide libraries that mirror openslide's API but back different slide
        types — ``openslide`` is never imported for a wrapped object, and
        :meth:`close` closes the wrapped object.
        """
        missing = [a for a in cls.REQUIRED_ATTRS if not hasattr(slide, a)]
        if missing:
            raise TypeError(
                "not an openslide-compatible slide object "
                f"(missing attributes: {', '.join(missing)}): {slide!r}"
            )
        self = cls.__new__(cls)
        self.path = _object_path(slide)
        self._os = slide
        self._init_meta()
        return self

    def _init_meta(self) -> None:
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
