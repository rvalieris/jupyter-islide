"""M0 slide viewer widget.

M0 scope (see DESIGN.md §10): toolbar/slider driven, no mouse. Rendering is
an HTML tile composite (absolutely-positioned ``<img>`` tiles) so the whole
Python pipeline (plan -> fetch -> cache -> encode -> composite) is exercised
without a browser. The M1 JS canvas view will replace this compositor and
add mouse input; the plan/fetch/cache/encode modules are unchanged.
"""
from __future__ import annotations

import ipywidgets as widgets

from .backend import OpenSlideBackend
from .cache import TileCache
from .encode import jpeg_data_url
from .fetch import fetch_tiles
from .plan import ReadPlan, plan_viewport
from .viewport import Viewport, fit_zoom


class SlideViewer(widgets.Box):
    """Interactive WSI viewer (M0: buttons + slider, no mouse)."""

    def __init__(
        self,
        path: str,
        canvas_w: int = 960,
        canvas_h: int = 540,
        tile_size: int = 256,
        cache_max_mb: int = 256,
    ) -> None:
        super().__init__()
        # M0 opens synchronously (fast for local files; background open
        # lands with the M1 JS view, which renders the loading state).
        self.backend = OpenSlideBackend(path)
        self.meta = self.backend.meta
        self.cache = TileCache(int(cache_max_mb * 1024 * 1024))
        self.tile_size = tile_size

        fit = fit_zoom(self.meta, canvas_w, canvas_h)
        self._min_zoom = fit / 4.0
        self._max_zoom = 16.0
        self.viewport = Viewport(
            cx=self.meta.dimensions[0] / 2.0,
            cy=self.meta.dimensions[1] / 2.0,
            zoom=fit,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
        )

        self.html = widgets.HTML(layout={"width": f"{canvas_w}px"})
        self.info = widgets.Label()
        self.zoom_slider = widgets.FloatLogSlider(
            min=self._min_zoom,
            max=self._max_zoom,
            base=2,
            value=fit,
            description="zoom",
            layout={"width": "220px"},
        )
        self.btn_zoom_in = widgets.Button(description="+", tooltip="zoom in (×2)")
        self.btn_zoom_out = widgets.Button(description="−", tooltip="zoom out (÷2)")
        self.btn_fit = widgets.Button(description="fit", tooltip="fit whole slide")
        self.btn_1to1 = widgets.Button(description="1:1", tooltip="100% (level 0)")
        self.btn_recenter = widgets.Button(description="recenter", tooltip="slide center")
        for btn, fn in (
            (self.btn_zoom_in, self._zoom_in),
            (self.btn_zoom_out, self._zoom_out),
            (self.btn_fit, self._fit),
            (self.btn_1to1, self._one_to_one),
            (self.btn_recenter, self._recenter),
        ):
            btn.on_click(lambda _btn, fn=fn: fn())
        self.zoom_slider.observe(self._on_slider, names="value")

        self.children = [
            self.html,
            widgets.VBox(
                [
                    self.info,
                    widgets.HBox(
                        [self.btn_zoom_out, self.btn_zoom_in, self.btn_fit, self.btn_1to1]
                    ),
                    self.zoom_slider,
                    self.btn_recenter,
                ],
                layout={"width": "240px"},
            ),
        ]
        self.render()

    # ------------------------------------------------------------------ input
    def _on_slider(self, change) -> None:
        value = change["new"]
        if value is not None and abs(value - self.viewport.zoom) > 1e-12:
            self.set_zoom(float(value))

    def _zoom_in(self) -> None:
        self.set_zoom(self.viewport.zoom * 2.0)

    def _zoom_out(self) -> None:
        self.set_zoom(self.viewport.zoom / 2.0)

    def _fit(self) -> None:
        self.set_zoom(fit_zoom(self.meta, self.viewport.canvas_w, self.viewport.canvas_h))

    def _one_to_one(self) -> None:
        self.set_zoom(1.0)

    def _recenter(self) -> None:
        self.center_on(self.meta.dimensions[0] / 2.0, self.meta.dimensions[1] / 2.0)

    # ------------------------------------------------------------ public API
    def set_zoom(self, zoom: float, cx: float | None = None, cy: float | None = None) -> Viewport:
        """Set zoom (clamped), optionally moving the view center."""
        zoom = min(max(zoom, self._min_zoom), self._max_zoom)
        vp = self.viewport.with_zoom(zoom)
        if cx is not None and cy is not None:
            vp = vp.with_center(cx, cy)
        self.viewport = vp
        if abs(self.zoom_slider.value - zoom) > 1e-12:
            self.zoom_slider.value = zoom  # may re-enter _on_slider; it no-ops
        self.render()
        return self.viewport

    def center_on(self, cx: float, cy: float) -> Viewport:
        """Move the view center to slide (level-0) coordinates."""
        self.viewport = self.viewport.with_center(cx, cy)
        self.render()
        return self.viewport

    def viewport_bbox(self) -> tuple[float, float, float, float]:
        """Current view in level-0 coordinates, clamped to the slide: (x0, y0, x1, y1)."""
        x0, y0, x1, y1 = self.viewport.l0_bbox
        w, h = self.meta.dimensions
        return (max(0.0, x0), max(0.0, y0), min(float(w), x1), min(float(h), y1))

    def read_crop(self, bbox: tuple[float, float, float, float], level: int = 0):
        """Read a level-0-bbox region as a PIL image (level 0 by default)."""
        x0, y0, x1, y1 = (int(v) for v in bbox)
        w, h = self.meta.dimensions
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"empty crop bbox: {bbox}")
        return self.backend.read_region((x0, y0), level, (x1 - x0, y1 - y0))

    # --------------------------------------------------------------- rendering
    def render(self) -> ReadPlan:
        """Recompute tiles for the current viewport and update the display."""
        vp = self.viewport
        plan = plan_viewport(self.meta, vp, self.tile_size)
        tiles = fetch_tiles(self.backend, self.cache, plan)
        parts = []
        for t in plan.tiles:
            url = jpeg_data_url(tiles[t.key])
            left, top, w, h = t.screen
            parts.append(
                f'<img src="{url}" style="position:absolute;left:{left:.2f}px;top:{top:.2f}px;'
                f'width:{w:.2f}px;height:{h:.2f}px;"/>'
            )
        self.html.value = (
            f'<div style="position:relative;width:{vp.canvas_w}px;height:{vp.canvas_h}px;'
            f'overflow:hidden;background:#fff;border:1px solid #ccc;">'
            + " ".join(parts)
            + "</div>"
        )
        mpp = self.meta.mpp
        mpp_txt = f"{mpp / vp.zoom:.4g} µm/px" if mpp else "mpp n/a"
        self.info.value = (
            f"zoom {vp.zoom:.4g}×  ·  {mpp_txt}  ·  level {plan.level}/"
            f"{self.meta.level_count - 1}  ·  {len(plan.tiles)} tiles"
        )
        return plan

    def close(self) -> None:
        try:
            self.backend.close()
        finally:
            self._closed = True
            super().close()
