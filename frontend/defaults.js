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
  tiles: {},
  tile_geo: {},
  minimap_img: '',
  last_click: {},
  last_region: {},
  status: '',
};

export const ISLIDE_MODULE_VERSION = '1.0.0';
