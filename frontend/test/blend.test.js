/**
 * Level cross-fade math tests (node --test).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { BLEND_MS, startTransition, levelAlphas, done } from '../blend.js';

test('BLEND_MS default is 300 ms', () => {
  assert.equal(BLEND_MS, 300);
});

test('startTransition is a no-op when the level is unchanged', () => {
  const b = startTransition(2, 2, 1000);
  assert.deepEqual(b, { active: false, from: 2, to: 2, start: 0 });
});

test('startTransition records the transition start time', () => {
  const b = startTransition(2, 0, 1234);
  assert.equal(b.active, true);
  assert.equal(b.from, 2);
  assert.equal(b.to, 0);
  assert.equal(b.start, 1234);
});

test('levelAlphas: the incoming level is 1, the outgoing level fades linearly', () => {
  const b = startTransition(2, 0, 0);
  // t = 0: the old level is still fully opaque
  assert.deepEqual(levelAlphas(b, 0), { 0: 1, 2: 1 });
  // t = BLEND_MS / 2: halfway through the fade
  const half = levelAlphas(b, BLEND_MS / 2);
  assert.equal(half[0], 1);
  assert.ok(Math.abs(half[2] - 0.5) < 1e-9);
  // t = BLEND_MS: the old level is at 0, the new level is complete
  assert.deepEqual(levelAlphas(b, BLEND_MS), { 0: 1, 2: 0 });
  // after the end: the old level stays at 0
  assert.deepEqual(levelAlphas(b, BLEND_MS * 2), { 0: 1, 2: 0 });
});

test('levelAlphas clamps to fully opaque before the fade starts', () => {
  const b = startTransition(1, 0, 1000);
  assert.deepEqual(levelAlphas(b, 500), { 0: 1, 1: 1 });
});

test('levelAlphas for a non-transition blend: only the current level', () => {
  const b = startTransition(3, 3, 42);
  assert.deepEqual(levelAlphas(b, 42), { 3: 1 });
});

test('done: false until the fade elapses, true afterwards (and at rest)', () => {
  const b = startTransition(2, 0, 0);
  assert.equal(done(b, 0), false);
  assert.equal(done(b, BLEND_MS - 1), false);
  assert.equal(done(b, BLEND_MS), true);
  assert.equal(done(b, BLEND_MS + 1000), true);
  // no fade to run: done immediately
  assert.equal(done(startTransition(1, 1, 0), 0), true);
});
