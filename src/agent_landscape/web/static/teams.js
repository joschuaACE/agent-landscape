// 👥 Teams: saved lead + specialists + repos + standing orders (teams.yaml) – run now, on request or on a schedule.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ago = (t) => {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? 'just now' : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : new Date(t * 1000).toLocaleDateString();
};
const until = (t) => {
  const s = Math.max(0, t - Date.now() / 1000);
  const when = new Date(t * 1000);
  const label = s < 3600 ? `in ${Math.max(1, Math.round(s / 60))}m` : s < 86400 ? `in ${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m` : `in ${Math.round(s / 86400)}d`;
  return `${label} · ${when.toLocaleDateString(undefined, { weekday: 'short' })} ${when.toTimeString().slice(0, 5)}`;
};
const TRIGGER = { schedule: '⏰', 'catch-up': '⏰↻', manual: '▶️' };

export function createTeams({ el, api, toast, onOpenReport, onFocusTopic, onOpenProfile }) {
  let data = null;
  let loading = null;
  const drafts = new Map(); // team key → prompt draft (survives re-renders)
  const implDrafts = new Map(); // topic id → "F1, F3"

  async function load() {
    loading ??= api('/api/teams').then((d) => { data = d; render(); }).catch(() => {}).finally(() => { loading = null; });
    return loading;
  }

  function profilePill(p, lead = false) {
    const marks = `${p.write ? ' ✍️' : ''}${p.checks ? ' 🧪' : ''}`;
    return `<span class="pill${lead ? ' lead' : ''}${p.missing ? ' missing' : ''}" data-profile-open="${esc(p.key)}" title="${p.missing ? 'Profile not found' : `Open profile ${esc(p.key)}`}">${p.icon} ${esc(p.title)}${marks}</span>`;
  }

  function runRow(t, r) {
    const done = r.status === 'completed';
    const impl = /\bF\d/.test(t.goal); // standing orders ask for numbered findings → approve them here
    return `<div class="team-run" data-topic="${esc(r.topic_id)}">
      <span class="chip ${esc(r.status)}">${esc(r.status)}</span>
      <a class="link" data-report="${esc(r.topic_id)}" title="Open the report">${esc(r.title)}</a>
      <small class="muted" title="${esc(r.prompt || '')}">${TRIGGER[r.trigger] || ''} ${ago(r.at)}${r.rounds > 1 ? ` · ${r.rounds} rounds` : ''}</small>
      ${done && impl ? `<form class="team-impl" data-impl="${esc(r.topic_id)}">
        <input name="findings" placeholder="F1, F3" value="${esc(implDrafts.get(r.topic_id) || '')}" maxlength="400" title="Findings to implement (numbers from the report)">
        <button class="btn small" title="Approve: a feedback round makes the repo editable and the integrator implements + tests these findings">🔩 Implement</button></form>` : ''}
    </div>`;
  }

  function teamCard(t) {
    const sched = t.schedule
      ? `<span class="team-sched ${t.paused ? 'paused' : ''}" title="cron: ${esc(t.schedule)} (local time)">⏰ ${esc(t.schedule_label)}${t.paused ? ' · paused' : t.next_run ? ` · next ${until(t.next_run)}` : ''}</span>
         <button class="btn small" data-pause="${esc(t.key)}" data-paused="${t.paused ? '1' : ''}">${t.paused ? '▶ Resume' : '⏸ Pause'}</button>`
      : '<span class="team-sched">💬 on request</span>';
    const repos = t.repos.map((r) => `<span class="pill repo">${r.write ? '✍️' : '👁'} ${esc(r.repo)}</span>`).join('');
    const busy = t.active_topic;
    return `<div class="panel team" data-team="${esc(t.key)}">
      <div class="panel-head"><h3>${t.icon} ${esc(t.title)}</h3><span class="team-head-r">${sched}</span></div>
      ${t.description ? `<p class="muted small">${esc(t.description)}</p>` : ''}
      <div class="team-members">${profilePill(t.lead_profile, true)}<span class="muted">→</span>${t.member_profiles.map((p) => profilePill(p)).join('') || '<small class="muted">all specialists</small>'}${repos}</div>
      <form class="team-run-form" data-run="${esc(t.key)}">
        <textarea name="prompt" rows="${t.on_request ? 3 : 1}" maxlength="8000" placeholder="${esc(t.on_request ? (t.prompt_hint || 'What should the team do?') : 'Extra instructions for this run (optional)')}">${esc(drafts.get(t.key) || '')}</textarea>
        <button class="btn ${t.on_request ? 'primary' : ''}" ${busy ? 'disabled title="A run of this team is active"' : ''}>${busy ? '⏳ Running…' : t.on_request ? '🚀 Deploy team' : '▶ Run now'}</button>
      </form>
      ${t.runs.length ? `<div class="team-runs">${t.runs.map((r) => runRow(t, r)).join('')}</div>` : '<p class="muted small">No runs yet.</p>'}
    </div>`;
  }

  function render() {
    if (!data || el.closest('.tab-body')?.hidden) return;
    if (el.contains(document.activeElement) && document.activeElement.matches('textarea, input')) return; // don't eat typing
    if (!data.enabled) {
      el.innerHTML = '<p class="muted">Teams are off. Set <code>TEAMS_FILE=teams.yaml</code> in <code>.env</code>.</p>';
      return;
    }
    const areas = new Map();
    for (const t of data.teams) (areas.get(t.area) || areas.set(t.area, []).get(t.area)).push(t);
    el.innerHTML = `
      <p class="muted small">Saved teams from <code title="${esc(data.file)}">teams.yaml</code> (edit it in VS Code – reloads automatically).
        Scheduler ${data.scheduler ? `<b>on</b> · missed runs are caught up for ${data.catch_up_hours}h` : '<b>off</b> (TEAMS_SCHEDULER)'}.</p>
      ${data.errors.map((e) => `<p class="pk-note err">⚠️ ${esc(e)}</p>`).join('')}
      ${[...areas.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([area, teams]) => `<h4 class="team-area">${esc(area)}</h4>${teams.map(teamCard).join('')}`).join('')
        || '<p class="muted">No teams defined yet.</p>'}`;
  }

  el.addEventListener('input', (e) => {
    const run = e.target.closest('[data-run]');
    if (run) drafts.set(run.dataset.run, e.target.value);
    const impl = e.target.closest('[data-impl]');
    if (impl) implDrafts.set(impl.dataset.impl, e.target.value);
  });

  el.addEventListener('submit', async (e) => {
    e.preventDefault();
    const run = e.target.closest('[data-run]');
    const impl = e.target.closest('[data-impl]');
    try {
      if (run) {
        const key = run.dataset.run;
        const prompt = run.elements.prompt.value.trim();
        run.querySelector('button').disabled = true;
        const topic = await api(`/api/teams/${encodeURIComponent(key)}/run`, { method: 'POST', body: JSON.stringify({ prompt: prompt || null }) });
        drafts.delete(key);
        toast('👥', `<b>${esc(topic.title)}</b> deployed`, 'success');
        onFocusTopic(topic.id);
      } else if (impl) {
        const teamKey = impl.closest('[data-team]').dataset.team;
        const findings = impl.elements.findings.value.trim();
        if (!findings) { toast('⚠️', 'Enter the finding numbers, e.g. F1, F3', 'error'); return; }
        await api(`/api/teams/${encodeURIComponent(teamKey)}/runs/${encodeURIComponent(impl.dataset.impl)}/implement`, { method: 'POST', body: JSON.stringify({ findings }) });
        implDrafts.delete(impl.dataset.impl);
        toast('🔩', `Approved <b>${esc(findings)}</b> – the integrator is on it`, 'success');
        onFocusTopic(impl.dataset.impl);
      }
    } catch { /* toast shown by api() */ }
    document.activeElement?.blur();
    load();
  });

  el.addEventListener('click', async (e) => {
    const pause = e.target.closest('[data-pause]');
    const rep = e.target.closest('[data-report]');
    const prof = e.target.closest('[data-profile-open]');
    if (prof) onOpenProfile(prof.dataset.profileOpen);
    if (pause) {
      try {
        await api(`/api/teams/${encodeURIComponent(pause.dataset.pause)}/pause`, { method: 'POST', body: JSON.stringify({ paused: !pause.dataset.paused }) });
      } catch { /* toast shown */ }
      load();
    }
    if (rep) { e.preventDefault(); onOpenReport(rep.dataset.report); }
  });

  return { load, render };
}
