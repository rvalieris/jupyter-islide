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
  last_polygon: null,
  // M3.5: the canonical annotation document (a GeoJSON FeatureCollection
  // in level-0 px; DESIGN.md §6.3). Rendered read-only by the JS view.
  annotations: { type: 'FeatureCollection', features: [] },
  // M4: the last issued annotation edit command (JS -> Py last-event slot;
  // DESIGN.md §6.5): {op: "delete" | "set_label" | "set_color", id, ...}
  // or null (no command yet). The Python observer applies it to
  // `annotations` and pushes the updated set.
  annotation_edit: null,
  status: '',
};

export const ISLIDE_MODULE_VERSION = '2.0.0';
