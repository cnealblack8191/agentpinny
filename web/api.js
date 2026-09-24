// Thin client for the viewer's local JSON API (see pinny/viewer/server.py).

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status; // 0 = network failure
    this.code = code;
  }
  // Worth retrying later: the request may not have reached the server.
  get retryable() {
    return this.status === 0 || this.status >= 500;
  }
}

async function request(method, path, { json, body, headers } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: json !== undefined ? { 'Content-Type': 'application/json', ...headers } : headers,
      body: json !== undefined ? JSON.stringify(json) : body,
    });
  } catch (err) {
    throw new ApiError(0, 'network_error', 'Could not reach the Pinny server. Is it still running?');
  }
  const isJson = (res.headers.get('Content-Type') || '').includes('application/json');
  const data = isJson ? await res.json().catch(() => null) : null;
  if (!res.ok) {
    const e = data && data.error;
    throw new ApiError(res.status, e ? e.code : 'http_' + res.status,
      e ? e.message : `Server returned ${res.status}.`);
  }
  return data;
}

const enc = encodeURIComponent;
const pagePath = (version, page) => `/api/documents/${enc(version)}/pages/${page}`;

export const api = {
  health: () => request('GET', '/api/health'),
  documents: () => request('GET', '/api/documents'),
  upload: (file, documentId) => {
    let q = `?filename=${enc(file.name || 'upload.pdf')}`;
    if (documentId) q += `&document_id=${enc(documentId)}`;
    return request('POST', '/api/documents' + q,
      { body: file, headers: { 'Content-Type': 'application/pdf' } });
  },
  frame: (version, page) => request('GET', pagePath(version, page) + '/frame'),
  rasterUrl: (version, page) => pagePath(version, page) + '/raster.png',
  pageScans: (version, page) => request('GET', pagePath(version, page) + '/scans'),
  models: () => request('GET', '/api/models'),
  scan: (body) => request('POST', '/api/scans', { json: body }),
  scanState: (scanId) => request('GET', `/api/scans/${enc(scanId)}`),
  act: (scanId, body) => request('POST', `/api/scans/${enc(scanId)}/actions`, { json: body }),
  reportUrl: (scanId) => `/api/scans/${enc(scanId)}/report`,
};

export function newRequestId() {
  if (globalThis.crypto && crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
