/**
 * Wire-contract check (wirecheck.js): what the Python widget declares
 * and sends vs. what this frontend implements (docs/DESIGN.md §6.1.1).
 * The data trait set comes from defaults.js — the same source of truth
 * the model and the Python-side contract test use.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { SLIDE_MODEL_DEFAULTS, ISLIDE_MODULE_VERSION } from '../defaults.js';
import { IDENTITY_TRAITS, parseWireVersion, wireWarnings } from '../wirecheck.js';

const DATA_TRAITS = Object.keys(SLIDE_MODEL_DEFAULTS);
// What ipywidgets' DOMWidget syncs on top of the contract traits —
// always in the comm state, always handled by the base widget model
// (not by our contract).
const BASE_SYNC_TRAITS = ['_dom_classes', 'layout', 'tabbable', 'tooltip'];
// The real SlideModel.defaults() keys (base WidgetModel + DOMWidgetModel
// defaults + ours). `layout` is deliberately NOT among them: the base
// model deserializes it via `serializers` instead of declaring a default.
const MODEL_DEFAULT_TRAITS = [
  ...IDENTITY_TRAITS, '_view_count',
  ...BASE_SYNC_TRAITS.filter((t) => t !== 'layout'), ...DATA_TRAITS,
];
const SERIALIZED_TRAITS = ['layout', 'style'];
const KERNEL_VERSIONS = {
  _model_module_version: ISLIDE_MODULE_VERSION,
  _view_module_version: ISLIDE_MODULE_VERSION,
};
// The real full comm state (verified against a live kernel): every trait
// the kernel's SlideViewer syncs, base plumbing included.
const FULL_STATE = [...MODEL_DEFAULT_TRAITS, 'layout'];

function opts(overrides = {}) {
  return {
    moduleVersion: ISLIDE_MODULE_VERSION,
    kernelVersions: KERNEL_VERSIONS,
    wireTraits: FULL_STATE,
    dataTraits: DATA_TRAITS,
    modelDefaultTraits: MODEL_DEFAULT_TRAITS,
    serializedTraits: SERIALIZED_TRAITS,
    ...overrides,
  };
}

test('parseWireVersion', () => {
  assert.deepEqual(parseWireVersion('2.1.0'), [2, 1, 0]);
  assert.deepEqual(parseWireVersion('2.1'), [2, 1, 0]);
  assert.deepEqual(parseWireVersion('10'), [10, 0, 0]);
  assert.equal(parseWireVersion('^2.1.0'), null);
  assert.equal(parseWireVersion('>=2.0.0'), null);
  assert.equal(parseWireVersion('2.1.0.0'), null);
  assert.equal(parseWireVersion('not-a-version'), null);
  assert.equal(parseWireVersion(2.1), null);
  assert.equal(parseWireVersion(''), null);
});

test('clean contract: no warnings', () => {
  // The real comm state carries the base DOMWidget plumbing (incl.
  // `layout`, which `defaults()` does not declare); a clean contract
  // must not flag any of it.
  assert.ok(FULL_STATE.includes('layout'));
  assert.ok(FULL_STATE.includes('_dom_classes'));
  assert.deepEqual(wireWarnings(opts()), []);
});

test('base DOMWidget plumbing traits are not unknown (layout regression)', () => {
  // ipywidgets 8 DOMWidget syncs _dom_classes/layout/tabbable/tooltip on
  // top of the contract. The base widget model handles them (layout via
  // `serializers`), so none may be reported as unknown.
  for (const t of BASE_SYNC_TRAITS) {
    assert.ok(FULL_STATE.includes(t));
  }
  assert.deepEqual(wireWarnings(opts()), []);
  // And the same holds if they arrive as the only "extra" traits.
  assert.deepEqual(wireWarnings(opts({
    wireTraits: [...IDENTITY_TRAITS, ...BASE_SYNC_TRAITS, ...DATA_TRAITS],
  })), []);
});

test('manager extras in the comm state are not unknown traits', () => {
  assert.deepEqual(wireWarnings(opts({
    wireTraits: [...FULL_STATE, 'model_id', 'comm', 'widget_manager'],
  })), []);
});

test('module version mismatch (kernel newer)', () => {
  const warnings = wireWarnings(opts({
    kernelVersions: {
      _model_module_version: '2.2.0',
      _view_module_version: ISLIDE_MODULE_VERSION,
    },
  }));
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /_model_module_version mismatch/);
  assert.match(warnings[0], /2\.2\.0/);
  assert.match(warnings[0], new RegExp(ISLIDE_MODULE_VERSION.replace('.', '\\.')));
});

test('module version mismatch (kernel older, both traits)', () => {
  const warnings = wireWarnings(opts({
    kernelVersions: {
      _model_module_version: '2.0.0',
      _view_module_version: '2.0.0',
    },
  }));
  assert.equal(warnings.length, 2);
  assert.match(warnings[0], /_model_module_version mismatch/);
  assert.match(warnings[1], /_view_module_version mismatch/);
});

test('unparseable kernel version is a warning', () => {
  const warnings = wireWarnings(opts({
    kernelVersions: {
      _model_module_version: 'garbage',
      _view_module_version: ISLIDE_MODULE_VERSION,
    },
  }));
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /unparseable _model_module_version/);
});

test('kernel traits this frontend does not handle', () => {
  const warnings = wireWarnings(opts({
    wireTraits: [...FULL_STATE, 'a_new_trait', 'another_new_trait'],
  }));
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /does not handle: a_new_trait, another_new_trait/);
});

test('kernel comm state missing data traits (full state)', () => {
  const wireTraits = FULL_STATE.filter((t) => t !== 'resync' && t !== 'canvas_h');
  const warnings = wireWarnings(opts({ wireTraits }));
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /has no trait\(s\) this frontend expects: canvas_h, resync/);
});

test('partial snapshot (no identity traits): missing-trait check skipped', () => {
  // A snapshot shape we do not recognize (e.g. a manager that sets state
  // after construction) must not turn every JS default into a warning.
  assert.deepEqual(wireWarnings(opts({ wireTraits: ['meta'] })), []);
  assert.deepEqual(wireWarnings(opts({ wireTraits: [] })), []);
});

test('no snapshot: only the version checks run', () => {
  assert.deepEqual(wireWarnings(opts({ wireTraits: undefined })), []);
  const warnings = wireWarnings(opts({
    wireTraits: undefined,
    kernelVersions: {
      _model_module_version: '9.9.9',
      _view_module_version: ISLIDE_MODULE_VERSION,
    },
  }));
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /9\.9\.9/);
});
