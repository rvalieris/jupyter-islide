# Annotation subsystem

Annotations are GeoJSON. `v.annotations` is the synced **canonical document**:
a plain-JSON GeoJSON `FeatureCollection` of `{id, geometry, properties}`
features in level-0 slide px. An imported shape, a hand-drawn polygon, and an
edited shape are all the same kind of feature — one document is the wire
format, the display source, and the export (`json.dump(v.annotations)` is the
export).

This document covers the canonical document, import, rendering, polygon
drawing, editing (select / delete / label / color), and vertex editing (drag /
insert). The synced traits these behaviors ride on are summarized in
[DESIGN.md §7](DESIGN.md#7-python-api-ipywidgets); the Python API for each
subsystem is at the end of its section here.

## The canonical document

`islide/annotations.py` (pure, over a parsed document) normalizes into the
**canonical annotation document** — the value the `annotations` trait holds,
the value the JS view renders, and the export. There is no second
representation and no inverse conversion; `v.annotations` *is* the document.

### Structure

```
{"type": "FeatureCollection", "features": [Feature, …]}
(the empty set is `{"type": "FeatureCollection", "features": []}` — the
`annotations` trait default)

Feature = {"type": "Feature",
           "id": <string>,
           "geometry": Point | MultiPoint | LineString
                       | Polygon | MultiPolygon,
           "properties": <object>}

Point        coordinates: [x, y]
MultiPoint   coordinates: [[x, y], …]
LineString   coordinates: [[x, y], …]      (≥ 2 positions)
Polygon      coordinates: [ring, …]        (ring 0 = outer, rest = holes)
MultiPolygon coordinates: [[ring, …], …]   (one polygon per island, each
                                             with its own holes)
ring         [[x, y], …]  stored **open**
```

`GeometryCollection` is **not** in the canonical form: it is a recursive
heterogeneous *container*, not a flat geometry. It is accepted at import and
expanded to one feature per member geometry (recursively), each stamped with
the parent feature's `properties`.

### Semantics

- `x, y` are **level-0 slide px** — the same space as the `viewport` trait.
  The display transform lives only in the view ([DESIGN.md §4](DESIGN.md#4-coordinates)).
  A position's third component (`z`) is **dropped** (2D only).
- `Point` = one marker.
- `MultiPoint` = independent markers — **a wire type**: a `MultiPoint` feature
  stays one feature, whole (all of its positions), and each position gets its
  own display / hit-test.
- `LineString` = polyline / path (≥ 2 positions).
- `Polygon` = outer ring + holes (a single island).
- `MultiPolygon` = several **islands, each with its own holes** —
  `coordinates[i]` is polygon `i` (`[outer, hole₀, …]`).
- `properties` is an object, kept whole (all keys, any JSON values, nested
  ok). The renderer reads exactly three keys from it — `label`, `color`,
  `fill` (each a string or absent); everything else passes through
  untouched. `label`/`color`/`fill` are all **optional** — a feature with
  none draws just its geometry (default black stroke, transparent fill).
  The canonical form has **no `None`-valued properties**: a `null` value is
  dropped (absence is the no-value state).
- `id`: a string, **unique across the document**. A feature's own `id`
  (`str`/`int`/`float`, stringified) is kept if present; otherwise a fresh
  `aN` id is assigned.
- **Coordinates outside the slide** are legal; the view culls/clips them.
- **Winding order is not canonical**: a ring is the outer iff its index in
  the polygon is 0, a hole iff index > 0 (GeoJSON leaves winding ambiguous;
  the display treats ring 0 as the outer unconditionally).

### Invariants (canonical form)

- `features` is a list (document order; the display draws back-to-front in
  list order, hit-testing topmost-first in reverse).
- every feature: `type == "Feature"`, a unique string `id`, `geometry` ∈
  {`Point`, `MultiPoint`, `LineString`, `Polygon`, `MultiPolygon`} (no
  `GeometryCollection`, nested or not), `properties` a dict (may be empty).
- all coordinates are **finite** 2D numbers.
- rings are **open** (no redundant closing position) with **≥ 3
  positions**, each `[x, y]`; a `LineString` has ≥ 2 positions.
- every ring is **non-degenerate**: ring area ≥ `1e-6` px² (Shoelace), a
  `LineString` total length ≥ `1e-6` px.

### Import normalization

`v.set_annotations(source)` / the `annotations` trait accept a GeoJSON
**`FeatureCollection`**, a single **`Feature`**, or a **bare geometry**
(any of the six types) — as a file path or an already-parsed `dict`.
`parse_annotations` (pure) normalizes:

| Input | Normalization |
|---|---|
| a bare `Feature` / bare geometry | wrapped in a `FeatureCollection` (a bare geometry gets empty `properties`, no id) |
| a feature without `properties` | `properties = {}` |
| a `null`-valued property key | dropped (the canonical form has no `None` values) |
| a `GeometryCollection` (at any depth) | expanded to one feature per member geometry (recursively), each stamped with the feature's `properties`; an empty GC produces nothing |
| a feature with a non-`None` `z` component | the `z` is dropped, the position kept |
| a `Polygon`/`MultiPolygon` ring (closed) | the redundant closing position is dropped (open form) |
| a **degenerate** ring / line / island | **dropped with a `UserWarning`** (a zero-area ring — including a hole — drops its whole `Polygon`; a `MultiPolygon` loses the degenerate islands; a zero-length `LineString` is dropped; a feature left without geometry is dropped) |
| coordinates outside the slide | kept (culled at display) |

A feature whose `geometry` is `null` is a legal no-op (produces nothing).
Degenerate but well-formed input is *skipped with a warning*; **malformed**
input is a hard `ValueError`:

- document: not a dict / unknown `type` / missing `features` or
  `geometry` or `geometries` or `coordinates` member / non-dict
  `properties` / an FC member that is not a `Feature`;
- ids: a non-string/non-number (or boolean) `id`, a **duplicate** `id`, or a
  `GeometryCollection` feature *with* an `id` that expands into 2+ members
  (the id cannot be shared);
- coordinates: non-numeric or boolean or non-finite values;
- rings with < 3 positions; 1-position `LineString`s;
- a non-string `label`/`color`/`fill` property.

**Fresh-id rule**: ids are `aN` (n = 0, 1, 2, …); a feature without an id
gets the **first `aN` not already used** in the document (scanning from
`a0`). The same rule names a drawn polygon
([Drawing polygons](#drawing-polygons)).

The result — the `annotations` trait value — is the canonical document;
`drawAnnotations`, the hit-test, and `json.dump(v.annotations)` all consume
it directly. The Python importer and the JS renderer share the semantics
(the cross-language contract test in [docs/testing.md](testing.md) pins them
together).

## Wire: the command traits

- **`annotations` (Py → JS, the document)**: on import *and* on any edit
  (a drawn polygon saved, a shape deleted / re-labeled / recolored, a vertex
  moved or inserted) the **whole normalized document** is set on the trait.
  The document is small (a few dozen features) and one value is the display
  source, the wire value, and the export — no partial patches.
- **`last_polygon` (JS → Py, command)**: the draft polygon in progress, as a
  **bare open ring** (a position sequence, level-0 px) — see
  [Drawing polygons](#drawing-polygons).
- **`annotation_edit` (JS → Py, command)**: one edit — see
  [Editing](#editing-select-delete-label-color) and
  [Vertex editing](#vertex-editing-drag-and-insert).

Both command traits are **last-event slots**: they hold the latest command
and are **not cleared** after handling (re-attaching a view resends the
state; `annotation_edit` commands are idempotent over the pushed document,
so a replayed command is a no-op). The *document* itself only ever moves
Py → JS on `annotations`.

## Rendering

The view's second canvas (`#annotations`, over the tile canvas, same
`transform`) draws the document with `drawAnnotations(ctx, transform, meta,
annotations)` — a **pure function over a 2D context** (the JS twin of
`compositor.drawScene`):

- **Culling**: a feature with nothing visible in the viewport is skipped —
  a `LineString` is drawn segment-by-segment (an off-screen segment is
  skipped, an on-screen one clipped to the canvas), a `Polygon` skipped
  entirely when its bbox is off-screen, else drawn whole with canvas
  clipping.
- **Draw order**: `features` list order, back to front (the last feature is
  topmost; hit-testing scans in reverse).
- **Primitives**:
  - `Point` / `MultiPoint` — small filled circles (each position of a
    `MultiPoint` is its own marker) + optional `label` (12px sans, white
    halo) below-right.
  - `LineString` — a stroked open polyline, segments clipped.
  - `Polygon` / `MultiPolygon` — per island: outer ring filled + stroked
    (implicit close), holes subtracted (`evenodd` fill of outer + holes),
    hole rings stroked.
- **`properties` is read, not rewritten**: `label` (drawn if present),
  `color` (stroke; default `#000000`), `fill` (fill; absent → the stroke
  color at 0.2 alpha). A **selected** feature overrides: stroke `#ff8c00`,
  width 3 (default stroke width 1.5, screen px).
- **Alpha**: the `#annotations` canvas element's `opacity` — the toolbar α
  slider (0–1, default 1). The whole annotation layer fades; tiles are
  unaffected. The slider is view-local (not a trait).
- **Redraw**: on `change:transform`, `change:annotations`, `change:viewport`
  (minimap), and during any drag / insert preview (requestAnimationFrame
  throttled).

## Python API (import / export)

```python
v.set_annotations("roi.geojson")     # str / Path (GeoJSON file), or a parsed
                                     # FeatureCollection / Feature / geometry dict
v.clear_annotations()               # -> the empty document
v.annotations                       # the normalized document — the export
```

`set_annotations` returns the canonical document (also stored on the
`annotations` trait) and raises `ValueError` on the malformed inputs above;
degenerate members are skipped with a `UserWarning`. The slide need not be
open.

## Drawing polygons

The view's **left-click polygon-drawing mode** — toggle with the **`A` key**
or the toolbar **annotate** button (one toggle: it enters the mode from
idle, and saves-and-exits from the mode). In the mode:

1. **left click** = append a vertex at the cursor (unclamped level-0 px).
   The draft is **previewed live** on the `#annotations` canvas (a dashed
   stroke + the points) as it grows — the draft is *view state*, not yet in
   the document.
2. **`A` / annotate** again = **save**: the draft ring is pushed over the
   `last_polygon` trait and the mode exits. Python normalizes the ring with
   the **shared** `normalize_ring` (the same code path imported GeoJSON
   rings use), assigns a fresh `aN` id, and appends a `Polygon` feature
   (`properties: {}` — the view's draft carries no label/color) to the
   document, pushing the whole document back. A **degenerate draft**
   (< 3 non-collinear finite points) is **discarded** — warned, status
   `Discarded: polygon needs ≥ 3 non-collinear points`, document
   unchanged.
3. **`Esc`** = **cancel**: the draft is dropped, the mode exits, the
   document is untouched.

The draft is **not in the document** until saved — `v.annotations` never
sees a half-drawn polygon, and a kernel restart mid-draft loses only the
draft. The draft state machine (`polydraw.js`) is pure; the Python observer
of `last_polygon` is the final authority on what gets appended.

## Editing (select, delete, label, color)

Selection and editing work on **saved** features (the document), not the
draft:

- **left click** (idle mode, still — a ≥ 4 px drag is a pan) = **hit-test**:
  the topmost feature under the cursor (reverse `features` order; a point
  within ~8 px, a line within ~6 px, a polygon by its filled area) is
  **selected** (orange, width 3) and the **del / label / color** toolbar
  buttons enable. A miss **deselects**.
- **del** = remove the selected feature.
- **label** = an inline text editor in the toolbar (pre-filled from the
  feature; Enter/blur commits, Esc cancels, empty-after-trim commits
  `null` — clears the label). The selection survives the edit.
- **color** = an inline editor with native stroke/fill color pickers + a
  *clear fill* checkbox (a native color input cannot encode transparent);
  each change commits the full `{color, fill}` pair and closes.
- **`Esc` / `A` / annotate** = exit drawing mode (the selection is kept —
  a selected feature keeps its [vertex handles](#vertex-editing-drag-and-insert)).

Every action is one **`annotation_edit`** command over the JS → Py trait,
discriminated by `op`:

```
{op: "delete",     id}
{op: "set_label",  id, label}                  ("" / whitespace / null = no label)
{op: "set_color",  id, color, fill}           (null members = the defaults:
                                               black stroke, transparent fill)
{op: "set_vertex", id, index, x, y}           (see Vertex editing)
{op: "add_vertex", id, index, x, y}           (see Vertex editing)
```

The Python side applies the command with the pure `apply_edit` and pushes
the whole updated document back on `annotations`. **Idempotent**: a replayed
`delete` finds no feature; the replayed `set_*` ops store the same values; a
replayed `set_vertex` stores the same position; a replayed `add_vertex`
finds its position already at the insertion spot (a no-op). A command whose
`id` no longer addresses a feature (a Python-side `set_annotations()`
replace can race a JS click) or a malformed / refused command leaves the
document untouched — the status reports it (`edit ignored: …` /
`edit ignored: unknown annotation id`) and a `UserWarning` is raised.

The Python API issues the same commands:

```python
v.delete_annotation("a3")
v.set_annotation_label("a3", "tumor")          # None / "" / whitespace = clear
v.set_annotation_color("a3", color="red", fill=None)
```

## Vertex editing (drag and insert)

A **selected** saved feature exposes its vertices for editing, in **both**
modes (the edit mode and the plain pan/zoom mode):

- **vertex handles**: small circles (radius 3, screen px) at every position
  of the selected feature, in its flat position order (below). A **still
  click within 8 px** (screen) of a handle **grabs** it; a **drag** moves
  the vertex — the feature re-draws live at the dragged position (a
  preview) — and a **release** commits a `set_vertex` command at the release
  position (level-0 px). A **draft** polygon's pending vertices wear the
  same handles; dragging one just updates the draft (view state — no
  command).
- **segment insert** (drawing mode): a **still click within 320 px** (screen)
  of the **edge** of a *selected* feature shows a **dashed insert preview**
  (the two edges the click would create, with the new vertex's handle at its
  spot) as the cursor nears; the **click** commits an `add_vertex` command
  at the click position, with the pending preview kept (and grabbable) until
  the round-trip resolves it — an accepted push commits a plain `set_vertex`
  for any further drag; a refused one retires the preview.
- **`set_vertex` / `add_vertex`** are the same `annotation_edit` commands as
  the other edits; Python applies them with `apply_edit` and pushes the
  document back.

### The flat position / segment index

Both vertex commands address a vertex by the feature's **flat index** — one
`int` that walks the geometry in a fixed order, so the index is stable for a
given document and the JS view and the Python API share one numbering:

- **positions** (`set_vertex`): a `Point`'s single position; a
  `MultiPoint`'s / `LineString`'s positions in order; a `Polygon`'s rings in
  order (outer first, then holes), positions within a ring; a
  `MultiPolygon`'s islands in order, rings within an island, positions
  within a ring.
- **segments** (`add_vertex`): a `LineString` of `n` positions has `n − 1`
  segments; a ring of `n` positions has `n` segments — the last being the
  **closing edge** back to the ring's first position; a `Polygon`'s rings in
  order; a `MultiPolygon`'s islands in order, rings within an island.
  `Point` / `MultiPoint` have **no** segments. The new position is placed
  **between** the segment's two endpoints (appended to the ring for a
  closing edge) at `(x, y)` (level-0 slide px).

### Degeneracy (both commands)

A vertex move / insert that would **degenerate** the geometry is **refused**
(`ValueError` → warned, `edit ignored: …` in the status, document
unchanged): a ring collapsing to zero area (a hole closing up included), a
`LineString` flattening to zero length, or an `add_vertex` landing **on top
of an existing vertex**. An out-of-range `index` (a position / segment the
feature does not have) is refused the same way.

### Python API

```python
v.set_annotation_vertex("a3", 0, 100.0, 200.0)   # move position 0 -> (100, 200)
v.add_annotation_vertex("a3", 1, 50.0, 80.0)     # insert on segment 1 at (50, 80)
```

(`index` as above; coordinates are level-0 slide px. An unknown id, an
out-of-range index, a coincident position, or a degenerate result is refused
with the set unchanged — the status reports it.)

## Not in v1

- per-shape **move/resize** (single-vertex drag/insert is in; the
  whole-shape transform is not)
- multi-select, hover highlights / tooltips, undo/redo
- **export** beyond `json.dump(v.annotations)` — the document *is* the
  GeoJSON export; there is no separate export step
