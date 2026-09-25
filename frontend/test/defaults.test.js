/**
 * The model defaults must mirror the Python-side sync trait set exactly
 * (DESIGN.md §7). The Python side has the reverse guard in
 * tests/test_widget_m1.py.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { SLIDE_MODEL_DEFAULTS, ISLIDE_MODULE_VERSION } from '../defaults.js';

test('defaults cover exactly the documented trait names', () => {
  assert.deepEqual(
    Object.keys(SLIDE_MODEL_DEFAULTS).sort(),
    ['canvas_h', 'last_click', 'last_region', 'meta', 'minimap_img',
     'slide_open', 'status', 'tile_geo', 'tiles', 'viewport'],
  );
});

test('module version matches the Python-side declaration', () => {
  assert.equal(ISLIDE_MODULE_VERSION, '1.0.0');
});
