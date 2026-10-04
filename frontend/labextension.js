/**
 * JupyterLab entry point: register the jupyter-islide widget module with the
 * JupyterLab widget manager's external widget registry.
 *
 * The registry matches the Python-side `_model_module` ("jupyter-islide") and
 * `_model_module_version` ("2.0.0") against the {name, version} pair
 * registered here (semver), then resolves the `_model_name` / `_view_name`
 * class from `exports`.
 */
import { IJupyterWidgetRegistry } from '@jupyter-widgets/base';
import { SlideModel } from './model.js';
import { SlideView } from './view.js';
import { ISLIDE_MODULE_VERSION } from './defaults.js';

const extension = {
  id: 'jupyter-islide:widget-registry',
  autoStart: true,
  requires: [IJupyterWidgetRegistry],
  activate(app, registry) {
    registry.registerWidget({
      name: 'jupyter-islide',
      version: ISLIDE_MODULE_VERSION,
      exports: { SlideModel, SlideView },
    });
  },
};

export default extension;
