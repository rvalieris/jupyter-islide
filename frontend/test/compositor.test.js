/**
 * Compositor tests with a mock canvas 2D context (node --test).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { drawScene, drawOverlay } from '../compositor.js';

function mockCtx() {
  const calls = { fill: 0, draw: [] };
  return {
    calls,
    set fillStyle(v) { this._fillStyle = v; },
    get fillStyle() { return this._fillStyle; },
    fillRect(x, y, w, h) { calls.fill += 1; this.lastFill = [x, y, w, h]; },
    drawImage(img, left, top, w, h) { calls.draw.push([img, left, top, w, h]); },
  };
}

const META = {
  dimensions: [37382, 73222],
  level_count: 8,
  level_downsamples: [1, 2, 4, 8, 16, 32, 64, 128],
};

test('drawScene paints the canvas white and draws every visible ready tile', () => {
  const t = { cx: 256, cy: 256, zoom: 1.0, canvasW: 512, canvasH: 512 };
  const geo = {
    '0:0:0': [0, 0, 0, 256, 256],
    '0:1:0': [0, 256, 0, 256, 256],
    '0:0:1': [0, 0, 256, 256, 256],
    '0:1:1': [0, 256, 256, 256, 256],
  };
  const images = new Map(
    Object.keys(geo).map((k) => {
      const img = { _ready: true };
      return [k, img];
    }),
  );
  const ctx = mockCtx();
  const n = drawScene(ctx, { transform: t, meta: META, tileGeo: geo, images });
  assert.equal(n, 4);
  assert.equal(ctx.calls.fill, 1);
  assert.deepEqual(ctx.lastFill, [0, 0, 512, 512]);
  assert.equal(ctx.fillStyle, '#ffffff');
  // screen origin is l0 (0,0): tile (tx,ty) draws at (tx*256, ty*256),
  // inflated by the seam margin (0.5 px per side, 1 px per dimension)
  for (const [img, left, top, w, h] of ctx.calls.draw) {
    assert.ok(Math.abs(w - 257) < 1e-9);
    assert.ok(Math.abs(h - 257) < 1e-9);
  }
  assert.deepEqual(
    ctx.calls.draw.map((d) => [d[1], d[2]]).sort((a, b) => a[0] - b[0]),
    [[-0.5, -0.5], [-0.5, 255.5], [255.5, -0.5], [255.5, 255.5]],
  );
});

test('adjacent tiles overlap on screen, so seams cannot show through', () => {
  const t = { cx: 128.3, cy: 128.3, zoom: 1.5, canvasW: 512, canvasH: 512 };
  // fractional screen rects: exact (un-inflated) edges do not land on
  // device-pixel boundaries
  const geo = {
    '0:0:0': [0, 0, 0, 256, 256],
    '0:1:0': [0, 256, 0, 256, 256],
    '0:0:1': [0, 0, 256, 256, 256],
    '0:1:1': [0, 256, 256, 256, 256],
  };
  const images = new Map(
    Object.keys(geo).map((k) => {
      const img = { _ready: true };
      return [k, img];
    }),
  );
  const ctx = mockCtx();
  assert.equal(drawScene(ctx, { transform: t, meta: META, tileGeo: geo, images }), 4);
  const keyOf = new Map([...images.values()].map((img, i) => [img, Object.keys(geo)[i]]));
  const rects = new Map(
    ctx.calls.draw.map(([img, left, top, w, h]) => [
      keyOf.get(img),
      { left, top, right: left + w, bottom: top + h },
    ]),
  );
  // each pair of screen-adjacent tiles must overlap (strictly), both
  // horizontally (east/west) and vertically (south/north):
  // west.right > east.left and north.bottom > south.top
  assert.ok(rects.get('0:0:0').right > rects.get('0:1:0').left);
  assert.ok(rects.get('0:0:1').right > rects.get('0:1:1').left);
  assert.ok(rects.get('0:0:0').bottom > rects.get('0:0:1').top);
  assert.ok(rects.get('0:1:0').bottom > rects.get('0:1:1').top);
});

test('drawScene skips off-canvas tiles and not-yet-loaded images', () => {
  const t = { cx: 0, cy: 0, zoom: 1.0, canvasW: 256, canvasH: 256 };
  const geo = {
    '0:0:0': [0, 0, 0, 256, 256],
    '0:5:5': [0, 1280, 1280, 256, 256], // far off-canvas
  };
  const images = new Map([['0:0:0', { _ready: false }]]); // not loaded yet
  const ctx = mockCtx();
  const n = drawScene(ctx, { transform: t, meta: META, tileGeo: geo, images });
  assert.equal(n, 0);
  assert.equal(ctx.calls.draw.length, 0);
});

test('drawScene reprojections under a local transform (smooth pan)', () => {
  // A tile fetched for one viewport must still land at the right screen
  // place after a pure client-side pan/zoom (no Python round-trip).
  const t0 = { cx: 512, cy: 512, zoom: 1.0, canvasW: 512, canvasH: 512 };
  const t1 = { cx: 640, cy: 384, zoom: 2.0, canvasW: 512, canvasH: 512 };
  // level-0 cell (tx=2, ty=1): l0 rect [512..768] x [256..512]
  const geo = { '0:2:1': [0, 512, 256, 256, 256] };
  const images = new Map([['0:2:1', { _ready: true, id: 'img' }]]);
  const ctx = mockCtx();
  assert.equal(drawScene(ctx, { transform: t0, meta: META, tileGeo: geo, images }), 1);
  const seam = 0.5; // compositor seam margin (see compositor.js)
  const [img, left0, top0, w0] = ctx.calls.draw[0];
  assert.equal(img.id, 'img');
  // l0 origin (512, 256) -> screen (rect inflated by the seam margin)
  assert.ok(Math.abs(left0 - ((512 - t0.cx) * t0.zoom + t0.canvasW / 2) + seam) < 1e-9);
  assert.ok(Math.abs(top0 - ((256 - t0.cy) * t0.zoom + t0.canvasH / 2) + seam) < 1e-9);
  assert.ok(Math.abs(w0 - (256 * 1.0 * t0.zoom + 2 * seam)) < 1e-9);
  ctx.calls.draw.length = 0;
  assert.equal(drawScene(ctx, { transform: t1, meta: META, tileGeo: geo, images }), 1);
  const [, left1, top1, w1] = ctx.calls.draw[0];
  // same l0 origin, new transform: cell now exactly fills the canvas
  assert.ok(Math.abs(left1 - ((512 - t1.cx) * t1.zoom + t1.canvasW / 2) + seam) < 1e-9);
  assert.ok(Math.abs(top1 - ((256 - t1.cy) * t1.zoom + t1.canvasH / 2) + seam) < 1e-9);
  assert.ok(Math.abs(w1 - (256 * 1.0 * t1.zoom + 2 * seam)) < 1e-9);
});

// ------------------------------------------------------------------ overlay
const OVERLAY = { _ready: true, id: 'overlay' };

test('drawOverlay stretches the image over the whole slide, at `alpha`', () => {
  const t = { cx: 200, cy: 300, zoom: 0.5, canvasW: 400, canvasH: 400 };
  const ctx = mockCtx();
  ctx.globalAlpha = 1;
  assert.equal(drawOverlay(ctx, { transform: t, meta: META, img: OVERLAY, alpha: 0.4 }), true);
  assert.equal(ctx.calls.draw.length, 1);
  const [img, left, top, w, h] = ctx.calls.draw[0];
  assert.equal(img.id, 'overlay');
  // slide origin (l0 0,0) -> screen; size = slide dimensions * zoom
  assert.ok(Math.abs(left - ((0 - t.cx) * t.zoom + t.canvasW / 2)) < 1e-9);
  assert.ok(Math.abs(top - ((0 - t.cy) * t.zoom + t.canvasH / 2)) < 1e-9);
  assert.ok(Math.abs(w - META.dimensions[0] * t.zoom) < 1e-9);
  assert.ok(Math.abs(h - META.dimensions[1] * t.zoom) < 1e-9);
  // globalAlpha is restored for subsequent passes (tiles, annotations)
  assert.equal(ctx.globalAlpha, 1);
});

test('drawOverlay is skipped for missing / not-ready / alpha=0 images', () => {
  const t = { cx: 0, cy: 0, zoom: 1.0, canvasW: 512, canvasH: 512 };
  const ctx = mockCtx();
  assert.equal(drawOverlay(ctx, { transform: t, meta: META, img: null, alpha: 0.5 }), false);
  assert.equal(
    drawOverlay(ctx, { transform: t, meta: META, img: { _ready: false }, alpha: 0.5 }), false);
  assert.equal(drawOverlay(ctx, { transform: t, meta: META, img: OVERLAY, alpha: 0 }), false);
  assert.equal(ctx.calls.draw.length, 0);
});

test('drawOverlay is skipped when the slide is fully off-canvas', () => {
  // Far east of the canvas: the whole slide lies right of the view.
  const t = { cx: 1e7, cy: 0, zoom: 0.01, canvasW: 512, canvasH: 512 };
  const ctx = mockCtx();
  assert.equal(drawOverlay(ctx, { transform: t, meta: META, img: OVERLAY, alpha: 1 }), false);
  assert.equal(ctx.calls.draw.length, 0);
});

test('drawOverlay reprojects under a local transform (smooth pan)', () => {
  const t0 = { cx: 100, cy: 100, zoom: 2.0, canvasW: 512, canvasH: 512 };
  const t1 = { cx: 400, cy: 100, zoom: 2.0, canvasW: 512, canvasH: 512 };
  const ctx = mockCtx();
  drawOverlay(ctx, { transform: t0, meta: META, img: OVERLAY, alpha: 1 });
  drawOverlay(ctx, { transform: t1, meta: META, img: OVERLAY, alpha: 1 });
  const [, l0, , w0] = ctx.calls.draw[0];
  const [, l1, , w1] = ctx.calls.draw[1];
  // panning the center +300 l0 px (zoom 2.0) moves the slide left by 600
  // screen px; the stretched size is unchanged
  assert.ok(Math.abs((l1 - l0) - (-600)) < 1e-9);
  assert.ok(Math.abs(w1 - w0) < 1e-9);
  assert.ok(Math.abs(w0 - META.dimensions[0] * 2.0) < 1e-9);
});
