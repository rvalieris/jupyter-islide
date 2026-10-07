# jupyter-islide

Interactive whole-slide pathology image viewer for Jupyter, backed by
[OpenSlide](https://openslide.org/).

## Install

```bash
pip install jupyter-islide
```

The package needs the `libopenslide` shared library at runtime. Install it
any one of these ways:

- **PyPI**: `pip install openslide-bin` — bundles `libopenslide`, no system package
- **conda**: `mamba install -c conda-forge openslide`
- **apt**: `libopenslide0`

The release wheel **bundles the pre-built `frontend/` JupyterLab widget
extension**, so a single `pip install` gives you both the Python API and the
interactive `SlideViewer` — no separate npm/extension step. The extension is
installed to `share/jupyter/labextensions/jupyter-islide/`, which JupyterLab 4
discovers automatically.

### Building the release wheel

Building a wheel compiles `frontend/` into a labextension and embeds it in
the wheel. This needs **Node/npm on the PATH**; `jupyterlab==4.*` is installed
into the isolated build environment automatically by the
`hatch-jupyter-builder` hook (see `pyproject.toml`). From the repo root:

```bash
pip install build          # one-time
python -m build --wheel    # builds frontend/ -> dist/jupyter-islide-<ver>-py3-none-any.whl
```

`pip install .` / installing from a git tag builds the same way under the
hood (pip invokes the same `hatchling` + `jupyter-builder` hook).

### Developing

- **Python** (editable): `pip install -e ".[dev]"`. This also runs the JS
  build via the hook; if Node/JupyterLab are unavailable the build is skipped
  with a warning rather than failing (`optional-editable-build`).
- **JS live reload**: with the package installed editable, run
  `jupyter-builder develop islide` (from the `jupyter-builder` package). It
  links `frontend/labextension/` into JupyterLab's labextensions directory
  and watches it. Edit the JS, then `cd frontend && npm run build` to
  rebuild; JupyterLab hot-reloads. Pass `--prefix <jupyterlab prefix>` if
  JupyterLab is not in the default location.
- **Pure JS tests** (no browser): `cd frontend && npm install && npm test`.

## Quickstart

```python
from islide import SlideViewer

v = SlideViewer("data/CMU-1.tiff")   # opens the slide (synchronous)
display(v)                           # canvas view (needs the JS extension)

v.center_on(23000, 16457)    # level-0 slide coordinates
v.set_zoom(2.0)
v.viewport_bbox()           # current view in level-0 px
v.read_crop(v.viewport_bbox())  # PIL image of the current view

v.canvas_h = 900            # resize the viewport height (CSS px);
                            # also a constructor arg: SlideViewer(path, canvas_h=900)
```

Other constructor args: `canvas_w`, `tile_size`, `image_cache_max` and
`jpeg_quality` (tile JPEG quality, 1–95, default 85 — the minimap and the
`read_crop` path are unaffected). `image_cache_max` (default 1000 tiles;
set at construction or at runtime via `v.image_cache_max = N`) is the
single cap on the tile image caches on both sides: the view's decoded
images *and* the kernel-side `TileCache` (encoded JPEG data URLs; both
LRU over the same tile keys, in tile count) — a runtime decrease evicts
on both sides immediately.

**Annotations.** Import a GeoJSON document — a file path or
a parsed dict: a `FeatureCollection` of `{id, geometry, properties}`
features (`Point`, `MultiPoint`, `LineString`, `Polygon`, `MultiPolygon`; a
single `Feature` or a bare geometry is accepted too). The synced
`annotations` trait holds the **canonical document** (docs/annotations.md):
a
plain-JSON `FeatureCollection` in level-0 px — rings open, coordinates
finite floats, ids assigned, `properties` kept whole. Features render on
an overlay canvas with per-feature `color`/`fill`/`label` properties and a
toolbar alpha slider; coordinates are level-0 slide px:

```python
v.set_annotations("roi.geojson")   # str / Path, or a parsed dict
v.annotations      # canonical document (GeoJSON FeatureCollection, level-0 px)
v.clear_annotations()
```

**Overlay (heatmap).** Show a full-slide image — e.g. a model's heatmap
rendered at the slide's `get_thumbnail` scale — over the tiles and under
the annotations, with transparency:

```python
heat = model.predict(slide.get_thumbnail((256, -1)))  # RGBA, slide aspect (-1 = auto)
v.set_overlay(heat)                  # or a PNG file path
v.overlay_alpha = 0.3                # opacity (0–1), or set_overlay(..., alpha=0.3)
v.set_overlay(heat, transparent=(0, 0, 0))  # pure-black pixels -> transparent (the default)
v.set_overlay(heat, transparent=None)       # keep the image's own alpha channel
v.clear_overlay()
```

The image is stretched over the whole slide, so its aspect ratio must
match the slide's (a `get_thumbnail` output matches up to rounding). By
default pure-black pixels (the typical "no prediction" background of a
model output) become fully transparent; pass `transparent=(r, g, b)` for
another key or `transparent=None` to keep the image's alpha as-is. PNG
transport keeps the alpha channel: a heatmap on a transparent background
(e.g. matplotlib's default colormap) composites cleanly over the slide.

**Drawing.** The canvas view adds polygons by hand: click the canvas
(for keyboard focus), press **A** (crosshair), **left-click** the
vertices — dragging still pans and wheel zoom stays live, so the draft
tracks the view — then press **A** to save or **Esc** to cancel. On save
the view sends the *open* ring (no redundant closing position), unclamped
level-0 px, as `last_polygon`; Python normalizes it with the same shared
ring helper the importer uses (≥ 3 points, nonzero area — degenerate
drafts are discarded with a warning, `annotations` untouched) and appends
a `Polygon` *feature* to the document. Drawn features work everywhere
imported ones do — e.g. `read_crop()` of the tissue under one:

```python
v.annotations["features"]    # the appended polygon (fresh id, empty properties)
ring = v.annotations["features"][-1]["geometry"]["coordinates"][0]
```

**Measurement.** The **ruler** button in the toolbar (the underlined
"r" hints the **R** alias; highlighted while active) switches to ruler
mode: **left-drag** on the canvas draws a line whose length is labeled
at its midpoint in micrometers and level-0 pixels (`200 µm · 400 px`; the
µm come from the slide's mpp metadata, so slides without one show
pixels only). The measurement is view-local — a new drag replaces the
current line, a still **left click** clears it, **Esc** (or the button
again) exits the mode, and wheel/minimap/pan keep the line reprojected
under the live view. It never touches the annotation document.

**Editing.** Existing shapes — imported or drawn — are editable in
the view: in the idle mode a **left click** selects a shape (thick accent
highlight; a miss deselects; pan/zoom/minimap keep the selection), and
while a shape is selected the toolbar enables **del** (remove the shape),
**label** (inline input — Enter or blur commits, empty clears the label,
Esc cancels; labels render on lines/polygons too, at the first vertex) and
**color** (stroke + fill pickers with a clear-fill checkbox; closing a
picker commits the full `(color, fill)` pair). The **annotate** button
(the underlined "a" in its label hints the **A** alias) toggles the drawing mode and is highlighted while the mode is active;
entering drawing mode keeps the selection (a selected feature's vertices
stay grabbable while drawing). The view issues each edit as an
`annotation_edit` command; Python applies it to the canonical document and
pushes the whole updated set back, so `v.annotations` always reflects the
edits. The same ops are available programmatically (ids from
`v.annotations`):

```python
v.delete_annotation("a1")
v.set_annotation_label("a1", "tumor")              # None / "" clears
v.set_annotation_color("a1", color="red", fill=None)  # None = default
```

Unknown or stale ids are no-ops: the document is untouched and the status
says `edit ignored:`.

**Vertex editing.** Individual vertices are draggable: the
in-progress draft's vertices while a polygon is being drawn (drawing
mode), and a selected saved feature's vertices in either mode. Each
vertex draws as a small white circle; a **left click** near a circle
grabs the nearest one (within 8 px), and dragging moves the vertex —
never the view (a still click on a vertex is a no-op). Dragging a saved
feature's vertex updates the shape immediately (optimistic preview); on
release the view commits the move as an `annotation_edit` command
`set_vertex` (level-0 px). Python re-validates and pushes the updated
document, so `v.annotations` always reflects the move; a move that would
degenerate the geometry (a ring collapsing onto a point, a hole
closing up, a line flattening) is refused — the status says
`edit ignored: …` and the shape stays as it was. Same programmatic API
(ids and indices from `v.annotations`):

```python
v.set_annotation_vertex("a1", 0, 1234, 567)   # move vertex #0 (level-0 px)
```

`index` is the feature's flat position index: `Point` has index 0;
`MultiPoint` / `LineString` use coordinate order; `Polygon` uses ring
order, then position within the ring; `MultiPolygon` uses island order,
then ring order, then position within the ring.

New vertices can also be **inserted** (drawing mode): with a saved
feature selected, a **left click** within 320 px of one of its edges adds
a vertex at the click position on the nearest segment (the shape
bulges toward the click — outward or inward) — as the cursor nears an
edge, a dashed preview of the two edges the click would create, with
the new vertex's handle at its spot, shows what will happen. The click is
committed as an `annotation_edit` command `add_vertex`, whose `index`
is the *flat segment index* — the same walk, a segment instead of a
position (a ring's last segment is its closing edge). Python re-validates
the same way: an insert on top of an existing vertex or one that would
degenerate the geometry is refused (`edit ignored: …`):

```python
v.add_annotation_vertex("a1", 0, 1234, 567)   # insert into segment #0 (level-0 px)
```

The viewer still has no per-shape move/resize, no multi-select, no
hover/tooltips, and no undo — the reset paths are `clear_annotations()`
or a re-import.

**Custom slide types.** If your slide library exposes the same API as
openslide (openslide-python's object-oriented API), pass an already-opened
slide object instead of a path — exactly one of the two:

```python
v = SlideViewer(slide=my_library.open_slide("my/other/slide.type"))
```

The viewer takes ownership of the object and closes it on `close()`.

The mouse does the rest: **scroll** zooms at the cursor, **drag** pans,
and the **minimap** jumps the view. Pan/zoom is applied instantly in the
view (a local transform) and the new viewport is synced back to Python
(debounced), which fetches any missing tiles.

Run `examples/viewer_demo.ipynb` (canvas, programmatic control, and the
rendering feel: center-first chunks + level cross-fade) or
`examples/annotations_demo.ipynb` (GeoJSON import, polygon drawing,
select/label/color/delete) for a walkthrough.

## Layout

```
islide/
  viewport.py  SlideMeta + Viewport (pure)
  plan.py      viewport -> read plan: one region read, sliced into tiles,
               chunked into grid-anchored 1024-px blocks, center-first
  annotations.py  GeoJSON document -> canonical annotation document (pure)
                 + apply_edit (the pure delete/set_label/set_color/
                              set_vertex/add_vertex ops)
  cache.py     count-budgeted LRU tile cache
  backend.py   SlideBackend protocol + OpenSlideBackend
  fetch.py     plan -> tiles (cache lookups + one read per chunk, cropped)
  encode.py    tile -> JPEG data URL
  widget.py    SlideViewer (custom widget: tiles, overlay, annotations)

frontend/      JS canvas view (JupyterLab extension; model + view + tests)
  tilemath.js     pure viewport/tile math (+ selected-level mirror)
  compositor.js  pure canvas scene drawing (per-level alpha)
  blend.js       level cross-fade math
  annotations.js pure annotation overlay drawing + hit testing
                 + vertex handles / set_vertex preview / segment hit-test
                 + add_vertex preview
  polydraw.js    polygon draw state machine + draft preview
  ruler.js       ruler measurement state machine + measure-line rendering
  model.js       SlideModel
  view.js        SlideView (canvas, mouse, minimap, toolbar)
  labextension.js  widget-registry registration
  labextension/  (build output, gitignored — the compiled labextension)

docs/            DESIGN.md (the design document) + annotations.md,
                 testing.md, packaging.md, openslide-api.md
tests/           pytest (pure math runs without openslide)
data/            test slides (local, gitignored — the test suite downloads
                 CMU-1.tiff from openslide-testdata)
```

## Tests

Python (pipeline + widget, headless):

```bash
pip install -e ".[dev]"
pytest
```

Frontend (pure JS math/compositor, no browser needed):

```bash
cd frontend
npm install
npm test
```
