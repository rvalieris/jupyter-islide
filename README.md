# jupyter-islide

Interactive whole-slide pathology image viewer for Jupyter, backed by
[OpenSlide](https://openslide.org/).

Status: **M4** — see [DESIGN.md](DESIGN.md). Interactive canvas viewer
(mouse pan/zoom, minimap, toolbar) backed by a Python tile pipeline, with a
GeoJSON **annotation document** overlay (points, lines, polygons —
level-0 px or microns) that accepts **hand-drawn polygons** (the
**annotate** button or **A**, left-click the vertices, **A** to save) and
supports **editing existing shapes**: click to select, then **del** /
**label** / **color** in the toolbar (or the same Python API). A
pure-ipywidgets HTML viewer (`HtmlSlideViewer`) is kept as the no-extension
fallback and as the reference pipeline.

## Install

Requires the `libopenslide` system library (conda:
`mamba install -c conda-forge openslide`, or apt `libopenslide0`), then:

```bash
pip install .          # from this repo (or: pip install jupyter-islide, once published)
```

The release wheel **bundles the pre-built `frontend/` JupyterLab widget
extension**, so a single `pip install` gives you both the Python API and the
interactive `SlideViewer` — no separate npm/extension step. The extension is
installed to `share/jupyter/labextensions/jupyter-islide/`, which JupyterLab 4
discovers automatically. Without the JS extension (e.g. a host that doesn't
load labextensions), use `HtmlSlideViewer` — identical pipeline, no custom JS.

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

v = SlideViewer("data/testslide.tiff")   # opens in the background
v.wait()                                 # block until ready
display(v)                              # canvas view (needs the JS extension)

v.center_on(18691, 36611)   # level-0 slide coordinates
v.set_zoom(2.0)
v.viewport_bbox()           # current view in level-0 px
v.read_crop(v.viewport_bbox())  # PIL image of the current view

v.canvas_h = 900            # resize the viewport height (CSS px);
                            # also a constructor arg: SlideViewer(path, canvas_h=900)
```

Other constructor args: `canvas_w`, `tile_size`, `cache_max_mb` and
`jpeg_quality` (tile JPEG quality, 1–95, default 85 — the minimap and the
`read_crop` path are unaffected).

**Annotations (M2, read-only).** Import a GeoJSON document — a file path or
a parsed dict: a `FeatureCollection` of `{id, geometry, properties}`
features (`Point`, `MultiPoint`, `LineString`, `Polygon`, `MultiPolygon`; a
single `Feature` or a bare geometry is accepted too). The synced
`annotations` trait holds the **canonical document** (DESIGN.md §6.3): a
plain-JSON `FeatureCollection` in level-0 px — rings open, coordinates
finite floats, ids assigned, `properties` kept whole. Features render on
an overlay canvas with per-feature `color`/`fill`/`label` properties and a
toolbar alpha slider; coordinates are level-0 px by default or microns
(`units="um"`, requires the slide's mpp):

```python
v.set_annotations("roi.geojson")
v.set_annotations(doc, units="um")
v.annotations      # canonical document (GeoJSON FeatureCollection, level-0 px)
v.clear_annotations()
```

**Drawing (M3).** The canvas view adds polygons by hand: click the canvas
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

**Editing (M4).** Existing shapes — imported or drawn — are editable in
the view: in the idle mode a **left click** selects a shape (thick accent
highlight; a miss deselects; pan/zoom/minimap keep the selection), and
while a shape is selected the toolbar enables **del** (remove the shape),
**label** (inline input — Enter or blur commits, empty clears the label,
Esc cancels; labels render on lines/polygons too, at the first vertex) and
**color** (stroke + fill pickers with a clear-fill checkbox; closing a
picker commits the full `(color, fill)` pair). The **annotate** button
toggles the M3 drawing mode (**A** stays the keyboard alias); entering
drawing mode clears the selection. The view issues each edit as an
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
says `edit ignored:`. M4 has no geometry editing (move/resize/vertex), no
multi-select, no hover/tooltips, and no undo — the reset paths are
`clear_annotations()` or a re-import.

**Custom slide types.** If your slide library exposes the same API as
openslide (openslide-python's object-oriented API), pass an already-opened
slide object instead of a path — exactly one of the two:

```python
v = SlideViewer(slide=my_library.open_slide("my/other/slide.type"))
```

`HtmlSlideViewer` accepts the same `slide=` argument. The viewer takes
ownership of the object and closes it on `close()`.

The mouse does the rest: **scroll** zooms at the cursor, **drag** pans,
and the **minimap** jumps the view. Pan/zoom is applied instantly in the
view (a local transform) and the new viewport is synced back to Python
(debounced), which fetches any missing tiles.

No-custom-JS fallback (M0 HTML tile composite, toolbar/slider driven):

```python
from islide import HtmlSlideViewer
v = HtmlSlideViewer("data/testslide.tiff")
display(v)
```

Run `examples/m4_demo.ipynb` (editing), `examples/m3_demo.ipynb`
(drawing), `examples/m2_demo.ipynb` (annotations), `examples/m1_demo.ipynb`
(canvas), or `examples/m0_demo.ipynb` (HTML) for a walkthrough.

## Layout

```
islide/
  viewport.py  SlideMeta + Viewport (pure)
  plan.py      viewport -> one region read, sliced into tiles (pure)
  annotations.py  GeoJSON document -> canonical annotation document (pure)
                 + apply_edit (M4: the pure delete/set_label/set_color ops)
  cache.py     byte-budgeted LRU tile cache
  backend.py   SlideBackend protocol + OpenSlideBackend
  fetch.py     plan -> tiles (cache lookups + one read, cropped)
  encode.py    tile -> JPEG data URL
  widget.py    SlideViewer (M1 custom widget) + HtmlSlideViewer (M0 fallback)

frontend/      JS canvas view (JupyterLab extension; model + view + tests)
  tilemath.js     pure viewport/tile math
  compositor.js  pure canvas scene drawing
  annotations.js pure annotation overlay drawing + hit testing (M2/M4)
  polydraw.js    polygon draw state machine + draft preview (M3)
  model.js       SlideModel
  view.js        SlideView (canvas, mouse, minimap, toolbar)
  labextension.js  widget-registry registration
  labextension/  (build output, gitignored — the compiled labextension)

tests/           pytest (pure math runs without openslide)
data/            test slide (whole-slide TIFF, 37382x73222, 8 levels)
```

## Tests

Python (pipeline + M0–M4 widget, headless):

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
