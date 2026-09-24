/**
 * Canvas compositor. Pure function over a canvas 2D context (duck-typed),
 * so it can be unit-tested with a mock ctx in Node.
 *
 * The view keeps a local transform (smooth pan/zoom between Python
 * round-trips); tiles carry level-space geometry and are reprojected here.
 * Tiles are 256px-grid crops, drawn at their exact screen rectangle — the
 * same math the M0 HTML compositor used, but without per-<img> seams.
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

export function drawScene(ctx, { transform, meta, tileGeo, images }) {
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, transform.canvasW, transform.canvasH);

  const ds = meta.level_downsamples;
  let n = 0;
  for (const tile of math.visibleTiles(transform, tileGeo, ds)) {
    const img = images.get(tile.key);
    if (!img || !img._ready) continue;
    ctx.drawImage(
      img,
      tile.left - SEAM_MARGIN,
      tile.top - SEAM_MARGIN,
      tile.w + 2 * SEAM_MARGIN,
      tile.h + 2 * SEAM_MARGIN,
    );
    n += 1;
  }
  return n;
}
