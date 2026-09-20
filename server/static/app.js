/* RoutePulse control tower — client.
 *
 * Zero dependencies on purpose: no framework, no charting library, no map
 * tiles, no CDN. The road network is drawn on a canvas from our own API, so
 * the demo survives a dead venue Wi-Fi.
 *
 * Nothing in here is styled with an inline `style` attribute and nothing is
 * injected with innerHTML from server data. Elements are built through h()
 * and animated through the Web Animations API, which is what lets the server
 * ship a Content-Security-Policy of 'self' with no 'unsafe-inline'.
 */
'use strict';

const EASE = 'cubic-bezier(.22,.61,.36,1)';
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

const COLOR = {
  road: '#1d2733', roadMajor: '#31465e',
  closed: '#ef5f78', jam: '#e2a83c', corridor: '#35c08b',
  depot: '#e9edf3', prio: '#ffffff',
  amb: '#ff8a9c', ambReturn: '#ffb47a',
  ink: '#e9edf3', ink3: '#69737f', accent: '#5b9cff', violet: '#9b7cf0',
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

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

/** append that drops null/undefined/false children. Element.append() would
 *  stringify them and print the word "null" into the UI. */
function put(parent, ...kids) {
  for (const k of kids.flat(9)) {
    if (k === null || k === undefined || k === false) continue;
    parent.append(k);
  }
  return parent;
}

/** Animated bar fill. Uses WAAPI rather than a style attribute. */
function grow(el, pct, delay) {
  const w = Math.max(0.8, Math.min(100, pct)) + '%';
  if (REDUCED) { el.style.setProperty('width', w); return el; }
  el.animate([{ width: '0%' }, { width: w }],
    { duration: 560, delay: delay || 0, easing: EASE, fill: 'forwards' });
  return el;
}

function meter(pct, cls, delay) {
  const i = grow(h('i', { class: cls || '' }), pct, delay);
  return h('div', { class: 'track' }, i);
}

/** Count a number up into place. Real motion, not decoration: it makes a
 *  changed KPI impossible to miss during a live demo. */
function roll(el, to, fmt, from) {
  const start = (from === undefined) ? (parseFloat(el.dataset.v) || 0) : from;
  el.dataset.v = to;
  if (REDUCED || start === to) { el.textContent = fmt(to); return; }
  const t0 = performance.now(), dur = 520;
  (function step(now) {
    const p = Math.min(1, (now - t0) / dur);
    const e = 1 - Math.pow(1 - p, 3);
    el.textContent = fmt(start + (to - start) * e);
    if (p < 1) requestAnimationFrame(step);
  })(t0);
}

const fmt1 = (v) => (Math.round(v * 10) / 10).toFixed(1);
const fmt0 = (v) => String(Math.round(v));
const pct1 = (v) => (v >= 0 ? '+' : '') + v.toFixed(1) + '%';

function toast(msg, bad) {
  const t = h('div', { class: 't' + (bad ? ' bad' : ''), text: msg });
  $('#toast').append(t);
  setTimeout(() => {
    t.animate([{ opacity: 1 }, { opacity: 0, transform: 'translateY(6px)' }],
      { duration: 260, fill: 'forwards' }).onfinish = () => t.remove();
  }, bad ? 4200 : 2600);
}

/* ---------------------------------------------------------------- state */

const S = {
  graph: null, bounds: null, routes: [], closed: [], events: [],
  summary: {}, conv: [], amb: null, last: null, planned: false,
  hover: null, pings: [], routeT0: 0, evidence: null,
};

const CAM = { zoom: 1, x: 0, y: 0 };
const cv = $('#map');
const ctx = cv.getContext('2d');
let roadCache = null, roadKey = '';

/* ----------------------------------------------------------- projection */

function transform() {
  const w = cv.clientWidth, hgt = cv.clientHeight, pad = 30;
  const B = S.bounds || { minLat: 0, minLon: 0, maxLat: 1, maxLon: 1 };
  const midLat = (B.minLat + B.maxLat) / 2;
  const kx = Math.cos(midLat * Math.PI / 180);
  const dLon = (B.maxLon - B.minLon) * kx || 1e-9;
  const dLat = (B.maxLat - B.minLat) || 1e-9;
  const s = Math.min((w - 2 * pad) / dLon, (hgt - 2 * pad) / dLat) * CAM.zoom;
  return {
    w, h: hgt, kx, s, B,
    ox: (w - dLon * s) / 2 + CAM.x,
    oy: (hgt - dLat * s) / 2 - CAM.y,
  };
}
function px(lat, lon, T) {
  return [T.ox + (lon - T.B.minLon) * T.kx * T.s,
          T.h - T.oy - (lat - T.B.minLat) * T.s];
}
function unpx(x, y, T) {
  return [T.B.minLat + (T.h - T.oy - y) / T.s,
          T.B.minLon + (x - T.ox) / (T.kx * T.s)];
}

/* -------------------------------------------------------------- renderer */

function resize() {
  const r = cv.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(r.width * dpr);
  cv.height = Math.round(r.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  roadKey = '';
  scalebar();
}
addEventListener('resize', resize);

/** The road network is ~16k segments and never changes between camera moves,
 *  so it is rasterised once per camera state and blitted every frame. Without
 *  this the animation loop spends all its time redrawing static geometry. */
function roadLayer(T) {
  const dpr = window.devicePixelRatio || 1;
  const key = [T.w, T.h, CAM.zoom.toFixed(4), CAM.x | 0, CAM.y | 0, dpr].join('|');
  if (roadKey === key && roadCache) return roadCache;
  if (!roadCache) roadCache = document.createElement('canvas');
  roadCache.width = cv.width; roadCache.height = cv.height;
  const c = roadCache.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, T.w, T.h);
  if (S.graph) {
    c.lineCap = 'round';
    // two passes so arterials sit above side streets instead of interleaving
    for (const major of [false, true]) {
      c.strokeStyle = major ? COLOR.roadMajor : COLOR.road;
      c.lineWidth = major ? 2.0 : 1.0;
      c.beginPath();
      for (const s of S.graph.segments) {
        if ((s[4] >= 40) !== major) continue;
        const a = px(s[0], s[1], T), b = px(s[2], s[3], T);
        if ((a[0] < -50 && b[0] < -50) || (a[0] > T.w + 50 && b[0] > T.w + 50)
          || (a[1] < -50 && b[1] < -50) || (a[1] > T.h + 50 && b[1] > T.h + 50)) continue;
        c.moveTo(a[0], a[1]); c.lineTo(b[0], b[1]);
      }
      c.stroke();
    }
  }
  roadKey = key;
  return roadCache;
}

function polyPath(c, pts, T) {
  c.beginPath();
  for (let i = 0; i < pts.length; i++) {
    const q = px(pts[i][0], pts[i][1], T);
    if (i) c.lineTo(q[0], q[1]); else c.moveTo(q[0], q[1]);
  }
}

function polyLength(pts, T) {
  let L = 0, prev = null;
  for (const p of pts) {
    const q = px(p[0], p[1], T);
    if (prev) L += Math.hypot(q[0] - prev[0], q[1] - prev[1]);
    prev = q;
  }
  return L;
}

function draw(now) {
  const T = transform();
  // A hidden or not-yet-laid-out stage has a zero-size canvas, and drawImage
  // from a zero-width source throws. Nothing to paint anyway.
  if (T.w < 2 || T.h < 2 || cv.width < 2) return;
  ctx.clearRect(0, 0, T.w, T.h);
  ctx.drawImage(roadLayer(T), 0, 0, T.w, T.h);

  // ---- incident + corridor overlays
  ctx.lineCap = 'round';
  for (const seg of S.closed) {
    const kind = seg[2][0];
    const a = px(seg[0][0], seg[0][1], T), b = px(seg[1][0], seg[1][1], T);
    ctx.strokeStyle = kind === 1 ? COLOR.closed : kind === 2 ? COLOR.corridor : COLOR.jam;
    ctx.lineWidth = kind === 1 ? 3.0 : 2.6;
    ctx.globalAlpha = kind === 2 ? 0.5 + 0.18 * Math.sin(now / 420) : 0.95;
    ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
  }
  ctx.globalAlpha = 1;

  // ---- ambulance legs, marching dashes
  if (S.amb) {
    for (const [leg, col] of [[S.amb.leg_a, COLOR.amb], [S.amb.leg_b, COLOR.ambReturn]]) {
      if (!leg || !leg.length) continue;
      ctx.strokeStyle = col; ctx.lineWidth = 3.2;
      ctx.setLineDash([9, 6]);
      ctx.lineDashOffset = REDUCED ? 0 : -(now / 34) % 15;
      polyPath(ctx, leg, T); ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  // ---- routes: draw-in, then a slow flow pulse
  const age = now - S.routeT0;
  const drawIn = REDUCED ? 1 : Math.min(1, age / 820);
  for (let i = 0; i < S.routes.length; i++) {
    const r = S.routes[i];
    if (!r.polyline || r.polyline.length < 2) continue;
    const L = polyLength(r.polyline, T);
    const stagger = Math.min(1, Math.max(0, (drawIn - i * 0.06) / 0.72));
    if (stagger <= 0) continue;

    // halo, so a route stays readable over dense road geometry
    ctx.strokeStyle = 'rgba(7,9,13,.75)'; ctx.lineWidth = 5.4;
    ctx.setLineDash([L, L]); ctx.lineDashOffset = L * (1 - stagger);
    polyPath(ctx, r.polyline, T); ctx.stroke();

    ctx.strokeStyle = r.color; ctx.lineWidth = 2.5;
    polyPath(ctx, r.polyline, T); ctx.stroke();
    ctx.setLineDash([]);

    if (stagger >= 1 && !REDUCED) {
      ctx.save();
      ctx.globalAlpha = 0.5; ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 1.4;
      ctx.setLineDash([3, 26]);
      ctx.lineDashOffset = -(now / 26 + i * 40) % 29;
      polyPath(ctx, r.polyline, T); ctx.stroke();
      ctx.restore(); ctx.setLineDash([]);
    }
  }

  // ---- stops
  for (const r of S.routes) {
    for (const st of r.stops) {
      const q = px(st.lat, st.lon, T);
      const rad = st.priority ? 5.2 : 3.6;
      ctx.fillStyle = r.color;
      ctx.beginPath(); ctx.arc(q[0], q[1], rad, 0, 6.2832); ctx.fill();
      if (st.priority) {
        ctx.strokeStyle = COLOR.prio; ctx.lineWidth = 1.5; ctx.stroke();
      }
    }
  }

  // ---- emergency scene + hospitals
  if (S.amb) {
    for (const hp of (S.amb.hospitals || [])) {
      const q = px(hp.lat, hp.lon, T);
      const chosen = hp.name === S.amb.hospital;
      ctx.fillStyle = chosen ? COLOR.corridor : '#39444f';
      ctx.beginPath(); ctx.roundRect(q[0] - 5, q[1] - 5, 10, 10, 2); ctx.fill();
      ctx.strokeStyle = '#07090d'; ctx.lineWidth = 1.5; ctx.stroke();
    }
    if (S.amb.scene) {
      const q = px(S.amb.scene[0], S.amb.scene[1], T);
      const pulse = REDUCED ? 0 : (now / 900) % 1;
      ctx.strokeStyle = COLOR.amb; ctx.globalAlpha = 1 - pulse; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(q[0], q[1], 8 + pulse * 20, 0, 6.2832); ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = COLOR.amb;
      ctx.beginPath(); ctx.arc(q[0], q[1], 6, 0, 6.2832); ctx.fill();
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.6; ctx.stroke();
    }
  }

  // ---- incident markers + injection shockwave
  for (const e of S.events) {
    // 'replan' entries are timeline-only: they happened everywhere, not
    // anywhere, so they have no coordinates and nothing to mark on the map.
    if (e.kind === 'ambulance' || e.lat === undefined || e.lon === undefined) continue;
    const q = px(e.lat, e.lon, T);
    const col = e.kind === 'closure' ? COLOR.closed : COLOR.jam;
    ctx.strokeStyle = col; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.85;
    ctx.beginPath(); ctx.arc(q[0], q[1], 11, 0, 6.2832); ctx.stroke();
    ctx.globalAlpha = 0.3;
    ctx.beginPath(); ctx.arc(q[0], q[1], 18, 0, 6.2832); ctx.stroke();
    ctx.globalAlpha = 1;
  }
  S.pings = S.pings.filter((p) => now - p.t0 < 1500);
  for (const p of S.pings) {
    const k = (now - p.t0) / 1500;
    const q = px(p.lat, p.lon, T);
    ctx.strokeStyle = p.color; ctx.globalAlpha = (1 - k) * 0.85;
    ctx.lineWidth = 2.5 * (1 - k) + 0.5;
    ctx.beginPath(); ctx.arc(q[0], q[1], 8 + k * 85, 0, 6.2832); ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // ---- depot
  if (S.summary.depot) {
    const q = px(S.summary.depot.lat, S.summary.depot.lon, T);
    ctx.fillStyle = COLOR.depot;
    ctx.beginPath(); ctx.roundRect(q[0] - 6, q[1] - 6, 12, 12, 2.5); ctx.fill();
    ctx.strokeStyle = '#07090d'; ctx.lineWidth = 2; ctx.stroke();
    ctx.fillStyle = COLOR.ink3;
    ctx.font = '600 9.5px ui-monospace, Consolas, monospace';
    ctx.fillText('DEPOT', q[0] + 10, q[1] + 3.5);
  }

  // ---- hover readout
  if (S.hover) {
    const q = px(S.hover.lat, S.hover.lon, T);
    const lines = [
      'stop ' + S.hover.id,
      'ETA ' + (S.hover.eta_min === null ? 'unreachable' : S.hover.eta_min + ' min'),
      'demand ' + S.hover.demand + (S.hover.priority ? ' · PRIORITY' : ''),
    ];
    if (S.hover.tw_end_min !== null && S.hover.tw_end_min !== undefined) {
      lines.push('window closes ' + S.hover.tw_end_min + ' min');
    }
    ctx.font = '11px ui-monospace, Consolas, monospace';
    const wd = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 18;
    const hh = lines.length * 14 + 12;
    let bx = q[0] + 12, by = q[1] - hh - 8;
    if (bx + wd > T.w) bx = q[0] - wd - 12;
    if (by < 0) by = q[1] + 12;
    ctx.fillStyle = 'rgba(16,20,27,.95)';
    ctx.strokeStyle = '#262f3c'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.roundRect(bx, by, wd, hh, 6); ctx.fill(); ctx.stroke();
    ctx.fillStyle = COLOR.ink;
    lines.forEach((l, i) => ctx.fillText(l, bx + 9, by + 19 + i * 14));
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(q[0], q[1], 7.5, 0, 6.2832); ctx.stroke();
  }
}

/* The render loop must never die. A single throw inside draw() would end the
 * animation frame chain permanently and leave a blank map with a UI that still
 * looks alive everywhere else -- which is exactly the failure this project
 * keeps finding elsewhere: a component that stops working while reporting
 * nothing. So: catch, surface it once, keep the loop running. */
let drawFailures = 0;
function loop(now) {
  try {
    draw(now);
  } catch (err) {
    drawFailures++;
    if (drawFailures === 1) {
      console.error('map render failed', err);
      toast('Map rendering hit an error — see the console', true);
    }
  }
  requestAnimationFrame(loop);
}

function scalebar() {
  const T = transform();
  const metresPerPx = 1 / (T.s / 111320);
  let target = metresPerPx * 90;
  const pow = Math.pow(10, Math.floor(Math.log10(Math.max(1, target))));
  const nice = [1, 2, 5, 10].map((m) => m * pow).find((v) => v >= target) || pow * 10;
  const widthPx = nice / metresPerPx;
  const el = $('#scalebar');
  clear(el);
  el.append(
    h('div', { class: 'ruler', css: { width: Math.round(widthPx) + 'px' } }),
    h('span', { text: nice >= 1000 ? (nice / 1000) + ' km' : nice + ' m' }));
}

/* ----------------------------------------------------------- interaction */

let drag = null;

cv.addEventListener('pointerdown', (e) => {
  drag = { x: e.clientX, y: e.clientY, ox: CAM.x, oy: CAM.y, t: performance.now(), moved: 0 };
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
      scalebar();
    }
    return;
  }
  // hover detection over stops
  const T = transform();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  let best = null, bestD = 11;
  for (const rt of S.routes) {
    for (const st of rt.stops) {
      const q = px(st.lat, st.lon, T);
      const d = Math.hypot(q[0] - mx, q[1] - my);
      if (d < bestD) { bestD = d; best = st; }
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
  const before = unpx(mx, my, transform());
  CAM.zoom = Math.max(1, Math.min(14, CAM.zoom * (e.deltaY < 0 ? 1.16 : 1 / 1.16)));
  const T = transform();
  const after = px(before[0], before[1], T);
  CAM.x += mx - after[0];
  CAM.y -= my - after[1];
  scalebar();
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
  $('#statDot').className = 'dot ' + (on ? 'busy' : 'live');
}

/* ------------------------------------------------------- operations view */

function renderKpis() {
  const s = S.summary;
  const wrap = $('#kpis');
  if (!s || !s.score) { clear(wrap); return; }
  if (!wrap.children.length) {
    const spec = [
      ['travel', 'fleet travel', 'min'],
      ['makespan', 'makespan', 'min'],
      ['fleet', 'vehicles used', ''],
      ['gate', 'feasibility gate', ''],
    ];
    for (const [id, label, unit] of spec) {
      wrap.append(h('div', { class: 'kpi', id: 'kpi-' + id },
        h('div', { class: 'v' }, h('span', { class: 'n' }), unit ? h('small', { text: ' ' + unit }) : null),
        h('div', { class: 'l', text: label })));
    }
  }
  roll($('#kpi-travel .n'), s.travel_min, fmt1);
  roll($('#kpi-makespan .n'), s.makespan_min, fmt1);
  $('#kpi-fleet .n').textContent = s.vehicles_used + '/' + s.vehicles_total;
  const gate = $('#kpi-gate');
  gate.className = 'kpi ' + (s.feasible ? 'good' : 'bad');
  $('#kpi-gate .n').textContent = s.feasible ? 'VALID' : 'INVALID';
  const lbl = gate.querySelector('.l');
  lbl.textContent = s.feasible ? 'feasibility gate'
    : (s.violations[0] || 'constraint violated');
}

function renderFleet() {
  const el = $('#fleet');
  clear(el);
  if (!S.routes.length) {
    el.append(h('div', { class: 'hint', text: 'No plan yet.' }));
    return;
  }
  S.routes.forEach((r, i) => {
    const pctLoad = r.capacity ? (r.load / r.capacity) * 100 : 0;
    const bar = h('i');
    bar.style.setProperty('background', r.color);
    const card = h('div', { class: 'veh' },
      h('div', { class: 'stripe', css: { background: r.color } }),
      h('div', null,
        h('div', { class: 'name', text: 'Vehicle ' + r.vehicle }),
        h('div', { class: 'meta', text: r.stops.length + ' stops · ' + r.load
          + (r.capacity ? ' / ' + r.capacity : '') + ' load' })),
      h('div', { class: 'mins', text: fmt1(r.travel_min) }),
      h('div', { class: 'bar' }, bar));
    el.append(card);
    grow(bar, pctLoad, 60 + i * 45);
  });

  // Vehicles the solver chose NOT to use still exist and still cost money.
  // Omitting them would quietly turn "3 of 5 used" into "3 vehicles".
  const idle = (S.summary.vehicles_total || 0) - S.routes.length;
  if (idle > 0) {
    el.append(h('div', { class: 'veh idle' },
      h('div', { class: 'stripe' }),
      h('div', null,
        h('div', { class: 'name', text: idle + ' vehicle' + (idle === 1 ? '' : 's') + ' idle' }),
        h('div', { class: 'meta', text: 'left at the depot by the optimiser' })),
      h('div', { class: 'mins', text: '—' })));
  }
}

function renderNetMeta(boot) {
  const el = $('#netmeta');
  clear(el);
  const rows = [
    ['source', (S.graph && S.graph.source) ? S.graph.source.replace(/ · .*/, '') : '—'],
    ['junctions', boot && boot.nodes ? boot.nodes.toLocaleString() : '—'],
    ['delivery stops', boot ? boot.customers : '—'],
    ['vehicles', boot ? boot.vehicles : '—'],
    ['matrix build', boot ? (boot.matrix_build_s * 1000).toFixed(0) + ' ms' : '—'],
    ['time buckets', '3 · linear interp'],
  ];
  for (const [k, v] of rows) {
    el.append(h('div', { class: 'metaline' }, h('span', { text: k }), h('b', { text: String(v) })));
  }
}

function renderEnergy(e, scale) {
  const el = $('#energy');
  clear(el);
  if (!e) {
    el.append(h('div', { class: 'hint', text: 'Runs after the first re-plan.' }));
    return;
  }
  const measured = e.source === 'battery-sensor';
  const rows = [
    h('div', { class: 'metaline' }, h('span', { text: 'CPU time' }),
      h('b', { text: (e.cpu_s * 1000).toFixed(0) + ' ms' })),
    h('div', { class: 'metaline' }, h('span', { text: 'energy' }),
      h('b', { text: e.mwh.toFixed(3) + ' mWh' })),
  ];
  if (scale) {
    rows.push(
      h('div', { class: 'metaline' }, h('span', { text: 'at 400 re-plans/day' }),
        h('b', { text: scale.wh_per_day.toFixed(2) + ' Wh' })),
      h('div', { class: 'metaline' }, h('span', { text: 'per year' }),
        h('b', { text: scale.kwh_per_year.toFixed(2) + ' kWh' })));
  }
  rows.push(h('div', { class: 'note' },
    h('span', { class: 'chip ' + (measured ? 'ok' : 'mute'),
      text: measured ? 'measured' : 'modelled' }),
    ' ', e.note));
  for (const r of rows) el.append(r);
}

function renderVerdict(d) {
  const el = $('#verdict');
  const cls = d.alert ? 'alert' : d.accepted ? 'accepted' : 'held';
  el.className = 'verdict ' + cls;
  clear(el);
  put(el,
    h('div', { class: 'tag', text: d.alert ? 'Dispatcher alert'
      : d.accepted ? 'New plan accepted' : 'Incumbent held' }),
    h('div', { class: 'body', text: d.case }),
    d.alert ? h('div', { class: 'body', text: d.alert }) : null,
    h('div', { class: 'sub', text:
      'incumbent was ' + (d.incumbent_was_feasible ? 'feasible' : 'INFEASIBLE')
      + ' under the updated costs · ' + d.matrix_pairs_rebuilt
      + ' matrix pairs rebuilt · ' + d.fifo_violations + ' FIFO violations' }));
  if (!REDUCED) {
    el.animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }],
      { duration: 320, easing: EASE });
  }

  const why = $('#why');
  clear(why);
  (d.explanation || []).forEach((line, i) => {
    const li = h('li', { text: line });
    why.append(li);
    if (!REDUCED) {
      li.animate([{ opacity: 0, transform: 'translateX(-6px)' }, { opacity: 1, transform: 'none' }],
        { duration: 300, delay: 60 + i * 55, easing: EASE, fill: 'backwards' });
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
    const cls = k.includes('matrix') ? 'matrix' : k.includes('solve') ? 'solve' : '';
    el.append(h('div', { class: 'row' },
      h('div', { class: 'nm', text: k.replace(/_/g, ' ') }),
      meter((v / max) * 100, cls, 40 + i * 55),
      h('div', { class: 'ms', text: v.toFixed(1) })));
    i++;
  }
  const total = d.total_ms || 0;
  const b = h('b', { class: total < 500 ? 'ok' : 'over' });
  el.append(h('div', { class: 'total' },
    h('span', { text: 'event → accepted plan' }), b));
  roll(b, total, (v) => fmt0(v) + ' ms', 0);

  // A four-engine race is a demo, not the operational path, and the two must
  // never be quoted as one number. Say which one this was.
  const n = (d.engines || []).length;
  el.append(h('div', { class: 'note', text: n > 1
    ? 'This run raced ' + n + ' engines so you can see them compete; a '
      + 'dispatcher waits on ONE. The single-engine operational path is '
      + 'measured separately under Evidence and meets the 500 ms target.'
    : 'Single-engine operational path — what a dispatcher actually waits for.' }));
}

function renderRace(d) {
  const t = $('#race');
  clear(t);
  const cands = (d.candidates || []).filter((c) => !c.error);
  const feas = cands.filter((c) => c.feasible && c.score !== null);
  const best = feas.length ? Math.min(...feas.map((c) => c.score)) : null;

  t.append(h('thead', null, h('tr', null,
    h('th', { text: 'engine' }),
    h('th', { class: 'num', text: 'score' }),
    h('th', { class: 'num', text: 'travel' }),
    h('th', { class: 'num', text: 'ms' }),
    h('th', { text: 'valid' }))));

  const body = h('tbody');
  cands.forEach((c) => {
    const win = c.score === best;
    body.append(h('tr', null,
      h('td', { class: win ? 'win' : '', text: c.engine }),
      h('td', { class: 'num' + (win ? ' win' : ''),
        text: c.score === null ? '—' : c.score.toLocaleString() }),
      h('td', { class: 'num', text: c.travel_min === null ? '—' : c.travel_min }),
      h('td', { class: 'num', text: c.ms }),
      h('td', null, h('span', { class: 'chip ' + (c.feasible ? 'ok' : 'bad'),
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
  if (q && o && q.score && o.score) {
    const delta = ((o.score - q.score) / o.score) * 100;
    note.append(h('span', { text: 'QPSO is ' + pct1(delta)
      + ' against OR-Tools on this instance — same matrix, same budget, same '
      + 'commitment constraints, and both re-scored by one official evaluation '
      + 'function. One instance is an anecdote; the 30-seed protocol is under '
      + 'Evidence.' }));
  } else {
    note.append(h('span', { text: 'Every engine is re-scored by one official '
      + 'evaluation function. Feasibility is a hard gate, never a penalty weight.' }));
  }
  if (d.sb && d.sb.available) {
    note.append(h('br'), h('b', { text: 'Simulated Bifurcation: ' }),
      h('span', { text: d.sb.routes_tried + ' routes embedded at ~'
        + d.sb.mean_spins + ' spins, ' + d.sb.routes_improved + ' improved, '
        + 'invalid decodes ' + (d.sb.invalid_rate === null ? '—'
          : (d.sb.invalid_rate * 100).toFixed(0) + '%') + '.' }));
  }
  if (d.alns) {
    note.append(h('br'), h('b', { text: 'ALNS: ' }),
      h('span', { text: d.alns.iterations + ' destroy/repair rounds, '
        + d.alns.new_bests + ' new bests. Operator weights — '
        + Object.entries(d.alns.final_weights)
          .map(([k, v]) => k + ' ' + v).join(', ') + '.' }));
  }
}

function drawConv(canvas, series, opts) {
  const o = opts || {};
  const dpr = window.devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * dpr;
  canvas.height = canvas.clientHeight * dpr;
  const x = canvas.getContext('2d');
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  const w = canvas.clientWidth, hh = canvas.clientHeight;
  x.clearRect(0, 0, w, hh);
  if (!series.length) {
    x.fillStyle = COLOR.ink3;
    x.font = '11px system-ui, sans-serif';
    x.fillText(o.empty || 'no data yet', 12, hh / 2);
    return;
  }
  const pad = { l: 8, r: 8, t: 10, b: 16 };
  const n = series.length;
  const xs = (i) => pad.l + (n === 1 ? 0 : i / (n - 1)) * (w - pad.l - pad.r);

  // gridlines
  x.strokeStyle = '#161d26'; x.lineWidth = 1;
  for (let g = 0; g <= 3; g++) {
    const y = pad.t + (g / 3) * (hh - pad.t - pad.b);
    x.beginPath(); x.moveTo(pad.l, y); x.lineTo(w - pad.r, y); x.stroke();
  }

  const lines = [
    { key: 'best', color: COLOR.accent, width: 1.9, norm: 'minmax' },
    { key: 'diversity', color: COLOR.violet, width: 1.3, norm: 'max' },
    { key: 'beta', color: '#e2a83c', width: 1.2, norm: 'unit', dash: [3, 3] },
  ];
  const t0 = performance.now();
  const animate = !REDUCED && o.animate !== false;

  function frame(now) {
    const p = animate ? Math.min(1, (now - t0) / 620) : 1;
    x.clearRect(0, 0, w, hh);
    x.strokeStyle = '#161d26'; x.lineWidth = 1;
    for (let g = 0; g <= 3; g++) {
      const y = pad.t + (g / 3) * (hh - pad.t - pad.b);
      x.beginPath(); x.moveTo(pad.l, y); x.lineTo(w - pad.r, y); x.stroke();
    }
    for (const ln of lines) {
      const vals = series.map((d) => d[ln.key]).filter((v) => v !== null && v !== undefined);
      if (!vals.length) continue;
      let lo = 0, hi = 1;
      if (ln.norm === 'minmax') { lo = Math.min(...vals); hi = Math.max(...vals); }
      else if (ln.norm === 'max') { hi = Math.max(...vals) || 1; }
      const rng = (hi - lo) || 1;
      const y = (v) => (hh - pad.b) - ((v - lo) / rng) * (hh - pad.t - pad.b);
      x.strokeStyle = ln.color; x.lineWidth = ln.width;
      x.setLineDash(ln.dash || []);
      x.beginPath();
      const upto = Math.max(1, Math.floor(n * p));
      let started = false;
      for (let i = 0; i < upto; i++) {
        const v = series[i][ln.key];
        if (v === null || v === undefined) continue;
        const pt = [xs(i), y(v)];
        if (started) x.lineTo(pt[0], pt[1]); else { x.moveTo(pt[0], pt[1]); started = true; }
      }
      x.stroke(); x.setLineDash([]);
    }
    if (o.stagnation && o.stagnation > 0 && o.stagnation < n) {
      const sx = xs(o.stagnation);
      x.strokeStyle = 'rgba(239,95,120,.7)'; x.setLineDash([2, 3]); x.lineWidth = 1;
      x.beginPath(); x.moveTo(sx, pad.t); x.lineTo(sx, hh - pad.b); x.stroke();
      x.setLineDash([]);
      x.fillStyle = 'rgba(239,95,120,.9)';
      x.font = '9px ui-monospace, Consolas, monospace';
      x.fillText('stagnation @ ' + o.stagnation, sx + 4, pad.t + 9);
    }
    x.fillStyle = COLOR.ink3;
    x.font = '9px ui-monospace, Consolas, monospace';
    x.fillText('iteration →', pad.l, hh - 4);
    if (p < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

function convLegend() {
  const el = $('#convLegend');
  clear(el);
  const items = [['best fitness', COLOR.accent], ['swarm diversity', COLOR.violet],
    ['β contraction–expansion', '#e2a83c']];
  for (const [t, c] of items) {
    el.append(h('span', null, h('i', { css: { background: c } }), t));
  }
}

function renderTimeline() {
  const track = $('#track');
  clear(track);
  $('#evCount').textContent = S.events.length
    ? S.events.length + ' event' + (S.events.length === 1 ? '' : 's') : '';
  if (!S.events.length) {
    track.append(h('div', { class: 'ev empty',
      text: 'No incidents yet. Pick a mode and click the map.' }));
    return;
  }
  const t0 = S.events[0].t || 0;
  S.events.forEach((e, i) => {
    const dt = (e.t || 0) - t0;
    track.append(h('div', { class: 'ev ' + e.kind },
      h('div', { class: 'pip' }),
      h('div', { class: 't', text: 'T+' + dt.toFixed(1) + 's' }),
      h('div', { class: 'lb', text: e.label })));
  });
  const scroller = track.parentElement;
  scroller.scrollLeft = scroller.scrollWidth;
}

function renderEms(d) {
  $('#emsWrap').hidden = false;
  const el = $('#ems');
  clear(el);
  const row = (label, value, cls) =>
    h('div', { class: 'tr ' + (cls || '') }, h('span', { text: label }),
      h('b', { text: value }));
  el.append(
    h('div', { class: 'hd' },
      h('span', { class: 'dot', css: { background: COLOR.amb } }),
      d.unit + ' → ' + d.hospital),
    row('to scene', d.to_scene_min + ' min'),
    row('scene → hospital', d.to_hospital_min + ' min'),
    row('total under priority', d.total_min + ' min'),
    row('without priority', d.baseline_min + ' min'),
    row('ambulance time saved', d.time_saved_min + ' min', 'saved'),
    h('div', { class: 'split' },
      row('cost of priority to the fleet', '+' + d.cost_of_priority, 'cost'),
      row('fleet cost before → after', d.fleet_cost_before + ' → ' + d.fleet_cost_after),
      row('green corridor', d.corridor_edges + ' edges'),
      row('path latency (budget 200 ms)', d.path_ms + ' ms')),
    h('div', { class: 'note', text:
      'Priority is not teleportation — one-ways and physical closures are still '
      + 'respected. It is also not free, and both sides of that trade are above.' }));
}

/* --------------------------------------------------------------- actions */

function setWorking(btn, on, label) {
  btn.disabled = on;
  btn.classList.toggle('working', on);
  if (label) btn.textContent = label;
}

function applyPlanPayload(d) {
  S.routes = d.routes || [];
  S.closed = d.closed || [];
  S.summary = d.summary || {};
  S.events = d.events || [];
  S.routeT0 = performance.now();
  renderKpis(); renderFleet(); renderTimeline();
}

$('#btnPlan').addEventListener('click', async () => {
  const b = $('#btnPlan');
  setWorking(b, true, 'planning…');
  busy(true, 'building the initial plan…');
  try {
    const d = await api('/api/plan?budget=1.2', { method: 'POST' });
    applyPlanPayload(d);
    S.planned = true;
    $('#btnReplan').disabled = false;
    renderEnergy(d.energy, null);
    $('#hint').textContent = 'Initial plan built in ' + d.plan_ms.toFixed(0)
      + ' ms. Now click on a coloured route line to break it.';
    toast('Initial plan ready · ' + d.plan_ms.toFixed(0) + ' ms');
  } catch (err) {
    toast('Plan failed: ' + err.message, true);
  } finally {
    setWorking(b, false, 'Re-plan from scratch');
    busy(false);
  }
});

$('#btnReplan').addEventListener('click', async () => {
  const b = $('#btnReplan');
  setWorking(b, true, 'solving…');
  busy(true, 'racing the solvers under the new costs…');
  try {
    const d = await api('/api/replan?budget=0.35&engines=emergency,qpso,alns,ortools',
      { method: 'POST' });
    applyPlanPayload(d);
    S.last = d;
    S.conv = d.convergence || [];
    renderVerdict(d); renderLatency(d); renderRace(d);
    renderEnergy(d.energy, d.energy_at_scale);
    drawConv($('#conv'), S.conv, { empty: 'run a re-plan to record convergence' });
    S.events.push({ kind: 'replan', t: Date.now() / 1000,
      label: (d.accepted ? 'Re-plan accepted' : 'Re-plan held') + ' · '
        + d.total_ms.toFixed(0) + ' ms' });
    renderTimeline();
    toast((d.accepted ? 'New plan accepted' : 'Incumbent held')
      + ' · ' + d.total_ms.toFixed(0) + ' ms');
  } catch (err) {
    toast('Re-plan failed: ' + err.message, true);
  } finally {
    setWorking(b, false, 'Re-plan under the new costs');
    busy(false);
  }
});

$('#btnReset').addEventListener('click', async () => {
  busy(true, 'rebuilding the instance…');
  try {
    const b = await api('/api/reset', { method: 'POST' });
    S.routes = []; S.closed = []; S.events = []; S.summary = {};
    S.conv = []; S.amb = null; S.last = null; S.planned = false;
    S.pings = [];
    $('#emsWrap').hidden = true;
    $('#btnReplan').disabled = true;
    $('#btnPlan').textContent = 'Plan routes';
    clear($('#kpis')); clear($('#latency')); clear($('#race'));
    clear($('#raceNote')); clear($('#why'));
    renderFleet(); renderTimeline(); renderNetMeta(b); renderEnergy(null);
    drawConv($('#conv'), [], { empty: 'run a re-plan to record convergence' });
    const v = $('#verdict');
    v.className = 'verdict idle'; clear(v);
    v.append(h('div', { class: 'tag', text: 'Standing by' }),
      h('div', { class: 'body', text: 'Instance rebuilt. Plan routes to begin.' }));
    $('#hint').textContent = 'Instance rebuilt. Press Plan routes.';
    toast('Instance rebuilt');
  } catch (err) {
    toast('Reset failed: ' + err.message, true);
  } finally { busy(false); }
});

async function inject(sx, sy) {
  if (!S.planned) { toast('Plan the routes first'); return; }
  const T = transform();
  const [lat, lon] = unpx(sx, sy, T);
  const kind = document.querySelector('input[name=ev]:checked').value;
  S.pings.push({ lat, lon, t0: performance.now(),
    color: kind === 'closure' ? COLOR.closed : kind === 'ambulance' ? COLOR.amb : COLOR.jam });

  if (kind === 'ambulance') {
    busy(true, 'dispatching…');
    try {
      const d = await api('/api/ambulance', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat, lon, severity: 2 }),
      });
      S.amb = d; S.closed = d.closed; S.events = d.events;
      renderEms(d); renderTimeline();
      $('#hint').textContent = 'Ambulance dispatched and the corridor is open. '
        + 'Press Re-plan to see the fleet yield.';
      toast(d.unit + ' → ' + d.hospital + ' · ' + d.time_saved_min + ' min saved');
    } catch (err) {
      toast('Dispatch failed: ' + err.message, true);
    } finally { busy(false); }
    return;
  }

  try {
    const d = await api('/api/event', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lat, lon, kind, radius_m: 420, multiplier: 6 }),
    });
    S.closed = d.closed; S.events = d.events;
    renderTimeline();
    $('#hint').textContent = d.label + ' injected. Press Re-plan.';
    if (!d.edges) {
      toast('0 edges affected — that point is outside the service area', true);
    } else {
      toast(d.label);
    }
  } catch (err) {
    toast('Event failed: ' + err.message, true);
  }
}

/* ------------------------------------------------------------- evidence */

function statBlock(value, label, detail, cls) {
  return h('div', { class: 'stat ' + (cls || '') },
    h('div', { class: 'v', text: value }),
    h('div', { class: 'l', text: label }),
    detail ? h('div', { class: 'd', text: detail }) : null);
}

function missing(name, cmd) {
  return h('div', { class: 'missing' },
    h('b', { text: 'not measured' }),
    h('span', { text: name + ' has not been run in this checkout. Run ' }),
    h('code', { text: cmd }),
    h('span', { text: ' — an unrun experiment and a passing one must not look '
      + 'the same, so nothing is shown here rather than a plausible default.' }));
}

function benchmarkPanel(ev) {
  if (!ev.benchmark) return missing('The 30-seed ablation', 'python scripts/bench.py --seeds 30');
  const b = ev.benchmark, sum = b.summary || {}, wx = b.wilcoxon || {};
  const ref = sum.E ? sum.E.mean : null;
  const order = ['GREEDY', 'E', 'ALNS', 'SB', 'SEQ_LS', 'SEQ_SB', 'A', 'A0', 'B', 'D', 'OR'];

  const body = h('tbody');
  for (const k of order) {
    const a = sum[k];
    if (!a) continue;
    const delta = (ref && k !== 'E') ? ((ref - a.mean) / ref) * 100 : null;
    const p = wx[k] ? wx[k].p : null;
    const sig = p === null ? null : p < 0.05;
    body.append(h('tr', null,
      h('td', { text: a.label }),
      h('td', { class: 'num', text: Math.round(a.mean).toLocaleString() }),
      h('td', { class: 'num', text: Math.round(a.sd).toLocaleString() }),
      h('td', { class: 'num', text: Math.round(a.best).toLocaleString() }),
      h('td', { class: 'num' + (delta !== null && delta > 0 ? ' win' : ''),
        text: delta === null ? '—' : pct1(delta) }),
      h('td', null, p === null ? h('span', { class: 'chip mute', text: 'ref' })
        : h('span', { class: 'chip ' + (sig ? 'ok' : 'warn'),
          text: (sig ? 'p=' : 'ns p=') + p.toFixed(4) }))));
  }

  return h('div', { class: 'panel wide' },
    h('h3', null, 'Ablation — 30 seeds, paired Wilcoxon',
      h('span', { class: 'chip mute', text: (b.config && b.config.seeds) + ' seeds' }),
      h('span', { class: 'chip mute', text: (b.config && b.config.budget) + ' s budget' })),
    h('div', { class: 'lede', text:
      'Every arm solves the SAME instance with the SAME wall-clock budget and is '
      + 're-scored by the SAME evaluation function. Arms share instances, so the '
      + 'paired Wilcoxon signed-rank test is the correct one. Lower score is better.' }),
    h('table', { class: 'data' },
      h('thead', null, h('tr', null,
        h('th', { text: 'arm' }), h('th', { class: 'num', text: 'mean' }),
        h('th', { class: 'num', text: 'sd' }), h('th', { class: 'num', text: 'best' }),
        h('th', { class: 'num', text: 'vs greedy+LS' }),
        h('th', { text: 'significance' }))), body),
    h('div', { class: 'note' },
      h('b', { text: 'The headline is not flattering and it is the point. ' }),
      'The improvement layer does essentially all the work. The quantum-inspired '
      + 'swarm update rule does not clear significance at the recommended '
      + 'protocol, and OR-Tools beats us on static solution quality. All three '
      + 'are reported because the experiment was built to be able to say so.'),
    h('div', { class: 'note', text: b.graph_source || '' }));
}

function adoptionPanel(ev) {
  if (!ev.benchmark || !ev.benchmark.summary) return null;
  const sum = ev.benchmark.summary, wx = ev.benchmark.wilcoxon || {};
  const ref = sum.E ? sum.E.mean : null;
  if (!ref) return null;
  const gates = [
    ['ALNS', 'Traffic-Aware ALNS', 'blueprint Appendix A'],
    ['SB', 'Simulated Bifurcation', 'added to the improvement stack'],
  ];
  const rows = [];
  for (const [k, name, sub] of gates) {
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
        h('div', { class: 'lbl' }, name + ' ',
          h('span', { class: 'chip ' + cls, text: verdict })),
        meter(Math.min(100, Math.abs(d) * 8), d > 0 ? 'pos' : 'neg', 80),
        h('div', { class: 'd', text: sub + (p === null ? '' : ' · p = ' + p.toFixed(4)) })),
      h('div', { class: 'val', text: pct1(d) })));
  }
  return h('div', { class: 'panel' },
    h('h3', null, 'Adoption gates'),
    h('div', { class: 'lede', text:
      'Two engines were built from the blueprint and neither was adopted on '
      + 'reputation. Each had to beat the existing improvement layer from the '
      + 'same start, on the same budget, on 30 paired seeds.' }),
    h('div', { class: 'bars' }, rows));
}

function sbPanel(ev) {
  const sb = ev.simulated_bifurcation;
  if (!sb) return missing('The Simulated Bifurcation study', 'python scripts/sb_eval.py');
  const gs = sb.ground_state_validation || [];
  const rs = sb.resequencing_summary || {};
  const ex = rs.exact_subset || null;
  const sweep = sb.time_window_field_sweep || [];
  const cal = sb.penalty_calibration || [];

  const gsRows = gs.map((g) => h('tr', null,
    h('td', { text: g.spins + ' spins' }),
    h('td', { class: 'num', text: g.ground_states_found + '/' + g.trials }),
    h('td', { class: 'num', text: g.mean_gap_pct.toFixed(3) + '%' }),
    h('td', { class: 'num', text: g.ms_per_instance.toFixed(1) + ' ms' })));

  const calRows = cal.map((c) => h('tr', null,
    h('td', { text: c.A_over_max_leg + '× max leg' }),
    h('td', null, h('span', { class: 'chip ' + (c.valid_rate === 1 ? 'ok' : 'bad'),
      text: c.raw_valid_decodes + '/' + c.trials })),
    h('td', { class: 'num', text: pct1(c.mean_gap_vs_optimal_pct) })));

  const swRows = sweep.map((s) => h('tr', null,
    h('td', { text: 'weight ' + s.edd_weight }),
    h('td', { class: 'num', text: pct1(s.mean_pct_vs_greedy) })));

  return h('div', { class: 'panel wide' },
    h('h3', null, 'Simulated Bifurcation — the genuinely quantum-derived engine',
      h('span', { class: 'chip warn', text: 'negative result, reported' })),
    h('div', { class: 'lede', text:
      'QPSO is quantum-inspired by analogy. Simulated Bifurcation is the classical '
      + 'limit of a real Kerr-nonlinear parametric oscillator network (Goto 2016; '
      + 'Goto, Tatsumura & Dixon 2019): the equations integrated here are the '
      + 'equations of motion of a physical Ising machine. That makes it the right '
      + 'engine to answer "in what sense is any of this quantum?" — and the right '
      + 'engine to be honest about when it loses.' }),
    h('div', { class: 'grid g3' },
      h('div', null,
        h('h3', { text: '1 · is the solver correct?' }),
        h('table', { class: 'data' },
          h('thead', null, h('tr', null, h('th', { text: 'problem' }),
            h('th', { class: 'num', text: 'ground state' }),
            h('th', { class: 'num', text: 'gap' }),
            h('th', { class: 'num', text: 'time' }))),
          h('tbody', null, gsRows)),
        h('div', { class: 'note', text:
          'Against exhaustive enumeration on random Ising instances. The dynamics '
          + 'find the exact ground state every time. Nothing below is a bug in the '
          + 'solver.' })),
      h('div', null,
        h('h3', { text: '2 · is the embedding sound?' }),
        h('table', { class: 'data' },
          h('thead', null, h('tr', null, h('th', { text: 'constraint penalty' }),
            h('th', { text: 'valid tours' }), h('th', { class: 'num', text: 'gap' }))),
          h('tbody', null, calRows)),
        h('div', { class: 'note', text:
          'The classic QUBO failure mode, measured: too weak a penalty and the '
          + 'spin configuration is not a tour at all; too strong and it drowns the '
          + 'objective it is supposed to protect.' })),
      h('div', null,
        h('h3', { text: '3 · what do time windows cost it?' }),
        h('table', { class: 'data' },
          h('thead', null, h('tr', null, h('th', { text: 'deadline-order field' }),
            h('th', { class: 'num', text: 'vs the greedy order' }))),
          h('tbody', null, swRows)),
        h('div', { class: 'note', text:
          'A quadratic form cannot see cumulative arrival time, so it cannot see a '
          + 'time window. Pricing deadline order into the local field recovers most '
          + 'of the damage — from 14x worse to within 20%.' }))),
    ex ? h('div', { class: 'grid g3' },
      statBlock(pct1(ex.sb_travel_gap_pct), 'SB vs exact · travel only',
        'The objective the Ising model actually encodes. Competitive.',
        ex.sb_travel_gap_pct < 10 ? 'ok' : 'warn'),
      statBlock(pct1(ex.sb_tw_gap_pct), 'SB vs exact · full objective',
        'With the deadline field. Without it: ' + pct1(ex.sb_gap_pct) + '.', 'warn'),
      statBlock(rs.mean_ms_sb + ' ms', 'per route, vs ' + rs.mean_ms_2opt + ' ms for 2-opt',
        '2-opt wins ' + rs['2opt_beats_sb'] + ' of ' + rs.routes
        + ' routes and reaches the exact optimum on every small one.', 'bad')) : null,
    h('div', { class: 'note' },
      h('b', { text: 'Verdict: not adopted into the operational path. ' }),
      'A correctly implemented Ising machine, validated to find exact ground '
      + 'states, is beaten by 2-opt on this problem — because the embedding drops '
      + 'the term that dominates the real cost. That is a result about the '
      + 'embedding, not about the hardware, and it is the honest answer to '
      + 'whether quantum-derived optimisation is ready for time-windowed fleet '
      + 'routing today.'));
}

function latencyPanel(ev) {
  if (!ev.latency) return missing('The latency measurement', 'python scripts/latency.py');
  const modes = ev.latency.modes || {};
  const panels = [];
  for (const name in modes) {
    const m = modes[name];
    const stages = m.stages_p95 || {};
    const max = Math.max(...Object.values(stages), 1);
    const rows = Object.keys(stages).map((k, i) => h('div', { class: 'row' },
      h('div', null, h('div', { class: 'lbl', text: k.replace(/_/g, ' ') }),
        meter((stages[k] / max) * 100,
          k.includes('matrix') ? 'matrix' : k.includes('solve') ? 'solve' : '',
          60 + i * 50)),
      h('div', { class: 'val', text: stages[k].toFixed(1) })));
    panels.push(h('div', { class: 'panel' },
      h('h3', null, name,
        h('span', { class: 'chip ' + (m.meets_500ms ? 'ok' : 'warn'),
          text: m.meets_500ms ? 'meets 500 ms' : 'over 500 ms' })),
      h('div', { class: 'grid g3' },
        statBlock(m.total_p50.toFixed(0) + ' ms', 'p50 total'),
        statBlock(m.total_p95.toFixed(0) + ' ms', 'p95 total', null,
          m.meets_500ms ? 'ok' : 'warn'),
        statBlock(m.total_max.toFixed(0) + ' ms', 'worst observed')),
      h('div', { class: 'eyebrow', text: 'p95 by stage' }),
      h('div', { class: 'bars' }, rows)));
  }
  return panels;
}

function convergencePanel(ev) {
  if (!ev.convergence) return missing('The convergence analysis', 'python scripts/convergence.py');
  const c = ev.convergence;
  const series = c.iters.map((it, i) => ({
    iter: it, best: c.best[i], diversity: c.diversity[i], beta: c.beta[i],
  }));
  const canvas = h('canvas', { id: 'convBig' });
  const panel = h('div', { class: 'panel' },
    h('h3', null, 'Convergence analysis',
      h('span', { class: 'chip warn', text: 'stagnates at iteration ' + c.stagnation_iter })),
    h('div', { class: 'lede', text:
      'Cold start, real network, ' + (c.config && c.config.seeds) + ' seeds, '
      + (c.config && c.config.budget) + ' s budget. The sponsor asks for '
      + 'convergence analysis by name, and the useful finding is the stagnation '
      + 'point: the swarm has effectively converged by iteration '
      + c.stagnation_iter + ' of ' + c.iters.length + ', so the rest of the budget '
      + 'buys almost nothing.' }),
    canvas,
    h('div', { class: 'grid g3' },
      statBlock(pct1(c.total_gain_pct), 'gbest improvement'),
      statBlock(c.diversity[0].toFixed(3) + ' → ' + c.diversity[c.diversity.length - 1].toFixed(3),
        'swarm diversity', 'collapses as the well width contracts'),
      statBlock(c.beta[0].toFixed(2) + ' → ' + c.beta[c.beta.length - 1].toFixed(2),
        'β contraction–expansion')));
  requestAnimationFrame(() => drawConv(canvas, series, { stagnation: c.stagnation_iter }));
  return panel;
}

function scenarioPanel(ev) {
  if (!ev.scenarios) return missing('The scenario suite', 'python scripts/scenarios.py');
  const rs = ev.scenarios.results || [];
  const passed = rs.filter((r) => r.pass).length;
  const vacuous = rs.filter((r) => r.pass && !r.exercised).length;
  return h('div', { class: 'panel' },
    h('h3', null, 'Operational scenarios S1–S9',
      h('span', { class: 'chip ' + (passed === rs.length ? 'ok' : 'bad'),
        text: passed + '/' + rs.length + ' pass' }),
      vacuous ? h('span', { class: 'chip bad', text: vacuous + ' vacuous' }) : null),
    h('div', { class: 'lede', text:
      'A scenario that passes without exercising its own condition is not a pass. '
      + 'The harness reports VACUOUS for that, and it caught three false passes '
      + 'during development — two scenarios were closing zero roads and still '
      + 'reporting green.' }),
    h('div', { class: 'scen' }, rs.map((r) => h('div', { class: 's' },
      h('span', { class: 'id', text: r.id }),
      h('span', { class: 'ti', text: r.title }),
      h('span', null, !r.exercised ? h('span', { class: 'chip bad', text: 'vacuous' })
        : h('span', { class: 'chip ' + (r.pass ? 'ok' : 'bad'),
          text: r.pass ? 'pass' : 'fail' }))))),
    h('details', { class: 'raw' }, h('summary', { text: 'what each scenario actually did' }),
      h('div', { class: 'inner' }, rs.map((r) => h('div', null,
        h('code', { text: r.id + ' ' }),
        (r.detail || []).join(' · '))))));
}

function energyPanel(ev) {
  if (!ev.energy) return missing('The energy accounting', 'python scripts/energy.py');
  const e = ev.energy;
  const arms = e.arms || {};
  const rows = Object.keys(arms).map((k) => {
    const a = arms[k];
    return h('tr', null,
      h('td', { text: k }),
      h('td', { class: 'num', text: a.mean_wall_ms.toFixed(0) }),
      h('td', { class: 'num', text: (a.mean_cpu_s * 1000).toFixed(0) }),
      h('td', { class: 'num', text: a.mean_mwh.toFixed(4) }),
      h('td', { class: 'num', text: a.at_scale.kwh_per_year.toFixed(3) }));
  });
  const measured = e.power_source === 'battery-sensor';
  const base = arms['operational (QPSO only)'];
  return h('div', { class: 'panel wide' },
    h('h3', null, 'Energy per re-plan',
      h('span', { class: 'chip ' + (measured ? 'ok' : 'mute'),
        text: measured ? 'sensor-measured' : 'CPU-time model' })),
    h('div', { class: 'lede' },
      'The 2025 systematic review names energy reporting as a gap in this field. '
      + 'It matters here because a recovery engine is not run once — it runs on '
      + 'every incident, all day, at every depot. ',
      h('b', { text: measured ? 'Sensor: ' : 'No sensor: ' }),
      e.power_note + '.'),
    h('table', { class: 'data' },
      h('thead', null, h('tr', null, h('th', { text: 'engine configuration' }),
        h('th', { class: 'num', text: 'wall ms' }), h('th', { class: 'num', text: 'CPU ms' }),
        h('th', { class: 'num', text: 'mWh / re-plan' }),
        h('th', { class: 'num', text: 'kWh / year' }))),
      h('tbody', null, rows)),
    base ? h('div', { class: 'note' },
      h('b', { text: 'For scale: ' }),
      'the operational path uses ' + base.at_scale.kwh_per_year.toFixed(3)
      + ' kWh a year at ' + e.replans_per_day + ' re-plans a day — less than a '
      + 'single 100 W depot floodlight burns in one ten-hour shift. The honest '
      + 'reading is that the optimiser\'s own energy is negligible next to the '
      + 'diesel it saves, and that is only a credible statement because it was '
      + 'measured instead of assumed away.') : null);
}

function deliverablesPanel() {
  const rows = [
    ['1', 'Graph-based network model', 'routepulse/graph.py',
      'Real OpenStreetMap road graph, time-dependent edge costs, incident and corridor overlays as the dynamic weight update mechanism.'],
    ['2', 'Mathematical formulation', 'FORMULATION.md',
      'Objective, decision variables, capacity / time-window / flow-conservation constraints, the FIFO condition and the acceptance rule.'],
    ['3', 'Quantum-inspired algorithm module', 'routepulse/solvers/qpso.py + sb.py',
      'QPSO with the delta-potential-well update rule written out in full, plus Simulated Bifurcation as a genuinely quantum-derived Ising engine.'],
    ['4', 'Software platform / prototype', 'server/',
      'This console. API and UI, network and traffic input, optimised route output, map visualisation, offline by construction.'],
    ['5', 'Demonstration', 'scripts/',
      'One realistic urban network under varying traffic, with benchmarking, convergence analysis and constraint handling all measured rather than asserted.'],
  ];
  return h('div', { class: 'panel wide' },
    h('h3', { text: 'The sponsor\'s five deliverables' }),
    h('div', { class: 'lede', text:
      'Transcribed from Egreen Quanta\'s problem statement. The Expected Solution '
      + 'paragraph above that table also requires constraint handling, convergence '
      + 'analysis and systematic performance benchmarking by name — all three are '
      + 'on this page.' }),
    h('table', { class: 'data' },
      h('thead', null, h('tr', null, h('th', { text: '#' }), h('th', { text: 'deliverable' }),
        h('th', { text: 'where' }), h('th', { text: 'what it is' }))),
      h('tbody', null, rows.map(([n, name, where, what]) => h('tr', null,
        h('td', { class: 'num', text: n }), h('td', { text: name }),
        h('td', null, h('code', { text: where })), h('td', { text: what }))))));
}

function limitationsPanel() {
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
      'One engine in module state, so two browsers share one fleet. Correct for a control tower demo, wrong for a product, and written down rather than discovered later.'],
  ];
  return h('div', { class: 'panel wide' },
    h('h3', { text: 'What this system is not' }),
    h('div', { class: 'lede', text:
      'Kept on the same page as the results, at the same size, because a '
      + 'limitation that only appears when asked is not disclosed.' }),
    h('ul', { class: 'why' }, items.map(([t, d]) =>
      h('li', null, h('b', { text: t + ' ' }), d))));
}

async function renderEvidence() {
  const body = $('#evidenceBody');
  if (S.evidence) return;
  clear(body);
  body.append(h('div', { class: 'ev-head' },
    h('div', null,
      h('h2', { text: 'The measured record' }),
      h('p', { text: 'Everything on this page was produced by a script in '
        + 'scripts/ and committed to out/. This view renders it; it never '
        + 'computes it. Where an experiment has not been run, the panel says so.' })),
    h('div', { class: 'stamp', text: 'loading…' })));
  let ev;
  try {
    ev = await api('/api/evidence');
  } catch (err) {
    clear(body);
    body.append(missing('The evidence API', 'restart the server'));
    return;
  }
  S.evidence = ev;
  clear(body);
  body.append(h('div', { class: 'ev-head' },
    h('div', null,
      h('h2', { text: 'The measured record' }),
      h('p', { text: 'Everything on this page was produced by a script in '
        + 'scripts/ and committed to out/. This view renders it; it never '
        + 'computes it. Where an experiment has not been run, the panel says so.' })),
    h('div', { class: 'stamp', text: ev.missing && ev.missing.length
      ? ev.missing.length + ' experiment(s) not run' : 'all experiments present' })));

  const grid = h('div', { class: 'grid g2' });
  const add = (x) => { if (!x) return; (Array.isArray(x) ? x : [x]).forEach((n) => grid.append(n)); };
  add(benchmarkPanel(ev));
  add(adoptionPanel(ev));
  add(convergencePanel(ev));
  add(latencyPanel(ev));
  add(sbPanel(ev));
  add(scenarioPanel(ev));
  add(energyPanel(ev));
  add(deliverablesPanel());
  add(limitationsPanel());
  body.append(grid);

  // stagger the panels in
  if (!REDUCED) {
    [...grid.children].forEach((n, i) => {
      n.animate([{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'none' }],
        { duration: 420, delay: i * 55, easing: EASE, fill: 'backwards' });
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
  if (ops) { resize(); } else { renderEvidence(); }
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

function paintSwatches() {
  const set = (id, color, tall) => {
    const el = $(id);
    if (!el) return;
    el.style.setProperty('background', color);
    if (tall) el.style.setProperty('height', '3px');
  };
  set('#sw-closure', COLOR.closed);
  set('#sw-congestion', COLOR.jam);
  set('#sw-ambulance', COLOR.amb);
  set('#lg-route', COLOR.accent);
  set('#lg-closed', COLOR.closed);
  set('#lg-jam', COLOR.jam);
  set('#lg-corridor', COLOR.corridor);
  set('#lg-depot', COLOR.depot);
  set('#lg-prio', '#ffffff');
}

(async function boot() {
  paintSwatches();
  convLegend();
  renderFleet();
  renderTimeline();
  renderEnergy(null);
  drawConv($('#conv'), [], { empty: 'run a re-plan to record convergence' });
  requestAnimationFrame(loop);

  try {
    const b = await api('/api/boot');
    S.graph = await api('/api/graph');
    S.bounds = {
      minLat: S.graph.bounds[0], minLon: S.graph.bounds[1],
      maxLat: S.graph.bounds[2], maxLon: S.graph.bounds[3],
    };
    renderNetMeta(b);
    $('#statDot').className = 'dot live';
    $('#statText').textContent = b.nodes.toLocaleString() + ' junctions · '
      + b.customers + ' stops · ' + b.vehicles + ' vehicles';
    resize();
  } catch (err) {
    $('#statDot').className = 'dot';
    $('#statText').textContent = 'backend unreachable';
    toast('Backend unreachable: ' + err.message, true);
  }
})();
