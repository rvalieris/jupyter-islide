/**
 * Polygon drawing tests (node --test): the M3 state machine, the JS twin of
 * Python's normalize_ring, and the draft renderer with a mock canvas 2D
 * context (no DOM).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DASH_PATTERN, MODE_DRAWING, MODE_IDLE,
  drawDraftPolygon, hitTestDraftVertex, normalizeRing, polyDrawInit,
  polyEvent,
} from '../polydraw.js';
import { POINT_RADIUS, HALO_EXTRA, VERTEX_PICK_RADIUS } from '../annotations.js';

// screen transform: level-0 identity, 512x512 canvas -> l0 (x,y) => (x+256, y+256)
const T = { cx: 0, cy: 0, zoom: 1, canvasW: 512, canvasH: 512 };
const TRI = [[0, 0], [100, 0], [0, 100]];

function mockCtx() {
  const ops = [];
  const state = {};
  const rec = (name) => (...args) => { ops.push([name, ...args]); };
  const ctx = {
    ops,
    state,
    save: rec('save'),
    restore: rec('restore'),
    beginPath: rec('beginPath'),
    moveTo: rec('moveTo'),
    lineTo: rec('lineTo'),
    closePath: rec('closePath'),
    arc: rec('arc'),
    fill: rec('fill'),
    stroke: rec('stroke'),
    setLineDash: rec('setLineDash'),
  };
  for (const k of ['fillStyle', 'strokeStyle', 'lineWidth', 'globalAlpha',
                   'lineJoin', 'lineCap']) {
    Object.defineProperty(ctx, k, {
      get: () => state[k],
      set: (v) => { state[k] = v; ops.push(['set:' + k, v]); },
    });
  }
  return ctx;
}

// ------------------------------------------------------------ state machine
test('init: idle with an empty draft', () => {
  assert.deepEqual(polyDrawInit(), { mode: MODE_IDLE, draft: [] });
});

test('toggle from idle enters drawing mode (no result)', () => {
  const s = polyDrawInit();
  const r = polyEvent(s, { type: 'toggle' });
  assert.deepEqual(r.state, { mode: MODE_DRAWING, draft: [] });
  assert.equal(r.result, null);
  // the input state is never mutated
  assert.deepEqual(s, { mode: MODE_IDLE, draft: [] });
});

test('vertices accumulate only while drawing', () => {
  let s = polyDrawInit();
  const r0 = polyEvent(s, { type: 'vertex', x: 1, y: 2 });
  assert.deepEqual(r0.state.draft, []); // idle: vertex ignored
  s = polyEvent(s, { type: 'toggle' }).state;
  s = polyEvent(s, { type: 'vertex', x: 1, y: 2 }).state;
  s = polyEvent(s, { type: 'vertex', x: 3.5, y: -4.25 }).state;
  assert.equal(s.mode, MODE_DRAWING);
  assert.deepEqual(s.draft, [[1, 2], [3.5, -4.25]]);
});

test('toggle in drawing mode saves a valid ring, open and normalized', () => {
  let s = polyDrawInit();
  s = polyEvent(s, { type: 'toggle' }).state;
  for (const p of TRI) {
    s = polyEvent(s, { type: 'vertex', x: p[0], y: p[1] }).state;
  }
  const { state, result } = polyEvent(s, { type: 'toggle' });
  assert.deepEqual(state, { mode: MODE_IDLE, draft: [] });
  assert.equal(result.op, 'save');
  assert.deepEqual(result.ring, [[0, 0], [100, 0], [0, 100]]);
});

test('toggle in drawing mode discards degenerate drafts', () => {
  const two = polyEvent(
    polyEvent(
      polyEvent(polyDrawInit(), { type: 'toggle' }).state,
      { type: 'vertex', x: 0, y: 0 }).state,
    { type: 'vertex', x: 5, y: 5 }).state;
  assert.equal(polyEvent(two, { type: 'toggle' }).result.op, 'discard');

  let s = polyEvent(polyDrawInit(), { type: 'toggle' }).state;
  for (const p of [[0, 0], [10, 0], [20, 0]]) {
    s = polyEvent(s, { type: 'vertex', x: p[0], y: p[1] }).state;
  }
  const r = polyEvent(s, { type: 'toggle' });
  assert.equal(r.result.op, 'discard'); // collinear
  assert.deepEqual(r.state, { mode: MODE_IDLE, draft: [] });
});

test('cancel exits drawing mode and discards the draft', () => {
  let s = polyDrawInit();
  s = polyEvent(s, { type: 'toggle' }).state;
  s = polyEvent(s, { type: 'vertex', x: 0, y: 0 }).state;
  s = polyEvent(s, { type: 'vertex', x: 9, y: 9 }).state;
  const { state, result } = polyEvent(s, { type: 'cancel' });
  assert.deepEqual(result, { op: 'cancel' });
  assert.deepEqual(state, { mode: MODE_IDLE, draft: [] });
});

test('cancel while idle is a no-op', () => {
  const s = polyDrawInit();
  const r = polyEvent(s, { type: 'cancel' });
  assert.equal(r.result, null);
  assert.equal(r.state, s); // same object
});

test('unknown event types are no-ops', () => {
  const s = polyDrawInit();
  assert.equal(polyEvent(s, { type: 'nope' }).state, s);
});

test('toggle after save returns to a fresh drawing round', () => {
  let s = polyDrawInit();
  s = polyEvent(s, { type: 'toggle' }).state;
  for (const p of TRI) s = polyEvent(s, { type: 'vertex', x: p[0], y: p[1] }).state;
  assert.equal(polyEvent(s, { type: 'toggle' }).result.op, 'save');
  s = polyEvent(polyDrawInit(), { type: 'toggle' }).state;
  s = polyEvent(s, { type: 'vertex', x: 1, y: 1 }).state;
  assert.deepEqual(s.draft, [[1, 1]]); // the previous ring did not leak
});

// ------------------------------------------------------------ normalizeRing
test('normalizeRing matches the Python contract', () => {
  assert.equal(normalizeRing(null), null);
  assert.equal(normalizeRing('nope'), null);
  assert.equal(normalizeRing([1, 2]), null);
  assert.equal(normalizeRing([[0, 0]]), null);
  assert.equal(normalizeRing([[0, 0], [5, 5]]), null);
  assert.equal(normalizeRing([[0, 0], [0, 0], [0, 0]]), null); // duplicates
  assert.equal(normalizeRing([[0, 0], [10, 0], [20, 0]]), null); // collinear
  assert.equal(normalizeRing([[0, 0], [10, 0], [NaN, 0]]), null);
  assert.equal(normalizeRing([[0, 0], [10, 0], [0, Infinity]]), null);
  assert.equal(normalizeRing([[0, 0], [10], [0, 10]]), null);
  assert.equal(normalizeRing([[0, 0], [10, 0, 0], [0, 10]]), null);
  // strip a redundant closing position only
  assert.deepEqual(
    normalizeRing([[0, 0], [10, 0], [0, 10], [0, 0]]),
    [[0, 0], [10, 0], [0, 10]],
  );
  // unclamped: off-slide coordinates pass through untouched
  assert.deepEqual(
    normalizeRing([[-50, -60], [4000, 0], [0, 9000]]),
    [[-50, -60], [4000, 0], [0, 9000]],
  );
});

// ------------------------------------------------------- draft renderer
test('drawDraftPolygon: empty draft draws nothing', () => {
  const ctx = mockCtx();
  assert.equal(
    drawDraftPolygon(ctx, { transform: T, draft: [] }), 0,
  );
  assert.equal(ctx.ops.length, 0);
});

test('drawDraftPolygon: vertex dots (halo + body), solid segments', () => {
  const ctx = mockCtx();
  assert.equal(drawDraftPolygon(ctx, { transform: T, draft: TRI }), 3);
  const arcs = ctx.ops.filter(([m]) => m === 'arc');
  assert.equal(arcs.length, 6);
  // l0 (0,0) -> screen (256,256): halo first, then body, M2 sizes
  assert.deepEqual(arcs[0].slice(1, 3), [256, 256]);
  assert.equal(arcs[0][3], POINT_RADIUS + HALO_EXTRA);
  assert.equal(arcs[1][3], POINT_RADIUS);
  assert.equal(ctx.state.fillStyle, '#000000');
  const lineTos = ctx.ops.filter(([m]) => m === 'lineTo');
  assert.equal(lineTos.length, 2); // two solid segments, no cursor
  const dashes = ctx.ops.filter(([m]) => m === 'setLineDash');
  assert.equal(dashes.length, 0);
});

test('drawDraftPolygon: dashed closure tracks the cursor', () => {
  const ctx = mockCtx();
  drawDraftPolygon(ctx, {
    transform: T, draft: TRI, cursor: { x: 300, y: 300 },
  });
  const lineTos = ctx.ops.filter(([m]) => m === 'lineTo');
  // last vertex -> cursor, plus cursor -> first vertex (>= 3 vertices)
  assert.equal(lineTos.length, 4);
  const dashes = ctx.ops.filter(([m]) => m === 'setLineDash');
  assert.equal(dashes.length, 2); // pattern on, then cleared after the stroke
  assert.deepEqual(dashes[0][1], DASH_PATTERN);
  assert.deepEqual(dashes[1][1], []);
});

test('drawDraftPolygon: a lone vertex shows a single dashed segment', () => {
  const ctx = mockCtx();
  drawDraftPolygon(ctx, {
    transform: T, draft: [[10, 20]], cursor: { x: 400, y: 400 },
  });
  assert.equal(
    ctx.ops.filter(([m]) => m === 'lineTo').length, 1,
  );
});

test('drawDraftPolygon: alpha applies to the whole draft', () => {
  const ctx = mockCtx();
  drawDraftPolygon(ctx, {
    transform: T, draft: TRI, cursor: { x: 300, y: 300 }, alpha: 0.5,
  });
  const alphas = ctx.ops.filter(([m]) => m === 'set:globalAlpha');
  assert.deepEqual(alphas, [['set:globalAlpha', 0.5]]);
});

// ------------------------------------------------------------ M6 (vertex editing)

test('hitTestDraftVertex: within VERTEX_PICK_RADIUS of a draft vertex', () => {
  const draft = [[0, 0], [30, 0]]; // l0 -> screen (256, 256) / (286, 256)
  assert.equal(hitTestDraftVertex(draft, T, 256, 256), 0);
  // the distance test is inclusive of the exact radius
  assert.equal(hitTestDraftVertex(draft, T, 256 + VERTEX_PICK_RADIUS, 256), 0);
  assert.equal(
    hitTestDraftVertex(draft, T, 286 + VERTEX_PICK_RADIUS + 1, 256), null);
  assert.equal(hitTestDraftVertex([], T, 256, 256), null);
  assert.equal(hitTestDraftVertex(null, T, 256, 256), null);
});

test('move_vertex: moves the addressed draft vertex; others untouched', () => {
  let s = polyEvent(polyDrawInit(), { type: 'toggle' }).state;
  for (const p of TRI) s = polyEvent(s, { type: 'vertex', x: p[0], y: p[1] }).state;
  const before = s.draft;
  const { state, result } = polyEvent(s, { type: 'move_vertex', index: 1, x: 50, y: 40 });
  assert.equal(result, null); // a move is not a commit
  assert.deepEqual(state.draft, [[0, 0], [50, 40], [0, 100]]);
  assert.equal(state.mode, MODE_DRAWING);
  assert.deepEqual(before, TRI); // the input state is never mutated
  // the moved draft is still a valid ring
  assert.ok(normalizeRing(state.draft));
});

test('move_vertex: bad index or position is a no-op', () => {
  let s = polyEvent(polyDrawInit(), { type: 'toggle' }).state;
  s = polyEvent(s, { type: 'vertex', x: 0, y: 0 }).state;
  const before = s;
  const noops = [
    { type: 'move_vertex', index: -1, x: 1, y: 1 },
    { type: 'move_vertex', index: 5, x: 1, y: 1 },
    { type: 'move_vertex', index: 1.5, x: 1, y: 1 },
    { type: 'move_vertex', index: '0', x: 1, y: 1 },
    { type: 'move_vertex', index: 0, x: NaN, y: 0 },
    { type: 'move_vertex', index: 0, x: Infinity, y: 0 },
    { type: 'move_vertex', index: 0, y: 0 }, // x missing
  ];
  for (const ev of noops) assert.equal(polyEvent(s, ev).state, before);
  // outside drawing mode: a no-op
  const idle = polyDrawInit();
  assert.equal(polyEvent(idle, { type: 'move_vertex', index: 0, x: 1, y: 1 }).state, idle);
});
