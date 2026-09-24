"""Slide viewer widgets.

Two classes, two compositor generations (see DESIGN.md §6, §10):

* :class:`SlideViewer` — M1: a custom Jupyter widget (``DOMWidget`` subclass).
  The JS ``SlideView`` owns a canvas compositor and mouse/keyboard input;
  Python owns the slide, plans/fetches/caches/encodes tiles and pushes them
  as data URLs plus level-space geometry. Viewport changes flow back through
  the ``viewport`` trait (debounced by the view). Slide opening happens on a
  background thread so the kernel stays responsive; call :meth:`wait` to
  block until open (or error).
* :class:`HtmlSlideViewer` — M0: pure-ipywidgets ``Box`` with an HTML tile
  composite (absolutely positioned ``<img>`` tiles). No custom JS required;
  works in any ipywidgets 8 environment. Kept as a fallback for
  environments without the JupyterLab extension and as the reference
  implementation of the Python pipeline.

Both share the identical pipeline: ``plan_viewport`` -> ``fetch_tiles`` ->
``TileCache`` -> ``jpeg_data_url``.
"""
from __future__ import annotations

import threading
from typing import Any

import ipywidgets as widgets
from traitlets import Bool, Dict, Unicode

from .backend import OpenSlideBackend
from .cache import TileCache
from .encode import jpeg_data_url
from .fetch import fetch_tiles
from .plan import ReadPlan, plan_viewport
from .viewport import SlideMeta, Viewport, fit_zoom

__all__ = ["SlideViewer", "HtmlSlideViewer"]


# ---------------------------------------------------------------------------
# M1: custom widget (canvas view in JupyterLab / Notebook 7)
# ---------------------------------------------------------------------------


def _meta_dict(meta: SlideMeta) -> dict:
    return {
        "dimensions": list(meta.dimensions),
        "level_count": meta.level_count,
        "level_downsamples": list(meta.level_downsamples),
        "level_dimensions": [list(d) for d in meta.level_dimensions],
        "mpp": meta.mpp,
        "vendor": meta.vendor,
    }


def _vp_dict(vp: Viewport) -> dict:
    return {
        "cx": vp.cx,
        "cy": vp.cy,
        "zoom": vp.zoom,
        "canvas_w": vp.canvas_w,
        "canvas_h": vp.canvas_h,
    }


class SlideViewer(widgets.DOMWidget):
    """Interactive WSI viewer (M1): canvas compositor, Python-driven tiles.

    * Slide opening runs on a background thread; ``slide_open`` flips to
      ``True`` when ready (or ``status`` carries the error message).
    * ``viewport`` (level-0 center + zoom + canvas size) is the shared
      state. The JS view updates it from mouse/wheel input (debounced);
      :meth:`set_zoom` / :meth:`center_on` update it programmatically.
    * ``tiles`` maps ``"level:tx:ty"`` to a JPEG data URL; ``tile_geo``
      maps the same key to ``[level, ox, oy, cw, ch]`` in level pixels
      (absolute origin of the tile's crop). The view reprojects geometry
      under its local transform, so panning/zooming stays smooth between
      Python round-trips.
    """

    # -- widget identity (must match the JS module registered by the
    # JupyterLab extension; see frontend/labextension.js) -------------------
    _model_name = Unicode("SlideModel").tag(sync=True)
    _view_name = Unicode("SlideView").tag(sync=True)
    _model_module = Unicode("jupyter-islide").tag(sync=True)
    _view_module = Unicode("jupyter-islide").tag(sync=True)
    _model_module_version = Unicode("1.0.0").tag(sync=True)
    _view_module_version = Unicode("1.0.0").tag(sync=True)

    # -- synced state (Python <-> JS), see DESIGN.md §7 ---------------------
    slide_open = Bool(False).tag(sync=True)
    meta = Dict(default_value=None, allow_none=True).tag(sync=True)  # SlideMeta as a dict
    viewport = Dict(default_value=None, allow_none=True).tag(sync=True)  # _vp_dict
    tiles = Dict({}).tag(sync=True)  # "L:tx:ty" -> JPEG data URL
    tile_geo = Dict({}).tag(sync=True)  # "L:tx:ty" -> [level, ox, oy, cw, ch]
    minimap_img = Unicode("").tag(sync=True)  # whole-slide overview data URL
    last_click = Dict({}).tag(sync=True)  # {"x": .., "y": ..} level-0 px
    last_region = Dict({}).tag(sync=True)  # {"x": .., "y": .., "w": .., "h": ..}
    status = Unicode("").tag(sync=True)

    def __init__(
        self,
        path: str,
        canvas_w: int = 960,
        canvas_h: int = 540,
        tile_size: int = 256,
        cache_max_mb: int = 256,
    ) -> None:
        super().__init__()
        self.path = str(path)
        self._default_canvas = (int(canvas_w), int(canvas_h))
        self.tile_size = int(tile_size)
        self.cache = TileCache(int(cache_max_mb * 1024 * 1024))
        self.backend: OpenSlideBackend | None = None
        self._meta: SlideMeta | None = None
        self._min_zoom = 1e-9
        self._max_zoom = 16.0
        self._open_error: BaseException | None = None
        self._render_lock = threading.Lock()
        self._rendering_bg = False
        self._render_dirty = False
        self._syncing_viewport = False
        self._closed = False
        self.status = "opening slide…"
        self._open_thread = threading.Thread(
            target=self._open, name="islide-open", daemon=True
        )
        self._open_thread.start()
        self.observe(self._on_viewport_change, names="viewport")

    # ------------------------------------------------------------ open/close
    def _open(self) -> None:
        try:
            backend = OpenSlideBackend(self.path)
        except Exception as e:  # noqa: BLE001 - report via `status`
            self._open_error = e
            self.status = f"error opening slide: {e}"
            return
        meta = backend.meta
        self._meta = meta
        self._min_zoom = fit_zoom(meta, *self._default_canvas) / 4.0
        self.backend = backend
        self.meta = _meta_dict(meta)
        self.minimap_img = self._overview_data_url(backend, meta)
        self.status = (
            f"ready: {meta.dimensions[0]}×{meta.dimensions[1]}, "
            f"{meta.level_count} levels"
        )
        self.slide_open = True
        if self.viewport is None:
            # Headless (no JS view attached): pick the fit viewport. A JS
            # view, once displayed, sends its own fit viewport (with the
            # real canvas size), so this only sticks headless.
            fit = fit_zoom(meta, *self._default_canvas)
            self.viewport = _vp_dict(
                Viewport(
                    meta.dimensions[0] / 2.0,
                    meta.dimensions[1] / 2.0,
                    fit,
                    *self._default_canvas,
                )
            )
        self._render_once()

    def _overview_data_url(self, backend, meta: SlideMeta, height: int = 180) -> str:
        w, h = meta.dimensions
        mw = max(1, round(height * w / h))
        return jpeg_data_url(backend.thumbnail((mw, height)))

    def wait(self, timeout: float | None = None) -> None:
        """Block until the slide is open (or raise if it failed to open)."""
        self._open_thread.join(timeout)
        if self._open_error is not None:
            raise RuntimeError(f"failed to open slide: {self._open_error}")
        if not self.slide_open:
            raise TimeoutError("slide still opening")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._open_thread.join()  # let the open (or its failure) settle
        self.slide_open = False
        if self.backend is not None:
            self.backend.close()
            self.backend = None
        super().close()

    # ----------------------------------------------------------- state access
    @property
    def _vp_current(self) -> Viewport:
        vp = self.viewport
        if vp is None:
            meta = self._meta
            assert meta is not None
            cw, ch = self._default_canvas
            fit = fit_zoom(meta, cw, ch)
            return Viewport(
                meta.dimensions[0] / 2.0,
                meta.dimensions[1] / 2.0,
                fit,
                canvas_w=cw,
                canvas_h=ch,
            )
        return Viewport(
            vp["cx"], vp["cy"], vp["zoom"],
            canvas_w=vp["canvas_w"], canvas_h=vp["canvas_h"],
        )

    # ------------------------------------------------------ viewport handling
    def _set_viewport_sync(self, vp: Viewport) -> None:
        """Apply a viewport from the Python side and render synchronously."""
        assert self._meta is not None
        zoom = min(max(vp.zoom, self._min_zoom), self._max_zoom)
        vp = vp.with_zoom(zoom)
        self._syncing_viewport = True
        try:
            self.viewport = _vp_dict(vp)
        finally:
            self._syncing_viewport = False
        self._render_once()

    def _on_viewport_change(self, change: dict) -> None:
        # Python-side sets (programmatic API) render inline; JS-originated
        # updates (comm) render on a background thread with coalescing.
        if not self.slide_open or self._syncing_viewport:
            return
        self._schedule_render()

    def _schedule_render(self) -> None:
        with self._render_lock:
            if self._rendering_bg:
                self._render_dirty = True
                return
            self._rendering_bg = True
        threading.Thread(
            target=self._render_loop, name="islide-render", daemon=True
        ).start()

    def _render_loop(self) -> None:
        try:
            while True:
                self._render_once()
                with self._render_lock:
                    dirty = self._render_dirty
                    self._render_dirty = False
                if not dirty:
                    return
        finally:
            with self._render_lock:
                self._rendering_bg = False

    # --------------------------------------------------------------- rendering
    def _render_once(self) -> ReadPlan | None:
        """Plan + fetch + encode for the current viewport; push to the view."""
        if self._closed or not self.slide_open or self._meta is None:
            return None
        vp = self._vp_current
        plan = plan_viewport(self._meta, vp, self.tile_size)
        tiles = fetch_tiles(self.backend, self.cache, plan)  # type: ignore[arg-type]
        geo: dict[str, list[int]] = {}
        urls: dict[str, str] = {}
        rx, ry = plan.read_origin
        for t in plan.tiles:
            k = f"{t.key[0]}:{t.key[1]}:{t.key[2]}"
            x0, y0, x1, y1 = t.crop
            geo[k] = [t.key[0], rx + x0, ry + y0, x1 - x0, y1 - y0]
            urls[k] = jpeg_data_url(tiles[t.key])
        self.tiles = urls
        self.tile_geo = geo
        mpp = self._meta.mpp
        mpp_txt = f" · {mpp / vp.zoom:.4g} µm/px" if mpp else ""
        self.status = (
            f"zoom {vp.zoom:.4g}×{mpp_txt} · level {plan.level}/"
            f"{self._meta.level_count - 1} · {len(plan.tiles)} tiles"
        )
        return plan

    def render(self) -> ReadPlan:
        """Synchronous render of the current viewport (headless/programmatic)."""
        if not self.slide_open:
            self.wait()
        plan = self._render_once()
        assert plan is not None
        return plan

    # --------------------------------------------------------------- public API
    def set_zoom(self, zoom: float, cx: float | None = None, cy: float | None = None) -> Viewport:
        """Set zoom (clamped), optionally moving the view center."""
        if not self.slide_open:
            self.wait()
        z = min(max(float(zoom), self._min_zoom), self._max_zoom)
        vp = self._vp_current.with_zoom(z)
        if cx is not None and cy is not None:
            vp = vp.with_center(cx, cy)
        self._set_viewport_sync(vp)
        return vp

    def center_on(self, cx: float, cy: float) -> Viewport:
        """Move the view center to slide (level-0) coordinates."""
        if not self.slide_open:
            self.wait()
        vp = self._vp_current.with_center(float(cx), float(cy))
        self._set_viewport_sync(vp)
        return vp

    def viewport_bbox(self) -> tuple[float, float, float, float]:
        """Current view in level-0 coordinates, clamped to the slide: (x0, y0, x1, y1)."""
        if not self.slide_open:
            self.wait()
        x0, y0, x1, y1 = self._vp_current.l0_bbox
        w, h = self._meta.dimensions  # type: ignore[union-attr]
        return (max(0.0, x0), max(0.0, y0), min(float(w), x1), min(float(h), y1))

    def read_crop(self, bbox: tuple[float, float, float, float], level: int = 0):
        """Read a level-0-bbox region as a PIL image (level 0 by default)."""
        if not self.slide_open:
            self.wait()
        x0, y0, x1, y1 = (int(v) for v in bbox)
        w, h = self._meta.dimensions  # type: ignore[union-attr]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"empty crop bbox: {bbox}")
        assert self.backend is not None
        return self.backend.read_region((x0, y0), level, (x1 - x0, y1 - y0))


# ---------------------------------------------------------------------------
# M0: pure-ipywidgets HTML tile composite (fallback / reference pipeline)
# ---------------------------------------------------------------------------

# Seam margin (screen px): each <img> box is inflated by this much on every
# side, so screen-adjacent tiles overlap by ~2*_SEAM_MARGIN_PX px. The
# browser lays out and rounds each <img>'s fractional CSS box independently,
# so without the overlap the shared edge can end up only partially covered
# by both neighbours, letting the white container background show through
# as a hairline. The stretch (1 px on a 256+ px tile) is imperceptible.
# Mirrors SEAM_MARGIN in frontend/compositor.js.
_SEAM_MARGIN_PX = 0.5


class HtmlSlideViewer(widgets.Box):
    """Toolbar/slider-driven WSI viewer without custom JS (M0).

    The display is an HTML composite of absolutely-positioned ``<img>``
    tiles; it works in any ipywidgets 8 environment (no JupyterLab
    extension needed) at the cost of no mouse input. Tiles are drawn
    inflated by a sub-pixel seam margin (see ``_SEAM_MARGIN_PX``) so
    adjacent tiles overlap and no white hairline can show through.
    """

    def __init__(
        self,
        path: str,
        canvas_w: int = 960,
        canvas_h: int = 540,
        tile_size: int = 256,
        cache_max_mb: int = 256,
    ) -> None:
        super().__init__()
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
            m = _SEAM_MARGIN_PX
            parts.append(
                f'<img src="{url}" style="position:absolute;'
                f'left:{left - m:.2f}px;top:{top - m:.2f}px;'
                f'width:{w + 2 * m:.2f}px;height:{h + 2 * m:.2f}px;"/>'
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
