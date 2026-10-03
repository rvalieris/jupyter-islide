# jupyter-islide

Interactive whole-slide pathology image viewer for Jupyter, backed by
[OpenSlide](https://openslide.org/).

Status: **M5** — see [DESIGN.md](DESIGN.md). Interactive canvas viewer
(mouse pan/zoom, minimap, toolbar) backed by a Python tile pipeline. M5
improves *feel*, not features: wide views are read as grid-anchored
1024-px **chunks in center-first order** — the center chunk is pushed
before the rest, so the most important tiles arrive first — and a change
of the selected pyramid level **cross-fades** (the old level fades out
over 300 ms while the new level draws at full opacity). No new traits,
no wire-format change (`tiles` / `tile_geo` are exactly as before). The
canvas view also carries a GeoJSON **annotation document** overlay
(points, lines, polygons — level-0 px or microns) that accepts
**hand-drawn polygons** (the **annotate** button or **A**, left-click the
vertices, **A** to save) and supports **editing existing shapes**: click
to select, then **del** / **label** / **color** in the toolbar (or the
same Python API).

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

v = SlideViewer("data/CMU-1.tiff")     # opens in the background
v.wait()                                 # block until ready
display(v)                              # canvas view (needs the JS extension)

v.center_on(23000, 16457)    # level-0 slide coordinates
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
               chunked into grid-anchored 1024-px blocks, center-first (M5)
  annotations.py  GeoJSON document -> canonical annotation document (pure)
                 + apply_edit (M4: the pure delete/set_label/set_color ops)
  cache.py     byte-budgeted LRU tile cache
  backend.py   SlideBackend protocol + OpenSlideBackend
  fetch.py     plan -> tiles (cache lookups + one read per chunk, cropped)
  encode.py    tile -> JPEG data URL
  widget.py    SlideViewer (custom widget: tiles, overlay, annotations)

frontend/      JS canvas view (JupyterLab extension; model + view + tests)
  tilemath.js     pure viewport/tile math (+ selected-level mirror)
  compositor.js  pure canvas scene drawing (per-level alpha, M5)
  blend.js       level cross-fade math (M5)
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

Python (pipeline + M1–M5 widget, headless):

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
