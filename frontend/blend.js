/**
 * Level cross-fade (docs/DESIGN.md §6.6) — pure transition math.
 *
 * When the selected pyramid level (Python `select_level`, mirrored by
 * `math.selectLevel`) changes between tile_geo pushes, the view records
 * a transition here; the compositor then draws the new level at full
 * opacity while the old level fades linearly to 0 over `duration` ms.
 * The view owns the state object (the return value of `startTransition`)
 * and steps it with requestAnimationFrame (`done` tells it when to
 * stop); no timer runs at rest.
 */

/** Default cross-fade duration (ms). */
export const BLEND_MS = 300;

/**
 * Record a level transition between frames.
 *
 * @param {number} from  level selected on the previous frame
 * @param {number} to    level selected on this frame
 * @param {number} now   timestamp in ms (e.g. Date.now())
 * @returns {{active: boolean, from: number, to: number, start: number}}
 *   transition state; `active: false` when from === to (no fade to run)
 */
export function startTransition(from, to, now) {
  if (from === to) {
    return { active: false, from: to, to, start: 0 };
  }
  return { active: true, from, to, start: now };
}

/**
 * Per-level alpha for a transition at time `now`: the new level (`to`)
 * is always 1; the old level (`from`) fades 1 -> 0 linearly over
 * `duration` ms; every other level gets no entry (drawn at alpha 0).
 *
 * @returns {Record<number, number>} level -> alpha
 */
export function levelAlphas(blend, now, duration = BLEND_MS) {
  const alphas = { [blend.to]: 1 };
  if (blend.active && blend.from !== blend.to) {
    const t = Math.min(1, Math.max(0, (now - blend.start) / duration));
    alphas[blend.from] = 1 - t;
  }
  return alphas;
}

/** True when the fade is complete (the rAF loop stops here). */
export function done(blend, now, duration = BLEND_MS) {
  return !blend.active || now - blend.start >= duration;
}
