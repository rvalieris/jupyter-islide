/**
 * Annotation overlay tests with a mock canvas 2D context (node --test).
 *
 * M3.5: the overlay consumes the canonical annotation document (a GeoJSON
 * FeatureCollection of {id, geometry, properties} features, DESIGN.md
 * §6.3), not a flat shape list.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  drawAnnotations,
  drawVertexHandles,
  featurePositions,
  hitTest,
  hitTestVertex,
  withMovedVertex,
  STROKE_WIDTH,
  POINT_RADIUS,
  HALO_EXTRA,
  POINT_HIT_RADIUS,
  LINE_HIT_TOLERANCE,
  SELECTED_COLOR,
  SELECTED_STROKE_WIDTH,
  VERTEX_HANDLE_RADIUS,
  VERTEX_PICK_RADIUS,
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

const feat = (geometry, properties = {}, id = 'f') => ({
  type: 'Feature', id, geometry, properties,
});
const doc = (...features) => ({ type: 'FeatureCollection', features: [...features] });
const point = (x, y) => ({ type: 'Point', coordinates: [x, y] });

const arcs = (ctx) => ctx.ops.filter(([m]) => m === 'arc');

test('no features: nothing is drawn', () => {
  const ctx = mockCtx();
  assert.equal(
    drawAnnotations(ctx, { transform: T, annotations: doc() }), 0);
  assert.equal(ctx.ops.length, 0);
});

test('malformed documents draw nothing and do not throw', () => {
  for (const annotations of [undefined, null, {}, [1, 2],
                             { features: 'nope' }]) {
    const ctx = mockCtx();
    assert.equal(drawAnnotations(ctx, { transform: T, annotations }), 0);
  }
});

test('culling: only features whose bbox intersects the canvas are drawn', () => {
  const ctx = mockCtx();
  const n = drawAnnotations(ctx, {
    transform: T,
    annotations: doc(
      feat(point(0, 0)),
      feat(point(1000, 1000)),
      feat({ type: 'LineString', coordinates: [[-5000, 0], [-4000, 0]] }),
      feat({
        type: 'Polygon',
        coordinates: [[[5000, 5000], [5100, 5000], [5100, 5100]]],
      }),
    ),
  });
  assert.equal(n, 1);
  assert.equal(arcs(ctx).length, 2); // halo + body of the single point
});

test('culling margin: a point just outside the edge is still drawn', () => {
  const ctx = mockCtx();
  // canvas edge is at level-0 x = 256; the point is 2 px outside, within
  // the screen-constant margin, so its halo is partially visible
  drawAnnotations(ctx, { transform: T, annotations: doc(feat(point(258, 0))) });
  assert.equal(arcs(ctx).length, 2);
});

test('point: white halo, body fill-else-color, label with text halo', () => {
  const ctx = mockCtx();
  const n = drawAnnotations(ctx, {
    transform: T,
    annotations: doc(feat(point(0, 0), { label: 'A', fill: 'lime', color: 'red' })),
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

test('multi point: one marker per position, label at the first', () => {
  const ctx = mockCtx();
  const n = drawAnnotations(ctx, {
    transform: T,
    annotations: doc(feat(
      { type: 'MultiPoint', coordinates: [[0, 0], [10, 10]] },
      { label: 'm', fill: 'blue' },
    )),
  });
  assert.equal(n, 1);
  assert.equal(arcs(ctx).length, 4); // halo + body per position
  const texts = ctx.ops.filter(([m]) => m === 'fillText');
  assert.equal(texts.length, 1);
  assert.deepEqual(texts[0].slice(1), ['m', 256 + POINT_RADIUS + 2, 256]);
});

test('line: path stroked at 1.5 px in color (default black)', () => {
  const ctx = mockCtx();
  drawAnnotations(ctx, {
    transform: T,
    annotations: doc(feat(
      { type: 'LineString', coordinates: [[-10, -10], [0, 0], [10, 10]] },
      { color: 'blue' },
    )),
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
  drawAnnotations(ctx2, {
    transform: T,
    annotations: doc(feat({ type: 'LineString', coordinates: [[0, 0], [5, 5]] })),
  });
  assert.equal(ctx2.state.strokeStyle, '#000000');
});

test('polygon: evenodd fill when fill set, outline stroked; transparent = no fill', () => {
  const rings = [[[-10, -10], [10, -10], [10, 10], [-10, 10]]];
  const ctx = mockCtx();
  drawAnnotations(ctx, {
    transform: T,
    annotations: doc(feat({ type: 'Polygon', coordinates: rings },
                          { fill: 'rgba(255,0,0,0.2)', color: 'red' })),
  });
  const fills = ctx.ops.filter(([m]) => m === 'fill');
  assert.equal(fills.length, 1);
  assert.equal(fills[0][1], 'evenodd');
  assert.equal(ctx.state.fillStyle, 'rgba(255,0,0,0.2)');
  assert.equal(ctx.state.strokeStyle, 'red');
  // rings are stored open; the outline closes the last -> first segment
  assert.equal(ctx.ops.filter(([m]) => m === 'closePath').length, 1);

  const ctx2 = mockCtx();
  drawAnnotations(ctx2, {
    transform: T,
    annotations: doc(feat({ type: 'Polygon', coordinates: rings })),
  });
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
    annotations: doc(feat({ type: 'Polygon', coordinates: rings },
                          { fill: 'rgba(0,0,255,0.3)' })),
  });
  assert.equal(ctx.ops.filter(([m]) => m === 'moveTo').length, 2);
  assert.equal(ctx.ops.filter(([m]) => m === 'fill')[0][1], 'evenodd');
  // outer ring AND hole are each closed (holes cut through, outlines whole)
  assert.equal(ctx.ops.filter(([m]) => m === 'closePath').length, 2);
});

test('multi polygon: islands flatten into one evenodd ring set', () => {
  const islands = [
    [[-10, -10], [10, -10], [10, 10], [-10, 10]],
    [[-5, -5], [5, -5], [5, 5], [-5, 5]],
  ].map((outer) => [outer]);
  const ctx = mockCtx();
  drawAnnotations(ctx, {
    transform: T,
    annotations: doc(feat({ type: 'MultiPolygon', coordinates: islands },
                          { fill: 'rgba(0,255,0,0.3)' })),
  });
  const fills = ctx.ops.filter(([m]) => m === 'fill');
  assert.equal(fills.length, 1);
  assert.equal(fills[0][1], 'evenodd');
  assert.equal(ctx.ops.filter(([m]) => m === 'moveTo').length, 2);
  assert.equal(ctx.ops.filter(([m]) => m === 'closePath').length, 2);
  assert.equal(ctx.ops.filter(([m]) => m === 'stroke').length, 1);
});

test('malformed features are skipped; the rest still draw', () => {
  const ctx = mockCtx();
  const n = drawAnnotations(ctx, {
    transform: T,
    annotations: doc(
      { type: 'Feature', id: 'bad1',
        geometry: { type: 'Point', coordinates: 'nope' }, properties: {} },
      { id: 'bad2', geometry: null },
      feat(point(0, 0)),
    ),
  });
  assert.equal(n, 1);
  assert.equal(arcs(ctx).length, 2);
});

test('styling is screen-constant under zoom; positions rescale', () => {
  // l0 (4, 4) stays in-canvas at both zooms (t8 shows l0 [-32, 32])
  const t1 = { cx: 0, cy: 0, zoom: 1, canvasW: 512, canvasH: 512 };
  const t8 = { cx: 0, cy: 0, zoom: 8, canvasW: 512, canvasH: 512 };
  const features = [
    feat(point(4, 4)),
    feat({ type: 'LineString', coordinates: [[0, 0], [50, 50]] }),
  ];
  const c1 = mockCtx();
  drawAnnotations(c1, { transform: t1, annotations: doc(...features) });
  const c8 = mockCtx();
  drawAnnotations(c8, { transform: t8, annotations: doc(...features) });
  const r1 = arcs(c1).map((o) => o[3]);
  const r8 = arcs(c8).map((o) => o[3]);
  assert.deepEqual(r1, r8); // same screen radius at both zooms
  assert.equal(c1.state.lineWidth, c8.state.lineWidth);
  assert.deepEqual(arcs(c1)[0].slice(1, 3), [260, 260]); // 4 + 256
  assert.deepEqual(arcs(c8)[0].slice(1, 3), [288, 288]); // 4*8 + 256
});

test('alpha sets globalAlpha for the whole layer; save/restore around it', () => {
  const ctx = mockCtx();
  drawAnnotations(ctx, { transform: T, annotations: doc(feat(point(0, 0))),
                         alpha: 0.25 });
  assert.equal(ctx.state.globalAlpha, 0.25);
  assert.equal(ctx.ops[0][0], 'save');
  assert.equal(ctx.ops[ctx.ops.length - 1][0], 'restore');
  const ctx2 = mockCtx();
  drawAnnotations(ctx2, { transform: T, annotations: doc(feat(point(0, 0))) });
  assert.equal(ctx2.state.globalAlpha, 1);
});

// ---------------------------------------------------------------- M4 (selection)

test('hitTest: empty/invalid documents are misses', () => {
  for (const annotations of [undefined, null, {}, { features: [] },
                             { features: 'nope' }]) {
    assert.equal(hitTest(annotations, T, 256, 256), null);
  }
});

test('hitTest: point hit within POINT_HIT_RADIUS, miss beyond', () => {
  const d = doc(feat(point(0, 0), {}, 'p'));
  assert.equal(hitTest(d, T, 256, 256), 'p');
  // the distance test is inclusive of the exact radius
  assert.equal(hitTest(d, T, 256 + POINT_HIT_RADIUS, 256), 'p');
  assert.equal(hitTest(d, T, 256 + POINT_HIT_RADIUS + 1, 256), null);
});

test('hitTest: multipoint — any marker position is a hit', () => {
  const d = doc(feat(
    { type: 'MultiPoint', coordinates: [[0, 0], [10, 10]] }, {}, 'm'));
  assert.equal(hitTest(d, T, 256, 256), 'm');
  assert.equal(hitTest(d, T, 256 + 10, 256 + 10), 'm');
  assert.equal(hitTest(d, T, 256 + 100, 256), null);
});

test('hitTest: line within LINE_HIT_TOLERANCE of any segment', () => {
  // l0 y=0, x in [-50, 50] -> screen y=256, x 206..306
  const d = doc(feat(
    { type: 'LineString', coordinates: [[-50, 0], [50, 0]] }, {}, 'l'));
  assert.equal(hitTest(d, T, 256, 256), 'l');
  assert.equal(hitTest(d, T, 256, 256 + LINE_HIT_TOLERANCE), 'l');
  assert.equal(hitTest(d, T, 256, 256 + LINE_HIT_TOLERANCE + 1), null);
  // 7 level-0 px (= 7 screen px at zoom 1) past the far endpoint: out
  assert.equal(hitTest(d, T, 256 + 50 + LINE_HIT_TOLERANCE + 1, 256), null);
  // a hit near the middle of a slanted segment (not an endpoint)
  const d2 = doc(feat(
    { type: 'LineString', coordinates: [[0, 0], [0, 100]] }, {}, 'v'));
  assert.equal(hitTest(d2, T, 256 + LINE_HIT_TOLERANCE, 256 + 50), 'v');
});

test('hitTest: polygon interior, evenodd holes, outline, miss', () => {
  const rings = [
    [[-50, -50], [50, -50], [50, 50], [-50, 50]],
    [[-25, -25], [25, -25], [25, 25], [-25, 25]], // hole
  ];
  const d = doc(feat({ type: 'Polygon', coordinates: rings }, {}, 'pg'));
  assert.equal(hitTest(d, T, 296, 256), 'pg');   // interior, outside the hole
  assert.equal(hitTest(d, T, 256, 256), null);   // hole centre: evenodd out
  assert.equal(hitTest(d, T, 256, 256 + 50), 'pg'); // on the bottom edge
  assert.equal(hitTest(d, T, 256, 256 + 90), null); // well outside
  // outline tolerance: just beyond STROKE_WIDTH of the bottom edge is out
  assert.equal(hitTest(d, T, 296, 256 + 50 + STROKE_WIDTH + 1), null);
  assert.equal(hitTest(d, T, 296, 256 + 50 + STROKE_WIDTH), 'pg');
});

test('hitTest: multipolygon islands flatten into the hit region', () => {
  const islands = [
    [[[-50, -50], [50, -50], [50, 50], [-50, 50]]],
    [[[100, 100], [150, 100], [150, 150], [100, 150]]],
  ];
  const d = doc(feat({ type: 'MultiPolygon', coordinates: islands }, {}, 'mp'));
  assert.equal(hitTest(d, T, 256 + 125, 256 + 125), 'mp'); // inside island 2
  assert.equal(hitTest(d, T, 256, 256 + 75), null);        // between islands
});

test('hitTest: the topmost (last) feature under the cursor wins', () => {
  const d = doc(
    feat(point(0, 0), {}, 'bottom'),
    feat(point(0, 0), {}, 'top'),
  );
  assert.equal(hitTest(d, T, 256, 256), 'top');
});

test('hitTest: undrawable features are unhittable; the rest are tested', () => {
  const d = doc(
    { type: 'Feature', id: 'bad',
      geometry: { type: 'Point', coordinates: 'nope' }, properties: {} },
    feat(point(0, 0), {}, 'ok'),
  );
  assert.equal(hitTest(d, T, 256, 256), 'ok');
  const d2 = doc({ type: 'Feature', id: 'bad', geometry: null, properties: {} });
  assert.equal(hitTest(d2, T, 256, 256), null);
});

test('drawAnnotations: the selected feature draws last with the accent style',
  () => {
    const ctx = mockCtx();
    const n = drawAnnotations(ctx, {
      transform: T,
      annotations: doc(
        feat(point(0, 0), {}, 'sel'),
        feat(point(20, 20), {}, 'other'),
      ),
      selectedId: 'sel',
    });
    assert.equal(n, 2);
    const a = arcs(ctx);
    // the unselected feature first (halo+body at screen (276, 276)); the
    // selected point after it, plus the accent ring
    assert.deepEqual(a.map((o) => [o[1], o[2], o[3]]), [
      [276, 276, POINT_RADIUS + HALO_EXTRA],
      [276, 276, POINT_RADIUS],
      [256, 256, POINT_RADIUS + HALO_EXTRA],
      [256, 256, POINT_RADIUS],
      [256, 256, POINT_RADIUS + HALO_EXTRA],
    ]);
    // the ring is stroked in the accent color at the accent width
    const stroke = ctx.ops.findIndex(([m]) => m === 'stroke');
    assert.ok(stroke >= 0);
    assert.equal(ctx.ops[stroke - 2][0], 'set:strokeStyle');
    assert.equal(ctx.ops[stroke - 2][1], SELECTED_COLOR);
    assert.equal(ctx.ops[stroke - 1][0], 'set:lineWidth');
    assert.equal(ctx.ops[stroke - 1][1], SELECTED_STROKE_WIDTH);
  });

test('drawAnnotations: a selected line gets the accent stroke and width', () => {
  const ctx = mockCtx();
  drawAnnotations(ctx, {
    transform: T,
    annotations: doc(feat(
      { type: 'LineString', coordinates: [[-10, 0], [10, 0]] },
      { color: 'blue' }, 'l')),
    selectedId: 'l',
  });
  assert.equal(ctx.state.strokeStyle, SELECTED_COLOR);
  assert.equal(ctx.state.lineWidth, SELECTED_STROKE_WIDTH);
});

test('drawAnnotations: a selected polygon keeps its fill; accent outline', () => {
  const rings = [[[-10, -10], [10, -10], [10, 10], [-10, 10]]];
  const ctx = mockCtx();
  drawAnnotations(ctx, {
    transform: T,
    annotations: doc(feat(
      { type: 'Polygon', coordinates: rings },
      { fill: 'rgba(255,0,0,0.4)' }, 'pg')),
    selectedId: 'pg',
  });
  assert.equal(ctx.ops.filter(([m, a]) => m === 'fill' && a === 'evenodd').length, 1);
  assert.equal(ctx.state.fillStyle, 'rgba(255,0,0,0.4)');
  assert.equal(ctx.state.strokeStyle, SELECTED_COLOR);
  assert.equal(ctx.state.lineWidth, SELECTED_STROKE_WIDTH);
});

test('drawAnnotations: selectedId that matches nothing draws normally', () => {
  const ctx = mockCtx();
  drawAnnotations(ctx, {
    transform: T,
    annotations: doc(feat(point(0, 0))),
    selectedId: 'nope',
  });
  assert.equal(arcs(ctx).length, 2); // halo + body only, no ring
  assert.equal(ctx.ops.filter(([m]) => m === 'stroke').length, 0);
});

// ------------------------------------------------------------ M6 (vertex editing)

const PG_HOLE = {
  type: 'Polygon',
  coordinates: [
    [[0, 0], [100, 0], [100, 100], [0, 100]],
    [[20, 20], [40, 20], [40, 40], [20, 40]],
  ],
};
const MPOLY = {
  type: 'MultiPolygon',
  coordinates: [
    [[[0, 0], [100, 0], [100, 100]]],
    [[[200, 200], [300, 200], [300, 300]]],
  ],
};

test('featurePositions: the flat canonical order per geometry type', () => {
  assert.deepEqual(featurePositions(feat(point(1, 2))), [[1, 2]]);
  assert.deepEqual(
    featurePositions(feat({ type: 'MultiPoint', coordinates: [[0, 0], [1, 1]] })),
    [[0, 0], [1, 1]]);
  assert.deepEqual(
    featurePositions(feat({ type: 'LineString', coordinates: [[0, 0], [5, 5]] })),
    [[0, 0], [5, 5]]);
  // polygon: the outer ring, then the holes
  assert.deepEqual(featurePositions(feat(PG_HOLE)), [
    [0, 0], [100, 0], [100, 100], [0, 100],
    [20, 20], [40, 20], [40, 40], [20, 40],
  ]);
  // multipolygon: the islands, in order
  assert.deepEqual(featurePositions(feat(MPOLY)), [
    [0, 0], [100, 0], [100, 100],
    [200, 200], [300, 200], [300, 300],
  ]);
});

test('featurePositions: undrawable features give []', () => {
  assert.deepEqual(featurePositions({
    type: 'Feature', geometry: { type: 'Point', coordinates: 'nope' },
    properties: {},
  }), []);
  assert.deepEqual(featurePositions({ type: 'Feature', geometry: null, properties: {} }), []);
  assert.deepEqual(featurePositions(null), []);
  assert.deepEqual(featurePositions({}), []);
});

test('withMovedVertex: point and linestring; input untouched', () => {
  const d = doc(
    feat(point(1, 2), {}, 'p'),
    feat({ type: 'LineString', coordinates: [[0, 0], [10, 0]] }, {}, 'l'));
  const out = withMovedVertex(d, 'p', 0, 9, 9);
  assert.deepEqual(out.features[0].geometry.coordinates, [9, 9]);
  assert.equal(out.features[1], d.features[1]); // the other feature is shared
  const out2 = withMovedVertex(d, 'l', 1, 10, 5);
  assert.deepEqual(out2.features[1].geometry.coordinates, [[0, 0], [10, 5]]);
  // the input document is never mutated
  assert.deepEqual(d.features[0].geometry.coordinates, [1, 2]);
  assert.deepEqual(d.features[1].geometry.coordinates, [[0, 0], [10, 0]]);
});

test('withMovedVertex: a polygon hole vertex via the flat index across rings', () => {
  const d = doc(feat(PG_HOLE, {}, 'pg'));
  const out = withMovedVertex(d, 'pg', 5, 30, 35);
  const rings = out.features[0].geometry.coordinates;
  assert.deepEqual(rings[0], [[0, 0], [100, 0], [100, 100], [0, 100]]);
  // flat 5 = the hole's second position (flat 4 is its first)
  assert.deepEqual(rings[1], [[20, 20], [30, 35], [40, 40], [20, 40]]);
});

test('withMovedVertex: multipolygon island regrouping keeps the shape', () => {
  const d = doc(feat(MPOLY, {}, 'mp'));
  const out = withMovedVertex(d, 'mp', 4, 250, 260);
  const islands = out.features[0].geometry.coordinates;
  assert.equal(islands.length, 2);
  assert.deepEqual(islands[0], [[[0, 0], [100, 0], [100, 100]]]);
  assert.deepEqual(islands[1], [[[200, 200], [250, 260], [300, 300]]]);
});

test('withMovedVertex: unknown id or index: the same document is returned', () => {
  const d = doc(feat(point(1, 2), {}, 'p'), feat(PG_HOLE, {}, 'pg'));
  assert.equal(withMovedVertex(d, 'nope', 0, 0, 0), d);
  assert.equal(withMovedVertex(d, 'p', 1, 0, 0), d); // a point has one position
  assert.equal(withMovedVertex(d, 'pg', 99, 0, 0), d); // out of range
  assert.equal(withMovedVertex(d, 'pg', -1, 0, 0), d);
  const bad = {}; // malformed document
  assert.equal(withMovedVertex(bad, 'p', 0, 0, 0), bad);
});

test('hitTestVertex: within VERTEX_PICK_RADIUS, nearest first', () => {
  // l0 (0,0)/(100,0) -> screen (256,256)/(356,256): far apart
  const f = feat(
    { type: 'LineString', coordinates: [[0, 0], [100, 0]] }, {}, 'l');
  assert.equal(hitTestVertex(f, T, 256, 256), 0);
  // the distance test is inclusive of the exact radius
  assert.equal(hitTestVertex(f, T, 256 + VERTEX_PICK_RADIUS, 256), 0);
  // beyond both radii: a miss
  assert.equal(hitTestVertex(f, T, 266 + VERTEX_PICK_RADIUS + 1, 256), null);
  // the second vertex hits at its own position
  assert.equal(hitTestVertex(f, T, 256 + 100 + VERTEX_PICK_RADIUS, 256), 1);
  // inside both radii (10 px apart): the nearer vertex wins
  const near = feat(
    { type: 'LineString', coordinates: [[0, 0], [10, 0]] }, {}, 'l2');
  assert.equal(hitTestVertex(near, T, 256 + 8, 256), 1); // 8 from v0, 2 from v1
  assert.equal(hitTestVertex(near, T, 256 + 2, 256), 0); // 2 from v0, 8 from v1
  // a polygon vertex beyond the first ring (flat index 4 = hole vertex)
  const pg = feat(PG_HOLE, {}, 'pg');
  assert.equal(hitTestVertex(pg, T, 256 + 20, 256 + 20), 4);
  assert.equal(hitTestVertex(pg, T, 256 + 20, 256 + 200), null);
  // undrawable feature: never a hit
  const bad = { type: 'Feature', geometry: null, properties: {} };
  assert.equal(hitTestVertex(bad, T, 256, 256), null);
});

test('drawVertexHandles: one handle per position, the grab enlarged', () => {
  const ctx = mockCtx();
  drawVertexHandles(ctx, T, [[0, 0], [10, 0], [0, 10]], 1);
  const a = arcs(ctx);
  assert.equal(a.length, 3);
  assert.deepEqual(a.map((o) => o[3]), [
    VERTEX_HANDLE_RADIUS,
    VERTEX_HANDLE_RADIUS + 1.5,
    VERTEX_HANDLE_RADIUS,
  ]);
  assert.equal(ctx.state.strokeStyle, SELECTED_COLOR);
  assert.deepEqual(a[0].slice(1, 3), [256, 256]); // l0 (0,0) under T
  assert.equal(ctx.ops[0][0], 'save');
  assert.equal(ctx.ops[ctx.ops.length - 1][0], 'restore');
});

test('drawVertexHandles: empty positions draw nothing', () => {
  const ctx = mockCtx();
  drawVertexHandles(ctx, T, [], -1);
  assert.equal(ctx.ops.length, 0);
});
