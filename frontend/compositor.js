/**
 * Canvas compositor. Pure function over a canvas 2D context (duck-typed),
 * so it can be unit-tested with a mock ctx in Node.
 *
 * The view keeps a local transform (smooth pan/zoom between Python
 * round-trips); tiles carry level-space geometry and are reprojected here.
 * Tiles are 256px-grid crops, drawn at their exact screen rectangle, with a
 * half-px seam margin so adjacent tiles overlap (SEAM_MARGIN below).
 */
import * as math from './tilemath.js';

/**
 * Draw the current scene into `ctx` (already scaled for device pixel
 * ratio, origin at canvas top-left, CSS-pixel units).
 *
 * @param {object} ctx          canvas 2D context
 * @param {object} opts
 * @param {object} opts.transform   JS-local transform {cx, cy, zoom, canvasW, canvasH}
 * @param {object} opts.meta        slide meta (for level_downsamples)
 * @param {object} opts.tileGeo     { "L:tx:ty": [level, ox, oy, cw, ch] }
 * @param {Map<string, object>} opts.images  key -> decoded image (HTMLImageElement)
 * @param {Record<number, number>|null} opts.levelAlphas  cross-fade: per-level
 *   alpha. When given, levels are drawn coarsest-first at their supplied
 *   alpha (levels without an entry, or at alpha 0, are skipped, so the
 *   fading-out level disappears at the fade's end). When omitted (the
 *   single-level contract) every visible ready tile is drawn at full opacity in
 *   `tileGeo` insertion order.
 * @returns {number} number of tiles drawn
 */
/**
 * Seam margin: each tile's destination rect is inflated by this many screen
 * px on every side, so adjacent tiles overlap by ~2*SEAM_MARGIN px. Tiles
 * carry fractional screen rects and each drawImage is rasterized
 * independently, so without overlap the shared boundary can end up only
 * partially covered by both neighbours, leaving a hairline (1 device px)
 * white line where the canvas background shows through. The slight stretch
 * (1 px on a 256+ px tile) is imperceptible.
 */
const SEAM_MARGIN = 0.5;

export function drawScene(ctx, { transform, meta, tileGeo, images, levelAlphas = null }) {
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, transform.canvasW, transform.canvasH);

  const ds = meta.level_downsamples;
  const tiles = math.visibleTiles(transform, tileGeo, ds);
  const drawTile = (tile) => {
    const img = images.get(tile.key);
    if (!img || !img._ready) return false;
    ctx.drawImage(
      img,
      tile.left - SEAM_MARGIN,
      tile.top - SEAM_MARGIN,
      tile.w + 2 * SEAM_MARGIN,
      tile.h + 2 * SEAM_MARGIN,
    );
    return true;
  };

  let n = 0;
  if (levelAlphas === null) {
    for (const tile of tiles) {
      if (drawTile(tile)) n += 1;
    }
  } else {
    for (let level = ds.length - 1; level >= 0; level -= 1) {
      const alpha = levelAlphas[level];
      if (!(alpha > 0)) continue; // absent / 0 / negative: skip level
      ctx.globalAlpha = alpha;
      for (const tile of tiles) {
        if (tile.level !== level) continue;
        if (drawTile(tile)) n += 1;
      }
      ctx.globalAlpha = 1;
    }
  }
  return n;
}

/**
 * Draw a full-slide overlay image (e.g. a model heatmap rendered at the
 * slide's get_thumbnail scale) stretched over the whole slide, over the
 * tiles and under the annotations (the view draws it between drawScene
 * and the annotation pass).
 *
 * The overlay PNG keeps its own alpha channel (transported as a PNG data
 * URL, since JPEG has none); `alpha` (0..1) is the view-level opacity on
 * top of that.
 *
 * @param {object} ctx          canvas 2D context
 * @param {object} opts
 * @param {object} opts.transform   JS-local transform {cx, cy, zoom, canvasW, canvasH}
 * @param {object} opts.meta        slide meta (for dimensions)
 * @param {object|null} opts.img    decoded overlay image (null = none)
 * @param {number} opts.alpha       overlay opacity in [0, 1]
 * @returns {boolean} true if the overlay was drawn
 */
export function drawOverlay(ctx, { transform, meta, img, alpha }) {
  if (!img || !img._ready || !(alpha > 0)) return false;
  const [sw, sh] = meta.dimensions;
  const zoom = transform.zoom;
  const [left, top] = math.l0ToScreen(transform, 0, 0);
  const w = sw * zoom;
  const h = sh * zoom;
  // Nothing to draw if the slide is fully off-canvas (or degenerate).
  if (left >= transform.canvasW || top >= transform.canvasH
      || left + w <= 0 || top + h <= 0) {
    return false;
  }
  ctx.globalAlpha = alpha;
  ctx.drawImage(img, left, top, w, h);
  ctx.globalAlpha = 1;
  return true;
}
