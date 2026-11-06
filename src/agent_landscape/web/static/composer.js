// Mission briefing composer: objective + a compact "Context" list. Everything the agents may use
// (repositories, knowledge, tools, files & folders, web pages) is chosen in the context picker (picker.js).
import { KIND_INFO, createContextPicker, fileChars, human, keyOf, readTextFiles } from './picker.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const SAMPLES = [
  ['Launch plan', 'Create a go-to-market launch plan for a developer tool that turns Jira tickets into pull requests.'],
  ['RAG stack', 'Research and compare vector databases for a RAG platform and recommend one with a migration plan.'],
  ['Onboarding revamp', 'Redesign the onboarding experience of a B2B SaaS app to cut time-to-value in half.'],
  ['Code review', 'Review the attached project folder: summarize the architecture, find dead code and propose missing tests.'],
  ['Edge AI', 'Evaluate running small LLMs on-device for a note-taking app: feasibility, models, UX and costs.'],
];
// "#" in the objective opens the picker on a category
const HASH_OPTIONS = [
  { key: 'suggested', icon: '✨', label: 'Suggested', hint: 'matches for this briefing' },
  { key: 'repos', icon: '📦', label: 'Repository', hint: 'research or change code' },
  { key: 'knowledge', icon: '📚', label: 'Knowledge', hint: 'vector stores' },
  { key: 'tools', icon: '🔌', label: 'Tools', hint: 'MCP tools via LiteLLM' },
  { key: 'files', icon: '📁', label: 'Files & folders', hint: 'upload or local path' },
  { key: 'web', icon: '🔗', label: 'Web page', hint: 'fetched at deploy' },
];
const ORDER = ['repo', 'store', 'tool', 'path', 'file', 'url'];

export function createComposer({ el, store, api, toast, onDeployed }) {
  const form = el.querySelector('#launch');
  const f = form.elements;
  const editor = el.querySelector('#cp-editor');
  const goal = f.namedItem('goal');
  const listEl = el.querySelector('#cp-ctx-list');
  const hintEl = el.querySelector('#cp-ctx-hint');
  const hashMenu = el.querySelector('#cp-hashmenu');
  const picker = createContextPicker({ el: el.querySelector('#context-picker'), store, api, toast });
  let items = []; // context items: {kind, label, detail, state, …payload}
  let hashSel = 0;
  let proposal = null; // operations proposal this briefing was pre-filled from: {id, title, new_region}
  const NEW_REGION = '__new__';

  function setProposal(p) {
    proposal = p;
    const sel = f.namedItem('region_id');
    sel.querySelector(`option[value="${NEW_REGION}"]`)?.remove();
    if (p?.new_region) {
      const o = document.createElement('option');
      o.value = NEW_REGION;
      o.textContent = `✨ New region: ${p.new_region.name}`;
      sel.append(o);
    }
    el.querySelector('.cp-head small').textContent = p ? `COMMAND HQ · 📨 FROM OPERATIONS PROPOSAL` : 'COMMAND HQ';
  }

  // ---------------------------------------------------------------- open/close
  function open(prefill = {}) {
    el.hidden = false;
    if (prefill.goal !== undefined) { // a new pre-filled briefing replaces any earlier proposal link
      setProposal(prefill.proposal || null);
      if (prefill.items) {
        items = prefill.items.map((i) => ({ ...i, label: i.label || i.name, detail: i.detail || human((i.content || '').length) }));
      }
    }
    if (prefill.goal !== undefined) goal.value = prefill.goal;
    if (prefill.title !== undefined) f.namedItem('title').value = prefill.title;
    if (prefill.mode) form.querySelector(`[name=mode][value="${prefill.mode}"]`)?.click();
    if (prefill.region_id !== undefined || prefill.proposal?.new_region) {
      f.namedItem('region_id').value = prefill.region_id || (prefill.proposal?.new_region ? NEW_REGION : '');
    }
    render();
    loadProfiles();
    picker.preload().then(render); // catalogs for the 💡 suggestion hint
    setTimeout(() => goal.focus(), 30);
  }
  function close() { picker.cancel(); el.hidden = true; hideHash(); }
  function reset() {
    form.reset();
    items = [];
    setProposal(null);
    const slider = f.namedItem('swarm_size');
    slider.value = store.config.default_swarm_size || 3;
    el.querySelector('#swarm-size-out').textContent = slider.value;
    syncModelPool();
    render();
  }
  function syncModelPool() { el.querySelector('#cp-model-pool').hidden = f.namedItem('model').value !== 'auto'; }
  f.namedItem('model').addEventListener('change', syncModelPool);

  // ------------------------------------------------------------- profiles
  let profiles = [];
  async function loadProfiles() {
    try { profiles = (await (await fetch('/api/profiles')).json()).profiles; } catch { profiles = []; }
    fillProfiles();
  }
  function fillProfiles() {
    const sel = f.namedItem('profile');
    const kind = f.namedItem('mode').value === 'swarm' ? 'swarm_leader' : 'orchestrator';
    const keep = sel.value;
    const opts = profiles.filter((p) => p.kind === kind && p.enabled);
    sel.innerHTML = `<option value="">default ${kind === 'swarm_leader' ? 'hive leader' : 'orchestrator'}</option>` +
      opts.map((p) => `<option value="${esc(p.key)}">${esc(p.icon)} ${esc(p.title)}${p.source === 'builtin' ? '' : ' ✦'}${p.model ? ` · ${esc(p.model)}` : ''}</option>`).join('');
    sel.value = opts.some((p) => p.key === keep) ? keep : '';
  }
  form.addEventListener('change', (e) => { if (e.target.name === 'mode') fillProfiles(); });

  // ---------------------------------------------------------------- picker
  const briefingText = () => `${f.namedItem('title').value} ${goal.value}`;
  async function openPicker(category = null) {
    hideHash();
    const result = await picker.open({ items, category, text: briefingText() });
    if (result) { items = result; render(); }
    goal.focus();
  }

  // ------------------------------------------------------------ context list
  /** Rows for the briefing: one per repo / store / file / path / url; tools grouped per MCP server. */
  function rows() {
    const out = [];
    const toolGroups = new Map();
    for (const i of items) {
      if (i.kind !== 'tool') { out.push({ kind: i.kind, keys: [keyOf(i)], items: [i] }); continue; }
      const server = i.tool.split('/')[0];
      if (!toolGroups.has(server)) {
        const g = { kind: 'tool', keys: [], items: [], server };
        toolGroups.set(server, g);
        out.push(g);
      }
      toolGroups.get(server).keys.push(keyOf(i));
      toolGroups.get(server).items.push(i);
    }
    return out.sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind));
  }
  function rowHtml(r, idx) {
    const info = KIND_INFO[r.kind];
    const i = r.items[0];
    let name = i.label, detail = i.detail || '', cls = '', title = i.title || i.label;
    if (r.kind === 'tool') {
      const whole = r.items.find((x) => !x.tool.includes('/'));
      const single = r.items.filter((x) => x.tool.includes('/'));
      const writes = single.filter((x) => x.state === 'warn').length;
      name = r.server;
      detail = [whole ? whole.detail : '', single.length ? `${single.length === 1 ? single[0].label : `${single.length} tools`}` : '']
        .filter(Boolean).join(' + ') + (writes ? ` · ⚠️ ${writes} change${writes > 1 ? '' : 's'} data` : '');
      title = r.items.map((x) => x.title || x.label).join('\n');
      cls = writes ? 'warn' : '';
    } else if (r.kind === 'repo') {
      cls = i.write ? 'edit' : '';
    }
    return `<div class="ctx-row ${cls}" data-row="${idx}" role="button" tabindex="0" title="${esc(title)} – click to change">
      <span class="ctx-ico">${info.icon}</span><span class="ctx-type">${info.label}</span>
      <b class="ctx-name">${esc(name)}</b><span class="ctx-detail">${esc(detail)}</span>
      <button type="button" class="ctx-x" data-remove="${idx}" title="Remove">✕</button></div>`;
  }
  let currentRows = [];
  function render() {
    currentRows = rows();
    listEl.innerHTML = currentRows.length ? currentRows.map(rowHtml).join('')
      : `<button type="button" class="ctx-empty" data-pick="">
          <b>No context yet</b><span>Agents work from the briefing alone. Add repositories, knowledge bases, tools, files or web pages they should use.</span></button>`;
    el.querySelector('#cp-ctx-count').textContent = items.length ? `${items.length}` : '';
    const sug = picker.suggest(briefingText(), items);
    hintEl.innerHTML = sug.length ? `💡 <span>${sug.length === 1 ? `<b>${esc(sug[0].label)}</b> looks relevant to this briefing`
      : `${sug.length} suggestions for this briefing: ${sug.slice(0, 3).map((s) => `<b>${esc(s.label)}</b>`).join(', ')}${sug.length > 3 ? ' …' : ''}`}</span>
      <button type="button" class="btn small" data-pick="suggested">Review</button>` : '';
    hintEl.hidden = !sug.length;
    renderIntel();
  }
  function renderIntel() {
    const docs = items.filter((i) => i.kind === 'file');
    const docChars = fileChars(items);
    const n = (k) => items.filter((i) => i.kind === k).length;
    const tools = items.filter((i) => i.kind === 'tool').reduce((s, t) => s + (t.count || 1), 0);
    const repos = items.filter((i) => i.kind === 'repo');
    const plural = (c, w) => `${c} ${w}${c === 1 ? '' : 's'}`;
    el.querySelector('#cp-intel').innerHTML = items.length ? `<ul>
        ${repos.map((r) => `<li>📦 ${esc(r.repo)} – ${r.write ? '✍️ editable branch' : '👁 read-only'}</li>`).join('')}
        ${n('store') ? `<li>📚 ${plural(n('store'), 'knowledge base')}</li>` : ''}
        ${tools ? `<li>🔌 ${plural(tools, 'tool')} via LiteLLM MCP</li>` : ''}
        ${n('path') ? `<li>📁 ${plural(n('path'), 'folder')} – read on demand</li>` : ''}
        ${docs.length ? `<li>📎 ${plural(docs.length, 'file')} · ${human(docChars)}</li>` : ''}
        ${n('url') ? `<li>🔗 ${plural(n('url'), 'web page')} – fetched at deploy</li>` : ''}
      </ul><p style="margin:8px 0 0">Agents get sandboxed tools for this context${repos.some((r) => r.write) ? ' – only ✍️ specialists may change editable repos' : ', read-only'}.</p>`
      : 'No context – agents work from the briefing only.';
    const tokens = Math.round((goal.value.length + Math.min(docChars, 4000)) / 4);
    el.querySelector('#cp-estimate').textContent = goal.value || items.length
      ? `≈ ${tokens.toLocaleString()} briefing tokens per agent${docChars > 4000 || n('path') || n('url') || repos.length ? ' · larger context is read on demand via tools' : ''}`
      : '';
  }
  goal.addEventListener('input', render);
  f.namedItem('title').addEventListener('input', render);

  el.addEventListener('click', (e) => {
    const rm = e.target.closest('[data-remove]');
    if (rm && listEl.contains(rm)) {
      const keys = new Set(currentRows[Number(rm.dataset.remove)]?.keys || []);
      items = items.filter((i) => !keys.has(keyOf(i)));
      render();
      return;
    }
    const pick = e.target.closest('[data-pick]');
    if (pick && el.querySelector('#launch').contains(pick)) { openPicker(pick.dataset.pick || null); return; }
    const rowEl = e.target.closest('[data-row]');
    if (rowEl && listEl.contains(rowEl)) openPicker(KIND_INFO[currentRows[Number(rowEl.dataset.row)].kind].cat);
  });
  listEl.addEventListener('keydown', (e) => {
    const rowEl = e.target.closest('[data-row]');
    if (!rowEl) return;
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); rowEl.click(); }
    if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); rowEl.querySelector('[data-remove]').click(); }
  });

  // --------------------------------------------- files dropped / pasted on the objective
  async function addFiles(list) {
    const added = await readTextFiles(list, fileChars(items), toast);
    const keys = new Set(items.map(keyOf));
    items.push(...added.filter((i) => !keys.has(keyOf(i))));
    if (added.length) toast('📎', `${added.length} file${added.length > 1 ? 's' : ''} added to the context`);
    render();
  }
  let dragDepth = 0;
  editor.addEventListener('dragenter', (e) => { if (e.dataTransfer?.types.includes('Files')) { e.preventDefault(); dragDepth++; editor.classList.add('drag'); } });
  editor.addEventListener('dragover', (e) => { if (e.dataTransfer?.types.includes('Files')) e.preventDefault(); });
  editor.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; editor.classList.remove('drag'); } });
  editor.addEventListener('drop', async (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    dragDepth = 0;
    editor.classList.remove('drag');
    await addFiles(e.dataTransfer.files);
  });
  goal.addEventListener('paste', async (e) => {
    if (e.clipboardData?.files?.length) { e.preventDefault(); await addFiles(e.clipboardData.files); }
  });

  // -------------------------------------------------------------- # menu
  function showHash() { hashSel = 0; hashMenu.hidden = false; drawHash(); }
  function hideHash() { hashMenu.hidden = true; }
  function drawHash() {
    hashMenu.innerHTML = HASH_OPTIONS.map((o, i) => `<div class="opt ${i === hashSel ? 'sel' : ''}" data-hash="${o.key}" role="option">
      <span>${o.icon}</span><b>${o.label}</b><small>${o.hint}</small></div>`).join('');
  }
  function pickHash(key) {
    hideHash();
    const pos = goal.selectionStart;
    if (goal.value[pos - 1] === '#') { // remove the trigger character
      goal.value = goal.value.slice(0, pos - 1) + goal.value.slice(pos);
      goal.setSelectionRange(pos - 1, pos - 1);
    }
    openPicker(key);
  }
  goal.addEventListener('input', (e) => {
    const pos = goal.selectionStart;
    if (e.inputType === 'insertText' && e.data === '#' && (pos === 1 || /\s/.test(goal.value[pos - 2]))) showHash();
    else if (!hashMenu.hidden) hideHash();
  });
  goal.addEventListener('keydown', (e) => {
    if (!hashMenu.hidden) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        hashSel = (hashSel + (e.key === 'ArrowDown' ? 1 : HASH_OPTIONS.length - 1)) % HASH_OPTIONS.length;
        drawHash();
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pickHash(HASH_OPTIONS[hashSel].key); return; }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); hideHash(); return; }
    }
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); form.requestSubmit(); }
  });
  hashMenu.addEventListener('mousedown', (e) => {
    const key = e.target.closest('[data-hash]')?.dataset.hash;
    if (key) { e.preventDefault(); pickHash(key); }
  });
  goal.addEventListener('blur', () => setTimeout(hideHash, 150));

  // ---------------------------------------------------------------- deploy
  el.querySelector('#random-mission').addEventListener('click', () => {
    const [title, text] = SAMPLES[Math.floor(Math.random() * SAMPLES.length)];
    f.namedItem('title').value = title;
    goal.value = text;
    render();
  });
  el.querySelectorAll('[data-cp=close]').forEach((b) => b.addEventListener('click', close));
  form.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); form.requestSubmit(); } });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (picker.isOpen()) return;
    const text = goal.value.trim();
    if (text.length < 3) { toast('⚠️', 'Describe the mission objective first', 'error'); goal.focus(); return; }
    const context = items.map(({ kind, name, content, mime, path, url, tool, store: kb, repo, ref, write }) =>
      ({ kind, name, content, mime, path, url, tool, store: kb, repo, ref, write: !!write }));
    const region = f.namedItem('region_id').value;
    const model = f.namedItem('model').value || null;
    const pool = model === 'auto' ? [...form.querySelectorAll('[name=model_pool]:checked')].map((i) => i.value) : [];
    if (model === 'auto' && !pool.length) { toast('⚠️', 'Pick at least one model for the auto pool', 'error'); return; }
    // claude-code/* runs without tools: agents could neither read nor change attached repos / files / MCP tools
    const toolLess = (m) => String(m || '').startsWith('claude-code/');
    const allToolLess = model === 'auto' ? pool.every(toolLess) : toolLess(model || store.config.default_model);
    const needsTools = items.some((i) => ['repo', 'path', 'tool', 'store'].includes(i.kind));
    if (allToolLess && needsTools && !store.config.claude_code_tools && !confirm(
      'All selected models are claude-code/* – they run WITHOUT tools.\n\n'
      + 'Agents will not be able to read or edit the attached repositories, files, knowledge bases or MCP tools '
      + '(an editable repo will get no commits).\n\nTick a gateway model in the pool (or force one). Deploy anyway?')) return;
    const body = {
      goal: text, title: f.namedItem('title').value.trim() || null, mode: f.namedItem('mode').value,
      swarm_size: Number(f.namedItem('swarm_size').value), model, model_pool: pool,
      region_id: region && region !== NEW_REGION ? region : null, profile: f.namedItem('profile').value || null, context,
      proposal_id: proposal?.id || null, new_region: region === NEW_REGION ? proposal?.new_region || null : null,
    };
    const btn = form.querySelector('button[type=submit]');
    btn.disabled = true;
    try {
      await api('/api/missions', { method: 'POST', body: JSON.stringify(body) });
      toast('⚔️', `Mission <b>${esc(body.title || text.slice(0, 40))}</b> deployed${context.length ? ` with ${context.length} context item${context.length > 1 ? 's' : ''}` : ''}`, 'success');
      reset();
      close();
      onDeployed?.(body);
    } catch { /* toast shown by api() */ } finally {
      btn.disabled = false;
    }
  });

  return { open, close, isOpen: () => !el.hidden, pickerOpen: () => picker.isOpen() };
}
