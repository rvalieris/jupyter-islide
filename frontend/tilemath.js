/**
 * Pure viewport/tile math shared by the islide view and its tests.
 *
 * Contract (see docs/DESIGN.md §4, §7):
 *   viewport wire form: { cx, cy, zoom, canvas_w }
 *       cx, cy  : slide (level-0) coordinates of the canvas center
 *       zoom    : screen px per level-0 pixel (1.0 == 1:1)
 *       (the canvas height never crosses the wire: the `canvas_h` model
 *        trait is the source of truth on both sides)
 *   transform (JS-local): { cx, cy, zoom, canvasW, canvasH }
 *   meta:      { dimensions: [w, h], level_count, level_downsamples: [...],
 *                level_dimensions: [...], mpp, vendor }
 *   tile_geo:  { "level:tx:ty": [level, ox, oy, cw, ch] }
 *       ox, oy : absolute origin of the tile's crop, in LEVEL pixels of
 *                that tile's level (level 0 == level px)
 *       cw, ch : crop size in level pixels
 *
 * Screen mapping (x: screen = (l0 - cx) * zoom + canvasW/2):
 */

export function fitZoom(meta, canvasW, canvasH) {
  const [w, h] = meta.dimensions;
  return Math.min(canvasW / w, canvasH / h);
}

export function clampZoom(zoom, minZoom, maxZoom) {
  return Math.min(Math.max(zoom, minZoom), maxZoom);
}

/**
 * Wire viewport -> local transform. The canvas height comes from the
 * `canvas_h` model trait (the wire form carries no height); the canvas
 * width is the wire's `canvas_w` (JS-owned: the view's actual width).
 */
export function viewportToTransform(vp, canvasH) {
  return {
    cx: vp.cx,
    cy: vp.cy,
    zoom: vp.zoom,
    canvasW: vp.canvas_w,
    canvasH: canvasH,
  };
}

/** Local transform -> wire viewport (the height is not sent back: the
 * `canvas_h` trait already holds it on the Python side). */
export function transformToViewport(t) {
  return {
    cx: t.cx,
    cy: t.cy,
    zoom: t.zoom,
    canvas_w: t.canvasW,
  };
}

export function makeTransform(cx, cy, zoom, canvasW, canvasH) {
  return { cx, cy, zoom, canvasW, canvasH };
}

export function l0ToScreen(t, x, y) {
  return [(x - t.cx) * t.zoom + t.canvasW / 2, (y - t.cy) * t.zoom + t.canvasH / 2];
}

export function screenToL0(t, sx, sy) {
  return [(sx - t.canvasW / 2) / t.zoom + t.cx, (sy - t.canvasH / 2) / t.zoom + t.cy];
}

/**
 * Zoom by `factor` keeping the slide point under (sx, sy) fixed.
 */
export function zoomAtCursor(t, factor, sx, sy, minZoom, maxZoom) {
  const zoom = clampZoom(t.zoom * factor, minZoom, maxZoom);
  const [lx, ly] = screenToL0(t, sx, sy);
  return makeTransform(
    lx - (sx - t.canvasW / 2) / zoom,
    ly - (sy - t.canvasH / 2) / zoom,
    zoom,
    t.canvasW,
    t.canvasH,
  );
}

/**
 * Pan by a screen-pixel delta (positive = drag toward +x/+y).
 */
export function panTransform(t, dxScreen, dyScreen) {
  return makeTransform(
    t.cx - dxScreen / t.zoom,
    t.cy - dyScreen / t.zoom,
    t.zoom,
    t.canvasW,
    t.canvasH,
  );
}

/**
 * Screen rectangle of a tile: [level, ox, oy, cw, ch] (level px, absolute).
 */
export function tileScreenRect(t, geo, downsamples) {
  const [level, ox, oy, cw, ch] = geo;
  const ds = downsamples[level];
  const [left, top] = l0ToScreen(t, ox * ds, oy * ds);
  return { left, top, w: cw * ds * t.zoom, h: ch * ds * t.zoom };
}

/**
 * Tiles intersecting the canvas: [{ key, left, top, w, h }].
 */
export function visibleTiles(t, tileGeo, downsamples, margin = 0) {
  const out = [];
  for (const [key, geo] of Object.entries(tileGeo)) {
    const r = tileScreenRect(t, geo, downsamples);
    if (
      r.left < t.canvasW + margin &&
      r.left + r.w > -margin &&
      r.top < t.canvasH + margin &&
      r.top + r.h > -margin
    ) {
      out.push({ key, level: geo[0], ...r });
    }
  }
  return out;
}

/**
 * Level-0 bbox of the current canvas: { x0, y0, x1, y1 } (may be off-slide).
 */
export function viewportL0Bbox(t) {
  const [x0, y0] = screenToL0(t, 0, 0);
  const [x1, y1] = screenToL0(t, t.canvasW, t.canvasH);
  return { x0, y0, x1, y1 };
}

/**
 * Selected pyramid level for a zoom: the smallest level L with
 * level_downsamples[L] >= 1 / zoom (the finest level whose own
 * resolution still meets the on-screen resolution). Mirrors Python's
 * `select_level` (islide.plan) — the view uses it to detect level
 * changes between tile_geo pushes (cross-fade, docs/DESIGN.md §6.6).
 */
export function selectLevel(downsamples, zoom) {
  const need = 1 / zoom;
  let L = downsamples.length - 1;
  for (let i = 0; i < downsamples.length; i += 1) {
    if (downsamples[i] >= need) {
      L = i;
      break;
    }
  }
  return L;
}
