/**
 * Annotation overlay tests with a mock canvas 2D context (node --test).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  drawAnnotations,
  STROKE_WIDTH,
  POINT_RADIUS,
  HALO_EXTRA,
} from '../annotations.js';

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
    fillText: rec('fillText'),
    strokeText: rec('strokeText'),
  };
  for (const k of ['fillStyle', 'strokeStyle', 'lineWidth', 'globalAlpha',
                   'font', 'textBaseline', 'lineJoin', 'lineCap']) {
    Object.defineProperty(ctx, k, {
      get: () => state[k],
      set: (v) => { state[k] = v; ops.push(['set:' + k, v]); },
    });
  }
  return ctx;
}

const T = { cx: 0, cy: 0, zoom: 1, canvasW: 512, canvasH: 512 };
// visible level-0 x/y range: [-256, 256]

const pt = (x, y, extra = {}) => ({
  id: 'p', kind: 'point', points: [[x, y]],
  label: null, color: null, fill: null, ...extra,
});
const line = (points, extra = {}) => ({
  id: 'l', kind: 'line', points, label: null, color: null, fill: null, ...extra,
});
const poly = (points, extra = {}) => ({
  id: 'g', kind: 'polygon', points, label: null, color: null, fill: null, ...extra,
});

const arcs = (ctx) => ctx.ops.filter(([m]) => m === 'arc');

test('no shapes: nothing is drawn', () => {
  const ctx = mockCtx();
  assert.equal(drawAnnotations(ctx, { transform: T, shapes: [] }), 0);
  assert.equal(ctx.ops.length, 0);
});

test('culling: only shapes whose bbox intersects the canvas are drawn', () => {
  const ctx = mockCtx();
  const n = drawAnnotations(ctx, {
    transform: T,
    shapes: [
      pt(0, 0),
      pt(1000, 1000),
      line([[-5000, 0], [-4000, 0]]),
      poly([[[5000, 5000], [5100, 5000], [5100, 5100]]]),
    ],
  });
  assert.equal(n, 1);
  assert.equal(arcs(ctx).length, 2); // halo + body of the single point
});

test('culling margin: a point just outside the edge is still drawn', () => {
  const ctx = mockCtx();
  // canvas edge is at level-0 x = 256; the point is 2 px outside, within
  // the screen-constant margin, so its halo is partially visible
  drawAnnotations(ctx, { transform: T, shapes: [pt(258, 0)] });
  assert.equal(arcs(ctx).length, 2);
});

test('point: white halo, body fill-else-color, label with text halo', () => {
  const ctx = mockCtx();
  const n = drawAnnotations(ctx, {
    transform: T,
    shapes: [pt(0, 0, { label: 'A', fill: 'lime', color: 'red' })],
  });
  assert.equal(n, 1);
  const a = arcs(ctx);
  // l0 (0,0) under T -> screen (256, 256)
  assert.deepEqual(a[0].slice(1, 4), [256, 256, POINT_RADIUS + HALO_EXTRA]);
  assert.deepEqual(a[1].slice(1, 4), [256, 256, POINT_RADIUS]);
  const fills = ctx.ops.filter(([m]) => m === 'set:fillStyle').map((o) => o[1]);
  assert.deepEqual(fills, ['#ffffff', 'lime', 'red']); // halo, body, label
  const strokeTexts = ctx.ops.filter(([m]) => m === 'strokeText');
  const texts = ctx.ops.filter(([m]) => m === 'fillText');
  assert.equal(strokeTexts.length, 1);
  assert.equal(texts.length, 1);
  // label sits right of the point, vertically centred on it
  assert.deepEqual(texts[0].slice(1), ['A', 256 + POINT_RADIUS + 2, 256]);
});

test('line: path stroked at 1.5 px in color (default black)', () => {
  const ctx = mockCtx();
  drawAnnotations(ctx, {
    transform: T,
    shapes: [line([[-10, -10], [0, 0], [10, 10]], { color: 'blue' })],
  });
  const moves = ctx.ops.filter(([m]) => m === 'moveTo');
  const lines = ctx.ops.filter(([m]) => m === 'lineTo');
  assert.equal(moves.length, 1);
  assert.equal(lines.length, 2);
  assert.deepEqual(moves[0].slice(1), [246, 246]);
  assert.deepEqual(lines[1].slice(1), [266, 266]);
  assert.equal(ctx.state.lineWidth, STROKE_WIDTH);
  assert.equal(ctx.state.strokeStyle, 'blue');
  assert.equal(ctx.ops.filter(([m]) => m === 'stroke').length, 1);
  // lines are open: no closing segment back to the first point
  assert.equal(ctx.ops.filter(([m]) => m === 'closePath').length, 0);
  const ctx2 = mockCtx();
  drawAnnotations(ctx2, { transform: T, shapes: [line([[0, 0], [5, 5]])] });
  assert.equal(ctx2.state.strokeStyle, '#000000');
});

test('polygon: evenodd fill when fill set, outline stroked; transparent = no fill', () => {
  const rings = [[[-10, -10], [10, -10], [10, 10], [-10, 10]]];
  const ctx = mockCtx();
  drawAnnotations(ctx, {
    transform: T,
    shapes: [poly(rings, { fill: 'rgba(255,0,0,0.2)', color: 'red' })],
  });
  const fills = ctx.ops.filter(([m]) => m === 'fill');
  assert.equal(fills.length, 1);
  assert.equal(fills[0][1], 'evenodd');
  assert.equal(ctx.state.fillStyle, 'rgba(255,0,0,0.2)');
  assert.equal(ctx.state.strokeStyle, 'red');
  // rings are stored open; the outline closes the last -> first segment
  assert.equal(ctx.ops.filter(([m]) => m === 'closePath').length, 1);

  const ctx2 = mockCtx();
  drawAnnotations(ctx2, { transform: T, shapes: [poly(rings)] });
  assert.equal(ctx2.ops.filter(([m]) => m === 'fill').length, 0);
  assert.equal(ctx2.state.strokeStyle, '#000000');
});

test('polygon holes: every ring is traced (moveTo per ring)', () => {
  const rings = [
    [[-10, -10], [10, -10], [10, 10], [-10, 10]],
    [[-5, -5], [5, -5], [5, 5], [-5, 5]],
  ];
  const ctx = mockCtx();
  drawAnnotations(ctx, {
    transform: T,
    shapes: [poly(rings, { fill: 'rgba(0,0,255,0.3)' })],
  });
  assert.equal(ctx.ops.filter(([m]) => m === 'moveTo').length, 2);
  assert.equal(ctx.ops.filter(([m]) => m === 'fill')[0][1], 'evenodd');
  // outer ring AND hole are each closed (holes cut through, outlines whole)
  assert.equal(ctx.ops.filter(([m]) => m === 'closePath').length, 2);
});

test('styling is screen-constant under zoom; positions rescale', () => {
  // l0 (4, 4) stays in-canvas at both zooms (t8 shows l0 [-32, 32])
  const t1 = { cx: 0, cy: 0, zoom: 1, canvasW: 512, canvasH: 512 };
  const t8 = { cx: 0, cy: 0, zoom: 8, canvasW: 512, canvasH: 512 };
  const shapes = [pt(4, 4), line([[0, 0], [50, 50]])];
  const c1 = mockCtx();
  drawAnnotations(c1, { transform: t1, shapes });
  const c8 = mockCtx();
  drawAnnotations(c8, { transform: t8, shapes });
  const r1 = arcs(c1).map((o) => o[3]);
  const r8 = arcs(c8).map((o) => o[3]);
  assert.deepEqual(r1, r8); // same screen radius at both zooms
  assert.equal(c1.state.lineWidth, c8.state.lineWidth);
  assert.deepEqual(arcs(c1)[0].slice(1, 3), [260, 260]); // 4 + 256
  assert.deepEqual(arcs(c8)[0].slice(1, 3), [288, 288]); // 4*8 + 256
});

test('alpha sets globalAlpha for the whole layer; save/restore around it', () => {
  const ctx = mockCtx();
  drawAnnotations(ctx, { transform: T, shapes: [pt(0, 0)], alpha: 0.25 });
  assert.equal(ctx.state.globalAlpha, 0.25);
  assert.equal(ctx.ops[0][0], 'save');
  assert.equal(ctx.ops[ctx.ops.length - 1][0], 'restore');
  const ctx2 = mockCtx();
  drawAnnotations(ctx2, { transform: T, shapes: [pt(0, 0)] }); // default 1
  assert.equal(ctx2.state.globalAlpha, 1);
});
