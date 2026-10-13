/**
 * Unit tests for the pure viewport/tile math (node --test).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import * as math from '../tilemath.js';

const META = {
  dimensions: [37382, 73222],
  level_count: 8,
  level_downsamples: [1, 2, 4, 8, 16, 32, 64, 128],
  level_dimensions: null,
  mpp: 0.25,
  vendor: 'test',
};

const T = math.makeTransform(18691, 36611, 1.0, 960, 540);

test('fitZoom matches the Python fit_zoom', () => {
  const fit = math.fitZoom(META, 960, 540);
  assert.ok(fit > 0);
  assert.ok(Math.abs(fit - Math.min(960 / 37382, 540 / 73222)) < 1e-15);
});

test('l0ToScreen / screenToL0 round-trip', () => {
  for (const [lx, ly] of [[0, 0], [18691, 36611], [37382, 73222], [-5, 12.5]]) {
    const [sx, sy] = math.l0ToScreen(T, lx, ly);
    const [bx, by] = math.screenToL0(T, sx, sy);
    assert.ok(Math.abs(bx - lx) < 1e-9, `x: ${bx} != ${lx}`);
    assert.ok(Math.abs(by - ly) < 1e-9, `y: ${by} != ${ly}`);
  }
});

test('screen center maps to the viewport center', () => {
  const [sx, sy] = math.l0ToScreen(T, T.cx, T.cy);
  assert.equal(sx, 480);
  assert.equal(sy, 270);
});

test('zoomAtCursor keeps the slide point under the cursor fixed', () => {
  for (const [sx, sy, factor] of [[100, 50, 2], [480, 270, 4], [959, 539, 0.5]]) {
    const next = math.zoomAtCursor(T, factor, sx, sy, 1e-9, 16);
    const [lx, ly] = math.screenToL0(T, sx, sy); // point under cursor before
    const [bx, by] = math.l0ToScreen(next, lx, ly); // after reproject
    assert.ok(Math.abs(bx - sx) < 1e-6, `x drift: ${bx} != ${sx}`);
    assert.ok(Math.abs(by - sy) < 1e-6, `y drift: ${by} != ${sy}`);
    assert.ok(Math.abs(next.zoom - T.zoom * factor) < 1e-12);
  }
});

test('zoomAtCursor clamps to [minZoom, maxZoom]', () => {
  const next = math.zoomAtCursor(T, 1e6, 100, 100, 0.01, 16);
  assert.equal(next.zoom, 16);
  const far = math.zoomAtCursor(T, 1e-9, 100, 100, 0.01, 16);
  assert.equal(far.zoom, 0.01);
});

test('panTransform: dragging right moves the view left on the slide', () => {
  const next = math.panTransform(T, 100, 50);
  assert.ok(Math.abs(next.cx - (T.cx - 100 / T.zoom)) < 1e-12);
  assert.ok(Math.abs(next.cy - (T.cy - 50 / T.zoom)) < 1e-12);
  assert.equal(next.zoom, T.zoom);
});

test('tileScreenRect: level-space crop -> screen rect (integer ds)', () => {
  // geo [level, ox, oy, cw, ch] in level px; ds=2 at level 1.
  const t = math.makeTransform(0, 0, 1.0, 100, 100);
  const r = math.tileScreenRect(t, [1, 10, 5, 256, 256], [1, 2]);
  // l0 origin = (20, 10); screen = l0 - center (0,0) + 50
  assert.ok(Math.abs(r.left - 70) < 1e-12);
  assert.ok(Math.abs(r.top - 60) < 1e-12);
  assert.ok(Math.abs(r.w - 512) < 1e-12);
  assert.ok(Math.abs(r.h - 512) < 1e-12);
});

test('tileScreenRect matches the screen-box reference math', () => {
  // Reference: left = (c0x*ds - x0)*z where x0 = cx - canvasW/(2z)
  const t = math.makeTransform(18691, 36611, 0.5, 960, 540);
  const [level, ox, oy, cw, ch] = [2, 12288, 16384, 256, 256];
  const ds = 4;
  const r = math.tileScreenRect(t, [level, ox, oy, cw, ch], [1, 2, 4]);
  const x0 = t.cx - t.canvasW / (2 * t.zoom);
  const y0 = t.cy - t.canvasH / (2 * t.zoom);
  assert.ok(Math.abs(r.left - (ox * ds - x0) * t.zoom) < 1e-9);
  assert.ok(Math.abs(r.top - (oy * ds - y0) * t.zoom) < 1e-9);
  assert.ok(Math.abs(r.w - cw * ds * t.zoom) < 1e-9);
});

test('visibleTiles selects exactly the on-canvas tiles', () => {
  const t = math.makeTransform(256, 256, 1.0, 512, 512);
  // 256px cells around the origin, level 0.
  const geo = {};
  for (let ty = -1; ty <= 2; ty++) {
    for (let tx = -1; tx <= 2; tx++) {
      geo[`0:${tx}:${ty}`] = [0, tx * 256, ty * 256, 256, 256];
    }
  }
  const visible = math.visibleTiles(t, geo, [1]).map((v) => v.key).sort();
  // canvas covers l0 [0,512]x[0,512] -> cells (0,0),(1,0),(0,1),(1,1)
  // plus edge-touching (2,0)? no: cell x=512..768 starts exactly at the
  // right edge -> left=512 < 512+margin is false... left == canvasW -> excluded
  assert.deepEqual(visible, ['0:0:0', '0:0:1', '0:1:0', '0:1:1']);
});

test('viewportL0Bbox inverts the transform', () => {
  const bbox = math.viewportL0Bbox(T);
  assert.ok(Math.abs(bbox.x0 - (T.cx - 480)) < 1e-9);
  assert.ok(Math.abs(bbox.y1 - (T.cy + 270)) < 1e-9);
});

test('viewport/transform wire form round-trip', () => {
  // The wire form carries no canvas height: the `canvas_h` trait is the
  // source of truth, so the transform takes it as a parameter and the
  // round-trip sends it back.
  const vp = { cx: 1, cy: 2, zoom: 3, canvas_w: 4 };
  const t = math.viewportToTransform(vp, 5);
  assert.equal(t.canvasH, 5);
  assert.deepEqual(math.transformToViewport(t), vp);
});

// ------------------------------------------------------------------ tile levels
test('selectLevel mirrors Python select_level (smallest ds >= 1/zoom)', () => {
  const ds = [1, 2, 4, 8];
  assert.equal(math.selectLevel(ds, 8), 0);   // need 1/8
  assert.equal(math.selectLevel(ds, 2), 0);   // need 1/2
  assert.equal(math.selectLevel(ds, 0.5), 1); // need 2
  assert.equal(math.selectLevel(ds, 0.25), 2); // need 4
  assert.equal(math.selectLevel(ds, 0.125), 3); // need 8
  // below the coarsest level: clamps to the last level
  assert.equal(math.selectLevel(ds, 0.0625), 3);
  assert.equal(math.selectLevel(ds, 0.001), 3);
  // degenerate: zoom 0 (need = Infinity) -> coarsest
  assert.equal(math.selectLevel(ds, 0), 3);
});

test('visibleTiles reports each tile level (cross-fade input)', () => {
  const t = math.makeTransform(256, 256, 1.0, 512, 512);
  const geo = {
    '0:0:0': [0, 0, 0, 256, 256],
    '1:0:0': [1, 0, 0, 512, 512], // ds 2: covers l0 [0..1024]^2
  };
  const levels = new Map(
    math.visibleTiles(t, geo, [1, 2]).map((v) => [v.key, v.level]),
  );
  assert.equal(levels.get('0:0:0'), 0);
  assert.equal(levels.get('1:0:0'), 1);
});


// --- wheel zoom: deltaMode normalisation ------------------------------------

const MAX_STEP = Math.exp(math.WHEEL_MAX_DELTA_PX * math.WHEEL_ZOOM_PER_PX); // ≈ 2.23

test('wheelZoomFactor: one notch zooms the same in Chrome and Firefox', () => {
  // Chrome/Safari report a mouse-wheel notch in pixels (~100 px); Firefox
  // reports the same notch as 3 lines (deltaMode 1).
  const chrome = math.wheelZoomFactor(100, 0);
  const firefox = math.wheelZoomFactor(3, 1);
  assert.ok(chrome < 1 && firefox < 1); // scrolling down zooms out
  assert.ok(
    Math.abs(chrome - firefox) < 0.01,
    `notch mismatch: chrome ${chrome}, firefox ${firefox}`,
  );
  // The old `exp(-deltaY * 0.002)` gave Firefox ~33x less zoom per notch.
  const oldFirefox = Math.exp(-3 * math.WHEEL_ZOOM_PER_PX);
  assert.ok(Math.log(oldFirefox) / Math.log(chrome) < 0.04);
  // Zoom in is the reciprocal of zoom out, in both browsers.
  assert.ok(Math.abs(math.wheelZoomFactor(-100, 0) - 1 / chrome) < 1e-12);
  assert.ok(Math.abs(math.wheelZoomFactor(-3, 1) - 1 / firefox) < 1e-12);
});

test('wheelZoomFactor: page mode scales by the canvas height', () => {
  const onePage = math.wheelZoomFactor(1, 2, 540);
  // 540 px of content, capped to WHEEL_MAX_DELTA_PX: a page notch is a big
  // gesture, but it is still bounded.
  assert.ok(Math.abs(onePage - 1 / MAX_STEP) < 1e-12);
  assert.ok(onePage < 1);
  // No canvas height available: the fallback page is bounded too.
  assert.ok(math.wheelZoomFactor(1, 2) >= 1 / MAX_STEP - 1e-12);
});

test('wheelZoomFactor: capped so one flick cannot teleport the zoom', () => {
  const cases = [[1e6, 0], [-1e6, 0], [1e4, 1], [-1e4, 1], [1e3, 2], [40, 1]];
  for (const [dy, mode] of cases) {
    const f = math.wheelZoomFactor(dy, mode);
    assert.ok(
      f <= MAX_STEP + 1e-12 && f >= 1 / MAX_STEP - 1e-12,
      `delta ${dy} mode ${mode}: ${f}`,
    );
  }
  // Ordinary gestures sit well inside the cap; it only shapes flicks.
  assert.ok(math.wheelZoomFactor(120, 0) > 1 / MAX_STEP);
  assert.ok(math.wheelZoomFactor(120, 0) < MAX_STEP);
  assert.ok(math.wheelZoomFactor(0.5, 1) > 1 / MAX_STEP); // a trackpad tick
});

test('wheelZoomFactor: a delta that is not a number zooms by nothing', () => {
  for (const dy of [0, undefined, null, NaN, Infinity, -Infinity, '100']) {
    assert.equal(math.wheelZoomFactor(dy, 0), 1, `delta ${String(dy)}`);
  }
});

test('wheelZoomFactor feeds zoomAtCursor identically across browsers', () => {
  const [minZoom, maxZoom] = [math.fitZoom(META, 960, 540) / 4, 16];
  const chrome = math.zoomAtCursor(T, math.wheelZoomFactor(100, 0), 480, 270, minZoom, maxZoom);
  const firefox = math.zoomAtCursor(T, math.wheelZoomFactor(3, 1), 480, 270, minZoom, maxZoom);
  assert.ok(Math.abs(chrome.zoom / firefox.zoom - 1) < 5e-3); // < 0.5 % apart
  // Cursor at the canvas center: the center stays put in both.
  assert.ok(Math.abs(chrome.cx - firefox.cx) < 1e-9);
  assert.ok(Math.abs(chrome.cy - firefox.cy) < 1e-9);
});
