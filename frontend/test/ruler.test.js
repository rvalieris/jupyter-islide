/**
 * Ruler measurement tests (node --test): the state machine, the length
 * formatting, and the ruler renderer with a mock canvas 2D context
 * (no DOM).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  RULER_COLOR, RULER_LABEL_FONT, RULER_STROKE_WIDTH,
  drawRuler, formatMeasure, lineLength, rulerEvent, rulerInit,
} from '../ruler.js';

// screen transform: level-0 identity, 512x512 canvas -> l0 (x,y) => (x+256, y+256)
const T = { cx: 0, cy: 0, zoom: 1, canvasW: 512, canvasH: 512 };
// A 400 level-0 px segment (screen: (256,256) to (656,256) under T).
const LINE = [[0, 0], [400, 0]];

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
    stroke: rec('stroke'),
    fillText: rec('fillText'),
    strokeText: rec('strokeText'),
  };
  for (const k of ['fillStyle', 'strokeStyle', 'lineWidth', 'lineJoin',
                   'lineCap', 'font', 'textBaseline']) {
    Object.defineProperty(ctx, k, {
      get: () => state[k],
      set: (v) => { state[k] = v; ops.push(['set:' + k, v]); },
    });
  }
  return ctx;
}

// ------------------------------------------------------------ state machine
test('init: inactive, no line', () => {
  assert.deepEqual(rulerInit(), { active: false, line: null });
});

test('toggle enters the mode; a second toggle exits (line cleared either way)',
  () => {
    let s = rulerInit();
    s = rulerEvent(s, { type: 'toggle' }).state;
    assert.deepEqual(s, { active: true, line: null });
    s = rulerEvent(s, { type: 'begin', x: 1, y: 2 }).state;
    s = rulerEvent(s, { type: 'move', x: 9, y: 8 }).state;
    assert.notEqual(s.line, null);
    s = rulerEvent(s, { type: 'toggle' }).state;
    assert.deepEqual(s, { active: false, line: null });
  });

test('begin drops the line at the press point (level-0 px)', () => {
  let s = rulerEvent(rulerInit(), { type: 'toggle' }).state;
  s = rulerEvent(s, { type: 'begin', x: 3, y: 4 }).state;
  assert.deepEqual(s.line, [[3, 4], [3, 4]]);
  // a new begin replaces the current measurement
  s = rulerEvent(s, { type: 'begin', x: 5, y: 6 }).state;
  assert.deepEqual(s.line, [[5, 6], [5, 6]]);
});

test('begin/move are no-ops when inactive or on non-finite coordinates',
  () => {
    const s = rulerInit();
    assert.equal(rulerEvent(s, { type: 'begin', x: 1, y: 2 }).state, s);
    assert.equal(rulerEvent(s, { type: 'move', x: 1, y: 2 }).state, s);
    const a = rulerEvent(s, { type: 'toggle' }).state;
    assert.equal(rulerEvent(a, { type: 'begin', x: NaN, y: 1 }).state, a);
    assert.equal(rulerEvent(a, { type: 'move', x: 5, y: 6 }).state, a);
  });

test('move keeps the start point and follows the cursor at the far end',
  () => {
    let s = rulerEvent(rulerInit(), { type: 'toggle' }).state;
    s = rulerEvent(s, { type: 'begin', x: 1, y: 2 }).state;
    s = rulerEvent(s, { type: 'move', x: 7, y: 9 }).state;
    assert.deepEqual(s.line, [[1, 2], [7, 9]]);
    // move with no line yet (a press-less drag) is a no-op
    const b = rulerEvent(rulerInit(), { type: 'toggle' }).state;
    assert.equal(rulerEvent(b, { type: 'move', x: 3, y: 4 }).state, b);
  });

test('clear drops the line (only while active)', () => {
  let s = rulerEvent(rulerInit(), { type: 'toggle' }).state;
  s = rulerEvent(s, { type: 'begin', x: 0, y: 0 }).state;
  s = rulerEvent(s, { type: 'clear' }).state;
  assert.equal(s.line, null);
  assert.equal(s.active, true);
  const c = rulerInit();
  assert.equal(rulerEvent(c, { type: 'clear' }).state, c);
});

test('cancel exits the mode and clears the line', () => {
  let s = rulerEvent(rulerInit(), { type: 'toggle' }).state;
  s = rulerEvent(s, { type: 'begin', x: 0, y: 0 }).state;
  s = rulerEvent(s, { type: 'cancel' }).state;
  assert.deepEqual(s, { active: false, line: null });
  const c = rulerInit();
  assert.equal(rulerEvent(c, { type: 'cancel' }).state, c);
});

// ------------------------------------------------------------ measurement
test('lineLength: Euclidean level-0 distance', () => {
  assert.equal(lineLength([[0, 0], [300, 400]]), 500);
  assert.equal(lineLength([[1, 1], [1, 1]]), 0);
});

test('lineLength: null / malformed / non-finite input -> 0', () => {
  assert.equal(lineLength(null), 0);
  assert.equal(lineLength(undefined), 0);
  assert.equal(lineLength([[1, 2]]), 0);
  assert.equal(lineLength([[1, 2], [3]]), 0);
  assert.equal(lineLength([[1, 2], [NaN, 3]]), 0);
  assert.equal(lineLength([null, null]), 0);
});

test('formatMeasure: µm and px when mpp is known', () => {
  assert.equal(formatMeasure(500, 0.5), '250 µm · 500 px');
  assert.equal(formatMeasure(0.05, 0.25), '0.0125 µm · 0.05 px');
  assert.equal(formatMeasure(0, 0.5), '0 µm · 0 px');
});

test('formatMeasure: px only without mpp (or a non-positive mpp)', () => {
  assert.equal(formatMeasure(500, null), '500 px');
  assert.equal(formatMeasure(500, undefined), '500 px');
  assert.equal(formatMeasure(500, 0), '500 px');
  assert.equal(formatMeasure(500, NaN), '500 px');
});

// ---------------------------------------------------------------- rendering
test('drawRuler: line, end ticks, and the µm·px label', () => {
  const ctx = mockCtx();
  const label = drawRuler(ctx, { transform: T, line: LINE, mpp: 0.5 });
  assert.equal(label, '200 µm · 400 px');
  // Screen endpoints under T: (256,256) to (656,256). Two perpendicular
  // end ticks (each a moveTo+lineTo) and the measurement line itself.
  const moves = ctx.ops.filter(([m]) => m === 'moveTo');
  const lines = ctx.ops.filter(([m]) => m === 'lineTo');
  assert.equal(moves.length, 3);
  assert.equal(lines.length, 3);
  // Ticks: vertical (the line is horizontal), centered on each end.
  assert.deepEqual(moves[0], ['moveTo', 256, 260]);
  assert.deepEqual(lines[0], ['lineTo', 256, 252]);
  assert.deepEqual(moves[1], ['moveTo', 656, 260]);
  assert.deepEqual(lines[1], ['lineTo', 656, 252]);
  assert.deepEqual(moves[2], ['moveTo', 256, 256]);
  assert.deepEqual(lines[2], ['lineTo', 656, 256]);
  // Midpoint (456,256) offset (8,-8) -> (464,248): white halo stroke +
  // brand-color fill.
  assert.deepEqual(
    ctx.ops.filter(([m]) => m === 'strokeText'),
    [['strokeText', '200 µm · 400 px', 464, 248]]);
  assert.deepEqual(
    ctx.ops.filter(([m]) => m === 'fillText'),
    [['fillText', '200 µm · 400 px', 464, 248]]);
  assert.equal(ctx.state.font, RULER_LABEL_FONT);
  assert.equal(ctx.state.strokeStyle, '#ffffff');
  assert.equal(ctx.state.fillStyle, RULER_COLOR);
  assert.equal(ctx.state.lineWidth, 3); // the halo reuses lineWidth
});

test('drawRuler: the label follows the level-0 length, not the screen',
  () => {
    // zoom 2: screen endpoints (256,256) to (1056,256) — the length is
    // still 400 level-0 px, so the label is unchanged.
    const t2 = { cx: 0, cy: 0, zoom: 2, canvasW: 512, canvasH: 512 };
    const ctx = mockCtx();
    const label = drawRuler(ctx, { transform: t2, line: LINE, mpp: 0.5 });
    assert.equal(label, '200 µm · 400 px');
    assert.deepEqual(ctx.ops.filter(([m]) => m === 'fillText'),
      [['fillText', '200 µm · 400 px', 656 + 8, 256 - 8]]);
  });

test('drawRuler: the px-only fallback without mpp', () => {
  const ctx = mockCtx();
  const label = drawRuler(ctx, { transform: T, line: LINE, mpp: null });
  assert.equal(label, '400 px');
});

test('drawRuler: null or zero-length line draws nothing', () => {
  const ctx = mockCtx();
  assert.equal(drawRuler(ctx, { transform: T, line: null, mpp: 0.5 }), '');
  assert.equal(ctx.ops.length, 0);
  const ctx2 = mockCtx();
  assert.equal(
    drawRuler(ctx2, { transform: T, line: [[3, 4], [3, 4]], mpp: 0.5 }), '');
  assert.equal(ctx2.ops.length, 0);
});
