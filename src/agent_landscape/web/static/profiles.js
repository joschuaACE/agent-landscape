// Agent profiles (system instructions, prompts, model, tool access) + repo skills – list and editor.
import { renderMarkdown } from './md.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const KIND_LABEL = { orchestrator: '🏰 Orchestrators', worker: '🧑‍🔧 Specialists', swarm_leader: '🌀 Hive leaders', swarm_member: '🐝 Swarm personas', ops_lead: '📡 Operations leads', monitor: '👂 Channel monitors' };
const KIND_HINT = {
  orchestrator: 'Plans missions and merges results. Edit the persona, the plan template ({max_subtasks}) and the synthesis prompt.',
  worker: 'Executes subtasks. Orchestrators pick specialists by their description – keep it short and specific.',
  swarm_leader: 'Releases a swarm and builds the consensus.',
  swarm_member: 'One perspective in a swarm. A hive picks personas at random.',
  ops_lead: 'Always-on lead of the operations team: turns monitor signals into an intel note for the Commander. Read-only, no tools.',
  monitor: 'Always-on, read-only watcher of one channel (key <code>&lt;channel&gt;-monitor</code>). The instructions define what counts as relevant. No tools.',
};
const SOURCE = { builtin: ['built-in', ''], override: ['override', 'warn'], repo: ['custom', 'ok'] };

export function createProfiles({ listEl, viewEl, store, api, toast }) {
  let data = null; // /api/profiles
  let skills = null; // /api/skills
  let tools = null; // /api/tools
  let models = [];
  let editing = null; // { profile, isNew }

  async function load() {
    try {
      [data, skills] = await Promise.all([(await fetch('/api/profiles')).json(), (await fetch('/api/skills')).json()]);
    } catch { return; }
    renderList();
  }

  // ------------------------------------------------------------- list
  function usage(key) { return [...store.agents.values()].filter((a) => a.profile === key).length; }

  function renderList() {
    if (!data) { listEl.innerHTML = '<p class="muted small">Loading…</p>'; return; }
    const byKind = {};
    data.profiles.forEach((p) => (byKind[p.kind] ||= []).push(p));
    const row = (p) => {
      const [label, cls] = SOURCE[p.source] || ['?', ''];
      const n = usage(p.key);
      return `<div class="pf-row ${p.enabled ? '' : 'off'}" data-profile="${esc(p.key)}">
        <span class="ico">${esc(p.icon)}</span>
        <div class="pf-main"><b>${esc(p.title)}</b> <code>${esc(p.key)}</code>
          <small>${esc(p.description || p.prompt.slice(0, 90))}</small></div>
        <div class="pf-badges">
          <span class="pill ${cls}">${label}</span>
          ${p.model ? `<span class="pill" title="Model">🧠 ${esc(p.model)}</span>` : ''}
          <span class="pill" title="Tool families: ${esc(p.tools.join(', ') || 'none')}">🔧 ${p.tools.length}</span>
          ${p.grant_mcp.length ? `<span class="pill" title="Granted MCP: ${esc(p.grant_mcp.join(', '))}">🔌 ${p.grant_mcp.length}</span>` : ''}
          ${n ? `<span class="pill" title="Agents on the map using it">🪖 ${n}</span>` : ''}
          ${p.enabled ? '' : '<span class="pill">disabled</span>'}
        </div></div>`;
    };
    const skillRows = (skills?.skills || []).map((s) => `<div class="pf-row" data-skill="${esc(s.name)}">
        <span class="ico">🧠</span><div class="pf-main"><b>${esc(s.name)}</b>
        <small>${esc(s.description)}</small></div>
        <div class="pf-badges">${s.files.length ? `<span class="pill">📄 ${s.files.length + 1}</span>` : ''}</div></div>`).join('');
    const errors = [...(data.errors || []), ...(skills?.errors || [])];
    listEl.innerHTML = `
      <div class="panel pf-intro">
        <p class="small muted" style="margin:0 0 8px">Each agent type is a <b>profile</b>: system instructions, prompt templates,
          model and tool access. Saved as Markdown in <code>${esc(data.dir || '(read-only)')}</code> – versioned in git.
          Changes apply to the next LLM turn.</p>
        <button class="btn primary" data-pf="new" ${data.editable ? '' : 'disabled'}>➕ New profile</button>
      </div>
      ${errors.length ? `<div class="panel pf-errors"><b>⚠️ ${errors.length} invalid file${errors.length > 1 ? 's' : ''}</b>
        ${errors.map((e) => `<div class="small"><code>${esc(e.path.split('/').slice(-2).join('/'))}</code> – ${esc(e.error)}</div>`).join('')}</div>` : ''}
      ${data.kinds.map((k) => byKind[k]?.length ? `<div class="pf-group"><div class="pf-ghead">${KIND_LABEL[k]}</div>${byKind[k].map(row).join('')}</div>` : '').join('')}
      <div class="pf-group"><div class="pf-ghead">🧠 Skills <small class="muted">from ${esc((skills?.roots || []).map((r) => r.split('/').slice(-2).join('/')).join(', '))}</small></div>
        ${skillRows || `<p class="small muted">No skills yet. Add a folder <code>skills/&lt;name&gt;/SKILL.md</code> (frontmatter <code>name</code> + <code>description</code>, body = instructions).</p>`}
      </div>`;
  }

  listEl.addEventListener('click', (e) => {
    if (e.target.closest('[data-pf=new]')) { openNew(); return; }
    const key = e.target.closest('[data-profile]')?.dataset.profile;
    if (key) { open(key); return; }
    const skill = e.target.closest('[data-skill]')?.dataset.skill;
    if (skill) openSkill(skill);
  });

  // ----------------------------------------------------------- editor
  async function ensureMeta() {
    if (!tools) { try { tools = await (await fetch('/api/tools')).json(); } catch { tools = { servers: [] }; } }
    if (!models.length) { try { models = (await (await fetch('/api/models')).json()).models; } catch { models = []; } }
  }

  async function open(key) {
    if (!data) await load();
    const p = data?.profiles.find((x) => x.key === key);
    if (!p) return;
    await ensureMeta();
    editing = { profile: structuredClone(p), isNew: false };
    renderEditor();
  }

  async function openNew(base = null) {
    await ensureMeta();
    const tpl = base || data.profiles.find((x) => x.key === 'researcher') || data.profiles[0];
    editing = { profile: { ...structuredClone(tpl), key: '', title: base ? `${tpl.title} (copy)` : 'New specialist',
      source: 'repo', path: null, enabled: true, description: base ? tpl.description : '' }, isNew: true };
    renderEditor();
  }

  function close() { editing = null; viewEl.hidden = true; viewEl.innerHTML = ''; }

  function renderEditor() {
    const p = editing.profile;
    const isNew = editing.isNew;
    const [label] = SOURCE[p.source] || ['?'];
    const fam = data.families;
    const servers = tools?.servers || [];
    const skillList = skills?.skills || [];
    const skillMode = p.skills.includes('*') ? 'all' : p.skills.length ? 'some' : 'none';
    const hasPlan = p.kind === 'orchestrator';
    const hasSynth = p.kind === 'orchestrator' || p.kind === 'swarm_leader';
    const n = usage(p.key);
    viewEl.hidden = false;
    viewEl.style.setProperty('--c', '#b388ff');
    viewEl.innerHTML = `<form id="pf-form" autocomplete="off">
      <header class="rv-head">
        <div class="rv-title"><div class="rv-crest"><input name="icon" class="pf-icon" value="${esc(p.icon)}" maxlength="4" title="Icon (emoji)"></div>
          <div style="min-width:0;flex:1"><small>AGENT PROFILE · ${esc(KIND_LABEL[p.kind] || p.kind).toUpperCase()}</small>
            <input name="title" class="pf-title" value="${esc(p.title)}" maxlength="40" required></div></div>
        <div class="rv-stats">
          ${isNew ? '<span class="rv-stat">new</span>' : `<span class="rv-stat">${label}</span><span class="rv-stat" title="Content revision – shown in mission reports">rev ${esc(p.rev)}</span>`}
          ${n ? `<span class="rv-stat">🪖 ${n} on the map</span>` : ''}
        </div>
        <div class="rv-actions"><button type="button" class="btn small" data-pf="close">✕</button></div>
      </header>
      <div class="cp-body pf-body">
        <div class="cp-main">
          <div class="row">
            <label class="pf-field">Key <input name="key" value="${esc(p.key)}" ${isNew ? '' : 'readonly'} pattern="[a-z0-9][a-z0-9-]{0,47}" required placeholder="e.g. copilot-studio-expert"></label>
            <label class="pf-field">Kind <select name="kind" ${isNew ? '' : 'disabled'}>${data.kinds.map((k) => `<option value="${k}" ${k === p.kind ? 'selected' : ''}>${KIND_LABEL[k]}</option>`).join('')}</select></label>
          </div>
          <label class="pf-field">Description <small class="muted">– ${p.kind === 'worker' ? 'orchestrators choose specialists by this' : 'shown in lists'}</small>
            <input name="description" value="${esc(p.description)}" maxlength="400"></label>
          <label class="pf-field grow">System instructions
            <textarea name="prompt" required>${esc(p.prompt)}</textarea></label>
          ${hasPlan ? `<label class="pf-field">Plan template <small class="muted">– placeholder {max_subtasks}; specialists, strategies &amp; JSON format are appended automatically</small>
            <textarea name="p_plan" rows="3">${esc(p.prompts.plan || '')}</textarea></label>` : ''}
          ${hasSynth ? `<label class="pf-field">Synthesis prompt <small class="muted">– how results are merged</small>
            <textarea name="p_synthesize" rows="3">${esc(p.prompts.synthesize || '')}</textarea></label>` : ''}
        </div>
        <aside class="cp-side">
          <h4>MODEL</h4>
          <select name="model"><option value="">inherit (mission / default)</option>
            ${[...new Set([...(models || []), ...(p.model ? [p.model] : [])])].map((m) => `<option ${m === p.model ? 'selected' : ''}>${esc(m)}</option>`).join('')}</select>
          <label class="slider" title="Empty = default">Temperature <input type="number" name="temperature" min="0" max="2" step="0.1" value="${p.temperature ?? ''}" placeholder="0.7" style="width:70px;margin:0"></label>
          <label class="slider" title="Tool-call rounds per answer – empty = MAX_TOOL_STEPS">Tool steps <input type="number" name="max_tool_steps" min="1" max="40" step="1" value="${p.max_tool_steps ?? ''}" placeholder="default" style="width:70px;margin:0"></label>
          <h4>TOOL ACCESS</h4>
          ${Object.entries(fam).map(([k, d]) => `<label class="pf-check" title="${esc(d)}"><input type="checkbox" name="tools" value="${k}" ${p.tools.includes(k) ? 'checked' : ''}>
            <span><b>${k}</b><small>${esc(d)}</small></span></label>`).join('')}
          <h4>ALWAYS-ON MCP TOOLS <small class="muted">(via LiteLLM)</small></h4>
          ${servers.length ? servers.map((s) => `<label class="pf-check"><input type="checkbox" name="grant_mcp" value="${esc(s.name)}" ${p.grant_mcp.includes(s.name) ? 'checked' : ''} ${s.tools.length ? '' : 'disabled'}>
            <span><b>${esc(s.name)}</b><small>${s.tools.filter((t) => !t.write).length} read-only tools${s.status === 'ok' ? '' : ` · ${esc(s.status)}`}</small></span></label>`).join('')
            : '<p class="small muted">No MCP servers on the LiteLLM gateway.</p>'}
          ${p.grant_mcp.filter((g) => g.includes('/')).map((g) => `<label class="pf-check"><input type="checkbox" name="grant_mcp" value="${esc(g)}" checked><span><b>${esc(g)}</b><small>single tool</small></span></label>`).join('')}
          <h4>SKILLS</h4>
          <div class="seg pf-seg">
            ${['all', 'some', 'none'].map((m) => `<label><input type="radio" name="skill_mode" value="${m}" ${skillMode === m ? 'checked' : ''}><span>${{ all: 'All', some: 'Selected', none: 'None' }[m]}</span></label>`).join('')}
          </div>
          <div class="pf-skills" ${skillMode === 'some' ? '' : 'hidden'}>
            ${skillList.map((s) => `<label class="pf-check"><input type="checkbox" name="skills" value="${esc(s.name)}" ${p.skills.includes(s.name) ? 'checked' : ''}><span><b>${esc(s.name)}</b><small>${esc(s.description.slice(0, 80))}</small></span></label>`).join('') || '<p class="small muted">No skills in the repo yet.</p>'}
          </div>
          <h4>STATUS</h4>
          <label class="pf-check"><input type="checkbox" name="enabled" ${p.enabled ? 'checked' : ''}><span><b>Enabled</b><small>disabled profiles are not used for new agents</small></span></label>
          ${p.path ? `<p class="small muted" style="word-break:break-all">📄 ${esc(p.path)}</p>` : ''}
        </aside>
      </div>
      <footer class="cp-foot">
        ${!isNew && p.source === 'override' ? '<button type="button" class="btn danger" data-pf="reset">↩️ Reset to built-in</button>' : ''}
        ${!isNew && p.source === 'repo' ? '<button type="button" class="btn danger" data-pf="delete">🗑️ Delete</button>' : ''}
        ${!isNew ? '<button type="button" class="btn" data-pf="duplicate">⧉ Duplicate as new</button>' : ''}
        <span class="sep"></span>
        <button type="button" class="btn" data-pf="close">Cancel</button>
        <button class="btn primary" type="submit" ${data.editable ? '' : 'disabled'}>💾 Save${p.source === 'builtin' && !isNew ? ' as override' : ''}</button>
      </footer></form>`;
    const form = viewEl.querySelector('#pf-form');
    form.addEventListener('change', (e) => {
      if (e.target.name === 'skill_mode') form.querySelector('.pf-skills').hidden = e.target.value !== 'some';
    });
    form.addEventListener('submit', save);
  }

  function collect() {
    const f = viewEl.querySelector('#pf-form');
    const fd = new FormData(f);
    const p = editing.profile;
    const mode = fd.get('skill_mode');
    const prompts = {};
    if (fd.has('p_plan') && fd.get('p_plan').trim()) prompts.plan = fd.get('p_plan').trim();
    if (fd.has('p_synthesize') && fd.get('p_synthesize').trim()) prompts.synthesize = fd.get('p_synthesize').trim();
    const temp = String(fd.get('temperature') || '').trim();
    const steps = String(fd.get('max_tool_steps') || '').trim();
    return {
      key: String(fd.get('key') || p.key).trim(), kind: editing.isNew ? fd.get('kind') : p.kind,
      title: String(fd.get('title')).trim(), icon: String(fd.get('icon') || '🤖').trim(),
      description: String(fd.get('description') || '').trim(), prompt: String(fd.get('prompt') || '').trim(),
      model: fd.get('model') || null, temperature: temp === '' ? null : Number(temp),
      max_tool_steps: steps === '' ? null : Number(steps),
      tools: fd.getAll('tools'), grant_mcp: fd.getAll('grant_mcp'),
      skills: mode === 'all' ? ['*'] : mode === 'none' ? [] : fd.getAll('skills'),
      prompts, enabled: fd.get('enabled') === 'on',
    };
  }

  async function save(e) {
    e.preventDefault();
    const body = collect();
    if (editing.isNew && data.profiles.some((p) => p.key === body.key)) { toast('⚠️', `Key '${esc(body.key)}' already exists`, 'error'); return; }
    try {
      const saved = await api(`/api/profiles/${encodeURIComponent(body.key)}`, { method: 'PUT', body: JSON.stringify(body) });
      toast('💾', `Profile <b>${esc(saved.title)}</b> saved – applies to the next LLM turn`, 'success');
      close();
      await load();
    } catch { /* toast shown */ }
  }

  viewEl.addEventListener('click', async (e) => {
    const act = e.target.closest('[data-pf]')?.dataset.pf;
    if (!act) return;
    if (act === 'close') close();
    if (act === 'duplicate') openNew({ ...editing.profile, key: '' });
    if (act === 'reset' || act === 'delete') {
      const p = editing.profile;
      if (!confirm(act === 'reset' ? `Reset '${p.title}' to the built-in default? Your file will be removed.` : `Delete profile '${p.title}'?`)) return;
      try {
        await api(`/api/profiles/${encodeURIComponent(p.key)}`, { method: 'DELETE' });
        toast(act === 'reset' ? '↩️' : '🗑️', `Profile <b>${esc(p.title)}</b> ${act === 'reset' ? 'reset' : 'deleted'}`, 'success');
        close();
        await load();
      } catch { /* toast shown */ }
    }
  });

  // ------------------------------------------------------------ skills
  async function openSkill(name) {
    let s;
    try { s = await (await fetch(`/api/skills/${encodeURIComponent(name)}`)).json(); } catch { return; }
    editing = null;
    viewEl.hidden = false;
    viewEl.style.setProperty('--c', '#80d8ff');
    viewEl.innerHTML = `<header class="rv-head">
        <div class="rv-title"><div class="rv-crest">🧠</div><div style="min-width:0"><small>SKILL · ${esc(s.root.split('/').slice(-2).join('/'))}</small><h2>${esc(s.name)}</h2></div></div>
        <div class="rv-stats"><span class="rv-stat">📄 ${s.files.length + 1} file${s.files.length ? 's' : ''}</span></div>
        <div class="rv-actions"><button type="button" class="btn small" data-pf="close">✕</button></div>
      </header>
      <div class="rv-body"><div class="rv-doc">
        <section class="rv-card"><h4>DESCRIPTION – shown to agents in their briefing</h4><p>${esc(s.description)}</p>
          <p class="small muted">Agents load the instructions below on demand via <code>use_skill</code>; extra files via <code>read_skill_file</code>.
          Edit <code>${esc(s.path)}/SKILL.md</code> in VS Code – changes are picked up automatically.</p></section>
        <article class="rv-card md">${renderMarkdown(s.instructions)}</article>
        ${s.files.length ? `<section class="rv-card"><h4>EXTRA FILES</h4><ul>${s.files.map((f) => `<li><code>${esc(f)}</code></li>`).join('')}</ul></section>` : ''}
      </div></div>`;
  }

  return { load, open, renderList, close, isOpen: () => !viewEl.hidden, profiles: () => data?.profiles || [] };
}
