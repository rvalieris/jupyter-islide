/**
 * Model attribute defaults shared by the SlideModel.
 *
 * The trait names here MUST match the Python-side `tag(sync=True)` traits
 * on `islide.widget.SlideViewer` (docs/DESIGN.md §7).
 */
export const SLIDE_MODEL_DEFAULTS = {
  slide_open: false,
  meta: null,
  viewport: null,
  canvas_h: 540,
  tiles: {},
  tile_geo: {},
  minimap_img: '',
  // Overlay: a full-slide image (e.g. a model heatmap at get_thumbnail
  // scale) drawn over the tiles and under the annotations. `overlay_img`
  // is a PNG data URL ('' = none); `overlay_alpha` its opacity in [0, 1].
  overlay_img: '',
  overlay_alpha: 0.5,
  last_polygon: null,
  // The canonical annotation document (a GeoJSON FeatureCollection
  // in level-0 px; docs/DESIGN.md §6.3). Rendered read-only by the JS view.
  annotations: { type: 'FeatureCollection', features: [] },
  // The last issued annotation edit command (JS -> Py last-event
  // slot; docs/annotations.md): {op: "delete" | "set_label" | "set_color",
  // id, ...} or {op: "set_vertex", id, index, x, y} (index: the feature's
  // flat canonical position index, x/y level-0 px) or {op: "add_vertex",
  // id, index, x, y} (index: the feature's flat canonical *segment* index,
  // x/y the new position in level-0 px), or null (no command yet). The
  // Python observer applies it to `annotations` and pushes the updated
  // set.
  annotation_edit: null,
  // The attach re-render counter (JS -> Py): bumped once per attach by the
  // view. A re-attached view seeds its image cache from the synced state —
  // the *last* chunk of the last render (per-chunk pushes, DESIGN §6.6.2)
  // — and its fit-echo can be a no-op in Python (traitlets fires no
  // observer for a value-equal set), so the full-set re-render is
  // requested explicitly here (last-event counter, like last_polygon /
  // annotation_edit).
  resync: 0,
  status: '',
};

export const ISLIDE_MODULE_VERSION = '2.1.0';
