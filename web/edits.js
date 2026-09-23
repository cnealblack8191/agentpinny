// Review edits waiting to be saved ("outbox").
//
// Every approve / reject / delete / add is queued here first, stored in
// localStorage, then sent to the server one at a time. Each edit carries a
// client request_id, so re-sending after a timeout or a page reload is safe:
// the server applies it at most once (contracts §5). An edit leaves the queue
// only when the server confirms it or the reviewer discards it after a
// failure, so edits are never silently lost.
//
// Pure logic with injected storage/send/timers, so node tests can drive it.

export const STORAGE_KEY = 'pinny.viewer.outbox.v1';
const MAX_AUTO_ATTEMPTS = 3;

export class EditQueue {
  constructor({ storage, send, onChange = () => {}, onSaved = () => {}, now = () => Date.now(),
    setTimer = (fn, ms) => setTimeout(fn, ms) }) {
    this.storage = storage;
    this.send = send;
    this.onChange = onChange;
    this.onSaved = onSaved;
    this.now = now;
    this.setTimer = setTimer;
    this.entries = [];
    this.running = false;
    this.timer = null;
    this.load();
  }

  load() {
    let raw = null;
    try {
      raw = this.storage && this.storage.getItem(STORAGE_KEY);
    } catch (e) { /* storage unavailable: keep edits in memory only */ }
    let list = [];
    try {
      list = raw ? JSON.parse(raw) : [];
    } catch (e) {
      list = [];
    }
    // An edit that was in flight when the page closed may or may not have
    // been applied; re-sending it is safe because of its request_id.
    this.entries = (Array.isArray(list) ? list : []).map((e) =>
      e.status === 'sending' ? { ...e, status: 'pending', retryAt: 0 } : e);
  }

  persist() {
    try {
      if (this.storage) this.storage.setItem(STORAGE_KEY, JSON.stringify(this.entries));
      this.storageOk = true;
    } catch (e) {
      this.storageOk = false;
    }
  }

  changed() {
    this.persist();
    this.onChange(this);
  }

  enqueue(edit) {
    const entry = { ...edit, status: 'pending', attempts: 0, retryAt: 0, error: null,
      queuedAt: this.now() };
    this.entries.push(entry);
    this.changed();
    this.pump();
    return entry;
  }

  find(requestId) {
    return this.entries.find((e) => e.request_id === requestId);
  }

  retry(requestId) {
    const e = this.find(requestId);
    if (!e || e.status !== 'failed') return;
    Object.assign(e, { status: 'pending', attempts: 0, retryAt: 0, error: null });
    this.changed();
    this.pump();
  }

  discard(requestId) {
    const e = this.find(requestId);
    if (!e || e.status === 'sending') return;
    this.entries = this.entries.filter((x) => x !== e);
    this.changed();
  }

  forScan(scanId) {
    return this.entries.filter((e) => e.scan_id === scanId);
  }

  counts(scanId) {
    const list = scanId === undefined ? this.entries : this.forScan(scanId);
    return {
      pending: list.filter((e) => e.status !== 'failed').length,
      failed: list.filter((e) => e.status === 'failed').length,
    };
  }

  next() {
    const t = this.now();
    return this.entries.find((e) => e.status === 'pending' && (e.retryAt || 0) <= t);
  }

  schedule() {
    const waiting = this.entries.filter((e) => e.status === 'pending' && e.retryAt > this.now());
    if (!waiting.length || this.timer) return;
    const delay = Math.max(0, Math.min(...waiting.map((e) => e.retryAt)) - this.now());
    this.timer = this.setTimer(() => {
      this.timer = null;
      this.pump();
    }, delay);
  }

  // Send queued edits one at a time, in order. Returns when nothing is
  // ready to send.
  async pump() {
    if (this.running) return;
    this.running = true;
    try {
      let e;
      while ((e = this.next())) {
        e.status = 'sending';
        e.attempts += 1;
        this.changed();
        try {
          const result = await this.send(e);
          this.entries = this.entries.filter((x) => x !== e);
          this.changed();
          this.onSaved(e, result);
        } catch (err) {
          e.error = { status: err.status ?? 0, code: err.code || 'error', message: err.message };
          if (err.retryable && e.attempts < MAX_AUTO_ATTEMPTS) {
            e.status = 'pending';
            e.retryAt = this.now() + 1000 * 2 ** (e.attempts - 1);
          } else {
            e.status = 'failed';
          }
          this.changed();
        }
      }
    } finally {
      this.running = false;
      this.schedule();
    }
  }
}

// Pins as the reviewer should see them: the server's pins with queued edits
// applied on top. Failed edits are not applied; the pin is flagged instead.
export function projectPins(serverPins, entries, scanId) {
  const pins = serverPins.map((p) => ({ ...p, pending: false, failed: null }));
  const byId = new Map(pins.map((p) => [p.pin_id, p]));
  for (const e of entries) {
    if (e.scan_id !== scanId) continue;
    const failed = e.status === 'failed';
    if (e.action === 'add_manual') {
      const id = 'tmp:' + e.request_id;
      if (!byId.has(id)) {
        const p = { pin_id: id, origin: 'manual', state: 'added', x: e.x, y: e.y, box: null,
          score: null, version: 0, temp: true, request_id: e.request_id,
          pending: !failed, failed: failed ? e.error : null };
        pins.push(p);
        byId.set(id, p);
      }
      continue;
    }
    const p = byId.get(e.pin_id);
    if (!p) continue;
    if (failed) {
      p.failed = e.error;
      continue;
    }
    p.pending = true;
    p.state = nextState(p, e.action);
  }
  return pins;
}

export function nextState(pin, action) {
  switch (action) {
    case 'approve': return 'approved';
    case 'reject': return 'rejected';
    case 'remove_manual': return 'removed';
    case 'delete_pin': return pin.origin === 'machine' ? 'rejected' : 'removed';
    default: return pin.state;
  }
}

// Merge a freshly loaded pin list into the known one, keeping whichever copy
// of each pin has the higher version. A slow load that read the database
// before a save finished can then never roll a pin back.
export function mergePins(known, loaded) {
  const out = new Map(known.map((p) => [p.pin_id, p]));
  for (const p of loaded) {
    const k = out.get(p.pin_id);
    if (!k || p.version >= k.version) out.set(p.pin_id, p);
  }
  return [...out.values()];
}
