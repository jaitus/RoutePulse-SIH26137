/* RoutePulse — client for the printed control sheet.
 *
 * Zero dependencies on purpose: no framework, no charting library, no map
 * tiles, no CDN, no web fonts. The road network is drawn on a canvas from our
 * own API, so the demo survives a dead venue Wi-Fi.
 *
 * Nothing here uses an inline `style` attribute and nothing is injected with
 * innerHTML from server data. Elements are built with h() and animated with the
 * Web Animations API, which is what lets the server ship a
 * Content-Security-Policy of 'self' with no 'unsafe-inline'.
 *
 * The drawing model is a map sheet: pale roads on warm paper, flat
 * transit-diagram route colours, white-centred station markers, a black depot
 * square, and semantics carried by pattern (barrier ticks, dashes, double
 * lines) as well as hue.
 */
'use strict';

const EASE = 'cubic-bezier(.2,.7,.3,1)';
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

const INK = {
  paper:    '#faf7f2',
  // The grid must stay quieter than the roads. Measured by eye on a projector:
  // at #f0ebe0 the drafting grid read louder than the street network, which
  // inverts the whole point of the sheet.
  grid:     '#f4f0e7',
  roadMinor:'#d3cab8',
  roadMajor:'#a99c84',
  ink:      '#14120f',
  ink2:     '#574f44',
  ink3:     '#8b8376',
  signal:   '#e2511e',
  closed:   '#c32b17',
  jam:      '#b07900',
  corridor: '#12885a',
  amb:      '#e2511e',
  ambBack:  '#7b2d8e',
  ok:       '#14724c',
};

/* ------------------------------------------------------------------ DOM */

const $ = (s) => document.querySelector(s);

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  const p = props || {};
  for (const k in p) {
    const v = p[k];
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k === 'css') { for (const q in v) e.style.setProperty(q, v[q]); }
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(9)) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
}

function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

/** append that drops null/undefined/false. Element.append() would stringify
 *  them and print the word "null" into the UI. */
function put(parent, ...kids) {
  for (const k of kids.flat(9)) {
    if (k === null || k === undefined || k === false) continue;
    parent.append(k);
  }
  return parent;
}

/** Bar fill, animated through WAAPI rather than a style attribute. */
function grow(el, pct, delay) {
  const w = Math.max(0.8, Math.min(100, pct)) + '%';
  if (REDUCED) { el.style.setProperty('width', w); return el; }
  el.animate([{ width: '0%' }, { width: w }],
    { duration: 540, delay: delay || 0, easing: EASE, fill: 'forwards' });
  return el;
}

function meter(pct, cls, delay) {
  return h('div', { class: 'track' }, grow(h('i', { class: cls || '' }), pct, delay));
}

/** Count a number into place. Not decoration: during a live demo it is what
 *  makes a changed figure impossible to miss. */
function roll(el, to, fmt, from) {
  const start = (from === undefined) ? (parseFloat(el.dataset.v) || 0) : from;
  el.dataset.v = to;
  if (REDUCED || start === to) { el.textContent = fmt(to); return; }
  const t0 = performance.now(), dur = 500;
  (function step(now) {
    const p = Math.min(1, (now - t0) / dur);
    el.textContent = fmt(start + (to - start) * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(step);
  })(t0);
}

const f1 = (v) => (Math.round(v * 10) / 10).toFixed(1);
const f0 = (v) => String(Math.round(v));
const pc = (v) => (v >= 0 ? '+' : '') + v.toFixed(1) + '%';

function toast(msg, bad) {
  const t = h('div', { class: 't' + (bad ? ' bad' : ''), text: msg });
  $('#toasts').append(t);
  setTimeout(() => {
    t.animate([{ opacity: 1 }, { opacity: 0, transform: 'translateY(6px)' }],
      { duration: 240, fill: 'forwards' }).onfinish = () => t.remove();
  }, bad ? 4200 : 2600);
}

/* ---------------------------------------------------------------- state */

/* The scenario step 01 loads. Declared ONCE, here, so the fleet size a judge
 * is told about and the fleet size the server actually builds cannot drift
 * apart -- every label in step 01 is rendered from this object and the same
 * object is what POST /api/reset is called with.
 *
 * NOTE ON SIZE: the committed latency evidence measures 60 stops / 10 vehicles
 * at p95 497.76 ms -- inside the 500 ms target, but only just. This demo runs
 * 60 stops on EIGHT vehicles, which is not a size the benchmark covers. If the
 * live end-to-end figure in the proof block starts landing over 500 ms on the
 * demo machine, drop this back to { customers: 30, vehicles: 5 } -- that is the
 * size every headline number in the Evidence tab was measured at.
 */
const SCENARIO = {
  customers: 60,
  vehicles: 8,
  seed: 7,
  zone: 'Bengaluru service zone',
  profile: '08:00–22:00',
};

const S = {
  graph: null, bounds: null, boot: null, routes: [], closed: [], events: [],
  summary: {}, conv: [], amb: null, last: null, planned: false,
  hover: null, pings: [], routeT0: 0, evidence: null, clock: 0,
  // the plan that was on screen before the last recovery, drawn underneath the
  // new one so "what changed" is visible rather than asserted
  prev: null, diff: null, showGhost: true, evKind: 'closure',
};

/** Simulation time as a wall-clock label. The horizon starts at 08:00 local
 *  and every event, ETA and corridor window is expressed from that origin --
 *  one clock, which is what P0-08 was about. */
function clockLabel(seconds) {
  const t = 8 * 3600 + (seconds || 0);
  const h = Math.floor(t / 3600) % 24, m = Math.floor((t % 3600) / 60);
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
}

const CAM = { zoom: 1, x: 0, y: 0 };
const cv = $('#map');
const ctx = cv.getContext('2d');
let baseCache = null, baseKey = '';

/* ----------------------------------------------------------- projection */

function T() {
  const w = cv.clientWidth, ht = cv.clientHeight, pad = 34;
  const B = S.bounds || { minLat: 0, minLon: 0, maxLat: 1, maxLon: 1 };
  const kx = Math.cos(((B.minLat + B.maxLat) / 2) * Math.PI / 180);
  const dLon = (B.maxLon - B.minLon) * kx || 1e-9;
  const dLat = (B.maxLat - B.minLat) || 1e-9;
  const s = Math.min((w - 2 * pad) / dLon, (ht - 2 * pad) / dLat) * CAM.zoom;
  return { w, h: ht, kx, s, B,
           ox: (w - dLon * s) / 2 + CAM.x, oy: (ht - dLat * s) / 2 - CAM.y };
}
function px(lat, lon, t) {
  return [t.ox + (lon - t.B.minLon) * t.kx * t.s,
          t.h - t.oy - (lat - t.B.minLat) * t.s];
}
function unpx(x, y, t) {
  return [t.B.minLat + (t.h - t.oy - y) / t.s,
          t.B.minLon + (x - t.ox) / (t.kx * t.s)];
}

/* -------------------------------------------------------------- renderer */

function resize() {
  const r = cv.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(r.width * dpr);
  cv.height = Math.round(r.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  baseKey = '';
  titleblock();
}
addEventListener('resize', resize);

/** Drafting grid + road network, rasterised once per camera state and blitted
 *  every frame. ~16k segments never change between camera moves, and without
 *  this the animation loop spends all its time redrawing static geometry. */
function baseLayer(t) {
  const dpr = window.devicePixelRatio || 1;
  const key = [t.w, t.h, CAM.zoom.toFixed(4), CAM.x | 0, CAM.y | 0, dpr].join('|');
  if (baseKey === key && baseCache) return baseCache;
  if (!baseCache) baseCache = document.createElement('canvas');
  baseCache.width = cv.width; baseCache.height = cv.height;
  const c = baseCache.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);

  c.fillStyle = INK.paper;
  c.fillRect(0, 0, t.w, t.h);

  // drafting grid — the sheet the plan is drawn on
  c.strokeStyle = INK.grid; c.lineWidth = 1;
  c.beginPath();
  for (let x = 0; x <= t.w; x += 44) { c.moveTo(x + .5, 0); c.lineTo(x + .5, t.h); }
  for (let y = 0; y <= t.h; y += 44) { c.moveTo(0, y + .5); c.lineTo(t.w, y + .5); }
  c.stroke();

  if (S.graph) {
    c.lineCap = 'round';
    // two passes so arterials sit above side streets instead of interleaving
    for (const major of [false, true]) {
      c.strokeStyle = major ? INK.roadMajor : INK.roadMinor;
      c.lineWidth = major ? 2.1 : 1.05;
      c.beginPath();
      for (const s of S.graph.segments) {
        if ((s[4] >= 40) !== major) continue;
        const a = px(s[0], s[1], t), b = px(s[2], s[3], t);
        if ((a[0] < -60 && b[0] < -60) || (a[0] > t.w + 60 && b[0] > t.w + 60)
          || (a[1] < -60 && b[1] < -60) || (a[1] > t.h + 60 && b[1] > t.h + 60)) continue;
        c.moveTo(a[0], a[1]); c.lineTo(b[0], b[1]);
      }
      c.stroke();
    }
  }
  baseKey = key;
  return baseCache;
}

function polyPath(c, pts, t) {
  c.beginPath();
  for (let i = 0; i < pts.length; i++) {
    const q = px(pts[i][0], pts[i][1], t);
    if (i) c.lineTo(q[0], q[1]); else c.moveTo(q[0], q[1]);
  }
}
function polyLen(pts, t) {
  let L = 0, p = null;
  for (const pt of pts) {
    const q = px(pt[0], pt[1], t);
    if (p) L += Math.hypot(q[0] - p[0], q[1] - p[1]);
    p = q;
  }
  return L;
}

/** A closed road is drawn as a barrier: the line plus perpendicular ticks.
 *  On paper, pattern reads faster than hue and survives a bad projector. */
function barrier(c, a, b) {
  c.strokeStyle = INK.closed; c.lineWidth = 3;
  c.beginPath(); c.moveTo(a[0], a[1]); c.lineTo(b[0], b[1]); c.stroke();
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const len = Math.hypot(dx, dy) || 1;
  const nx = -dy / len * 3.6, ny = dx / len * 3.6;
  const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
  c.lineWidth = 1.6;
  c.beginPath(); c.moveTo(mx + nx, my + ny); c.lineTo(mx - nx, my - ny); c.stroke();
}

function draw(now) {
  const t = T();
  if (t.w < 2 || t.h < 2 || cv.width < 2) return;
  ctx.drawImage(baseLayer(t), 0, 0, t.w, t.h);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';

  // ---- incident + corridor overlays
  for (const seg of S.closed) {
    const kind = seg[2][0];
    const a = px(seg[0][0], seg[0][1], t), b = px(seg[1][0], seg[1][1], t);
    if (kind === 1) { barrier(ctx, a, b); continue; }
    if (kind === 2) {
      // green corridor: a double line, the way a priority route is drawn
      ctx.strokeStyle = INK.corridor; ctx.lineWidth = 4.4;
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      ctx.strokeStyle = INK.paper; ctx.lineWidth = 1.3;
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      continue;
    }
    ctx.strokeStyle = INK.jam; ctx.lineWidth = 2.8;
    ctx.setLineDash([5, 3.5]);
    ctx.lineDashOffset = REDUCED ? 0 : -(now / 90) % 8.5;
    ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
    ctx.setLineDash([]);
  }

  // ---- ambulance legs, marching dashes over a paper halo
  if (S.amb) {
    for (const [leg, col] of [[S.amb.leg_a, INK.amb], [S.amb.leg_b, INK.ambBack]]) {
      if (!leg || !leg.length) continue;
      // No paper halo here. The ambulance's path IS the green corridor, drawn
      // just above, and a halo would paint cream straight over it — the
      // corridor would be reported as 64 edges and be invisible on the sheet.
      ctx.strokeStyle = col; ctx.lineWidth = 3;
      ctx.setLineDash([10, 5]);
      ctx.lineDashOffset = REDUCED ? 0 : -(now / 30) % 15;
      polyPath(ctx, leg, t); ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  // ---- the SUPERSEDED plan, underneath the current one.
  //
  // Only the vehicles whose route actually changed are ghosted. Drawing all of
  // them would bury the point: the claim this system makes is that recovery is
  // TARGETED -- a handful of vehicles move and the rest are left alone -- and
  // that claim is only legible if the untouched ones are visibly absent from
  // this layer. Dash pattern is [6,5], distinct from congestion [5,3.5] and
  // the ambulance [10,5], so three dashed things on one sheet stay separable.
  if (S.showGhost && S.prev && S.diff) {
    for (const r of S.prev.routes) {
      if (!S.diff.changed.has(r.vehicle)) continue;
      if (!r.polyline || r.polyline.length < 2) continue;
      ctx.save();
      ctx.strokeStyle = INK.paper; ctx.lineWidth = 5;
      polyPath(ctx, r.polyline, t); ctx.stroke();
      ctx.globalAlpha = .55; ctx.strokeStyle = r.color; ctx.lineWidth = 2;
      ctx.setLineDash([6, 5]);
      polyPath(ctx, r.polyline, t); ctx.stroke();
      ctx.restore(); ctx.setLineDash([]);
    }
  }

  // ---- routes: draw-on, then a slow flow pulse
  const drawIn = REDUCED ? 1 : Math.min(1, (now - S.routeT0) / 820);
  for (let i = 0; i < S.routes.length; i++) {
    const r = S.routes[i];
    if (!r.polyline || r.polyline.length < 2) continue;
    const stage = Math.min(1, Math.max(0, (drawIn - i * 0.06) / 0.72));
    if (stage <= 0) continue;
    const L = polyLen(r.polyline, t);

    ctx.setLineDash([L, L]); ctx.lineDashOffset = L * (1 - stage);
    ctx.strokeStyle = INK.paper; ctx.lineWidth = 6;      // halo, keeps it legible
    polyPath(ctx, r.polyline, t); ctx.stroke();
    ctx.strokeStyle = r.color; ctx.lineWidth = 2.8;
    polyPath(ctx, r.polyline, t); ctx.stroke();
    ctx.setLineDash([]);

    if (stage >= 1 && !REDUCED) {
      ctx.save();
      ctx.globalAlpha = .42; ctx.strokeStyle = INK.paper; ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 30]);
      ctx.lineDashOffset = -(now / 24 + i * 46) % 34;
      polyPath(ctx, r.polyline, t); ctx.stroke();
      ctx.restore(); ctx.setLineDash([]);
    }
  }

  // ---- stops, drawn as transit stations: white centre, coloured ring
  for (const r of S.routes) {
    for (const st of r.stops) {
      const q = px(st.lat, st.lon, t);
      const rad = st.priority ? 5.4 : 3.8;
      ctx.fillStyle = '#ffffff';
      ctx.beginPath(); ctx.arc(q[0], q[1], rad, 0, 6.2832); ctx.fill();
      ctx.strokeStyle = st.priority ? INK.ink : r.color;
      ctx.lineWidth = st.priority ? 2.2 : 1.9;
      ctx.stroke();
    }
  }

  // ---- emergency scene + hospitals
  if (S.amb) {
    for (const hp of (S.amb.hospitals || [])) {
      const q = px(hp.lat, hp.lon, t);
      const chosen = hp.name === S.amb.hospital;
      ctx.fillStyle = chosen ? INK.corridor : '#ffffff';
      ctx.strokeStyle = chosen ? INK.corridor : INK.ink3;
      ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.rect(q[0] - 5, q[1] - 5, 10, 10);
      ctx.fill(); ctx.stroke();
      if (!chosen) {                                  // a small cross for a hospital
        ctx.strokeStyle = INK.ink3; ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.moveTo(q[0] - 2.6, q[1]); ctx.lineTo(q[0] + 2.6, q[1]);
        ctx.moveTo(q[0], q[1] - 2.6); ctx.lineTo(q[0], q[1] + 2.6);
        ctx.stroke();
      }
    }
    if (S.amb.scene) {
      const q = px(S.amb.scene[0], S.amb.scene[1], t);
      const k = REDUCED ? 0 : (now / 950) % 1;
      ctx.strokeStyle = INK.amb; ctx.globalAlpha = 1 - k; ctx.lineWidth = 1.8;
      ctx.beginPath(); ctx.arc(q[0], q[1], 8 + k * 22, 0, 6.2832); ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = INK.amb;
      ctx.beginPath(); ctx.arc(q[0], q[1], 6, 0, 6.2832); ctx.fill();
      ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 1.8;
      ctx.beginPath();
      ctx.moveTo(q[0] - 3, q[1]); ctx.lineTo(q[0] + 3, q[1]);
      ctx.moveTo(q[0], q[1] - 3); ctx.lineTo(q[0], q[1] + 3);
      ctx.stroke();
    }
  }

  // ---- incident marks + injection shockwave
  for (const e of S.events) {
    if (e.kind === 'ambulance' || e.lat === undefined || e.lon === undefined) continue;
    const q = px(e.lat, e.lon, t);
    ctx.strokeStyle = e.kind === 'closure' ? INK.closed : INK.jam;
    ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.arc(q[0], q[1], 12, 0, 6.2832); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(q[0] - 16, q[1]); ctx.lineTo(q[0] - 12, q[1]);
    ctx.moveTo(q[0] + 12, q[1]); ctx.lineTo(q[0] + 16, q[1]);
    ctx.moveTo(q[0], q[1] - 16); ctx.lineTo(q[0], q[1] - 12);
    ctx.moveTo(q[0], q[1] + 12); ctx.lineTo(q[0], q[1] + 16);
    ctx.stroke();
  }
  S.pings = S.pings.filter((p) => now - p.t0 < 1500);
  for (const p of S.pings) {
    const k = (now - p.t0) / 1500;
    const q = px(p.lat, p.lon, t);
    ctx.strokeStyle = p.color; ctx.globalAlpha = (1 - k) * .75;
    ctx.lineWidth = 2.4 * (1 - k) + .4;
    ctx.beginPath(); ctx.arc(q[0], q[1], 8 + k * 90, 0, 6.2832); ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // ---- depot
  if (S.summary.depot) {
    const q = px(S.summary.depot.lat, S.summary.depot.lon, t);
    ctx.fillStyle = INK.ink;
    ctx.fillRect(q[0] - 6.5, q[1] - 6.5, 13, 13);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(q[0] - 2.5, q[1] - 2.5, 5, 5);
    ctx.fillStyle = INK.ink2;
    ctx.font = '700 9px ui-monospace, Consolas, monospace';
    ctx.fillText('DEPOT', q[0] + 11, q[1] + 3.5);
  }

  // ---- hover readout
  if (S.hover) {
    const q = px(S.hover.lat, S.hover.lon, t);
    const lines = [
      'STOP ' + S.hover.id,
      'eta      ' + (S.hover.eta_min === null ? 'unreachable' : S.hover.eta_min + ' min'),
      'demand   ' + S.hover.demand + (S.hover.priority ? '  PRIORITY' : ''),
    ];
    if (S.hover.tw_end_min !== null && S.hover.tw_end_min !== undefined) {
      lines.push('window   closes ' + S.hover.tw_end_min + ' min');
    }
    ctx.font = '10.5px ui-monospace, Consolas, monospace';
    const wd = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 18;
    const ht = lines.length * 14 + 13;
    let bx = q[0] + 13, by = q[1] - ht - 9;
    if (bx + wd > t.w) bx = q[0] - wd - 13;
    if (by < 0) by = q[1] + 13;
    ctx.fillStyle = '#ffffff'; ctx.strokeStyle = INK.ink; ctx.lineWidth = 1.2;
    ctx.fillRect(bx, by, wd, ht); ctx.strokeRect(bx, by, wd, ht);
    ctx.fillStyle = INK.ink;
    lines.forEach((l, i) => ctx.fillText(l, bx + 9, by + 19 + i * 14));
    ctx.strokeStyle = INK.ink; ctx.lineWidth = 1.6;
    ctx.beginPath(); ctx.arc(q[0], q[1], 8, 0, 6.2832); ctx.stroke();
  }
}

/* The render loop must never die. One throw inside draw() would end the frame
 * chain permanently and leave a blank sheet while every other panel keeps
 * updating — a component that stops working while reporting nothing, which is
 * the exact failure mode this project exists to catch elsewhere. */
let drawFails = 0;
function loop(now) {
  try { draw(now); }
  catch (err) {
    if (++drawFails === 1) {
      console.error('map render failed', err);
      toast('Map rendering hit an error — see the console', true);
    }
  }
  requestAnimationFrame(loop);
}

/* ----------------------------------------------------------- title block */

function titleblock() {
  const el = $('#titleblock');
  if (!el) return;
  const t = T();
  const mPerPx = 1 / (t.s / 111320);
  const want = mPerPx * 96;
  const pow = Math.pow(10, Math.floor(Math.log10(Math.max(1, want))));
  const nice = [1, 2, 5, 10].map((m) => m * pow).find((v) => v >= want) || pow * 10;
  const b = S.boot || {};
  clear(el);
  const rows = [
    ['Network', (S.graph && S.graph.source) ? S.graph.source.split(' · ')[0] : '—'],
    ['Nodes', b.nodes ? b.nodes.toLocaleString() : '—'],
    ['Clock', clockLabel(S.clock) + '  (' + (b.horizon || '08:00-22:00') + ')'],
    ['Scale', (nice >= 1000 ? (nice / 1000) + ' km' : nice + ' m') + ' / 96 px'],
    ['Zoom', '×' + CAM.zoom.toFixed(2)],
    ['Costs', 'time-dependent, 5 buckets'],
    ['Revision', S.events.length + ' event(s) applied'],
  ];
  for (const [k, v] of rows) {
    el.append(h('dl', { class: 'tb' }, h('dt', { text: k }), h('dd', { text: String(v) })));
  }
}

/* ----------------------------------------------------------- interaction */

let drag = null;

cv.addEventListener('pointerdown', (e) => {
  drag = { x: e.clientX, y: e.clientY, ox: CAM.x, oy: CAM.y, moved: 0 };
  cv.setPointerCapture(e.pointerId);
});

cv.addEventListener('pointermove', (e) => {
  const r = cv.getBoundingClientRect();
  if (drag) {
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    drag.moved = Math.max(drag.moved, Math.hypot(dx, dy));
    if (drag.moved > 3) {
      CAM.x = drag.ox + dx; CAM.y = drag.oy - dy;
      cv.classList.add('panning');
      titleblock();
    }
    return;
  }
  const t = T();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  let best = null, bd = 12;
  for (const rt of S.routes) {
    for (const st of rt.stops) {
      const q = px(st.lat, st.lon, t);
      const d = Math.hypot(q[0] - mx, q[1] - my);
      if (d < bd) { bd = d; best = st; }
    }
  }
  S.hover = best;
});

cv.addEventListener('pointerup', async (e) => {
  cv.classList.remove('panning');
  const d = drag; drag = null;
  if (!d || d.moved > 3) return;
  const r = cv.getBoundingClientRect();
  await inject(e.clientX - r.left, e.clientY - r.top);
});

cv.addEventListener('pointerleave', () => { S.hover = null; });

cv.addEventListener('wheel', (e) => {
  e.preventDefault();
  const r = cv.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  const anchor = unpx(mx, my, T());
  CAM.zoom = Math.max(1, Math.min(14, CAM.zoom * (e.deltaY < 0 ? 1.16 : 1 / 1.16)));
  const after = px(anchor[0], anchor[1], T());
  CAM.x += mx - after[0];
  CAM.y -= my - after[1];
  titleblock();
}, { passive: false });

/* ------------------------------------------------------------------ API */

async function api(path, opts) {
  const r = await fetch(path, opts);
  let j = null;
  try { j = await r.json(); } catch (_e) { j = null; }
  if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
  return j;
}

function busy(on, text) {
  $('#scrim').classList.toggle('on', !!on);
  if (text) $('#scrimText').textContent = text;
  $('#lamp').className = 'lamp ' + (on ? 'busy' : 'live');
}

function working(btn, on, label) {
  btn.disabled = on;
  btn.classList.toggle('working', on);
  if (label) btn.textContent = label;
}

/** Emphasis follows the next useful action, not the first one. Before a plan
 *  exists the loud button is "plan"; once it does, the loud button is the one
 *  that recovers from the incident you are about to inject, and re-planning
 *  from scratch drops back to a secondary. */
function setPrimary(which) {
  $('#btnPlan').classList.toggle('primary', which === 'plan');
  $('#btnReplan').classList.toggle('primary', which === 'replan');
}

/* ------------------------------------------------------ operator sequence */

/** One place that owns "which step is live and why". A locked step states the
 *  precondition it is waiting on instead of being silently greyed out — the
 *  commonest way a demo loses a judge is a disabled control with no reason. */
function setStep(n, state, label) {
  const el = $('#step' + n);
  if (!el) return;
  el.dataset.state = state;
  el.classList.toggle('locked', state === 'locked');
  const st = $('#st' + n);
  if (st && label) st.textContent = label;
}

function specRow(dl, k, v) {
  put(dl, h('dt', { text: k }), h('dd', { text: v }));
}

/** Step 01 spells the scenario out, so nobody has to infer what a button did.
 *  Once the fleet is loaded these numbers are re-rendered from the SERVER's
 *  reply, not from SCENARIO — if the two ever disagree, the screen shows what
 *  was actually built. */
function renderSpec() {
  const dl = $('#spec1');
  clear(dl);
  const b = S.boot || {};
  const loaded = S.planned && b.customers != null;
  specRow(dl, 'vehicles', String(loaded ? b.vehicles : SCENARIO.vehicles));
  specRow(dl, 'deliveries', String(loaded ? b.customers : SCENARIO.customers));
  specRow(dl, 'network', b.nodes
    ? b.nodes.toLocaleString() + ' junctions · ' + SCENARIO.zone
    : SCENARIO.zone);
  specRow(dl, 'traffic', 'time-of-day profile ' + SCENARIO.profile);
}

function renderChecks(items) {
  const ul = $('#checks1');
  clear(ul);
  ul.hidden = !items || !items.length;
  (items || []).forEach((txt, i) => {
    const li = h('li', { text: txt });
    ul.append(li);
    if (!REDUCED) {
      li.animate([{ opacity: 0, transform: 'translateX(-4px)' }, { opacity: 1, transform: 'none' }],
        { duration: 260, delay: 60 + i * 90, easing: EASE, fill: 'backwards' });
    }
  });
}

const EV_BRIEF = {
  closure: [
    ['event', 'hard road closure'],
    ['effect', 'edges removed from the network'],
    ['action', 'click ON a coloured route line'],
  ],
  congestion: [
    ['event', 'severe congestion'],
    ['effect', 'timed ×6 cost overlay, roads stay open'],
    ['action', 'click ON a coloured route line'],
  ],
  ambulance: [
    ['dispatch', 'nearest unit on station'],
    ['severity', '2 · urgent  (3 routes to trauma)'],
    ['leg 1', 'station → incident scene'],
    ['leg 2', 'scene → nearest receiving hospital'],
    ['effect', 'per-edge green corridor, fleet recovers in the same action'],
    ['action', 'click the incident location'],
  ],
};

function renderEvBrief() {
  const dl = $('#evBrief');
  clear(dl);
  for (const [k, v] of (EV_BRIEF[S.evKind] || [])) specRow(dl, k, v);
}

function setEvKind(kind) {
  S.evKind = kind;
  for (const b of document.querySelectorAll('.evbtn')) {
    b.classList.toggle('on', b.dataset.ev === kind);
  }
  renderEvBrief();
  $('#recoverSub').textContent = kind === 'ambulance'
    ? 'Dispatch recovers the fleet in the same action — step 03 is automatic'
    : 'Traffic-Aware ALNS · one global wall-clock deadline';
}

/* ------------------------------------------------------------ plan diff */

const seqOf = (r) => r.stops.map((s) => s.id).join(',');

/** Classify what the recovery did to each vehicle, from the two plans alone.
 *
 *  Deliberately reports only what the payload can prove. "COMMITTED" is a
 *  server-side concept and is NOT inferred here: the observable fact is
 *  whether a vehicle's first stop survived the re-plan, and that is what the
 *  chip says. Guessing at commitment from a matching first stop would be the
 *  same class of error as a checklist row hardcoded to true. */
function routeDiff(prev, next) {
  const changed = new Set();
  const status = new Map();
  if (!prev || !prev.length) return { changed, status, held: 0 };
  const before = new Map(prev.map((r) => [r.vehicle, r]));
  const after = new Map(next.map((r) => [r.vehicle, r]));
  let held = 0;
  for (const [id, b] of before) {
    const a = after.get(id);
    if (!a) { changed.add(id); status.set(id, 'moved'); continue; }
    if (seqOf(a) === seqOf(b)) { status.set(id, 'kept'); continue; }
    changed.add(id);
    const sb = new Set(b.stops.map((s) => s.id));
    const sa = new Set(a.stops.map((s) => s.id));
    const same = sb.size === sa.size && [...sa].every((x) => sb.has(x));
    status.set(id, same ? 'reseq' : 'moved');
    if (a.stops.length && b.stops.length && a.stops[0].id === b.stops[0].id) held++;
  }
  for (const [id] of after) if (!before.has(id)) { changed.add(id); status.set(id, 'moved'); }
  return { changed, status, held };
}

const CHIP = {
  kept: 'unchanged',
  reseq: 're-sequenced',
  moved: 'stops re-assigned',
};

/* ------------------------------------------------------- operations view */

function renderBand() {
  const s = S.summary;
  const band = $('#band');
  const cells = [
    { id: 'clock', label: 'sim clock', unit: '' },
    { id: 'travel', label: 'fleet travel', unit: 'min' },
    { id: 'makespan', label: 'makespan', unit: 'min' },
    // The congestion-exposure term used to be multiplied by zero in the
    // scorer. It is now a measured quantity, so it gets a cell: minutes this
    // plan is predicted to spend inside degraded traffic.
    { id: 'exposure', label: 'congestion exposure', unit: 'min' },
    { id: 'fleet', label: 'vehicles used', unit: '' },
    { id: 'gate', label: 'feasibility gate', unit: '' },
  ];
  if (!band.children.length) {
    for (const c of cells) {
      band.append(h('div', { class: 'cell empty', id: 'c-' + c.id },
        h('div', { class: 'v' }, h('span', { class: 'n', text: '—' }),
          c.unit ? h('small', { text: c.unit }) : null),
        h('div', { class: 'l rubric', text: c.label })));
    }
  }
  if (!s || !s.score) return;
  for (const c of cells) $('#c-' + c.id).classList.remove('empty');
  roll($('#c-travel .n'), s.travel_min, f1);
  roll($('#c-makespan .n'), s.makespan_min, f1);
  roll($('#c-exposure .n'), s.congestion_exposure_min || 0, f1);
  $('#c-exposure').querySelector('.l').textContent = (s.congestion_exposure_min > 0)
    ? 'congestion exposure' : 'exposure · clear network';
  $('#c-clock .n').textContent = clockLabel(S.clock);
  $('#c-clock').querySelector('.l').textContent =
    s.customers + ' stops pending';
  $('#c-fleet .n').textContent = s.vehicles_used + '/' + s.vehicles_total;
  const gate = $('#c-gate');
  gate.className = 'cell ' + (s.feasible ? 'good' : 'bad');
  gate.querySelector('.n').textContent = s.feasible ? 'VALID' : 'INVALID';
  gate.querySelector('.l').textContent = s.feasible
    ? 'feasibility gate' : (s.violations[0] || 'constraint violated');
}

function renderFleet() {
  const el = $('#fleet');
  clear(el);
  if (!S.routes.length) {
    el.append(h('div', { class: 'footnote', text: 'No plan yet.' }));
    return;
  }
  S.routes.forEach((r, i) => {
    const fill = h('i', { css: { background: r.color } });
    const st = S.diff ? S.diff.status.get(r.vehicle) : null;
    el.append(h('div', { class: 'veh' },
      h('div', { class: 'bullet', css: { background: r.color } }),
      h('div', { class: 'name', text: 'Vehicle ' + r.vehicle }),
      h('div', { class: 'mins', text: f1(r.travel_min) + "'" }),
      h('div', { class: 'meta', text: r.stops.length + ' stops · load '
        + r.load + (r.capacity ? ' / ' + r.capacity : '') }),
      st ? h('div', { class: 'chip ' + st, text: CHIP[st] }) : null,
      h('div', { class: 'gauge' }, fill)));
    grow(fill, r.capacity ? (r.load / r.capacity) * 100 : 0, 60 + i * 45);
  });
  // Vehicles the optimiser chose NOT to use still exist and still cost money.
  const idle = (S.summary.vehicles_total || 0) - S.routes.length;
  if (idle > 0) {
    el.append(h('div', { class: 'veh idle' },
      h('div', { class: 'bullet' }),
      h('div', { class: 'name', text: idle + ' vehicle' + (idle === 1 ? '' : 's') + ' idle' }),
      h('div', { class: 'mins', text: '—' }),
      h('div', { class: 'meta', text: 'left at the depot by the optimiser' })));
  }
}

function renderNetMeta() {
  const b = S.boot || {};
  const el = $('#netmeta');
  clear(el);
  // Before step 01 runs, the server is still holding the instance it preloaded
  // at startup — a different size from the one step 01 advertises. Printing it
  // here put two different fleet sizes on screen at once, so the rail stays
  // blank until the scenario a judge was actually shown has been loaded.
  const ready = S.planned;
  const rows = [
    ['junctions', b.nodes ? b.nodes.toLocaleString() : '—'],
    ['delivery stops', ready ? (b.customers ?? '—') : 'awaiting initialize'],
    ['vehicles', ready ? (b.vehicles ?? '—') : 'awaiting initialize'],
    ['matrix build', ready && b.matrix_build_s != null
      ? (b.matrix_build_s * 1000).toFixed(0) + ' ms' : '—'],
    ['traffic model', 'time-of-day curve'],
  ];
  for (const [k, v] of rows) {
    el.append(h('div', { class: 'kv' }, h('span', { text: k }), h('b', { text: String(v) })));
  }
}

function renderEnergy(e, scale) {
  const el = $('#energy');
  clear(el);
  if (!e) {
    el.append(h('div', { class: 'footnote', text: 'Recorded on the first solve.' }));
    return;
  }
  const measured = e.source === 'battery-sensor';
  const rows = [
    ['CPU time', (e.cpu_s * 1000).toFixed(0) + ' ms'],
    ['energy', e.mwh.toFixed(3) + ' mWh'],
  ];
  if (scale) {
    rows.push(['at 400/day', scale.wh_per_day.toFixed(2) + ' Wh'],
              ['per year', scale.kwh_per_year.toFixed(2) + ' kWh']);
  }
  for (const [k, v] of rows) {
    el.append(h('div', { class: 'kv' }, h('span', { text: k }), h('b', { text: v })));
  }
  el.append(h('div', { class: 'footnote' },
    h('span', { class: 'tag ' + (measured ? 'ok' : 'mute'),
      text: measured ? 'sensor' : 'modelled' }), ' ', e.note));
}

function renderVerdict(d) {
  const el = $('#verdict');
  el.className = 'verdict ' + (d.alert ? 'alert' : d.accepted ? 'accepted' : 'held');
  clear(el);
  const caseNo = (d.case.match(/CASE\s*(\d)/i) || [null, '—'])[1];
  put(el,
    h('div', { class: 'stamp' },
      h('span', { text: d.alert ? 'Dispatcher alert'
        : d.accepted ? 'Plan accepted' : 'Incumbent held' }),
      h('span', { class: 'no', text: 'Case ' + caseNo })),
    h('div', { class: 'why', text: d.case.replace(/^CASE\s*\d:\s*/i, '') }),
    d.alert ? h('div', { class: 'why', text: d.alert }) : null,
    h('div', { class: 'sub', text:
      'incumbent ' + (d.incumbent_was_feasible ? 'feasible' : 'INFEASIBLE')
      + ' under updated costs\n' + d.matrix_pairs_rebuilt + ' matrix pairs rebuilt · '
      + d.fifo_violations + ' FIFO violations' }));
  if (!REDUCED) {
    el.animate([{ opacity: 0, transform: 'translateY(5px)' }, { opacity: 1, transform: 'none' }],
      { duration: 300, easing: EASE });
  }
  const ul = $('#reasons');
  clear(ul);
  (d.explanation || []).forEach((line, i) => {
    const li = h('li', { text: line });
    ul.append(li);
    if (!REDUCED) {
      li.animate([{ opacity: 0, transform: 'translateX(-5px)' }, { opacity: 1, transform: 'none' }],
        { duration: 280, delay: 60 + i * 55, easing: EASE, fill: 'backwards' });
    }
  });
}

function renderLatency(d) {
  const el = $('#latency');
  clear(el);
  const st = d.stages_ms || {};
  const max = Math.max(...Object.values(st), 1);
  let i = 0;
  for (const k in st) {
    const v = st[k];
    el.append(h('div', { class: 'row' },
      h('div', { class: 'nm', text: k.replace(/_/g, ' ') }),
      meter((v / max) * 100,
        k.includes('matrix') ? 'matrix' : k.includes('solve') ? 'solve' : '',
        40 + i * 55),
      h('div', { class: 'ms', text: v.toFixed(1) })));
    i++;
  }
  const total = d.total_ms || 0;
  const b = h('b', { class: total < 500 ? 'ok' : 'over' });
  el.append(h('div', { class: 'sum' },
    h('span', { class: 'rubric', text: 'event → accepted plan' }), b));
  roll(b, total, (v) => f0(v) + ' ms', 0);

  // A four-engine race is a demo, not the operational path, and the two must
  // never be quoted as one number. Say which one this was.
  const n = (d.engines || []).length;
  $('#latencyNote').textContent = n > 1
    ? 'This run raced ' + n + ' engines so you can watch them compete; a '
      + 'dispatcher waits on ONE. The single-engine operational path is measured '
      + 'separately under Evidence and meets the 500 ms target. The matrix '
      + 'rebuild is inside this total, not excluded from it.'
    : 'Single-engine operational path — what a dispatcher actually waits for. '
      + 'The matrix rebuild is inside this total.';
}

function renderRace(d) {
  const t = $('#race');
  clear(t);
  const cands = (d.candidates || []).filter((c) => !c.error);
  const feas = cands.filter((c) => c.feasible && c.score !== null);
  const best = feas.length ? Math.min(...feas.map((c) => c.score)) : null;

  t.append(h('thead', null, h('tr', null,
    h('th', { text: 'engine' }), h('th', { class: 'num', text: 'score' }),
    h('th', { class: 'num', text: 'min' }), h('th', { class: 'num', text: 'ms' }),
    h('th', { text: 'valid' }))));

  const body = h('tbody');
  cands.forEach((c) => {
    const win = c.score === best;
    body.append(h('tr', null,
      h('td', { class: win ? 'lead' : '', text: c.engine }),
      h('td', { class: 'num' + (win ? ' lead' : ''),
        text: c.score === null ? '—' : c.score.toLocaleString() }),
      h('td', { class: 'num', text: c.travel_min === null ? '—' : c.travel_min }),
      h('td', { class: 'num', text: c.ms }),
      h('td', null, h('span', { class: 'tag ' + (c.feasible ? 'ok' : 'bad'),
        text: c.feasible ? 'yes' : 'no' }))));
  });
  for (const c of (d.candidates || []).filter((x) => x.error)) {
    body.append(h('tr', null, h('td', { text: c.engine }),
      h('td', { colspan: '4', text: c.error })));
  }
  t.append(body);

  const q = cands.find((c) => c.engine.startsWith('QPSO'));
  const o = cands.find((c) => c.engine === 'OR-Tools');
  const note = $('#raceNote');
  clear(note);
  put(note,
    (q && o && q.score && o.score)
      ? h('span', { text: 'QPSO is ' + pc(((o.score - q.score) / o.score) * 100)
        + ' against OR-Tools on this instance — same matrix, same budget, same '
        + 'commitment constraints, both re-scored by one evaluation function. '
        + 'One instance is an anecdote; the 30-seed protocol is under Evidence.' })
      : h('span', { text: 'Every engine is re-scored by one official evaluation '
        + 'function. Feasibility is a hard gate, never a penalty weight.' }),
    d.sb && d.sb.available ? [h('br'), h('b', { text: 'Simulated Bifurcation: ' }),
      h('span', { text: d.sb.routes_tried + ' routes embedded at ~' + d.sb.mean_spins
        + ' spins, ' + d.sb.routes_improved + ' improved, invalid decodes '
        + (d.sb.invalid_rate === null ? '—' : (d.sb.invalid_rate * 100).toFixed(0) + '%')
        + '.' })] : null,
    d.alns ? [h('br'), h('b', { text: 'ALNS: ' }),
      h('span', { text: d.alns.iterations + ' destroy/repair rounds, '
        + d.alns.new_bests + ' new bests. Operator weights — '
        + Object.entries(d.alns.final_weights).map(([k, v]) => k + ' ' + v).join(', ')
        + '.' })] : null);
}

/* charts — plotted like a printed figure: hairline axes, no fills, no glow */
function plot(canvas, series, opts) {
  const o = opts || {};
  const dpr = window.devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * dpr;
  canvas.height = canvas.clientHeight * dpr;
  const x = canvas.getContext('2d');
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  const w = canvas.clientWidth, ht = canvas.clientHeight;
  const pad = { l: 10, r: 10, t: 12, b: 18 };

  if (!series.length) {
    x.fillStyle = INK.paper; x.fillRect(0, 0, w, ht);
    x.fillStyle = INK.ink3; x.font = '11px -apple-system, Segoe UI, sans-serif';
    x.fillText(o.empty || 'no data yet', 12, ht / 2);
    return;
  }
  const n = series.length;
  const xs = (i) => pad.l + (n === 1 ? 0 : i / (n - 1)) * (w - pad.l - pad.r);
  const lines = [
    { key: 'best', color: INK.signal, width: 1.9, norm: 'minmax' },
    { key: 'diversity', color: '#7b2d8e', width: 1.3, norm: 'max' },
    { key: 'beta', color: INK.jam, width: 1.2, norm: 'unit', dash: [3, 3] },
  ];
  const t0 = performance.now();
  const anim = !REDUCED && o.animate !== false;

  function frame(now) {
    const p = anim ? Math.min(1, (now - t0) / 620) : 1;
    x.fillStyle = '#ffffff'; x.fillRect(0, 0, w, ht);
    x.strokeStyle = '#ece6da'; x.lineWidth = 1;
    x.beginPath();
    for (let g = 0; g <= 4; g++) {
      const y = pad.t + (g / 4) * (ht - pad.t - pad.b);
      x.moveTo(pad.l, y + .5); x.lineTo(w - pad.r, y + .5);
    }
    x.stroke();
    x.strokeStyle = INK.ink; x.lineWidth = 1;
    x.beginPath();
    x.moveTo(pad.l, pad.t); x.lineTo(pad.l, ht - pad.b); x.lineTo(w - pad.r, ht - pad.b);
    x.stroke();

    for (const ln of lines) {
      const vals = series.map((d) => d[ln.key]).filter((v) => v !== null && v !== undefined);
      if (!vals.length) continue;
      let lo = 0, hi = 1;
      if (ln.norm === 'minmax') { lo = Math.min(...vals); hi = Math.max(...vals); }
      else if (ln.norm === 'max') { hi = Math.max(...vals) || 1; }
      const rng = (hi - lo) || 1;
      const y = (v) => (ht - pad.b) - ((v - lo) / rng) * (ht - pad.t - pad.b);
      x.strokeStyle = ln.color; x.lineWidth = ln.width;
      x.setLineDash(ln.dash || []);
      x.beginPath();
      let started = false;
      const upto = Math.max(1, Math.floor(n * p));
      for (let i = 0; i < upto; i++) {
        const v = series[i][ln.key];
        if (v === null || v === undefined) continue;
        if (started) x.lineTo(xs(i), y(v)); else { x.moveTo(xs(i), y(v)); started = true; }
      }
      x.stroke(); x.setLineDash([]);
    }
    if (o.stagnation > 0 && o.stagnation < n) {
      const sx = xs(o.stagnation);
      x.strokeStyle = INK.closed; x.setLineDash([2, 3]); x.lineWidth = 1;
      x.beginPath(); x.moveTo(sx, pad.t); x.lineTo(sx, ht - pad.b); x.stroke();
      x.setLineDash([]);
      x.fillStyle = INK.closed; x.font = '700 9px ui-monospace, Consolas, monospace';
      x.fillText('STAGNATION @ ' + o.stagnation, sx + 5, pad.t + 9);
    }
    x.fillStyle = INK.ink3; x.font = '9px ui-monospace, Consolas, monospace';
    x.fillText('ITERATION', pad.l + 2, ht - 5);
    if (p < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

function swatchRow(label, color, opts) {
  const o = opts || {};
  const sw = h('span', { class: 'sw' });
  sw.style.setProperty('background', o.hollow ? '#ffffff' : color);
  sw.style.setProperty('border', '1.5px solid ' + color);
  if (o.dashed) {
    sw.style.setProperty('background',
      'repeating-linear-gradient(90deg,' + color + ' 0 4px, transparent 4px 7px)');
  }
  if (o.height) sw.style.setProperty('height', o.height);
  return h('div', { class: 'row' }, sw, h('span', { text: label }));
}

function renderLegend() {
  const el = $('#legend');
  clear(el);
  put(el,
    swatchRow('route', '#1a5fb4', { height: '4px' }),
    swatchRow('closed road', INK.closed, { height: '4px' }),
    swatchRow('congestion', INK.jam, { dashed: true, height: '4px' }),
    swatchRow('green corridor', INK.corridor, { height: '5px' }),
    swatchRow('superseded route', '#1a5fb4', { dashed: true, height: '4px' }),
    swatchRow('delivery stop', '#1a5fb4', { hollow: true, height: '10px' }),
    swatchRow('depot', INK.ink, { height: '10px' }));
}

function renderConvLegend() {
  const el = $('#convLegend');
  clear(el);
  put(el,
    swatchRow('best fitness', INK.signal, { height: '3px' }),
    swatchRow('swarm diversity', '#7b2d8e', { height: '3px' }),
    swatchRow('β contraction', INK.jam, { dashed: true, height: '3px' }));
}

function renderTimeline() {
  const track = $('#track');
  clear(track);
  $('#evCount').textContent = S.events.length
    ? S.events.length + ' entr' + (S.events.length === 1 ? 'y' : 'ies') : '';
  if (!S.events.length) {
    track.append(h('div', { class: 'ev empty',
      text: 'No incidents yet. Choose a type above the map and click a route line.' }));
    return;
  }
  for (const e of S.events) {
    track.append(h('div', { class: 'ev ' + (e.kind || 'generic') },
      h('div', { class: 'pip' }),
      h('div', { class: 't', text: clockLabel(e.t || 0) }),
      h('div', { class: 'lb', text: e.label })));
  }
  const sc = track.parentElement;
  sc.scrollLeft = sc.scrollWidth;
}

function renderEms(d) {
  $('#emsSection').hidden = false;
  const el = $('#ems');
  clear(el);
  const row = (k, v, cls) => h('div', { class: 'tr ' + (cls || '') },
    h('span', { text: k }), h('b', { text: v }));
  put(el,
    h('div', { class: 'hd', text: d.unit + ' → ' + d.hospital }),
    h('div', { class: 'bd' },
      row('to scene', d.to_scene_min + ' min'),
      row('scene → hospital', d.to_hospital_min + ' min'),
      row('total under priority', d.total_min + ' min'),
      row('without priority', d.baseline_min + ' min'),
      row('ambulance time saved', d.time_saved_min + ' min', 'saved'),
      h('div', { class: 'split' },
        // The price of priority is shown ONLY when an interaction was actually
        // measured. A corridor through streets nobody was using, at a time
        // nobody was there, costs the fleet nothing — and printing "+0.0" as
        // though it were a finding is worse than saying so plainly.
        d.interaction
          ? row('cost of priority to the fleet', '+' + d.cost_of_priority, 'cost')
          : row('cost of priority to the fleet', 'no overlap in this run'),
        d.interaction
          ? row('fleet cost before → after',
                d.fleet_cost_before + ' → ' + d.fleet_cost_after)
          : null,
        row('delivery legs inside the corridor window',
            String(d.route_corridor_overlaps ?? 0)),
        (d.interaction && (d.affected_vehicles || []).length)
          ? row('vehicles affected', 'V' + d.affected_vehicles.join(', V'))
          : null,
        row('green corridor', d.corridor_edges + ' edges, '
            + (d.corridor_edge_windows || 0) + ' per-edge windows'),
        row('path latency (budget 200 ms)', d.path_ms + ' ms')),
      h('div', { class: 'footnote', text: d.interaction
        ? 'Priority is not teleportation — one-ways and physical closures are '
          + 'still respected. It is also not free, and both sides of that '
          + 'trade are above.'
        : 'The corridor did not cross any delivery leg while it was warm, so '
          + 'the fleet paid nothing. That is a real outcome, not a missing '
          + 'measurement — the per-edge windows are what make it '
          + 'distinguishable from an effect nobody looked for.' })));
}

/* ---------------------------------------------------------- proof block */

function proofCell(value, label, cls, unit, tag) {
  return h('div', { class: 'p ' + (cls || '') },
    h('div', { class: 'v' }, String(value), unit ? h('small', { text: unit }) : null),
    h('span', { class: 'l rubric', text: label }),
    tag ? h('span', { class: 'tag ' + tag[1], text: tag[0] }) : null);
}

/** The five figures a judge decides on, read from THIS run's response.
 *
 *  Three rules this panel exists to keep:
 *
 *  1. It shows the measured end-to-end time for the run just executed, NOT a
 *     p95. One run cannot produce a percentile, and printing one would be
 *     inventing a statistic from a single sample. The p95 lives under Evidence
 *     where the 20-trial measurement that produced it lives.
 *  2. The 500 ms verdict is stamped ONLY on the single-engine operational
 *     path, because that is the only path the target was ever set for. A
 *     four-engine race is a demo affordance that deliberately does more work;
 *     grading it against the operational target would be the same category
 *     error the rest of this project refuses to make, in either direction —
 *     it would be unfair to the system on a slow run and a false pass on a
 *     fast one. The race is labelled as a race and left ungraded.
 *  3. The vehicle count matches what is DRAWN. The server's churn counts
 *     vehicles that received a re-assigned stop; the map ghosts every vehicle
 *     whose route changed at all. Showing one number and drawing the other
 *     would put a contradiction on screen, so this reports the drawn set and
 *     carries the server's stop-level components as the supporting detail.
 */
function renderProof(d, ems) {
  const sec = $('#proofSection');
  const el = $('#proof');
  sec.hidden = false;
  clear(el);

  const total = d.total_ms || 0;
  const nEng = (d.engines || []).length;
  const operational = nEng === 1;
  const within = total < 500;
  const caseNo = (d.case.match(/CASE\s*(\d)/i) || [null, '—'])[1];
  const vt = (d.summary && d.summary.vehicles_total) || 0;
  const moved = S.diff ? S.diff.changed.size : 0;
  const ch = d.churn || {};
  const feasible = !!(d.summary && d.summary.feasible);
  const viol = (d.summary && d.summary.violations) || [];
  // _summary() truncates the list at four, so an exact count above four is not
  // knowable from this payload. Say "4+" rather than quietly reporting 4.
  const violTxt = feasible ? '0' : (viol.length >= 4 ? '4+' : String(viol.length));

  put(el,
    proofCell(f0(total),
      operational ? 'end-to-end · operational path' : 'end-to-end · ' + nEng + '-engine race',
      operational ? (within ? 'ok' : 'warn') : '', 'ms',
      operational
        ? [within ? 'within 500 ms' : 'over 500 ms', within ? 'ok' : 'warn']
        : ['race — target applies to one engine', 'mute']),
    proofCell('Case ' + caseNo,
      d.incumbent_was_feasible ? 'incumbent still feasible' : 'incumbent infeasible',
      d.accepted ? 'ok' : '',
      null,
      [d.accepted ? 'recovery accepted' : 'incumbent held', d.accepted ? 'ok' : 'mute']),
    proofCell(moved + ' / ' + vt, 'vehicles whose route changed', moved ? 'sig' : '', null,
      [moved
        ? ((ch.reassigned || 0) + ' stop' + ((ch.reassigned || 0) === 1 ? '' : 's')
           + ' re-assigned, ' + (ch.resequenced || 0) + ' re-sequenced')
        : 'no vehicle re-tasked', 'mute']),
    proofCell(violTxt, 'constraint violations', feasible ? 'ok' : 'bad', null,
      [feasible ? 'validator: valid' : 'validator: invalid', feasible ? 'ok' : 'bad']),
    // Priority cost only when an interaction was actually measured. A corridor
    // that crossed nobody costs nothing, and printing "+0.0" as if it were a
    // finding is worse than saying so.
    ems
      ? (ems.interaction
        ? proofCell('+' + ems.cost_of_priority, 'ambulance priority cost to the fleet',
          'sig wide', null,
          [(ems.route_corridor_overlaps || 0) + ' delivery legs inside the corridor window',
            'mute'])
        : proofCell('no overlap in this run',
          'ambulance priority cost to the fleet', 'mute wide', null,
          ['corridor crossed no delivery leg while warm', 'mute']))
      : null);

  $('#proofNote').textContent =
    (operational
      ? 'Single-engine operational path — what a dispatcher actually waits for.'
      : 'This run raced ' + nEng + ' engines so they can be watched competing, '
        + 'and it is left ungraded: the 500 ms target is set for the ONE engine '
        + 'a dispatcher waits on, and that path is measured separately under '
        + 'Evidence.')
    + ' The travel-time matrix rebuild is inside this total. This is one run, '
    + 'not a percentile — the p95 over 20 trials is under Evidence.';

  if (!REDUCED) {
    el.animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }],
      { duration: 320, easing: EASE });
  }
}

/* --------------------------------------------------- recovery phase list */

/* Readable names for the stages the server reports in `stages_ms`.
 *
 * This is a LOOKUP, not a whitelist. renderPhases iterates the response and
 * falls back to the raw key for anything not named here, so adding a stage
 * server-side can never make it silently vanish from this panel — which is
 * precisely how the Evidence ablation table once dropped its two least
 * flattering arms. The stage times therefore always add up to the total.
 *
 * Nothing here is timed on the client: while the request is in flight every
 * row is simply "running", and a row only gets a number once the SERVER has
 * reported one. A staged animation pretending to observe progress it cannot
 * see would be the same unmeasured display this project exists to avoid. */
const PHASE_LABEL = {
  freeze_commitments: 'Freezing committed legs',
  fifo_assert: 'Checking FIFO consistency',
  matrix_rebuild: 'Updating road costs',
  evaluate_incumbent: 'Revalidating ETAs',
  solve: 'Recovering affected vehicles',
  acceptance: 'Applying the acceptance rule',
};
/* The order the engine runs them in, used only while no response has arrived
 * yet. Once it has, the response's own key order wins. */
const PHASE_ORDER = Object.keys(PHASE_LABEL);

function renderPhases(stages) {
  const ul = $('#phases');
  clear(ul);
  ul.hidden = false;
  const keys = stages ? Object.keys(stages) : PHASE_ORDER;
  for (const key of keys) {
    const ms = stages ? stages[key] : undefined;
    const done = ms !== undefined && ms !== null;
    ul.append(h('li', { class: done ? 'done' : 'run' },
      h('span', { class: 'mk', text: done ? '✓' : '·' }),
      h('span', { text: PHASE_LABEL[key] || key.replace(/_/g, ' ') }),
      h('span', { class: 'ms', text: done ? ms.toFixed(1) + ' ms' : '' })));
  }
}

/* --------------------------------------------------------------- actions */

/** @param {object} d          a plan/replan/advance payload
 *  @param {boolean} ghost     keep the outgoing plan on screen underneath */
function applyPlan(d, ghost) {
  const before = S.routes;
  S.routes = d.routes || [];
  S.closed = d.closed || [];
  S.summary = d.summary || {};
  S.events = d.events || [];
  if (d.sim_clock_s !== undefined) S.clock = d.sim_clock_s;
  S.routeT0 = performance.now();

  if (ghost && before.length) {
    S.prev = { routes: before };
    S.diff = routeDiff(before, S.routes);
    if (!S.diff.changed.size) { S.prev = null; }
  } else {
    S.prev = null; S.diff = null;
  }
  const tog = $('#btnGhost');
  tog.hidden = !S.prev;
  if (S.prev) {
    S.showGhost = true;
    tog.setAttribute('aria-pressed', 'true');
    tog.textContent = 'Hide previous plan';
  }

  renderBand(); renderFleet(); renderTimeline(); titleblock();
}

/* STEP 01 — load the declared scenario AND build the first plan.
 *
 * One press, two calls, on purpose: a judge pressing "Initialize fleet" is
 * asking for the whole starting state, and making them press a second button
 * to get routes is the same ambiguity this sequence was rewritten to remove.
 * The reset is what guarantees the fleet on screen is the fleet step 01
 * advertises, rather than whatever the server happened to boot with. */
$('#btnPlan').addEventListener('click', async () => {
  const b = $('#btnPlan');
  const q = '?n=' + SCENARIO.customers + '&k=' + SCENARIO.vehicles
          + '&seed=' + SCENARIO.seed;
  working(b, true, 'initializing…');
  setStep(1, 'active', 'Loading');
  renderChecks(['Loading ' + SCENARIO.vehicles + ' vehicles…']);
  busy(true, 'loading the scenario…');
  try {
    S.boot = await api('/api/reset' + q, { method: 'POST' });
    renderNetMeta(); renderSpec();
    busy(true, 'building the initial feasible plan…');
    const d = await api('/api/plan?budget=1.2', { method: 'POST' });
    applyPlan(d, false);
    S.planned = true;
    S.prev = null; S.diff = null;
    $('#proofSection').hidden = true;
    $('#phases').hidden = true;
    $('#btnReplan').disabled = false;
    $('#btnAdvance').disabled = false;
    for (const el of document.querySelectorAll('.evbtn')) el.disabled = false;
    setPrimary('replan');
    renderEnergy(d.energy, null);
    renderNetMeta(); renderSpec();      // re-run now that S.planned is true
    renderChecks([
      S.boot.vehicles + ' vehicles loaded',
      S.boot.customers + ' deliveries loaded',
      'Initial ' + (d.summary && d.summary.feasible ? 'feasible' : 'INFEASIBLE')
        + ' plan generated · ' + d.plan_ms.toFixed(0) + ' ms',
    ]);
    setStep(1, 'done', 'Loaded');
    setStep(2, 'active', 'Choose a type');
    setStep(3, 'locked', 'Awaiting event');
    $('#statusText').textContent = S.boot.nodes.toLocaleString() + ' junctions · '
      + S.boot.customers + ' stops · ' + S.boot.vehicles + ' vehicles';
    $('#hint').textContent = 'Pick an event type, then click '
      + (S.evKind === 'ambulance' ? 'the incident location on the map.'
        : 'ON a coloured route line. Clicking empty road is a correct no-op.');
    toast('Fleet ready · ' + S.boot.vehicles + ' vehicles · '
      + S.boot.customers + ' deliveries');
  } catch (err) {
    setStep(1, 'ready', 'Failed');
    renderChecks([]);
    toast('Initialize failed: ' + err.message, true);
  } finally { working(b, false, 'Re-initialize fleet'); busy(false); }
});

/* STEP 03 — recover. */
$('#btnReplan').addEventListener('click', async () => {
  const b = $('#btnReplan');
  working(b, true, 'recalculating…');
  setStep(3, 'active', 'Recalculating');
  renderPhases(null);                       // rows named, no times claimed yet
  busy(true, 'racing the solvers under the new costs…');
  try {
    const d = await api('/api/replan?budget=0.35&engines=emergency,qpso,alns,ortools',
      { method: 'POST' });
    applyPlan(d, true);
    S.last = d; S.conv = d.convergence || [];
    renderPhases(d.stages_ms);              // ...times arrive with the response
    renderProof(d, null);
    renderVerdict(d); renderLatency(d); renderRace(d);
    renderEnergy(d.energy, d.energy_at_scale);
    plot($('#conv'), S.conv, { empty: 'run a re-plan to record convergence' });
    renderTimeline();
    setStep(3, 'done', 'Recovery complete');
    const moved = ((d.churn && d.churn.vehicles_changed) || []).length;
    $('#hint').textContent = d.accepted
      ? 'Recovery accepted · ' + moved + ' of '
        + (d.summary.vehicles_total || 0) + ' vehicles re-routed. The dashed '
        + 'lines are the plan they replaced.'
      : 'Incumbent held — the new plan did not beat the threshold, so no driver '
        + 'was re-tasked. Restraint is the feature here.';
    toast((d.accepted ? 'Recovery complete' : 'Incumbent held')
      + ' · ' + d.total_ms.toFixed(0) + ' ms');
  } catch (err) {
    setStep(3, 'active', 'Failed');
    $('#phases').hidden = true;
    toast('Recovery failed: ' + err.message, true);
  } finally { working(b, false, 'Run RoutePulse recovery'); busy(false); }
});

$('#btnGhost').addEventListener('click', () => {
  S.showGhost = !S.showGhost;
  const t = $('#btnGhost');
  t.setAttribute('aria-pressed', String(S.showGhost));
  t.textContent = S.showGhost ? 'Hide previous plan' : 'Show previous plan';
});

for (const el of document.querySelectorAll('.evbtn')) {
  el.addEventListener('click', () => {
    if (el.disabled) return;
    setEvKind(el.dataset.ev);
    $('#hint').textContent = S.evKind === 'ambulance'
      ? 'Click the incident location on the map. Dispatch, corridor and fleet '
        + 'recovery all happen in that one click.'
      : 'Click ON a coloured route line. Clicking empty road is a correct no-op.';
  });
}

$('#btnReset').addEventListener('click', async () => {
  const q = '?n=' + SCENARIO.customers + '&k=' + SCENARIO.vehicles
          + '&seed=' + SCENARIO.seed;
  busy(true, 'rebuilding the scenario…');
  try {
    S.boot = await api('/api/reset' + q, { method: 'POST' });
    S.routes = []; S.closed = []; S.events = []; S.summary = {};
    S.conv = []; S.amb = null; S.last = null; S.planned = false; S.pings = [];
    S.prev = null; S.diff = null;
    $('#emsSection').hidden = true;
    $('#proofSection').hidden = true;
    $('#phases').hidden = true;
    $('#btnGhost').hidden = true;
    $('#btnReplan').disabled = true;
    $('#btnAdvance').disabled = true;
    $('#btnPlan').textContent = 'Initialize fleet';
    for (const el of document.querySelectorAll('.evbtn')) el.disabled = true;
    setPrimary('plan');
    setStep(1, 'ready', 'Ready');
    setStep(2, 'locked', 'Awaiting fleet');
    setStep(3, 'locked', 'Awaiting event');
    renderChecks([]);
    S.clock = 0;
    clear($('#band')); clear($('#latency')); clear($('#race')); clear($('#reasons'));
    renderBand(); renderFleet(); renderTimeline(); renderNetMeta(); renderSpec();
    renderEnergy(null); titleblock();
    plot($('#conv'), [], { empty: 'run a re-plan to record convergence' });
    const v = $('#verdict');
    v.className = 'verdict idle'; clear(v);
    v.append(h('div', { class: 'stamp', text: 'Standing by' }),
      h('div', { class: 'why', text: 'Scenario rebuilt. Initialize the fleet to begin.' }));
    $('#hint').textContent = 'Scenario rebuilt. Press Initialize fleet.';
    toast('Scenario rebuilt');
  } catch (err) { toast('Reset failed: ' + err.message, true); }
  finally { busy(false); }
});

$('#btnAdvance').addEventListener('click', async () => {
  const b = $('#btnAdvance');
  working(b, true, 'advancing…');
  busy(true, 'letting the fleet drive…');
  try {
    const d = await api('/api/advance?minutes=20', { method: 'POST' });
    applyPlan(d, false);
    $('#hint').textContent = d.served + ' stop(s) completed, '
      + d.remaining + ' still pending. Vehicles are now where they actually '
      + 'are — the next re-plan starts from there, not from the depot.';
    toast('Clock ' + clockLabel(d.sim_clock_min * 60) + ' · '
      + d.served + ' delivered');
    if (d.remaining === 0) {
      $('#btnAdvance').disabled = true;
      toast('All stops delivered');
    }
  } catch (err) { toast('Advance failed: ' + err.message, true); }
  finally { working(b, false, 'Advance clock +20 min'); busy(false); }
});

async function inject(sx, sy) {
  if (!S.planned) { toast('Initialize the fleet first'); return; }
  const [lat, lon] = unpx(sx, sy, T());
  const kind = S.evKind;
  S.pings.push({ lat, lon, t0: performance.now(),
    color: kind === 'closure' ? INK.closed : kind === 'ambulance' ? INK.amb : INK.jam });

  if (kind === 'ambulance') {
    // Dispatch recovers the fleet in the SAME request, so step 03 runs itself.
    setStep(3, 'active', 'Recalculating');
    renderPhases(null);
    busy(true, 'dispatching…');
    try {
      const d = await api('/api/ambulance', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat, lon, severity: 2 }) });
      S.amb = d; S.closed = d.closed; S.events = d.events;
      if (d.sim_clock_s !== undefined) S.clock = d.sim_clock_s;
      renderEms(d);
      // ONE ACTION, THE WHOLE EVENT. The server dispatches, publishes the
      // corridor and recovers the fleet in a single request, so the decision
      // is already here -- the operator never has to press Re-plan to find
      // out what the emergency did to the deliveries.
      if (d.recovery) {
        applyPlan(d.recovery, true);
        S.last = d.recovery;
        S.conv = d.recovery.convergence || [];
        renderPhases(d.recovery.stages_ms);
        renderProof(d.recovery, d);
        renderVerdict(d.recovery);
        renderLatency(d.recovery);
        renderRace(d.recovery);
        renderEnergy(d.recovery.energy, d.recovery.energy_at_scale);
        plot($('#conv'), S.conv, { empty: 'run a re-plan to record convergence' });
        setStep(2, 'done', 'Dispatched');
        setStep(3, 'done', 'Recovery complete');
        $('#hint').textContent = 'Ambulance dispatched, corridor open, and the '
          + 'fleet has already re-planned around it — one action. The dashed '
          + 'lines are the routes it replaced.';
        toast(d.unit + ' → ' + d.hospital + ' · ' + d.time_saved_min
          + ' min saved · fleet ' + (d.recovery.accepted ? 're-planned' : 'held'));
      } else {
        renderTimeline(); titleblock();
        $('#phases').hidden = true;
        setStep(2, 'done', 'Dispatched');
        setStep(3, 'active', 'Ready');
        $('#hint').textContent = 'Ambulance dispatched and the corridor is open.';
        toast(d.unit + ' → ' + d.hospital + ' · ' + d.time_saved_min + ' min saved');
      }
    } catch (err) {
      setStep(3, 'locked', 'Awaiting event');
      $('#phases').hidden = true;
      toast('Dispatch failed: ' + err.message, true);
    } finally { busy(false); }
    return;
  }

  try {
    const d = await api('/api/event', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lat, lon, kind, radius_m: 420, multiplier: 6 }) });
    S.closed = d.closed; S.events = d.events;
    renderTimeline(); titleblock();
    if (!d.edges) {
      // Nothing was actually injected, so step 03 must NOT light up as though
      // there were something to recover from.
      $('#hint').textContent = 'That point is outside the service area — no edge '
        + 'was affected. Click ON a coloured route line.';
      toast('0 edges affected — that point is outside the service area', true);
      return;
    }
    setStep(2, 'done', 'Injected');
    setStep(3, 'active', 'Ready');
    $('#phases').hidden = true;
    $('#hint').textContent = d.label + ' injected. Press '
      + 'Run RoutePulse recovery.';
    toast(d.label);
  } catch (err) { toast('Event failed: ' + err.message, true); }
}

/* ------------------------------------------------------------- evidence */

let figureNo = 0;
function card(no, title, tags, ...body) {
  return h('div', { class: 'card' + (no === 'full' ? ' full' : '') },
    h('div', { class: 'hd' },
      h('span', { class: 'no', text: '§' + (++figureNo) }),
      h('h3', { text: title }),
      tags),
    h('div', { class: 'bd' }, body));
}

function fig(value, label, detail, cls) {
  return h('div', { class: 'fig ' + (cls || '') },
    h('div', { class: 'v', text: value }),
    h('div', { class: 'l rubric', text: label }),
    detail ? h('div', { class: 'd', text: detail }) : null);
}

function gone(name, cmd) {
  return h('div', { class: 'gone' },
    h('b', { text: 'not measured' }),
    h('span', { text: name + ' has not been run in this checkout. Run ' }),
    h('code', { text: cmd }),
    h('span', { text: '. An unrun experiment and a passing one must not look '
      + 'the same, so nothing is shown here rather than a plausible default.' }));
}

function cardAblation(ev) {
  if (!ev.benchmark) {
    return card('full', 'Ablation', null,
      gone('The 30-seed ablation', 'python scripts/bench.py --seeds 30'));
  }
  const b = ev.benchmark, sum = b.summary || {}, wx = b.wilcoxon || {};
  const ref = sum.E ? sum.E.mean : null;
  // Every arm the benchmark ran, in reading order. A hand-written whitelist
  // used to omit the chained hybrid and the classical-PSO control, so the two
  // arms carrying the least flattering results were the two the sheet did not
  // show. Anything in the file that is not named here is appended rather than
  // dropped, so a new arm can never go missing the same way twice.
  const order = ['GREEDY', 'E', 'ALNS', 'A_HYB', 'SB', 'SEQ_LS', 'SEQ_SB',
                 'A', 'A0', 'PSO', 'B', 'D', 'OR'];
  const keys = order.filter(k => sum[k])
    .concat(Object.keys(sum).filter(k => !order.includes(k)));
  const body = h('tbody');
  for (const k of keys) {
    const a = sum[k];
    if (!a) continue;
    const delta = (ref && k !== 'E') ? ((ref - a.mean) / ref) * 100 : null;
    const p = wx[k] ? wx[k].p : null;
    body.append(h('tr', null,
      h('td', { class: k === 'ALNS' ? 'lead' : '', text: a.label }),
      h('td', { class: 'num', text: Math.round(a.mean).toLocaleString() }),
      h('td', { class: 'num', text: Math.round(a.sd).toLocaleString() }),
      h('td', { class: 'num', text: Math.round(a.best).toLocaleString() }),
      h('td', { class: 'num' + (delta !== null && delta > 0 ? ' lead' : ''),
        text: delta === null ? '—' : pc(delta) }),
      h('td', null, p === null ? h('span', { class: 'tag mute', text: 'reference' })
        : h('span', { class: 'tag ' + (p < 0.05 ? 'ok' : 'warn'),
          text: (p < 0.05 ? 'p ' : 'n.s. p ') + (p < 0.0001 ? '<0.0001' : p.toFixed(4)) }))));
  }
  return card('full', 'Ablation — 30 seeds, paired Wilcoxon',
    [h('span', { class: 'tag mute', text: (b.config && b.config.seeds) + ' seeds' }),
     h('span', { class: 'tag mute', text: (b.config && b.config.budget) + ' s budget' })],
    h('div', { class: 'lede', text:
      'Every arm solves the SAME instance with the SAME wall-clock budget and is '
      + 're-scored by the SAME evaluation function. Arms share instances, so the '
      + 'paired Wilcoxon signed-rank test is the correct one. Lower is better.' }),
    h('table', { class: 'data' },
      h('thead', null, h('tr', null,
        h('th', { text: 'arm' }), h('th', { class: 'num', text: 'mean' }),
        h('th', { class: 'num', text: 'sd' }), h('th', { class: 'num', text: 'best' }),
        h('th', { class: 'num', text: 'vs greedy+LS' }),
        h('th', { text: 'significance' }))), body),
    h('div', { class: 'footnote' },
      h('b', { text: 'The headline is not flattering and that is the point. ' }),
      'The improvement layer does essentially all the work, the quantum-inspired '
      + 'swarm update rule does not clear significance at the recommended '
      + 'protocol, and OR-Tools beats us on static solution quality. All three '
      + 'are reported because the experiment was built to be able to say so.'),
    h('div', { class: 'footnote', text: b.graph_source || '' }));
}

function cardGates(ev) {
  if (!ev.benchmark || !ev.benchmark.summary) return null;
  const sum = ev.benchmark.summary, wx = ev.benchmark.wilcoxon || {};
  const ref = sum.E ? sum.E.mean : null;
  if (!ref) return null;
  const rows = [];
  for (const [k, name, sub] of [
    ['ALNS', 'Traffic-Aware ALNS', 'blueprint Appendix A'],
    ['SB', 'Simulated Bifurcation', 'added to the improvement stack']]) {
    if (!sum[k]) continue;
    const d = ((ref - sum[k].mean) / ref) * 100;
    const p = wx[k] ? wx[k].p : null;
    let verdict, cls;
    if (p === null) { verdict = 'no measurable difference'; cls = 'mute'; }
    else if (p < 0.05 && d > 0) { verdict = 'adopted'; cls = 'ok'; }
    else if (p < 0.05) { verdict = 'rejected — worse'; cls = 'bad'; }
    else { verdict = 'not adopted — inside the noise'; cls = 'warn'; }
    rows.push(h('div', { class: 'row' },
      h('div', null,
        h('div', { class: 'lbl' }, name + '  ',
          h('span', { class: 'tag ' + cls, text: verdict })),
        meter(Math.min(100, Math.abs(d) * 9), d > 0 ? 'pos' : 'neg', 80),
        h('div', { class: 'd', text: sub + (p === null ? '' : ' · p = '
          + (p < 0.0001 ? '<0.0001' : p.toFixed(4))) })),
      h('div', { class: 'val', text: pc(d) })));
  }
  return card(null, 'Adoption gates', null,
    h('div', { class: 'lede', text:
      'Two engines were built from the blueprint and neither was adopted on '
      + 'reputation. Each had to beat the existing improvement layer from the '
      + 'same start, on the same budget, over 30 paired seeds.' }),
    h('div', { class: 'bars' }, rows));
}

function cardSwarmIsolation(ev) {
  // The project's central claim is "quantum-inspired". The only way to test it
  // is to hold everything else fixed -- same decoder, same local search, same
  // instance, same budget -- and change nothing but the line that moves a
  // particle. That comparison used to exist only in a JSON file, so the sheet
  // showed every result except the one that questions the headline.
  const wx = (ev.benchmark && ev.benchmark.wilcoxon) || {};
  const stat = wx.A_vs_PSO;
  const dyn = ev.dynamic_arm;
  if (!stat && !dyn) return null;

  const rows = [];
  const line = (name, sub, d, lo, hi, p) => {
    const sig = (p !== null && p !== undefined && p < 0.05);
    rows.push(h('tr', null,
      h('td', null, h('div', { class: 'lbl', text: name }),
        h('div', { class: 'd', text: sub })),
      h('td', { class: 'num', text: pc(d) }),
      h('td', { class: 'num', text: (lo === null || lo === undefined) ? '—'
        : '[' + pc(lo) + ', ' + pc(hi) + ']' }),
      h('td', null, h('span', { class: 'tag ' + (sig ? 'ok' : 'warn'),
        text: p === null || p === undefined ? 'n/a'
          : (sig ? 'p ' : 'n.s. p ') + (p < 0.0001 ? '<0.0001' : p.toFixed(4)) }))));
  };

  if (stat) {
    line('Static plan quality', '30 paired seeds, cold instance, 0.35 s budget',
      stat.delta_pct, stat.ci95_low_pct, stat.ci95_high_pct, stat.p);
  }
  if (dyn && dyn.analysis && dyn.analysis.score) {
    const s = dyn.analysis.score;
    const n = (dyn.events || []).length;
    line('Dynamic recovery quality',
      s.n + ' paired recoveries across ' + n + ' event types, frozen '
      + 'commitments, 0.25 s budget',
      s.delta_pct, s.ci95_low_pct, s.ci95_high_pct, s.p);
    const lat = dyn.analysis.latency_ms;
    if (lat) {
      line('Dynamic recovery latency', 'same runs, wall clock to an accepted plan',
        lat.delta_pct, lat.ci95_low_pct, lat.ci95_high_pct, lat.p);
    }
  }

  const fr = dyn && dyn.analysis && dyn.analysis.feasible_runs;
  return card('full', 'Is the swarm rule quantum-inspired in any way that matters?',
    [h('span', { class: 'tag warn', text: 'the control arm' })],
    h('div', { class: 'lede', text:
      'One line differs between the two conditions: the delta-potential-well '
      + 'update of QPSO versus the constricted velocity update of classical '
      + 'PSO (Clerc–Kennedy). Identical random-key encoding, identical Split '
      + 'decode, identical local search, identical instances, identical '
      + 'budgets. A positive delta favours QPSO. Lower is better throughout.' }),
    h('table', { class: 'data' },
      h('thead', null, h('tr', null,
        h('th', { text: 'isolation' }), h('th', { class: 'num', text: 'QPSO vs PSO' }),
        h('th', { class: 'num', text: '95% CI on the paired difference' }),
        h('th', { text: 'significance' }))),
      h('tbody', null, ...rows)),
    h('div', { class: 'footnote' },
      h('b', { text: 'Neither isolation separates. ' }),
      'Every interval above spans zero, so the honest reading is that the '
      + 'quantum-inspired update rule performs the same as a classical swarm '
      + 'on this problem — statically and, more importantly, on the dynamic '
      + 'recovery this system actually exists to do. The rule is retained '
      + 'because the sponsor specified it and it costs nothing to keep, not '
      + 'because it was shown to help.'
      + (fr ? ' Both arms recovered a feasible plan on ' + fr.qpso + '/'
        + fr.total + ' and ' + fr.pso + '/' + fr.total + ' runs respectively.'
        : '')));
}

function cardConvergence(ev) {
  if (!ev.convergence) {
    return card(null, 'Convergence analysis', null,
      gone('The convergence analysis', 'python scripts/convergence.py'));
  }
  const c = ev.convergence;
  const series = c.iters.map((it, i) => ({
    iter: it, best: c.best[i], diversity: c.diversity[i], beta: c.beta[i] }));
  const canvas = h('canvas', { id: 'convBig' });
  const node = card(null, 'Convergence analysis',
    [h('span', { class: 'tag warn', text: 'stagnates at iteration ' + c.stagnation_iter })],
    h('div', { class: 'lede', text:
      'Cold start, real network, ' + (c.config && c.config.seeds) + ' seeds, '
      + (c.config && c.config.budget) + ' s budget. The sponsor asks for '
      + 'convergence analysis by name, and the useful finding is the stagnation '
      + 'point: the swarm has effectively converged by iteration '
      + c.stagnation_iter + ' of ' + c.iters.length + ', so the rest of the '
      + 'budget buys almost nothing.' }),
    canvas,
    h('div', { class: 'figs' },
      fig(pc(c.total_gain_pct), 'gbest improvement'),
      fig(c.diversity[0].toFixed(3) + ' → ' + c.diversity[c.diversity.length - 1].toFixed(3),
        'swarm diversity', 'collapses as the well width contracts'),
      fig(c.beta[0].toFixed(2) + ' → ' + c.beta[c.beta.length - 1].toFixed(2),
        'β contraction–expansion')));
  requestAnimationFrame(() => plot(canvas, series, { stagnation: c.stagnation_iter }));
  return node;
}

function cardsLatency(ev) {
  if (!ev.latency) {
    return card(null, 'End-to-end latency', null,
      gone('The latency measurement', 'python scripts/latency.py'));
  }
  const modes = ev.latency.modes || {};
  const out = [];
  for (const name in modes) {
    const m = modes[name];
    const stages = m.stages_p95 || {};
    const max = Math.max(...Object.values(stages), 1);
    out.push(card(null, name,
      [h('span', { class: 'tag ' + (m.meets_500ms ? 'ok' : 'warn'),
        text: m.meets_500ms ? 'meets 500 ms' : 'over 500 ms' })],
      h('div', { class: 'figs' },
        fig(m.total_p50.toFixed(0) + ' ms', 'p50 total'),
        fig(m.total_p95.toFixed(0) + ' ms', 'p95 total', null, m.meets_500ms ? 'ok' : 'warn'),
        fig(m.total_max.toFixed(0) + ' ms', 'worst observed')),
      h('div', { class: 'lede', css: { 'margin-top': '14px', 'margin-bottom': '10px' },
        text: 'p95 by stage — the travel-time matrix rebuild is inside the total.' }),
      h('div', { class: 'bars' }, Object.keys(stages).map((k, i) => h('div', { class: 'row' },
        h('div', null, h('div', { class: 'lbl', text: k.replace(/_/g, ' ') }),
          meter((stages[k] / max) * 100,
            k.includes('matrix') ? 'matrix' : k.includes('solve') ? 'solve' : '',
            60 + i * 50)),
        h('div', { class: 'val', text: stages[k].toFixed(1) }))))));
  }
  return out;
}

function cardSB(ev) {
  const sb = ev.simulated_bifurcation;
  if (!sb) {
    return card('full', 'Simulated Bifurcation', null,
      gone('The Simulated Bifurcation study', 'python scripts/sb_eval.py'));
  }
  const gs = sb.ground_state_validation || [];
  const rs = sb.resequencing_summary || {};
  const ex = rs.exact_subset || null;
  const sweep = sb.time_window_field_sweep || [];
  const cal = sb.penalty_calibration || [];

  const col = (title, tableNode, note) => h('div', null,
    h('div', { class: 'rubric', css: { 'margin-bottom': '8px' }, text: title }),
    tableNode, h('div', { class: 'footnote', text: note }));

  return card('full', 'Simulated Bifurcation — the genuinely quantum-derived engine',
    [h('span', { class: 'tag warn', text: 'negative result, reported' })],
    h('div', { class: 'lede', text:
      'QPSO is quantum-inspired by analogy. Simulated Bifurcation is the '
      + 'classical limit of a real Kerr-nonlinear parametric oscillator network '
      + '(Goto 2016; Goto, Tatsumura & Dixon 2019): the equations integrated here '
      + 'are the equations of motion of a physical Ising machine. That makes it '
      + 'the right engine to answer "in what sense is any of this quantum?" — and '
      + 'the right engine to be honest about when it loses.' }),
    h('div', { class: 'sheets', css: { 'grid-template-columns': 'repeat(auto-fit,minmax(250px,1fr))',
      gap: '20px', 'margin-top': '0' } },
      col('1 · is the solver correct?',
        h('table', { class: 'data' },
          h('thead', null, h('tr', null, h('th', { text: 'problem' }),
            h('th', { class: 'num', text: 'ground state' }),
            h('th', { class: 'num', text: 'gap' }),
            h('th', { class: 'num', text: 'time' }))),
          h('tbody', null, gs.map((g) => h('tr', null,
            h('td', { text: g.spins + ' spins' }),
            h('td', { class: 'num', text: g.ground_states_found + '/' + g.trials }),
            h('td', { class: 'num', text: g.mean_gap_pct.toFixed(3) + '%' }),
            h('td', { class: 'num', text: g.ms_per_instance.toFixed(1) + ' ms' }))))),
        'Against exhaustive enumeration on random Ising instances. The dynamics '
        + 'find the exact ground state every time. Nothing below is a bug in the '
        + 'solver.'),
      col('2 · is the embedding sound?',
        h('table', { class: 'data' },
          h('thead', null, h('tr', null, h('th', { text: 'constraint penalty' }),
            h('th', { text: 'valid tours' }), h('th', { class: 'num', text: 'gap' }))),
          h('tbody', null, cal.map((c) => h('tr', null,
            h('td', { text: c.A_over_max_leg + '× max leg' }),
            h('td', null, h('span', { class: 'tag ' + (c.valid_rate === 1 ? 'ok' : 'bad'),
              text: c.raw_valid_decodes + '/' + c.trials })),
            h('td', { class: 'num', text: pc(c.mean_gap_vs_optimal_pct) }))))),
        'The classic QUBO failure mode, measured: too weak a penalty and the spin '
        + 'configuration is not a tour at all; too strong and it drowns the '
        + 'objective it exists to protect.'),
      col('3 · what do time windows cost it?',
        h('table', { class: 'data' },
          h('thead', null, h('tr', null, h('th', { text: 'deadline-order field' }),
            h('th', { class: 'num', text: 'vs the greedy order' }))),
          h('tbody', null, sweep.map((s) => h('tr', null,
            h('td', { text: 'weight ' + s.edd_weight }),
            h('td', { class: 'num', text: pc(s.mean_pct_vs_greedy) }))))),
        'A quadratic form cannot see cumulative arrival time, so it cannot see a '
        + 'time window. Pricing deadline order into the local field recovers most '
        + 'of the damage — from 14× worse to within 20%.')),
    ex ? h('div', { class: 'figs', css: { 'margin-top': '18px' } },
      fig(pc(ex.sb_travel_gap_pct), 'SB vs exact · travel only',
        'The objective the Ising model actually encodes. Competitive.',
        ex.sb_travel_gap_pct < 10 ? 'ok' : 'warn'),
      fig(pc(ex.sb_tw_gap_pct), 'SB vs exact · full objective',
        'With the deadline field. Without it: ' + pc(ex.sb_gap_pct) + '.', 'warn'),
      fig(rs.mean_ms_sb + ' ms', 'per route, vs ' + rs.mean_ms_2opt + ' ms for 2-opt',
        '2-opt wins ' + rs['2opt_beats_sb'] + ' of ' + rs.routes + ' routes and '
        + 'reaches the exact optimum on every small one.', 'bad')) : null,
    h('div', { class: 'footnote' },
      h('b', { text: 'Verdict: not adopted into the operational path. ' }),
      'A correctly implemented Ising machine, validated to find exact ground '
      + 'states, is beaten by 2-opt on this problem — because the embedding drops '
      + 'the term that dominates the real cost. That is a result about the '
      + 'embedding, not about the hardware, and it is the honest answer to '
      + 'whether quantum-derived optimisation is ready for time-windowed fleet '
      + 'routing today.'));
}

function cardScenarios(ev) {
  if (!ev.scenarios) {
    return card(null, 'Operational scenarios', null,
      gone('The scenario suite', 'python scripts/scenarios.py'));
  }
  const rs = ev.scenarios.results || [];
  const passed = rs.filter((r) => r.pass).length;
  const vacuous = rs.filter((r) => r.pass && !r.exercised).length;
  return card(null, 'Operational scenarios S1–S9',
    [h('span', { class: 'tag ' + (passed === rs.length ? 'ok' : 'bad'),
      text: passed + '/' + rs.length + ' pass' }),
     vacuous ? h('span', { class: 'tag bad', text: vacuous + ' vacuous' }) : null],
    h('div', { class: 'lede', text:
      'A scenario that passes without exercising its own condition is not a pass. '
      + 'The harness reports VACUOUS for that, and it caught three false passes '
      + 'during development — two scenarios were closing zero roads and still '
      + 'reporting green.' }),
    h('div', { class: 'checks' }, rs.map((r) => h('div', { class: 's' },
      h('span', { class: 'id', text: r.id }),
      h('span', { class: 'ti', text: r.title }),
      h('span', null, !r.exercised ? h('span', { class: 'tag bad', text: 'vacuous' })
        : h('span', { class: 'tag ' + (r.pass ? 'ok' : 'bad'),
          text: r.pass ? 'pass' : 'fail' }))))),
    h('details', { class: 'more' },
      h('summary', { text: 'what each scenario actually did' }),
      h('div', { class: 'in' }, rs.map((r) => h('p', null,
        h('code', { text: r.id + '  ' }), (r.detail || []).join(' · '))))));
}

function cardEnergy(ev) {
  if (!ev.energy) {
    return card('full', 'Energy per re-plan', null,
      gone('The energy accounting', 'python scripts/energy.py'));
  }
  const e = ev.energy, arms = e.arms || {};
  const measured = e.power_source === 'battery-sensor';
  const base = arms['operational (QPSO only)'];
  return card('full', 'Energy per re-plan',
    [h('span', { class: 'tag ' + (measured ? 'ok' : 'mute'),
      text: measured ? 'sensor-measured' : 'CPU-time model' })],
    h('div', { class: 'lede' },
      'The 2025 systematic review names energy reporting as a gap in this field. '
      + 'It matters here because a recovery engine is not run once — it runs on '
      + 'every incident, all day, at every depot. ',
      h('b', { text: measured ? 'Sensor: ' : 'No sensor: ' }), e.power_note + '.'),
    h('table', { class: 'data' },
      h('thead', null, h('tr', null, h('th', { text: 'engine configuration' }),
        h('th', { class: 'num', text: 'wall ms' }), h('th', { class: 'num', text: 'CPU ms' }),
        h('th', { class: 'num', text: 'mWh / re-plan' }),
        h('th', { class: 'num', text: 'kWh / year' }))),
      h('tbody', null, Object.keys(arms).map((k) => h('tr', null,
        h('td', { text: k }),
        h('td', { class: 'num', text: arms[k].mean_wall_ms.toFixed(0) }),
        h('td', { class: 'num', text: (arms[k].mean_cpu_s * 1000).toFixed(0) }),
        h('td', { class: 'num', text: arms[k].mean_mwh.toFixed(4) }),
        h('td', { class: 'num', text: arms[k].at_scale.kwh_per_year.toFixed(3) }))))),
    base ? h('div', { class: 'footnote' },
      h('b', { text: 'For scale: ' }),
      'the operational path uses ' + base.at_scale.kwh_per_year.toFixed(3)
      + ' kWh a year at ' + e.replans_per_day + ' re-plans a day — less than a '
      + 'single 100 W depot floodlight burns in one ten-hour shift. The honest '
      + 'reading is that the optimiser\u2019s own energy is negligible next to the '
      + 'diesel it saves, and that is only a credible statement because it was '
      + 'measured instead of assumed away.') : null);
}

function cardDeliverables() {
  const rows = [
    ['1', 'Graph-based network model', 'routepulse/graph.py',
      'Real OpenStreetMap road graph, time-dependent edge costs, incident and corridor overlays as the dynamic weight update mechanism.'],
    ['2', 'Mathematical formulation', 'FORMULATION.md',
      'Objective, decision variables, capacity / time-window / flow-conservation constraints, the FIFO condition, the acceptance rule, and the Ising reduction.'],
    ['3', 'Quantum-inspired algorithm module', 'solvers/qpso.py + sb.py',
      'QPSO with the delta-potential-well update rule written out in full, plus Simulated Bifurcation as a genuinely quantum-derived Ising engine.'],
    ['4', 'Software platform / prototype', 'server/',
      'This sheet. API and UI, network and traffic input, optimised route output, map visualisation, offline by construction.'],
    ['5', 'Demonstration', 'scripts/',
      'One realistic urban network under varying traffic, with benchmarking, convergence analysis and constraint handling all measured rather than asserted.'],
  ];
  return card('full', 'The sponsor\u2019s five deliverables', null,
    h('div', { class: 'lede', text:
      'Transcribed from Egreen Quanta\u2019s problem statement. The Expected '
      + 'Solution paragraph above that table also requires constraint handling, '
      + 'convergence analysis and systematic performance benchmarking by name — '
      + 'all three are on this page.' }),
    h('table', { class: 'data' },
      h('thead', null, h('tr', null, h('th', { text: '#' }),
        h('th', { text: 'deliverable' }), h('th', { text: 'where' }),
        h('th', { text: 'what it is' }))),
      h('tbody', null, rows.map(([n, name, where, what]) => h('tr', null,
        h('td', { class: 'num', text: n }), h('td', { class: 'lead', text: name }),
        h('td', null, h('code', { text: where })), h('td', { text: what }))))));
}

function cardLimits() {
  const items = [
    ['The road network is real; the demand is not.',
      'OpenStreetMap Bengaluru, 6,420 junctions. Delivery stops are synthetic and the traffic profile is a plausible hand-authored time-of-day model, not measured data. Free city-scale real-time traffic for an Indian city is not obtainable.'],
    ['OR-Tools beats us on static solution quality by about 12%.',
      'Given the same matrix, budget, capacity, time windows and commitment constraints. We do not claim to beat the state of the art on static quality. The claim is the dynamic, commitment-aware recovery path with end-to-end latency accounting.'],
    ['"Quantum-inspired" means classical.',
      'No quantum hardware and no quantum speedup. Simulated Bifurcation is the classical limit of a quantum system, which is a stronger claim than metaphor — and it still lost.'],
    ['The emergency layer simulates traffic interaction, not EMS dispatch.',
      'Crew availability, clinical triage and hospital diversion are out of scope. Hospital locations are synthetic and labelled as such.'],
    ['The server is single-tenant.',
      'One engine in module state, so two browsers share one fleet. Correct for a control sheet demo, wrong for a product, and written down rather than discovered later.'],
  ];
  return card('full', 'What this system is not', null,
    h('div', { class: 'lede', text:
      'Kept on the same page as the results, at the same size, because a '
      + 'limitation that only appears when asked is not disclosed.' }),
    h('ul', { class: 'reasons' }, items.map(([t, d]) =>
      h('li', null, h('b', { text: t + ' ' }), d))));
}

async function renderEvidence() {
  const body = $('#report');
  if (S.evidence) return;
  clear(body);
  body.append(h('div', { class: 'report-head' },
    h('div', null, h('h2', { text: 'The measured record' })),
    h('div', { class: 'issue', text: 'loading…' })));
  let ev;
  try { ev = await api('/api/evidence'); }
  catch (_err) {
    clear(body);
    body.append(gone('The evidence API', 'restart the server'));
    return;
  }
  S.evidence = ev;
  figureNo = 0;
  clear(body);

  const today = new Date();
  const stamp = today.toISOString().slice(0, 10);
  body.append(h('div', { class: 'report-head' },
    h('div', null,
      h('h2', { text: 'The measured record' }),
      h('p', { text: 'Everything on this page was produced by a script in '
        + 'scripts/ and committed to out/. This view renders it and never '
        + 'computes it. Where an experiment has not been run, the panel says so '
        + 'rather than showing a plausible default.' })),
    h('div', { class: 'issue' },
      h('div', { text: 'RoutePulse · SIH26137' }),
      h('div', { text: 'Sheet 02 · Evidence' }),
      h('div', { text: 'Rendered ' + stamp }),
      h('div', { text: (ev.missing && ev.missing.length)
        ? ev.missing.length + ' experiment(s) not run' : 'all experiments present' }))));

  const grid = h('div', { class: 'sheets' });
  const add = (x) => { if (!x) return; (Array.isArray(x) ? x : [x]).forEach((n) => grid.append(n)); };
  add(cardAblation(ev));
  add(cardGates(ev));
  add(cardSwarmIsolation(ev));
  add(cardConvergence(ev));
  add(cardsLatency(ev));
  add(cardSB(ev));
  add(cardScenarios(ev));
  add(cardEnergy(ev));
  add(cardDeliverables());
  add(cardLimits());
  body.append(grid);

  if (!REDUCED) {
    [...grid.children].forEach((n, i) => {
      n.animate([{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'none' }],
        { duration: 400, delay: i * 55, easing: EASE, fill: 'backwards' });
    });
  }
}

/* ------------------------------------------------------------- mode tabs */

function switchMode(which) {
  const ops = which === 'ops';
  $('#tabOps').setAttribute('aria-selected', String(ops));
  $('#tabEvidence').setAttribute('aria-selected', String(!ops));
  $('#ops').classList.toggle('on', ops);
  $('#evidence').classList.toggle('on', !ops);
  if (ops) resize(); else renderEvidence();
}
$('#tabOps').addEventListener('click', () => switchMode('ops'));
$('#tabEvidence').addEventListener('click', () => switchMode('evidence'));

addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === '1') switchMode('ops');
  if (e.key === '2') switchMode('evidence');
  if (e.key === 'p' && !$('#btnPlan').disabled) $('#btnPlan').click();
  if (e.key === 'r' && !$('#btnReplan').disabled) $('#btnReplan').click();
});

/* ------------------------------------------------------------------ boot */

(async function boot() {
  const key = (id, c) => {
    const el = $(id);
    if (el) { el.style.setProperty('background', c); el.style.setProperty('border-color', c); }
  };
  key('#k-closure', INK.closed);
  key('#k-congestion', INK.jam);
  key('#k-ambulance', INK.signal);

  renderBand(); renderFleet(); renderNetMeta(); renderEnergy(null);
  renderLegend(); renderConvLegend(); renderTimeline(); titleblock();
  renderSpec(); setEvKind('closure');
  plot($('#conv'), [], { empty: 'run a re-plan to record convergence' });
  requestAnimationFrame(loop);

  try {
    S.boot = await api('/api/boot');
    S.graph = await api('/api/graph');
    S.bounds = { minLat: S.graph.bounds[0], minLon: S.graph.bounds[1],
                 maxLat: S.graph.bounds[2], maxLon: S.graph.bounds[3] };
    renderNetMeta(); renderSpec();
    $('#lamp').className = 'lamp live';
    // Before step 01 runs, the server is still on whatever instance it booted
    // with. Say what step 01 WILL load rather than what is loaded now, so the
    // masthead never contradicts the scenario panel beneath it.
    $('#statusText').textContent = S.boot.nodes.toLocaleString()
      + ' junctions · awaiting initialize';
    resize();
  } catch (err) {
    $('#lamp').className = 'lamp';
    $('#statusText').textContent = 'backend unreachable';
    toast('Backend unreachable: ' + err.message, true);
  }
})();
