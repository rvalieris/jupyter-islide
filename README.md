# islide

Interactive whole-slide pathology image viewer for Jupyter, backed by
[OpenSlide](https://openslide.org/).

Status: **M0** — see [DESIGN.md](DESIGN.md). Toolbar/slider-driven HTML tile
viewer (no mouse yet; mouse + canvas compositor arrive with the M1 JS view).

## Install

Requires the `libopenslide` system library (conda:
`mamba install -c conda-forge openslide`, or apt `libopenslide0`), then:

```bash
pip install -e .
```

## Quickstart

```python
from islide import SlideViewer

v = SlideViewer("data/testslide.tiff")
display(v)

v.center_on(18691, 36611)   # level-0 slide coordinates
v.set_zoom(2.0)
v.viewport_bbox()           # current view in level-0 px
```

Run `examples/m0_demo.ipynb` for a walkthrough.

## Layout

```
islide/
  viewport.py  SlideMeta + Viewport (pure)
  plan.py      viewport -> one region read, sliced into tiles (pure)
  cache.py     byte-budgeted LRU tile cache
  backend.py   SlideBackend protocol + OpenSlideBackend
  fetch.py     plan -> tiles (cache lookups + one read, cropped)
  encode.py    tile -> JPEG data URL
  widget.py    SlideViewer (M0 HTML compositor)

tests/           pytest (pure math runs without openslide)
data/            test slide (whole-slide TIFF, 37382x73222, 8 levels)
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```
