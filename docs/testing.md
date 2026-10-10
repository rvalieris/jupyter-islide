# Testing

Two suites, no browser required:

- **Python**: `python -m pytest tests/`
- **JS**: `cd frontend && node --test test/`

The Python widget is a plain Python object until displayed, so the whole
Python-side state machine (open, render, cache, traits, annotation edits) is
tested headless — no JS view, no browser. The JS view is thin DOM/event
wiring over pure modules; the pure modules are unit-tested against mock
2D contexts, and the wiring is verified by the manual matrix (classic
nb / lab / ×2 DPR) when a browser is available.

## The test slide

- **Integration (needs libopenslide):** real open/read against a real slide,
  `data/CMU-1.tiff` (whole-slide generic-TIFF, 46 000 × 32 914 px, 9 levels,
  1.0 mm/px; opens via the generic-TIFF vendor). The slide is **not
  committed**: the session-scoped `slide_path` fixture in `tests/conftest.py`
  downloads it at test time from openslide-testdata
  (`https://openslide.cs.cmu.edu/download/openslide-testdata/Generic-TIFF/CMU-1.tiff`)
  if `data/CMU-1.tiff` is missing, and dependent tests skip if the file is
  absent and the download fails.
  - Historical note: a single-level tiled TIFF written by `tifffile`
    (`tile=(256, 256)`) opens fine, and a hand-written multi-page "pyramid"
    TIFF was detected as 1 level (the generic-TIFF multi-resolution rules
    are stricter than "smaller sub-IFDs") — a real multi-resolution file is
    what the integration path exercises.
- **`FakeSlide`** (`tests/util.py`): a metadata-only stub of the backend,
  used by every unit test that needs a slide without libopenslide.
- Level-selection math is additionally unit-tested against a fixed
  `SlideMeta` fixture in `tests/test_plan.py`.

## Python unit tests (no OpenSlide)

| File | Covers |
|---|---|
| `tests/test_plan.py` | the pure planning math: level selection, grid-anchored chunking (a viewport inside one 1024 px block yields a single chunk identical to the un-chunked plan — byte-for-byte), chunk ordering (viewport-center block first, distance order, tie-break by reading order), grid-anchored + clamped read rects, chunk-tile union == whole-viewport tile set |
| `tests/test_cache.py` | the LRU tile cache |
| `tests/test_annotations.py` | the pure annotation document: `parse_annotations` (all six source geometry types; `MultiPoint`/`MultiPolygon` kept whole; `GeometryCollection` expanded; `properties` pass-through with null-value drop; ids unique — collision `ValueError`; degenerate drops with warnings; bare-geometry + single-`Feature` inputs; strict error paths) and `apply_edit` (all five ops, per geometry type; the flat position/segment indices; idempotence; unknown id → `None`; bad index / non-finite / coincident / degenerate → `ValueError`; input non-mutation) |
| `tests/test_backend.py` | the backend seam + the `slide=` constructor argument |
| `tests/test_versions.py` | the version guard: `pyproject.toml` (the source of truth) must match `frontend/package.json` and `frontend/package-lock.json`, and `frontend/sync-version.mjs` must replace stale versions |

## Widget tests (headless, no JS view)

| File | Covers |
|---|---|
| `tests/test_widget.py` | the core `SlideViewer`: open, `meta` shape, headless fit-viewport default, the `tiles`/`tile_geo` key + geometry contract (the per-chunk pushes' union, cross-checked against a fresh `plan_viewport`), cache invariance (pan away and back → byte-identical tile payloads), `set_zoom`/`center_on`/`viewport_bbox`/`read_crop`, zoom clamping, JS-originated viewport trait change → background render, the `resync` attach counter → background render of the current viewport, open-failure error path, `close()` idempotence, and the cross-language trait contract with `frontend/defaults.js` |
| `tests/test_widget_annotations.py` | annotation import: `set_annotations`/`clear_annotations` → `annotations` trait shape and level-0 values, replace semantics, import |
| `tests/test_widget_polygon.py` | drawn polygons: `last_polygon` → a `Polygon` feature appended (fresh non-colliding id, empty `properties`); degenerate drafts (2 pts, collinear 3 pts, non-finite) dropped with a warning and no state change; successive saves get unique ids; `jpeg_quality` constructor arg (default 85, alters the `tiles` payload only) |
| `tests/test_widget_edit.py` | annotation editing: the `annotation_edit` trait → `apply_edit` → updated `annotations` (unknown id → no push, `edit ignored` status; a degenerate/coincident/out-of-range vertex edit refused with the document unchanged); the `delete_annotation` / `set_annotation_label` / `set_annotation_color` / `set_annotation_vertex` / `add_annotation_vertex` API routes; `clear_annotations()` after |
| `tests/test_widget_chunks.py` | smooth zoom: per-chunk push (a multi-chunk render assigns `tiles`/`tile_geo` once per chunk in the plan's viewport-center-first order — each push's `tile_geo` the chunk's absolute level-px crops, the union of the render's pushes the full viewport set, the traits holding the last chunk (last-event); a single-chunk render pushes exactly once; a superseded (dirty) render mid-pass leaves the final traits consistent with the latest viewport) |
| `tests/test_overlay.py` | the full-slide overlay: `set_overlay`/`clear_overlay`, the `overlay_img`/`overlay_alpha` traits, aspect checking, the `transparent` key |
| `tests/test_integration.py` | real open/read against the test slide (needs openslide + the downloaded slide; skips otherwise) |
| `tests/test_rerun.py` | notebook re-run / display-timing regression: a view that attaches and bumps `resync` right after construction, and re-running the construction cell (a new widget while the previous one may still be open); both keep serving tiles when the user zooms |

## JS tests (no browser, `frontend/test/`, `node --test`)

The pure modules, against mock 2D contexts:

| File | Covers |
|---|---|
| `tilemath.test.js` | the pure math: round-trips, cursor-fixed zoom, pan, tile screen rects (checked against the reference screen-box formula), visible-tile selection, zoom clamping |
| `blend.test.js` | the level cross-fade: `selectLevel` matches `plan.select_level` on a shared downsample table (the §4 rule, both sides, incl. the coarsest-level fallback); a transition is recorded on a selected-level change and only then; the alpha schedule (new level 1, old level `1 → 0` over `BLEND_MS`, clamped, direction-independent; stale levels 0); a new transition replaces the old |
| `compositor.test.js` | the compositor against a mock ctx: the background fill (plain light fill for the pattern-less mock; the repeating checkerboard pattern for a pattern-capable ctx — one `2s×2s` tile canvas, `paintCheckerTile`'s rects, pattern reuse across frames), per-tile `drawImage` rects, skip-not-ready, re-projection under a changed local transform, multi-level draw with per-level `globalAlpha`, coarsest-first ordering, seam margin, and `drawOverlay` (the full-slide overlay image stretched over the slide at `alpha`) |
| `annotations.test.js` | `drawAnnotations` against a mock ctx (culling, the three draw primitives — markers incl. `MultiPoint` loops, open path, evenodd ring-set incl. flattened `MultiPolygon` islands — screen-constant widths, `color`/`fill` defaults (black/transparent), alpha (`globalAlpha`), label rendering, the selected-feature override, per-feature guard — a malformed feature is skipped, the rest still draw); `hitTest` (polygon evenodd interior / outline / holes, line distance tolerance, point radius, topmost-first, miss → `null`); the vertex-editing helpers (`featurePositions`, `withMovedVertex`, `withAddedVertex`, `hitTestVertex`, `hitTestSegment`, `addedVertexIndex`, `drawVertexHandles`, `drawInsertPreview` — all pure, incl. the flat indices, immutability, and the malformed-input → same-document / empty cases) |
| `polydraw.test.js` | the pure draw-mode state machine (A/Esc enter/save/cancel, click-vs-drag threshold, vertex accumulation, draft-vertex moves) and `drawDraftPolygon` against a mock ctx (vertex dots, segments, dashed closure to cursor / first vertex) |
| `defaults.test.js` | the shared model defaults — the exact trait name set (the JS half of the cross-language contract) — and the widget module version |

## Cross-language contract

The Python synced trait names are asserted to equal the keys in
`frontend/defaults.js` (Python side: `tests/test_widget.py`; JS side:
`frontend/test/defaults.test.js` — both directions of the same guard), and
the package version is asserted synced across
`pyproject.toml` / `frontend/package.json` / `frontend/package-lock.json`
(`tests/test_versions.py`) — so the comm contract and the release versions
can't drift.
