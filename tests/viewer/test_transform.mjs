// node --test tests/viewer/test_transform.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import * as T from '../../web/transform.js';

const FRAME = { width: 7200, height: 4800 };
const ZOOMS = [T.MIN_ZOOM, 0.05, 0.1, 0.25, 0.5, 1, 1.5, 2, 4, 8, 16, T.MAX_ZOOM];
const PANS = [[0, 0], [123.4, -56.7], [-5000, 3000]];
const POINTS = [[0, 0], [7200, 0], [0, 4800], [7200, 4800], [3600, 2400], [0.5, 0.5], [1234.567, 890.123]];

test('toCanonical inverts toScreen at every zoom, rotation and pan', () => {
  let worst = 0;
  for (const zoom of ZOOMS) for (const rotation of T.ROTATIONS) for (const [panX, panY] of PANS) {
    const v = { zoom, rotation, panX, panY };
    for (const [x, y] of POINTS) {
      const s = T.toScreen(v, x, y);
      const c = T.toCanonical(v, s.x, s.y);
      worst = Math.max(worst, Math.hypot(c.x - x, c.y - y));
    }
  }
  assert.ok(worst < 1e-6, `worst round-trip error ${worst} px`);
});

test('rotation turns clockwise on a y-down screen', () => {
  const v = { zoom: 1, rotation: 90, panX: 0, panY: 0 };
  const s = T.toScreen(v, 1, 0); // +x (right) should point down
  assert.ok(Math.abs(s.x) < 1e-12 && Math.abs(s.y - 1) < 1e-12);
});

test('zoomAt keeps the canonical point under the cursor fixed', () => {
  for (const rotation of T.ROTATIONS) {
    let v = { zoom: 0.3, rotation, panX: 40, panY: 700 };
    const before = T.toCanonical(v, 321, 123);
    for (const f of [2, 3.7, 0.2, 10]) v = T.zoomAt(v, f, 321, 123);
    const after = T.toCanonical(v, 321, 123);
    assert.ok(Math.hypot(after.x - before.x, after.y - before.y) < 1e-6);
  }
});

test('zoom is clamped', () => {
  const v = T.zoomAt({ zoom: 1, rotation: 0, panX: 0, panY: 0 }, 1e6, 0, 0);
  assert.equal(v.zoom, T.MAX_ZOOM);
  assert.equal(T.zoomAt(v, 1e-9, 0, 0).zoom, T.MIN_ZOOM);
});

test('rotateAt keeps the rotation centre fixed and fit centres the page', () => {
  let v = T.fit({ zoom: 1, rotation: 0, panX: 0, panY: 0 }, FRAME, 1000, 800);
  const c = T.toScreen(v, 3600, 2400);
  assert.ok(Math.abs(c.x - 500) < 1e-9 && Math.abs(c.y - 400) < 1e-9);
  for (let i = 0; i < 4; i++) {
    const p = T.toCanonical(v, 500, 400);
    v = T.rotateAt(v, 500, 400);
    const q = T.toCanonical(v, 500, 400);
    assert.ok(Math.hypot(p.x - q.x, p.y - q.y) < 1e-9);
  }
  assert.equal(v.rotation, 0);
  const f = T.fit({ zoom: 1, rotation: 90, panX: 0, panY: 0 }, FRAME, 1000, 800);
  const corners = T.boxCorners(f, { x: 0, y: 0, ...FRAME });
  for (const k of corners) assert.ok(k.x >= 15.99 && k.x <= 984.01 && k.y >= 15.99 && k.y <= 784.01);
});

test('boxFromDrag gives whole pixels clipped to the page, in any drag direction', () => {
  assert.deepEqual(T.boxFromDrag({ x: 10.2, y: 20.8 }, { x: 3.9, y: 5.1 }, FRAME),
    { x: 3, y: 5, width: 8, height: 16 });
  assert.deepEqual(T.boxFromDrag({ x: -50, y: -50 }, { x: 10, y: 10 }, FRAME),
    { x: 0, y: 0, width: 10, height: 10 });
  assert.deepEqual(T.boxFromDrag({ x: 7190.5, y: 4790 }, { x: 9000, y: 9000 }, FRAME),
    { x: 7190, y: 4790, width: 10, height: 10 });
  assert.equal(T.boxFromDrag({ x: -10, y: 5 }, { x: -1, y: 50 }, FRAME), null);
});

test('clampPoint keeps pins on the page', () => {
  assert.deepEqual(T.clampPoint({ x: -3, y: 9999 }, FRAME), { x: 0, y: 4800 });
  assert.deepEqual(T.clampPoint({ x: 12.5, y: 7 }, FRAME), { x: 12.5, y: 7 });
});

test('clampPan keeps part of the page visible', () => {
  const v = T.clampPan({ zoom: 1, rotation: 0, panX: -99999, panY: 99999 }, FRAME, 800, 600);
  const cs = T.boxCorners(v, { x: 0, y: 0, ...FRAME });
  assert.ok(Math.max(...cs.map((c) => c.x)) >= 48 - 1e-9);
  assert.ok(Math.min(...cs.map((c) => c.y)) <= 600 - 48 + 1e-9);
});

test('hitTest picks the nearest pin within the radius', () => {
  const v = { zoom: 2, rotation: 0, panX: 0, panY: 0 };
  const pins = [{ x: 10, y: 10 }, { x: 14, y: 10 }];
  assert.equal(T.hitTest(v, pins, 27, 20), 1);
  assert.equal(T.hitTest(v, pins, 200, 200), -1);
});
