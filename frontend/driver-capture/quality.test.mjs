import assert from 'node:assert/strict';
import test from 'node:test';
import { quality, steps } from './quality.mjs';

const pixels = (w, h, sample) => Uint8ClampedArray.from(
  { length: w * h * 4 }, (_, i) => i % 4 === 3 ? 255 : sample(Math.floor(i / 4)),
);
test('capture sequence includes four required images and optional GPLX back', () => {
  assert.equal(steps.filter(step => !step.optional).length, 4);
  assert.equal(steps[3].optional, true);
  assert.equal(steps[4].selfie, true);
});
test('small dark low-detail images get advisory codes', () => {
  assert.deepEqual(quality(pixels(8, 8, () => 20), 8, 8, 100, 300),
    ['SMALL', 'DARK', 'LOW_DETAIL']);
});
test('bright flat image is distinguished from a lit detailed image', () => {
  assert.deepEqual(quality(pixels(8, 8, () => 255), 8, 8, 800, 600), ['BRIGHT', 'LOW_DETAIL']);
  assert.deepEqual(quality(pixels(8, 8, i => i % 2 ? 190 : 75), 8, 8, 800, 600), []);
});
