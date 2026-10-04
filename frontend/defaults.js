/**
 * Model attribute defaults shared by the SlideModel.
 *
 * The trait names here MUST match the Python-side `tag(sync=True)` traits
 * on `islide.widget.SlideViewer` (DESIGN.md §7).
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
  // M3.5: the canonical annotation document (a GeoJSON FeatureCollection
  // in level-0 px; DESIGN.md §6.3). Rendered read-only by the JS view.
  annotations: { type: 'FeatureCollection', features: [] },
  // M4/M6: the last issued annotation edit command (JS -> Py last-event
  // slot; DESIGN.md §6.5/§6.7): {op: "delete" | "set_label" | "set_color",
  // id, ...} or {op: "set_vertex", id, index, x, y} (index: the feature's
  // flat canonical position index, x/y level-0 px), or null (no command
  // yet). The Python observer applies it to `annotations` and pushes the
  // updated set.
  annotation_edit: null,
  status: '',
};

export const ISLIDE_MODULE_VERSION = '2.0.0';
