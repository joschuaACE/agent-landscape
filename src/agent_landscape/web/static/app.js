import { HexMap, STATUS_COLORS, ACTIVE } from './map.js';
import { createReportView } from './report.js';
import { createComposer } from './composer.js';
import { createProfiles } from './profiles.js';
import { createOps } from './ops.js';
import { createTeams } from './teams.js';

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${(n / 1e3).toFixed(1)}k` : String(n ?? 0));

const store = { agents: new Map(), topics: new Map(), regions: new Map(), stats: {}, config: {}, ops: null, selected: null };
// full-screen panels (briefing, report, profile editor) hide the map → it stops painting while they are open
const map = new HexMap($('#map'), $('#minimap'), store, {
  onSelect: select, onHover: hover,
  onBanner: (id) => { if (store.topics.get(id)?.kind === 'ops') switchTab('ops'); },
  isCovered: () => !$('#composer').hidden || !$('#report-view').hidden || !$('#profile-view').hidden,
});
const reports = createReportView({
  el: $('#report-view'), store, api, toast, map,
  onInspect: (id) => { reports.close(); select(id); map.focusAgent(id); },
  onChange: () => scheduleRender(),
});
let firstSnapshot = true;
let missionsLaunched = Number(localStorage.getItem('al.launched') || 0);
const isPlaced = (t) => t && t.q !== null && t.q !== undefined;

// ------------------------------------------------------------------ socket
function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => { $('#conn').classList.add('on'); renderLlmPop(); };
  ws.onclose = () => { $('#conn').classList.remove('on'); renderLlmPop(); setTimeout(connect, 1500); };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}

function handle(ev) {
  switch (ev.type) {
    case 'snapshot': {
      store.agents = new Map(ev.agents.map((a) => [a.id, a]));
      store.topics = new Map(ev.topics.map((t) => [t.id, t]));
      store.regions = new Map((ev.regions || []).map((r) => [r.id, r]));
      store.config = ev.config;
      store.ops = ev.ops || null;
      updateStats(ev.stats);
      map.territories.clear();
      map.refreshLand();
      renderConfig();
      loadModels(); // (re)connect may mean a restarted server with other models
      if (firstSnapshot) {
        firstSnapshot = false;
        // #focus=<topic-id> or #focus=latest opens the map on a specific territory
        const want = new URLSearchParams(location.hash.slice(1)).get('focus');
        const missions = [...store.topics.values()].filter((t) => t.kind === 'mission').sort((a, b) => b.created_at - a.created_at);
        const target = want === 'latest' ? missions[0]?.id : want;
        setTimeout(() => (target && store.topics.has(target) ? map.focusTopic(target) : map.fit()), 50);
      }
      break;
    }
    case 'config_changed':
      store.config = ev.config;
      renderConfig();
      break;
    case 'profiles_changed':
      if (!$('#tab-agents').hidden) profiles.load();
      break;
    case 'teams_changed':
      if (!$('#tab-teams').hidden) teams.load();
      break;
    case 'ops_polled':
      ops.apply(ev);
      break;
    case 'ops_message': {
      ops.apply(ev);
      const e = ev.entry;
      if (e.relevant) {
        const m = store.agents.get(e.monitor_id);
        if (m) map.floatText(m, '📨 relevant', '#80deea');
        addLog(e.channel === 'teams' ? '💬' : '📧', `<b>${esc(m?.name || 'Monitor')}</b> flagged a message from <b>${esc(String(e.sender).split('<')[0])}</b>: ${esc(e.subject || e.preview)}`, 'info', e.monitor_id);
      }
      break;
    }
    case 'ops_proposal': {
      const p = ops.apply(ev);
      if (p) {
        toast('📨', `<b>${esc(p.title)}</b> – the Commander prepared a mission. Click to review.`, 'achievement', 'MISSION PROPOSAL')
          .addEventListener('click', () => switchTab('ops'));
        addLog('📨', `Commander proposes <b>${esc(p.title)}</b> – awaiting your approval`, 'success', 'commander');
      }
      break;
    }
    case 'repo_changed': {
      reports.repoChanged(ev.topic_id);
      const a = [...store.agents.values()].find((x) => x.name === ev.by && x.topic_id === ev.topic_id);
      if (a) map.floatText(a, `📦 ${ev.commits} commit${ev.commits === 1 ? '' : 's'}`, '#b9f6ca');
      break;
    }
    case 'topic_created':
      store.topics.set(ev.topic.id, ev.topic);
      if (ev.topic.kind === 'mission') addLog('🧭', `HQ is surveying <b>${esc(ev.topic.title)}</b>`, 'info');
      break;
    case 'topic_updated': {
      const prev = store.topics.get(ev.topic.id);
      const t = ev.topic;
      store.topics.set(t.id, t);
      map.onTopicChanged(t.id);
      if (t.team && (!prev || prev.status !== t.status || prev.rounds !== t.rounds) && !$('#tab-teams').hidden) teams.load();
      if (prev && !isPlaced(prev) && isPlaced(t) && t.kind === 'mission') { // HQ placed the territory
        const reg = store.regions.get(t.region_id);
        addLog('🏰', `<b>${esc(t.title)}</b> claimed a territory in ${reg ? `${reg.icon} <b>${esc(reg.name)}</b>` : 'the map'}`, 'info');
        if ($('#follow').checked) setTimeout(() => map.focusTopic(t.id), 150);
      }
      if (prev && t.rounds > prev.rounds) {
        toast('🔁', `Feedback round ${t.rounds}: the team of <b>${esc(t.title)}</b> is back at work`, 'success');
        if ($('#follow').checked) map.focusTopic(t.id);
      }
      if (prev && prev.status === 'active' && ev.topic.status !== 'active') {
        map.onTopicDone(ev.topic);
        const ok = ev.topic.status === 'completed';
        toast(ok ? '🏆' : ev.topic.status === 'failed' ? '💀' : '🏳️',
          `${ok ? 'Victory' : ev.topic.status === 'failed' ? 'Defeat' : 'Retreat'}: ${esc(ev.topic.title)}`, ok ? 'success' : 'error');
      }
      break;
    }
    case 'topic_removed':
      store.topics.delete(ev.topic_id);
      for (const [id, a] of store.agents) if (a.topic_id === ev.topic_id) store.agents.delete(id);
      map.territories.delete(ev.topic_id);
      map.refreshLand();
      if (store.selected && !store.agents.has(store.selected)) select(null);
      break;
    case 'region_created': {
      const reg = ev.region;
      store.regions.set(reg.id, reg);
      map.onRegionCreated(reg);
      if (reg.kind === 'domain') {
        toast(reg.icon, `New region charted: <b>${esc(reg.name)}</b> – ${esc(reg.biome_label)}`, 'achievement', 'COMMAND HQ');
        addLog('🗺️', `HQ charted <b>${esc(reg.name)}</b> (${reg.icon} ${esc(reg.biome_label)})`, 'success');
      }
      break;
    }
    case 'region_updated':
      store.regions.set(ev.region.id, ev.region);
      map.refreshLand();
      addLog(ev.region.icon, `Region <b>${esc(ev.region.name)}</b> is now ${esc(ev.region.biome_label)}`, 'info');
      break;
    case 'agent_spawned':
      store.agents.set(ev.agent.id, ev.agent);
      map.onSpawn(ev.agent);
      if (ev.agent.role !== 'commander') addLog(ev.agent.icon, `<b>${esc(ev.agent.name)}</b> deployed`, 'info', ev.agent.id);
      break;
    case 'agent_updated': {
      const prev = store.agents.get(ev.agent.id);
      store.agents.set(ev.agent.id, ev.agent);
      map.onAgentUpdate(prev, ev.agent);
      if (prev && prev.status !== ev.agent.status && ev.agent.status === 'failed') {
        addLog('❌', `<b>${esc(ev.agent.name)}</b> failed: ${esc(ev.agent.activity)}`, 'error', ev.agent.id);
      }
      break;
    }
    case 'a2a_message': {
      map.onMessage(ev);
      const from = ev.from === 'user' ? 'You' : store.agents.get(ev.from)?.name || ev.from;
      const to = store.agents.get(ev.to)?.name || ev.to;
      const icon = ev.kind === 'request' ? '📜' : ev.kind === 'error' ? '💥' : '✉️';
      addLog(icon, `${esc(from)} → ${esc(to)} <span class="muted">${ev.kind}</span>`, ev.kind === 'error' ? 'error' : '', ev.to);
      break;
    }
    case 'tool_call': {
      const a = store.agents.get(ev.agent_id);
      if (a) {
        map.floatText(a, `🔧 ${ev.tool}`, '#80d8ff');
        addLog('🔧', `<b>${esc(a.name)}</b> ${esc(ev.label)}`, '', a.id);
      }
      break;
    }
    case 'level_up': {
      const a = store.agents.get(ev.agent_id);
      if (a) {
        a.level = ev.level;
        map.floatText(a, `LEVEL ${ev.level}!`, '#ffd54f');
        addLog('⭐', `<b>${esc(a.name)}</b> reached level ${ev.level}`, 'success', a.id);
      }
      break;
    }
    case 'log':
      if (ev.level !== 'info' || /plan/.test(ev.text)) addLog(ev.level === 'success' ? '✅' : ev.level === 'error' ? '❌' : ev.level === 'warn' ? '⚠️' : '🗺️', esc(ev.text), ev.level, ev.agent_id);
      break;
    case 'stats':
      updateStats(ev.stats);
      break;
  }
  reports.poke(ev);
  scheduleRender();
  checkAchievements();
}

// ------------------------------------------------------------------- HUD
const RANKS = [[0, 'Recruit'], [300, 'Sergeant'], [1000, 'Lieutenant'], [2500, 'Captain'], [5000, 'Major'], [9000, 'Colonel'], [15000, 'General'], [25000, 'Warlord']];
function updateStats(s) {
  const prev = store.stats;
  store.stats = s;
  const set = (id, v, changed) => {
    const el = $(id);
    if (el.textContent !== v) {
      el.textContent = v;
      if (changed) { el.parentElement.classList.remove('bump'); void el.offsetWidth; el.parentElement.classList.add('bump'); }
    }
  };
  set('#r-active', String(s.active_agents ?? 0));
  set('#r-missions', String(s.active_missions ?? 0));
  set('#r-won', String(s.missions_completed ?? 0), prev.missions_completed !== s.missions_completed);
  set('#r-spawned', String(s.agents_spawned ?? 0));
  set('#r-msgs', fmt(s.a2a_messages));
  set('#r-tokens', fmt(s.tokens));
  set('#r-cost', `$${(s.cost || 0).toFixed(s.cost < 1 ? 4 : 2)}`);
  const xp = (s.missions_completed || 0) * 250 + (s.agents_spawned || 0) * 15 + Math.floor((s.tokens || 0) / 50);
  let idx = 0;
  RANKS.forEach(([min], i) => { if (xp >= min) idx = i; });
  const next = RANKS[idx + 1];
  $('#rank').textContent = `🎖️ ${RANKS[idx][1]} · ${fmt(xp)} XP${next ? ` / ${fmt(next[0])}` : ''}`;
  if (prev.rankIdx !== undefined && idx > prev.rankIdx) toast('🎖️', `Promoted to <b>${RANKS[idx][1]}</b>!`, 'achievement', 'PROMOTION');
  s.rankIdx = idx;
}

function renderConfig() {
  const c = store.config;
  const badge = $('#llm-badge');
  const problems = c.model_problems || [];
  $('#llm-chip').className = `badge ${problems.length ? 'bad' : c.mock ? 'mock' : 'live'}`;
  badge.textContent = problems.length ? `⚠️ ${c.default_model}` : c.mock ? '🧪 MOCK' : c.default_model;
  $('#llm-chip').title = problems.length ? problems.join('\n') : 'Model & connection';
  renderLlmPop();
  if (problems.length && !renderConfig.warned) {
    renderConfig.warned = true;
    problems.forEach((p) => toast('⚠️', esc(p), 'error'));
  }
  const slider = $('[name=swarm_size]');
  slider.max = c.max_swarm_size || 6;
  if (!slider.dataset.touched) { slider.value = c.default_swarm_size || 3; $('#swarm-size-out').textContent = slider.value; }
  $('#litellm-snippet').textContent =
`agents:
  - agent_name: agent-landscape
    agent_card_params:
      name: "Agent Landscape Commander"
      url: "${c.commander_url}"
      protocolVersion: "0.3"`;
}

function renderLlmPop() {
  const el = $('#llm-pop');
  if (!el || el.contains(document.activeElement)) return; // don't close an open dropdown
  const c = store.config || {};
  const problems = c.model_problems || [];
  const live = $('#conn').classList.contains('on');
  const opts = [...new Set([...(c.default_model ? [c.default_model] : []), ...modelList])];
  el.innerHTML = `<h4>⚡ MODEL &amp; CONNECTION</h4>
    <dl class="kv">
      <dt>Server</dt><dd><span class="dot ${live ? 'ok' : 'bad'}"></span> ${live ? 'live (WebSocket)' : 'reconnecting…'}</dd>
      <dt>Mode</dt><dd>${c.mock ? '🧪 Mock – LiteLLM <code>mock_response</code>' : c.proxy ? '⚡ Live via LiteLLM proxy' : '⚡ Live (direct)'}</dd>
      <dt>Default model</dt><dd>${opts.length > 1 ? `<select id="default-model" title="Applies to new LLM turns of units without a mission, profile or pinned model – until restart">
        ${opts.map((m) => `<option value="${esc(m)}" ${m === c.default_model ? 'selected' : ''}>${esc(m)}</option>`).join('')}</select>`
        : esc(c.default_model || '—')}</dd>
      <dt>Vector store</dt><dd>${esc(c.vectors || '—')}${c.mcp?.length ? ` · MCP ${c.mcp.length}` : ''}</dd>
    </dl>
    ${problems.length ? `<div class="pop-warn">${problems.map((p) => `<p>⚠️ ${esc(p)}</p>`).join('')}</div>` : ''}
    <p class="muted small">Switching here lasts until the server restarts – set <code>DEFAULT_MODEL</code> in <code>.env</code> to keep it.</p>`;
}
$('#llm-pop').addEventListener('change', async (e) => {
  if (e.target.id !== 'default-model') return;
  const model = e.target.value;
  e.target.blur();
  try {
    store.config = await api('/api/config', { method: 'PATCH', body: JSON.stringify({ default_model: model }) });
    renderConfig();
    toast('🎛️', `Default model is now <b>${esc(model)}</b>`, 'success');
  } catch { renderLlmPop(); }
});

// -------------------------------------------------------------- popovers
function closePops(except = null) {
  document.querySelectorAll('.pop').forEach((p) => { if (p !== except) p.hidden = true; });
  document.querySelectorAll('[data-pop]').forEach((b) => b.classList.toggle('open', !!except && b.dataset.pop === except.id));
}
document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-pop]');
  if (btn) {
    const pop = document.getElementById(btn.dataset.pop);
    const show = pop.hidden;
    closePops(show ? pop : null);
    pop.hidden = !show;
    if (show && pop.id === 'llm-pop') { loadModels(); renderLlmPop(); }
    return;
  }
  if (!e.target.closest('.pop')) closePops();
});

// --------------------------------------------------------------- rendering
let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    // live events arrive many times per second – only rebuild what is visible (style/layout cost → INP);
    // switchTab() / select() re-render the pane that becomes visible
    const shown = (id) => !$(id).hidden;
    if (shown('#tab-missions')) renderMissions();
    renderRegionSelect(); // cheap (signature check) and the composer may open any moment
    if (shown('#unit-panel') || !store.selected) renderInspector();
    if (shown('#tab-allies')) renderAllies();
    if (shown('#tab-reports')) renderReportList();
    renderOps();
    $('#empty-hint').hidden = [...store.topics.values()].some((t) => t.kind === 'mission');
  });
}

function topicAgents(id) { return [...store.agents.values()].filter((a) => a.topic_id === id); }

function renderOps() {
  const n = ops.pending().length;
  const badge = $('#ops-count');
  badge.hidden = !n;
  badge.textContent = n ? String(n) : '';
  if (!$('#tab-ops').hidden) ops.render();
}

const feedbackOpen = new Set();
const feedbackDrafts = new Map();
let missionsDirty = false;

function missionCard(t, openResults) {
  const agents = topicAgents(t.id);
  const workers = agents.filter((a) => a.id !== t.root_agent_id);
  const done = workers.filter((a) => a.status === 'done').length;
  const pct = t.status === 'completed' ? 100 : workers.length ? Math.round((done / workers.length) * 95) : 3;
  const swarms = agents.filter((a) => a.role === 'swarm_leader').length;
  const veterans = agents.filter((a) => a.veteran).length;
  const modeIcon = { auto: '🧠', hierarchy: '🏰', swarm: '🐝' }[t.mode] || '🧠';
  const units = agents.map((a) => `<span class="${ACTIVE.has(a.status) ? 'busy' : a.status === 'done' || a.status === 'canceled' ? 'dim' : ''}" title="${esc(a.name)} · ${a.status}${a.veteran ? ' · 🎖️ veteran' : ''}">${a.icon}</span>`).join('');
  const canFeedback = t.status !== 'active' && t.root_agent_id;
  const fbOpen = canFeedback && feedbackOpen.has(t.id);
  return `<div class="mission" style="--c:${t.color}" data-id="${t.id}">
    <div class="m-head"><b title="${esc(t.goal)}">${esc(t.title)}</b>${t.rounds > 1 ? `<span class="chip round" title="Feedback rounds">🔁 ${t.rounds}</span>` : ''}<span class="chip ${t.status}">${t.status}</span></div>
    <div class="m-meta">${modeIcon} ${t.mode} · ${agents.length} units${swarms ? ` · ${swarms} swarm${swarms > 1 ? 's' : ''}` : ''}${veterans ? ` · 🎖️ ${veterans}` : ''}${t.knowledge?.length ? ` · <span title="${esc(t.knowledge.map((k) => k.name).join(', '))}">📚 ${t.knowledge.length}</span>` : ''} · ${done}/${workers.length} reports</div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    <div class="m-units">${units}</div>
    <div class="m-actions">
      <button class="btn small" data-act="focus">🎯 Focus</button>
      ${t.root_agent_id ? `<button class="btn small" data-act="report" title="Mission report: deliverable, inputs, artifacts">📑 Report</button>` : ''}
      ${canFeedback ? `<button class="btn small" data-act="feedback">💬 Feedback</button>` : ''}
      ${t.status === 'interrupted' ? '<button class="btn small resume" data-act="resume" title="Continue with the same team">▶️ Resume</button>' : ''}
      ${t.status === 'active' ? '<button class="btn small danger" data-act="cancel">✋ Recall</button>' : '<button class="btn small" data-act="dismiss">🗑️ Dismiss</button>'}
    </div>
    ${fbOpen ? `<div class="feedback-box">
      <textarea data-fb="${t.id}" rows="2" placeholder="What should the team improve? The same agents re-work it with memory of round ${t.rounds}…">${esc(feedbackDrafts.get(t.id) || '')}</textarea>
      <button class="btn primary" data-act="send-feedback">🔁 Send back to the team</button></div>` : ''}
    ${t.feedback?.length ? `<ol class="feedback-log" start="2" title="Feedback rounds">${t.feedback.map((f) => `<li>${esc(f.length > 90 ? `${f.slice(0, 89)}…` : f)}</li>`).join('')}</ol>` : ''}
    ${t.result ? `<div class="m-preview" data-act="report" title="Open the full mission report"><span>📜 ${esc(t.result.replace(/[#*_`>|-]+/g, ' ').replace(/\s+/g, ' ').slice(0, 260))}</span></div>` : ''}
  </div>`;
}

function regionHeader(reg, missions) {
  const active = missions.filter((t) => t.status === 'active').length;
  const opts = (store.config.biomes || []).map((b) => `<option value="${b.key}" ${b.key === reg.biome ? 'selected' : ''}>${b.icon} ${esc(b.label)}</option>`).join('');
  const accent = { desert: '#d9a95b', meadow: '#8bc34a', crystal: '#b388ff', metropolis: '#64b5f6', forest: '#66bb6a', volcanic: '#ff7043', tundra: '#b3e5fc', swamp: '#9ccc65', tropical: '#4db6ac', autumn: '#ff9800', highlands: '#b0bec5' }[reg.biome] || '#8a93a6';
  return `<div class="r-head" style="--rc:${accent}" data-region="${reg.id}">
    <span class="r-icon">${reg.icon}</span>
    <div class="r-name"><b title="${esc(reg.description)}">${esc(reg.name)}</b><small>${esc(reg.biome_label)} · ${missions.length} mission${missions.length === 1 ? '' : 's'}${active ? ` · ${active} active` : ''}</small></div>
    <div class="r-tools">
      <select data-act="biome" title="Change the biome of this region">${opts}</select>
      <button class="btn ghost small" data-act="rename" title="Rename region">✏️</button>
      <button class="btn ghost small" data-act="focus-region" title="Show region on the map">🎯</button>
    </div>
  </div>`;
}

function renderMissions() {
  const list = $('#mission-list');
  // don't rebuild while the user is typing feedback or picking a biome
  if (list.contains(document.activeElement) && document.activeElement.matches('textarea, select, input')) {
    missionsDirty = true;
    return;
  }
  missionsDirty = false;
  const missions = [...store.topics.values()].filter((t) => t.kind === 'mission');
  const openResults = new Set([...list.querySelectorAll('details[open]')].map((d) => d.dataset.id));
  const latest = (ts) => Math.max(...ts.map((t) => t.created_at));
  const groups = [...store.regions.values()].filter((r) => r.kind === 'domain')
    .map((reg) => ({ reg, ts: missions.filter((t) => t.region_id === reg.id) }))
    .filter((g) => g.ts.length)
    .sort((a, b) => latest(b.ts) - latest(a.ts));
  const surveying = missions.filter((t) => !t.region_id);
  const byNewest = (a, b) => b.created_at - a.created_at;
  list.innerHTML = (surveying.length ? `<div class="region-group surveying">
      <div class="r-head"><span class="r-icon">🧭</span><div class="r-name"><b>Being surveyed</b><small class="surveying-hint">Command HQ is choosing a region…</small></div></div>
      ${surveying.sort(byNewest).map((t) => missionCard(t, openResults)).join('')}</div>` : '')
    + groups.map(({ reg, ts }) => `<div class="region-group">${regionHeader(reg, ts)}${ts.sort(byNewest).map((t) => missionCard(t, openResults)).join('')}</div>`).join('')
    || '<p class="muted small" style="text-align:center">No missions yet. Deploy your first agents above.</p>';
}

$('#mission-list').addEventListener('focusout', () => setTimeout(() => { if (missionsDirty) scheduleRender(); }, 0));
$('#mission-list').addEventListener('input', (e) => {
  if (e.target.dataset.fb) feedbackDrafts.set(e.target.dataset.fb, e.target.value);
});
$('#mission-list').addEventListener('change', async (e) => {
  const regionId = e.target.closest('[data-region]')?.dataset.region;
  if (regionId && e.target.dataset.act === 'biome') {
    await api(`/api/regions/${regionId}`, { method: 'PATCH', body: JSON.stringify({ biome: e.target.value }) });
    e.target.blur();
  }
});
$('#mission-list').addEventListener('click', async (e) => {
  const act = e.target.closest('[data-act]')?.dataset.act;
  const regionId = e.target.closest('[data-region]')?.dataset.region;
  if (regionId) {
    if (act === 'rename') {
      const reg = store.regions.get(regionId);
      const name = prompt('Rename region', reg?.name || '');
      if (name && name.trim()) await api(`/api/regions/${regionId}`, { method: 'PATCH', body: JSON.stringify({ name: name.trim() }) });
    } else if (act !== 'biome') {
      map.focusRegion(regionId);
    }
    return;
  }
  const card = e.target.closest('.mission');
  if (!card || e.target.closest('details, textarea')) return;
  const id = card.dataset.id;
  if (act === 'cancel') {
    await api(`/api/topics/${id}/cancel`, { method: 'POST' });
  } else if (act === 'resume') {
    try {
      await api(`/api/topics/${id}/resume`, { method: 'POST' });
      toast('▶️', 'Mission resumed – the team is back at work', 'success');
    } catch { /* toast shown */ }
  } else if (act === 'dismiss') {
    await api(`/api/topics/${id}`, { method: 'DELETE' });
  } else if (act === 'report') {
    reports.open(id);
  } else if (act === 'feedback') {
    if (feedbackOpen.has(id)) feedbackOpen.delete(id); else feedbackOpen.add(id);
    scheduleRender();
    setTimeout(() => $(`textarea[data-fb="${id}"]`)?.focus(), 30);
  } else if (act === 'send-feedback') {
    const text = (feedbackDrafts.get(id) || '').trim();
    if (text.length < 2) { toast('💬', 'Write some feedback first', 'error'); return; }
    try {
      await api(`/api/topics/${id}/feedback`, { method: 'POST', body: JSON.stringify({ text }) });
      feedbackDrafts.delete(id);
      feedbackOpen.delete(id);
      scheduleRender();
    } catch { /* toast shown */ }
  } else {
    map.focusTopic(id);
  }
});

function renderReportList() {
  const el = $('#report-list');
  const missions = [...store.topics.values()].filter((t) => t.kind === 'mission' && t.root_agent_id)
    .sort((a, b) => b.created_at - a.created_at);
  const openId = reports.openId();
  const groups = new Map();
  for (const t of missions) {
    const key = t.region_id || '';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(t);
  }
  const stIcon = { active: '⚔️', completed: '🏆', failed: '💀', canceled: '🏳️', interrupted: '⏸️' };
  el.innerHTML = [...groups].map(([rid, ts]) => {
    const reg = store.regions.get(rid);
    return `<div class="region-group">${reg ? `<div class="small muted" style="margin:6px 2px">${reg.icon} <b>${esc(reg.name)}</b> · ${esc(reg.biome_label)}</div>` : ''}
      ${ts.map((t) => `<div class="report-row ${t.id === openId ? 'open' : ''}" style="--c:${t.color}" data-report="${t.id}">
        <span class="ico">${stIcon[t.status] || '📑'}</span><b title="${esc(t.goal)}">${esc(t.title)}</b>
        <span class="chip ${t.status}">${t.status}</span>
        <small>${t.rounds > 1 ? `🔁 ${t.rounds} rounds · ` : ''}🪙 ${fmt(t.tokens || 0)} · $${(t.cost || 0).toFixed(4)} · ${new Date(t.created_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</small>
      </div>`).join('')}</div>`;
  }).join('') || '<p class="muted small" style="text-align:center">No reports yet – they appear as soon as a mission is deployed.</p>';
}
let searchTimer = null;
$('#report-search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  const out = $('#search-results');
  if (q.length < 2) { out.innerHTML = ''; $('#report-list').hidden = false; return; }
  searchTimer = setTimeout(async () => {
    const res = await (await fetch(`/api/search?q=${encodeURIComponent(q)}`)).json();
    if (e.target.value.trim() !== q) return;
    $('#report-list').hidden = true;
    if (!res.enabled) { out.innerHTML = '<p class="muted small">Search needs persistence (DB_PATH) – it is disabled.</p>'; return; }
    out.innerHTML = res.hits.length ? res.hits.map((h) => `<div class="search-hit ${h.exists ? '' : 'gone'}" data-report="${h.exists ? esc(h.topic_id) : ''}" data-kind="${h.kind}">
        <div class="sh-head">${h.kind === 'mission' ? '📜' : '🧩'} <b>${esc(h.kind === 'mission' ? h.title : h.title)}</b></div>
        ${h.kind !== 'mission' && h.mission ? `<small class="muted">in ${esc(h.mission)}</small>` : ''}
        <div class="sh-snip">${esc(h.snippet).replaceAll('[[', '<mark>').replaceAll(']]', '</mark>')}</div></div>`).join('')
      : `<p class="muted small">No matches for “${esc(q)}”.</p>`;
  }, 200);
});
$('#search-results').addEventListener('click', (e) => {
  const hit = e.target.closest('[data-report]');
  if (!hit?.dataset.report) return;
  reports.open(hit.dataset.report, hit.dataset.kind === 'artifact' ? 'artifacts' : 'deliverable');
  map.focusTopic(hit.dataset.report);
});
$('#report-list').addEventListener('click', (e) => {
  const id = e.target.closest('[data-report]')?.dataset.report;
  if (id) { reports.open(id); map.focusTopic(id); }
});

let regionSelectSig = '';
function renderRegionSelect() {
  const sel = $('[name=region_id]');
  const regions = [...store.regions.values()].filter((r) => r.kind === 'domain');
  const sig = regions.map((r) => `${r.id}|${r.name}|${r.biome}`).join(';');
  if (sig === regionSelectSig || document.activeElement === sel) return;
  regionSelectSig = sig;
  const keep = sel.value;
  const proposed = sel.querySelector('option[value="__new__"]'); // new region suggested by an ops proposal
  sel.innerHTML = '<option value="">🧭 Auto – Command HQ picks or charts the region</option>'
    + regions.map((r) => `<option value="${r.id}">${r.icon} ${esc(r.name)} (${esc(r.biome_label)})</option>`).join('');
  if (proposed) sel.append(proposed);
  sel.value = regions.some((r) => r.id === keep) || (proposed && keep === '__new__') ? keep : '';
}

function renderInspector() {
  const el = $('#inspector');
  const a = store.selected && store.agents.get(store.selected);
  if (!a) { $('#unit-panel').hidden = true; return; }
  if (document.activeElement?.matches('#inspector select') && document.activeElement.dataset.agentId === a.id) return; // don't close an open dropdown
  const parent = a.parent_id && store.agents.get(a.parent_id);
  const topic = store.topics.get(a.topic_id);
  const curLvl = 40 * (a.level - 1) ** 2, nextLvl = 40 * a.level ** 2;
  const xpPct = Math.round(((a.xp - curLvl) / Math.max(1, nextLvl - curLvl)) * 100);
  const sc = STATUS_COLORS[a.status] || '#888';
  const kids = a.children.map((id) => store.agents.get(id)).filter(Boolean);
  const scroll = el.querySelector('pre')?.scrollTop;
  el.innerHTML = `
    <div class="unit-head">
      <div class="unit-icon" style="--sc:${sc}">${a.icon}</div>
      <div><h2>${esc(a.name)}</h2><div class="sub">${esc(a.title)} · Lv ${a.level} · <span style="color:${sc}">${a.status}</span></div></div>
    </div>
    <div class="activity">${a.status === 'thinking' ? '💭' : '📍'} ${esc(a.activity)}</div>
    <div class="xp"><div class="bar"><i style="width:${xpPct}%"></i></div><small>${a.xp} XP · next level at ${nextLvl}</small></div>
    <dl class="kv">
      ${a.objective ? `<dt>Objective</dt><dd title="${esc(a.objective)}">${esc(a.objective)}</dd>` : ''}
      <dt>Territory</dt><dd><a class="link" data-topic="${a.topic_id}">${esc(topic?.title || a.topic_id)}</a></dd>
      <dt>Role</dt><dd>${esc(a.role)}${a.specialty ? ` · ${esc(a.specialty)}` : ''}${a.persona ? ` · ${esc(a.persona)}` : ''}</dd>
      ${a.profile ? `<dt>Profile</dt><dd><a class="link" data-profile-open="${esc(a.profile)}" title="Edit system instructions &amp; tool access">🧬 ${esc(a.profile)}</a> <small class="muted">rev ${esc(a.profile_rev || '?')}</small></dd>` : ''}
      <dt>Model</dt><dd>${modelPicker(a)}</dd>
      <dt>Reports to</dt><dd>${parent ? `<a class="link" data-agent="${parent.id}">${parent.icon} ${esc(parent.name)}</a>` : '—'}</dd>
      <dt>Tokens</dt><dd>${fmt(a.tokens)} · $${a.cost.toFixed(4)}</dd>
      <dt>Tasks done</dt><dd>${a.tasks_done}${a.veteran ? ' · 🎖️ veteran' : ''}${a.recruitments ? ` · re-deployed ${a.recruitments}×` : ''}</dd>
      <dt>Depth</dt><dd>${a.depth}</dd>
      <dt>A2A card</dt><dd><a class="link" href="${a.url}/.well-known/agent-card.json" target="_blank">agent-card.json ↗</a></dd>
      ${a.remote_url ? `<dt>Remote</dt><dd>${esc(a.remote_url)}</dd>` : ''}
    </dl>
    ${kids.length ? `<div class="small muted">Commands ${kids.length} unit(s)</div><div class="children">${kids.map((k) => `<span class="pill" data-agent="${k.id}" style="border-color:${STATUS_COLORS[k.status]}">${k.icon} ${esc(k.name)}</span>`).join('')}</div>` : ''}
    ${a.memory?.length ? `<details class="result" ${a.memory.length > 1 ? 'open' : ''}><summary>🎖️ Service record (memory carried into new orders)</summary>
      <ul class="memory-list">${a.memory.map((m) => `<li><b>${esc(m.task)}</b><br>${esc(m.result)}</li>`).join('')}</ul></details>` : ''}
    ${a.last_input ? `<details class="result"><summary>📥 Orders</summary><pre>${esc(a.last_input)}</pre></details>` : ''}
    ${a.last_output ? `<details class="result" open><summary>📤 Report</summary><pre>${esc(a.last_output)}</pre></details>` : ''}
  `;
  if (scroll) { const pre = el.querySelector('details[open] pre'); if (pre) pre.scrollTop = scroll; }
}

let modelList = []; // filled by loadModels()
const MODEL_SOURCE = { mission: 'mission model', profile: 'profile model', default: 'default' };
function modelPicker(a) {
  const shown = String(a.model || '').replace(/^litellm_proxy\//, '');
  if (a.role === 'ally') return esc(shown);
  const pinned = a.pinned_model;
  const opts = [...new Set([...modelList, ...(pinned ? [pinned] : [])])];
  const inherit = pinned ? 'inherit (unpin)' : `${shown} · ${MODEL_SOURCE[a.model_source] || a.model_source}`;
  return `<select class="model-pick${pinned ? ' pinned' : ''}" data-agent-id="${a.id}" title="Pin a model for this unit – applies from its next LLM turn and survives re-deployment">
    <option value="">${pinned ? '↩️ ' : ''}${esc(inherit)}</option>
    ${opts.map((m) => `<option value="${esc(m)}" ${m === pinned ? 'selected' : ''}>📌 ${esc(m)}</option>`).join('')}
  </select>`;
}
$('#inspector').addEventListener('change', async (e) => {
  const sel = e.target.closest('select.model-pick');
  if (!sel) return;
  const id = sel.dataset.agentId;
  try {
    const a = await api(`/api/agents/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ pinned_model: sel.value || null }) });
    store.agents.set(a.id, { ...store.agents.get(a.id), ...a });
    toast('🎛️', sel.value ? `${esc(a.name)} now uses <b>${esc(sel.value)}</b>` : `${esc(a.name)} inherits its model again`);
  } catch { /* toast shown by api() */ }
  sel.blur();
  scheduleRender();
});

$('#inspector').addEventListener('click', (e) => {
  const ag = e.target.closest('[data-agent]')?.dataset.agent;
  const tp = e.target.closest('[data-topic]')?.dataset.topic;
  const pf = e.target.closest('[data-profile-open]')?.dataset.profileOpen;
  if (ag) { select(ag); map.focusAgent(ag); }
  if (tp) map.focusTopic(tp);
  if (pf) { select(null); switchTab('agents'); profiles.open(pf); }
});
$('#unit-panel').addEventListener('click', (e) => {
  if (e.target.closest('[data-up=close]')) select(null);
});

function renderAllies() {
  const allies = [...store.agents.values()].filter((a) => a.role === 'ally');
  $('#ally-list').innerHTML = allies.map((a) => `<div class="mission" style="--c:#8e7cc3" data-agent="${a.id}">
    <div class="m-head"><b>${a.icon} ${esc(a.name)}</b><span class="chip base">${a.status}</span></div>
    <div class="m-meta">${esc(a.remote_url)}</div><div class="small muted">${a.tasks_done} tasks · Lv ${a.level}</div></div>`).join('');
}
$('#ally-list').addEventListener('click', (e) => {
  const id = e.target.closest('[data-agent]')?.dataset.agent;
  if (id) { select(id); map.focusAgent(id); }
});

function select(id) {
  if (id !== store.selected) map.yieldTo(100); // the unit panel slides in first, then the map repaints (INP)
  store.selected = id;
  map.select(id);
  $('#unit-panel').hidden = !id;
  if (id) $('#unit-panel .up-body').scrollTop = 0;
  scheduleRender();
}

function hover(agent, sx, sy) {
  const tip = $('#tooltip');
  if (!agent) { tip.hidden = true; return; }
  tip.hidden = false;
  tip.innerHTML = `<b>${agent.icon} ${esc(agent.name)}</b><br><span style="color:${STATUS_COLORS[agent.status]}">● ${agent.status}</span> · Lv ${agent.level}<br><span class="muted">${esc(agent.activity)}</span>`;
  const stage = $('#stage').getBoundingClientRect();
  tip.style.left = `${Math.min(sx + 16, stage.width - 290)}px`;
  tip.style.top = `${Math.min(sy + 16, stage.height - 90)}px`;
}

// ----------------------------------------------------------------- log
const LOG_MAX = 300;
function addLog(icon, html, level = '', agentId = null) {
  const el = $('#log');
  const row = document.createElement('div');
  row.className = `log-entry ${level || ''}`;
  if (agentId) row.dataset.agent = agentId;
  const t = new Date();
  row.innerHTML = `<time>${t.toTimeString().slice(0, 8)}</time><span>${icon}</span><span class="txt">${html}</span>`;
  el.prepend(row);
  while (el.childElementCount > LOG_MAX) el.lastElementChild.remove();
  const latest = $('#log-latest');
  latest.innerHTML = `<time>${t.toTimeString().slice(0, 5)}</time> ${icon} ${html}`;
  latest.className = `ld-latest ${level || ''}`;
}
function toggleLog(open = $('#log-dock').classList.contains('collapsed')) {
  $('#log-dock').classList.toggle('collapsed', !open);
  localStorage.setItem('al.logOpen', open ? '1' : '0');
}
$('#log-toggle').addEventListener('click', () => toggleLog());
toggleLog(localStorage.getItem('al.logOpen') === '1');
$('#log').addEventListener('click', (e) => {
  const id = e.target.closest('[data-agent]')?.dataset.agent;
  if (id && store.agents.has(id)) { select(id); map.focusAgent(id); }
});

// -------------------------------------------------------------- toasts
function toast(icon, html, kind = '', title = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="ico">${icon}</span><div>${title ? `<div class="t-title">${title}</div>` : ''}<div>${html}</div></div>`;
  const box = $('#toasts');
  box.append(el);
  while (box.children.length > 4) box.firstElementChild.remove(); // never a wall of toasts
  setTimeout(() => el.remove(), kind === 'achievement' ? 9000 : 6200);
  return el;
}

// --------------------------------------------------------- achievements
const ACHIEVEMENTS = [
  { id: 'first_mission', icon: '⚔️', name: 'To Arms!', desc: 'Launch your first mission', test: (c) => c.launched >= 1 },
  { id: 'first_win', icon: '🏆', name: 'First Victory', desc: 'Complete a mission', test: (c) => c.s.missions_completed >= 1 },
  { id: 'hive', icon: '🐝', name: 'Hive Mind', desc: 'Deploy a swarm', test: (c) => c.roles.has('swarm_leader') },
  { id: 'chain', icon: '🔗', name: 'Chain of Command', desc: 'Reach hierarchy depth 3', test: (c) => c.maxDepth >= 3 },
  { id: 'legion', icon: '🛡️', name: 'Legion', desc: '15 agents active at once', test: (c) => c.s.active_agents >= 15 },
  { id: 'warlord', icon: '🗺️', name: 'Multi-Front War', desc: '3 missions running at once', test: (c) => c.s.active_missions >= 3 },
  { id: 'diplomat', icon: '🤝', name: 'Diplomat', desc: 'Recruit an allied A2A agent', test: (c) => c.roles.has('ally') },
  { id: 'veteran', icon: '⭐', name: 'Veteran', desc: 'An agent reaches level 5', test: (c) => c.maxLevel >= 5 },
  { id: 'chatter', icon: '📨', name: 'Signal Corps', desc: 'Exchange 100 A2A messages', test: (c) => c.s.a2a_messages >= 100 },
  { id: 'tycoon', icon: '🪙', name: 'Token Tycoon', desc: 'Spend 50k tokens', test: (c) => c.s.tokens >= 50000 },
  { id: 'cartographer', icon: '🗺️', name: 'Cartographer', desc: 'HQ charts 3 topic regions', test: (c) => c.s.regions >= 3 },
  { id: 'band', icon: '🎖️', name: 'Band of Brothers', desc: 'Re-deploy a veteran unit', test: (c) => c.s.veterans_redeployed >= 1 },
  { id: 'iterate', icon: '🔁', name: 'Iterate!', desc: 'Send a mission back for a feedback round', test: (c) => c.s.feedback_rounds >= 1 },
];
const unlocked = new Set(JSON.parse(localStorage.getItem('al.achievements') || '[]'));
let achTimer = null;
function checkAchievements() {
  if (achTimer) return;
  achTimer = setTimeout(() => {
    achTimer = null;
    const agents = [...store.agents.values()];
    const c = {
      s: store.stats, launched: missionsLaunched, roles: new Set(agents.map((a) => a.role)),
      maxDepth: Math.max(0, ...agents.map((a) => a.depth)), maxLevel: Math.max(0, ...agents.map((a) => a.level)),
    };
    for (const ach of ACHIEVEMENTS) {
      if (!unlocked.has(ach.id) && ach.test(c)) {
        unlocked.add(ach.id);
        toast(ach.icon, `<b>${ach.name}</b> – ${ach.desc}`, 'achievement', 'ACHIEVEMENT UNLOCKED');
      }
    }
    localStorage.setItem('al.achievements', JSON.stringify([...unlocked]));
    renderTrophies();
  }, 300);
}
function renderTrophies() {
  $('#trophy-count').textContent = `${unlocked.size}/${ACHIEVEMENTS.length}`;
  $('#trophies').innerHTML = ACHIEVEMENTS.map((a) => `<div class="trophy ${unlocked.has(a.id) ? 'unlocked' : ''}">
    <div class="ico">${a.icon}</div><b>${a.name}</b><small>${a.desc}</small></div>`).join('');
}

// ---------------------------------------------------------------- forms
async function api(url, opts = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* ignore */ }
    toast('⚠️', esc(typeof msg === 'string' ? msg : JSON.stringify(msg)), 'error');
    throw new Error(msg);
  }
  return res.json();
}

function countLaunch() {
  missionsLaunched++;
  localStorage.setItem('al.launched', String(missionsLaunched));
  checkAchievements();
}
const composer = createComposer({ el: $('#composer'), store, api, toast, onDeployed: countLaunch });
const profiles = createProfiles({ listEl: $('#profile-list'), viewEl: $('#profile-view'), store, api, toast });
const ops = createOps({
  el: $('#ops-panel'), store, api, toast, composer,
  onSelectAgent: (id) => { select(id); map.focusAgent(id); },
  onFocusTopic: (id) => map.focusTopic(id),
});
const teams = createTeams({
  el: $('#teams-panel'), api, toast,
  onOpenReport: (id) => { switchTab('reports'); reports.open(id); map.focusTopic(id); }, // Missions › Archive
  onFocusTopic: (id) => map.focusTopic(id),
  onOpenProfile: (key) => { switchTab('agents'); profiles.open(key); },
});
$('[name=swarm_size]').addEventListener('input', (e) => {
  e.target.dataset.touched = '1';
  $('#swarm-size-out').textContent = e.target.value;
});
$('#open-composer').addEventListener('click', () => composer.open());
$('#empty-hint').addEventListener('click', () => composer.open());
$('#quick-expand').addEventListener('click', () => {
  const q = $('#quick-goal');
  composer.open(q.value.trim() ? { goal: q.value.trim() } : {});
  q.value = '';
});
$('#quick-goal').addEventListener('keydown', async (e) => {
  if (e.key !== 'Enter') return;
  const goal = e.target.value.trim();
  if (goal.length < 3) return;
  e.preventDefault();
  try {
    await api('/api/missions', { method: 'POST', body: JSON.stringify({ goal, mode: 'auto' }) });
    e.target.value = '';
    toast('⚔️', `Mission <b>${esc(goal.slice(0, 40))}</b> deployed`, 'success');
    countLaunch();
  } catch { /* toast shown */ }
});
$('#ally-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target, f = form.elements;
  try {
    const body = { url: f.namedItem('url').value.trim(), api_key: f.namedItem('api_key').value || null };
    const a = await api('/api/allies', { method: 'POST', body: JSON.stringify(body) });
    toast('🤝', `<b>${esc(a.name)}</b> joined your alliance`, 'success');
    form.reset();
  } catch { /* toast shown */ }
});
$('#copy-snippet').addEventListener('click', async () => {
  await navigator.clipboard.writeText($('#litellm-snippet').textContent);
  toast('📋', 'Copied LiteLLM config snippet');
});

// ------------------------------------------------------------ chrome
// 4 top-level tabs; Missions and Roster each hold two panes (segmented control)
const PANE_TAB = { missions: 'missions', reports: 'missions', ops: 'ops', teams: 'teams', agents: 'agents', allies: 'agents' };
const lastPane = { missions: 'missions', ops: 'ops', teams: 'teams', agents: 'agents' };
function switchTab(pane) {
  const tab = PANE_TAB[pane] || 'missions';
  lastPane[tab] = pane;
  $('#unit-panel').hidden = true; // the unit slide-over sits on top of the tabs
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.tab-body').forEach((b) => { b.hidden = b.id !== `tab-${pane}`; });
  if (pane === 'agents') profiles.load();
  if (pane === 'ops') ops.render();
  if (pane === 'teams') teams.load();
  scheduleRender();
}
document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => {
  if (store.selected) { store.selected = null; map.select(null); }
  switchTab(lastPane[t.dataset.tab]);
}));
$('#sidebar').addEventListener('click', (e) => {
  const seg = e.target.closest('.seg [data-pane]');
  if (seg) switchTab(seg.dataset.pane);
});
const MAP_KEYS = new Set(['+', '=', '-', 'f']);
// clicks / typing anywhere outside the map (sidebar, HUD popovers, log dock, overlays): let the UI's next
// frame paint before the (expensive) map frame – INP. Map / minimap input keeps the map painting.
for (const ev of ['pointerdown', 'keydown']) {
  document.addEventListener(ev, (e) => {
    // map shortcuts (+ / - / f on <body>) must keep painting, even with key repeat
    if (e.target === document.body && MAP_KEYS.has(e.key)) return;
    if (e.target.closest?.('#map, #minimap')) return;
    map.yieldTo();
  }, { capture: true, passive: true });
}
$('#map-controls').addEventListener('click', (e) => {
  const act = e.target.closest('button')?.dataset.act;
  if (act === 'zoom-in') map.zoomBy(1.25);
  if (act === 'zoom-out') map.zoomBy(0.8);
  if (act === 'fit') map.fit();
});
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && [...document.querySelectorAll('.pop')].some((p) => !p.hidden)) { closePops(); return; }
  if (e.key === 'Escape' && profiles.isOpen()) { profiles.close(); return; }
  if (e.key === 'Escape' && composer.isOpen()) { composer.close(); return; }
  if (e.target.matches('input, textarea, select')) return;
  if (e.key === 'n' && !e.metaKey && !e.ctrlKey) { e.preventDefault(); composer.open(); return; }
  if (composer.isOpen()) return;
  if (e.key === 'f') map.fit();
  if (e.key === 'l') toggleLog();
  if (e.key === 'Escape') { if (reports.isOpen()) reports.close(); else select(null); }
  if (e.key === '+' || e.key === '=') map.zoomBy(1.2);
  if (e.key === '-') map.zoomBy(0.83);
  const n = Number(e.key);
  if (n >= 1 && n <= 9) {
    const missions = [...store.topics.values()].filter((t) => t.kind === 'mission').sort((a, b) => b.created_at - a.created_at);
    if (missions[n - 1]) map.focusTopic(missions[n - 1].id);
  }
});

async function loadModels() {
  try {
    const { models, default: def } = await (await fetch('/api/models')).json();
    modelList = models || [];
    const sel = $('#composer [name=model]');
    const keep = sel.value;
    sel.innerHTML = `<option value="">default (${esc(def)})</option>`
      + '<option value="auto">🧠 Auto – orchestrator picks per subtask</option>'
      + `<optgroup label="Force for every agent">${models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join('')}</optgroup>`;
    sel.value = keep === 'auto' || models.includes(keep) ? keep : ''; // drop stale picks
    const list = $('#cp-model-pool .cp-pool-list');
    const was = new Set([...list.querySelectorAll('input:not(:checked)')].map((i) => i.value)); // keep unticks
    list.innerHTML = models.map((m) => `<label class="small" title="${m.startsWith('claude-code/') ? 'No tools – text-only work' : 'Gateway model'}">
      <input type="checkbox" name="model_pool" value="${esc(m)}" ${was.has(m) || m.startsWith('claude-code/') ? '' : 'checked'}>
      ${esc(m)}${m.startsWith('claude-code/') ? ' <em class="muted">no tools</em>' : ''}</label>`).join('');
    $('#cp-model-pool').hidden = sel.value !== 'auto';
    renderLlmPop();
  } catch { /* ignore */ }
}

renderTrophies();
connect(); // the snapshot on (re)connect also (re)loads the model list
