/**
 * jupyter-islide widget model.
 *
 * Registered in JupyterLab through the widget registry (see
 * labextension.js); resolved by the widget manager from the Python-side
 * `_model_name` / `_model_module` / `_model_module_version` traits.
 */
import { DOMWidgetModel } from '@jupyter-widgets/base';
import { SLIDE_MODEL_DEFAULTS, ISLIDE_MODULE_VERSION } from './defaults.js';

export class SlideModel extends DOMWidgetModel {
  defaults() {
    return {
      ...super.defaults(),
      _model_name: 'SlideModel',
      _view_name: 'SlideView',
      _model_module: 'jupyter-islide',
      _view_module: 'jupyter-islide',
      _model_module_version: ISLIDE_MODULE_VERSION,
      _view_module_version: ISLIDE_MODULE_VERSION,
      ...SLIDE_MODEL_DEFAULTS,
    };
  }
}
