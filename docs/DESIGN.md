# jupyter-islide — Design Document

An [ipywidgets](https://ipywidgets.readthedocs.io/)-based widget for
interactively exploring pathology whole-slide images (WSIs) in Jupyter,
backed by [OpenSlide](https://openslide.org/) via
[openslide-python](https://github.com/openslide/openslide-python).

This document describes the system as it is. Deep dives live in
[docs/](docs/): the annotation document model, drawing, and editing
([docs/annotations.md](docs/annotations.md)); the test suites
([docs/testing.md](docs/testing.md)); packaging, dependencies, and release
([docs/packaging.md](docs/packaging.md)); verified OpenSlide API notes
([docs/openslide-api.md](docs/openslide-api.md)).

---

## 1. Problem

Whole-slide images are enormous (typically 100k × 100k px, several GB) and
are customarily viewed in dedicated desktop apps (OpenSlide viewer, Aperio,
etc.). Researchers working in Jupyter have no convenient way to:

- open a WSI next to their data,
- zoom into a region of interest (ROI) and inspect it interactively,
- navigate programmatically (e.g. "jump to the bounding box computed by my
  segmentation"),
- correlate image regions with annotations.

Existing Python options are either static (dump a full image or a pre-cut
crop) or heavyweight (standalone viewers, remote servers like
`openslide_server`, DeepZoom tileservers).

**Goal:** a `SlideViewer` widget: open a file, pan/zoom it in the
notebook, drive it from Python.

## 2. Goals / Non-goals

### Goals

- Open any slide format supported by OpenSlide (Aperio SVS, MIRAX,
  Hamamatsu, Leica, Ventana, generic tiled TIFF, …).
- Smooth interactive pan/zoom with the mouse (scroll = zoom at cursor,
  drag = pan), a minimap for coarse navigation, and a level cross-fade so
  zooming across a level boundary stays smooth (§6.6).
- Programmatic control: `center_on(x, y)`, `set_zoom(z)`, viewport queries,
  `read_crop`.
- Annotation overlay: import shapes from GeoJSON, hand-draw polygons, and
  select / delete / label / recolor / vertex-edit them in the view
  ([docs/annotations.md](docs/annotations.md)).
- Full-slide overlay image with adjustable opacity (e.g. a model heatmap
  over the H&E stain).
- Stay in the kernel: no external server, no tile pyramid to pre-generate.

### Non-goals

- Full geometry editing of annotations: the view can drag an individual
  vertex and insert one into a selected feature's edge, but there is no
  per-shape move, resize, or bulk geometry edit — and no multi-select,
  undo/redo, export, or hover tooltips.
- Remote slides (HTTP/`openslide_server`, S3). *The backend interface is
  designed to allow it later (§7.1); the shipped viewer is local files
  only.*
- JupyterLite (OpenSlide is a C library; no WASM build to maintain).
- Multi-slide side-by-side comparison (the API allows it; UI doesn't).
- Serving slides to other people (this is a personal/research viewer, not a
  web service).
- Associated images (e.g. Aperio whole-slide macro shots in
  `slide.associated_images`) — a candidate future "overview" source; not
  exposed.

## 3. Architecture Overview

```
┌────────────────────────────────────────────────────────────┐
│  Jupyter frontend (classic notebook or JupyterLab)         │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  jupyter-islide JS view (small @jupyter-widgets view)│  │
│  │                                                      │  │
│  │  canvas.islide-canvas        canvas.islide-annotations│  │
│  │  (tile + overlay composit)   (shapes in slide coords)│  │
│  │  canvas.minimap              toolbar (buttons, mpp   │  │
│  │                              readout, α slider)      │  │
│  │                                                      │  │
│  │  mouse/keyboard: wheel, drag, A, Esc ──► traits     │  │
│  └───────────────────────▲──────────────────────────────┘  │
│                          │ comm (ipywidgets traits)
└──────────────────────────┼──────────────────────────────────┘
                           │  viewport: {cx, cy, zoom, …}
                           │  tiles:    {key: b64-jpeg}   (Py ─► JS only)
                           │  annotations: {FC}           (Py ─► JS;
                           │                                   edits return
                           │                                   as commands)
┌──────────────────────────┴──────────────────────────────────┐
│  Kernel (Python)                                            │
│                                                             │
│  SlideViewer (widgets.DOMWidget)                            │
│    ├─ SlideBackend ── openslide.OpenSlide                  │
│    │    ├─ metadata (levels, ds factors, mpp, thumbnail)   │
│    │    └─ read_region() ─► PIL.Image (RGBA)               │
│    ├─ plan  (viewport ─► grid-anchored, center-first reads)│
│    ├─ fetch (chunks ─► cache ─► one read per chunk, crop)  │
│    ├─ TileCache   (LRU, count-budgeted, holds PIL images) │
│    └─ state: SlideMeta, Viewport, annotation document      │
└─────────────────────────────────────────────────────────────┘
```

Design rule: **the Python side owns all state and all decoding.** The JS
view is a dumb compositor: it renders whatever tiles it's given, at the
positions the Python side computes, and reports pointer input. This keeps
the notebook kernel the single source of truth, makes programmatic control
trivial, and keeps the JS tiny.

## 4. Coordinates

- **Slide coordinates** are canonical: integer pixels at level 0 (full
  resolution), origin top-left, y down. All public API, callbacks, and
  annotations use these.
- **Viewport** = `(cx, cy, zoom)` where `(cx, cy)` is the slide-coordinate
  center of the canvas and `zoom` = screen pixels per level-0 pixel.
  - `zoom = 1` ⇒ 1:1 with full resolution.
  - Microscope magnification and µm/px are derived:
    `mpp_screen = mpp_level0 / zoom` (mpp from the `openslide.mpp-x`
    property); the JS readout shows both (`"2× · 500 µm/px"`).
- **Level selection:** for the current zoom pick the *finest* pyramid
  level L with `downsample[L] >= 1/zoom` (smallest such L; coarsest level
  as a fallback below the pyramid's range). At that level the slide is at
  least as detailed as the screen, so rendering only ever down-scales
  (crisp, cheap). We do **not** use
  `slide.get_best_level_for_downsample(1/zoom)`: it picks the *coarsest*
  level with `ds <= requested`, which can up-scale (see
  [docs/openslide-api.md](docs/openslide-api.md)).

## 5. Rendering Pipeline

Per viewport change (coalesced to at most one in-flight pass):

1. **Plan.** Viewport in slide coords → screen rect → the covering grid
   cells at level L (global 256-px grid; cell `(tx, ty)` covers level px
   `[tx·T, (tx+1)·T) × [ty·T, (ty+1)·T)`). The **read rectangle is the
   union of those cells, clamped to the level bounds** — anchored to the
   tile grid, *not* to the viewport edges. This invariant is what makes
   the `(level, tx, ty)` cache key sound: a tile's crop then depends only
   on the cell and the slide boundary, never on the viewport (a
   viewport-anchored rect cuts cells at its edges; the cached partial crop
   is later served stretched as a full cell — the bug this avoids,
   regression-tested in
   `tests/test_integration.py::test_tile_cache_is_viewport_invariant`).
2. **Fetch.** Chunks of the grid (4×4-tile blocks, viewport-center first —
   §6.6.2), one `read_region(location, level=L, size=rect_L.size)` call
   per uncached chunk (see §5.1), then crop the result into display tiles
   in Python with PIL. The tile cache is checked per tile; only uncached
   rectangles are read.
3. **Encode.** Each tile → JPEG (quality 85 default,
   `jpeg_quality=1..95` constructor argument) → base64 in the `tiles`
   trait: `{ "<L>:<tx>:<ty>": "data:image/jpeg;base64,…" }`.
4. **Draw.** The JS view composites tiles into `canvas.islide-canvas` at
   `screen_pos = (tile_pos_in_slide / zoom)` with `imageSmoothing` on for
   down-scale levels, then the full-slide overlay image (if set) and, on
   the second canvas, the annotations. Areas with no tile (outside the
   slide bounds) show the canvas background — a gray/white checkerboard
   (transparency-checkerboard style, an 8-px check), painted as a
   repeating pattern fill before the tiles — out-of-bounds reads are
   clamped, never requested.

### 5.1 Fetch strategy: "one big read, crop in Python"

`read_region` is an OpenSlide decode call; calling it per 256 px tile
(≈ 50+ calls per screen at moderate zoom) is dominated by decode setup,
not pixels. Instead, per chunk we read the *whole covering rectangle* in a
single call (at the selected level the full-viewport plan is a single
chunk on the order of the canvas size, e.g. 1280 × 720 ≈ 3.7 MB RGBA)
and slice it into tiles. Consequences:

- a few decode calls per viewport instead of ~50;
- the tile cache still makes re-visits of panned-to regions free;
- no per-tile thread pool (a single in-flight fetch pass is enough).

### 5.2 Caching

- **TileCache**: LRU keyed by `(level, tx, ty)`, valued by PIL image
  (decoded, kept in `RGBA`). Budgeted by tile *count* (`image_cache_max`,
  default 1000 — tiles are grid-anchored `tile_size` level-px cells, so
  at most `tile_size²·4` bytes each, and the count bounds memory
  closely). Same cap and key scheme as the JS view's decoded-image
  cache: one user setting, both sides. Key validity rests on the
  grid-anchored read rect (§5, step 1): the cached image for a cell is
  always exactly `cell ∩ slide bounds`, at every zoom and viewport.
- **Slide handle**: `OpenSlide` objects are opened synchronously in the
  widget constructor (opening large SVS files can take seconds; the
  constructor blocks until open, and raises on failure) and kept open while
  the viewer is live; released by `close()` (which waits for an in-flight
  background render) and, when the frontend disposes the widget without
  `close()` being called, by the comm `on_close` handler registered in the
  constructor (ipywidgets 8 has no Python-side dispose hook, and a
  kernel-side `close()` does not fire the comm's `on_close`).
- **Minimap image** and slide **metadata** computed once at open.

### 5.3 Encoding choice

- JPEG q≈85 for tiles: 10–40 KB/tile over the comm; visually lossless for
  tissue at display size. Quality is a widget constructor argument
  (`jpeg_quality=85` default); there is no PNG fallback — exact pixels are
  `read_crop()`'s job (it returns the raw PIL image, unencoded).
- Tiles are always sent from Python only. The JS side never echoes tile
  data back (the trait is write-once from Python per update; JS just
  merges each push into its tile map).

## 6. Frontend (JS view)

A small custom widget view (`frontend/`, npm name `jupyter-islide`, one
view, no build deps beyond `@jupyter-widgets/base`). This is the standard
ipywidgets route and works identically in classic notebooks and
JupyterLab. The canvas view is the only view.

### 6.1 DOM layout (implemented in `view.js`)

```
<div.islide-view>
  <div.islide-canvas-wrap>
    <canvas.islide-canvas>           ← tiles + overlay image (device-pixel-ratio aware)
    <canvas.islide-annotations>      ← annotation shapes in slide coords (same size)
    <div.islide-minimap>
       <img.islide-minimap-img>      ← slide overview (top-level JPEG)
       <canvas.islide-minimap-rect>  ← current-viewport rectangle
    </div>
  </div>
  <div.islide-toolbar>
    [fit] [1:1] [annotate] [ruler] [del] [label] [color]
    α <range>   zoom readout "2× · 500 µm/px"   cursor "23000, 16457"   status
  </div>
</div>
```

`del` / `label` / `color` are disabled until a feature is selected;
`annotate` is pressed while drawing, `ruler` while measuring. The α
slider is the annotation layer's
opacity (view-local, 0–1). The full-slide overlay image is **not** a
separate layer in the DOM — the compositor draws it on the tile canvas
between the tiles and the annotations, at `overlay_alpha` opacity.

### 6.1.1 Package layout & registration

```
frontend/
  tilemath.js    pure math: fitZoom, zoomAtCursor, panTransform,
                 tileScreenRect, visibleTiles, viewportL0Bbox, selectLevel
  compositor.js  drawScene(ctx, {transform, meta, tileGeo, images, levelAlphas, overlay})
                 — the only place tile pixels are drawn; pure over a ctx
  blend.js       level cross-fade: levelAlphas(transition, zoom, ds, now) (pure)
  annotations.js drawAnnotations(ctx, {transform, annotations, …}) — the overlay
                 pass; hitTest / vertex+segment hit tests / draw helpers (pure)
  polydraw.js    drawing-mode state machine + drawDraftPolygon (pure)
  ruler.js       ruler-measurement state machine + drawRuler (pure)
  model.js       SlideModel extends DOMWidgetModel (defaults only)
  view.js        SlideView extends DOMWidgetView (canvas, mouse, minimap, toolbar)
  defaults.js    SLIDE_MODEL_DEFAULTS — the trait names shared with Python
                 + ISLIDE_MODULE_VERSION
  labextension.js  registers the module with IJupyterWidgetRegistry
  index.js     re-exports (npm "main")
  style/index.css
```

Registration (base-6 pattern, verified against `@jupyter-widgets/base`
6.0.12 and `jupyter-widgets-jupyterlab-manager` 5.0.16):

```js
registry.registerWidget({
  name: 'jupyter-islide',    // must equal _model_module
  version: '2.1.0',          // must satisfy _model_module_version (semver)
  exports: { SlideModel, SlideView },  // keys = _model_name/_view_name
});
```

The manager resolves the Python widget's `_model_module` /
`_model_module_version` through this registry
(`semver.maxSatisfying` over registered versions) and instantiates the
`_model_name` class, then creates a view per the `_view_name` export.
Views use the base-6 lifecycle: subclass `DOMWidgetView`, override
`render()` (`this.el` already exists), bind with
`this.listenTo(this.model, 'change:<attr>', …)`, and send state with
`this.model.set(attr, value); this.model.save();`.

### 6.2 Interactions → state

| Input | Effect |
|---|---|
| wheel / trackpad pinch | zoom about the cursor; `zoom` continuous, clamped to `[fit_zoom/4, 16]` (the fit floor and the 16× ceiling match the Python-side clamp in `_set_viewport_sync`) |
| left drag (≥ 4 px) | pan (in both modes) |
| right drag | pan (in both modes) |
| minimap click/drag | center viewport on that point |
| toolbar **fit** / **1:1** | fit slide / 1:1 (the −/+ zoom buttons were dropped — wheel zoom covers them) |
| toolbar **annotate** | toggle drawing mode (the visible entry point; pressed while drawing; **A** stays the alias) |
| key **A** | toggle drawing mode (enter / save-and-exit) |
| toolbar **ruler** | toggle ruler mode — drag to measure (the visible entry point; pressed while measuring; **R** stays the alias) |
| key **R** | toggle ruler mode |
| left drag (ruler mode) | measure the line under the drag — its length is labeled at the midpoint in µm and level-0 px (a still click clears the measurement; right drag still pans) |
| key **Esc** (drawing mode) | cancel: discard the draft, exit |
| key **Esc** (ruler mode) | clear the measurement, exit ruler mode |
| left click (drawing mode) | append a polygon vertex (a ≥ 4 px left drag is still a pan) |
| left click (idle) | hit-test the annotations at the cursor: hit → select (thick accent highlight), miss → deselect; a ≥ 4 px drag is still a pan |
| toolbar **del** / **label** / **color** (a feature selected) | remove the selected feature / attach or edit its label / set its stroke color and fill (incl. clearing the fill) |
| vertex-handle drag (a feature selected, in either mode) | move one position — optimistic local preview, committed on release |
| still click within 320 screen px of a selected feature's segment (drawing mode) | insert a vertex at the click into the closest segment (dashed preview + ghost vertex as the cursor nears) |
| α slider | annotation layer opacity (view-local) |

All navigation mutates the **viewport trait** — the *only* JS→Python
channel for navigation. The wire form is
`{cx, cy, zoom, canvas_w}`: slide-center + zoom plus the canvas *width*
(the width is JS-owned — the view's actual width after sidebar resizes —
while the canvas *height* never crosses the wire: the `canvas_h` synced
trait is the source of truth on both sides, so Python can plan exactly
what the view sees, and programmatic `set_zoom`/`center_on` round-trip
through the same shape). The view is the **sole writer** of `viewport`
from JS: it never echoes Python's viewport back, it only sends
interaction-driven updates (debounced 120 ms,
coalesced by Python onto one background render thread).

Between round-trips the view keeps a **local transform**
(`{cx, cy, zoom, canvasW, canvasH}`) and reprojects the *same* tile
geometry (`tile_geo`, absolute level-pixel crops) under it — pan/zoom is
instant, then the debounced sync triggers the next Python tile pass. The
view caches decoded tile images (draw-order LRU — drawn tiles are touched
on draw, `image_cache_max` entries, default 1000) so back-pans are
canvas-only and eviction only ever takes off-screen tiles.

The annotation updates are the same shape of one-way updates
(`last_polygon`, §6.4; `annotation_edit`, §6.5), so the same round-trip
covers everything and Python can replay state after a kernel restart of
the widget.

The canvas is DPR-aware: backing store is `clientSize × devicePixelRatio`,
drawing in CSS pixels (`setTransform(dpr, 0, 0, dpr, 0, 0)`). Resizes go
through a `ResizeObserver`, which updates the transform's canvas size,
**redraws synchronously** (the RO callback runs after layout but before
paint, so the frame that shows the new box is already drawn at the new
size — deferring the draw one frame to `requestAnimationFrame`
CSS-stretches the old backing store, which reads as squished tiles while
JupyterLab animates the sidebar) and re-syncs. `_drawNow` additionally
re-syncs the transform to the live canvas size if the RO has not caught
up, so scene layout and backing store always share a size.

### 6.3 Annotation overlay

Annotations enter as a GeoJSON document from Python and are rendered on
the second canvas: each feature becomes one of three draw primitives —
markers (`Point`/`MultiPoint`), an open path (`LineString`), or an evenodd
ring set (`Polygon`/`MultiPolygon`) — culled to the viewport and drawn
with screen-constant styling (black stroke / transparent fill defaults;
per-feature `label` / `color` / `fill` properties; the α slider's
opacity). The `annotations` trait **is** the document: a restricted
canonical GeoJSON `FeatureCollection` in level-0 px (one `{id, geometry,
properties}` feature per shape, unique string ids). The full form —
source geometries, normalization rules, validation errors, the
`properties` contract — is specified in
[docs/annotations.md](docs/annotations.md).

### 6.4 Polygon drawing

Key **A** (or the toolbar **annotate** button) toggles drawing mode:
crosshair cursor, live draft (vertex dots, segments, dashed closure to the
cursor). Left click appends a vertex; a ≥ 4 px drag pans; wheel/minimap
work live; **Esc** cancels (discards the draft); the second **A** saves —
the view sets `last_polygon` (a bare open ring in level-0 px, JS→Py),
Python normalizes it (≥ 3 non-collinear finite points, else discarded
with a warning and a status line) and appends a `Polygon` feature with a
fresh non-colliding id (`a0`, `a1`, …). No programmatic draw API —
drawing is a view interaction; the result lands in `v.annotations`
like everything else. Details in
[docs/annotations.md](docs/annotations.md).

### 6.4.1 Ruler measurement (view-local; `frontend/ruler.js`)

The toolbar's **ruler** button (alias **R**) toggles a measurement mode
mutually exclusive with polygon drawing (entering either clears the
other's live draft / measurement). In ruler mode a **left press** starts
a measurement at the cursor and the **drag** moves its far end — no pan
(right drag still pans; wheel zoom still works and reprojects the line,
which is stored in level-0 px). On release the line and its label stay;
a new drag replaces them, a **still click** clears them, and **Esc**
(or the button again) exits the mode. The label at the line's midpoint
shows the length in micrometers — the level-0 distance times the `meta`
mpp — and in level-0 pixels (e.g. `200 µm · 400 px`; pixels only when
the slide has no mpp). The measurement is pure view-local state
(`{active, line}`, level-0 px, unclamped — `ruler.js`'s state machine,
its `formatMeasure` formatting, and its `drawRuler` line+ticks+label
render); it never touches the annotation document and never crosses the
wire.

### 6.5 Annotation editing

In idle mode a left click hit-tests the annotations (polygon evenodd
interior/outline, line distance tolerance, point radius, topmost first) →
selection (thick accent highlight, drawn last); a miss clears it;
navigation keeps it; entering drawing mode clears it. While selected, the
toolbar's **del** / **label** / **color** act, and vertex handles appear
at every position: dragging one moves that position (optimistic local
preview committed on release), and in drawing mode a still click within
320 screen px of a segment previews a vertex insertion there.

Every edit rides the JS→Py `annotation_edit` trait — a **last-event
command slot** `{op, id, …}` with `op` ∈ `delete | set_label | set_color |
set_vertex | add_vertex`. Python applies it with the pure `apply_edit`
(idempotent over the current document, so replay after a widget
re-attach is safe) and pushes the whole updated `annotations` set. A stale
id → no push, status `edit ignored: unknown annotation id`; a refused
vertex edit (bad index / non-finite / coincident / degenerate result) →
`ValueError` from `apply_edit`, status `edit ignored: <reason>`, document
untouched. Details in
[docs/annotations.md](docs/annotations.md).

### 6.6 Smooth zoom: level cross-fade, center-first fetch

Two OpenSeadragon ideas make its zoom smooth — a temporal cross-fade
across level changes and viewport-center-first tile fetch. Both are ported
*as ideas* (not code) into the push architecture. The design rule stands:
**Python owns all state and decoding; the JS view is a compositor**
(§3). The cross-fade adds no wire (view-local draw policy); the
§6.6.2 per-chunk push changes *how* the tile set is delivered (last-event
`tiles`/`tile_geo` pushes + an attach `resync` counter — module version
2.1.0).

#### 6.6.1 Level cross-fade (view-local draw policy; `frontend/blend.js`, pure module)

Without it, a zoom that crosses a level boundary swaps `tile_geo`
wholesale: the old level vanishes and the new one pops in at 100 % the
moment the push lands. The cross-fade (OSD's `blendTime` idea) instead:

- **Accumulated tile map.** The view already merges `tiles` into
  `this._images` (draw-order LRU, `image_cache_max` entries: drawn tiles
  are re-queued at the MRU end each frame, so
  eviction only ever takes off-screen tiles — a cap below the visible tile
  count is the exception). `tile_geo` accumulates the same way:
  a view-local `_tileGeo` map merged per push, evicted in lockstep with
  `_images` (same key, same budget). `drawScene` draws from `_tileGeo`,
  not the latest trait value — so the level a zoom is *leaving* stays on
  screen while the level it is *entering* fills in tile by tile.
- **Per-level alpha** — pure
  `levelAlphas(transition, zoom, levelDownsamples, now) -> {level: alpha}`:
  - `selected(z)` = the §4 rule (smallest `L` with `ds[L] >= 1/z`),
    mirrored as `selectLevel` in `tilemath.js` and unit-tested against
    `plan.select_level` on a shared downsample table.
  - A **transition** is recorded when `selected(z)` changes between two
    consecutive frames: `{from, to, start}` (either direction).
  - During a transition (default `BLEND_MS = 300`): the **new** level
    draws at alpha 1, the **old** level at `1 − (now − start)/BLEND_MS`
    (clamped); other levels at 0. No transition: the selected level at
    1, others 0.
  - Same-level pushes (a pan) never start a transition — tiles appear at
    full alpha as they arrive.
  - The fade is the only continuous loop in the view: a transient rAF
    loop runs only while a transition is live (stepped with real frame
    deltas); between transitions the view redraws on events, so idle CPU
    stays 0 (§9).
- **Compositor** (`drawScene` takes `levelAlphas`): checkerboard underlay, then
  present levels **coarsest first** (finest on top, OSD draw order), each
  pass at its `globalAlpha`, seam margin and DPR handling unchanged. The
  coarse-on-bottom ordering means the fading old level covers the screen
  while the new level's tiles land on top of it — no holes at either end
  of the fade.
- Big zoom jumps that skip an intermediate level fade directly
  (old level out, endpoint in); OSD behaves the same.

Memory: the cross-fade transiently holds two levels; the `image_cache_max` image
LRU is sized for this (a 960×540 canvas needs well under 100 tiles per
level). Tile *transfer* is unchanged — the kernel pushes exactly the
plan's level per viewport; the fade is over data already in transit/on
screen, not extra bytes.

#### 6.6.2 Center-first fetch (Python: `plan.py` + `fetch.py` + `widget.py`)

OSD's priority queue exists because its browser fetches are independent
HTTP jobs; ours are sequential local `read_region` calls on one thread,
so the ported *idea* is **fetch order = viewport center first**,
implemented as a pre-sorted chunk list (the chunks are all known at plan
time — no heap):

- `ReadPlan` carries `chunks`: the viewport's grid cells grouped into
  **grid-anchored blocks of 4×4 tiles** (1024 level px), each block's
  read rect = the union of its cells clamped to the level bounds — the
  exact §5 invariant, so the `(level, tx, ty)` cache key and the
  viewport-invariance regression test are untouched. Chunks ordered by
  block-center distance to the viewport center (the center block first).
  A plan whose viewport fits one block is a single chunk — the
  whole-viewport plan byte-for-byte, including the common zoomed-out
  case.
- `fetch_chunk` (the public fetch layer; the render path, §6.6.2, calls
  it once per chunk, center-first) reads one `read_region` per chunk —
  a chunk is skipped when every tile in it is cached; otherwise one
  `read_region` for the block, crop, cache — the §5.1 "one big read,
  crop in Python" strategy, just smaller and ordered. ~4 chunks for a
  1280×720 canvas; the extra decode setups are cheap against a single
  viewport-sized read.
- **Per-chunk push** (this is what makes center-first visible):
  `_render_once` pushes `tiles`/`tile_geo` **once per chunk**, in the
  plan's viewport-center-first order, each chunk after its own
  `read_region` + encode lands — the center chunk is on screen without
  waiting for the rest of the pass, and it is not re-sent in a
  follow-up push (a single-chunk plan pushes exactly once). The trait
  *semantics* are the **last chunk pushed** (last-event slots, like
  `last_polygon` / `annotation_edit`): the JS view accumulates a
  render's pushes (its image cache is a merge, not a replace), so after
  a render the view holds the full viewport while the traits hold the
  last chunk. Re-attach seeding stays correct because the JS view bumps
  a `resync` counter (JS→Py last-event) once per attach and the Python
  observer re-renders the current viewport — needed because the
  fit-echo alone can be a no-op (traitlets fires no observer for a
  value-equal viewport set). A partial push from a superseded viewport
  is harmless: the JS side only merges, and the render loop's
  coalescing re-plans from the latest viewport. Comm cost: *less* than
  the old two-stage push — N chunk-sets instead of N+1 (the center chunk
  is no longer re-sent), and the same for single-chunk plans (one push
  either way). **Display after construction.** Slide opening is
  synchronous in the constructor, so by the time `display(v)` runs the
  widget's state is complete (open slide, fit viewport, initial tiles):
  the first comm state carries the full snapshot, and the view attaches
  to a consistent, already-open widget.

#### 6.6.3 Out of scope

Animated pan/zoom (springs — wheel/drag/minimap/toolbar and programmatic
`center_on`/`set_zoom` apply instantly; the cross-fade is the only
animation), WebGL compositing, off-screen prefetch (the LRU covers
back-pans), per-tile (sub-level) fades, parallel fetch threads, touch
pinch, keyboard navigation, OSD-style reference strip. Unchanged: the 120
ms debounce, `read_crop`, the Python API, `TileCache` (count budget,
keying), the OpenSlide backend.

## 7. Python API

```python
from islide import SlideViewer

v = SlideViewer("sample.svs")   # opens the slide synchronously
                               # (the constructor blocks until open,
                               #  and raises on open failure)
display(v)                     # display right after construction: the
                               # widget's first comm state already carries
                               # the full state (open slide, fit viewport,
                               # initial tiles)

# programmatic navigation (slide coords = level-0 px)
v.center_on(120_000, 80_000)
v.set_zoom(2.0)                # clamped to [fit/4, 16]
v.set_zoom(1.0, cx=50_000, cy=40_000)   # zoom + center in one call
bbox = v.viewport_bbox()       # (x0, y0, x1, y1) in slide coords, clamped

# data out
img = v.read_crop(bbox)        # PIL Image at level 0 (RGBA)

# full-slide overlay (e.g. a model heatmap PNG at the slide's
# get_thumbnail scale) — over the tiles, under the annotations
v.set_overlay("heatmap.png", alpha=0.6, transparent=(0, 0, 0))
v.clear_overlay()

# annotations (level-0 slide px; str / Path or a parsed dict)
v.set_annotations("roi.geojson")
v.clear_annotations()
v.annotations                  # the canonical document — the GeoJSON export itself (§6.3)

# drawing: key A in the view, left-click the vertices, key A again
# saves — the polygon lands in v.annotations (no programmatic draw API)

# annotation editing — the same ops the view's del/label/color buttons
# and vertex drag/insert issue; ids from v.annotations
v.delete_annotation("a3")
v.set_annotation_label("a3", "tumor")     # None / "" clears the label
v.set_annotation_color("a3", color="red", fill=None)  # None = default
v.set_annotation_vertex("a3", 0, 100.0, 200.0)   # move flat position 0
v.add_annotation_vertex("a3", 1, 50.0, 80.0)     # insert at flat segment 1
# (x, y level-0 px; flat indices = canonical position/segment order,
#  docs/annotations.md)

v.close()                      # joins open thread, closes the slide handle
```

Widget identity (must match the JS module, §6.1.1):
`_model_name="SlideModel"`, `_view_name="SlideView"`,
`_model_module="jupyter-islide"`, `_model_module_version="2.1.0"`. The
module version is the **wire** version — bumped when the trait contract
changes (the canonical annotation document, §6.3), independent of the
package version.

Traits (the comm contract; synced names are guarded by a cross-language
test — Python trait set == `frontend/defaults.js` keys):

| Trait | Dir | Type | Notes |
|---|---|---|---|
| `slide_open` | Py→JS | bool | always `True` once the widget is constructed (open is synchronous in the constructor; a failed open raises before the widget exists) |
| `meta` | Py→JS | dict | `{dimensions, level_count, level_downsamples, level_dimensions, mpp, vendor}` |
| `viewport` | JS⇄Py | dict | `{cx, cy, zoom, canvas_w}` (level-0 center + zoom + canvas width; the canvas height is not in the wire form — see `canvas_h`) |
| `canvas_h` | Py→JS | int | on-screen viewport height (CSS px), user-settable at construction or runtime; the JS view applies it to the canvas, and the Python observer re-plans the current viewport at the new height (never crosses the wire) |
| `image_cache_max` | Py→JS | int | the single decoded-tile cache cap (tile count, default 1000): the JS view's decoded image cache *and* the kernel `TileCache` (both LRU, same keys); user-settable at construction or runtime; a decrease re-limits both caches |
| `tiles` | Py→JS | dict | `{"level:tx:ty": dataURL}` — last-event slot: a render pushes one chunk at a time (viewport-center-first, §6.6.2), so the trait holds the **last chunk** while the JS view accumulates the render's pushes (merges, never evicts per push) into the full viewport |
| `tile_geo` | Py→JS | dict | `{"level:tx:ty": [level, ox, oy, cw, ch]}` — absolute level-pixel crop origin + size; the view reprojects this under its local transform |
| `minimap_img` | Py→JS | dataURL | whole-slide overview (top-level JPEG), set once |
| `overlay_img` | Py→JS | dataURL | full-slide overlay image as a PNG data URL (`""` = none); drawn by the compositor over the tiles, under the annotations |
| `overlay_alpha` | Py→JS | float | overlay opacity in [0, 1], default 0.5 |
| `annotations` | Py→JS | dict | the canonical annotation document — a restricted GeoJSON `FeatureCollection` in level-0 px (§6.3); imports, drawn polygons, and edits all land here |
| `last_polygon` | JS→Py | list | the just-saved drawn polygon — bare open ring `[[x, y], …]`, level-0 px (`null` = none yet); Python validates and appends a `Polygon` feature to `annotations` |
| `annotation_edit` | JS→Py | dict | the last issued edit command — `{op: "delete" \| "set_label" \| "set_color" \| "set_vertex" \| "add_vertex", id, …}` (`null` = none yet); Python applies it with `apply_edit` and pushes the updated `annotations` (§6.5) |
| `resync` | JS→Py | int | attach re-render counter (last-event, like `last_polygon`): the view bumps it once per attach; Python re-renders the current viewport — the full-set re-push against the partial attach seed (§6.6.2) |
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

`OpenSlideBackend` implements this over `openslide.open_slide(path)`. A
future `HTTPBackend` (openslide-server) or `S3Backend` slots in without
touching the widget.

`OpenSlideBackend.from_object(slide)` wraps an *already-opened* object
that duck-types the openslide OO API surface (`properties`, `dimensions`,
`level_count`, `level_downsamples`, `level_dimensions`, `read_region`,
`get_thumbnail`, `close`) — the seam for custom slide libraries that
mirror openslide's API over other slide types (no `openslide` import
needed; the backend closes the wrapped object). The viewer exposes it as
a keyword-only `slide=` constructor argument, mutually exclusive with the
path.

## 8. Verified OpenSlide / openslide-python API notes

The contract `islide/backend.py` relies on, verified against
openslide-python 1.4.6 / libopenslide 4.0.1 (properties-not-callables,
`read_region` out-of-bounds and location-anchor behavior, why
`get_best_level_for_downsample` is not used, …): see
[docs/openslide-api.md](docs/openslide-api.md).

## 9. Performance budget (targets)

| Metric | Target |
|---|---|
| Time to first pixel (local file, after kernel import) | < 2 s on a typical SVS (open is synchronous in the constructor; minimap first) |
| Pan responsiveness (cached region) | ≤ 1 frame of perceived lag; tiles already on JS side |
| Pan into uncached region | one `read_region` pass; ≤ ~300 ms for a 1280×720 canvas at a mid level on a local NVMe |
| Zoom into uncached region | viewport-center chunk first: the center visible within one 1024 px-block read + round trip (≤ ~200 ms); each remaining chunk is pushed as its own read lands, in the same render pass |
| Tile transfer per full viewport | ≤ ~400 KB (≈ 20 tiles × 20 KB JPEG) |
| Steady-state memory (cache on) | ≤ ~256 MB per side, worst case (`image_cache_max` 1000 tiles × ≤ 256 KB decoded) + slide handle overhead |
| Idle CPU | 0 (no polling; everything is trait-driven) |

Explicit non-goals: no prefetching of off-screen tiles (the LRU cache
makes back-pans free, which covers 90 % of the benefit); no
multi-threaded fetch pass.

## 10. Testing

Two suites, no browser required — Python (`python -m pytest tests/`)
and JS (`cd frontend && node --test test/`). Because the widget is a
plain Python object until displayed, the whole Python-side state machine
is tested headless, and the JS pure modules are tested against mock 2D
contexts. Full per-file coverage table, the test slide sourcing, and the
cross-language trait-contract guard:
[docs/testing.md](docs/testing.md).

Two notes that stay here:

- The integration path runs against a real slide (`data/CMU-1.tiff`,
  downloaded from openslide-testdata at test time, not committed).
  Redistributable example slides for docs remain to be found — the
  package itself is MIT-licensed (repo `LICENSE`), but a small WSI suitable
  for docs/examples needs its own open license.
- The DOM/event wiring in `view.js` is thin over the pure modules and is
  verified by the manual matrix (classic nb / lab / ×2 DPR) when a
  browser is available.

## 11. Packaging & Dependencies

Layout, the version-sync rules (package version vs. the 2.1.0 wire
version), the dependencies (Python / libopenslide / JS), and the release
mechanism — `hatchling` + the `hatch-jupyter-builder` hook builds the
extension at wheel-build time so a single `pip install jupyter-islide`
carries both the Python API and the pre-built JupyterLab extension:
[docs/packaging.md](docs/packaging.md).
