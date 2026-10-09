/**
 * Wire-contract check (docs/DESIGN.md §6.1.1): what the Python widget
 * declares and sends vs. what this frontend implements. The widget
 * manager has already resolved the module version strings to
 * instantiate this view; this runs at view attach to catch what slips
 * past that resolution and what the version strings cannot express —
 * a kernel/extension version skew and drift in the synced trait names.
 *
 * Pure functions, no DOM / no Backbone — see test/wirecheck.test.js.
 */

// The six identity traits a full ipywidgets comm state always carries
// (the comm-state shape guard for the "missing trait" direction).
export const IDENTITY_TRAITS = [
  '_model_name', '_view_name', '_model_module', '_view_module',
  '_model_module_version', '_view_module_version',
];

// Keys the widget manager may pass into the model constructor alongside
// the comm state — not wire traits, so never "unknown traits".
const MANAGER_EXTRAS = new Set(['model_id', 'comm', 'widget_manager']);

/**
 * Parse a module version: '2.1.0' -> [2, 1, 0] (also '2.1', '2').
 * Both sides of this project declare exact versions
 * (tests/test_versions.py); anything else — a semver range, garbage —
 * is null, and the caller reports it.
 */
export function parseWireVersion(version) {
  if (typeof version !== 'string') return null;
  const m = /^\d+(\.\d+){0,2}$/.exec(version.trim());
  if (!m) return null;
  const parts = m[0].split('.').map(Number);
  while (parts.length < 3) parts.push(0);
  return parts;
}

/**
 * Check the wire contract; returns human-readable warnings (empty =
 * clean).
 *
 * @param {object} opts
 * @param {string} opts.moduleVersion the version this extension
 *   registered (ISLIDE_MODULE_VERSION).
 * @param {Object<string, string>} opts.kernelVersions the module
 *   version strings the Python widget declares
 *   (`_model_module_version`, `_view_module_version`).
 * @param {string[]|undefined} opts.wireTraits the trait names the
 *   kernel actually sent in the comm state (SlideModel._wireTraits);
 *   undefined/empty = unknown shape, trait checks are skipped.
 * @param {string[]} opts.dataTraits the JS-contract data traits
 *   (the SLIDE_MODEL_DEFAULTS keys).
 * @param {string[]} opts.modelDefaultTraits the full trait set this
 *   model declares (the SlideModel.defaults() keys).
 * @param {string[]} opts.serializedTraits the trait names the base
 *   widget model deserializes natively (the SlideModel.serializers
 *   keys, e.g. `layout`/`style` — constructed kernel-side, synced to us,
 *   and not part of `defaults()`).
 */
export function wireWarnings({
  moduleVersion, kernelVersions, wireTraits, dataTraits,
  modelDefaultTraits, serializedTraits = [],
}) {
  const warnings = [];
  const module = parseWireVersion(moduleVersion);

  // 1. Module version skew, either direction: the wire contract this
  //    frontend implements is keyed to its registered version, and the
  //    kernel's declaration is its own.
  for (const [trait, kernelVersion] of Object.entries(kernelVersions || {})) {
    const kernel = parseWireVersion(kernelVersion);
    if (kernel === null) {
      warnings.push(
        `unparseable ${trait} from the kernel: '${kernelVersion}' — ` +
        `expected a module version like '${moduleVersion}'`,
      );
    } else if (module === null) {
      warnings.push(
        `unparseable module version '${moduleVersion}' ` +
        `registered by this extension`,
      );
    } else if (kernel.join('.') !== module.join('.')) {
      warnings.push(
        `${trait} mismatch: the kernel declares '${kernelVersion}' ` +
        `but this frontend provides '${moduleVersion}'`,
      );
    }
  }

  if (!Array.isArray(wireTraits) || wireTraits.length === 0) return warnings;

  // 2. The kernel sent trait names this frontend's contract does not
  //    include (the Python side is newer than this frontend, or a
  //    trait was added without the wire-version bump). What "this
  //    frontend handles" = the traits in defaults() plus the traits
  //    the base widget model deserializes natively (serializers — e.g.
  //    `layout`, which DOMWidget syncs but `defaults()` does not
  //    declare), plus the manager's own extras.
  const expected = new Set([
    ...(modelDefaultTraits || []),
    ...(serializedTraits || []),
  ]);
  const unknown = wireTraits
    .filter((name) => !expected.has(name) && !MANAGER_EXTRAS.has(name))
    .sort();
  if (unknown.length > 0) {
    warnings.push(
      `the kernel sent trait(s) this frontend does not handle: ` +
      `${unknown.join(', ')}`,
    );
  }

  // 3. The kernel's comm state has no trait this frontend expects
  //    (this frontend is newer than the Python side). Only meaningful
  //    for a *full* comm state — the identity traits are always
  //    present in one, so their presence is the shape guard (a partial
  //    snapshot would turn every default into a "missing" warning).
  if (IDENTITY_TRAITS.every((name) => wireTraits.includes(name))) {
    const missing = (dataTraits || [])
      .filter((name) => !wireTraits.includes(name))
      .sort();
    if (missing.length > 0) {
      warnings.push(
        `the kernel's comm state has no trait(s) this frontend expects: ` +
        `${missing.join(', ')}`,
      );
    }
  }

  return warnings;
}
