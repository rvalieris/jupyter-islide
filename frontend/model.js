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

  initialize(attributes, options) {
    super.initialize(attributes, options);
    // Snapshot of the trait names the kernel actually sent in the comm
    // state (the wire contract, DESIGN.md §6.1.1). Backbone merges the
    // JS defaults into the constructor attributes, so the server-side
    // set is no longer recoverable from `this.attributes` after this —
    // SlideView's wire-contract check (wirecheck.js) uses the snapshot.
    this._wireTraits = attributes
      ? Object.keys(attributes).filter((k) => k !== 'model_id')
      : [];
  }
}
