// Hex strategy map renderer: terrain, territories (topics), units (agents),
// A2A message projectiles, particles and a minimap. Pure canvas, no deps.

export const HEX = 34;
const SQ3 = Math.sqrt(3);
const DIRS = [[1, 0], [1, -1], [0, -1], [-1, 0], [-1, 1], [0, 1]];
const CORNERS = [...Array(6)].map((_, i) => {
  const a = (Math.PI / 180) * (60 * i - 30);
  return [HEX * Math.cos(a), HEX * Math.sin(a)];
});
const EMOJI_FONT = '"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif';

export const ACTIVE = new Set(['working', 'thinking', 'delegating', 'waiting']);
export const STATUS_COLORS = {
  idle: '#8a93a6', working: '#4fc3f7', thinking: '#b388ff', delegating: '#ffb74d',
  waiting: '#ffd54f', done: '#66bb6a', failed: '#ef5350', canceled: '#78909c', monitoring: '#26c6da',
};
const ROLE_STYLE = {
  commander: { base: '#f5c542', scale: 0.74 },
  orchestrator: { base: '#3f7fd0', scale: 0.62 },
  worker: { base: '#2f9a78', scale: 0.52 },
  swarm_leader: { base: '#d98e22', scale: 0.58 },
  swarm_member: { base: '#cdb32e', scale: 0.4 },
  ally: { base: '#8e7cc3', scale: 0.58 },
  ops_lead: { base: '#1f8f9e', scale: 0.62 },
  monitor: { base: '#26a69a', scale: 0.52 },
};
const TOPIC_ICON = { active: '⚔️', completed: '🏆', failed: '💀', canceled: '🏳️', interrupted: '⏸️' };

export const hexToPixel = (q, r) => ({ x: HEX * SQ3 * (q + r / 2), y: HEX * 1.5 * r });
const key = (q, r) => `${q},${r}`;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
const easeOutBack = (t) => 1 + 2.7 * Math.pow(t - 1, 3) + 1.7 * Math.pow(t - 1, 2);

export function rgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
function strHash(s) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) | 0; return (h >>> 0) / 4294967295; }
function hash(q, r) {
  let h = (q * 374761393 + r * 668265263) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967295;
}
function vnoise(x, y) {
  const xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi;
  const s = (t) => t * t * (3 - 2 * t);
  const a = hash(xi, yi), b = hash(xi + 1, yi), c = hash(xi, yi + 1), d = hash(xi + 1, yi + 1);
  const u = s(xf), v = s(yf);
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
}
// unexplored wilderness between regions (drawn under a fog layer)
const TERRAIN = [
  { max: 0.31, fills: ['#0c2238', '#0e253c', '#0b2035'], deco: 'wave' },
  { max: 0.38, fills: ['#13324c', '#153550', '#122f48'], deco: null },
  { max: 0.61, fills: ['#1b3627', '#1d3a2a', '#1a3325'], deco: null },
  { max: 0.81, fills: ['#152c1d', '#173020', '#14291b'], deco: 'tree' },
  { max: 2.00, fills: ['#272b35', '#2a2f39', '#252832'], deco: 'peak' },
];

// Topic-region biomes (keys match runtime/regions.py). decos = [emoji, probability per tile]
export const BIOMES = {
  capital: { fills: ['#4a5a2e', '#506232', '#45552b'], accent: '#f5c542', decos: [['🌳', 0.08], ['🌾', 0.06], ['🏡', 0.025]] },
  meadow: { fills: ['#4f8a34', '#589439', '#4a8231'], accent: '#ffd54f', decos: [['🌻', 0.3], ['🌼', 0.1], ['🌾', 0.06]] },
  desert: { fills: ['#b08850', '#ba9258', '#a7804a'], accent: '#ffcc80', decos: [['🌵', 0.13], ['🪨', 0.04], ['🐪', 0.012], ['🦂', 0.008]] },
  crystal: { fills: ['#4b3672', '#543d7d', '#45316a'], accent: '#ea80fc', decos: [['💎', 0.08], ['🔮', 0.03], ['✨', 0.06]] },
  metropolis: { fills: ['#3a4458', '#414c62', '#36404f'], accent: '#80d8ff', decos: [['🏢', 0.14], ['🏬', 0.06], ['🏭', 0.03], ['🏗️', 0.02]] },
  forest: { fills: ['#2e6a3a', '#337240', '#2a6235'], accent: '#a5d6a7', decos: [['🌲', 0.38], ['🌳', 0.17], ['🍄', 0.03], ['🦌', 0.008]] },
  volcanic: { fills: ['#4d2620', '#572a23', '#45221c'], accent: '#ff7043', decos: [['🌋', 0.03], ['🔥', 0.06], ['🪨', 0.08]] },
  tundra: { fills: ['#b9cbd8', '#c3d4e0', '#afc2d0'], accent: '#e1f5fe', decos: [['❄️', 0.07], ['🌲', 0.08], ['🧊', 0.02], ['⛄', 0.008]] },
  swamp: { fills: ['#3d4d2c', '#445532', '#384627'], accent: '#c5e1a5', decos: [['🌿', 0.18], ['🍄', 0.07], ['🪵', 0.03], ['🐸', 0.02]] },
  tropical: { fills: ['#3f8f72', '#46997a', '#3a866a'], accent: '#64ffda', decos: [['🌴', 0.22], ['🌺', 0.05], ['🐚', 0.02], ['🦜', 0.01]] },
  autumn: { fills: ['#8a4f24', '#955628', '#7f4920'], accent: '#ffab40', decos: [['🍁', 0.18], ['🍂', 0.14], ['🌳', 0.08], ['🎃', 0.008]] },
  highlands: { fills: ['#5d6370', '#666c79', '#565c68'], accent: '#cfd8dc', decos: [['⛰️', 0.2], ['🏔️', 0.06], ['🐐', 0.01]] },
};
const LAND_R = 6; // region land reaches ~TERRITORY_RADIUS + 3 tiles around each of its territories
const MIN_Z = 0.08; // strategic zoom: below STRATEGIC_Z only charted land is drawn (cheap, overview of all regions)
const STRATEGIC_Z = 0.2;
const MAX_DETAIL_CELLS = 3500; // above this many visible hexes the map also switches to the strategic view
const hexDist = (aq, ar, bq, br) => (Math.abs(aq - bq) + Math.abs(ar - br) + Math.abs(aq + ar - bq - br)) / 2;
const easeInOut = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

const sprites = new Map(); // emoji → pre-rendered canvas (drawImage is far cheaper than fillText)
function sprite(emoji) {
  let c = sprites.get(emoji);
  if (!c) {
    c = document.createElement('canvas');
    c.width = c.height = 64;
    const x = c.getContext('2d');
    x.font = `50px ${EMOJI_FONT}`;
    x.textAlign = 'center';
    x.textBaseline = 'middle';
    x.fillText(emoji, 32, 35);
    sprites.set(emoji, c);
  }
  return c;
}
/** Emoji at font size ``size`` centred on (x, y) – color-emoji fillText costs ~1–3 ms each on macOS. */
function drawEmoji(ctx, emoji, x, y, size) {
  const s = (size * 64) / 50;
  ctx.drawImage(sprite(emoji), x - s / 2, y - (s * 35) / 64, s, s);
}
function pickDeco(biome, q, r) {
  let h = hash(q * 7 + 11, r * 3 - 7), acc = 0;
  for (const [emoji, p] of biome.decos) { acc += p; if (h < acc) return emoji; }
  return null;
}
const placed = (t) => t && t.q !== null && t.q !== undefined;
const FOG_CELL = Object.freeze({ fill: '#0a0f1c', deco: null, emoji: null, region: null }); // not yet computed (frame budget)

export class HexMap {
  constructor(canvas, minimap, store, { onSelect, onHover, isCovered, onBanner } = {}) {
    this.onBanner = onBanner || (() => {});
    this.isCovered = isCovered || (() => false); // a full-screen panel hides the map → don't paint it
    this.lastDraw = 0;
    this.dirty = true;
    this.c = canvas;
    this.ctx = canvas.getContext('2d');
    this.mm = minimap;
    this.mmCtx = minimap?.getContext('2d');
    this.store = store;
    this.onSelect = onSelect || (() => {});
    this.onHover = onHover || (() => {});
    this.cam = { x: 0, y: 0, z: 0.9 };
    this.tgt = { ...this.cam };
    this.terrain = new Map();
    this.territories = new Map();
    this.anchors = []; // [{q, r, region}] – territory/region centers that define region land
    this.anchorSig = '';
    this.revealing = new Map(); // regionId → t0 of the "fog lifting" animation
    this.anim = new Map();
    this.projectiles = [];
    this.particles = [];
    this.floaters = [];
    this.ripples = [];
    this.labelJobs = [];
    this.bannerRects = [];
    this.hoverId = null;
    this.selectedId = null;
    this.last = performance.now();
    this.frameNo = 0;
    this.resize();
    new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
    this.bindInput();
    for (const ev of ['pointermove', 'pointerdown', 'pointerup', 'wheel', 'pointerleave']) {
      canvas.addEventListener(ev, () => { this.dirty = true; }, { passive: true });
    }
    requestAnimationFrame((t) => this.frame(t));
  }

  /** Something on the map moves → paint every frame; otherwise ambient animation at ~15 fps. */
  busy() {
    const c = this.cam, t = this.tgt;
    if (Math.abs(t.x - c.x) > 0.3 || Math.abs(t.y - c.y) > 0.3 || Math.abs(t.z - c.z) > 0.002) return true;
    if (this.projectiles.length || this.particles.length || this.floaters.length || this.ripples.length || this.revealing.size) return true;
    for (const a of this.store.agents.values()) if (ACTIVE.has(a.status)) return true;
    return false;
  }

  // ------------------------------------------------------------ camera
  resize() {
    const r = this.c.parentElement.getBoundingClientRect();
    // pixel budget (~4.5 MP): a full Retina backing store makes every map frame expensive to raster,
    // which shows up as presentation delay on clicks elsewhere (INP)
    const budget = Math.sqrt(4.5e6 / Math.max(1, r.width * r.height));
    this.dpr = Math.max(1, Math.min(window.devicePixelRatio || 1, budget));
    this.w = r.width; this.h = r.height;
    this.c.width = Math.round(r.width * this.dpr);
    this.c.height = Math.round(r.height * this.dpr);
    const g = this.ctx.createRadialGradient(this.w / 2, this.h / 2, Math.min(this.w, this.h) * 0.35,
      this.w / 2, this.h / 2, Math.max(this.w, this.h) * 0.75);
    g.addColorStop(0, 'rgba(0,0,0,0)');
    g.addColorStop(1, 'rgba(2,4,10,0.75)');
    this.vignette = g;
  }
  toScreen(x, y) { return { x: (x - this.cam.x) * this.cam.z + this.w / 2, y: (y - this.cam.y) * this.cam.z + this.h / 2 }; }
  toWorld(sx, sy) { return { x: (sx - this.w / 2) / this.cam.z + this.cam.x, y: (sy - this.h / 2) / this.cam.z + this.cam.y }; }
  flyTo(x, y, z) { this.tgt.x = x; this.tgt.y = y; if (z) this.tgt.z = clamp(z, MIN_Z, 2.4); }
  zoomBy(f, sx = this.w / 2, sy = this.h / 2) {
    const before = this.toWorld(sx, sy);
    const nz = clamp(this.tgt.z * f, MIN_Z, 2.4);
    this.tgt.z = nz; this.cam.z = nz;
    const after = this.toWorld(sx, sy);
    this.cam.x += before.x - after.x; this.cam.y += before.y - after.y;
    this.tgt.x = this.cam.x; this.tgt.y = this.cam.y;
  }
  bounds(filter = () => true) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const R = (LAND_R + 1) * HEX * 1.75;
    for (const a of this.anchors) {
      if (!filter(a)) continue;
      const p = hexToPixel(a.q, a.r);
      x0 = Math.min(x0, p.x - R); x1 = Math.max(x1, p.x + R);
      y0 = Math.min(y0, p.y - R - 40); y1 = Math.max(y1, p.y + R);
    }
    if (x0 === Infinity) return { x0: -400, y0: -300, x1: 400, y1: 300 };
    return { x0, y0, x1, y1 };
  }
  fitBounds(b) {
    const z = Math.min(this.w / (b.x1 - b.x0), this.h / (b.y1 - b.y0));
    this.flyTo((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2, clamp(z, MIN_Z, 1.3));
  }
  fit() { this.fitBounds(this.bounds()); }
  focusRegion(id) { this.fitBounds(this.bounds((a) => a.region.id === id)); }
  focusTopic(id) {
    const t = this.store.topics.get(id);
    if (!placed(t)) return;
    const p = hexToPixel(t.q, t.r);
    const z = Math.min(this.w, this.h) / (HEX * 2 * (t.radius + 2.2) * 1.75);
    this.flyTo(p.x, p.y - HEX * 0.6, clamp(z, 0.4, 1.6));
  }
  focusAgent(id) {
    const a = this.store.agents.get(id);
    if (!a) return;
    const p = hexToPixel(a.q, a.r);
    this.flyTo(p.x, p.y, Math.max(this.tgt.z, 1.15));
  }

  // ------------------------------------------------------------- input
  bindInput() {
    const c = this.c;
    let drag = null;
    c.addEventListener('pointerdown', (e) => {
      drag = { x: e.clientX, y: e.clientY, moved: 0 };
      c.setPointerCapture(e.pointerId);
    });
    c.addEventListener('pointermove', (e) => {
      const r = c.getBoundingClientRect();
      const sx = e.clientX - r.left, sy = e.clientY - r.top;
      if (drag) {
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        drag.moved += Math.abs(dx) + Math.abs(dy);
        drag.x = e.clientX; drag.y = e.clientY;
        if (drag.moved > 4) {
          c.classList.add('dragging');
          this.cam.x -= dx / this.cam.z; this.cam.y -= dy / this.cam.z;
          this.tgt.x = this.cam.x; this.tgt.y = this.cam.y;
        }
        return;
      }
      const hit = this.hitAgent(sx, sy);
      const banner = hit ? null : this.hitBanner(sx, sy);
      c.classList.toggle('pointer', !!(hit || banner));
      if ((hit?.id || null) !== this.hoverId) {
        this.hoverId = hit?.id || null;
      }
      this.onHover(hit, sx, sy);
    });
    c.addEventListener('pointerup', (e) => {
      c.classList.remove('dragging');
      if (drag && drag.moved <= 4) {
        const r = c.getBoundingClientRect();
        const sx = e.clientX - r.left, sy = e.clientY - r.top;
        const hit = this.hitAgent(sx, sy);
        if (hit) { this.select(hit.id); this.onSelect(hit.id); }
        else {
          const banner = this.hitBanner(sx, sy);
          if (banner) { this.focusTopic(banner); this.onBanner(banner); }
          else { this.select(null); this.onSelect(null); }
        }
      }
      drag = null;
    });
    c.addEventListener('pointerleave', () => { this.hoverId = null; this.onHover(null); });
    c.addEventListener('dblclick', (e) => {
      const r = c.getBoundingClientRect();
      const w = this.toWorld(e.clientX - r.left, e.clientY - r.top);
      for (const t of this.store.topics.values()) {
        if (!placed(t)) continue;
        const p = hexToPixel(t.q, t.r);
        if (Math.hypot(p.x - w.x, p.y - w.y) < (t.radius + 1) * HEX * 1.6) { this.focusTopic(t.id); return; }
      }
    });
    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      this.zoomBy(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    if (this.mm) {
      this.mm.addEventListener('pointerdown', (e) => {
        const r = this.mm.getBoundingClientRect();
        const m = this.mmTransform;
        if (!m) return;
        const sx = (e.clientX - r.left) * (this.mm.width / r.width), sy = (e.clientY - r.top) * (this.mm.height / r.height);
        this.flyTo((sx - m.ox) / m.s, (sy - m.oy) / m.s);
      });
    }
  }
  select(id) { this.selectedId = id; this.dirty = true; }
  hitAgent(sx, sy) {
    const w = this.toWorld(sx, sy);
    let best = null, bd = Infinity;
    const now = performance.now();
    for (const a of this.store.agents.values()) {
      const p = this.unitPixel(a, now);
      const rr = HEX * (ROLE_STYLE[a.role]?.scale || 0.55) * 1.15;
      const d = Math.hypot(p.x - w.x, p.y - w.y);
      if (d < rr && d < bd) { best = a; bd = d; }
    }
    return best;
  }
  hitBanner(sx, sy) {
    for (const b of this.bannerRects) if (sx >= b.x && sx <= b.x + b.w && sy >= b.y && sy <= b.y + b.h) return b.id;
    return null;
  }

  // ------------------------------------------------------------ events
  onSpawn(a) {
    const now = performance.now();
    const parent = a.parent_id && this.store.agents.get(a.parent_id);
    const p = hexToPixel(a.q, a.r);
    const from = parent && parent.topic_id === a.topic_id ? hexToPixel(parent.q, parent.r) : p;
    this.anim.set(a.id, { born: now, fx: from.x, fy: from.y });
    const topic = this.store.topics.get(a.topic_id);
    const color = topic?.color || '#ffffff';
    setTimeout(() => {
      this.ripples.push({ x: p.x, y: p.y, t0: performance.now(), dur: 900, color, max: HEX * 1.4 });
      this.burst(p.x, p.y, color, 10, 1.4);
    }, 600);
    this.territories.delete(a.topic_id);
  }
  onAgentUpdate(prev, a) {
    if (!prev) return;
    if (prev.q !== a.q || prev.r !== a.r) { // veteran re-deployed → march across the map
      const now = performance.now();
      const from = this.unitPixel(prev, now), to = hexToPixel(a.q, a.r);
      const dist = Math.hypot(to.x - from.x, to.y - from.y);
      this.anim.set(a.id, { move: now, fx: from.x, fy: from.y, dur: clamp(dist * 2.2, 900, 3200) });
      this.burst(from.x, from.y, '#d7c7a0', 8, 1.2);
      this.territories.delete(prev.topic_id);
      this.territories.delete(a.topic_id);
      if (a.recruitments > (prev.recruitments || 0)) this.floatText(a, '🎖️ Veteran +25 XP', '#ffd54f', 600);
    }
    if (prev.status === a.status) return;
    const p = hexToPixel(a.q, a.r);
    if (a.status === 'done' && !a.persistent) {
      this.burst(p.x, p.y, STATUS_COLORS.done, 18, 2.2);
      this.burst(p.x, p.y, '#ffe08a', 8, 1.6);
      this.floatText(a, '+50 XP', '#ffe08a');
    } else if (a.status === 'failed') {
      this.burst(p.x, p.y, STATUS_COLORS.failed, 16, 1.6);
      const an = this.anim.get(a.id) || {};
      an.shake = performance.now();
      this.anim.set(a.id, an);
    } else if (a.status === 'canceled') {
      this.burst(p.x, p.y, '#9aa4b8', 10, 1.2);
    }
  }
  onTopicChanged(id) { this.territories.delete(id); this.refreshLand(); }
  /** Rebuild the anchors that define region land. Only the cells around added / removed / re-biomed anchors
   *  are dropped from the terrain cache – a full rebuild (~6k cells) in one frame caused 50-400 ms frames (INP). */
  refreshLand() {
    const anchors = [];
    for (const reg of this.store.regions.values()) anchors.push({ q: reg.q, r: reg.r, region: reg });
    for (const t of this.store.topics.values()) {
      const reg = placed(t) && this.store.regions.get(t.region_id);
      if (reg && (t.q !== reg.q || t.r !== reg.r)) anchors.push({ q: t.q, r: t.r, region: reg });
    }
    this.anchors = anchors;
    const sigs = new Set(anchors.map((a) => `${a.q},${a.r},${a.region.id},${a.region.biome}`));
    const old = this.anchorSigs || new Set();
    this.anchorSigs = sigs;
    const changed = [...sigs].filter((s) => !old.has(s)).concat([...old].filter((s) => !sigs.has(s)));
    if (!changed.length) return;
    if (!old.size || changed.length > 24) { this.terrain.clear(); return; }
    const R = LAND_R + 2; // landRegion() never looks further than this
    for (const s of changed) {
      const [aq, ar] = s.split(',').map(Number);
      for (let dq = -R; dq <= R; dq++) {
        for (let dr = Math.max(-R, -dq - R); dr <= Math.min(R, -dq + R); dr++) this.terrain.delete(key(aq + dq, ar + dr));
      }
    }
  }
  onRegionCreated(region) {
    this.refreshLand();
    if (region.kind !== 'domain') return;
    const now = performance.now();
    this.revealing.set(region.id, now);
    const p = hexToPixel(region.q, region.r);
    const accent = (BIOMES[region.biome] || BIOMES.meadow).accent;
    for (let i = 0; i < 3; i++) {
      this.ripples.push({ x: p.x, y: p.y, t0: now + i * 350, dur: 1800, color: accent, max: HEX * LAND_R * 2.2 });
    }
    this.burst(p.x, p.y, accent, 36, 4.5);
  }
  onTopicDone(topic) {
    if (!placed(topic)) return;
    const p = hexToPixel(topic.q, topic.r);
    const color = topic.status === 'completed' ? '#ffe08a' : topic.status === 'failed' ? '#ef5350' : '#9aa4b8';
    for (let i = 0; i < 3; i++) {
      setTimeout(() => {
        this.ripples.push({ x: p.x, y: p.y, t0: performance.now(), dur: 1400, color, max: HEX * (topic.radius + 1) * 1.8 });
        if (topic.status === 'completed') this.burst(p.x, p.y, i % 2 ? topic.color : '#ffe08a', 30, 4);
      }, i * 260);
    }
  }
  onMessage(ev) {
    const colors = { request: '#ffd54f', response: '#69f0ae', error: '#ff5252' };
    this.projectiles.push({
      from: ev.from, to: ev.to, kind: ev.kind, color: colors[ev.kind] || '#fff', t0: performance.now(),
    });
  }
  floatText(a, text, color, delay = 0) {
    const p = hexToPixel(a.q, a.r);
    this.floaters.push({ x: p.x, y: p.y - HEX * 0.6, text, color, t0: performance.now() + delay });
  }
  burst(x, y, color, n = 14, speed = 2) {
    for (let i = 0; i < n; i++) {
      const ang = Math.random() * Math.PI * 2, v = (0.4 + Math.random()) * speed;
      this.particles.push({ x, y, vx: Math.cos(ang) * v, vy: Math.sin(ang) * v - 0.6, life: 0, max: 0.7 + Math.random() * 0.7, color, size: 1.5 + Math.random() * 2.5 });
    }
  }

  // ---------------------------------------------------------- geometry
  agentPixel(id, now) {
    if (id === 'user') {
      const cmd = [...this.store.agents.values()].find((a) => a.role === 'commander');
      if (!cmd) return null;
      const p = hexToPixel(cmd.q, cmd.r);
      return { x: p.x, y: p.y - HEX * 6 };
    }
    const a = this.store.agents.get(id);
    return a ? this.unitPixel(a, now) : null;
  }
  unitPixel(a, now) {
    const p = hexToPixel(a.q, a.r);
    let x = p.x, y = p.y, s = 1;
    const an = this.anim.get(a.id);
    if (an && an.born) {
      const k = clamp((now - an.born) / 650, 0, 1);
      const e = easeOutCubic(k);
      x = an.fx + (p.x - an.fx) * e;
      y = an.fy + (p.y - an.fy) * e - Math.sin(k * Math.PI) * HEX * 0.7;
      s = 0.35 + 0.65 * easeOutBack(k);
    } else if (an && an.move) {
      const k = clamp((now - an.move) / an.dur, 0, 1);
      const e = easeInOut(k);
      x = an.fx + (p.x - an.fx) * e;
      y = an.fy + (p.y - an.fy) * e - Math.abs(Math.sin(k * Math.PI * 7)) * HEX * 0.16 * (1 - k); // marching hops
    }
    if (ACTIVE.has(a.status)) y += Math.sin(now / 240 + strHash(a.id) * 6.28) * 2.4;
    if (an?.shake && now - an.shake < 600) {
      const k = (now - an.shake) / 600;
      x += Math.sin(k * 40) * 4 * (1 - k);
    }
    return { x, y, s };
  }
  terrainAt(q, r) {
    const k = key(q, r);
    let t = this.terrain.get(k);
    if (!t) {
      // at most ~6 ms of new cells per frame (load, zoom-in, fly-to): the rest shows as fog and fills in over
      // the next frames instead of one 50-400 ms frame that blocks clicks (INP)
      if (!this.terrainOver && (++this.terrainMisses & 31) === 0 && performance.now() > this.terrainDeadline) this.terrainOver = true;
      if (this.terrainOver) { this.dirty = true; return FOG_CELL; }
      const j = Math.floor(hash(q * 7 + 3, r * 13 + 1) * 3);
      const region = this.landRegion(q, r);
      if (region) {
        const b = BIOMES[region.biome] || BIOMES.meadow;
        t = { fill: b.fills[j], emoji: pickDeco(b, q, r), region: region.id, deco: null };
      } else {
        const x = q + r / 2, y = r * 0.866;
        const n = 0.55 * vnoise(x / 7 + 100, y / 7 + 100) + 0.3 * vnoise(x / 3.1 + 50, y / 3.1 + 50) + 0.15 * hash(q, r);
        const band = TERRAIN.find((b) => n < b.max);
        t = { fill: band.fills[j], deco: band.deco && hash(q + 11, r - 7) > 0.35 ? band.deco : null, region: null };
      }
      this.terrain.set(k, t);
      if (this.terrain.size > 60000) this.terrain.clear();
    }
    return t;
  }
  /** Region owning a cell: nearest anchor (territory / region center) within a slightly ragged radius. */
  landRegion(q, r) {
    let best = null, bd = Infinity;
    for (const a of this.anchors) {
      if (Math.abs(a.q - q) > LAND_R + 2 || Math.abs(a.r - r) > LAND_R + 2) continue;
      const d = hexDist(q, r, a.q, a.r);
      if (d < bd) { bd = d; best = a.region; }
    }
    const coast = LAND_R + (hash(q * 3 + 17, r * 5 - 11) - 0.5) * 2.2; // organic coastline
    return bd <= coast ? best : null;
  }
  territory(topic) {
    const agents = [...this.store.agents.values()].filter((a) => a.topic_id === topic.id);
    const cacheKey = `${topic.status}|${agents.length}`;
    let t = this.territories.get(topic.id);
    if (t && t.key === cacheKey) return t;
    const cells = new Map();
    for (let dq = -topic.radius; dq <= topic.radius; dq++) {
      for (let dr = Math.max(-topic.radius, -dq - topic.radius); dr <= Math.min(topic.radius, -dq + topic.radius); dr++) {
        cells.set(key(topic.q + dq, topic.r + dr), [topic.q + dq, topic.r + dr]);
      }
    }
    for (const a of agents) cells.set(key(a.q, a.r), [a.q, a.r]);
    const edges = [];
    for (const [q, r] of cells.values()) {
      const c = hexToPixel(q, r);
      DIRS.forEach(([dq, dr], d) => {
        if (cells.has(key(q + dq, r + dr))) return;
        const i = (6 - d) % 6, j = (i + 1) % 6;
        edges.push([c.x + CORNERS[i][0], c.y + CORNERS[i][1], c.x + CORNERS[j][0], c.y + CORNERS[j][1]]);
      });
    }
    t = { key: cacheKey, cells: [...cells.values()], edges };
    this.territories.set(topic.id, t);
    return t;
  }

  // ------------------------------------------------------------- frame
  /** The UI next to the map needs the next frame(s) – skip map painting meanwhile (INP). */
  yieldTo(ms = 160) { this.pauseUntil = performance.now() + ms; }

  frame(ts) {
    requestAnimationFrame((t) => this.frame(t));
    // Painting a full-screen Retina canvas every frame delays clicks / typing in the panels (INP).
    if (this.isCovered() || ts < (this.pauseUntil || 0)) { this.last = ts; this.dirty = true; return; }
    if (!this.dirty && ts - this.lastDraw < 66 && !this.busy()) return;
    this.dirty = false;
    this.lastDraw = ts;
    this.terrainDeadline = performance.now() + 6;
    this.terrainMisses = 0;
    this.terrainOver = false;
    const now = ts;
    const dt = Math.min(0.05, (now - this.last) / 1000);
    this.last = now;
    this.frameNo++;
    const k = 1 - Math.exp(-dt * 6);
    this.cam.x += (this.tgt.x - this.cam.x) * k;
    this.cam.y += (this.tgt.y - this.cam.y) * k;
    this.cam.z += (this.tgt.z - this.cam.z) * k;

    const { ctx, dpr, w, h } = this;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#060a14';
    ctx.fillRect(0, 0, w, h);
    const z = this.cam.z;
    ctx.setTransform(dpr * z, 0, 0, dpr * z, dpr * (w / 2 - this.cam.x * z), dpr * (h / 2 - this.cam.y * z));

    this.drawTerrain(now);
    this.drawTerritories(now);
    this.drawLinks(now);
    this.drawRipples(now);
    this.drawUnits(now);
    this.drawProjectiles(now);
    this.drawParticles(dt);

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = this.vignette;
    ctx.fillRect(0, 0, w, h);
    this.drawRegionLabels(now);
    this.drawBanners(now);
    this.drawLabels(now);
    this.drawFloaters(now);
    if (this.frameNo % 3 === 0) this.drawMinimap();
  }

  /** Far zoom: skip the wilderness, draw only charted region land (≈130 cells per territory). */
  drawStrategicTerrain() {
    const { ctx } = this;
    const buckets = new Map();
    const seen = new Set();
    for (const a of this.anchors) {
      for (let dq = -LAND_R - 1; dq <= LAND_R + 1; dq++) {
        for (let dr = Math.max(-LAND_R - 1, -dq - LAND_R - 1); dr <= Math.min(LAND_R + 1, -dq + LAND_R + 1); dr++) {
          const q = a.q + dq, r = a.r + dr, k = key(q, r);
          if (seen.has(k)) continue;
          seen.add(k);
          const t = this.terrainAt(q, r);
          if (!t.region) continue;
          let b = buckets.get(t.fill);
          if (!b) { b = []; buckets.set(t.fill, b); }
          b.push(q, r);
        }
      }
    }
    for (const [fill, cells] of buckets) {
      ctx.beginPath();
      for (let i = 0; i < cells.length; i += 2) {
        const c = hexToPixel(cells[i], cells[i + 1]);
        ctx.moveTo(c.x + CORNERS[0][0], c.y + CORNERS[0][1]);
        for (let j = 1; j < 6; j++) ctx.lineTo(c.x + CORNERS[j][0], c.y + CORNERS[j][1]);
        ctx.closePath();
      }
      ctx.fillStyle = fill;
      ctx.fill();
    }
  }

  drawTerrain(now) {
    const { ctx } = this;
    const tl = this.toWorld(0, 0), br = this.toWorld(this.w, this.h);
    // strategic view by zoom *or* by visible cell count: on big / Retina windows the full wilderness at
    // z≈0.2-0.3 is ~8k hexes (30-140 ms per frame → INP); charted land alone is a fraction of that
    const visibleCells = ((br.x - tl.x) * (br.y - tl.y)) / (HEX * HEX * 1.5 * SQ3);
    if (this.cam.z < STRATEGIC_Z || visibleCells > MAX_DETAIL_CELLS) { this.drawStrategicTerrain(); return; }
    const r0 = Math.floor(tl.y / (HEX * 1.5)) - 1, r1 = Math.ceil(br.y / (HEX * 1.5)) + 1;
    const buckets = new Map();
    const decos = [];
    const emojis = [];
    const fog = [];
    const reveal = new Map(); // regionId → cells still under lifting fog
    const showDeco = this.cam.z > 0.55;
    const showEmoji = this.cam.z > 0.4;
    const occupied = new Set();
    for (const a of this.store.agents.values()) occupied.add(key(a.q, a.r));
    for (const [id, t0] of this.revealing) if (now - t0 > 2200) this.revealing.delete(id);
    for (let r = r0; r <= r1; r++) {
      const q0 = Math.floor(tl.x / (HEX * SQ3) - r / 2) - 1, q1 = Math.ceil(br.x / (HEX * SQ3) - r / 2) + 1;
      for (let q = q0; q <= q1; q++) {
        const t = this.terrainAt(q, r);
        let b = buckets.get(t.fill);
        if (!b) { b = []; buckets.set(t.fill, b); }
        b.push(q, r);
        if (!t.region) {
          fog.push(q, r);
          if (showDeco && t.deco) decos.push(t.deco, q, r);
        } else {
          if (showEmoji && t.emoji && !occupied.has(key(q, r))) emojis.push(t.emoji, q, r);
          if (this.revealing.has(t.region)) {
            let cells = reveal.get(t.region);
            if (!cells) { cells = []; reveal.set(t.region, cells); }
            cells.push(q, r);
          }
        }
      }
    }
    const hexPath = (path, q, r, s = 1) => {
      const c = hexToPixel(q, r);
      path.moveTo(c.x + CORNERS[0][0] * s, c.y + CORNERS[0][1] * s);
      for (let i = 1; i < 6; i++) path.lineTo(c.x + CORNERS[i][0] * s, c.y + CORNERS[i][1] * s);
      path.closePath();
    };
    for (const [fill, cells] of buckets) {
      ctx.beginPath();
      for (let i = 0; i < cells.length; i += 2) hexPath(ctx, cells[i], cells[i + 1], 0.97);
      ctx.fillStyle = fill;
      ctx.fill();
    }
    this.drawWildDecos(decos, now, showDeco);
    // fog of war over unexplored land – charted regions stand out
    ctx.beginPath();
    for (let i = 0; i < fog.length; i += 2) hexPath(ctx, fog[i], fog[i + 1], 1.02);
    ctx.fillStyle = 'rgba(3,5,12,0.5)';
    ctx.fill();
    // biome decorations (pre-rendered emoji sprites)
    for (let i = 0; i < emojis.length; i += 3) {
      const q = emojis[i + 1], r = emojis[i + 2];
      const c = hexToPixel(q, r);
      const s = HEX * (0.55 + hash(q - 3, r + 9) * 0.3);
      const jx = (hash(q, r + 5) - 0.5) * HEX * 0.6, jy = (hash(q + 5, r) - 0.5) * HEX * 0.45;
      ctx.drawImage(sprite(emojis[i]), c.x + jx - s / 2, c.y + jy - s / 2, s, s);
    }
    // newly charted region: fog lifts outward from its center
    for (const [id, cells] of reveal) {
      const reg = this.store.regions.get(id);
      if (!reg) continue;
      const k = (now - this.revealing.get(id)) / 2000;
      const front = easeOutCubic(clamp(k, 0, 1)) * (LAND_R + 3);
      ctx.beginPath();
      for (let i = 0; i < cells.length; i += 2) {
        if (hexDist(cells[i], cells[i + 1], reg.q, reg.r) > front) hexPath(ctx, cells[i], cells[i + 1], 1.02);
      }
      ctx.fillStyle = 'rgba(3,5,12,0.92)';
      ctx.fill();
    }
  }

  drawWildDecos(decos, now, showDeco) {
    const { ctx } = this;
    if (showDeco) {
      ctx.beginPath();
      const waves = [];
      for (let i = 0; i < decos.length; i += 3) {
        const [type, q, r] = [decos[i], decos[i + 1], decos[i + 2]];
        const c = hexToPixel(q, r);
        const jx = (hash(q, r + 5) - 0.5) * HEX * 0.5, jy = (hash(q + 5, r) - 0.5) * HEX * 0.4;
        if (type === 'tree') {
          for (const [ox, oy] of [[-8, 4], [6, -2], [0, 10]]) {
            const x = c.x + jx + ox, y = c.y + jy + oy;
            ctx.moveTo(x, y - 9); ctx.lineTo(x + 5, y + 2); ctx.lineTo(x - 5, y + 2); ctx.closePath();
          }
        } else if (type === 'peak') {
          const x = c.x + jx, y = c.y + jy + 6;
          ctx.moveTo(x - 13, y); ctx.lineTo(x - 3, y - 15); ctx.lineTo(x + 3, y - 7);
          ctx.lineTo(x + 7, y - 11); ctx.lineTo(x + 14, y); ctx.closePath();
        } else waves.push(c.x + jx, c.y + jy);
      }
      ctx.fillStyle = 'rgba(0,0,0,0.22)';
      ctx.fill();
      ctx.beginPath();
      const phase = now / 900;
      for (let i = 0; i < waves.length; i += 2) {
        const x = waves[i], y = waves[i + 1] + Math.sin(phase + x * 0.05) * 1.5;
        ctx.moveTo(x - 8, y); ctx.quadraticCurveTo(x - 4, y - 3, x, y); ctx.quadraticCurveTo(x + 4, y + 3, x + 8, y);
      }
      ctx.strokeStyle = 'rgba(120,180,255,0.12)';
      ctx.lineWidth = 1.2;
      ctx.stroke();
    }
  }

  drawTerritories(now) {
    const { ctx } = this;
    for (const topic of this.store.topics.values()) {
      if (!placed(topic)) continue; // still being surveyed by the HQ
      const t = this.territory(topic);
      const finished = topic.status !== 'active' && topic.status !== 'base';
      const tint = topic.status === 'failed' ? '#ef5350' : topic.status === 'canceled' ? '#78909c' : topic.color;
      ctx.beginPath();
      for (const [q, r] of t.cells) {
        const c = hexToPixel(q, r);
        ctx.moveTo(c.x + CORNERS[0][0], c.y + CORNERS[0][1]);
        for (let i = 1; i < 6; i++) ctx.lineTo(c.x + CORNERS[i][0], c.y + CORNERS[i][1]);
        ctx.closePath();
      }
      ctx.fillStyle = rgba(tint, finished ? 0.08 : 0.15);
      ctx.fill();
      ctx.strokeStyle = rgba(tint, 0.1);
      ctx.lineWidth = 1;
      ctx.stroke();

      const pulse = topic.status === 'active' ? 0.55 + 0.25 * Math.sin(now / 500) : finished ? 0.35 : 0.6;
      ctx.beginPath();
      for (const [x1, y1, x2, y2] of t.edges) { ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); }
      ctx.lineCap = 'round';
      ctx.strokeStyle = rgba(tint, pulse * 0.25);
      ctx.lineWidth = 9;
      ctx.stroke();
      ctx.strokeStyle = rgba(tint, pulse + 0.2);
      ctx.lineWidth = 2.5;
      ctx.stroke();
    }
  }

  drawLinks(now) {
    const { ctx } = this;
    const agents = this.store.agents;
    for (const a of agents.values()) {
      if (!a.parent_id) continue;
      const p = agents.get(a.parent_id);
      if (!p) continue;
      const A = this.unitPixel(p, now), B = this.unitPixel(a, now);
      const topic = this.store.topics.get(a.topic_id);
      const color = topic?.color || '#888888';
      const cross = p.topic_id !== a.topic_id;
      const active = ACTIVE.has(a.status);
      ctx.beginPath();
      ctx.moveTo(A.x, A.y);
      if (cross) {
        const d = Math.hypot(B.x - A.x, B.y - A.y);
        ctx.quadraticCurveTo((A.x + B.x) / 2, (A.y + B.y) / 2 - d * 0.18, B.x, B.y);
        ctx.strokeStyle = active ? 'rgba(245,197,66,0.45)' : 'rgba(245,197,66,0.16)';
        ctx.lineWidth = 3;
        ctx.setLineDash([12, 12]);
      } else {
        ctx.lineTo(B.x, B.y);
        ctx.strokeStyle = rgba(color, active ? 0.75 : 0.28);
        ctx.lineWidth = active ? 2.5 : 1.8;
        ctx.setLineDash(active ? [7, 6] : []);
      }
      ctx.lineDashOffset = active ? -now / 35 : 0;
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }

  hexOutline(q, r, scale = 1) {
    const { ctx } = this;
    const c = hexToPixel(q, r);
    ctx.beginPath();
    ctx.moveTo(c.x + CORNERS[0][0] * scale, c.y + CORNERS[0][1] * scale);
    for (let i = 1; i < 6; i++) ctx.lineTo(c.x + CORNERS[i][0] * scale, c.y + CORNERS[i][1] * scale);
    ctx.closePath();
  }

  drawUnits(now) {
    const { ctx } = this;
    this.labelJobs = [];
    const z = this.cam.z;
    const tl = this.toWorld(-80, -80), br = this.toWorld(this.w + 80, this.h + 80);
    const list = [...this.store.agents.values()].sort((a, b) => a.r - b.r || a.q - b.q);
    for (const a of list) {
      const p = this.unitPixel(a, now);
      if (p.x < tl.x || p.x > br.x || p.y < tl.y || p.y > br.y) continue;
      const topic = this.store.topics.get(a.topic_id);
      const tcolor = topic?.color || '#888888';
      const style = ROLE_STYLE[a.role] || ROLE_STYLE.worker;
      const sc = STATUS_COLORS[a.status] || '#8a93a6';
      const R = HEX * style.scale * p.s;
      const active = ACTIVE.has(a.status);
      const finished = a.status === 'done' || a.status === 'canceled';

      // occupied tile
      this.hexOutline(a.q, a.r, 0.94);
      ctx.fillStyle = rgba(tcolor, active ? 0.32 : 0.18);
      ctx.fill();
      if (a.id === this.selectedId) {
        ctx.setLineDash([8, 6]);
        ctx.lineDashOffset = -now / 30;
        ctx.strokeStyle = '#f5c542';
        ctx.lineWidth = 3;
        ctx.stroke();
        ctx.setLineDash([]);
      } else if (a.id === this.hoverId) {
        ctx.strokeStyle = 'rgba(255,255,255,0.55)';
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      // shadow
      ctx.beginPath();
      ctx.ellipse(p.x, hexToPixel(a.q, a.r).y + R * 0.85, R * 0.85, R * 0.28, 0, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(0,0,0,0.35)';
      ctx.fill();

      // aura for active units
      if (active) {
        const pulse = 0.5 + 0.5 * Math.sin(now / 300 + strHash(a.id) * 6);
        const g = ctx.createRadialGradient(p.x, p.y, R * 0.6, p.x, p.y, R * (1.7 + pulse * 0.3));
        g.addColorStop(0, rgba(sc, 0.35));
        g.addColorStop(1, rgba(sc, 0));
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(p.x, p.y, R * 2.1, 0, Math.PI * 2);
        ctx.fill();
      }
      if (a.status === 'delegating') {
        const k = (now % 1200) / 1200;
        ctx.beginPath();
        ctx.arc(p.x, p.y, R * (1 + k * 1.2), 0, Math.PI * 2);
        ctx.strokeStyle = rgba(sc, 0.6 * (1 - k));
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      // token body
      ctx.globalAlpha = finished ? 0.62 : 1;
      const g = ctx.createRadialGradient(p.x - R * 0.35, p.y - R * 0.4, R * 0.1, p.x, p.y, R);
      g.addColorStop(0, rgba(style.base, 1));
      g.addColorStop(0.65, rgba(style.base, 0.75));
      g.addColorStop(1, '#0b1020');
      ctx.beginPath();
      ctx.arc(p.x, p.y, R, 0, Math.PI * 2);
      ctx.fillStyle = g;
      ctx.fill();
      ctx.lineWidth = Math.max(2, R * 0.13);
      ctx.strokeStyle = tcolor;
      ctx.stroke();

      // status ring
      ctx.lineWidth = Math.max(2, R * 0.12);
      if (a.status === 'thinking') {
        const rot = now / 260;
        ctx.strokeStyle = sc;
        for (let i = 0; i < 3; i++) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, R + ctx.lineWidth * 1.4, rot + (i * Math.PI * 2) / 3, rot + (i * Math.PI * 2) / 3 + 1.2);
          ctx.stroke();
        }
      } else if (a.status === 'waiting') {
        const k = (now % 2000) / 2000;
        ctx.beginPath();
        ctx.strokeStyle = sc;
        ctx.arc(p.x, p.y, R + ctx.lineWidth * 1.4, -Math.PI / 2, -Math.PI / 2 + k * Math.PI * 2);
        ctx.stroke();
      } else if (a.status === 'monitoring') { // always-on listener: radar sweep + slow sonar ping
        const rr = R + ctx.lineWidth * 1.4;
        const rot = now / 900 + strHash(a.id) * 6.28;
        ctx.strokeStyle = rgba(sc, 0.35);
        ctx.beginPath();
        ctx.arc(p.x, p.y, rr, 0, Math.PI * 2);
        ctx.stroke();
        ctx.strokeStyle = sc;
        ctx.beginPath();
        ctx.arc(p.x, p.y, rr, rot, rot + 0.9);
        ctx.stroke();
        const k = ((now + strHash(a.id) * 3000) % 3000) / 3000;
        ctx.strokeStyle = rgba(sc, 0.45 * (1 - k));
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(p.x, p.y, rr + k * R * 1.3, 0, Math.PI * 2);
        ctx.stroke();
      } else if (a.status !== 'idle') {
        ctx.beginPath();
        ctx.strokeStyle = sc;
        ctx.arc(p.x, p.y, R + ctx.lineWidth * 1.4, 0, Math.PI * 2);
        ctx.stroke();
      }

      // icon
      drawEmoji(ctx, a.icon || '🤖', p.x, p.y + R * 0.06, R * 1.05);
      ctx.globalAlpha = 1;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';

      // level badge
      if (z > 0.45 && a.role !== 'swarm_member') {
        const bx = p.x + R * 0.78, by = p.y + R * 0.72, br2 = Math.max(7, R * 0.32);
        ctx.beginPath();
        ctx.arc(bx, by, br2, 0, Math.PI * 2);
        ctx.fillStyle = '#1a1305';
        ctx.fill();
        ctx.strokeStyle = '#f5c542';
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.fillStyle = '#ffe08a';
        ctx.font = `700 ${Math.round(br2 * 1.15)}px Inter, sans-serif`;
        ctx.fillText(String(a.level), bx, by + 0.5);
      }
      // status badge
      const badge = a.status === 'done' && !a.persistent ? '✅' : a.status === 'failed' ? '❌'
        : a.status === 'waiting' ? '⏳' : a.status === 'canceled' ? '🏳️' : null;
      if (badge) drawEmoji(ctx, badge, p.x + R * 0.8, p.y - R * 0.75, R * 0.55);
      // veteran medal (served ≥ 2 tasks – reusable unit with a service record)
      if (a.veteran && z > 0.45) {
        const s = Math.max(12, R * 0.62);
        ctx.drawImage(sprite('🎖️'), p.x - R * 0.95 - s / 2, p.y + R * 0.7 - s / 2, s, s);
      }
      this.labelJobs.push({ a, x: p.x, y: p.y, R });
    }
  }

  drawProjectiles(now) {
    const { ctx } = this;
    const keep = [];
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    for (const pr of this.projectiles) {
      const A = this.agentPixel(pr.from, now), B = this.agentPixel(pr.to, now);
      if (!A || !B) continue;
      const dist = Math.hypot(B.x - A.x, B.y - A.y);
      pr.dur = pr.dur || clamp(dist / 650, 0.5, 1.6) * 1000;
      const u = (now - pr.t0) / pr.dur;
      if (u >= 1) {
        this.ripples.push({ x: B.x, y: B.y, t0: now, dur: 500, color: pr.color, max: HEX * 0.9 });
        if (pr.kind !== 'request') this.burst(B.x, B.y, pr.color, 6, 1);
        continue;
      }
      keep.push(pr);
      if (u < 0) continue;
      const lift = Math.min(dist * 0.3, 160) + 20;
      const cx = (A.x + B.x) / 2, cy = (A.y + B.y) / 2 - lift;
      const at = (t) => {
        const it = 1 - t;
        return [it * it * A.x + 2 * it * t * cx + t * t * B.x, it * it * A.y + 2 * it * t * cy + t * t * B.y];
      };
      for (let i = 9; i >= 0; i--) {
        const t = Math.max(0, u - i * 0.018);
        const [x, y] = at(t);
        ctx.beginPath();
        ctx.arc(x, y, 5 * (1 - i / 11), 0, Math.PI * 2);
        ctx.fillStyle = rgba(pr.color, 0.5 * (1 - i / 10));
        ctx.fill();
      }
      const [hx, hy] = at(u);
      const g = ctx.createRadialGradient(hx, hy, 0, hx, hy, 14);
      g.addColorStop(0, rgba(pr.color, 0.95));
      g.addColorStop(1, rgba(pr.color, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(hx, hy, 14, 0, Math.PI * 2);
      ctx.fill();
      if (this.cam.z > 0.6) {
        ctx.globalCompositeOperation = 'source-over';
        drawEmoji(ctx, pr.kind === 'request' ? '📜' : pr.kind === 'error' ? '💥' : '✉️', hx, hy - 1, 16);
        ctx.globalCompositeOperation = 'lighter';
      }
    }
    ctx.restore();
    this.projectiles = keep;
  }

  drawRipples(now) {
    const { ctx } = this;
    this.ripples = this.ripples.filter((rp) => {
      const k = (now - rp.t0) / rp.dur;
      if (k >= 1) return false;
      if (k < 0) return true;
      ctx.beginPath();
      ctx.arc(rp.x, rp.y, rp.max * easeOutCubic(k), 0, Math.PI * 2);
      ctx.strokeStyle = rgba(rp.color, 0.8 * (1 - k));
      ctx.lineWidth = 3 * (1 - k) + 0.5;
      ctx.stroke();
      return true;
    });
  }

  drawParticles(dt) {
    const { ctx } = this;
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    this.particles = this.particles.filter((p) => {
      p.life += dt;
      if (p.life >= p.max) return false;
      p.x += p.vx * dt * 60; p.y += p.vy * dt * 60; p.vy += 0.05 * dt * 60; p.vx *= 0.98;
      const k = 1 - p.life / p.max;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size * k + 0.3, 0, Math.PI * 2);
      ctx.fillStyle = rgba(p.color, k);
      ctx.fill();
      return true;
    });
    ctx.restore();
  }

  // ------------------------------------------------------ screen space
  roundRect(x, y, w, h, r) {
    const { ctx } = this;
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  /** Province names along the southern edge of each charted region. */
  drawRegionLabels(now) {
    const { ctx } = this;
    const z = this.cam.z;
    for (const reg of this.store.regions.values()) {
      if (reg.kind !== 'domain') continue;
      const pts = this.anchors.filter((a) => a.region.id === reg.id).map((a) => hexToPixel(a.q, a.r));
      if (!pts.length) continue;
      const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
      const maxY = Math.max(...pts.map((p) => p.y));
      const s = this.toScreen(cx, maxY + HEX * 1.5 * (LAND_R - 0.6));
      if (s.x < -300 || s.x > this.w + 300 || s.y < -60 || s.y > this.h + 60) continue;
      const b = BIOMES[reg.biome] || BIOMES.meadow;
      const t0 = this.revealing.get(reg.id);
      const alpha = t0 ? clamp((now - t0 - 600) / 900, 0, 1) : 1;
      if (alpha <= 0) continue;
      const missions = [...this.store.topics.values()].filter((t) => t.region_id === reg.id && t.kind === 'mission');
      const active = missions.filter((t) => t.status === 'active').length;
      const size = clamp(34 * z, 13, 28);
      ctx.save();
      ctx.globalAlpha = alpha;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.font = `800 ${size}px Cinzel, Georgia, serif`;
      if ('letterSpacing' in ctx) ctx.letterSpacing = `${Math.round(size * 0.18)}px`;
      const name = reg.name.toUpperCase();
      ctx.lineWidth = Math.max(3, size * 0.22);
      ctx.strokeStyle = 'rgba(0,0,0,0.65)';
      ctx.strokeText(name, s.x, s.y);
      ctx.fillStyle = '#fff4d6';
      ctx.fillText(name, s.x, s.y);
      if ('letterSpacing' in ctx) ctx.letterSpacing = '0px';
      const sub = `${reg.icon} ${reg.biome_label} · ${missions.length} mission${missions.length === 1 ? '' : 's'}`
        + (active ? ` · ${active} active` : '');
      ctx.font = `600 ${Math.max(10, size * 0.46)}px Inter, sans-serif`;
      ctx.lineWidth = 3;
      ctx.strokeText(sub, s.x, s.y + size * 0.95);
      ctx.fillStyle = rgba(b.accent, 0.95);
      ctx.fillText(sub, s.x, s.y + size * 0.95);
      ctx.restore();
    }
  }

  drawBanners(now) {
    const { ctx } = this;
    this.bannerRects = [];
    if (this.cam.z < 0.2) return;
    for (const topic of this.store.topics.values()) {
      if (!placed(topic)) continue;
      const c = hexToPixel(topic.q, topic.r);
      const s = this.toScreen(c.x, c.y - HEX * 1.5 * (topic.radius + 0.85));
      if (s.x < -200 || s.x > this.w + 200 || s.y < -60 || s.y > this.h + 60) continue;
      const agents = [...this.store.agents.values()].filter((a) => a.topic_id === topic.id);
      const icon = topic.kind === 'hq' ? '👑' : topic.kind === 'allies' ? '🤝' : topic.kind === 'ops' ? '📡'
        : TOPIC_ICON[topic.status] || '⚔️';
      const title = topic.title.length > 30 ? `${topic.title.slice(0, 29)}…` : topic.title;
      ctx.font = '700 13px Cinzel, Georgia, serif';
      const tw = ctx.measureText(title).width;
      const w = Math.max(150, tw + 58), h = topic.kind === 'mission' ? 38 : 30;
      const x = s.x - w / 2, y = s.y - h;
      this.roundRect(x, y, w, h, 8);
      ctx.fillStyle = 'rgba(8,12,26,0.88)';
      ctx.fill();
      ctx.strokeStyle = rgba(topic.color, topic.status === 'active' ? 0.9 : 0.55);
      ctx.lineWidth = 1.5;
      ctx.stroke();
      // pennant
      ctx.beginPath();
      ctx.moveTo(s.x - 6, y + h); ctx.lineTo(s.x + 6, y + h); ctx.lineTo(s.x, y + h + 8); ctx.closePath();
      ctx.fillStyle = rgba(topic.color, 0.9);
      ctx.fill();

      ctx.textBaseline = 'middle';
      ctx.textAlign = 'left';
      drawEmoji(ctx, icon, x + 19, y + 15, 15);
      ctx.font = '700 13px Cinzel, Georgia, serif';
      ctx.fillStyle = '#f8e6a8';
      ctx.fillText(title, x + 32, y + 15);
      if (topic.kind === 'mission') {
        const workers = agents.filter((a) => a.id !== topic.root_agent_id);
        const done = workers.filter((a) => a.status === 'done').length;
        const pct = topic.status === 'completed' ? 1 : workers.length ? (done / workers.length) * 0.95 : 0.03;
        const bx = x + 10, by = y + 27, bw = w - 20;
        this.roundRect(bx, by, bw, 5, 2.5);
        ctx.fillStyle = 'rgba(255,255,255,0.08)';
        ctx.fill();
        if (pct > 0) {
          this.roundRect(bx, by, Math.max(5, bw * pct), 5, 2.5);
          ctx.fillStyle = topic.status === 'failed' ? '#ef5350' : topic.color;
          ctx.fill();
        }
        if (topic.status === 'active') {
          const sx = bx + ((now / 12) % (bw + 40)) - 20;
          const g = ctx.createLinearGradient(sx - 20, 0, sx + 20, 0);
          g.addColorStop(0, 'rgba(255,255,255,0)');
          g.addColorStop(0.5, 'rgba(255,255,255,0.35)');
          g.addColorStop(1, 'rgba(255,255,255,0)');
          ctx.save();
          this.roundRect(bx, by, Math.max(5, bw * pct), 5, 2.5);
          ctx.clip();
          ctx.fillStyle = g;
          ctx.fillRect(sx - 20, by, 40, 5);
          ctx.restore();
        }
      }
      if (topic.kind === 'ops') { // proposals waiting for the human → pulsing badge on the banner
        const n = (this.store.ops?.proposals || []).filter((p) => p.status === 'pending').length;
        if (n) {
          const label = `📨 ${n}`;
          ctx.font = '700 12px Inter, sans-serif';
          const bw = ctx.measureText(label).width + 16, bx = x + w - bw / 2 - 4, by = y - 10;
          const pulse = 0.6 + 0.4 * Math.sin(now / 300);
          this.roundRect(bx, by, bw, 20, 10);
          ctx.fillStyle = `rgba(239,83,80,${0.75 + 0.25 * pulse})`;
          ctx.fill();
          ctx.strokeStyle = '#fff';
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.fillStyle = '#fff';
          ctx.textAlign = 'center';
          ctx.fillText(label, bx + bw / 2, by + 10.5);
          ctx.textAlign = 'left';
        }
      }
      this.bannerRects.push({ id: topic.id, x, y, w, h });
    }
  }

  drawLabels(now) {
    const { ctx } = this;
    const z = this.cam.z;
    const placed = [];
    const overlaps = (r) => placed.some((p) => r.x < p.x + p.w && r.x + r.w > p.x && r.y < p.y + p.h && r.y + r.h > p.y);
    const ROLE_PRIO = { commander: 2, orchestrator: 3, swarm_leader: 4, worker: 5, ally: 5, swarm_member: 6 };
    const prio = (a) => (a.id === this.selectedId ? 0 : a.id === this.hoverId ? 1 : ROLE_PRIO[a.role] ?? 6)
      - (ACTIVE.has(a.status) ? 0.5 : 0);
    const jobs = [...this.labelJobs].sort((x, y) => prio(x.a) - prio(y.a));
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    let bubbles = 0;
    for (const job of jobs) {
      const { a } = job;
      const s = this.toScreen(job.x, job.y);
      const Rs = job.R * z;
      const focus = a.id === this.selectedId || a.id === this.hoverId;

      // speech bubble with the current activity (stacked upwards when crowded)
      const wantBubble = focus || (ACTIVE.has(a.status) && z > 0.7 && a.role !== 'swarm_member' && bubbles < 24);
      if (wantBubble && a.activity) {
        let text = a.activity.length > 38 ? `${a.activity.slice(0, 37)}…` : a.activity;
        if (a.status === 'thinking') text += '.'.repeat(1 + (Math.floor(now / 400) % 3));
        ctx.font = '500 11.5px Inter, sans-serif';
        const bw = ctx.measureText(text).width + 18, bh = 22;
        const rect = { x: s.x - bw / 2, y: s.y - Rs - 16 - bh, w: bw, h: bh };
        let lift = 0;
        while (overlaps(rect) && lift < 3) { rect.y -= bh + 4; lift++; }
        if (!overlaps(rect) || focus) {
          bubbles++;
          placed.push(rect);
          this.roundRect(rect.x, rect.y, bw, bh, 7);
          ctx.fillStyle = focus ? 'rgba(255,240,200,0.97)' : 'rgba(245,247,255,0.94)';
          ctx.fill();
          if (lift === 0) {
            ctx.beginPath();
            ctx.moveTo(s.x - 5, rect.y + bh); ctx.lineTo(s.x + 5, rect.y + bh); ctx.lineTo(s.x, rect.y + bh + 6); ctx.closePath();
            ctx.fill();
          } else {
            ctx.beginPath();
            ctx.moveTo(s.x, rect.y + bh); ctx.lineTo(s.x, s.y - Rs - 4);
            ctx.strokeStyle = 'rgba(245,247,255,0.6)';
            ctx.lineWidth = 1;
            ctx.stroke();
          }
          ctx.fillStyle = '#141a2e';
          ctx.fillText(text, s.x, rect.y + bh / 2 + 0.5);
        }
      }

      // name plate (skipped when it would collide with a more important label)
      if ((z > 0.75 && a.role !== 'swarm_member') || focus || z > 1.3) {
        ctx.font = '600 11px Inter, sans-serif';
        const name = a.name.length > 24 ? `${a.name.slice(0, 23)}…` : a.name;
        const tw = ctx.measureText(name).width;
        const rect = { x: s.x - tw / 2 - 3, y: s.y + Rs + 7, w: tw + 6, h: 15 };
        if (!overlaps(rect) || focus) {
          placed.push(rect);
          ctx.lineWidth = 3;
          ctx.strokeStyle = 'rgba(0,0,0,0.85)';
          ctx.strokeText(name, s.x, rect.y + 7.5);
          ctx.fillStyle = focus ? '#ffe08a' : '#dfe4f0';
          ctx.fillText(name, s.x, rect.y + 7.5);
        }
      }
    }
  }

  drawFloaters(now) {
    const { ctx } = this;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    this.floaters = this.floaters.filter((f) => {
      const k = (now - f.t0) / 1600;
      if (k >= 1) return false;
      if (k < 0) return true; // delayed
      const s = this.toScreen(f.x, f.y);
      ctx.globalAlpha = 1 - k * k;
      ctx.font = '800 15px Cinzel, Georgia, serif';
      ctx.lineWidth = 4;
      ctx.strokeStyle = 'rgba(0,0,0,0.8)';
      ctx.strokeText(f.text, s.x, s.y - k * 46);
      ctx.fillStyle = f.color;
      ctx.fillText(f.text, s.x, s.y - k * 46);
      ctx.globalAlpha = 1;
      return true;
    });
  }

  drawMinimap() {
    const ctx = this.mmCtx;
    if (!ctx) return;
    const W = this.mm.width, H = this.mm.height;
    ctx.clearRect(0, 0, W, H);
    const b = this.bounds();
    const pad = 300;
    const bw = b.x1 - b.x0 + pad * 2, bh = b.y1 - b.y0 + pad * 2;
    const s = Math.min(W / bw, H / bh);
    const ox = W / 2 - ((b.x0 + b.x1) / 2) * s, oy = H / 2 - ((b.y0 + b.y1) / 2) * s;
    this.mmTransform = { s, ox, oy };
    for (const a of this.anchors) { // region land in biome colors
      const p = hexToPixel(a.q, a.r);
      ctx.beginPath();
      ctx.arc(p.x * s + ox, p.y * s + oy, (LAND_R + 0.5) * HEX * 1.6 * s, 0, Math.PI * 2);
      ctx.fillStyle = rgba((BIOMES[a.region.biome] || BIOMES.meadow).fills[1], 0.75);
      ctx.fill();
    }
    for (const t of this.store.topics.values()) {
      if (!placed(t)) continue;
      const p = hexToPixel(t.q, t.r);
      const R = (t.radius + 0.6) * HEX * 1.75 * s;
      ctx.beginPath();
      for (let i = 0; i < 6; i++) {
        const a = (Math.PI / 180) * (60 * i - 30);
        ctx.lineTo(p.x * s + ox + R * Math.cos(a), p.y * s + oy + R * Math.sin(a));
      }
      ctx.closePath();
      ctx.fillStyle = rgba(t.color, t.status === 'active' || t.status === 'base' ? 0.35 : 0.15);
      ctx.fill();
      ctx.strokeStyle = rgba(t.color, 0.8);
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    for (const a of this.store.agents.values()) {
      const p = hexToPixel(a.q, a.r);
      ctx.fillStyle = STATUS_COLORS[a.status] || '#fff';
      ctx.fillRect(p.x * s + ox - 1.2, p.y * s + oy - 1.2, 2.4, 2.4);
    }
    const tl = this.toWorld(0, 0), br = this.toWorld(this.w, this.h);
    ctx.strokeStyle = 'rgba(255,224,138,0.9)';
    ctx.lineWidth = 1.2;
    ctx.strokeRect(tl.x * s + ox, tl.y * s + oy, (br.x - tl.x) * s, (br.y - tl.y) * s);
  }
}
