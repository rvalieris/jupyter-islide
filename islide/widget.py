"""Slide viewer widget.

:class:`SlideViewer` is a custom Jupyter widget (``DOMWidget`` subclass;
see docs/DESIGN.md §6, §7). The JS ``SlideView`` owns a canvas compositor and
mouse/keyboard input; Python owns the slide, plans/fetches/caches/encodes
tiles and pushes them as data URLs plus level-space geometry. Viewport
changes flow back through the ``viewport`` trait (debounced by the view).
Slide opening happens synchronously in the constructor: by the time
``SlideViewer(...)`` returns, the slide is open (or the constructor raised),
so the widget's first sync to the frontend already carries the full state.

Pipeline: ``plan_viewport`` -> per-chunk ``fetch_chunk`` -> ``TileCache``
-> ``jpeg_data_url``.
"""
from __future__ import annotations

import json
import threading
import time
import warnings
from pathlib import Path
from typing import Any

import ipywidgets as widgets
from PIL import Image, ImageChops
from traitlets import Bool, Dict, Float, Int, List, TraitError, Unicode, validate

from .annotations import apply_edit, normalize_ring, parse_annotations
from .backend import OpenSlideBackend, _object_path
from .cache import TileCache
from .encode import jpeg_data_url, png_data_url
from .fetch import fetch_chunk
from .plan import Chunk, ReadPlan, plan_viewport
from .viewport import SlideMeta, Viewport, fit_zoom

__all__ = ["SlideViewer"]

# The empty canonical annotation document (docs/DESIGN.md §6.3); the
# `annotations` trait default, coerced through the normalizer on access.
_EMPTY_ANNOTATION_DOC = {"type": "FeatureCollection", "features": []}

# Overlay aspect tolerance: a full-slide image (e.g. a get_thumbnail
# output) matches the slide's aspect ratio up to integer rounding.
_OVERLAY_ASPECT_TOL = 0.01


def _load_overlay_image(source: str | Path | Image.Image) -> Image.Image:
    """A full-slide overlay source (PNG path or PIL image) as an RGBA image."""
    if isinstance(source, (str, Path)):
        with Image.open(source) as im:
            return im.convert("RGBA")
    if isinstance(source, Image.Image):
        return source.convert("RGBA")
    raise TypeError("source must be a PNG file path or a PIL image")


def _check_overlay_aspect(img: Image.Image, slide_w: int, slide_h: int) -> None:
    """The overlay is stretched over the whole slide, so its aspect ratio
    must match the slide's (within the rounding tolerance)."""
    ratio = (img.width * slide_h) / (img.height * slide_w)
    if abs(ratio - 1.0) > _OVERLAY_ASPECT_TOL:
        raise ValueError(
            "overlay aspect ratio does not match the slide "
            f"({img.width}×{img.height} vs {slide_w}×{slide_h}); "
            "expected a full-slide image (e.g. a get_thumbnail output)"
        )


def _validate_transparent_key(
    key: tuple[int, int, int] | None,
) -> tuple[int, int, int] | None:
    """Coerce/validate an overlay transparent-key (RGB triple or None)."""
    if key is None:
        return None
    try:
        r, g, b = (int(c) for c in key)
    except (TypeError, ValueError) as e:
        raise TypeError(
            "transparent must be an (r, g, b) triple or None, got "
            f"{key!r}"
        ) from e
    if not all(0 <= c <= 255 for c in (r, g, b)):
        raise ValueError(f"transparent channels must be in 0..255, got {key!r}")
    return (r, g, b)


def _apply_transparency(img: Image.Image, key: tuple[int, int, int] | None) -> None:
    """Make every pixel exactly equal to the RGB ``key`` fully transparent
    (in place on the RGBA image); every other pixel keeps its own alpha.
    ``None`` keeps the image's own alpha channel untouched.

    PIL-only: the per-channel difference from the key is non-zero in
    exactly one channel per mismatch, so the per-pixel max (lighter of
    lighter) is 0 iff all three match.
    """
    if key is None:
        return
    r, g, b = (int(c) for c in key)
    diff = ImageChops.difference(
        img.convert("RGB"), Image.new("RGB", img.size, (r, g, b))
    )
    dr, dg, db = diff.split()
    mismatch = ImageChops.lighter(ImageChops.lighter(dr, dg), db)
    mask = mismatch.point(lambda v: 255 if v else 0)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))


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
    # The viewport wire form: the canvas height is NOT part of it — the
    # `canvas_h` synced trait is the source of truth on both sides
    # (`canvas_w` is: it is JS-owned, the width the view actually has).
    return {
        "cx": vp.cx,
        "cy": vp.cy,
        "zoom": vp.zoom,
        "canvas_w": vp.canvas_w,
    }


def _require_slide_source(path: str | None, slide: Any | None) -> tuple[Any | None, str]:
    """Validate the two ways a slide is provided: a file ``path`` or an
    already-opened, openslide-compatible ``slide`` object (exactly one).

    Returns ``(slide_or_None, path)``; ``path`` is display-only (it may come
    from the slide object itself).
    """
    if path is None and slide is None:
        raise ValueError(
            "provide a slide path or an opened, openslide-compatible slide object"
        )
    if path is not None and slide is not None:
        raise ValueError("provide either a slide path or an opened slide object, not both")
    if slide is not None:
        return slide, _object_path(slide)
    return None, str(path)


class SlideViewer(widgets.DOMWidget):
    """Interactive WSI viewer: canvas compositor, Python-driven tiles.

    Construct with a file path (``SlideViewer(path)``) or, for slide
    libraries that mirror openslide's API over other slide types, with an
    already-opened slide object (``SlideViewer(slide=obj)``) — exactly one
    of the two.

    * Slide opening is synchronous: by the time the constructor returns the
      slide is open (``slide_open`` is ``True`` and the initial viewport and
      tiles are set), or the constructor raised. Displaying the widget right
      after construction is safe — its first sync to the frontend carries the
      full state.
    * ``viewport`` (level-0 center + zoom + canvas size) is the shared
      state. The JS view updates it from mouse/wheel input (debounced);
      :meth:`set_zoom` / :meth:`center_on` update it programmatically.
    * ``tiles`` maps ``"level:tx:ty"`` to a JPEG data URL; ``tile_geo``
      maps the same key to ``[level, ox, oy, cw, ch]`` in level pixels
      (absolute origin of the tile's crop). The view reprojects geometry
      under its local transform, so panning/zooming stays smooth between
      Python round-trips. A render pushes one chunk at a time
      (viewport-center-first; docs/DESIGN.md §6.6.2), so the traits hold
      the *last* chunk (last-event semantics) while the JS view
      accumulates the render's pushes into the full viewport.
    * ``canvas_h`` (synced int, CSS px) is the user-settable on-screen
      viewport height: set it at construction or at runtime
      (``v.canvas_h = 900``). It is the source of truth for canvas
      height on both sides — the JS view applies it to the canvas, and
      the Python observer re-plans the current viewport at the new
      height (it is not part of the ``viewport`` wire form).
    * ``image_cache_max`` (synced int) is the single decoded-tile cache
      cap: the JS view's decoded image cache *and* the kernel's
      :class:`TileCache` (both LRU, same ``level:tx:ty`` keys, counted
      in tiles; ``v.image_cache_max = 2000``, default 1000). Setting it
      at runtime re-limits both caches (a decrease evicts immediately).
    * ``annotation_edit`` (JS->Py last-event slot, docs/DESIGN.md §6.5)
      carries one view edit command (``delete`` / ``set_label`` /
      ``set_color`` / ``set_vertex`` / ``add_vertex``); the Python
      observer applies it with the pure ``apply_edit`` and pushes the
      updated ``annotations`` set.
    """

    # -- widget identity (must match the JS module registered by the
    # JupyterLab extension; see frontend/labextension.js) -------------------
    _model_name = Unicode("SlideModel").tag(sync=True)
    _view_name = Unicode("SlideView").tag(sync=True)
    _model_module = Unicode("jupyter-islide").tag(sync=True)
    _view_module = Unicode("jupyter-islide").tag(sync=True)
    _model_module_version = Unicode("2.1.0").tag(sync=True)
    _view_module_version = Unicode("2.1.0").tag(sync=True)

    # -- synced state (Python <-> JS), see docs/DESIGN.md §7 ---------------------
    slide_open = Bool(False).tag(sync=True)
    meta = Dict(default_value=None, allow_none=True).tag(sync=True)  # SlideMeta as a dict
    viewport = Dict(default_value=None, allow_none=True).tag(sync=True)  # _vp_dict
    canvas_h = Int(540).tag(sync=True)  # on-screen viewport height (CSS px)
    # The single decoded-tile cache cap (tile count): the JS view's decoded
    # image cache and the kernel TileCache (both LRU, same keys).
    image_cache_max = Int(1000).tag(sync=True)

    @validate("canvas_h")
    def _check_canvas_h(self, proposal):
        v = int(proposal["value"])
        if v <= 0:
            raise TraitError("canvas_h must be a positive number of CSS px")
        return v

    @validate("image_cache_max")
    def _check_image_cache_max(self, proposal):
        v = int(proposal["value"])
        if v <= 0:
            raise TraitError("image_cache_max must be a positive int")
        return v

    tiles = Dict({}).tag(sync=True)  # "L:tx:ty" -> JPEG data URL
    tile_geo = Dict({}).tag(sync=True)  # "L:tx:ty" -> [level, ox, oy, cw, ch]
    minimap_img = Unicode("").tag(sync=True)  # whole-slide overview data URL
    # Overlay: a full-slide image (e.g. a model heatmap rendered at the
    # slide's get_thumbnail scale) drawn over the tiles and under the
    # annotations. `overlay_img` is a PNG data URL ("" = no overlay);
    # `overlay_alpha` its opacity in [0, 1].
    overlay_img = Unicode("").tag(sync=True)
    overlay_alpha = Float(0.5).tag(sync=True)

    @validate("overlay_alpha")
    def _check_overlay_alpha(self, proposal):
        v = float(proposal["value"])
        if not 0.0 <= v <= 1.0:
            raise TraitError("overlay_alpha must be in [0, 1]")
        return v
    # The last drawn polygon ring (JS -> Py): an open position list in
    # unclamped level-0 px, or None (the last-event slot; docs/DESIGN.md §6.4).
    # The Python observer is the final authority: valid ring -> append a
    # polygon feature to `annotations` (fresh id, empty properties);
    # degenerate ring -> warn + status, no state change.
    last_polygon = List(default_value=None, allow_none=True).tag(sync=True)
    # The canonical annotation document in level-0 px (Py->JS;
    # the JS view renders it read-only on an overlay canvas; docs/DESIGN.md
    # §6.3): a GeoJSON FeatureCollection of {type, id, geometry,
    # properties} features. Any assignment is coerced through the
    # normalizer (validate below), so the trait always holds the
    # canonical document.
    annotations = Dict(default_value=_EMPTY_ANNOTATION_DOC).tag(sync=True)
    # The last issued annotation edit command (JS -> Py): a
    # {"op": "delete" | "set_label" | "set_color" | "set_vertex" | "add_vertex",
    # "id", ...} object or None (no command yet; docs/DESIGN.md §6.5). The
    # Python observer applies it to `annotations` (pure apply_edit) and
    # pushes the updated set; an unknown/stale id leaves the set untouched
    # (a status note instead). Last-event slot: the trait carries the
    # last issued command (a re-attached view applies it to its own
    # state); the observer is not re-triggered on re-attach.
    annotation_edit = Dict(default_value=None, allow_none=True).tag(sync=True)
    # The attach re-render counter (JS -> Py): the JS view bumps it once
    # per attach (last-event counter, like last_polygon / annotation_edit
    # — the trait carries the last issued command; a re-attached view
    # simply applies it to its own state).
    # A re-attached view seeds its image cache from the traits' current
    # value — the *last* chunk of the last render (per-chunk pushes,
    # docs/DESIGN.md §6.6.2) — and its initial fit-echo can be a no-op on
    # the Python side (traitlets fires no observer for a value-equal
    # set), so the full-set re-render is requested explicitly: the
    # observer schedules a render of the current viewport.
    resync = Int(0).tag(sync=True)
    status = Unicode("").tag(sync=True)

    @validate("annotations")
    def _check_annotations(self, proposal):
        """Coerce any assignment through the normalizer."""
        try:
            return parse_annotations(proposal["value"])
        except ValueError as e:
            raise TraitError(str(e)) from e

    def __init__(
        self,
        path: str | None = None,
        canvas_w: int = 960,
        canvas_h: int = 540,
        tile_size: int = 256,
        image_cache_max: int = 1000,
        *,
        slide: Any | None = None,
        jpeg_quality: int = 85,
    ) -> None:
        """Create a viewer from a file path, or from an already-opened
        openslide-compatible object (``slide=``; exactly one of the two).

        ``jpeg_quality`` (1–95) is the JPEG quality of the tile data URLs
        (the minimap/thumbnail keep the default 85).

        ``image_cache_max`` caps the decoded-tile image caches — the
        JS view's decoded images *and* the kernel's :class:`TileCache`
        (both LRU, counted in tiles; set at construction or at runtime
        via ``v.image_cache_max = N``).
        """
        super().__init__()
        # Teardown state before anything that can raise, so __del__/close()
        # stay safe on a half-constructed widget.
        self._closed = False
        self.backend: OpenSlideBackend | None = None
        try:
            q = int(jpeg_quality)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"jpeg_quality must be an int in 1..95, got {jpeg_quality!r}"
            ) from e
        if not 1 <= q <= 95:
            raise ValueError(f"jpeg_quality must be in 1..95, got {jpeg_quality!r}")
        slide_obj, self.path = _require_slide_source(path, slide)
        self._initial_slide = slide_obj
        self._jpeg_quality = q
        self._canvas_w = int(canvas_w)
        self.canvas_h = int(canvas_h)
        self.image_cache_max = int(image_cache_max)
        self.tile_size = int(tile_size)
        self.cache = TileCache(int(image_cache_max))
        self._meta: SlideMeta | None = None
        self._min_zoom = 1e-9
        self._max_zoom = 16.0
        self._render_lock = threading.Lock()
        self._rendering_bg = False
        self._render_dirty = False
        self._syncing_viewport = False
        self.status = "opening slide…"
        # Open the slide synchronously: by the time the constructor returns
        # the widget is fully open (or this raised). Observers are registered
        # first so the initial viewport set / render below behave the same as
        # any later programmatic update.
        self.observe(self._on_viewport_change, names="viewport")
        self.observe(self._on_canvas_h_change, names="canvas_h")
        self.observe(self._on_image_cache_max_change, names="image_cache_max")
        self.observe(self._on_last_polygon_change, names="last_polygon")
        self.observe(self._on_annotation_edit_change, names="annotation_edit")
        self.observe(self._on_resync_change, names="resync")
        self._open()

        # Frontend-initiated disposal (e.g. clearing the cell output) sends
        # a comm close without any Python-side dispose hook in ipywidgets 8,
        # and a kernel-side close() likewise does not fire the comm's
        # on_close. Register the handler explicitly so the OpenSlide handle
        # is released on the frontend path too. _close_backend is
        # idempotent, so this is safe whether or not close() was also
        # called from Python.
        if self.comm is not None:
            self.comm.on_close = lambda msg: self._close_backend()

    # ------------------------------------------------------------ open/close
    def _open(self) -> None:
        if self._initial_slide is not None:
            backend = OpenSlideBackend.from_object(self._initial_slide)
        else:
            backend = OpenSlideBackend(self.path)
        meta = backend.meta
        self._meta = meta
        self._min_zoom = fit_zoom(meta, *self._default_canvas) / 4.0
        self.backend = backend
        self.meta = _meta_dict(meta)
        self.minimap_img = self._overview_data_url(backend, meta)
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
        self.status = (
            f"ready: {meta.dimensions[0]}×{meta.dimensions[1]}, "
            f"{meta.level_count} levels"
        )
        self.slide_open = True
        self._render_once()

    def _overview_data_url(self, backend, meta: SlideMeta, height: int = 180) -> str:
        w, h = meta.dimensions
        mw = max(1, round(height * w / h))
        return jpeg_data_url(backend.thumbnail((mw, height)))

    def close(self) -> None:
        """Close the slide handle and release the widget.

        Idempotent — safe to call multiple times, or after a re-run cell
        created a new widget while this one was still open. Blocks until
        an in-flight background render finishes, so the render thread
        never touches a closed slide handle.
        """
        deadline = time.monotonic() + 10
        while self._rendering_bg and time.monotonic() < deadline:
            time.sleep(0.01)
        self._close_backend()
        super().close()

    def _close_backend(self) -> None:
        """Release the OpenSlide handle (idempotent; also run from the
        comm on_close handler when the frontend disposes the widget)."""
        if self._closed:
            return
        self._closed = True
        self.slide_open = False
        if self.backend is not None:
            self.backend.close()
            self.backend = None

    # ----------------------------------------------------------- state access
    @property
    def _default_canvas(self) -> tuple[int, int]:
        """Planning canvas size: constructor width + the user-settable
        ``canvas_h`` trait. Used for the headless initial fit and the
        min-zoom clamp."""
        return (self._canvas_w, int(self.canvas_h))

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
            canvas_w=vp["canvas_w"], canvas_h=int(self.canvas_h),
        )

    def _on_canvas_h_change(self, change: dict) -> None:
        """Canvas height changed (user-settable synced trait): keep the
        min-zoom clamp in step, and re-plan the current viewport at the
        new canvas size — the height is not part of the ``viewport`` wire
        form (planning reads it from this trait), so a plain render of
        the unchanged viewport is what picks it up, headless or with a
        JS view attached (the view's ResizeObserver resizes the canvas;
        its debounced send is deduped — same wire form)."""
        if self._meta is not None:
            self._min_zoom = fit_zoom(self._meta, self._canvas_w, int(self.canvas_h)) / 4.0
        if self.viewport is not None:
            self._schedule_render()

    def _on_image_cache_max_change(self, change: dict) -> None:
        """Runtime cap change: re-limit the kernel cache (the JS view
        re-limits its own from the trait). A decrease evicts immediately;
        evicted tiles are re-fetched on the next render. The observer is
        registered after the constructor's initial assignment, so it only
        fires for later (user/wire) changes."""
        self.cache.relimit(int(self.image_cache_max))

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
                        # Hand off *under the lock*: a superseding
                        # viewport registered between the last render
                        # and this check must not be lost — clearing the
                        # flag outside the lock would let _schedule_render
                        # see flag=True and start nothing, and this
                        # thread's flag clear would then swallow the
                        # dirty render.
                        self._rendering_bg = False
                        return
        finally:
            with self._render_lock:
                self._rendering_bg = False

    # --------------------------------------------------------------- rendering
    def _render_once(self) -> ReadPlan | None:
        """Plan + fetch + encode for the current viewport; push to the
        view.

        The plan's chunks are pushed one at a time in the plan's
        viewport-center-first order (docs/DESIGN.md §6.6.2): each chunk
        is read, encoded, and pushed as its own ``tiles``/``tile_geo``
        assignment as soon as its read lands — progressive center-out
        visibility, each tile's crop read exactly once per render, and
        no follow-up push re-sending the center chunk. A single-chunk
        plan pushes once. The JS view accumulates a render's pushes
        (its image cache is a merge, not a replace), so the render's
        final frame is the full viewport while the traits hold only the
        last chunk (last-event semantics).
        """
        if self._closed or not self.slide_open or self._meta is None:
            return None
        vp = self._vp_current
        plan = plan_viewport(self._meta, vp, self.tile_size)
        for chunk in plan.chunks:
            self._push_chunk(
                plan, chunk, fetch_chunk(self.backend, self.cache, chunk, plan)
            )
        # Pipeline info only: the live zoom + µm/px belong to the JS
        # readout (which tracks the local transform); repeating them here
        # would show the zoom twice — and this line lags the pointer by
        # the sync debounce anyway. It reports the last completed render.
        self.status = (
            f"level {plan.level}/{self._meta.level_count - 1} · "
            f"{len(plan.tiles)} tiles"
        )
        return plan

    def _push_chunk(
        self,
        plan: ReadPlan,
        chunk: Chunk,
        chunk_tiles: dict[tuple, Any],
    ) -> None:
        """Encode one chunk's tiles and push them as the ``tiles`` +
        ``tile_geo`` last-event values (the JS view accumulates a
        render's pushes into its image cache).

        Geometry is absolute level px (``plan.read_origin`` + the tile
        crops) — identical for a tile across a render's pushes, so the
        view can reproject each tile under its own local transform.
        """
        geo: dict[str, list[int]] = {}
        urls: dict[str, str] = {}
        rx, ry = plan.read_origin
        for t in chunk.tiles:
            k = f"{t.key[0]}:{t.key[1]}:{t.key[2]}"
            x0, y0, x1, y1 = t.crop
            geo[k] = [t.key[0], rx + x0, ry + y0, x1 - x0, y1 - y0]
            urls[k] = jpeg_data_url(chunk_tiles[t.key], self._jpeg_quality)
        self.tiles = urls
        self.tile_geo = geo

    def render(self) -> ReadPlan:
        """Synchronous render of the current viewport (headless/programmatic)."""
        plan = self._render_once()
        assert plan is not None
        return plan

    # --------------------------------------------------------------- public API
    def set_zoom(self, zoom: float, cx: float | None = None, cy: float | None = None) -> Viewport:
        """Set zoom (clamped), optionally moving the view center."""
        z = min(max(float(zoom), self._min_zoom), self._max_zoom)
        vp = self._vp_current.with_zoom(z)
        if cx is not None and cy is not None:
            vp = vp.with_center(cx, cy)
        self._set_viewport_sync(vp)
        return vp

    def center_on(self, cx: float, cy: float) -> Viewport:
        """Move the view center to slide (level-0) coordinates."""
        vp = self._vp_current.with_center(float(cx), float(cy))
        self._set_viewport_sync(vp)
        return vp

    def viewport_bbox(self) -> tuple[float, float, float, float]:
        """Current view in level-0 coordinates, clamped to the slide: (x0, y0, x1, y1)."""
        x0, y0, x1, y1 = self._vp_current.l0_bbox
        w, h = self._meta.dimensions  # type: ignore[union-attr]
        return (max(0.0, x0), max(0.0, y0), min(float(w), x1), min(float(h), y1))

    def read_crop(self, bbox: tuple[float, float, float, float], level: int = 0):
        """Read a level-0-bbox region as a PIL image (level 0 by default)."""
        x0, y0, x1, y1 = (int(v) for v in bbox)
        w, h = self._meta.dimensions  # type: ignore[union-attr]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"empty crop bbox: {bbox}")
        assert self.backend is not None
        return self.backend.read_region((x0, y0), level, (x1 - x0, y1 - y0))

    # ------------------------------------------------- read-only annotations
    def set_annotations(self, source: str | Path | dict) -> dict:
        """Import a GeoJSON annotation document (replaces the current set).

        ``source`` is a GeoJSON file path or an already-parsed document
        (dict); its coordinates are level-0 slide px.

        Returns the canonical annotation document (a GeoJSON
        ``FeatureCollection`` in level-0 px of ``{id, geometry,
        properties}`` features), also stored on the synced ``annotations``
        trait; the JS view renders it read-only.
        """
        if isinstance(source, (str, Path)):
            doc: dict = json.loads(Path(source).read_text())
        else:
            doc = source
        parsed = parse_annotations(doc)
        self.annotations = parsed
        return self.annotations

    def clear_annotations(self) -> None:
        """Remove all annotations (the JS overlay clears)."""
        self.annotations = _EMPTY_ANNOTATION_DOC

    # ------------------------------------------------------- overlay (heatmap)
    def set_overlay(
        self,
        source: str | Path | Image.Image,
        alpha: float | None = None,
        transparent: tuple[int, int, int] | None = (0, 0, 0),
    ) -> None:
        """Show a full-slide overlay image over the tiles (e.g. a model
        heatmap PNG rendered at the slide's ``get_thumbnail`` scale).

        ``source`` is a PNG file path or a PIL image. The image is
        stretched over the whole slide, so its aspect ratio must match
        the slide's (a ``get_thumbnail`` output matches up to rounding);
        its alpha channel (or a grayscale/RGB image's content) composites
        with ``overlay_alpha`` opacity — use a heatmap with an alpha
        channel (e.g. matplotlib's default colormap) for transparent
        background. ``alpha`` (0–1) sets the opacity; ``None`` leaves the
        current value. ``transparent``: pixels exactly equal to this RGB
        triple (default: pure black) become fully transparent; pass
        ``None`` to keep the image's own alpha channel untouched.
        Replaces any previous overlay.
        """
        tkey = _validate_transparent_key(transparent)
        img = _load_overlay_image(source)
        _apply_transparency(img, tkey)
        w, h = self._meta.dimensions  # type: ignore[union-attr]
        _check_overlay_aspect(img, w, h)
        self.overlay_img = png_data_url(img)
        if alpha is not None:
            self.overlay_alpha = alpha
        self.status = f"overlay: {img.width}×{img.height} png"

    def clear_overlay(self) -> None:
        """Remove the overlay image (the view draws only tiles again)."""
        self.overlay_img = ""

    # ------------------------------------------------- annotation editing
    def delete_annotation(self, feature_id: str) -> None:
        """Delete the annotation with the given id (the JS view's ``del``
        action, programmatically). Unknown ids are ignored (the status
        reports it)."""
        self.annotation_edit = {"op": "delete", "id": feature_id}

    def set_annotation_label(self, feature_id: str, label: str | None) -> None:
        """Set the label of the annotation with the given id. ``None``,
        ``""`` and whitespace-only clear it (no label)."""
        self.annotation_edit = {
            "op": "set_label", "id": feature_id, "label": label,
        }

    def set_annotation_color(
        self, feature_id: str, color: str | None = None, fill: str | None = None
    ) -> None:
        """Set the stroke color and/or fill of the annotation with the given
        id (CSS color strings; ``None`` = the default: black stroke,
        transparent fill)."""
        self.annotation_edit = {
            "op": "set_color", "id": feature_id, "color": color, "fill": fill,
        }

    def set_annotation_vertex(
        self, feature_id: str, index: int, x: float, y: float
    ) -> None:
        """Move one position of the annotation with the given id
        (docs/annotations.md) — the same edit a vertex drag in the view
        issues: the
        position at flat ``index`` in the feature's canonical position
        order (a Point's single position; a MultiPoint's / LineString's
        positions; a Polygon's rings in order — outer first, then holes,
        positions within a ring; a MultiPolygon's islands in order, rings
        within an island, positions within a ring) goes to level-0 px
        ``(x, y)``. An id that does not address a feature of the current
        set is ignored (the status reports it); an out-of-range ``index``
        or a move that would degenerate the geometry is refused (the set
        is unchanged)."""
        self.annotation_edit = {
            "op": "set_vertex", "id": feature_id,
            "index": int(index), "x": float(x), "y": float(y),
        }

    def add_annotation_vertex(
        self, feature_id: str, index: int, x: float, y: float
    ) -> None:
        """Add one position to the annotation with the given id
        (docs/annotations.md) — the same edit a click on the selected
        feature's edge in the view issues: a position of level-0 px
        ``(x, y)`` is inserted at flat ``index`` in the feature's
        canonical segment order (a LineString's ``n`` positions give
        ``n - 1`` segments; a Polygon ring of ``n`` positions gives ``n``
        segments — the last being the closing edge back to the ring's
        first position; a Polygon's rings in order, a MultiPolygon's
        islands in order, rings within an island). An id that does not
        address a feature of the current set is ignored (the status
        reports it); an out-of-range ``index``, a position on top of an
        existing vertex, or an insertion that would degenerate the
        geometry is refused (the set is unchanged)."""
        self.annotation_edit = {
            "op": "add_vertex", "id": feature_id,
            "index": int(index), "x": float(x), "y": float(y),
        }

    # --------------------------------------- drawn polygons (JS -> Py)
    # (the edit-command observer is registered with this one above;
    # both apply to `annotations` and push the updated set)
    def _on_last_polygon_change(self, change: dict) -> None:
        """A drawn polygon ring arrived from the view (docs/DESIGN.md §6.4).

        Normalize with the shared ring helper (the same code path imported
        GeoJSON rings use), then either append a polygon feature to
        ``annotations`` — fresh non-colliding id, empty properties — or
        discard: warn + status, no state change. Either way the
        ``last_polygon`` trait itself is untouched: it is a last-event
        slot, not a collection, and assigning the *updated* document
        syncs the result back down.
        """
        ring = change["new"]
        if ring is None:
            return
        norm = normalize_ring(ring)
        if norm is None:
            warnings.warn(
                "islide: discarding drawn polygon "
                "(needs >= 3 non-collinear finite points)",
                UserWarning,
            )
            self.status = "Discarded: polygon needs ≥ 3 non-collinear points"
            return
        fid = self._next_annotation_id()
        doc = self.annotations
        self.annotations = {
            "type": "FeatureCollection",
            "features": [
                *doc["features"],
                {
                    "type": "Feature",
                    "id": fid,
                    "geometry": {"type": "Polygon", "coordinates": [norm]},
                    "properties": {},
                },
            ],
        }
        self.status = f"Added polygon #{fid}"

    def _next_annotation_id(self) -> str:
        """A fresh ``aN`` id that collides with no current feature id."""
        used = {f.get("id") for f in self.annotations.get("features", [])}
        n = 0
        while f"a{n}" in used:
            n += 1
        return f"a{n}"

    # ----------------------------------- annotation editing (JS -> Py)
    def _on_annotation_edit_change(self, change: dict) -> None:
        """A view edit command arrived (docs/DESIGN.md §6.5): apply it to
        ``annotations`` (pure ``apply_edit``) and push the updated set.

        A malformed command (warned) or an id that no longer addresses a
        feature (a Python-side ``set_annotations()`` replace can race a JS
        click) leaves the document untouched; the status reports the
        ignored edit either way. The ``annotation_edit`` trait itself is
        the last-event slot and is not cleared: it carries the last
        issued command (a re-attached view applies it to its own state;
        the observer does not re-apply it).
        """
        cmd = change["new"]
        if cmd is None:
            return
        try:
            doc = apply_edit(self.annotations, cmd)
        except ValueError as e:
            # A malformed wire payload, or a vertex move / insert Python
            # refused (bad index, non-finite position, a coincident
            # vertex, or the edit would degenerate the geometry) — warn,
            # keep state, and say so.
            warnings.warn(f"islide: ignoring annotation edit: {e}", UserWarning)
            self.status = f"edit ignored: {e}"
            return
        if doc is None:
            self.status = "edit ignored: unknown annotation id"
            return
        self.annotations = doc
        fid = cmd["id"]
        if cmd["op"] == "delete":
            self.status = f"deleted #{fid}"
        else:
            what = {
                "set_label": "label",
                "set_color": "color",
                "set_vertex": "vertex",
                "add_vertex": "vertex",
            }[cmd["op"]]
            self.status = f"edited #{fid} ({what})"

    # ------------------------------------------------------------ resync
    def _on_resync_change(self, change: dict) -> None:
        """A JS view attached (or re-attached): re-render the current
        viewport so it receives the full set — its image-cache seed from
        the synced state is only the last chunk of the last render.

        The counter always changes, so this fires even when the view's
        fit-echo matches the current ``viewport`` value (a value-equal set
        fires no viewport observer)."""
        self._schedule_render()
