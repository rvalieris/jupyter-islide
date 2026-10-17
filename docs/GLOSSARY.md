# jupyter-islide

An interactive whole-slide pathology image viewer in Jupyter: a Python widget that owns
slide access and tile planning, and a notebook-side view that owns drawing. One context
spanning both sides of the wire (`islide/` and `frontend/`).

## Coordinates and levels

**Slide coordinates**:
Integer pixels at level 0 (full resolution), origin top-left, y down. Every public API value
and every annotation is in these.
_Avoid_: slide coords, world coordinates, pixel coordinates

**Level**:
One layer of a slide's pyramid, named by index; its **downsample** is how much smaller that
layer is than level 0.
_Avoid_: zoom level, magnification

**Zoom**:
Screen pixels per level-0 pixel; 1.0 is 1:1 with full resolution. Magnification and µm/px are
derived from it and are not the same quantity.
_Avoid_: magnification, power

## View state

**Viewport**:
The authoritative view state — `(cx, cy, zoom)` in slide coordinates, owned by Python.
_Avoid_: camera, view state

**Transform**:
The Viewport as the view holds it locally: the same geometry plus the canvas size.
_Avoid_: viewport (for the JS-local copy)

**Chunk**:
The group of tiles served by one covering read — a grid-anchored 4×4 block of Tile cells,
delivered center-first.
_Avoid_: block, batch, region

**Tile cell**:
The grid unit a single tile belongs to, identified by `(level, tx, ty)`.
_Avoid_: tile key, block

## Whole-slide images

**Overview**:
The low-resolution whole-slide image drawn under the tiles so an uncached region is never
blank.
_Avoid_: minimap (for the underlay), thumbnail

**Minimap**:
The corner locator showing the whole slide with a rectangle for the current Viewport.
_Avoid_: overview (for the locator)

**Overlay**:
A user-supplied whole-slide image — typically a model heatmap — drawn over the tiles and
under the annotations.
_Avoid_: heatmap (as the name of the feature), mask

## Annotations

**Canonical annotation document**:
The one GeoJSON FeatureCollection, in slide coordinates, that Python normalizes and owns;
every other view of the annotations derives from it.
_Avoid_: annotation document, GeoJSON (as the name of the thing)

**Feature**:
One annotated thing in that document, addressed by a stable **annotation id**.
_Avoid_: ROI, region (an ROI is what a Feature may depict, not the entry itself)

**Flat position**:
The single linear order of a Feature's vertices across its rings — how both sides address
one vertex for editing.
_Avoid_: vertex id, handle index

## Between the two sides

**Attach**:
A view binding itself to an already-live widget, seeding from the widget's current state.
_Avoid_: reconnect, re-display

**Wire version**:
The version that pins the trait contract between the Python widget and the view module.
_Avoid_: API version, protocol version

**Module version**:
The name of the trait pair that carries the Wire version.
_Avoid_: package version

**Package version**:
The distribution version, owned by `pyproject.toml` alone.
_Avoid_: module version
