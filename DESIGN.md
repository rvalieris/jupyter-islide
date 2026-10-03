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
  hand-drawn polygons (M3), with select/delete/label/recolor of
  existing shapes in the view (M4).
- Stay in the kernel: no external server, no tile pyramid to pre-generate.

### Non-goals (v1)

- Geometry editing of annotations in the UI: M3 *creates* polygons by
  drawing and M4 can select, delete, label, and recolor existing shapes,
  but there is no per-shape move, resize, or vertex edit.
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
                           │  annotations: {FC}            (Py ⇄ JS)
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
  tissue at display size. Quality is a widget constructor argument
  (`jpeg_quality=85` default, M3); there is no PNG fallback — exact pixels
  are `read_crop()`'s job (it returns the raw PIL image, unencoded).
- Tiles are always sent from Python only. The JS side never echoes tile data
  back (trait is treated as write-once from Python per update; JS just
  replaces its tile map on each push).

## 6. Frontend (JS view)

A small custom widget view (`frontend/`, npm name `jupyter-islide`, one
view, no build deps beyond `@jupyter-widgets/base`). This is the standard
ipywidgets route and works identically in classic notebooks and JupyterLab.
M0's HTML tile-composite spike viewer was removed in M6 (polish) — the
canvas view is the only one.

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
    [fit] [1:1]  zoom readout "2× · 500 µm/px"  cursor "23000, 16457"  status
  </div>
</div>
```

The second `#annotations` canvas is implemented (M2; §6.3). M4 adds the
`annotate` toolbar toggle and the `del` / `label` / `color` context
actions (§6.5).

### 6.1.1 Package layout & registration (implemented)

```
frontend/
  tilemath.js    pure math: fitZoom, zoomAtCursor, panTransform,
                 tileScreenRect, visibleTiles, viewportL0Bbox, …
  compositor.js  drawScene(ctx, {transform, meta, tileGeo, images}) —
                 the only place tile pixels are drawn; pure over a ctx
  annotations.js drawAnnotations(ctx, {transform, annotations}) — M2 overlay
  polydraw.js    M3 drawing: mode state machine + drawDraftPolygon (§6.4)
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
| double-click | zoom in ×2 at cursor — **removed in M3** (unnecessary; wheel / toolbar / minimap cover zoom) | M1 ✅ / M3 |
| minimap click/drag | center viewport on that point | M1 ✅ |
| toolbar | fit slide, 1:1 (the −/+ zoom buttons were removed in the M4 polish pass — wheel zoom covers them) | M1 ✅ / M4 |
| right-drag | pan (both modes) | M3 |
| left click (M3 drawing mode) | append a polygon vertex | M3 |
| key **A** | toggle M3 drawing mode (enter / save-and-exit) | M3 |
| key **Esc** (M3 drawing mode) | cancel: discard the draft, exit | M3 |
| toolbar **annotate** | toggle M3 drawing mode (the visible entry point; **A** stays the alias; pressed while drawing) | M4 |
| left click (idle, no mode) | hit-test the annotations at the cursor: hit → select (thick accent highlight), miss → deselect; a ≥ 4 px drag is still a pan | M4 |
| toolbar **del** / **label** / **color** (a shape selected) | remove the selected shape / attach or edit its label / set its stroke color and fill (incl. clearing the fill) | M4 |

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

**The annotation document** (canonical form; M3.5 supersedes the M2
shape list — `islide/annotations.py`, pure over a parsed document + mpp,
same style as `plan.py`): annotations are **one** normalized, restricted
GeoJSON `FeatureCollection` — the `annotations` trait value, the wire
format, and the export. There is no second representation and no inverse
conversion; `v.annotations` *is* the document.

*Structure* —

    {"type": "FeatureCollection", "features": [Feature, …]}
    (the empty set is `{"type": "FeatureCollection", "features": []}` —
    the `annotations` trait default)

    Feature = {"type": "Feature",
               "id": <string>,
               "geometry": Point | MultiPoint | LineString
                           | Polygon | MultiPolygon,
               "properties": <object>}

    Point        coordinates: [x, y]
    MultiPoint   coordinates: [[x, y], …]
    LineString   coordinates: [[x, y], …]      (≥ 2 positions)
    Polygon      coordinates: [ring, …]        (ring 0 = outer, rest = holes)
    MultiPolygon coordinates: [[ring, …], …]   (one polygon per island,
                                                 each with its own holes)
    ring         [[x, y], …]  stored **open**

`GeometryCollection` is **not** in the canonical form: it is a recursive
heterogeneous *container*, not a flat geometry — a mixed Point +
LineString + Polygon feature has no coherent one-annotation semantics,
and GC-in-GC makes depth unbounded. It is accepted at import and expanded.

*Semantics* —

- Coordinates: level-0 slide px, floats, origin at the slide origin,
  **y down** (slide orientation; no CRS handling), unclamped —
  off-slide coordinates are legal, the renderer culls them. A position's
  third component (z) is dropped at import.
- **One feature = one annotation**: one id, one `properties` bag, one
  draw / hit-test / select / edit unit. A `MultiPolygon` is one
  annotation spanning several islands (one label, one color, one
  delete), not several — the evenodd fill and hit-test treat all its
  rings as one parity region.
- `id`: unique string across the collection, stable for the document's
  lifetime — assigned once at import (or on draw, M3), never renumbered
  by edits. This is the *whole* role ids play: M4's selection
  (`selectedId`) and edit commands address features by id across the
  Py⇄JS round trip. Rendering, culling, and draw order are list-order,
  not id-keyed.
- `properties`: arbitrary string keys/values pass through untouched,
  except a `null`-valued key is dropped (the canonical form has no
  `null` values; absence is the no-value state). The viewer recognizes
  `label` (string), `color` (CSS stroke/outline color, default
  **black**), and `fill` (CSS interior fill, default **transparent** —
  polygons are outline-only unless a fill is given; an `rgba()` string
  gives a translucent fill). Absence means *"use the default"*, not
  *"none"* — the renderer treats absent and `null` identically.

*Invariants* — every document the viewer produces satisfies these (which
is what keeps the renderer's per-feature guard a one-line `continue`):

- `Point` / `MultiPoint`: finite positions;
- `LineString`: ≥ 2 finite positions, length ≥ 1e-6 px;
- `Polygon` / `MultiPolygon`: each ring ≥ 3 finite, non-collinear
  positions (area ≥ 1e-6 px²), stored open; a `MultiPolygon` keeps ≥ 1
  island.

*Import normalization* (the only place any conversion in the system
lives):

- Inputs: a `FeatureCollection`, a single `Feature`, or a bare geometry
  object (no properties), **any of the six GeoJSON geometry types**;
  strict structural validation (bad structure or non-finite coordinates
  → `ValueError`).
- **Structure-preserving**: geometry type and nesting are never
  changed — only coordinate *values* are normalized (floats, open rings),
  degenerate members dropped, ids/`properties` normalized. GeoJSON in →
  same-structure GeoJSON out (modulo dropped degenerates), so the stored
  document reads back to its source's shape.
- Coordinates are level-0 slide px (origin at the slide origin, **y
  down**, no CRS) — no other unit convention is supported.
- Degenerates (same thresholds as the M2 parser): a `MultiPolygon` loses
  degenerate islands (warning); a feature left with nothing is dropped
  (warning); a `Polygon` with any degenerate ring — including a hole —
  is dropped (warning); a `LineString` below the length threshold is
  dropped (warning).
- ids: the feature's `id` (str/int/float → `str`) when present, else a
  fresh `aN`; **uniqueness enforced** — a collision is a `ValueError`
  (fixes the M2 latent bug: a `Multi*` / `GeometryCollection` feature's
  id was stamped on *every* shape it expanded to).
- `properties` kept whole minus `null`-valued keys; a non-string
  `label` / `color` / `fill` is a `ValueError` (as M2).

**Wire:** new trait `annotations` (Py→JS, dict — the document). The *entire* normalized
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

- **Culling:** features whose bbox does not intersect the current
  viewport are not rendered (checked per frame against the local
  transform).
- Screen-constant styling (does **not** scale with zoom), per feature,
  three primitives: *markers* — `Point` / `MultiPoint`, one filled circle
  per position (~4 px radius; body `fill` if given, else `color`; white
  halo for contrast on tissue); *open path* — `LineString`, 1.5 px in
  `color`; *evenodd ring-set* — `Polygon` / `MultiPolygon` (a
  `MultiPolygon`'s islands flatten into the same ring set), one path over
  all rings, interior `fill` (default transparent; evenodd so holes cut
  through) and a 1.5 px `color` outline (default black).
- **Alpha:** a toolbar slider (0–1, default 1) sets the overlay's
  `globalAlpha`, fading the whole annotation layer. View-local display
  state, not a trait (resets on restart, like the local transform).
- Labels: 12 px screen-space text at the feature's first vertex (a
  `Point`'s position — the M2 next-to-the-point placement), only when
  `label` is set (M4: any kind — §6.5).
- Pure module `frontend/annotations.js`
  (`drawAnnotations(ctx, {transform, annotations, alpha})`), unit-tested
  against a mock ctx like the compositor; a malformed feature is skipped
  (the one-line per-feature guard the document invariants make
  possible).

**Python API:**

```python
v.set_annotations("roi.geojson")     # str / Path, or a parsed dict
v.clear_annotations()
v.annotations      # the normalized document (FeatureCollection, level-0 px)
```

`set_annotations` replaces the current set; coordinates are level-0 slide
px, so it never needs the slide open.

**Not in M2** (explicit): any UI editing (no add-on-click, drag, edit,
or delete — polygon drawing is M3, §6.4; select/delete/label/recolor is
M4, §6.5); annotation hit-testing, hover, and tooltips (M4 adds
click hit-testing + selection, §6.5; hover and tooltips stay out);
per-feature visibility; annotation export as an API (there is none —
the document *is* `v.annotations`, §6.3; `json.dump` is the export);
`HtmlSlideViewer` annotations (canvas view only, M0 fallback untouched).

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
  tiles); the minimap stays live; M1's double-click zoom is **removed in
  M3** — the gesture is unnecessary, and in the mode a dblclick would
  merely inject two coincident vertices.
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
3. valid → append a `Polygon` **feature** to `self.annotations`'s
   `features` — `{"id": <fresh, non-colliding>, "geometry":
   {"type": "Polygon", "coordinates": [ring]}, "properties": {}}` —
   and push the `annotations` trait. From then on the feature is
   indistinguishable from an imported one: same defaults (empty
   `properties` = black stroke, transparent fill, §6.3), same renderer,
   same `clear_annotations()`.

"Closing the first and last point" is the M2 renderer's existing behavior
(rings are stored open, closed when traced), so the saved shape needs no
new rendering code.

**Not in M3** (explicit): Python-side event hooks (gone with
`last_click`/`last_region`); point/line/rect creation; editing or
deleting individual shapes (`clear_annotations()` only — selection and
the del / label / color actions are M4, §6.5); hit-testing, hover,
tooltips (M4 adds click hit-testing + selection, §6.5; hover and
tooltips stay out); annotation export; keyboard beyond A/Esc; touch;
`HtmlSlideViewer` (M0 fallback untouched, as in M2).

### 6.5 Annotation editing (M4)

M4 is **editing**: a shape that already exists — imported (M2) or drawn
(M3) — can be selected in the view and deleted, labeled, or recolored.
No geometry editing: no per-shape move, resize, or vertex edit (the §2
non-goal stands); `coordinates` only change by re-importing.

**Annotate button.** M3's drawing mode was keyboard-only. M4 gives it a
toolbar toggle button, **annotate** (pressed while drawing): the visible
entry point to the annotation tools. **A** stays the alias and the two
drive the same mode state machine (§6.4) — the button only mirrors the
mode in its pressed state.

**Selection (idle mode).** In no mode, a left click that is a click (not
a drag — the same ≥ 4 CSS px threshold as M3) hit-tests the annotation
set at the cursor, in screen space under the local transform:

- Hit-testing is per geometry: `Point` / `MultiPoint` — within ~8 px of
  any marker position (the 4 px radius + halo + slack); `LineString` —
  within ~6 px of any segment; `Polygon` / `MultiPolygon` —
  point-in-polygon under evenodd parity over all rings (holes are no
  hits; a click on the outline hits, since the outline is inside; a
  `MultiPolygon`'s islands are just more rings of the *same* feature —
  a click inside any island hits it). Topmost (last feature in list
  order, matching draw order) wins. Pure
  `hitTest(annotations, transform, x, y) -> id | null` in
  `annotations.js` (reuses the module's feature helpers; mock-ctx-tested
  like the rest of it).
- Hit → **select**: a view-local `selectedId` is set — like the local
  transform, the alpha slider, and the M3 draft, not a trait; Python
  never sees the selection, only the edit commands. The selected shape
  is highlighted: drawn last, with a 3 px accent (`#ff8c00`) stroke in
  place of the usual 1.5 px (a point gets an accent ring around its
  marker). That is a new `selectedId` argument to `drawAnnotations` —
  one draw pass per frame, no extra overlay.
- Miss → deselect; clicking another shape moves the selection.
- Navigation never touches the selection: a ≥ 4 px left drag is a pan,
  wheel/minimap reposition — the highlight is per-frame over the shape
  data + `selectedId`, so it tracks the shape through re-projection for
  free.
- Entering drawing mode (**A** / annotate) clears the selection (in that
  mode a click is a vertex). Any `annotations` push re-validates: the
  selected id is no longer in the set → clear (covers
  `clear_annotations()`, a `set_annotations()` replace, and a delete
  coming back through the round-trip).

**Contextual actions.** While a shape is selected, three toolbar buttons
are enabled (disabled otherwise):

- **del** — removes the shape: the view clears the selection and issues
  the command below; the round-tripped set drops it from the overlay.
- **label** — opens an inline text input in the toolbar, prefilled with
  the current label (empty when `null`) and focused; Enter or blur
  commits (empty text → clear the label), Esc cancels. The label is
  data on any kind, and M4 extends label *rendering* (M2 drew labels
  next to points only) so a `line` / `polygon` label is drawn at its
  first vertex.
- **color** — opens two native color pickers (stroke, fill) plus a
  clear-fill checkbox (a native color input cannot encode transparent),
  prefilled from the shape (null → default black / clear-fill). Picking a
  fill color auto-unchecks clear-fill (the picker wins); checking the box
  commits `null`. Each picker close (`change` event) commits the full
  target `(color, fill)` pair, so the user can change the stroke only, the
  fill only (incl. no fill), or both — every command is self-contained.

After **del** the actions re-disable (selection cleared); after
**label** / **color** the selection persists so edits chain.

**Wire: `annotation_edit` (JS→Py, default `null`).** The house
last-event pattern (`viewport`, §6.2; `last_polygon`, §6.4) — one trait
for all three ops, discriminated by `op`, so the contract grows by one
row instead of three:

```
null | {op: "delete",  id}
     | {op: "set_label", id, label}        # string; "" / whitespace-only = clear (null)
     | {op: "set_color", id, color, fill}  # each a CSS color, or null (= default)
```

The view is the sole writer, saving per command (`model.set` +
`model.save()`, as M3's `last_polygon`); the trait holds the *last
issued* command, not cleared (same convention). The Python `observe()`
handler applies it with a pure `apply_edit(doc, cmd)` in
`annotations.py` — a new document, or `None` when the id is unknown (no
state change, `status` = `edit ignored: unknown annotation id` — the id
may be stale if a Python-side `set_annotations()` replace raced the
click) — `delete` removes the feature, `set_label` / `set_color` mutate
its `properties` (no-value → the key is dropped: `set_label` with
`""` / whitespace-only clears the label; `set_color` with a `null`
member drops it — the M2 *use-the-default* convention, black stroke,
transparent fill) and pushes
the whole updated set on `annotations` (the M2 wire rule: whole set, no
viewport filtering). `status` reports the applied op (`deleted #a3` /
`edited #a3 (label)` / `edited #a3 (color)`).

Last-event replay is safe here in a way it is not for `last_polygon`:
on re-attach the JS snapshot re-sends both `annotations` (the pushed
set) and the last command, and all three ops are **idempotent** over
that set — a `delete`'s id is already gone, `set_label` / `set_color`
re-apply the same values — so replay is a harmless no-op. (`last_polygon`
is an append: re-sent after the set is restored, it duplicates the shape
— a known M3 limitation, out of scope here.)

**Python API** (the same apply logic, for headless/programmatic use;
ids from `v.annotations`):

```python
v.delete_annotation(id)
v.set_annotation_label(id, "tumor")      # None / "" clears
v.set_annotation_color(id, color="red", fill=None)   # None = default
```

**Not in M4** (explicit): geometry editing (move / resize / vertex — §2
non-goal); multi-select and drag-select; hover highlight and tooltips
(click only); keyboard shortcuts for del / label / color (**A** stays
with the mode toggle); undo (the reset paths are `clear_annotations()`
/ re-import); annotation export (after edits `v.annotations` is the
live set; a GeoJSON export is not v1); `HtmlSlideViewer` (canvas view
only, as in M2/M3).

### 6.6 Smooth zoom (M5): level cross-fade, center-first fetch

M5 is **feel**: port the two OpenSeadragon ideas that make its zoom smooth —
a temporal cross-fade across level changes and viewport-center-first tile
fetch — into the existing push architecture. The design rule stands:
**Python owns all state and decoding; the JS view is a compositor** (§3).
No new traits, no wire change (module version stays 2.0.0; the
labextension rebuild is still required for the JS changes),
`HtmlSlideViewer` untouched. Attribution: both are OSD *ideas* (the
`blendTime` temporal blend; the tile-priority queue as a pre-sorted list),
not code ports.

#### 6.6.1 Level cross-fade (view-local draw policy; `frontend/blend.js`, new pure module)

Today a zoom that crosses a level boundary swaps `tile_geo` wholesale: the
old level vanishes and the new one pops in at 100 % the moment the push
lands. M5 cross-fades, temporally (OSD's `blendTime` idea):

- **Accumulated tile map.** The view already merges `tiles` into `this._images`
  (insertion-order LRU, 400 entries, never evicts a still-visible key in
  practice). M5 makes `tile_geo` accumulate the same way: a view-local
  `_tileGeo` map merged per push, evicted in lockstep with `_images` (same
  key, same budget). `drawScene` draws from `_tileGeo`, not the latest trait
  value — so the level a zoom is *leaving* stays on screen while the level
  it is *entering* fills in tile by tile.
- **Per-level alpha** — pure `levelAlphas(transition, zoom, levelDownsamples,
  now) -> {level: alpha}`:
  - `selected(z)` = the §4 rule (smallest `L` with `ds[L] >= 1/z`),
    mirrored as `selectLevel` in `tilemath.js` and unit-tested against
    `plan.select_level` on a shared downsample table.
  - A **transition** is recorded when `selected(z)` changes between two
    consecutive frames: `{from, to, start}` (either direction).
  - During a transition (default `BLEND_MS = 300`): the **new** level draws
    at alpha 1, the **old** level at `1 − (now − start)/BLEND_MS` (clamped);
    other levels at 0. No transition: the selected level at 1, others 0.
  - Same-level pushes (a pan) never start a transition — tiles appear at
    full alpha as they arrive, as today.
  - The fade is the only continuous loop in the view: a transient rAF loop
    runs only while a transition is live (stepped with real frame deltas);
    between transitions the view redraws on events as today, so idle CPU
    stays 0 (§9).
- **Compositor** (`drawScene` gains `levelAlphas`): white underlay, then
  present levels **coarsest first** (finest on top, OSD draw order), each
  pass at its `globalAlpha`, seam margin and DPR handling unchanged. The
  coarse-on-bottom ordering means the fading old level covers the screen
  while the new level's tiles land on top of it — no holes at either end of
  the fade.
- Big zoom jumps that skip an intermediate level fade directly
  (old level out, endpoint in); OSD behaves the same.

Memory: the cross-fade transiently holds two levels; the 400-entry image
LRU is sized for this (a 960×540 canvas needs well under 100 tiles per
level). Tile *transfer* is unchanged — the kernel pushes exactly the
plan's level per viewport, as today; the fade is over data already in
transit/on screen, not extra bytes.

#### 6.6.2 Center-first fetch (Python: `plan.py` + `fetch.py` + `widget.py`)

OSD's priority queue exists because its browser fetches are independent
HTTP jobs; ours are sequential local `read_region` calls on one thread, so
the ported *idea* is **fetch order = viewport center first**, implemented as
a pre-sorted chunk list (the chunks are all known at plan time — no heap):

- `ReadPlan` gains `chunks`: the viewport's grid cells grouped into
  **grid-anchored blocks of 4×4 tiles** (1024 level px), each block's read
  rect = the union of its cells clamped to the level bounds — the exact
  §5 invariant, so the `(level, tx, ty)` cache key and the viewport-
  invariance regression test are untouched. Chunks ordered by block-center
  distance to the viewport center (the center block first). A plan whose
  viewport fits one block is a single chunk — the current behavior byte-for-
  byte, including the common zoomed-out case.
- `fetch_tiles` iterates chunks in order (a chunk is skipped when every
  tile in it is cached; otherwise one `read_region` for the block, crop,
  cache — the §5.1 "one big read, crop in Python" strategy, just smaller
  and ordered). ~4 blocks for a 1280×720 canvas; the extra decode setups
  are cheap against a single viewport-sized read.
- **Two-stage push** (this is what makes center-first visible): `_render_once`
  pushes `tiles`/`tile_geo` (a) after the center chunk — partial set — and
  (b) after the remaining chunks — the full set. Single-chunk plans push
  once, as today. The trait *semantics* become "tiles available so far for
  the current viewport"; the **final** value after a render is identical to
  today's full-viewport set, so re-attach replay and every existing
  contract (keys, `tile_geo` geometry, status) hold. A partial push from a
  superseded viewport is harmless: the JS side only merges, and the render
  loop's coalescing re-plans from the latest viewport. Comm cost: one extra
  ~50–150 KB partial push per multi-chunk render, only at mid/zoomed-in
  levels.

#### 6.6.3 Unchanged / out of scope

Wire contract (no new traits; module version 2.0.0), the 120 ms debounce
(measure before touching it), M2–M4 annotation behavior, `read_crop`, the Python
API, `HtmlSlideViewer`, `TileCache` (byte budget, keying), the OpenSlide
backend. Explicitly *not* in M5: animated pan/zoom (springs — dropped from
this plan; wheel/drag/minimap/toolbar and programmatic
`center_on`/`set_zoom` apply instantly, as today — the cross-fade is the
only animation), WebGL compositing, off-screen prefetch
(the LRU covers back-pans), per-tile (sub-level) fades, parallel fetch
threads, touch pinch, keyboard navigation, OSD-style reference strip.

## 7. Python API

```python
from islide import SlideViewer

v = SlideViewer("sample.svs")   # opens in background; safe to display now
# M3: SlideViewer(path, jpeg_quality=90) — tile JPEG quality (default 85)
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
v.set_annotations("roi.geojson")   # level-0 slide px; str / Path or a parsed dict
v.clear_annotations()
v.annotations                     # the normalized document — the GeoJSON export itself (§6.3)

# M3: draw a polygon in the view — key A, left-click the vertices,
# key A again saves it into v.annotations (no programmatic API)

# annotation editing (M4) — the same ops the view's del/label/color
# buttons issue; ids from v.annotations
v.delete_annotation("a3")
v.set_annotation_label("a3", "tumor")   # None / "" clears the label
v.set_annotation_color("a3", color="red", fill=None)  # None = default

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
| `tiles` | Py→JS | dict | `{"level:tx:ty": dataURL}` — replaced wholesale per push; M5: the *final* push of a render is the full viewport set, but a multi-chunk render pushes a partial (center-first, §6.6.2) set en route — the JS view merges, never evicts per push |
| `tile_geo` | Py→JS | dict | `{"level:tx:ty": [level, ox, oy, cw, ch]}` — absolute level-pixel crop origin + size; the view reprojects this under its local transform |
| `minimap_img` | Py→JS | dataURL | whole-slide overview (top-level JPEG), set once |
| `annotations` | Py→JS | dict | the normalized annotation document — a restricted GeoJSON `FeatureCollection` in level-0 px (§6.3); M2: imported set, M3: drawn polygons appended (§6.4) |
| `last_polygon` | JS→Py | list | M3: the just-saved drawn polygon — open ring `[[x, y], …]`, level-0 px (`null` = none yet); Python validates and appends the shape to `annotations` |
| `annotation_edit` | JS→Py | dict | M4: the last issued edit command — `{op: "delete" \| "set_label" \| "set_color", id, …}` (`null` = none yet); Python applies it to `annotations` and pushes the updated set (§6.5) |
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
backend closes the wrapped object). The viewer exposes it as a keyword-only
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
| Zoom into uncached region (M5) | viewport-center chunk first: the center visible within one 1024 px-block read + round trip (≤ ~200 ms), the rest of the viewport follows in the same render pass |
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
| M1 ✅ | **Interactive JS view** *(done)*. Canvas compositor (`compositor.js` + pure `tilemath.js`), wheel/drag/dblclick pan-zoom at the cursor, minimap with viewport rect, −/+/fit/1:1 toolbar, DPR-aware canvas, `ResizeObserver` resize. Trait contract per §7; viewport sync is JS-written + debounced (120 ms) + coalesced onto a Python background render thread. M0's HTML viewer is kept as the standalone class `HtmlSlideViewer` (no-extension fallback / reference pipeline), and the Python-side `SlideViewer` gained the background open + `wait()` + programmatic viewport API the JS view drives. **Not yet covered:** in-browser verification (no browser in the dev sandbox — the view is verified by node unit tests of the pure math/compositor/draw state + headless widget tests; the extension build path is documented, see §11). | `examples/m1_demo.ipynb` (canvas) + smooth pan/zoom |
| M2 ✅ | **Read-only annotations** *(done)*. GeoJSON import (FeatureCollection / point / line / polygon; level-0 `px` default, `um` option) → normalized shape list → `annotations` trait (Py→JS) → second canvas overlay: viewport culling, screen-constant styling (black stroke / transparent fill defaults, per-feature `color`/`fill`/`label`), point labels, alpha slider. No UI editing (M3 adds polygon *drawing*, §6.4). | `examples/m2_demo.ipynb`: `set_annotations` (inline GeoJSON, level-0 px + microns) + smooth pan/zoom over the overlay |
| M3 ✅ | **Polygon drawing** *(done)*. Key **A** toggles a drawing mode (crosshair; live draft: vertex dots, segments, dashed closure to the cursor); left click appends a vertex (≥ 4 px left drag = pan, right-drag pans in both modes, wheel/minimap live); M1's double-click zoom gesture is removed (unnecessary); the second **A** saves the ring — the view sets `last_polygon` (JS→Py) and Python normalizes it (≥ 3 pts, nonzero area, else discarded) and appends a `polygon` shape to `annotations`; **Esc** cancels. No callbacks; no point/line/rect features; the reserved `last_click`/`last_region` traits are removed from the contract. Tile JPEG quality becomes a constructor argument (`jpeg_quality`, default 85; the §5.3 PNG fallback is dropped — exact pixels via `read_crop()`). | `examples/m3_demo.ipynb`: hand-drawn polygon + `v.annotations` + `read_crop` of its bbox |
| M3.5 ✅ | **Canonical GeoJSON annotation format.** The M2/M3 flat shape list becomes the annotation document of §6.3: the `annotations` trait is a `Dict` (empty-`FeatureCollection` default; trait assignment coerced through the normalizer); `set_annotations(doc, units)` keeps its contract — validate → normalize → assign → push — and the stored/returned value *is* the document; the `last_polygon` observer appends a `Polygon` feature (fresh non-colliding id, empty `properties`); the renderer iterates features over three primitives (markers / open path / evenodd ring-set) and reads `label`/`color`/`fill` from `properties`. `MultiPoint` / `MultiPolygon` stay whole; `GeometryCollection` expands at import; degenerate members drop with warnings; ids unique (collision → `ValueError`) and stable — fixing the M2 duplicate-id bug. Wire-contract change: module version 1.0.0 → 2.0.0 + labextension rebuild; the `last_polygon` and planned `annotation_edit` contracts are unchanged. | `tests/test_annotations.py` (canonical document over all six source geometry types; units; degenerate drops; id uniqueness; `properties` pass-through), `test_widget_m2.py` / `test_widget_m3.py` (document trait; `last_polygon` appends a feature), `frontend/test/annotations.test.js` (three primitives; per-feature guard); `examples/m2_demo.ipynb` + `m3_demo.ipynb`: same behavior, `v.annotations` is the document |
| M4 ✅ | **Annotation editing.** Toolbar **annotate** toggle (visible entry to the M3 drawing mode; **A** unchanged). In idle mode a left click hit-tests existing annotations → selection (thick accent highlight; miss clears; navigation keeps it; entering drawing mode clears it). While selected: **del** removes the shape, **label** attaches/edits its label (inline input; Enter commits, Esc cancels; labels now render on lines/polygons too), **color** sets stroke color and/or fill (pickers + clear-fill). Edits ride the new JS→Py `annotation_edit` last-event trait; Python applies with pure `apply_edit` and pushes the whole set; the ops are idempotent over the restored set, so replay is safe. No geometry editing (move/resize/vertex), no multi-select, no hover/tooltips, no undo, no export. Polish: the −/+ zoom toolbar buttons are removed (wheel zoom covers them; **fit** / **1:1** stay). | `examples/m4_demo.ipynb`: select an imported polygon → label + color; draw a polygon (M3) → delete; `v.annotations` reflects the edits |
| M5 | **Smooth zoom.** Two OSD feel-ideas, ported into the push architecture (§6.6): (a) *level cross-fade* — `tile_geo` accumulates view-side (evicted in lockstep with the 400-image LRU); when the §4 selected level changes the compositor draws the new level at alpha 1 and fades the old one out over 300 ms, coarsest-first (pure `blend.js`, no wire change, no extra bytes — the kernel still pushes one plan-level per viewport; a transient rAF loop runs only while the fade is live, idle CPU stays 0). (b) *center-first fetch* — the read plan is split into grid-anchored 4×4-tile (1024 px) blocks sorted by distance to the viewport center (the OSD tile-priority-queue idea as a pre-sorted list — no heap; the §5 cache-key invariant and the viewport-invariance regression test are untouched), and `_render_once` pushes in two stages (center chunk, then the full set) so the viewport center appears first; single-block plans (the zoomed-out case) push once, byte-for-byte as today. No new traits; module version 2.0.0; `HtmlSlideViewer` untouched. Out: animated pan/zoom (springs, dropped from this plan), WebGL, prefetch, per-tile fades, parallel fetch, touch pinch (§6.6.3). | `examples/m5_demo.ipynb`: wheel-zoom/pan walkthrough (cross-fade), M3 draw + M4 select during/after a fade; `tests/test_plan.py` (chunking, center-first order, single-chunk regression), `test_widget_m5.py` (two-stage push; final trait == full set), `frontend/test/blend.test.js`, compositor multi-level alpha |
| M6 | **Polish & ship.** Docs (README + docsite), example slides in docs, perf pass (DPR-aware canvas, HiDPI crispness), PyPI release `jupyter-islide`, `pip install jupyter-islide[dev]`, CI. | published package |

M6 cleanup (done): removed the M0 spike viewer `HtmlSlideViewer` (the HTML
tile-composite class in `widget.py`) and `examples/m0_demo.ipynb` — the M1
canvas view is the sole view, so the "no-extension fallback" no longer
exists. The per-milestone demo notebooks `m1_demo`–`m5_demo` were merged
into two: `examples/viewer_demo.ipynb` (viewer: canvas, programmatic
control, M5 rendering feel) and `examples/annotations_demo.ipynb`
(annotations: import, drawing, editing). Milestone rows M0–M5 above are
historical record and keep their references. |

## 11. Testing

- **Unit (no OpenSlide needed):** tile planning, level selection, viewport
  math, coordinate transforms, LRU cache, trait/callback logic — pure
  functions over a `FakeSlide` (metadata-only stub of `SlideBackend`).
- **Integration (needs libopenslide):** real open/read against a real slide,
  `data/CMU-1.tiff` (whole-slide generic-TIFF, 46 000 × 32 914 px, 9 levels,
  1.0 mm/px, opens via the generic-TIFF vendor — this resolved the
  design-phase question about synthetic pyramids; level-selection math is
  additionally unit-tested against a fixed `SlideMeta` fixture in
  `tests/test_plan.py`). The slide is not committed: the session-scoped
  `slide_path` fixture in `tests/conftest.py` downloads it at test time from
  openslide-testdata
  (`https://openslide.cs.cmu.edu/download/openslide-testdata/Generic-TIFF/CMU-1.tiff`)
  if `data/CMU-1.tiff` is missing, and dependent tests skip if the file is
  absent and the download fails.
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
  GeometryCollection, properties (label/color/defaults), error paths
  (malformed document), degenerate-feature skip with warning. Headless widget
  (`tests/test_widget_m2.py`): `set_annotations`/`clear_annotations` →
  `annotations` trait shape and level-0 values, replace semantics, no-wait
  import. JS (`frontend/test/annotations.test.js`): `drawAnnotations`
  against a mock ctx — culling, per-kind draw calls, screen-constant widths,
  `color`/`fill` defaults (black/transparent), alpha (`globalAlpha`),
  label rendering. Trait contract: `annotations` added to `defaults.js`,
  covered by the existing cross-language name guard.
- **M3 polygon tests:** Python headless (`tests/test_widget_m3.py`):
  setting `last_polygon` → a `polygon` shape appended (kind, ring,
  `null` styling, fresh non-colliding id); degenerate drafts (2 pts,
  collinear 3 pts, non-finite) dropped with a warning and no state
  change; successive saves get unique ids; `null` default ignored;
  `clear_annotations()` after; `jpeg_quality` constructor arg (default 85,
  alters the `tiles` payload only). JS (`frontend/test/polydraw.test.js`):
  the pure state machine (A/Esc enter/save/cancel, click-vs-drag
  threshold, vertex accumulation) and `drawDraftPolygon` against a mock
  ctx (vertex dots, segments, dashed closure to cursor / first vertex).
  Contract: `last_polygon` added to `defaults.js` and
  `last_click`/`last_region` removed from both sides — covered by the
  existing cross-language name guard.
- **M3.5 canonical-form tests:** Python (`tests/test_annotations.py`,
  rewritten for the new output): the canonical document over all six
  source geometry types — `MultiPoint` / `MultiPolygon` kept whole,
  `GeometryCollection` expanded, `properties` pass-through (unknown keys
  preserved), ids unique (collision now `ValueError` — the M2
  duplicate-stamp bug), degenerate drops (islands, rings, short
  lines) with warnings, bare-geometry + single-Feature inputs. Headless
  widget (`tests/test_widget_m2.py` / `test_widget_m3.py`): `annotations`
  is the document (empty-FC default, `set_annotations` replace semantics,
  no-wait import); `last_polygon` appends a `Polygon` feature (fresh
  non-colliding id, empty `properties`). JS
  (`frontend/test/annotations.test.js`): the three draw primitives
  (markers incl. `MultiPoint` loops, open path, evenodd ring-set incl.
  flattened `MultiPolygon` islands), `properties` styling, per-feature
  guard (a malformed feature is skipped, the rest still draw).
- **M4 annotation-edit tests:** Python headless
  (`tests/test_widget_m4.py`): `apply_edit` pure over the document
  (delete removes exactly the matching feature, order preserved;
  `set_label`
  sets / clears (`""` / whitespace-only → key dropped); `set_color` sets
  stroke and/or fill incl. resetting to the defaults (`null` members
  dropped); unknown id → no state change, input list untouched); setting the
  `annotation_edit` trait → `annotations` trait updated with the
  normalized set, unknown id → no push; the
  `delete_annotation` / `set_annotation_label` /
  `set_annotation_color` API routes through the same `apply_edit`;
  `clear_annotations()` after. JS
  (`frontend/test/annotations.test.js`): `hitTest` pure (polygon
  evenodd interior / outline / holes, line distance tolerance, point
  radius, topmost-first, miss → `null`) and `drawAnnotations` with
  `selectedId` (accent stroke, wider width, drawn last). Contract:
  `annotation_edit` added to `defaults.js` — covered by the existing
  cross-language name guard.
- **M5 smooth-zoom tests:** Python (`tests/test_plan.py` additions +
  `tests/test_widget_m5.py`): chunking — a viewport inside one 1024 px
  block yields a single chunk **identical to the pre-M5 plan** (level, loc,
  size, read_origin, tiles, crops: byte-for-byte regression); a wider
  viewport yields ≥ 2 chunks with the viewport-center block **first**
  (distance order, tie-break by reading order), every chunk's read rect
  grid-anchored and clamped (the §5 invariant per chunk), and the union of
  chunk tiles == the whole-viewport tile set. Widget: two-stage push — a
  multi-chunk render assigns `tiles`/`tile_geo` twice, the first value a
  strict subset of the final one (spied via a subclass or a
  `read_region`-instrumented `FakeSlide`), the **final** value identical to
  the pre-M5 full-viewport set (keys, `tile_geo` geometry, payload —
  `test_widget_m1.py`'s cache-invariance and tile-contract tests pass
  unchanged); a single-chunk render pushes exactly once; a superseded
  (dirty) render mid-pass leaves the final traits consistent with the
  latest viewport. JS (`frontend/test/`): `blend.test.js` —
  `selectLevel` matches `plan.select_level` on a shared downsample table
  (the §4 rule, both sides, incl. the coarsest-level fallback); a
  transition is recorded on a selected-level change and only then
  (same-level pan: none); the alpha schedule — new level 1, old level
  `1 → 0` over `BLEND_MS` (clamped, direction-independent: zoom-in and
  zoom-out), stale/other levels 0; a new transition replaces the old.
  `compositor.test.js` — multi-level draw with per-level `globalAlpha`,
  coarsest-first ordering, seam margin and skip-not-ready unchanged.
- **Cross-language contract test:** the Python synced trait names are
  asserted to equal the keys in `frontend/defaults.js` (both directions of
  the same guard), so the comm contract can't drift.
- **JS tests (no browser, `frontend/test/`, `node --test`):** the pure
  math (`tilemath.js`) — round-trips, cursor-fixed zoom, pan, tile screen
  rects (checked against the reference screen-box formula), visible-tile selection,
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
│   ├── __init__.py           # SlideViewer, Viewport, …
│   │                         # + _jupyter_labextension_paths()
│   ├── widget.py             # M1 DOMWidget + state machine
│   ├── backend.py            # SlideBackend protocol, OpenSlideBackend
│   ├── plan.py               # viewport -> read plan (pure)
│   ├── annotations.py        # GeoJSON -> normalized annotation document (pure)
│   ├── viewport.py           # SlideMeta + Viewport (pure)
│   ├── cache.py              # TileCache
│   ├── fetch.py              # plan -> tiles (cache + one read, cropped)
│   └── encode.py             # tile -> JPEG data URL
└── frontend/                 # JS canvas view (npm: jupyter-islide)
    ├── tilemath.js  compositor.js  annotations.js  polydraw.js  model.js  view.js
    ├── blend.js                     # M5: level cross-fade (pure)
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
   traits are removed from the contract; M4 = **annotation editing**
   (selection + del / label / color, §6.5); M3.5 inserted: the M2/M3
   shape-list store becomes the canonical annotation document (§6.3) —
   the `annotations` trait's type changes (wire break: module version
   bump); polish & ship moved to M5. *M2 done; M3 re-scoped as
   proposed; M3.5 as proposed.*
