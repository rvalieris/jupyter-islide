# islide — Design Document

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
│  │  islide JS view (small @jupyter-widgets view)        │  │
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
- **Level selection:** for the current zoom pick the highest pyramid level L
  with `downsample[L] >= 1/zoom`
  (i.e. `slide.get_best_level_for_downsample(1.0 / zoom)`). At that level the
  slide is at least as detailed as the screen, so rendering only ever
  down-scales (crisp, cheap). We fetch **one** level per viewport, never a
  level that is lower-res than the screen (which would look blurry).

## 5. Rendering Pipeline

Per viewport change (coalesced to at most one in-flight pass):

1. **Plan.** Viewport in slide coords → screen rect → for level L, the
   covering rectangle in level-L pixels:
   `rect_L = scale(screen_rect, downsample[L])`, clamped to level-L
   dimensions.
2. **Fetch.** One `read_region(location, level=L, size=rect_L.size)` call per
   level (see §5.1), then crop the result into display tiles (e.g. 256 px
   squares) in Python with PIL. Check the tile cache first; only fetch
   missing tiles' rectangles.
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
  kept in `RGBA`). Budgeted by encoded-size estimate (default 256 MB,
  configurable). Eviction is by bytes, not count.
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

A small custom widget view (`islide` npm package, one view, no build deps
beyond `@jupyter-widgets/base`). This is the standard ipywidgets route and
works identically in classic notebooks and JupyterLab. (A prototype mode is
allowed for M0 — see §10 — but the shipped design is the JS view.)

### 6.1 DOM layout

```
<div.islide-view>
  <canvas #image>            ← tiles
  <canvas #annotations>      ← shapes (device-pixel-ratio aware)
  <div #minimap>
     <img #minimap-img>      ← slide overview (top level / thumbnail)
     <canvas #minimap-rect>  ← current-viewport rectangle
  </div>
  <div #toolbar>
     [−] [+] [fit] [1:1]  mpp readout: "0.48 µm/px"   zoom readout
  </div>
</div>
```

### 6.2 Interactions → state

| Input | Effect |
|---|---|
| wheel / trackpad pinch | zoom about the cursor; `zoom` is continuous, clamped to `[1/level_downsamples[-1]·… , max_zoom]` (max_zoom ≈ 2–4× level-0 for pixel peeping) |
| left drag | pan |
| double-click | zoom in one step at cursor |
| right-drag or shift+drag | rubber-band region select (Python callback with slide-coord rect) |
| click | point callback (slide coords) |
| minimap click/drag | center viewport on that point; viewport rect drag = move |
| toolbar | −/+, fit slide to height, 1:1 |

All of these mutate the **viewport trait** (`{cx, cy, zoom}` in slide
coords) — the *only* JS→Python channel for navigation. Python re-plans tiles
and pushes back. Click/region callbacks are also trait updates
(`last_click`, `last_region`), so the same round-trip covers everything and
Python can replay state after a kernel restart of the widget.

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
display(v)                      # renders as soon as the slide is open

# programmatic navigation (slide coords = level-0 px)
v.center_on(120_000, 80_000, mpp=0.25)
v.center_on(120_000, 80_000, zoom=2)          # alternative units
viewport = v.viewport          # Viewport(cx, cy, zoom, mpp_per_px)
bbox = v.viewport_bbox()       # (x0, y0, x1, y1) in slide coords

# event hooks (slide coordinates, always)
v.on_click.connect(lambda p: print("clicked", p))
v.on_region.connect(lambda rect: crop = v.read_crop(rect))

# data out
img = v.read_crop((x0, y0, x1, y1))     # PIL Image at level 0 (RGBA)
v.set_annotations(geojson="roi.geojson")
```

Traits (the comm contract):

| Trait | Dir | Type | Notes |
|---|---|---|---|
| `path` | Py | str | slide file |
| `slide_open` | Py→JS | bool | disables toolbar until open |
| `meta` | Py→JS | dict | dims, levels, ds factors, mpp, vendor |
| `viewport` | JS⇄Py | dict | `{cx, cy, zoom}` |
| `tiles` | Py→JS | dict | `{key: dataURL}` — replaced wholesale per push |
| `annotations` | Py⇄JS | list | shape model |
| `last_click` / `last_region` | JS→Py | dict | consumed by Python callbacks |
| `minimap_img` | Py→JS | dataURL | set once |

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
  size (pixels at level L). Out-of-bounds regions must be clamped by us.
- `slide.get_best_level_for_downsample(ds) -> int`
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
| M0 | **Spike: static pipeline.** Open slide (bg thread), viewport state in Python, toolbar/sliders (ipywidgets buttons + a zoom `LogSlider`) drive rendering; tiles pushed to an `HTML` widget as stacked `<img>` tags or a single data-URL image. No mouse. Proves: openslide plumbing, level selection, one-read-per-viewport, JPEG over comm, cache. | `display(SlideViewer("sample.svs"))` + zoom buttons |
| M1 | **Interactive JS view.** Canvas compositor, wheel/drag/dblclick pan-zoom, minimap, toolbar, trait contract from §7. Replaces M0's HTML hack (kept as `fallback=True` mode for broken-JS environments? — decide at M1). | smooth pan/zoom in classic nb + lab |
| M2 | **Annotations.** Canvas overlay layer, GeoJSON import, click-to-add point, `last_region` rubber-band select, callbacks. | `v.on_region` + GeoJSON overlay example |
| M3 | **Polish & ship.** Docs (README + docsite), example slides in docs, perf pass (DPR-aware canvas, HiDPI crispness), PyPI release `islide`, `pip install islide[dev]`, CI. | published package |

## 11. Testing

- **Unit (no OpenSlide needed):** tile planning, level selection, viewport
  math, coordinate transforms, LRU cache, trait/callback logic — pure
  functions over a `FakeSlide` (metadata-only stub of `SlideBackend`).
- **Integration (needs libopenslide):** real open/read against synthetic
  slides. Status of synthetic assets (verified during design):
  - A **single-level tiled TIFF** written by `tifffile` (`tile=(256, 256)`)
    opens fine via the generic-TIFF vendor and `read_region` round-trips.
  - A hand-written **multi-page "pyramid" TIFF was detected as 1 level** —
    the generic-TIFF multi-resolution rules are stricter than "smaller
    sub-IFDs". M0 action item: pin down a working synthetic-pyramid recipe
    (or vendor a small public-domain WSI as test data); either way, the
    level-selection logic is unit-tested against recorded
    `(level_count, level_downsamples)` fixtures so this can't block M1.
- **Widget smoke test:** instantiate `SlideViewer`, simulate viewport trait
  changes, assert tile keys/payloads without a browser. JS covered by manual
  matrix (classic nb / lab / ×2 DPR / ×1).

## 12. Packaging & Dependencies

```
islide/
├── pyproject.toml            # hatchling; deps: openslide-python>=1.4,
│                             #   pillow>=9, ipywidgets>=8
├── python/islide/
│   ├── __init__.py           # SlideViewer, Viewport, shapes
│   ├── widget.py             # DOMWidget + traits + state machine
│   ├── backend.py            # SlideBackend protocol, OpenSlideBackend
│   ├── plan.py               # viewport -> read plan (pure)
│   ├── cache.py              # TileCache
│   └── annotations.py        # shape model + GeoJSON in/out
└── js/src/
    ├── index.ts              # widget registration (npm: islide)
    └── view.ts               # canvas compositor + input (no deps)
```

- Python: `openslide-python>=1.4` (uses the OO API; §8 notes), `pillow`,
  `ipywidgets>=8`. System lib: `libopenslide` — conda-forge `openslide`
  package, or `libopenslide0` on Debian/Ubuntu.
- JS: private npm package `islide`, no dependencies beyond
  `@jupyter-widgets/base`; built with the standard jupyterlab extension tooling.

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
