# jupyter-islide — Design Document

An [ipywidgets](https://ipywidgets.readthedocs.io/)-based widget for interactively
exploring pathology whole-slide images (WSIs) in Jupyter, backed by
[OpenSlide](https://openslide.org/) via
[openslide-python](https://github.com/openslide/openslide-python).

---

## 1. Problem

Whole-slide images are enormous (typically 100k × 100k px, several GB) and are
customarily viewed in dedicated desktop apps (OpenSlide viewer, Aperio, etc.).
Researchers working in Jupyter have no convenient way to:

- open a WSI next to their data,
- zoom into a region of interest (ROI) and inspect it interactively,
- navigate programmatically (e.g. "jump to the bounding box computed by my
  segmentation"),
- correlate image regions with annotations.

Existing Python options are either static (dump a full image or a pre-cut crop)
or heavyweight (standalone viewers, remote servers like `openslide_server`,
DeepZoom tileservers).

**Goal:** a `SlideViewer` widget: double-click a file, pan/zoom it in the
notebook, drive it from Python.

## 2. Goals / Non-goals

### Goals (v1)

- Open any slide format supported by OpenSlide (Aperio SVS, MIRAX, Hamamatsu,
  Leica, Ventana, generic tiled TIFF, …).
- Smooth interactive pan/zoom with the mouse (scroll = zoom at cursor, drag =
  pan), plus a minimap for coarse navigation.
- Programmatic control: `center_on(x, y, mpp=…)`, viewport queries.
- Annotation overlay: import existing shapes from GeoJSON (M2) plus
  hand-drawn polygons (M3).
- Stay in the kernel: no external server, no tile pyramid to pre-generate.

### Non-goals (v1)

- Editing/deleting individual annotations in the UI (M3 can *create*
  polygons by drawing; there is no per-shape move/edit/delete).
- Remote slides (HTTP/`openslide_server`, S3). *The backend interface is
  designed to allow it later; v1 is local files only.*
- JupyterLite (OpenSlide is a C library; no WASM build to maintain).
- Multi-slide side-by-side comparison (the API allows it; UI doesn't).
- Serving slides to other people (this is a personal/research viewer, not a
  web service).

## 3. Architecture Overview

```
┌────────────────────────────────────────────────────────────┐
│  Jupyter frontend (classic notebook or JupyterLab)         │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  jupyter-islide JS view (small @jupyter-widgets view)│  │
│  │                                                      │  │
│  │  canvas#image      canvas#annotations                │  │
│  │  (tile composit)   (shapes in slide coords)          │  │
│  │  canvas#minimap    toolbar (buttons, mpp readout)    │  │
│  │                                                      │  │
│  │  mouse: wheel/drag/dblclick  ──► viewport trait      │  │
│  └───────────────────────▲──────────────────────────────┘  │
│                          │ comm (ipywidgets traits)
└──────────────────────────┼──────────────────────────────────┘
                           │  viewport: {cx, cy, zoom}
                           │  tiles:    {key: b64-jpeg}   (Py ─► JS only)
                           │  annotations: [...]           (Py ⇄ JS)
┌──────────────────────────┴──────────────────────────────────┐
│  Kernel (Python)                                            │
│                                                             │
│  SlideViewer (widgets.DOMWidget)                            │
│    ├─ SlideBackend ── openslide.OpenSlide                  │
│    │    ├─ metadata (levels, ds factors, mpp, thumbnail)   │
│    │    └─ read_region() ─► PIL.Image (RGBA)               │
│    ├─ TilePlanner  (viewport ─► (level, rect) reads)        │
│    ├─ TileCache    (LRU, byte-budgeted, holds PIL images)   │
│    └─ state: SlideMeta, Viewport, callbacks                 │
└─────────────────────────────────────────────────────────────┘
```

Design rule: **the Python side owns all state and all decoding.** The JS view
is a dumb compositor: it renders whatever tiles it's given, at the positions
the Python side computes, and reports pointer input. This keeps the notebook
kernel the single source of truth, makes programmatic control trivial, and
keeps the JS tiny.

## 4. Coordinates

- **Slide coordinates** are canonical: integer pixels at level 0 (full
  resolution), origin top-left, y down. All public API, callbacks, and
  annotations use these.
- **Viewport** = `(cx, cy, zoom)` where `(cx, cy)` is the slide-coordinate
  center of the canvas and `zoom` = screen pixels per level-0 pixel.
  - `zoom = 1` ⇒ 1:1 with full resolution.
  - Microscope magnification and µm/px are derived:
    `mpp_screen = mpp_level0 / zoom` (mpp from `openslide.mpp-x` property).
- **Level selection:** for the current zoom pick the *finest* pyramid level
  L with `downsample[L] >= 1/zoom` (smallest such L; coarsest level as a
  fallback below the pyramid's range). At that level the slide is at least
  as detailed as the screen, so rendering only ever down-scales (crisp,
  cheap). We do **not** use `slide.get_best_level_for_downsample(1/zoom)`:
  it picks the *coarsest* level with `ds <= requested`, which can up-scale
  (see §8).

## 5. Rendering Pipeline

Per viewport change (coalesced to at most one in-flight pass):

1. **Plan.** Viewport in slide coords → screen rect → the covering grid cells
   at level L (global 256-px grid; cell `(tx, ty)` covers level px
   `[tx·T, (tx+1)·T) × [ty·T, (ty+1)·T)`). The **read rectangle is the union
   of those cells, clamped to the level bounds** — anchored to the tile
   grid, *not* to the viewport edges. This invariant is what makes the
   `(level, tx, ty)` cache key sound: a tile's crop then depends only on the
   cell and the slide boundary, never on the viewport (a viewport-anchored
   rect cuts cells at its edges; the cached partial crop is later served
   stretched as a full cell — the bug this avoids, regression-tested in
   `tests/test_integration.py::test_tile_cache_is_viewport_invariant`).
2. **Fetch.** One `read_region(location, level=L, size=rect_L.size)` call per
   level (see §5.1), then crop the result into display tiles in Python with
   PIL. Check the tile cache first; only fetch missing tiles' rectangles.
3. **Encode.** Each tile → JPEG (quality ≈ 85) → base64 in the `tiles` trait:
   `{ "<L>:<tx>:<ty>": "data:image/jpeg;base64,…" }`.
4. **Draw.** The JS view composites tiles into `canvas#image` at
   `screen_pos = (tile_pos_in_slide / zoom)` with `imageSmoothing` on for
   down-scale levels. Areas with no tile (outside the slide bounds) show a
   plain white CSS background — out-of-bounds reads are clamped, never
   requested.

### 5.1 Fetch strategy: "one big read, crop in Python"

`read_region` is an OpenSlide decode call; calling it per 256 px tile
(≈ 50+ calls per screen at moderate zoom) is dominated by decode setup, not
pixels. Instead, per level we read the *whole covering rectangle* in a single
call (at the selected level this is at most on the order of the canvas size,
e.g. 1280 × 720 ≈ 3.7 MB RGBA) and slice it into tiles. Consequences:

- ~1–2 decode calls per viewport instead of ~50;
- the tile cache still makes re-visits of panned-to regions free;
- no per-tile thread pool needed in v1 (a single in-flight fetch pass is
  enough; see §7 for what we don't do yet).

### 5.2 Caching

- **TileCache**: LRU keyed by `(level, tx, ty)`, valued by PIL image (decoded,
  kept in `RGBA`). Budgeted by decoded-size estimate `w·h·4` (default 256 MB,
  configurable). Eviction is by bytes, not count. Key validity rests on the
  grid-anchored read rect (§5, step 1): the cached image for a cell is always
  exactly `cell ∩ slide bounds`, at every zoom and viewport.
- **Slide handle**: `OpenSlide` objects are opened lazily on a background
  thread (opening large SVS files can take seconds) and kept open while the
  viewer is live; closed on widget disposal (`on_widget_disposed` / close in
  cell teardown).
- **Minimap image** and slide **metadata** computed once at open.

### 5.3 Encoding choice

- JPEG q≈85 for tiles: 10–40 KB/tile over the comm; visually lossless for
  tissue at display size. PNG fallback trait option for users who need exact
  pixels (e.g. color quantization debugging).
- Tiles are always sent from Python only. The JS side never echoes tile data
  back (trait is treated as write-once from Python per update; JS just
  replaces its tile map on each push).

## 6. Frontend (JS view)

A small custom widget view (`frontend/`, npm name `jupyter-islide`, one
view, no build deps beyond `@jupyter-widgets/base`). This is the standard
ipywidgets route and works identically in classic notebooks and JupyterLab
(the M0 HTML viewer `HtmlSlideViewer` is kept as the no-extension fallback —
see §10).

### 6.1 DOM layout (implemented in `view.js`)

```
<div.islide-view>
  <div.islide-canvas-wrap>
    <canvas.islide-canvas>           ← tiles (device-pixel-ratio aware)
    <div.islide-minimap>
       <img.islide-minimap-img>      ← slide overview (top-level JPEG)
       <canvas.islide-minimap-rect>  ← current-viewport rectangle
    </div>
  </div>
  <div.islide-toolbar>
    [−] [+] [fit] [1:1]  zoom readout "0.25× · 0.5 µm/px"  cursor "18691, 36611"  status
  </div>
</div>
```

The second `#annotations` canvas is implemented (M2; §6.3).

### 6.1.1 Package layout & registration (implemented)

```
frontend/
  tilemath.js    pure math: fitZoom, zoomAtCursor, panTransform,
                 tileScreenRect, visibleTiles, viewportL0Bbox, …
  compositor.js  drawScene(ctx, {transform, meta, tileGeo, images}) —
                 the only place tile pixels are drawn; pure over a ctx
  annotations.js drawAnnotations(ctx, {transform, shapes}) — M2 overlay
  model.js       SlideModel extends DOMWidgetModel (defaults only)
  view.js        SlideView extends DOMWidgetView (canvas, mouse, minimap)
  defaults.js    SLIDE_MODEL_DEFAULTS — the trait names shared with Python
  labextension.js  registers the module with IJupyterWidgetRegistry
  index.js     re-exports (npm "main")
  style/index.css
```

Registration (base-6 pattern, verified against `@jupyter-widgets/base`
6.0.12 and `jupyter-widgets-jupyterlab-manager` 5.0.16):

```js
registry.registerWidget({
  name: 'jupyter-islide',    // must equal _model_module
  version: '1.0.0',          // must satisfy _model_module_version (semver)
  exports: { SlideModel, SlideView },  // keys = _model_name/_view_name
});
```

The manager resolves the Python widget's `_model_module`/
`_model_module_version` through this registry
(`semver.maxSatisfying` over registered versions) and instantiates the
`_model_name` class, then creates a view per the `_view_name` export.
Views use the base-6 lifecycle: subclass `DOMWidgetView`, override
`render()` (`this.el` already exists), bind with
`this.listenTo(this.model, 'change:<attr>', …)`, and send state with
`this.model.set(attr, value); this.model.save();`.

### 6.2 Interactions → state

| Input | Effect | Status |
|---|---|---|
| wheel / trackpad pinch | zoom about the cursor; `zoom` continuous, clamped to `[fit_zoom/4, 16]` (the fit floor and the 16× ceiling match the Python-side clamp in `_set_viewport_sync`) | M1 ✅ |
| left drag (pointer events) | pan (M3: in drawing mode a ≥ 4 px left drag is also a pan — a still click adds a vertex) | M1 ✅ / M3 |
| double-click | zoom in ×2 at cursor (disabled in M3 drawing mode) | M1 ✅ / M3 |
| minimap click/drag | center viewport on that point | M1 ✅ |
| toolbar | −/+, fit slide, 1:1 | M1 ✅ |
| right-drag | pan (both modes) | M3 |
| left click (M3 drawing mode) | append a polygon vertex | M3 |
| key **A** | toggle M3 drawing mode (enter / save-and-exit) | M3 |
| key **Esc** (M3 drawing mode) | cancel: discard the draft, exit | M3 |

All of these mutate the **viewport trait** — the *only* JS→Python channel
for navigation. The wire form is
`{cx, cy, zoom, canvas_w, canvas_h}`: slide-center + zoom plus the canvas
size (so Python can plan exactly what the view sees, and so programmatic
`set_zoom`/`center_on` round-trips through the same shape). The view is the
**sole writer** of `viewport` from JS: it never echoes Python's viewport
back, it only sends interaction-driven updates (debounced 120 ms, coalesced
by Python onto one background render thread).

Between round-trips the view keeps a **local transform**
(`{cx, cy, zoom, canvasW, canvasH}`) and reprojects the *same* tile geometry
(`tile_geo`, absolute level-pixel crops) under it — pan/zoom is instant,
then the debounced sync triggers the next Python tile pass. The view caches
decoded tile images (insertion-order LRU, 400 entries) so back-pans are
canvas-only.

M3's polygon save is the same shape of update (`last_polygon`, §6.4), so
the same round-trip covers everything and Python can replay state after a
kernel restart of the widget.

Canvas is DPR-aware: backing store is `clientSize × devicePixelRatio`,
drawing is in CSS pixels (`setTransform(dpr, 0, 0, dpr, 0, 0)`). Resizes go
through a `ResizeObserver`, which updates the transform's canvas size,
**redraws synchronously** (the RO callback runs after layout but before
paint, so the frame that shows the new box is already drawn at the new size
— deferring the draw one frame to `requestAnimationFrame` CSS-stretches the
old backing store, which reads as squished tiles while JupyterLab animates
the sidebar) and re-syncs. `_drawNow` additionally re-syncs the transform to
the live canvas size if the RO has not caught up, so scene layout and
backing store always share a size.

### 6.3 Annotations layer (M2: read-only import + overlay)

M2 is **read-only**: annotations enter as a GeoJSON document from Python
and are rendered on a canvas overlay. No creating, editing, or deleting
annotations in the UI (the v1 non-goal stands); polygon *drawing* lands in
M3 (§6.4).

**Import: GeoJSON, in level-0 `px` (default) or `um`**
(`islide/annotations.py`, pure over a parsed document + mpp — same
style as `plan.py`):

- Accepted documents: a `FeatureCollection`, a single `Feature`, or a
  bare geometry object (no properties).
- Geometries: `Point` / `MultiPoint` → `point`, `LineString` → `line`,
  `Polygon` / `MultiPolygon` → `polygon`, `GeometryCollection` (recursed).
- Feature `properties` (all optional, else defaults): `color` (CSS
  stroke/outline color, default **black**), `fill` (CSS interior fill
  color, default **transparent** — polygons are outline-only unless a
  fill is given; an `rgba()` string gives a translucent fill), and
  `label` (string; drawn next to points).
- Coordinates: GeoJSON numbers are local coordinates from the slide
  origin, **y down** (slide orientation; no CRS handling). Two unit
  conventions, chosen at import:
  - `units="px"` (default): coordinates are already level-0 px.
  - `units="um"`: microns from the origin; converted to level-0 px via
    the slide's mpp (`px = um / mpp`, one mpp for both axes — WSIs are
    isotropic to within rounding). Physical, vendor-agnostic
    convention: a re-scan of the same tissue keeps its annotations.
    Requires mpp (`ValueError` otherwise).
- Validation: strict at the document level (bad structure → `ValueError`);
  per feature, non-finite coordinates → `ValueError`, degenerate geometry
  (empty ring, zero area) → skipped with a warning. Coordinates outside
  the slide are legal; drawing clips them.

**Shape model** (Python-owned, Py→JS sync — M2's only direction):
`{id, kind, points, label, color, fill}` in level-0 px, where `points` is:
- `point`: `[[x, y]]` — a single position;
- `line`: `[[x, y], [x, y], …]` — an open position list;
- `polygon`: a *list of rings* (outer ring first, holes after), each ring
  `[[x, y], …]`, stored **open** — a redundant closing position is stripped
  at import and the renderer closes each ring when tracing, so the outline
  covers the last→first segment. Holes are punched with an evenodd fill.

`label`, `color`, `fill` are `null` when the feature gave none (JSON `null`
over the wire). `null` means *"use the default"*, not *"none"*: `color`
defaults to black, `fill` to transparent, `label` to no label — so the
renderer treats `null` and absence identically. (`kind: "rect"` was once
deferred to M3 with the region select that produced it; the region select
was dropped when M3 became polygon drawing — `rect` is no longer planned,
§6.4.)

**Wire:** new trait `annotations` (Py→JS, list). The *entire* normalized
set is sent once at import — deliberately **not** filtered by viewport in
Python: the JS local transform is the authoritative viewport at frame time
and Python's copy is ≥120 ms stale, so viewport-filtered pushes would make
shapes pop in on every pan (the lag the tile re-projection design avoids,
here for data that costs nothing to send). Culling is per-frame in JS
(bbox ∩ current transform). The only pre-send filters are the
viewport-independent import-time ones: validation errors, degenerate-shape
skip, unit conversion. Because it is a synced trait, the overlay rebuilds
after a kernel restart just like the tiles.

**Rendering** (second `#annotations` canvas, §6.1; same size/DPR as the
image canvas, drawn after the tiles):

- **Culling:** shapes whose bbox does not intersect the current viewport
  are not rendered (checked per frame against the local transform).
- Screen-constant styling (does **not** scale with zoom): polygons with
  a 1.5 px stroke (`color`, default black) and interior `fill` (default
  transparent); lines at 1.5 px in `color`; points as filled circles of
  ~4 px radius (body: `fill` if given, else `color`) with a white halo
  for contrast on tissue.
- **Alpha:** a toolbar slider (0–1, default 1) sets the overlay's
  `globalAlpha`, fading the whole annotation layer. View-local display
  state, not a trait (resets on restart, like the local transform).
- Labels: 12 px screen-space text next to the point, only when `label`
  is set.
- Pure module `frontend/annotations.js`
  (`drawAnnotations(ctx, {transform, shapes, alpha})`), unit-tested
  against a mock ctx like the compositor.

**Python API:**

```python
v.set_annotations("roi.geojson")     # str / Path, or a parsed dict
v.set_annotations(doc, units="um")   # coordinates in microns (requires mpp)
v.clear_annotations()
v.annotations      # the normalized shape list (level-0 px)
```

`set_annotations` replaces the current set and waits for the slide if
needed (the `um` conversion requires mpp).

**Not in M2** (explicit): any UI editing (no add-on-click, drag, edit,
or delete — polygon drawing is M3, §6.4); annotation hit-testing, hover,
and tooltips; per-feature visibility; annotation export (the GeoJSON file
is the source of truth); `HtmlSlideViewer` annotations (canvas view only,
M0 fallback untouched).

### 6.4 Polygon drawing (M3)

M3 is **drawing**: the user hand-draws one polygon at a time, and it lands
in the same `annotations` set as an imported one. The earlier M3 plan
(`on_click`/`on_region` callbacks, click-to-add point, rubber-band region
select) is dropped — no Python-side event hooks exist anymore, the
reserved `last_click`/`last_region` traits are removed from the contract,
and `kind: "rect"` is no longer planned.

**Mode.** One extra mode on top of the M1 interactions, toggled by the
**A** key. Mode and draft are **view-local** state (like the local
transform and the alpha slider — not traits, so Python never sees the
mode, only the result):

```
idle    --A-->                            drawing(draft = [])
drawing --left click (Δ < ~4 CSS px)-->   drawing(draft += vertex)
drawing --left drag (Δ ≥ 4 px) / right drag--> pan (draft untouched)
drawing --A--> valid ring?  save + exit : discard + exit
drawing --Esc-->                         discard + exit
```

- **Enter (A, from idle):** crosshair cursor; status
  `Polygon: click to add points — A saves, Esc cancels`. Wheel zoom stays
  live (vertices are slide coords, so the draft tracks the view like the
  tiles); the minimap stays live; **double-click zoom is disabled** in the
  mode (a dblclick would inject two coincident vertices).
- **Vertex (left click):** a pointerup within ~4 CSS px of the
  pointerdown appends the cursor's level-0 position to the draft. A left
  drag ≥ 4 px is a pan (M1's left-drag pan, now with the click threshold;
  right-drag pans in both modes) — the view can be repositioned mid-draft
  without leaving the mode.
- **Save (A, from drawing):** the draft is emitted to Python (below) and
  the mode exits. A ring is valid iff it has ≥ 3 finite points and
  nonzero area — the same degeneracy rules as the GeoJSON parser; an
  invalid draft (0–2 clicks, collinear) is discarded with status
  `Discarded: polygon needs ≥ 3 non-collinear points`. **A is a hard
  toggle** either way.
- **Cancel (Esc, from drawing):** discard the draft, exit, status
  `Polygon cancelled`.
- Keyboard: `keydown` on the view root (`tabindex=0`, so the canvas area
  can hold focus), ignored while the alpha `<input>` has focus, and **A is
  a no-op until `slide_open`** (status `Slide not open`).

**Draft preview** (annotations overlay canvas, drawn after the shapes,
under the alpha slider; cleared on exit): vertex dots (filled circles
with a white halo, like point markers), solid 1.5 px screen-constant
segments between consecutive vertices, and a dashed segment from the last
vertex to the **cursor** (plus one to the first vertex once ≥ 3 points) so
the closure about to happen is visible. Pure module `frontend/polydraw.js`
— the mode state machine (pointer/keyboard events → `{mode, draft}`,
node-testable like `tilemath.js`) and
`drawDraftPolygon(ctx, {transform, draft, cursor})` (mock-ctx tested).

**Wire: `last_polygon` (JS→Py, default `null`).** On save the view sets
`last_polygon = [[x, y], …]` — the clicked ring, **open** (no redundant
closing position), unclamped level-0 px — and does nothing else.
"Last-event state" semantics, exactly like `viewport` is JS→Py today: the
trait holds the *last saved* ring, Python's observer fires once per save,
and re-attach replay is a no-op (the value is already set; nothing
re-fires in-process). The Python `observe()` handler then:

1. normalizes the ring with the **shared** validation/normalization from
   `annotations.py` (finite coords, ≥ 3 pts, nonzero area — a
   `normalize_ring`-style helper, so drawn and imported rings can't drift
   apart);
2. invalid → `warnings.warn` + status, no state change;
3. valid → append `{id, kind: "polygon", points: [ring], label: null,
   color: null, fill: null}` to `self.annotations` (a fresh id that does
   not collide with the current set) and push the `annotations` trait.
   From then on the shape is indistinguishable from an imported one: same
   defaults (black stroke, transparent fill), same renderer, same
   `clear_annotations()`.

"Closing the first and last point" is the M2 renderer's existing behavior
(rings are stored open, closed when traced), so the saved shape needs no
new rendering code.

**Not in M3** (explicit): Python-side event hooks (gone with
`last_click`/`last_region`); point/line/rect creation; editing or
deleting individual shapes (`clear_annotations()` only); hit-testing,
hover, tooltips; annotation export; keyboard beyond A/Esc; touch;
`HtmlSlideViewer` (M0 fallback untouched, as in M2).

## 7. Python API

```python
from islide import SlideViewer

v = SlideViewer("sample.svs")   # opens in background; safe to display now
display(v)                      # canvas renders once the slide is open
v.wait()                       # block until open (raises on open failure)

# programmatic navigation (slide coords = level-0 px)
v.center_on(120_000, 80_000)
v.set_zoom(2.0)                # clamped to [fit/4, 16]
v.set_zoom(1.0, cx=50_000, cy=40_000)   # zoom + center in one call
bbox = v.viewport_bbox()       # (x0, y0, x1, y1) in slide coords, clamped

# data out
img = v.read_crop(bbox)        # PIL Image at level 0 (RGBA)

# annotations (M2)
v.set_annotations("roi.geojson")   # level-0 px (default); units="um" for microns (needs mpp)
v.clear_annotations()

# M3: draw a polygon in the view — key A, left-click the vertices,
# key A again saves it into v.annotations (no programmatic API)

v.close()                      # joins open thread, closes the slide handle
```

Widget identity (must match the JS module, §6.1.1):
`_model_name="SlideModel"`, `_view_name="SlideView"`,
`_model_module="jupyter-islide"`, `_model_module_version="1.0.0"`.

Traits (the comm contract; synced names are guarded by a cross-language
test — Python trait set == `frontend/defaults.js` keys):

| Trait | Dir | Type | Notes |
|---|---|---|---|
| `slide_open` | Py→JS | bool | false until open (or on error) |
| `meta` | Py→JS | dict | `{dimensions, level_count, level_downsamples, level_dimensions, mpp, vendor}` |
| `viewport` | JS⇄Py | dict | `{cx, cy, zoom, canvas_w, canvas_h}` (level-0 center + zoom + canvas size) |
| `canvas_h` | Py→JS | int | on-screen viewport height (CSS px), user-settable at construction or runtime; the JS view applies it to the canvas and its ResizeObserver syncs the resized viewport back |
| `tiles` | Py→JS | dict | `{"level:tx:ty": dataURL}` — replaced wholesale per push |
| `tile_geo` | Py→JS | dict | `{"level:tx:ty": [level, ox, oy, cw, ch]}` — absolute level-pixel crop origin + size; the view reprojects this under its local transform |
| `minimap_img` | Py→JS | dataURL | whole-slide overview (top-level JPEG), set once |
| `annotations` | Py→JS | list | normalized shapes in level-0 px (`{id, kind, points, label, color, fill}`); M2: imported set, M3: drawn polygons appended (§6.4) |
| `last_polygon` | JS→Py | list | M3: the just-saved drawn polygon — open ring `[[x, y], …]`, level-0 px (`null` = none yet); Python validates and appends the shape to `annotations` |
| `status` | Py→JS | str | status line (open progress / error / last render's level·tile info; the live zoom·µm/px is the JS readout's) |

### 7.1 `SlideBackend` (the seam for future remotes)

```python
class SlideBackend(Protocol):
    dimensions: tuple[int, int]
    level_count: int
    level_downsamples: tuple[float, ...]
    level_dimensions: tuple[tuple[int, int], ...]
    mpp: float | None
    def read_region(self, location, level, size) -> Image.Image: ...
    def thumbnail(self, size) -> Image.Image: ...
    def close(self) -> None: ...
```

`OpenSlideBackend` implements this over `openslide.open_slide(path)`. A future
`HTTPBackend` (openslide-server) or `S3Backend` slots in without touching the
widget.

`OpenSlideBackend.from_object(slide)` wraps an *already-opened* object that
duck-types the openslide OO API surface (`properties`, `dimensions`,
`level_count`, `level_downsamples`, `level_dimensions`, `read_region`,
`get_thumbnail`, `close`) — the seam for custom slide libraries that mirror
openslide's API over other slide types (no `openslide` import needed; the
backend closes the wrapped object). Both viewers expose it as a keyword-only
`slide=` constructor argument, mutually exclusive with the path.

## 8. Verified OpenSlide / openslide-python API notes

Environment checked during design (openslide-python 1.4.6, libopenslide 4.0.1):

- `openslide.open_slide(path) -> OpenSlide` (object-oriented API; the C-style
  `lowlevel` module also exists).
- **Properties, not callables** (easy to get wrong — older docs show the C
  call style):
  - `slide.dimensions -> (w, h)` (level 0)
  - `slide.level_count -> int`
  - `slide.level_downsamples -> (1.0, 2.0, 4.0, …)`
  - `slide.level_dimensions -> ((w0,h0), (w1,h1), …)`
- `slide.read_region(location=(x,y), level=L, size=(w,h)) -> PIL.Image`
  (mode `RGBA`). `location` is in level-0 coordinates; `size` is the region
  size (pixels at level L).
  - **Out-of-bounds** (verified, generic-tiff / libopenslide 4.0.1): the
    return is always exactly `size`, and out-of-bounds areas are filled with
    *transparent black* — compositing onto white before JPEG gives the
    expected white background at the slide edge. Fill color is
    vendor-dependent, but "always exactly `size`" holds.
  - **Location anchor** (verified, generic-tiff / 4.0.1): the returned
    image's top-left is level pixel `floor(location / ds)` per axis
    (unaligned level-0 locations are accepted). Therefore to anchor a read
    at exactly level pixel `p` use `location = ceil(p * ds)` (see
    `islide.plan.anchor_l0`; tested for the real downsample factors of the
    test slide, including the non-integer ones).
- `slide.get_best_level_for_downsample(ds) -> int` — picks the *coarsest*
  level with `ds_level <= requested` (verified: it can return a level that
  up-samples). islide uses its own never-up-scale selector instead:
  smallest `L` with `ds[L] >= 1/zoom` (see `islide.plan.select_level`).
- `slide.get_thumbnail(size=(w,h)) -> PIL.Image`
- `slide.properties` dict carries `openslide.mpp-x/y`, `openslide.objective-power`,
  `openslide.vendor`, per-level dims/ds, …
- `slide.color_profile` exists; v1 ignores color management and documents it.

## 9. Performance budget (targets)

| Metric | Target |
|---|---|
| Time to first pixel (local file, after kernel import) | < 2 s on a typical SVS (open on bg thread; minimap first) |
| Pan responsiveness (cached region) | ≤ 1 frame of perceived lag; tiles already on JS side |
| Pan into uncached region | one `read_region` pass; ≤ ~300 ms for a 1280×720 canvas at a mid level on a local NVMe |
| Tile transfer per full viewport | ≤ ~400 KB (≈ 20 tiles × 20 KB JPEG) |
| Steady-state memory (cache on) | 256 MB default budget + slide handle overhead |
| Idle CPU | 0 (no polling; everything is trait-driven) |

Explicit non-goals: no prefetching of off-screen tiles in v1 (the LRU cache
makes back-pans free, which covers 90 % of the benefit); no multi-threaded
fetch pass.

## 10. Milestones

| # | Milestone | Demo |
|---|---|---|
| M0 ✅ | **Spike: static pipeline** *(done)*. Viewport state in Python, toolbar/sliders (ipywidgets buttons + a zoom `FloatLogSlider`) drive rendering; tiles pushed to an `HTML` widget as stacked `<img>` tags. No mouse. Proves: openslide plumbing, level selection, one-read-per-viewport, JPEG over comm, cache. Implementation notes: the slide opens **synchronously** in `__init__` (local opens were measured at ~0 s; background open comes with the M1 JS view's loading state), and the HTML compositor lives in `widget.py` only — `plan`/`fetch`/`cache`/`encode` are final-shape. | `examples/m0_demo.ipynb` + zoom buttons |
| M1 ✅ | **Interactive JS view** *(done)*. Canvas compositor (`compositor.js` + pure `tilemath.js`), wheel/drag/dblclick pan-zoom at the cursor, minimap with viewport rect, −/+/fit/1:1 toolbar, DPR-aware canvas, `ResizeObserver` resize. Trait contract per §7; viewport sync is JS-written + debounced (120 ms) + coalesced onto a Python background render thread. M0's HTML viewer is kept as the standalone class `HtmlSlideViewer` (no-extension fallback / reference pipeline), and the Python-side `SlideViewer` gained the background open + `wait()` + programmatic viewport API the JS view drives. **Not yet covered:** in-browser verification (no browser in the dev sandbox — the view is verified by node unit tests of the pure math/compositor + headless widget tests; the extension build path is documented, see §11), and the M3 polygon-drawing interaction (§6.4). | `examples/m1_demo.ipynb` (canvas) + smooth pan/zoom |
| M2 ✅ | **Read-only annotations** *(done)*. GeoJSON import (FeatureCollection / point / line / polygon; level-0 `px` default, `um` option) → normalized shape list → `annotations` trait (Py→JS) → second canvas overlay: viewport culling, screen-constant styling (black stroke / transparent fill defaults, per-feature `color`/`fill`/`label`), point labels, alpha slider. No UI editing (M3 adds polygon *drawing*, §6.4). | `examples/m2_demo.ipynb`: `set_annotations` (inline GeoJSON, level-0 px + microns) + smooth pan/zoom over the overlay |
| M3 | **Polygon drawing.** Key **A** toggles a drawing mode (crosshair; live draft: vertex dots, segments, dashed closure to the cursor); left click appends a vertex (≥ 4 px left drag = pan, right-drag pans in both modes, double-click disabled, wheel/minimap live); the second **A** saves the ring — the view sets `last_polygon` (JS→Py) and Python normalizes it (≥ 3 pts, nonzero area, else discarded) and appends a `polygon` shape to `annotations`; **Esc** cancels. No callbacks; no point/line/rect features; the reserved `last_click`/`last_region` traits are removed from the contract. | `examples/m3_demo.ipynb`: hand-drawn polygon + `v.annotations` + `read_crop` of its bbox |
| M4 | **Polish & ship.** Docs (README + docsite), example slides in docs, perf pass (DPR-aware canvas, HiDPI crispness), PyPI release `jupyter-islide`, `pip install jupyter-islide[dev]`, CI. | published package |

## 11. Testing

- **Unit (no OpenSlide needed):** tile planning, level selection, viewport
  math, coordinate transforms, LRU cache, trait/callback logic — pure
  functions over a `FakeSlide` (metadata-only stub of `SlideBackend`).
- **Integration (needs libopenslide):** real open/read against a real slide,
  `data/testslide.tiff` (whole-slide tiled TIFF, 37 382 × 73 222 px, 8
  levels, 0.25 µm/px, opens via the generic-TIFF vendor — this resolved the
  design-phase question about synthetic pyramids; level-selection math is
  additionally unit-tested against a fixed `SlideMeta` fixture in
  `tests/test_plan.py`).
  - Historical note: a single-level tiled TIFF written by `tifffile`
    (`tile=(256, 256)`) opens fine, and a hand-written multi-page "pyramid"
    TIFF was detected as 1 level (the generic-TIFF multi-resolution rules
    are stricter than "smaller sub-IFDs").
- **M1 widget tests (headless, `tests/test_widget_m1.py`):** the M1
  `SlideViewer` is a plain Python object until displayed, so the whole
  Python-side state machine is tested without a browser: background open +
  `wait()`, `meta` shape, headless fit viewport default, `tiles`/`tile_geo`
  key + geometry contract (cross-checked against a fresh `plan_viewport`),
  cache invariance (pan away and back → byte-identical tile payloads),
  `set_zoom`/`center_on`/`viewport_bbox`/`read_crop`, zoom clamping,
  JS-originated viewport trait change → background render, open-failure
  error path, `close()` idempotence.
- **M2 annotation tests:** pure parser unit tests (`tests/test_annotations.py`,
  no OpenSlide): FeatureCollection/Feature/bare-geometry documents, Multi* +
  GeometryCollection, um→px conversion at a fixed mpp, properties (label/
  color/defaults), `units="px"`, error paths (um without mpp, malformed
  document), degenerate-feature skip with warning. Headless widget
  (`tests/test_widget_m2.py`): `set_annotations`/`clear_annotations` →
  `annotations` trait shape and level-0 values, replace semantics,
  wait-for-open. JS (`frontend/test/annotations.test.js`): `drawAnnotations`
  against a mock ctx — culling, per-kind draw calls, screen-constant widths,
  `color`/`fill` defaults (black/transparent), alpha (`globalAlpha`),
  label rendering. Trait contract: `annotations` added to `defaults.js`,
  covered by the existing cross-language name guard.
- **M3 polygon tests:** Python headless (`tests/test_widget_m3.py`):
  setting `last_polygon` → a `polygon` shape appended (kind, ring,
  `null` styling, fresh non-colliding id); degenerate drafts (2 pts,
  collinear 3 pts, non-finite) dropped with a warning and no state
  change; successive saves get unique ids; `null` default ignored;
  `clear_annotations()` after. JS (`frontend/test/polydraw.test.js`):
  the pure state machine (A/Esc enter/save/cancel, click-vs-drag
  threshold, vertex accumulation) and `drawDraftPolygon` against a mock
  ctx (vertex dots, segments, dashed closure to cursor / first vertex).
  Contract: `last_polygon` added to `defaults.js` and
  `last_click`/`last_region` removed from both sides — covered by the
  existing cross-language name guard.
- **Cross-language contract test:** the Python synced trait names are
  asserted to equal the keys in `frontend/defaults.js` (both directions of
  the same guard), so the comm contract can't drift.
- **JS tests (no browser, `frontend/test/`, `node --test`):** the pure
  math (`tilemath.js`) — round-trips, cursor-fixed zoom, pan, tile screen
  rects (checked against the M0 screen-box formula), visible-tile selection,
  zoom clamping — and the compositor (`compositor.js`) against a mock 2D
  context (white underlay, per-tile `drawImage` rects, skip-not-ready, and
  re-projection under a changed local transform). The DOM/event wiring in
  `view.js` is thin over these and is verified by the manual matrix when a
  browser is available (classic nb / lab / ×2 DPR).

## 12. Packaging & Dependencies

```
islide/
├── pyproject.toml            # hatchling + hatch-jupyter-builder hook;
│                             #   deps: openslide-python>=1.4,
│                             #   pillow>=9, ipywidgets>=8
├── islide/
│   ├── __init__.py           # SlideViewer, HtmlSlideViewer, Viewport, …
│   │                         # + _jupyter_labextension_paths()
│   ├── widget.py             # M1 DOMWidget + M0 HTML viewer + state machine
│   ├── backend.py            # SlideBackend protocol, OpenSlideBackend
│   ├── plan.py               # viewport -> read plan (pure)
│   ├── annotations.py        # GeoJSON -> normalized shapes (pure, M2)
│   ├── viewport.py           # SlideMeta + Viewport (pure)
│   ├── cache.py              # TileCache
│   ├── fetch.py              # plan -> tiles (cache + one read, cropped)
│   └── encode.py             # tile -> JPEG data URL
└── frontend/                 # JS canvas view (npm: jupyter-islide)
    ├── tilemath.js  compositor.js  annotations.js  model.js  view.js
    ├── defaults.js  labextension.js  index.js  style/index.css
    ├── labextension/  # build output (gitignored) — compiled labextension
    └── test/             # node --test (pure math + compositor)
```

- Python: `openslide-python>=1.4` (uses the OO API; §8 notes), `pillow`,
  `ipywidgets>=8`. System lib: `libopenslide` — conda-forge `openslide`
  package, or `libopenslide0` on Debian/Ubuntu.
- JS: npm package `jupyter-islide` (the wire module name registered with
  the widget registry is `jupyter-islide`), no runtime dependency beyond
  `@jupyter-widgets/base` (declared a shared/singleton package so it is never
  bundled into the extension bundle).

### 12.1 Release packaging: one `pip install`, Python + extension

The wheel is built by `hatchling` with the `hatch-jupyter-builder` hook
(`[tool.hatch.build.hooks.jupyter-builder]` in `pyproject.toml`). At wheel
build time the hook runs `npm install` + `npm run build` in `frontend/` — i.e.
`jupyter labextension build .` — producing `frontend/labextension/` (the
`outputDir` in `frontend/package.json`). hatchling's `shared-data` then places
that directory at `share/jupyter/labextensions/jupyter-islide/` inside the wheel, the
standard JupyterLab 4 labextensions discovery path. So:

- `pip install .` / `pip install jupyter-islide` installs **both** the Python package
  and the pre-built extension in one step (no end-user npm).
- The build env gets `jupyterlab==4.*` automatically (declared in
  `[build-system] requires`); Node/npm must be on the PATH.
- `islide/_jupyter_labextension_paths()` (matching the ipyleaflet pattern)
  points at `../frontend/labextension` → `dest: jupyter-islide`, which is what
  `jupyter-builder develop islide` uses for the editable live-reload workflow.
- Mirrors ipyleaflet's mechanism (hatch `jupyter-builder` hook +
  `shared-data`); we keep a single package (`jupyter-islide`) rather than splitting
  into a pure-Python package + a separate `jupyter-islide` extension package.

## 13. Open Questions

1. **Zoom units in the public API** — keep `zoom` internal; expose `mpp`
   (pathology-native) in all public methods and the UI. *Proposed, confirm.*
2. **HiDPI canvas** — draw at device pixel ratio, CSS-scale down. *Yes, M1.*
3. **`tiles` trait size on extreme zoom-out** — when the whole slide fits on
   screen at level N, we send the entire level as a few tiles (fine: bounded
   by canvas size). No special "overview mode" needed. *Confirm.*
4. **Kernel-restart behavior** — widgets rebuild from traits; tiles re-fetch
   on demand. Document, don't persist.
5. **Associated images** (e.g. Aperio whole-slide macro shots in
   `slide.associated_images`) — expose as a future "overview" source; not v1.
6. **Color profiles** — ignore (documented) or run through Pillow ICC? v1: ignore.
7. **License & example slides** — need 2–3 small example WSI that are OK to
   redistribute for docs/tests (candidates: generated synthetic pyramid once
   §11's recipe is pinned, plus one small real SVS under an open license).
8. **Milestone split** — M2 = read-only GeoJSON import (done); M3 was
   re-scoped from "click/region-select interaction + optional annotate
   mode" to **polygon drawing only** (§6.4): no callbacks, no
   point/line/region features, and the reserved `last_click`/`last_region`
   traits are removed from the contract; M4 = polish & ship (was M3).
   *M2 done; M3 re-scoped as proposed.*
