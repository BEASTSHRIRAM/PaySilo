// Tiny Lottie (bodymovin 5.x) authoring kit used by build.mjs.
// Shapes are authored in absolute canvas coordinates; animated groups set
// anchor == position == pivot so rotation/scale happen around that pivot.

export const FPS = 60;

export function hex(color, alpha = 1) {
  const h = color.replace("#", "");
  const n = parseInt(h, 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255, alpha];
}

const arr = (v) => (Array.isArray(v) ? v : [v]);
export const st = (k) => ({ a: 0, k });

/** Animated property from [[frame, value], ...] with ease-in-out between keys. */
export function anim(keys, ease = [0.42, 0.58]) {
  return {
    a: 1,
    k: keys.map(([t, v], idx) =>
      idx < keys.length - 1
        ? { t, s: arr(v), i: { x: [ease[1]], y: [1] }, o: { x: [ease[0]], y: [0] } }
        : { t, s: arr(v) },
    ),
  };
}

/** Accept a static value or an anim() object. */
const prop = (v, fallback) => (v === undefined ? st(fallback) : v && v.a === 1 ? v : st(v));

export const ellipse = (x, y, w, h) => ({ ty: "el", d: 1, p: st([x, y]), s: st([w, h]) });
export const rect = (x, y, w, h, r = 0) => ({ ty: "rc", d: 1, p: st([x, y]), s: st([w, h]), r: st(r) });
export const fill = (color, opacity = 100) => ({ ty: "fl", c: st(hex(color)), o: prop(opacity, 100), r: 1 });
export const stroke = (color, width, opacity = 100) => ({
  ty: "st", c: st(hex(color)), o: prop(opacity, 100), w: st(width), lc: 2, lj: 2, ml: 4,
});
export const trim = (start, end) => ({ ty: "tm", s: prop(start, 0), e: prop(end, 100), o: st(0), m: 1 });

/** Closed (or open) path through points with Catmull-Rom smoothing. */
export function smooth(points, { closed = true, k = 1 / 6 } = {}) {
  const n = points.length;
  const i = [], o = [];
  for (let idx = 0; idx < n; idx++) {
    const prev = points[closed ? (idx - 1 + n) % n : Math.max(0, idx - 1)];
    const next = points[closed ? (idx + 1) % n : Math.min(n - 1, idx + 1)];
    const t = [(next[0] - prev[0]) * k, (next[1] - prev[1]) * k];
    o.push(t);
    i.push([-t[0], -t[1]]);
  }
  if (!closed) {
    i[0] = [0, 0];
    o[n - 1] = [0, 0];
  }
  return { ty: "sh", d: 1, ks: st({ i, o, v: points, c: closed }) };
}

/** Raw bezier path: verts [[x,y]], ins/outs relative tangents. */
export const path = (v, i, o, closed = false) => ({ ty: "sh", d: 1, ks: st({ i, o, v, c: closed }) });

/** A group = shapes + paint, with an optional pivoted transform. */
export function group(name, items, { pivot = [0, 0], position, scale, rotation, opacity } = {}) {
  return {
    ty: "gr",
    nm: name,
    it: [
      ...items,
      {
        ty: "tr",
        a: st(pivot),
        p: prop(position, pivot),
        s: prop(scale, [100, 100]),
        r: prop(rotation, 0),
        o: prop(opacity, 100),
        sk: st(0),
        sa: st(0),
      },
    ],
  };
}

let nextInd = 1;
export function shapeLayer(name, groups, op, { pivot = [0, 0], position, scale, rotation, opacity } = {}) {
  return {
    ddd: 0,
    ind: nextInd++,
    ty: 4,
    nm: name,
    sr: 1,
    ks: {
      o: prop(opacity, 100),
      r: prop(rotation, 0),
      p: prop(position, pivot),
      a: st(pivot),
      s: prop(scale, [100, 100]),
    },
    ao: 0,
    shapes: groups,
    ip: 0,
    op,
    st: 0,
    bm: 0,
  };
}

/** layersBackToFront: authored back-first; Lottie renders layers[0] on top. */
export function composition(name, { w, h, op, layers }) {
  nextInd = 1;
  return { v: "5.7.4", fr: FPS, ip: 0, op, w, h, nm: name, ddd: 0, assets: [], layers: [...layers].reverse() };
}

/** Loop-friendly oscillation keyframes: values cycle evenly over [0, op]. */
export function cycle(op, values) {
  const step = op / values.length;
  return anim([...values.map((v, idx) => [Math.round(idx * step), v]), [op, values[0]]]);
}

export function star(cx, cy, r) {
  const pts = [];
  for (let idx = 0; idx < 8; idx++) {
    const rad = (Math.PI / 4) * idx - Math.PI / 2;
    const rr = idx % 2 === 0 ? r : r * 0.32;
    pts.push([cx + Math.cos(rad) * rr, cy + Math.sin(rad) * rr]);
  }
  return path(pts, pts.map(() => [0, 0]), pts.map(() => [0, 0]), true);
}
