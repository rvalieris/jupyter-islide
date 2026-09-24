# jupyter-islide

Interactive whole-slide pathology image viewer for Jupyter, backed by
[OpenSlide](https://openslide.org/).

Status: **M1** — see [DESIGN.md](DESIGN.md). Interactive canvas viewer
(mouse pan/zoom, minimap, toolbar) backed by a Python tile pipeline. A
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
```

The mouse does the rest: **scroll** zooms at the cursor, **drag** pans,
**double-click** zooms in, and the **minimap** jumps the view. Pan/zoom is
applied instantly in the view (a local transform) and the new viewport is
synced back to Python (debounced), which fetches any missing tiles.

No-custom-JS fallback (M0 HTML tile composite, toolbar/slider driven):

```python
from islide import HtmlSlideViewer
v = HtmlSlideViewer("data/testslide.tiff")
display(v)
```

Run `examples/m1_demo.ipynb` (canvas) or `examples/m0_demo.ipynb` (HTML)
for a walkthrough.

## Layout

```
islide/
  viewport.py  SlideMeta + Viewport (pure)
  plan.py      viewport -> one region read, sliced into tiles (pure)
  cache.py     byte-budgeted LRU tile cache
  backend.py   SlideBackend protocol + OpenSlideBackend
  fetch.py     plan -> tiles (cache lookups + one read, cropped)
  encode.py    tile -> JPEG data URL
  widget.py    SlideViewer (M1 custom widget) + HtmlSlideViewer (M0 fallback)

frontend/      JS canvas view (JupyterLab extension; model + view + tests)
  tilemath.js     pure viewport/tile math
  compositor.js  pure canvas scene drawing
  model.js       SlideModel
  view.js        SlideView (canvas, mouse, minimap, toolbar)
  labextension.js  widget-registry registration
  labextension/  (build output, gitignored — the compiled labextension)

tests/           pytest (pure math runs without openslide)
data/            test slide (whole-slide TIFF, 37382x73222, 8 levels)
```

## Tests

Python (pipeline + M0/M1 widget, headless):

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
