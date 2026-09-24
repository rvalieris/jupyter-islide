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
- Programmatic control: `center_on(x, y, mpp=…)`, viewport queries,
  click callbacks in slide coordinates.
- Read-only annotation overlay (view existing shapes/points; export current
  viewport coordinates).
- Stay in the kernel: no external server, no tile pyramid to pre-generate.

### Non-goals (v1)

- Writing/editing annotations on the slide (UI for creating polygons etc. —
  view + point selection only).
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
    [−] [+] [fit] [1:1]  zoom readout "0.25× · 0.5 µm/px"  status
  </div>
</div>
```

A second `#annotations` canvas lands with M2.

### 6.1.1 Package layout & registration (implemented)

```
frontend/
  tilemath.js    pure math: fitZoom, zoomAtCursor, panTransform,
                 tileScreenRect, visibleTiles, viewportL0Bbox, …
  compositor.js  drawScene(ctx, {transform, meta, tileGeo, images}) —
                 the only place pixels are drawn; pure over a ctx
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
| left drag (pointer events) | pan | M1 ✅ |
| double-click | zoom in ×2 at cursor | M1 ✅ |
| minimap click/drag | center viewport on that point | M1 ✅ |
| toolbar | −/+, fit slide, 1:1 | M1 ✅ |
| right-drag or shift+drag | rubber-band region select (Python callback with slide-coord rect) | M2 |
| click | point callback (slide coords) | M2 |

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

Click/region callbacks are also trait updates (`last_click`,
`last_region` — reserved now, wired in M2), so the same round-trip covers
everything and Python can replay state after a kernel restart of the widget.

Canvas is DPR-aware: backing store is `clientSize × devicePixelRatio`,
drawing is in CSS pixels (`setTransform(dpr, 0, 0, dpr, 0, 0)`). Resizes go
through a `ResizeObserver`, which updates the transform's canvas size and
re-syncs.

### 6.3 Annotations layer

- Shape model (Python-owned, synced in both directions):
  `{id, kind: point|rect|polygon, points: [(x,y), …], label, color}` in
  slide coordinates.
- Rendered as a canvas overlay; on viewport change the JS re-draws from the
  synced shapes list (cheap: N strokes). No annotation edits in v1 except
  adding a point on click (toggle "annotate" mode).
- Import in v1: GeoJSON (point/polygon) — the de-facto WSI annotation format.

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

# M2 (reserved): event hooks, annotations
# v.on_click.connect(lambda p: print("clicked", p))
# v.on_region.connect(lambda rect: v.read_crop(rect))
# v.set_annotations(geojson="roi.geojson")

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
| `tiles` | Py→JS | dict | `{"level:tx:ty": dataURL}` — replaced wholesale per push |
| `tile_geo` | Py→JS | dict | `{"level:tx:ty": [level, ox, oy, cw, ch]}` — absolute level-pixel crop origin + size; the view reprojects this under its local transform |
| `minimap_img` | Py→JS | dataURL | whole-slide overview (top-level JPEG), set once |
| `last_click` / `last_region` | JS→Py | dict | reserved for M2 callbacks |
| `status` | Py→JS | str | status line (open progress / error / live zoom·level·tile info) |

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
| M1 ✅ | **Interactive JS view** *(done)*. Canvas compositor (`compositor.js` + pure `tilemath.js`), wheel/drag/dblclick pan-zoom at the cursor, minimap with viewport rect, −/+/fit/1:1 toolbar, DPR-aware canvas, `ResizeObserver` resize. Trait contract per §7; viewport sync is JS-written + debounced (120 ms) + coalesced onto a Python background render thread. M0's HTML viewer is kept as the standalone class `HtmlSlideViewer` (no-extension fallback / reference pipeline), and the Python-side `SlideViewer` gained the background open + `wait()` + programmatic viewport API the JS view drives. **Not yet covered:** in-browser verification (no browser in the dev sandbox — the view is verified by node unit tests of the pure math/compositor + headless widget tests; the extension build path is documented, see §11), and the M2 mouse callbacks. | `examples/m1_demo.ipynb` (canvas) + smooth pan/zoom |
| M2 | **Annotations.** Canvas overlay layer, GeoJSON import, click-to-add point, `last_region` rubber-band select, callbacks. | `v.on_region` + GeoJSON overlay example |
| M3 | **Polish & ship.** Docs (README + docsite), example slides in docs, perf pass (DPR-aware canvas, HiDPI crispness), PyPI release `jupyter-islide`, `pip install jupyter-islide[dev]`, CI. | published package |

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
│   ├── viewport.py           # SlideMeta + Viewport (pure)
│   ├── cache.py              # TileCache
│   ├── fetch.py              # plan -> tiles (cache + one read, cropped)
│   └── encode.py             # tile -> JPEG data URL
└── frontend/                 # JS canvas view (npm: jupyter-islide)
    ├── tilemath.js  compositor.js  model.js  view.js
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
