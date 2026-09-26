/**
 * Polygon drawing support (M3, DESIGN.md §6.4). Two pure parts, both
 * node-testable without a DOM:
 *
 *   - the drawing-mode state machine: keyboard/pointer events advance
 *     `{mode, draft}`;
 *   - `drawDraftPolygon`: the live draft preview (vertex dots, solid
 *     segments, dashed closure to the cursor).
 *
 * The Python observer of `SlideViewer.last_polygon` is the final authority
 * on accepting a saved draft: this module only decides *what was drawn*
 * (the open, normalized ring) and never mutates synced state.
 */
import * as math from './tilemath.js';
import {
  DEFAULT_COLOR, STROKE_WIDTH, POINT_RADIUS, HALO_EXTRA,
} from './annotations.js';

export const MODE_IDLE = 'idle';
export const MODE_DRAWING = 'drawing';

/**
 * A pointerup within this distance of the pointerdown (CSS px) is a click
 * (a draft vertex in drawing mode); at or beyond it, the gesture is a pan.
 */
export const CLICK_THRESHOLD_PX = 4;

/** Dashed closure segments, screen px (dash, gap). */
export const DASH_PATTERN = [6, 4];

const _DEGENERATE_AREA = 1e-6; // level-0 px^2 (matches annotations.py)

/**
 * JS twin of Python's `normalize_ring` (islide/annotations.py): a position
 * sequence (polygon ring, level-0 px) in canonical form — a list of
 * `[x, y]` numbers, **open** (redundant closing position removed) — or
 * `null` for a degenerate or malformed ring (fewer than 3 points, a
 * non-finite coordinate, zero area). Kept in lockstep with the Python
 * helper so the JS-side save/discard decision cannot diverge from the
 * Python-side accept/discard decision.
 */
export function normalizeRing(ring) {
  if (!Array.isArray(ring) || ring.length < 3) return null;
  const pts = [];
  for (const p of ring) {
    if (!Array.isArray(p) || p.length !== 2) return null;
    const x = p[0], y = p[1];
    if (typeof x !== 'number' || typeof y !== 'number' ||
        !Number.isFinite(x) || !Number.isFinite(y)) return null;
    pts.push([x, y]);
  }
  const n = pts.length;
  if (n > 3 && pts[0][0] === pts[n - 1][0] && pts[0][1] === pts[n - 1][1]) {
    pts.pop();
  }
  if (pts.length < 3 || ringArea(pts) < _DEGENERATE_AREA) return null;
  return pts;
}

/** Shoelace area of a ring (closed implicitly). */
function ringArea(pts) {
  let a = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x0, y0] = pts[i];
    const [x1, y1] = pts[(i + 1) % pts.length];
    a += x0 * y1 - x1 * y0;
  }
  return Math.abs(a) / 2;
}

/** The initial drawing state (idle, empty draft). */
export function polyDrawInit() {
  return { mode: MODE_IDLE, draft: [] };
}

/**
 * Advance the drawing state machine by one event; returns `{state,
 * result}`.
 *
 * Events (all positions in level-0 px; the view converts them):
 *   { type: 'toggle' }           A: idle -> enter drawing; drawing -> save
 *                                (valid ring) or discard
 *   { type: 'cancel' }           Esc: discard the draft, exit
 *   { type: 'vertex', x, y }     a still left click: append a vertex
 *
 * `state` is always `{mode, draft}` (a fresh object when anything changes —
 * the input is never mutated); `result` is
 *   null | { op: 'save', ring } | { op: 'discard' } | { op: 'cancel' }
 * where `ring` is the open, normalized ring to send as `last_polygon`.
 */
export function polyEvent(state, event) {
  switch (event.type) {
    case 'toggle': {
      if (state.mode === MODE_IDLE) {
        return { state: { mode: MODE_DRAWING, draft: [] }, result: null };
      }
      const ring = normalizeRing(state.draft);
      return {
        state: { mode: MODE_IDLE, draft: [] },
        result: ring === null ? { op: 'discard' } : { op: 'save', ring },
      };
    }
    case 'cancel': {
      if (state.mode !== MODE_DRAWING) return { state, result: null };
      return { state: { mode: MODE_IDLE, draft: [] }, result: { op: 'cancel' } };
    }
    case 'vertex': {
      if (state.mode !== MODE_DRAWING) return { state, result: null };
      return {
        state: {
          mode: MODE_DRAWING,
          draft: [...state.draft, [event.x, event.y]],
        },
        result: null,
      };
    }
    default:
      return { state, result: null };
  }
}

/**
 * Draw an in-progress polygon draft onto `ctx` (already DPR-scaled,
 * CSS-pixel units) under `transform`: vertex dots (body + white halo, like
 * the point markers), solid screen-constant segments between consecutive
 * vertices, and — while the pointer is on the canvas — dashed closure
 * segments from the last vertex to `cursor` and, once the ring would close
 * (≥ 3 vertices), from `cursor` to the first vertex.
 * `alpha` (0..1) applies to the whole draft (the view passes the
 * annotation layer's opacity, so the α slider keeps working).
 * Returns the number of draft vertices drawn.
 */
export function drawDraftPolygon(ctx, { transform: t, draft, cursor = null, alpha = 1 }) {
  const pts = Array.isArray(draft) ? draft : [];
  if (pts.length === 0) return 0;
  ctx.save();
  ctx.globalAlpha = Math.min(1, Math.max(0, Number(alpha) || 0));
  if (pts.length > 1) {
    let [sx, sy] = math.l0ToScreen(t, pts[0][0], pts[0][1]);
    ctx.beginPath();
    ctx.moveTo(sx, sy);
    for (let i = 1; i < pts.length; i++) {
      [sx, sy] = math.l0ToScreen(t, pts[i][0], pts[i][1]);
      ctx.lineTo(sx, sy);
    }
    ctx.strokeStyle = DEFAULT_COLOR;
    ctx.lineWidth = STROKE_WIDTH;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
  }
  if (cursor) {
    const last = pts[pts.length - 1];
    const [fx, fy] = math.l0ToScreen(t, last[0], last[1]);
    ctx.beginPath();
    ctx.moveTo(fx, fy);
    ctx.lineTo(cursor.x, cursor.y);
    if (pts.length >= 3) {
      const [ax, ay] = math.l0ToScreen(t, pts[0][0], pts[0][1]);
      ctx.moveTo(ax, ay);
      ctx.lineTo(cursor.x, cursor.y);
    }
    ctx.setLineDash(DASH_PATTERN);
    ctx.strokeStyle = DEFAULT_COLOR;
    ctx.lineWidth = STROKE_WIDTH;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
    ctx.setLineDash([]);
  }
  for (const [x, y] of pts) {
    const [sx, sy] = math.l0ToScreen(t, x, y);
    ctx.beginPath();
    ctx.arc(sx, sy, POINT_RADIUS + HALO_EXTRA, 0, 2 * Math.PI);
    ctx.fillStyle = '#ffffff';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(sx, sy, POINT_RADIUS, 0, 2 * Math.PI);
    ctx.fillStyle = DEFAULT_COLOR;
    ctx.fill();
  }
  ctx.restore();
  return pts.length;
}
