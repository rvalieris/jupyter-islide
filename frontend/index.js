/**
 * jupyter-islide — JupyterLab extension package for the islide WSI viewer.
 *
 * Exports the widget model/view resolved by the JupyterLab widget manager
 * from the Python-side `_model_module: "jupyter-islide"` declaration.
 */
export { SlideModel } from './model.js';
export { SlideView } from './view.js';
export * from './tilemath.js';
export { drawScene } from './compositor.js';
export { SLIDE_MODEL_DEFAULTS, ISLIDE_MODULE_VERSION } from './defaults.js';
