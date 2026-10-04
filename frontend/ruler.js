/**
 * Ruler measurement (docs/DESIGN.md §6.4.1). A view-local measuring
 * tool: while the ruler mode is active the user drags on the canvas and
 * a line is drawn between two level-0 points, with the segment's length
 * labeled in micrometers (the slide's mpp, from the `meta` trait) and
 * level-0 pixels. Pure parts, both node-testable without a DOM:
 *
 *   - the mode state machine: pointer/keyboard events advance
 *     `{active, line}`;
 *   - `lineLength` / `formatMeasure`: the length readout;
 *   - `drawRuler`: the live measurement (line, end ticks, midpoint label).
 *
 * The mode never crosses the wire: the measurement is a function of a
 * level-0 line and the slide's mpp, both view-side, so no JS->Py write
 * (and no Python round-trip) is involved — like the zoom readout.
 */
import * as math from './tilemath.js';

// Ruler styling (screen space, CSS px — screen-constant, like the
// annotation overlay; the view draws the line on the annotation canvas).
export const RULER_COLOR = '#0066cc';
export const RULER_STROKE_WIDTH = 2;
export const RULER_TICK_LENGTH = 8; // perpendicular end tick, screen px
export const RULER_LABEL_FONT = '12px sans-serif';
export const RULER_LABEL_HALO = 3; // white text halo, screen px
export const RULER_LABEL_OFFSET_X = 8; // label offset from the midpoint
export const RULER_LABEL_OFFSET_Y = -8;

/** The initial ruler state (inactive, no measurement). */
export function rulerInit() {
  return { active: false, line: null };
}

/**
 * Advance the ruler state machine by one event; returns `{state}`.
 * `line` is `[[x0, y0], [x1, y1]]` (level-0 px, unclamped — the start
 * point, then the far end) or null. Events (all positions in level-0 px;
 * the view converts them):
 *
 *   { type: 'toggle' }     the toolbar **ruler** button: enter / exit the
 *                          mode (exit clears the measurement)
 *   { type: 'cancel' }     Esc: exit the mode, clear the measurement
 *   { type: 'begin', x, y } a left press: a new measurement starts at the
 *                          press (both ends at the same point); replaces
 *                          any current measurement
 *   { type: 'move',  x, y } a left drag: the far end follows the cursor
 *                          (the start point stays put)
 *   { type: 'clear' }      a still left click: erase the measurement
 *
 * `state` is a fresh object when anything changes (the input is never
 * mutated; a no-op event returns the input state unchanged).
 */
export function rulerEvent(state, event) {
  const finite = (v) => typeof v === 'number' && Number.isFinite(v);
  switch (event.type) {
    case 'toggle':
      return { state: { active: !state.active, line: null } };
    case 'cancel': {
      if (!state.active) return { state };
      return { state: { active: false, line: null } };
    }
    case 'begin': {
      if (!state.active || !finite(event.x) || !finite(event.y)) {
        return { state };
      }
      return {
        state: {
          active: state.active,
          line: [[event.x, event.y], [event.x, event.y]],
        },
      };
    }
    case 'move': {
      if (!state.active || !state.line
          || !finite(event.x) || !finite(event.y)) {
        return { state };
      }
      return {
        state: {
          active: state.active,
          line: [[state.line[0][0], state.line[0][1]], [event.x, event.y]],
        },
      };
    }
    case 'clear': {
      if (!state.active) return { state };
      return { state: { active: state.active, line: null } };
    }
    default:
      return { state };
  }
}

/**
 * The length of a level-0 segment `[[x0, y0], [x1, y1]]` in level-0 px
 * (0 for null / malformed input or a zero-length line).
 */
export function lineLength(line) {
  if (!Array.isArray(line) || line.length !== 2) return 0;
  const a = line[0], b = line[1];
  if (!Array.isArray(a) || !Array.isArray(b)) return 0;
  const [ax, ay] = a, [bx, by] = b;
  if (![ax, ay, bx, by].every(Number.isFinite)) return 0;
  return Math.hypot(bx - ax, by - ay);
}

/**
 * Format a level-0 px length for the ruler label, converting to
 * micrometers when `mpp` (microns per level-0 pixel, the `meta` trait)
 * is positive: `"312 µm · 269 px"` — or `"269 px"` alone without mpp.
 * Each value keeps three significant digits (like the zoom readout).
 */
export function formatMeasure(px, mpp) {
  const num = (v) => String(Number(v.toPrecision(3)));
  const hasMpp = typeof mpp === 'number' && Number.isFinite(mpp) && mpp > 0;
  return hasMpp
    ? `${num(px * mpp)} µm · ${num(px)} px`
    : `${num(px)} px`;
}

/**
 * Draw the current ruler measurement onto `ctx` (already DPR-scaled,
 * CSS-pixel units) under `transform`: the line between the two level-0
 * ends, a perpendicular tick at each end, and — at the midpoint (offset
 * toward the upper right, white halo like the annotation labels) — the
 * `formatMeasure` label for `mpp`. A null or zero-length line draws
 * nothing. Returns the label text drawn (or `''`).
 */
export function drawRuler(ctx, { transform: t, line, mpp = null }) {
  const len = lineLength(line);
  if (len === 0) return '';
  const [ax, ay] = math.l0ToScreen(t, line[0][0], line[0][1]);
  const [bx, by] = math.l0ToScreen(t, line[1][0], line[1][1]);
  ctx.save();
  ctx.strokeStyle = RULER_COLOR;
  ctx.lineWidth = RULER_STROKE_WIDTH;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  const sx = bx - ax, sy = by - ay;
  const sl = Math.hypot(sx, sy);
  if (sl > 0) {
    // Perpendicular end ticks (screen space): the unit perpendicular of
    // the screen direction, half the tick length each way.
    const px = -sy / sl, py = sx / sl;
    const half = RULER_TICK_LENGTH / 2;
    ctx.beginPath();
    ctx.moveTo(ax + px * half, ay + py * half);
    ctx.lineTo(ax - px * half, ay - py * half);
    ctx.moveTo(bx + px * half, by + py * half);
    ctx.lineTo(bx - px * half, by - py * half);
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.moveTo(ax, ay);
  ctx.lineTo(bx, by);
  ctx.stroke();
  const label = formatMeasure(len, mpp);
  const mx = (ax + bx) / 2 + RULER_LABEL_OFFSET_X;
  const my = (ay + by) / 2 + RULER_LABEL_OFFSET_Y;
  ctx.font = RULER_LABEL_FONT;
  ctx.textBaseline = 'middle';
  ctx.lineJoin = 'round';
  ctx.lineWidth = RULER_LABEL_HALO;
  ctx.strokeStyle = '#ffffff';
  ctx.strokeText(label, mx, my);
  ctx.fillStyle = RULER_COLOR;
  ctx.fillText(label, mx, my);
  ctx.restore();
  return label;
}
