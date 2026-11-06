// Operations team: always-on, read-only monitors (Outlook, Teams) → Communication Orchestrator → Commander
// → mission proposals that wait for the human's approval.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const CH = { outlook: '📧', teams: '💬' };
const CH_LABEL = { outlook: 'Outlook', teams: 'Teams' };
const PRIO = { high: ['🔴', 'High priority'], normal: ['🟡', 'Normal priority'], low: ['⚪', 'Low priority'] };
const ago = (t) => {
  if (!t) return '—';
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : new Date(t * 1000).toLocaleDateString();
};
const sender = (s) => String(s || '').split('<')[0].trim();

export function createOps({ el, store, api, toast, composer, onSelectAgent, onFocusTopic }) {
  let feedFilter = 'relevant';
  let lastSig = '';

  const ops = () => store.ops || { enabled: false, monitors: [], proposals: [], feed: [] };
  const pending = () => ops().proposals.filter((p) => p.status === 'pending');

  /** Live updates from the websocket. Returns a newly created proposal (for a toast) or null. */
  function apply(ev) {
    const o = store.ops;
    if (ev.type === 'ops_polled') { store.ops = ev.snapshot; return null; }
    if (!o) return null;
    if (ev.type === 'ops_message') {
      o.feed = [...o.feed.filter((e) => e.id !== ev.entry.id), ev.entry].slice(-60);
      for (const m of o.monitors) Object.assign(m, ev.stats?.[m.agent_id] || {});
      return null;
    }
    if (ev.type === 'ops_proposal') {
      const p = ev.proposal;
      o.proposals = [p, ...o.proposals.filter((x) => x.id !== p.id)].sort((a, b) => b.created_at - a.created_at);
      for (const e of o.feed) if (p.messages.some((m) => m.id === e.id)) e.proposal_id = p.id;
      return ev.created ? p : null;
    }
    return null;
  }

  // ----------------------------------------------------------------- render
  function teamHtml(o) {
    const lead = store.agents.get(o.lead_id);
    const row = (a, extra) => a ? `<div class="ops-unit" data-agent="${a.id}">
        <span class="ops-ico ${a.status}">${a.icon}</span>
        <span class="ops-name"><b>${esc(a.name)}</b><small>${esc(a.activity)}</small></span>${extra || ''}</div>` : '';
    const state = !o.enabled ? '<span class="chip canceled">off</span>'
      : o.mock ? '<span class="chip interrupted" title="Simulated Outlook / Teams feed">mock</span>'
      : `<span class="chip completed" title="${esc(o.folder || '')}">${o.folder ? '📁 OneDrive' : 'live'}</span>`;
    return `<div class="panel ops-team">
      <div class="panel-head"><h3>📡 Operations Center</h3><span class="ops-head-r">${o.running ? `<button class="btn small" data-refresh title="Check the ${o.folder ? 'OneDrive folder' : 'sources'} now instead of waiting for the next poll">🔄 Refresh</button>` : ''}${state}</span></div>
      <p class="muted small">Always on – not started by the Commander. Monitors only <b>read</b> Outlook and Teams;
        nobody replies or changes anything. Relevant messages become mission proposals that wait for you.</p>
      ${row(lead)}
      <div class="ops-monitors">${o.monitors.map((m) => row(store.agents.get(m.agent_id),
        `<span class="ops-stats" title="last poll ${esc(ago(m.last_poll))}${m.errors ? ` · ${m.errors} errors` : ''}${m.source ? `\n${esc(m.source.dirs.join('\n') || 'folder missing')}${m.source.errors.length ? `\nnot readable yet: ${esc(m.source.errors.join('; '))}` : ''}` : ''}">${m.source ? `${m.source.files} files · ` : ''}${m.seen} seen · ${m.forwarded} fwd</span>`)).join('')}</div>
      ${o.folder?.startsWith('⏳') ? `<p class="pk-note err">⏳ <b>AgentInbox</b> isn't on this Mac yet. In Finder (OneDrive) right-click it → <b>Always Keep on This Device</b>, or set <code>OPS_INBOX_DIR</code>. Monitoring starts automatically once it's there.</p>`
        : o.folder ? `<p class="muted small ops-path" title="${esc(o.folder)}">📁 ${esc(o.folder.replace(/^.*CloudStorage\//, ''))} · read-only</p>` : ''}
      ${o.error ? `<p class="pk-note err">⚠️ ${esc(o.error)}</p>` : ''}
      ${o.monitors.filter((m) => m.source?.errors?.length).map((m) => `<p class="pk-note err">⚠️ <b>${esc(m.label)}</b>: ${m.source.errors.length} file(s) can't be read<br><small>${esc(m.source.errors[0])}</small></p>`).join('')}
      ${!o.enabled ? '<p class="muted small">Enable it in <code>.env</code>: <code>OPS_SOURCE=folder</code> (Power Automate JSON in OneDrive/AgentInbox) or <code>OPS_SOURCE=mock</code>.</p>' : ''}
      ${o.mock ? `<div class="ops-sim"><small class="muted">Simulate an incoming message:</small>
        <div class="ops-sim-row">
          <button class="btn small" data-sim="outlook" data-rel="1" title="A mail with a concrete request">📧 Request mail</button>
          <button class="btn small" data-sim="teams" data-rel="1" title="A Teams message with a concrete request">💬 Request msg</button>
          <button class="btn small ghost" data-sim="outlook" data-rel="0" title="Newsletter, notification …">📧 Noise</button>
          <button class="btn small ghost" data-sim="teams" data-rel="0" title="Small talk, emoji …">💬 Noise</button>
        </div></div>` : ''}
    </div>`;
  }

  function regionLabel(p) {
    if (p.kind === 'followup') {
      const t = store.topics.get(p.followup_topic_id);
      return `<span class="chip round" title="Continues an existing mission">🔁 feedback · ${esc(t?.title || 'mission')}</span>`;
    }
    const reg = store.regions.get(p.region_id);
    if (reg) return `<span class="ops-region" title="${esc(reg.description)}">${reg.icon} ${esc(reg.name)}</span>`;
    if (p.new_region) {
      const b = (store.config.biomes || []).find((x) => x.key === p.new_region.biome);
      return `<span class="ops-region new" title="${esc(p.new_region.description)}">✨ new region: ${b ? b.icon : ''} ${esc(p.new_region.name)}</span>`;
    }
    return '<span class="ops-region">🧭 HQ decides</span>';
  }

  function proposalCard(p) {
    const [pi, pt] = PRIO[p.priority] || PRIO.normal;
    return `<div class="ops-prop prio-${p.priority}" data-prop="${p.id}">
      <div class="m-head"><span title="${pt}">${pi}</span><b title="${esc(p.title)}">${esc(p.title)}</b>
        <small class="muted">${esc(ago(p.created_at))}</small></div>
      <div class="ops-where">${regionLabel(p)} <span class="muted small">· ${p.mode}</span></div>
      ${p.rationale ? `<div class="ops-why">${esc(p.rationale)}</div>` : ''}
      <ul class="ops-msgs">${p.messages.slice(-4).map((m) => `<li title="${esc(m.preview)}">${CH[m.channel] || '📨'} <b>${esc(sender(m.sender))}</b>
        <span class="muted">${esc(m.subject || m.preview)}</span></li>`).join('')}${p.messages.length > 4 ? `<li class="muted">+ ${p.messages.length - 4} earlier message(s)</li>` : ''}</ul>
      <details data-key="goal-${p.id}"><summary>📜 Pre-filled briefing${p.note ? ' &amp; intel note' : ''}</summary>
        <pre>${esc(p.goal)}</pre>${p.note ? `<div class="small muted" style="margin-top:6px">📡 Communication Orchestrator:</div><pre>${esc(p.note)}</pre>` : ''}</details>
      <div class="m-actions">
        ${p.kind === 'followup'
          ? `<button class="btn small ok" data-act="feedback" title="The mission's team re-works its deliverable with this input">🔁 Approve as feedback</button>
             <button class="btn small" data-act="review" title="Open as a new mission briefing instead">📜 New mission…</button>`
          : `<button class="btn small ok" data-act="review" title="Opens the pre-filled briefing – edit, then deploy">✅ Review &amp; deploy</button>`}
        <button class="btn small ghost" data-act="dismiss">🗑️ Dismiss</button>
      </div>
    </div>`;
  }

  function historyHtml(o) {
    const done = o.proposals.filter((p) => p.status !== 'pending').slice(0, 15);
    if (!done.length) return '';
    return `<details class="ops-history" data-key="history"><summary>🗂️ Decided (${done.length})</summary>${done.map((p) => {
      const t = store.topics.get(p.topic_id);
      return `<div class="ops-hist ${p.status}">${p.status === 'approved' ? '✅' : '🗑️'} <b>${esc(p.title)}</b>
        ${t ? `<a class="link" data-topic="${t.id}">→ ${esc(t.title)}</a>` : `<small class="muted">${p.status}</small>`}</div>`;
    }).join('')}</details>`;
  }

  function feedHtml(o) {
    const list = o.feed.filter((e) => feedFilter === 'all' || e.relevant).slice().reverse().slice(0, 40);
    return `<div class="panel ops-feed">
      <div class="panel-head"><h3>📥 Seen messages</h3>
        <select data-act="filter" class="ops-filter"><option value="relevant" ${feedFilter === 'relevant' ? 'selected' : ''}>relevant</option><option value="all" ${feedFilter === 'all' ? 'selected' : ''}>all</option></select></div>
      ${list.length ? list.map((e) => `<div class="ops-entry ${e.relevant ? 'rel' : 'noise'}" title="${esc(e.reason || '')}${e.fallback ? ' (keyword rules)' : ''}">
        <span class="ops-ch">${CH[e.channel] || '📨'}</span>
        <div><div class="ops-line"><b>${esc(sender(e.sender))}</b> <small class="muted">${esc(e.location || '')} · ${esc(ago(e.received_at))}</small></div>
          <div class="ops-text">${esc(e.subject || e.preview)}</div>
          ${e.relevant ? `<small class="ops-sum">→ ${esc(e.summary || '')}${e.proposal_id ? ' · 📨 proposal' : ''}</small>` : ''}</div>
        <span class="chip ${e.relevant ? 'completed' : 'canceled'}" title="${esc(e.reason || '')}">${e.relevant ? (PRIO[e.priority] || PRIO.normal)[0] : e.ignored ? 'ignored' : 'noise'}</span></div>`).join('')
        : `<p class="muted small">${o.enabled ? `Nothing ${feedFilter === 'relevant' ? 'relevant ' : ''}yet – the monitors are listening.` : 'The operations team is off.'}</p>`}
    </div>`;
  }

  function render() {
    if (el.contains(document.activeElement) && document.activeElement.matches('select')) return;
    const o = ops();
    const team = [o.lead_id, ...o.monitors.map((m) => m.agent_id)].map((id) => store.agents.get(id))
      .map((a) => a && `${a.status}|${a.activity}`);
    const sig = JSON.stringify([feedFilter, o.enabled, team, o.monitors.map((m) => [m.seen, m.forwarded, m.source?.files, m.source?.errors?.length]),
      o.proposals.map((p) => [p.id, p.status, p.updated_at]), o.feed.length && o.feed[o.feed.length - 1].id,
      o.feed.map((e) => e.proposal_id), Math.floor(Date.now() / 30000)]);
    if (sig === lastSig) return;
    lastSig = sig;
    const opened = new Set([...el.querySelectorAll('details[open]')].map((d) => d.dataset.key));
    const scroll = el.scrollTop;
    const open = pending();
    el.innerHTML = teamHtml(o)
      + `<div class="ops-pending"><h4 class="ops-h">📨 AWAITING YOUR APPROVAL <em>${open.length || ''}</em></h4>
        ${open.length ? open.map(proposalCard).join('') : '<p class="muted small">No proposals right now. The Commander drafts one as soon as a monitor reports a relevant message.</p>'}</div>`
      + historyHtml(o) + feedHtml(o);
    el.querySelectorAll('details[data-key]').forEach((d) => { if (opened.has(d.dataset.key)) d.open = true; });
    el.scrollTop = scroll;
  }

  // ---------------------------------------------------------------- actions
  function review(p) {
    composer.open({
      goal: p.goal, title: p.title, mode: p.mode, region_id: p.region_id || '', items: p.intel || [],
      proposal: { id: p.id, title: p.title, new_region: p.new_region },
    });
  }

  el.addEventListener('change', (e) => {
    if (e.target.dataset.act === 'filter') { feedFilter = e.target.value; e.target.blur(); render(); }
  });
  async function refresh(btn) {
    btn.disabled = true;
    btn.textContent = '⏳ Checking…';
    try {
      const { channels } = await api('/api/ops/refresh', { method: 'POST' });
      const rows = Object.entries(channels);
      const total = rows.reduce((s, [, c]) => s + (c.new || 0), 0);
      const waiting = rows.find(([, c]) => c.waiting);
      const detail = rows.map(([ch, c]) => `${CH[ch] || '📨'} ${c.error ? `⚠️ ${esc(c.error)}` : `${c.new} new${c.files != null ? ` of ${c.files} files` : ''}${c.unreadable ? ` · ⚠️ ${c.unreadable} unreadable` : ''}`}`).join(' · ');
      if (waiting) toast('⏳', `AgentInbox is not on this Mac yet – nothing to read. ${detail}`, 'error');
      else toast(total ? '📨' : '✅', total ? `<b>${total} new message${total > 1 ? 's' : ''}</b> – the monitors are triaging them<br><small>${detail}</small>`
        : `No new messages<br><small>${detail}</small>`, total ? 'success' : '');
    } catch { /* toast shown */ } finally {
      btn.disabled = false;
      btn.textContent = '🔄 Refresh';
    }
  }

  el.addEventListener('click', async (e) => {
    const rf = e.target.closest('[data-refresh]');
    if (rf) { refresh(rf); return; }
    const sim = e.target.closest('[data-sim]');
    if (sim) {
      sim.disabled = true;
      try {
        const m = await api('/api/ops/simulate', { method: 'POST', body: JSON.stringify({ channel: sim.dataset.sim, relevant: sim.dataset.rel === '1' }) });
        toast(CH[m.channel], `New ${CH_LABEL[m.channel]} message from <b>${esc(sender(m.sender))}</b>`, '', 'SIMULATED');
      } catch { /* toast shown */ } finally { setTimeout(() => { sim.disabled = false; }, 400); }
      return;
    }
    const topic = e.target.closest('[data-topic]')?.dataset.topic;
    if (topic) { onFocusTopic(topic); return; }
    const card = e.target.closest('[data-prop]');
    const act = e.target.closest('[data-act]')?.dataset.act;
    if (card && act) {
      const p = ops().proposals.find((x) => x.id === card.dataset.prop);
      if (!p) return;
      if (act === 'review') review(p);
      else if (act === 'dismiss') {
        try { await api(`/api/ops/proposals/${p.id}/dismiss`, { method: 'POST' }); } catch { /* toast shown */ }
      } else if (act === 'feedback') {
        try {
          await api(`/api/ops/proposals/${p.id}/feedback`, { method: 'POST', body: JSON.stringify({ text: p.goal, context: p.intel || [] }) });
          toast('🔁', `Feedback sent – the team of the mission is back at work`, 'success');
        } catch { /* toast shown */ }
      }
      return;
    }
    const agent = e.target.closest('[data-agent]')?.dataset.agent;
    if (agent) onSelectAgent(agent);
  });

  return { render, apply, pending, review };
}
