// View transform between canonical raster pixels and viewport CSS pixels.
//
// Canonical coordinates (contracts §2): origin top-left, x right, y down,
// 200 DPI. Pixel (i, j) covers [i, i+1) x [j, j+1).
//
// A view is {zoom, rotation, panX, panY}. It maps a canonical point p to the
// viewport point s = R(rotation) * zoom * p + pan, where R turns clockwise
// (on a y-down screen) by 0, 90, 180 or 270 degrees. The page image and every
// overlay are drawn through this one matrix, and pointer positions are
// converted back with its exact inverse, so they cannot drift apart.
//
// Pure functions only; no DOM. Imported by app.js and by the node tests.

export const MIN_ZOOM = 0.02;
export const MAX_ZOOM = 32;
export const ROTATIONS = [0, 90, 180, 270];

// Matrix in canvas setTransform order: X = a*x + c*y + e, Y = b*x + d*y + f.
export function matrix(view) {
  const z = view.zoom;
  let a, b, c, d;
  switch (normRotation(view.rotation)) {
    case 0: a = z; b = 0; c = 0; d = z; break;
    case 90: a = 0; b = z; c = -z; d = 0; break;
    case 180: a = -z; b = 0; c = 0; d = -z; break;
    case 270: a = 0; b = -z; c = z; d = 0; break;
  }
  return { a, b, c, d, e: view.panX, f: view.panY };
}

export function normRotation(r) {
  const n = ((Math.round(r / 90) * 90) % 360 + 360) % 360;
  return n;
}

export function toScreen(view, x, y) {
  const m = matrix(view);
  return { x: m.a * x + m.c * y + m.e, y: m.b * x + m.d * y + m.f };
}

export function toCanonical(view, sx, sy) {
  const m = matrix(view);
  const det = m.a * m.d - m.b * m.c;
  const u = sx - m.e;
  const v = sy - m.f;
  return { x: (m.d * u - m.c * v) / det, y: (-m.b * u + m.a * v) / det };
}

export function clampZoom(z) {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
}

// New view with the canonical point p placed at viewport point (sx, sy).
function placing(view, p, sx, sy) {
  const m = matrix({ ...view, panX: 0, panY: 0 });
  return { ...view, panX: sx - (m.a * p.x + m.c * p.y), panY: sy - (m.b * p.x + m.d * p.y) };
}

// Zoom by `factor`, keeping the canonical point under (sx, sy) fixed.
export function zoomAt(view, factor, sx, sy) {
  const p = toCanonical(view, sx, sy);
  return placing({ ...view, zoom: clampZoom(view.zoom * factor) }, p, sx, sy);
}

export function setZoomAt(view, zoom, sx, sy) {
  return zoomAt(view, clampZoom(zoom) / view.zoom, sx, sy);
}

export function panBy(view, dx, dy) {
  return { ...view, panX: view.panX + dx, panY: view.panY + dy };
}

// Rotate the view a quarter turn clockwise about viewport point (sx, sy).
export function rotateAt(view, sx, sy, quarterTurns = 1) {
  const p = toCanonical(view, sx, sy);
  return placing({ ...view, rotation: normRotation(view.rotation + 90 * quarterTurns) }, p, sx, sy);
}

// Centre canonical point (x, y) in a viewport of size vw x vh.
export function centreOn(view, x, y, vw, vh) {
  return placing(view, { x, y }, vw / 2, vh / 2);
}

// Fit the whole page into the viewport, keeping the current rotation.
export function fit(view, frame, vw, vh, margin = 16) {
  const r = normRotation(view.rotation);
  const w = r % 180 === 0 ? frame.width : frame.height;
  const h = r % 180 === 0 ? frame.height : frame.width;
  const zoom = clampZoom(Math.min((vw - 2 * margin) / w, (vh - 2 * margin) / h));
  return centreOn({ ...view, rotation: r, zoom }, frame.width / 2, frame.height / 2, vw, vh);
}

// Keep at least `keep` CSS px of the page inside the viewport, so the page
// can't be panned out of sight.
export function clampPan(view, frame, vw, vh, keep = 48) {
  const corners = [[0, 0], [frame.width, 0], [0, frame.height], [frame.width, frame.height]]
    .map(([x, y]) => toScreen(view, x, y));
  const minX = Math.min(...corners.map((c) => c.x));
  const maxX = Math.max(...corners.map((c) => c.x));
  const minY = Math.min(...corners.map((c) => c.y));
  const maxY = Math.max(...corners.map((c) => c.y));
  let dx = 0;
  let dy = 0;
  if (maxX < keep) dx = keep - maxX;
  else if (minX > vw - keep) dx = vw - keep - minX;
  if (maxY < keep) dy = keep - maxY;
  else if (minY > vh - keep) dy = vh - keep - minY;
  return dx || dy ? panBy(view, dx, dy) : view;
}

// Constrain a canonical point to the page: x in [0, width], y in [0, height].
export function clampPoint(p, frame) {
  return {
    x: Math.min(frame.width, Math.max(0, p.x)),
    y: Math.min(frame.height, Math.max(0, p.y)),
  };
}

export function insidePage(p, frame) {
  return p.x >= 0 && p.y >= 0 && p.x <= frame.width && p.y <= frame.height;
}

// Whole-pixel box {x, y, width, height} covering the dragged rectangle,
// clipped to the page. Returns null when it has no area.
export function boxFromDrag(p0, p1, frame) {
  const a = clampPoint(p0, frame);
  const b = clampPoint(p1, frame);
  const x0 = Math.floor(Math.min(a.x, b.x));
  const y0 = Math.floor(Math.min(a.y, b.y));
  const x1 = Math.min(frame.width, Math.ceil(Math.max(a.x, b.x)));
  const y1 = Math.min(frame.height, Math.ceil(Math.max(a.y, b.y)));
  if (x1 - x0 < 1 || y1 - y0 < 1) return null;
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
}

// Screen-space corners of a canonical box, in drawing order.
export function boxCorners(view, box) {
  const x1 = box.x + box.width;
  const y1 = box.y + box.height;
  return [[box.x, box.y], [x1, box.y], [x1, y1], [box.x, y1]].map(([x, y]) => toScreen(view, x, y));
}

// Index of the pin nearest (sx, sy) within `radius` CSS px, or -1.
export function hitTest(view, pins, sx, sy, radius = 10) {
  let best = -1;
  let bestD = radius * radius;
  pins.forEach((p, i) => {
    const s = toScreen(view, p.x, p.y);
    const d = (s.x - sx) ** 2 + (s.y - sy) ** 2;
    if (d <= bestD) {
      bestD = d;
      best = i;
    }
  });
  return best;
}
