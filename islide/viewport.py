"""Viewport state and slide metadata. Pure Python, no openslide dependency."""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SlideMeta:
    """Slide pyramid metadata, decoupled from any slide backend."""

    dimensions: tuple[int, int]  # level 0 (w, h)
    level_count: int
    level_downsamples: tuple[float, ...]  # ascending, [0] == 1.0
    level_dimensions: tuple[tuple[int, int], ...]  # per level (w, h)
    mpp: float | None = None  # microns per level-0 pixel (mpp-x)
    vendor: str | None = None

    @property
    def dims(self) -> tuple[int, int]:
        return self.dimensions


@dataclass(frozen=True)
class Viewport:
    """The visible window: a slide-coordinate center plus a zoom factor.

    ``zoom`` is screen pixels per level-0 pixel (1.0 == full resolution).
    """

    cx: float  # level-0 x of canvas center
    cy: float  # level-0 y of canvas center
    zoom: float  # screen px per level-0 px
    canvas_w: int = 960
    canvas_h: int = 540

    @property
    def l0_bbox(self) -> tuple[float, float, float, float]:
        """Viewport in level-0 coordinates: (x0, y0, x1, y1), floats."""
        x0 = self.cx - self.canvas_w / (2.0 * self.zoom)
        y0 = self.cy - self.canvas_h / (2.0 * self.zoom)
        return (x0, y0, x0 + self.canvas_w / self.zoom, y0 + self.canvas_h / self.zoom)

    def with_zoom(self, zoom: float) -> "Viewport":
        return replace(self, zoom=zoom)

    def with_center(self, cx: float, cy: float) -> "Viewport":
        return replace(self, cx=cx, cy=cy)


def fit_zoom(meta: SlideMeta, canvas_w: int, canvas_h: int) -> float:
    """Zoom at which the entire slide fits inside the canvas."""
    w, h = meta.dimensions
    return min(canvas_w / w, canvas_h / h)
