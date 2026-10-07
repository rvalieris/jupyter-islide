# jupyter-islide

Interactive whole-slide pathology image viewer for Jupyter, backed by
[OpenSlide](https://openslide.org/).

## Install

```bash
pip install jupyter-islide
```

The package needs the `libopenslide` shared library at runtime. Install it
any one of these ways:

- **PyPI**: `pip install openslide-bin`, which bundles `libopenslide`
- **conda**: `mamba install -c conda-forge openslide`, openslide-python from conda also brings openslide automatically
- **apt**: `libopenslide0`

The release wheel **bundles the pre-built JupyterLab widget extension**,
so a single `pip install` gives you both the Python API and the interactive widget.
The extension is installed to `share/jupyter/labextensions/jupyter-islide/`,
which JupyterLab 4 discovers automatically.

## Quickstart

```python
from islide import SlideViewer

v = SlideViewer("data/CMU-1.tiff", canvas_h=700)   # create the widget with the given height
display(v)                                         # display the widget

# useful methods
v.center_on(23000, 16457)    # level-0 slide coordinates
v.set_zoom(2.0)
v.viewport_bbox()           # current view in level-0 px
v.read_crop(v.viewport_bbox())  # PIL image of the current view
v.canvas_h = 900            # resize the viewport height
```

## Annotations

Import a GeoJSON document and render on an overlay canvas,
with per-feature `color`/`fill`/`label` properties:

```python
v.set_annotations("roi.geojson")   # str / Path, or a parsed dict
v.annotations      # canonical document (GeoJSON FeatureCollection, level-0 px)
v.clear_annotations()
```

## Heatmap overlay

Show a full-slide image, e.g. a model's heatmap rendered at the slide's `get_thumbnail` scale,
over the tiles and under the annotations, with optional transparency:

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

## Drawing annotations

You can create new or edit existing annotations by hand:

Press **A** (crosshair) to anter annotation mode, **left-click** the vertices, 
then press **A** to save or **Esc** to cancel. Annotations are also saved in geojson:

```python
v.annotations    # the new polygon is appended here

v.delete_annotation("a1") # remove annotation
v.set_annotation_label("a1", "tumor")              # set label
v.set_annotation_color("a1", color="red", fill=None)  # set colors
```

## Examples

See `examples/viewer_demo.ipynb` (canvas, programmatic control) or
`examples/annotations_demo.ipynb` (GeoJSON import, polygon drawing, editing) for a walkthrough.

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

