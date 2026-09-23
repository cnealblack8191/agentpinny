// node --test tests/viewer/test_edits.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { EditQueue, projectPins, mergePins, STORAGE_KEY } from '../../web/edits.js';

class MemStorage {
  constructor() { this.m = new Map(); }
  getItem(k) { return this.m.has(k) ? this.m.get(k) : null; }
  setItem(k, v) { this.m.set(k, String(v)); }
}

const err = (status, message = 'boom') =>
  Object.assign(new Error(message), { status, code: 'x', retryable: status === 0 || status >= 500 });

function harness(sendImpl) {
  let t = 0;
  const timers = [];
  const saved = [];
  const storage = new MemStorage();
  const q = new EditQueue({ storage, send: sendImpl, now: () => t,
    setTimer: (fn, ms) => timers.push({ fn, at: t + ms }), onSaved: (e, r) => saved.push([e, r]) });
  const advance = async (ms) => {
    t += ms;
    const due = timers.filter((x) => x.at <= t);
    for (const d of due) { timers.splice(timers.indexOf(d), 1); d.fn(); }
    await new Promise((r) => setTimeout(r, 0));
  };
  return { q, storage, saved, advance };
}

test('edits are sent in order, one at a time, and leave the queue when saved', async () => {
  const order = [];
  let inFlight = 0;
  const { q, saved } = harness(async (e) => {
    inFlight++;
    assert.equal(inFlight, 1);
    order.push(e.request_id);
    await new Promise((r) => setTimeout(r, 1));
    inFlight--;
    return { pin: { pin_id: e.pin_id, version: 2 } };
  });
  q.enqueue({ scan_id: 's', action: 'approve', pin_id: 'det-1', request_id: 'a' });
  q.enqueue({ scan_id: 's', action: 'reject', pin_id: 'det-1', request_id: 'b' });
  await q.pump();
  while (q.entries.length) await new Promise((r) => setTimeout(r, 2));
  assert.deepEqual(order, ['a', 'b']);
  assert.equal(saved.length, 2);
});

test('network failures retry automatically, then stay as failed until retried or discarded', async () => {
  let calls = 0;
  const { q, advance } = harness(async () => { calls++; throw err(0); });
  q.enqueue({ scan_id: 's', action: 'approve', pin_id: 'det-1', request_id: 'a' });
  await new Promise((r) => setTimeout(r, 0));
  await advance(1000);
  await advance(2000);
  assert.equal(calls, 3);
  assert.equal(q.entries[0].status, 'failed');
  assert.deepEqual(q.counts('s'), { pending: 0, failed: 1 });
  q.retry('a');
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls, 4);
  q.discard('a');
  assert.equal(q.entries.length, 0);
});

test('4xx failures are not retried automatically and keep their message', async () => {
  let calls = 0;
  const { q } = harness(async () => { calls++; throw err(409, 'changed elsewhere'); });
  q.enqueue({ scan_id: 's', action: 'approve', pin_id: 'det-1', request_id: 'a' });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls, 1);
  assert.equal(q.entries[0].status, 'failed');
  assert.equal(q.entries[0].error.message, 'changed elsewhere');
});

test('queued edits survive a reload and in-flight ones are re-sent with the same request_id', async () => {
  const storage = new MemStorage();
  storage.setItem(STORAGE_KEY, JSON.stringify([
    { scan_id: 's', action: 'approve', pin_id: 'det-1', request_id: 'a', status: 'sending', attempts: 1 },
    { scan_id: 's', action: 'add_manual', x: 1, y: 2, request_id: 'b', status: 'failed', attempts: 3,
      error: { message: 'x' } },
  ]));
  const sent = [];
  const q = new EditQueue({ storage, send: async (e) => { sent.push(e.request_id); return { pin: {} }; } });
  assert.equal(q.entries[0].status, 'pending');
  await q.pump();
  assert.deepEqual(sent, ['a']);
  assert.equal(q.entries.length, 1); // the failed add waits for the reviewer
  assert.equal(JSON.parse(storage.getItem(STORAGE_KEY)).length, 1);
});

test('projectPins applies pending edits and flags failed ones', () => {
  const server = [
    { pin_id: 'det-1', origin: 'machine', state: 'unreviewed', x: 1, y: 1, version: 1 },
    { pin_id: 'det-2', origin: 'machine', state: 'unreviewed', x: 2, y: 2, version: 1 },
    { pin_id: 'm1', origin: 'manual', state: 'added', x: 3, y: 3, version: 1 },
  ];
  const entries = [
    { scan_id: 's', action: 'approve', pin_id: 'det-1', request_id: 'a', status: 'pending' },
    { scan_id: 's', action: 'delete_pin', pin_id: 'm1', request_id: 'b', status: 'sending' },
    { scan_id: 's', action: 'delete_pin', pin_id: 'det-2', request_id: 'c', status: 'failed', error: { message: 'no' } },
    { scan_id: 's', action: 'add_manual', x: 5, y: 6, request_id: 'd', status: 'pending' },
    { scan_id: 'other', action: 'approve', pin_id: 'det-2', request_id: 'e', status: 'pending' },
  ];
  const pins = Object.fromEntries(projectPins(server, entries, 's').map((p) => [p.pin_id, p]));
  assert.equal(pins['det-1'].state, 'approved');
  assert.equal(pins['det-1'].pending, true);
  assert.equal(pins.m1.state, 'removed');
  assert.equal(pins['det-2'].state, 'unreviewed');
  assert.equal(pins['det-2'].failed.message, 'no');
  assert.equal(pins['tmp:d'].state, 'added');
  assert.equal(pins['tmp:d'].x, 5);
  assert.equal(server[0].state, 'unreviewed'); // inputs untouched
});

test('mergePins never rolls a pin back to an older version', () => {
  const known = [{ pin_id: 'a', version: 3, state: 'approved' }];
  const stale = [{ pin_id: 'a', version: 2, state: 'unreviewed' }, { pin_id: 'b', version: 1 }];
  const out = Object.fromEntries(mergePins(known, stale).map((p) => [p.pin_id, p]));
  assert.equal(out.a.state, 'approved');
  assert.ok(out.b);
});
