// Mission report viewer ("after-action report"): deliverable, inputs & feedback rounds, A2A artifacts, team.
import { renderMarkdown } from './md.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${(n / 1e3).toFixed(1)}k` : String(n ?? 0));
const dur = (s) => (s == null ? '–' : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);
const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '');
const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'mission';
const STATE = {
  completed: ['✅', '#66bb6a'], failed: ['❌', '#ef5350'], canceled: ['🏳️', '#78909c'],
  working: ['⏳', '#4fc3f7'], submitted: ['⏳', '#ffd54f'], 'input-required': ['❓', '#ffb74d'],
};
const stateIcon = (st) => (STATE[st] || ['•'])[0];
const stateColor = (st) => (STATE[st] || [, '#8a93a6'])[1];
const ROLE_LABEL = { orchestrator: 'Orchestrator', worker: 'Specialist', swarm_leader: 'Hive leader', swarm_member: 'Swarm drone', ally: 'Ally' };

export function createReportView({ el, store, api, toast, map, onInspect, onChange }) {
  let s = null; // { id, data, json, round, follow, tab, task, draft, pending }
  let timer = null;
  let loading = false;

  // ------------------------------------------------------------------ data
  async function load() {
    if (!s || loading) return;
    loading = true;
    const id = s.id;
    try {
      const res = await fetch(`/api/topics/${id}/report`);
      if (!s || s.id !== id) return;
      if (res.status === 404) { close(); return; }
      if (!res.ok) return;
      const json = await res.text();
      if (json === s.json) return;
      const data = JSON.parse(json);
      const last = data.rounds.at(-1)?.round || 1;
      if (s.follow || !data.rounds.some((r) => r.round === s.round)) s.round = last;
      s.data = data;
      s.json = json;
      render();
    } catch { /* transient – next poke retries */ } finally {
      loading = false;
    }
  }

  function open(id, tab = 'deliverable') {
    if (s && s.id === id) { setTab(tab); return; }
    s = { id, data: null, json: '', round: null, follow: true, tab, task: null, draft: '', pending: false };
    el.dataset.view = '';
    render();
    load();
    onChange?.(id);
  }

  function close() {
    s = null;
    el.hidden = true;
    el.innerHTML = '';
    onChange?.(null);
  }

  /** Called for every live event; re-fetches (throttled) while the mission is running. */
  function poke(ev) {
    if (!s) return;
    if (ev.type === 'topic_removed' && ev.topic_id === s.id) { close(); return; }
    const t = store.topics.get(s.id);
    if (!(t && t.status === 'active') && ev.topic?.id !== s.id) return;
    if (timer) return;
    timer = setTimeout(() => { timer = null; load(); }, 700);
  }

  function setTab(tab) { s.tab = tab; render(); if (tab === 'code') loadCode(); }

  /** An agent committed / changed files in a mission repo (live event). */
  let codeTimer = null;
  function repoChanged(topicId) {
    if (!s || s.id !== topicId || s.tab !== 'code' || codeTimer) return;
    codeTimer = setTimeout(() => { codeTimer = null; loadCode(); }, 600);
  }

  // ---------------------------------------------------------------- render
  function render() {
    if (!s) return;
    el.hidden = false;
    const d = s.data;
    if (!d) { el.innerHTML = '<div class="rv-loading">📑 Compiling after-action report…</div>'; return; }
    const ae = document.activeElement;
    if (el.contains(ae) && ae.matches('textarea, input')) { s.pending = true; return; } // don't wipe typing
    s.pending = false;
    const round = d.rounds.find((r) => r.round === s.round) || d.rounds.at(-1);
    s.round = round.round;
    if (!round.artifacts.some((a) => a.task_id === s.task)) s.task = round.artifacts[0]?.task_id || null;

    const view = `${s.id}|${s.tab}|${s.round}|${s.task}`;
    const scroll = {};
    if (el.dataset.view === view) {
      for (const sel of ['.rv-body', '.rv-tree', '.rv-detail']) { const n = el.querySelector(sel); if (n) scroll[sel] = n.scrollTop; }
    }
    el.style.setProperty('--c', d.topic.color);
    el.innerHTML = header(d) + nav(d, round) + body(d, round);
    for (const [sel, top] of Object.entries(scroll)) { const n = el.querySelector(sel); if (n) n.scrollTop = top; }
    el.dataset.view = view;
  }

  function header(d) {
    const t = d.topic, reg = d.region, T = d.totals;
    return `<header class="rv-head">
      <div class="rv-title">
        <div class="rv-crest">${reg?.icon || '📑'}</div>
        <div style="min-width:0"><small>MISSION REPORT${reg ? ` · ${esc(reg.name.toUpperCase())}` : ''}</small>
          <h2 title="${esc(t.goal)}">${esc(t.title)}</h2></div>
      </div>
      <div class="rv-stats">
        <span class="chip ${t.status}">${t.status}</span>
        <span class="rv-stat" title="Feedback rounds">🔁 ${T.rounds}</span>
        <span class="rv-stat" title="Units involved">🪖 ${T.units}</span>
        <span class="rv-stat" title="A2A tasks (orders + results)">📨 ${T.a2a_tasks}</span>
        <span class="rv-stat" title="Tokens">🪙 ${fmt(T.tokens)}</span>
        <span class="rv-stat" title="Cost reported by LiteLLM">💰 $${(T.cost || 0).toFixed(4)}</span>
        ${T.duration ? `<span class="rv-stat" title="Duration">⏱ ${dur(T.duration)}</span>` : ''}
      </div>
      <div class="rv-actions">
        <a class="btn small" href="/api/topics/${t.id}/report.md" download title="Full report incl. all artifacts as Markdown">⬇️ Report .md</a>
        <button class="btn small" data-rv="focus" title="Show territory on the map">🎯 Map</button>
        <button class="btn small" data-rv="close" title="Close (Esc)">✕</button>
      </div>
    </header>`;
  }

  function nav(d, round) {
    const tab = (key, label) => `<button class="rv-tab ${s.tab === key ? 'active' : ''}" data-rv="tab" data-tab="${key}">${label}</button>`;
    const rounds = d.rounds.length > 1 ? d.rounds.map((r) => `<button class="rv-round ${r.round === round.round ? 'active' : ''}" data-rv="round" data-round="${r.round}"
        title="${r.kind === 'mission' ? 'Initial mission' : 'Feedback round'} · ${r.state}">R${r.round} ${r.kind === 'mission' ? '🎯' : '💬'} ${stateIcon(r.state)}</button>`).join('') : '';
    return `<nav class="rv-nav">
      ${tab('deliverable', '📜 Deliverable')}
      ${tab('input', `📥 Input${d.rounds.length > 1 ? ` & feedback (${d.rounds.length - 1})` : ''}`)}
      ${tab('artifacts', `🧩 Artifacts (${round.artifacts.length})`)}
      ${tab('team', `👥 Team (${d.team.length})`)}
      ${repoCount(d) ? tab('code', `📦 Code (${repoCount(d)})`) : ''}
      <span class="sep"></span>${rounds}
    </nav>`;
  }

  function body(d, round) {
    if (s.tab === 'input') return inputTab(d);
    if (s.tab === 'artifacts') return artifactsTab(d, round);
    if (s.tab === 'team') return teamTab(d);
    if (s.tab === 'code') return codeTab(d);
    return deliverableTab(d, round);
  }

  // ------------------------------------------------------------------ code
  const repoCount = (d) => new Set((d.topic.context || []).filter((c) => c.kind === 'repo' && c.ok).map((c) => c.name)).size;

  async function loadCode() {
    if (!s) return;
    const id = s.id;
    try {
      const res = await fetch(`/api/topics/${id}/repos`);
      if (!s || s.id !== id || !res.ok) return;
      s.code = (await res.json()).repos;
      if (s.tab === 'code') render();
    } catch { /* retry on next event */ }
  }

  function diffHtml(diff) {
    if (!diff) return '<p class="muted small">No changes vs the base commit.</p>';
    return `<pre class="rv-diff">${diff.split('\n').map((l) => {
      const cls = l.startsWith('diff --git') ? 'f' : l.startsWith('@@') ? 'h' : l.startsWith('+') && !l.startsWith('+++') ? 'a'
        : l.startsWith('-') && !l.startsWith('---') ? 'd' : '';
      return `<span class="${cls}">${esc(l)}</span>`;
    }).join('\n')}</pre>`;
  }

  function codeTab() {
    if (!s.code) { loadCode(); return '<div class="rv-body"><div class="rv-loading">📦 Reading mission branches…</div></div>'; }
    const cards = s.code.map((r) => {
      const vs = `vscode://file${r.path.startsWith('/') ? '' : '/'}${encodeURI(r.path)}`;
      const canPush = r.write && r.commits.length && !(r.pushed && r.pushed.sha === r.commits.at(-1).sha);
      return `<section class="rv-card">
        <h4>📦 ${esc(r.name)} · ${r.write ? `✍️ branch <code>${esc(r.branch)}</code>` : '👁 read-only snapshot'}</h4>
        <dl class="kv" style="margin:0 0 10px">
          <dt>Base</dt><dd><code>${esc(r.base)}</code> @ ${esc(r.base_sha.slice(0, 8))}</dd>
          <dt>Worktree</dt><dd><code>${esc(r.path)}</code>${r.exists ? '' : ' (removed)'}</dd>
          ${r.url ? `<dt>Remote</dt><dd>${esc(r.url)}</dd>` : ''}
          ${r.pushed ? `<dt>Pushed</dt><dd>✅ ${esc(r.pushed.branch)} @ ${esc(r.pushed.sha)} · ${new Date(r.pushed.at * 1000).toLocaleString()}</dd>` : ''}
        </dl>
        <div class="rv-doc-tools" style="justify-content:flex-start">
          ${r.exists ? `<a class="btn small" href="${vs}" title="Open the mission worktree in VS Code">🧩 Open in VS Code</a>` : ''}
          ${r.branch ? `<button class="btn small" data-rv="copy-branch" data-branch="${esc(r.branch)}">📋 Branch name</button>` : ''}
          ${canPush ? `<button class="btn primary small" data-rv="push" data-repo="${esc(r.name)}" title="git push -u origin ${esc(r.branch)} – agents never push">⬆️ Push branch</button>` : ''}
          <button class="btn small" data-rv="code-refresh">⟳</button>
        </div>
        ${r.error ? `<p class="status err">${esc(r.error)}</p>` : ''}
        ${r.commits.length ? `<h4>COMMITS (${r.commits.length})</h4><ul class="rv-intel">${r.commits.map((c) => `<li>🔖 <span><code>${esc(c.sha)}</code> <b>${esc(c.message)}</b>
            <br><small>${esc(c.author)} · ${clock(c.at)} · ${c.files.length} file(s): ${c.files.slice(0, 6).map(esc).join(', ')}${c.files.length > 6 ? ' …' : ''}</small></span></li>`).join('')}</ul>`
          : `<p class="muted small">${r.write ? 'No commits yet.' : 'Read-only – agents researched this repo without changing it.'}</p>`}
        ${r.write ? `<h4 style="margin-top:12px">DIFF VS BASE ${r.changed?.length ? `(${r.changed.length} files)` : ''}</h4>${diffHtml(r.diff)}` : ''}
      </section>`;
    }).join('');
    return `<div class="rv-body"><div class="rv-doc">
      <p class="muted small">Every mission works in its own git worktree. Agents commit to the mission branch – review the diff, then push it
        (or open the worktree in VS Code to amend, test and push yourself). Dismissing the mission removes the worktree; branches with commits stay.</p>
      ${cards || '<p class="muted">No repositories attached.</p>'}
    </div></div>`;
  }

  function progress(round) {
    const done = round.artifacts.filter((a) => a.state === 'completed').length;
    return `${done} of ${round.artifacts.length} artifacts delivered`;
  }

  function deliverableTab(d, round) {
    if (!round.deliverable || round.state === 'working' || round.state === 'submitted') {
      const running = round.state === 'working' || round.state === 'submitted';
      return `<div class="rv-body"><div class="rv-empty">
        <span class="big">${running ? '⚔️' : stateIcon(round.state)}</span>
        ${running ? `The team is still working on round ${round.round}… <br><small>${progress(round)} – follow along in 🧩 Artifacts</small>`
          : `No deliverable for round ${round.round} (${esc(round.state)}).`}
        ${!running && round.deliverable ? `<pre>${esc(round.deliverable)}</pre>` : ''}
      </div></div>`;
    }
    return `<div class="rv-body"><div class="rv-doc">
      <div class="rv-doc-tools">
        <span class="muted small" style="margin-right:auto">Round ${round.round}${round.kind === 'feedback' ? ' (after feedback)' : ''} ·
          ${round.artifacts.length} artifacts · 🪙 ${fmt(round.tokens)} · ${dur(round.duration)}</span>
        <button class="btn small" data-rv="copy" data-what="deliverable">📋 Copy</button>
        <button class="btn small" data-rv="dl" data-what="deliverable">⬇️ Download</button>
      </div>
      <article class="md rv-card">${renderMarkdown(round.deliverable)}</article>
    </div></div>`;
  }

  function inputTab(d) {
    const t = d.topic, reg = d.region;
    const canFeedback = t.status !== 'active' && t.root_agent_id;
    const rounds = d.rounds.map((r) => `<li data-n="${r.round}">
        <div><b>${r.kind === 'mission' ? '🎯 Mission briefing' : '💬 Feedback'}</b> ${stateIcon(r.state)}
          <span class="when">${clock(r.started_at)}${r.duration ? ` · ${dur(r.duration)}` : ''} · 🪙 ${fmt(r.tokens)} · $${(r.cost || 0).toFixed(4)}</span>
          <button class="btn ghost small" data-rv="goto" data-round="${r.round}">📜 Result</button></div>
        <div class="md">${renderMarkdown(r.input)}</div>
      </li>`).join('');
    const kindIcon = { file: '📎', path: '📁', url: '🔗', tool: '🔌', note: '📝', store: '📚', repo: '📦' };
    const how = { created: '🆕 created by', linked: '🔗 linked by', region: '🗺️ region archive ·', recall: '🧠 recalled by', attached: '📎 attached by' };
    const knowledge = (t.knowledge || []).map((k) => `<li>📚 <span><b>${esc(k.name)}</b>
        <small>${k.files} files · ${how[k.how] || ''} ${esc(k.linked_by || '')}${k.owned ? '' : ' · read-only'}</small>
        ${k.purpose ? `<br><small>${esc(k.purpose)}</small>` : ''}
        ${k.used_by?.length ? `<br><small>used by ${k.used_by.map(esc).join(', ')}</small>` : ''}</span></li>`).join('');
    const intel = (t.context || []).map((c) => `<li class="${c.ok ? '' : 'err'}">${c.ok ? kindIcon[c.kind] || '•' : '❌'}
        <span><b>${esc(c.name)}</b> ${c.detail ? `<small>${esc(c.detail)}</small>` : ''}${c.error ? `<br><small>${esc(c.error)}</small>` : ''}</span></li>`).join('');
    return `<div class="rv-body"><div class="rv-doc">
      <section class="rv-card"><h4>🎯 OBJECTIVE</h4><div class="md">${renderMarkdown(t.goal)}</div></section>
      ${intel ? `<section class="rv-card"><h4>🧷 INTEL – ATTACHED CONTEXT</h4><ul class="rv-intel">${intel}</ul></section>` : ''}
      ${knowledge ? `<section class="rv-card"><h4>📚 KNOWLEDGE BASES</h4><ul class="rv-intel">${knowledge}</ul></section>` : ''}
      <section class="rv-card"><h4>⚙️ ORDERS</h4>
        <dl class="kv" style="margin:0">
          <dt>Formation</dt><dd>${{ auto: '🧠 Auto', hierarchy: '🏰 Hierarchy', swarm: '🐝 Swarm' }[t.mode] || esc(t.mode)}</dd>
          <dt>Region</dt><dd>${reg ? `${reg.icon} ${esc(reg.name)} · ${esc(reg.biome_label)}` : '–'}</dd>
          <dt>Started</dt><dd>${new Date(t.created_at * 1000).toLocaleString()}</dd>
          <dt>A2A context</dt><dd><code>${esc(t.id)}</code></dd>
        </dl></section>
      <section class="rv-card"><h4>🔁 ROUNDS</h4><ol class="rv-timeline">${rounds}</ol>
        ${canFeedback ? `<div class="rv-feedback">
          <textarea data-rv-draft placeholder="Feedback for round ${t.rounds + 1} – the same team re-works the deliverable with memory of what they did…">${esc(s.draft)}</textarea>
          <button class="btn primary" data-rv="feedback">🔁 Send back</button></div>`
          : t.status === 'active' ? '<p class="muted small">Feedback is possible once the current round has finished.</p>' : ''}
      </section>
    </div></div>`;
  }

  function artifactsTab(d, round) {
    if (!round.artifacts.length) {
      return `<div class="rv-body"><div class="rv-empty"><span class="big">🧩</span>No artifacts in round ${round.round} yet.</div></div>`;
    }
    const tree = round.artifacts.map((a) => `<div class="rv-node ${a.task_id === s.task ? 'sel' : ''}" data-rv="node" data-task="${a.task_id}"
        style="padding-left:${8 + a.depth * 16}px; --d:${a.depth}; --branch:${a.depth ? 'block' : 'none'}; --sc:${stateColor(a.state)}">
        <span class="ni">${a.icon}</span><b>${esc(a.agent_name)}</b><span class="dot" title="${a.state}"></span>
        <small>${esc(a.subtask || a.title || a.role)} · 🪙 ${fmt(a.tokens)}</small>
      </div>`).join('');
    const a = round.artifacts.find((x) => x.task_id === s.task) || round.artifacts[0];
    const parent = round.artifacts.find((x) => x.agent_id === a.parent_id);
    const live = store.agents.get(a.agent_id);
    const running = a.state === 'working' || a.state === 'submitted';
    return `<div class="rv-body split"><div class="rv-split">
      <div class="rv-tree">${tree}</div>
      <div class="rv-detail">
        <div class="rv-dhead">
          <div class="unit-icon" style="--sc:${stateColor(a.state)}">${a.icon}</div>
          <div><h3>${esc(a.agent_name)}</h3>
            <div class="sub">${esc(ROLE_LABEL[a.role] || a.title || a.role)}${a.level ? ` · Lv ${a.level}` : ''} · ${stateIcon(a.state)} ${esc(a.state)}
              ${parent ? ` · reports to ${parent.icon} ${esc(parent.agent_name)}` : ''}</div>
            <div class="sub">${a.profile ? `<span title="Agent profile (system instructions &amp; tool access) – ${esc(a.profile_source || '')}">🧬 ${esc(a.profile)}${a.profile_rev ? `@${esc(a.profile_rev)}` : ''}</span> · ` : ''}${a.skills_used?.length ? `🧠 skills: ${a.skills_used.map(esc).join(', ')} · ` : ''}${a.model ? `🤖 ${esc(a.model)} · ` : ''}🪙 ${fmt(a.tokens)} · $${(a.cost || 0).toFixed(4)} · ⏱ ${dur(a.duration)} · A2A task <code>${esc(a.task_id.slice(0, 8))}</code></div>
          </div>
          <div class="tools">
            ${live ? `<button class="btn small" data-rv="inspect" data-agent="${a.agent_id}" title="Show unit on the map">🎯 Unit</button>` : ''}
          </div>
        </div>
        ${a.subtask ? `<div class="activity" style="margin-bottom:12px">🎯 <b>Objective:</b> ${esc(a.subtask)}</div>` : ''}
        <details class="rv-io input">
          <summary>📥 Input – orders received <span class="sp"></span>
            <button class="btn ghost small" data-rv="copy" data-what="input">📋</button></summary>
          <div class="md">${renderMarkdown(a.input)}</div>
        </details>
        ${a.tool_calls?.length ? `<details class="rv-io tools" ${a.tool_calls.length <= 4 ? 'open' : ''}>
          <summary>🔧 Tool calls (${a.tool_calls.length}) <span class="sp"></span>
            <small class="muted">${a.tool_calls.filter((c) => !c.ok).length ? `${a.tool_calls.filter((c) => !c.ok).length} failed` : 'all ok'}</small></summary>
          <ul class="rv-tools">${a.tool_calls.map((c) => `<li class="${c.ok ? '' : 'bad'}"><details><summary>${c.ok ? '🔧' : '⚠️'} <code>${esc(c.label)}</code>
              <small class="muted">${c.ms} ms</small></summary>
              <pre>${esc(JSON.stringify(c.args, null, 2))}</pre><pre>${esc(c.result)}</pre></details></li>`).join('')}</ul>
        </details>` : ''}
        <section class="rv-io output">
          <div class="io-h">📤 Output – artifact delivered <span class="sp"></span>
            ${a.output ? `<button class="btn ghost small" data-rv="copy" data-what="output">📋 Copy</button>
            <button class="btn ghost small" data-rv="dl" data-what="output">⬇️</button>` : ''}</div>
          <div class="md">${a.output ? renderMarkdown(a.output) : `<p class="muted">${running ? '⏳ Still working…' : 'No output.'}</p>`}</div>
        </section>
      </div>
    </div></div>`;
  }

  function teamTab(d) {
    const rows = d.team.map((m) => `<tr data-rv="inspect" data-agent="${m.id}">
        <td>${m.icon} <b>${esc(m.name)}</b>${m.veteran ? ' 🎖️' : ''}</td><td>${esc(m.title)}</td><td>Lv ${m.level}</td>
        <td>${m.tasks_done}</td><td>${fmt(m.tokens)}</td><td>$${(m.cost || 0).toFixed(4)}</td>
        <td><span style="color:${stateColor(m.status === 'done' ? 'completed' : m.status)}">●</span> ${esc(m.status)}</td></tr>`).join('');
    return `<div class="rv-body"><div class="rv-doc">
      <p class="muted small">Units that worked on this mission (all rounds). Tokens & cost are lifetime totals per unit – veterans may have served other missions too.</p>
      <table class="rv-team"><thead><tr><th>Unit</th><th>Role</th><th>Level</th><th>Tasks</th><th>Tokens</th><th>Cost</th><th>Status</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7" class="muted">No units yet.</td></tr>'}</tbody></table>
    </div></div>`;
  }

  // --------------------------------------------------------------- actions
  function currentText(what) {
    const round = s.data.rounds.find((r) => r.round === s.round);
    if (what === 'deliverable') return round?.deliverable || '';
    const a = round?.artifacts.find((x) => x.task_id === s.task);
    return (what === 'input' ? a?.input : a?.output) || '';
  }

  function download(name, text) {
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
    const link = Object.assign(document.createElement('a'), { href: url, download: name });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  el.addEventListener('input', (e) => { if (e.target.matches('[data-rv-draft]') && s) s.draft = e.target.value; });
  el.addEventListener('focusout', () => setTimeout(() => { if (s?.pending) render(); }, 0));
  el.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-rv]');
    if (!btn || !s?.data) { if (btn?.dataset.rv === 'close') close(); return; }
    const act = btn.dataset.rv;
    if (act === 'copy' || act === 'dl') { e.preventDefault(); e.stopPropagation(); }
    const t = s.data.topic;
    switch (act) {
      case 'close': close(); break;
      case 'focus': map.focusTopic(t.id); break;
      case 'tab': setTab(btn.dataset.tab); break;
      case 'round': case 'goto': {
        s.round = Number(btn.dataset.round);
        s.follow = s.round === s.data.rounds.at(-1).round;
        if (act === 'goto') s.tab = 'deliverable';
        s.task = null;
        render();
        break;
      }
      case 'node': s.task = btn.dataset.task; render(); break;
      case 'inspect': onInspect?.(btn.dataset.agent); break;
      case 'code-refresh': loadCode(); break;
      case 'copy-branch':
        await navigator.clipboard.writeText(btn.dataset.branch);
        toast('📋', `Copied <code>${esc(btn.dataset.branch)}</code>`);
        break;
      case 'push': {
        if (!confirm(`Push the mission branch of ${btn.dataset.repo} to origin?`)) return;
        btn.disabled = true;
        btn.textContent = '⏳ Pushing…';
        try {
          const r = await api(`/api/topics/${t.id}/repos/${encodeURIComponent(btn.dataset.repo)}/push`, { method: 'POST' });
          toast('⬆️', `Pushed <code>${esc(r.pushed.branch)}</code> – open a pull request in your git host`, 'success');
        } catch { /* toast shown */ }
        loadCode();
        break;
      }
      case 'copy': {
        const text = currentText(btn.dataset.what);
        if (!text) return;
        await navigator.clipboard.writeText(text);
        toast('📋', `Copied ${btn.dataset.what}`);
        break;
      }
      case 'dl': {
        const what = btn.dataset.what;
        const a = s.data.rounds.find((r) => r.round === s.round)?.artifacts.find((x) => x.task_id === s.task);
        const name = what === 'deliverable' ? `${slug(t.title)}-round-${s.round}.md` : `${slug(t.title)}-${slug(a?.agent_name || 'artifact')}.md`;
        download(name, currentText(what));
        break;
      }
      case 'feedback': {
        const text = s.draft.trim();
        if (text.length < 2) { toast('💬', 'Write some feedback first', 'error'); return; }
        try {
          await api(`/api/topics/${t.id}/feedback`, { method: 'POST', body: JSON.stringify({ text }) });
          s.draft = '';
          s.follow = true;
          s.tab = 'artifacts';
          document.activeElement?.blur();
          toast('🔁', 'Sent back to the team – watch the artifacts arrive', 'success');
          setTimeout(load, 400);
        } catch { /* toast shown by api() */ }
        break;
      }
    }
  });

  return { open, close, poke, repoChanged, isOpen: () => !!s, openId: () => s?.id || null };
}
