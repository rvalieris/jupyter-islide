/**
 * Compositor tests with a mock canvas 2D context (node --test).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { drawScene, drawOverlay, paintCheckerTile, CHECKER_SIZE } from '../compositor.js';

function mockCtx() {
  const calls = { fill: 0, draw: [], fills: [] };
  return {
    calls,
    set fillStyle(v) { this._fillStyle = v; },
    get fillStyle() { return this._fillStyle; },
    fillRect(x, y, w, h) {
      calls.fill += 1;
      calls.fills.push([this._fillStyle, x, y, w, h]);
      this.lastFill = [x, y, w, h];
    },
    drawImage(img, left, top, w, h) {
      calls.draw.push([img, left, top, w, h, this.globalAlpha]);
    },
  };
}

const META = {
  dimensions: [37382, 73222],
  level_count: 8,
  level_downsamples: [1, 2, 4, 8, 16, 32, 64, 128],
};

test('drawScene paints the canvas background and draws every visible ready tile', () => {
  // Mock ctx has no createPattern (and Node has no DOM): the background
  // falls back to a plain light fill.
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

test('drawScene reports the tiles it draws in `drawn` (the LRU touch set)', () => {
  const t = { cx: 128, cy: 128, zoom: 1.0, canvasW: 256, canvasH: 256 };
  const geo = {
    '0:0:0': [0, 0, 0, 256, 256],
    '0:1:0': [0, 256, 0, 256, 256],
    '0:5:5': [0, 1280, 1280, 256, 256], // far off-canvas
  };
  const images = new Map([
    ['0:0:0', { _ready: true }],
    ['0:1:0', { _ready: false }], // not loaded: drawn nothing, reported nothing
  ]);
  const ctx = mockCtx();
  const drawn = new Set();
  const n = drawScene(ctx, {
    transform: t, meta: META, tileGeo: geo, images, drawn,
  });
  assert.equal(n, 1);
  assert.deepEqual([...drawn], ['0:0:0']);
  // No `drawn` set: same contract as before (the parameter defaults to null).
  ctx.calls.draw.length = 0;
  assert.equal(drawScene(ctx, {
    transform: t, meta: META, tileGeo: geo, images,
  }), 1);
});

test('drawScene reports nothing for alpha-0 levels (cross-fade tail)', () => {
  const t = { cx: 0, cy: 0, zoom: 0.5, canvasW: 512, canvasH: 512 };
  // level-0 tile (the fading-out level) + level-1 tile (the new level),
  // both on-canvas
  const geo = {
    '0:0:0': [0, 0, 0, 256, 256],
    '1:0:0': [1, 0, 0, 256, 256],
  };
  const images = new Map(Object.keys(geo).map((k) => [k, { _ready: true }]));
  const ctx = mockCtx();
  const drawn = new Set();
  // fade at its end: the outgoing level is at alpha 0 (skipped), the new
  // level at 1 — only the drawn level may be touched
  const n = drawScene(ctx, {
    transform: t, meta: META, tileGeo: geo, images,
    levelAlphas: { 0: 0, 1: 1 }, drawn,
  });
  assert.equal(n, 1);
  assert.deepEqual([...drawn], ['1:0:0']);
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

// ------------------------------------------------------------ checkerboard
test('paintCheckerTile: light 2s×2s base, dark squares top-left and bottom-right', () => {
  const s = CHECKER_SIZE;
  const ctx = mockCtx();
  paintCheckerTile(ctx);
  assert.equal(ctx.calls.fill, 3);
  assert.deepEqual(ctx.calls.fills, [
    ['#ffffff', 0, 0, 2 * s, 2 * s],
    ['#cccccc', 0, 0, s, s],
    ['#cccccc', s, s, s, s],
  ]);
});

test('drawScene fills the background with the repeating checker pattern (pattern ctx)', () => {
  const s = CHECKER_SIZE;
  const prevDoc = globalThis.document;
  const tileCanvas = { width: 0, height: 0 };
  const tileCtx = mockCtx();
  globalThis.document = {
    createElement: () => {
      tileCanvas.getContext = () => tileCtx;
      return tileCanvas;
    },
  };
  const patterns = [];
  const PATTERN = { sentinel: 'pattern' };
  const ctx = Object.assign(mockCtx(), {
    createPattern(tile, mode) {
      patterns.push([tile, mode]);
      return PATTERN;
    },
  });
  const t = { cx: 0, cy: 0, zoom: 1.0, canvasW: 256, canvasH: 128 };
  try {
    drawScene(ctx, { transform: t, meta: META, tileGeo: {}, images: new Map() });
    // the whole canvas is one pattern fill...
    assert.equal(ctx.calls.fill, 1);
    assert.deepEqual(ctx.lastFill, [0, 0, 256, 128]);
    assert.equal(ctx.fillStyle, PATTERN);
    // ...built from a 2s×2s tile canvas painted by paintCheckerTile...
    assert.equal(patterns.length, 1);
    assert.equal(patterns[0][0], tileCanvas);
    assert.equal(patterns[0][1], 'repeat');
    assert.equal(tileCanvas.width, 2 * s);
    assert.equal(tileCanvas.height, 2 * s);
    assert.deepEqual(tileCtx.calls.fills, [
      ['#ffffff', 0, 0, 2 * s, 2 * s],
      ['#cccccc', 0, 0, s, s],
      ['#cccccc', s, s, s, s],
    ]);
    // ...reused on the next frame (no second pattern for the same ctx)
    drawScene(ctx, { transform: t, meta: META, tileGeo: {}, images: new Map() });
    assert.equal(patterns.length, 1);
    assert.equal(ctx.calls.fill, 2);
  } finally {
    if (prevDoc === undefined) delete globalThis.document;
    else globalThis.document = prevDoc;
  }
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

// --------------------------------------------------------------- overview
const OVERVIEW = { id: 'overview' };

test('drawScene stretches the overview over the slide rect, below the tiles', () => {
  const t = { cx: 200, cy: 300, zoom: 0.5, canvasW: 400, canvasH: 400 };
  const geo = { '0:0:0': [0, 0, 0, 256, 256] };
  const images = new Map([['0:0:0', { _ready: true, id: 'tile' }]]);
  const ctx = mockCtx();
  drawScene(ctx, {
    transform: t, meta: META, tileGeo: geo, images, overview: OVERVIEW,
  });
  const draws = ctx.calls.draw;
  assert.equal(draws.length, 2);
  // The overview is first (below the tiles), at the slide's screen rect
  // (slide origin l0 (0,0) -> screen; size = slide dimensions * zoom).
  assert.equal(draws[0][0].id, 'overview');
  assert.ok(Math.abs(draws[0][1] - ((0 - t.cx) * t.zoom + t.canvasW / 2)) < 1e-9);
  assert.ok(Math.abs(draws[0][2] - ((0 - t.cy) * t.zoom + t.canvasH / 2)) < 1e-9);
  assert.ok(Math.abs(draws[0][3] - META.dimensions[0] * t.zoom) < 1e-9);
  assert.ok(Math.abs(draws[0][4] - META.dimensions[1] * t.zoom) < 1e-9);
  // The tile draws above it (the seam-inflated rect is unaffected).
  assert.equal(draws[1][0].id, 'tile');
});

test('drawScene skips the overview when the slide is fully off-canvas', () => {
  // Far east of the canvas: the whole slide lies right of the view.
  const t = { cx: 1e7, cy: 0, zoom: 0.01, canvasW: 512, canvasH: 512 };
  const ctx = mockCtx();
  drawScene(ctx, {
    transform: t, meta: META, tileGeo: {}, images: new Map(), overview: OVERVIEW,
  });
  assert.equal(ctx.calls.draw.length, 0);
});

test('drawScene omits the overview when it is null (image still decoding)', () => {
  const t = { cx: 200, cy: 300, zoom: 0.5, canvasW: 400, canvasH: 400 };
  const ctx = mockCtx();
  drawScene(ctx, {
    transform: t, meta: META, tileGeo: {}, images: new Map(), overview: null,
  });
  assert.equal(ctx.calls.draw.length, 0);
});

// ---------------------------------------------------------------- cross-fade
const FADE_GEO = {
  '0:0:0': [0, 0, 0, 256, 256],
  '0:1:0': [0, 256, 0, 256, 256],
  '0:0:1': [0, 0, 256, 256, 256],
  '0:1:1': [0, 256, 256, 256, 256],
  // one big level-2 tile (ds 4) covering l0 [0..8192]^2
  '2:0:0': [2, 0, 0, 2048, 2048],
};

function fadeImages() {
  return new Map(Object.keys(FADE_GEO).map((k) => [k, { _ready: true, k }]));
}

test('levelAlphas: coarsest-first at the supplied alpha, absent levels skipped', () => {
  const t = { cx: 256, cy: 256, zoom: 1.0, canvasW: 512, canvasH: 512 };
  const ctx = mockCtx();
  const n = drawScene(ctx, {
    transform: t,
    meta: META,
    tileGeo: FADE_GEO,
    images: fadeImages(),
    levelAlphas: { 2: 1, 0: 0.5 }, // mid-fade: old level half out
  });
  assert.equal(n, 5);
  const draws = ctx.calls.draw;
  // coarsest first: the level-2 tile is drawn first, at alpha 1...
  assert.equal(draws[0][0].k, '2:0:0');
  assert.ok(Math.abs(draws[0][5] - 1) < 1e-9);
  // ...then the four level-0 tiles at alpha 0.5
  for (let i = 1; i < draws.length; i++) {
    assert.ok(draws[i][0].k.startsWith('0:'));
    assert.ok(Math.abs(draws[i][5] - 0.5) < 1e-9);
  }
  // globalAlpha is restored for subsequent passes (annotations, overlay)
  assert.equal(ctx.globalAlpha, 1);
});

test('levelAlphas: a zero-alpha level is not drawn (fade finished)', () => {
  const t = { cx: 256, cy: 256, zoom: 1.0, canvasW: 512, canvasH: 512 };
  const ctx = mockCtx();
  const n = drawScene(ctx, {
    transform: t,
    meta: META,
    tileGeo: FADE_GEO,
    images: fadeImages(),
    levelAlphas: { 2: 1 }, // level 0 absent: the fade is over
  });
  assert.equal(n, 1);
  assert.equal(ctx.calls.draw[0][0].k, '2:0:0');
});

test('levelAlphas: all-zero alphas draw nothing', () => {
  const t = { cx: 256, cy: 256, zoom: 1.0, canvasW: 512, canvasH: 512 };
  const ctx = mockCtx();
  const n = drawScene(ctx, {
    transform: t,
    meta: META,
    tileGeo: FADE_GEO,
    images: fadeImages(),
    levelAlphas: { 0: 0, 2: 0 },
  });
  assert.equal(n, 0);
  assert.equal(ctx.calls.draw.length, 0);
});
