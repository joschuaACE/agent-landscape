// Mission briefing composer – Copilot-chat-style attachments: files, folders, URLs and LiteLLM MCP tools.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const human = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const MAX_FILE = 1024 * 1024;
const MAX_TOTAL = 5 * 1024 * 1024;

const SAMPLES = [
  ['Launch plan', 'Create a go-to-market launch plan for a developer tool that turns Jira tickets into pull requests.'],
  ['RAG stack', 'Research and compare vector databases for a RAG platform and recommend one with a migration plan.'],
  ['Onboarding revamp', 'Redesign the onboarding experience of a B2B SaaS app to cut time-to-value in half.'],
  ['Code review', 'Review the attached project folder: summarize the architecture, find dead code and propose missing tests.'],
  ['Edge AI', 'Evaluate running small LLMs on-device for a note-taking app: feasibility, models, UX and costs.'],
];
const HASH_OPTIONS = [
  { key: 'file', icon: '📎', label: 'File', hint: 'text files from your computer' },
  { key: 'folder', icon: '📁', label: 'Folder / path', hint: 'read-only access on this machine' },
  { key: 'url', icon: '🔗', label: 'URL', hint: 'fetch a web page' },
  { key: 'tools', icon: '🔌', label: 'Tools', hint: 'MCP tools & APIs via LiteLLM' },
  { key: 'knowledge', icon: '📚', label: 'Knowledge base', hint: 'link a vector store' },
];

export function createComposer({ el, store, api, toast, onDeployed }) {
  const form = el.querySelector('#launch');
  const f = form.elements;
  const editor = el.querySelector('#cp-editor');
  const goal = f.namedItem('goal');
  const chipsEl = el.querySelector('#cp-chips');
  const inline = el.querySelector('#cp-inline');
  const hashMenu = el.querySelector('#cp-hashmenu');
  const fileInput = el.querySelector('#cp-file');
  let items = []; // {id, kind, ... , label, detail, state: ok|warn|err}
  let seq = 0;
  let toolCatalog = null;
  let hashSel = 0;

  // ---------------------------------------------------------------- open/close
  function open(prefill = {}) {
    el.hidden = false;
    if (prefill.goal !== undefined) goal.value = prefill.goal;
    if (prefill.title !== undefined) f.namedItem('title').value = prefill.title;
    if (prefill.region_id) f.namedItem('region_id').value = prefill.region_id;
    render();
    loadProfiles();
    setTimeout(() => goal.focus(), 30);
  }

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
  function close() { el.hidden = true; closeInline(); hideHash(); }
  function reset() {
    form.reset();
    items = [];
    const slider = f.namedItem('swarm_size');
    slider.value = store.config.default_swarm_size || 3;
    el.querySelector('#swarm-size-out').textContent = slider.value;
    render();
  }

  // ------------------------------------------------------------------ items
  function add(item) {
    const dupKey = item.kind + '|' + (item.path || item.url || item.tool || item.store || item.name);
    if (items.some((i) => i.kind + '|' + (i.path || i.url || i.tool || i.store || i.name) === dupKey)) {
      toast('ℹ️', `${esc(item.label)} is already attached`);
      return;
    }
    items.push({ id: ++seq, state: 'ok', ...item });
    render();
  }
  function remove(id) { items = items.filter((i) => i.id !== id); render(); }

  function render() {
    const icon = { file: '📎', path: '📁', url: '🔗', tool: '🔌', store: '📚' };
    chipsEl.innerHTML = items.map((i) => `<span class="cp-chip ${i.state === 'ok' ? '' : i.state}" title="${esc(i.title || i.label)}">
      ${icon[i.kind] || '•'} <b>${esc(i.label)}</b>${i.detail ? `<small>${esc(i.detail)}</small>` : ''}
      <button type="button" data-remove="${i.id}" title="Remove">✕</button></span>`).join('');
    const docs = items.filter((i) => i.kind === 'file');
    const docChars = docs.reduce((s, i) => s + i.content.length, 0);
    const folders = items.filter((i) => i.kind === 'path');
    const urls = items.filter((i) => i.kind === 'url');
    const tools = items.filter((i) => i.kind === 'tool');
    const stores = items.filter((i) => i.kind === 'store');
    const intel = el.querySelector('#cp-intel');
    intel.innerHTML = items.length ? `<ul>
        ${docs.length ? `<li>📎 ${docs.length} file${docs.length > 1 ? 's' : ''} · ${human(docChars)}</li>` : ''}
        ${folders.length ? `<li>📁 ${folders.length} path${folders.length > 1 ? 's' : ''} – read on demand</li>` : ''}
        ${urls.length ? `<li>🔗 ${urls.length} URL${urls.length > 1 ? 's' : ''} – fetched at deploy</li>` : ''}
        ${tools.length ? `<li>🔌 ${tools.reduce((s, t) => s + (t.count || 1), 0)} tool${tools.length > 1 || (tools[0]?.count || 1) > 1 ? 's' : ''} via LiteLLM MCP</li>` : ''}
        ${stores.length ? `<li>📚 ${stores.length} knowledge base${stores.length > 1 ? 's' : ''} – searchable by all agents</li>` : ''}
      </ul><p style="margin:8px 0 0">Every agent of this mission gets an intel briefing plus tools to read it (sandboxed, read-only).</p>`
      : 'No context attached – agents work from the briefing only.';
    const tokens = Math.round((goal.value.length + Math.min(docChars, 4000)) / 4);
    el.querySelector('#cp-estimate').textContent = goal.value || items.length
      ? `≈ ${tokens.toLocaleString()} briefing tokens per agent${docChars > 4000 || folders.length || urls.length ? ' · larger intel is read on demand via tools' : ''}`
      : '';
  }
  goal.addEventListener('input', render);
  chipsEl.addEventListener('click', (e) => { const id = e.target.closest('[data-remove]')?.dataset.remove; if (id) remove(Number(id)); });

  // ------------------------------------------------------------------ files
  async function addFiles(fileList) {
    let total = items.filter((i) => i.kind === 'file').reduce((s, i) => s + i.content.length, 0);
    for (const file of fileList) {
      if (file.size > MAX_FILE) { toast('⚠️', `${esc(file.name)} is larger than 1 MB – attach its folder instead`, 'error'); continue; }
      const buf = new Uint8Array(await file.arrayBuffer());
      if (buf.subarray(0, 8192).includes(0)) { toast('⚠️', `${esc(file.name)} is binary – only text files are supported`, 'error'); continue; }
      const content = new TextDecoder('utf-8').decode(buf);
      if (total + content.length > MAX_TOTAL) { toast('⚠️', 'Attachments exceed 5 MB in total', 'error'); break; }
      total += content.length;
      add({ kind: 'file', name: file.name, content, mime: file.type || 'text/plain', label: file.name, detail: human(file.size) });
    }
  }
  fileInput.addEventListener('change', async () => { await addFiles(fileInput.files); fileInput.value = ''; });
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

  // ----------------------------------------------------------- inline panels
  function closeInline() { inline.hidden = true; inline.innerHTML = ''; inline.dataset.kind = ''; }
  function openInline(kind) {
    if (kind === 'file') { fileInput.click(); return; }
    if (inline.dataset.kind === kind && !inline.hidden) { closeInline(); return; }
    inline.dataset.kind = kind;
    inline.hidden = false;
    if (kind === 'folder') folderPanel();
    if (kind === 'url') urlPanel();
    if (kind === 'tools') toolsPanel();
    if (kind === 'knowledge') knowledgePanel();
  }

  async function knowledgePanel() {
    inline.innerHTML = '<div class="muted small">Loading knowledge bases…</div>';
    let kb;
    try { kb = await (await fetch('/api/knowledge')).json(); } catch { kb = { enabled: false, stores: [] }; }
    if (inline.dataset.kind !== 'knowledge') return;
    if (!kb.enabled) {
      inline.innerHTML = '<div class="status err">No vector store configured – set VECTOR_STORE_URL / VECTOR_STORE_KEY in .env.</div>';
      return;
    }
    const picked = new Set(items.filter((i) => i.kind === 'store').map((i) => i.store));
    const row = (s) => `<label class="kb-row" title="${esc(s.purpose || '')}">
        <input type="checkbox" data-store="${esc(s.name)}" ${picked.has(s.name) ? 'checked' : ''}>
        <span><b>${esc(s.name)}</b> <small>${s.files} files${s.region ? ` · ${esc(s.region)}` : ''}${s.owned ? '' : ' · read-only'}</small>
        ${s.purpose ? `<br><small>${esc(s.purpose)}${s.created_by ? ` – by ${esc(s.created_by)}` : ''}</small>` : ''}</span></label>`;
    const own = kb.stores.filter((s) => s.owned);
    const ext = kb.stores.filter((s) => !s.owned);
    inline.innerHTML = `<div class="row"><input id="cp-kb-filter" placeholder="Filter knowledge bases…" spellcheck="false">
        <button type="button" class="btn primary" id="cp-kb-done">Done</button></div>
      <div class="status muted">Linked bases are searchable by every agent of the mission (<code>kb_search</code>).
        Orchestrators can also create new ones (named <code>${esc(kb.prefix || 'al')}-&lt;region&gt;-&lt;purpose&gt;</code>) –
        and finished missions are archived into <code>…-knowledge</code> of their region automatically.${kb.kind === 'memory' ? ' <b>Mock mode:</b> in-memory stores.' : ''}</div>
      <div class="cp-tools">
        ${own.length ? `<div class="kb-group">🏰 Created by agents</div>${own.map(row).join('')}` : ''}
        ${ext.length ? `<div class="kb-group">📦 Other stores (read-only for agents)</div>${ext.map(row).join('')}` : ''}
        ${kb.stores.length ? '' : '<div class="muted small">No knowledge bases yet.</div>'}
      </div>`;
    inline.querySelector('#cp-kb-done').addEventListener('click', () => { closeInline(); goal.focus(); });
    inline.querySelector('#cp-kb-filter').addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase();
      inline.querySelectorAll('.kb-row').forEach((r) => { r.hidden = q && !r.textContent.toLowerCase().includes(q); });
    });
    inline.querySelectorAll('input[data-store]').forEach((cb) => cb.addEventListener('change', () => {
      const s = kb.stores.find((x) => x.name === cb.dataset.store);
      if (cb.checked) add({ kind: 'store', store: s.name, label: s.name, detail: `${s.files} files`, title: s.purpose || s.name });
      else { items = items.filter((i) => !(i.kind === 'store' && i.store === s.name)); render(); }
    }));
    inline.querySelector('#cp-kb-filter').focus();
  }

  function folderPanel() {
    const recent = JSON.parse(localStorage.getItem('al.recentPaths') || '[]');
    const roots = (store.config.context_roots || []).join(', ');
    inline.innerHTML = `<div class="row"><input id="cp-path" placeholder="/Users/you/git/project  or  ~/docs/spec.md" spellcheck="false">
      <button type="button" class="btn primary" id="cp-path-add" disabled>📁 Attach</button></div>
      <div class="status muted" id="cp-path-status">Agents get read-only tools (list, read, search) for this folder. Secrets like .env are hidden.${roots ? ` Allowed roots: <code>${esc(roots)}</code>` : ''}</div>
      ${recent.length ? `<div class="recent">${recent.map((p) => `<button type="button" class="pill" data-path="${esc(p)}">🕘 ${esc(p)}</button>`).join('')}</div>` : ''}`;
    const input = inline.querySelector('#cp-path');
    const btn = inline.querySelector('#cp-path-add');
    const status = inline.querySelector('#cp-path-status');
    let last = null, timer = null;
    const check = async () => {
      const path = input.value.trim();
      if (!path) { btn.disabled = true; return; }
      const res = await (await fetch(`/api/context/inspect?path=${encodeURIComponent(path)}`)).json();
      if (input.value.trim() !== path) return;
      last = res.ok ? res : null;
      btn.disabled = !res.ok || (res.kind === 'file' && (!res.text || res.too_big));
      status.className = `status ${res.ok ? 'ok' : 'err'}`;
      status.textContent = !res.ok ? `✗ ${res.error}` : res.kind === 'dir'
        ? `✓ Folder · ${res.files}${res.files_capped ? '+' : ''} files · ${res.size_h}${res.types?.length ? ` · ${res.types.join(', ')}` : ''}`
        : !res.text ? '✗ Binary file – not supported' : res.too_big ? `✗ File too large (${res.size_h}) – attach its folder`
          : `✓ File · ${res.size_h}`;
    };
    input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(check, 250); });
    const attach = () => {
      if (!last) return;
      add({ kind: 'path', path: last.path, label: last.name, title: last.path,
        detail: last.kind === 'dir' ? `${last.files}${last.files_capped ? '+' : ''} files` : last.size_h });
      localStorage.setItem('al.recentPaths', JSON.stringify([last.path, ...recent.filter((p) => p !== last.path)].slice(0, 6)));
      closeInline();
      goal.focus();
    };
    btn.addEventListener('click', attach);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); if (!btn.disabled) attach(); } });
    inline.querySelectorAll('[data-path]').forEach((p) => p.addEventListener('click', () => { input.value = p.dataset.path; check(); }));
    input.focus();
  }

  function urlPanel() {
    inline.innerHTML = `<div class="row"><input id="cp-url" placeholder="https://docs.example.com/guide" spellcheck="false" type="url">
      <button type="button" class="btn primary" id="cp-url-add">🔗 Attach</button></div>
      <div class="status muted">The page is fetched when the mission deploys and given to the agents as a document.</div>`;
    const input = inline.querySelector('#cp-url');
    const attach = () => {
      const url = input.value.trim();
      if (!/^https?:\/\/\S+\.\S+/.test(url)) { toast('⚠️', 'Enter a valid http(s) URL', 'error'); return; }
      add({ kind: 'url', url, label: url.replace(/^https?:\/\//, '').slice(0, 60), title: url });
      closeInline();
      goal.focus();
    };
    inline.querySelector('#cp-url-add').addEventListener('click', attach);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); attach(); } });
    input.focus();
  }

  async function toolsPanel() {
    inline.innerHTML = '<div class="muted small">Loading tools from the LiteLLM MCP gateway…</div>';
    try {
      toolCatalog = await (await fetch('/api/tools')).json();
    } catch { inline.innerHTML = '<div class="status err">Could not load tools.</div>'; return; }
    if (inline.dataset.kind !== 'tools') return;
    const cat = toolCatalog;
    if (!cat.available) {
      inline.innerHTML = '<div class="status err">No LiteLLM proxy configured – set LITELLM_PROXY_URL to use MCP tools.</div>';
      return;
    }
    const picked = new Set(items.filter((i) => i.kind === 'tool').map((i) => i.tool));
    const servers = cat.servers.map((s) => {
      const ro = s.tools.filter((t) => !t.write).length;
      return `<div class="cp-server" data-server="${esc(s.name)}">
        <div class="sh"><input type="checkbox" data-tool="${esc(s.name)}" ${picked.has(s.name) ? 'checked' : ''} ${s.tools.length ? '' : 'disabled'}
            title="All ${ro} read-only tools of this server">
          <b>${esc(s.name)}</b><small>${s.tools.length} tools · ${ro} read-only</small>
          <span class="st ${s.status === 'ok' ? '' : 'bad'}">${esc(s.status)}</span><span class="tw">▾</span></div>
        <div class="tl">${s.tools.map((t) => `<label title="${esc(t.description)}"><input type="checkbox" data-tool="${esc(t.ref)}" ${picked.has(t.ref) ? 'checked' : ''}>
            <span>${t.write ? '⚠️ ' : ''}<b>${esc(t.name)}</b> <small>${esc(t.description.slice(0, 90))}</small></span></label>`).join('')}</div>
      </div>`;
    }).join('');
    inline.innerHTML = `<div class="row"><input id="cp-tool-filter" placeholder="Filter tools…" spellcheck="false">
        <button type="button" class="btn small" id="cp-tools-refresh" title="Reload from LiteLLM">⟳</button>
        <button type="button" class="btn primary" id="cp-tools-done">Done</button></div>
      <div class="status muted">Server checkbox = all <b>read-only</b> tools. ⚠️ tools change data and must be picked individually.
        ${cat.mock ? ' <b>Mock mode:</b> external tools are simulated, not called.' : ''}
        REST APIs (e.g. your vector store) can be added in LiteLLM as an MCP server from their OpenAPI spec.</div>
      <div class="cp-tools">${servers || '<div class="muted small">No MCP servers on the gateway.</div>'}</div>`;
    inline.querySelector('#cp-tools-done').addEventListener('click', () => { closeInline(); goal.focus(); });
    inline.querySelector('#cp-tools-refresh').addEventListener('click', async () => { await fetch('/api/tools?refresh=true'); toolsPanel(); });
    inline.querySelector('#cp-tool-filter').addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase();
      inline.querySelectorAll('.cp-server').forEach((s) => {
        let any = false;
        s.querySelectorAll('.tl label').forEach((l) => { const hit = !q || l.textContent.toLowerCase().includes(q); l.hidden = !hit; any ||= hit; });
        s.hidden = !any && !s.dataset.server.toLowerCase().includes(q);
        s.classList.toggle('open', !!q && any);
      });
    });
    inline.querySelectorAll('.cp-server .sh').forEach((h) => h.addEventListener('click', (e) => {
      if (e.target.matches('input')) return;
      h.parentElement.classList.toggle('open');
    }));
    inline.querySelectorAll('input[data-tool]').forEach((cb) => cb.addEventListener('change', () => {
      const ref = cb.dataset.tool;
      const isServer = !ref.includes('/');
      const server = cat.servers.find((s) => s.name === ref.split('/')[0]);
      if (cb.checked) {
        const t = isServer ? null : server.tools.find((x) => x.ref === ref);
        add({ kind: 'tool', tool: ref, label: isServer ? ref : `${t.name}`, title: isServer ? `${server.name}: all read-only tools` : `${ref} – ${t.description}`,
          detail: isServer ? `${server.tools.filter((x) => !x.write).length} tools` : (t.write ? '⚠️ writes' : server.name),
          count: isServer ? server.tools.filter((x) => !x.write).length : 1, state: !isServer && t.write ? 'warn' : 'ok' });
      } else {
        items = items.filter((i) => !(i.kind === 'tool' && i.tool === ref));
        render();
      }
    }));
    inline.querySelector('#cp-tool-filter').focus();
  }

  el.querySelectorAll('[data-attach]').forEach((b) => b.addEventListener('click', () => openInline(b.dataset.attach)));

  // -------------------------------------------------------------- # menu
  function showHash() {
    hashSel = 0;
    hashMenu.hidden = false;
    drawHash();
  }
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
    openInline(key);
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
    const text = goal.value.trim();
    if (text.length < 3) { toast('⚠️', 'Describe the mission objective first', 'error'); goal.focus(); return; }
    const context = items.map(({ kind, name, content, mime, path, url, tool, store }) => ({ kind, name, content, mime, path, url, tool, store }));
    const body = {
      goal: text, title: f.namedItem('title').value.trim() || null, mode: f.namedItem('mode').value,
      swarm_size: Number(f.namedItem('swarm_size').value), model: f.namedItem('model').value || null,
      region_id: f.namedItem('region_id').value || null, profile: f.namedItem('profile').value || null, context,
    };
    const btn = form.querySelector('button[type=submit]');
    btn.disabled = true;
    try {
      await api('/api/missions', { method: 'POST', body: JSON.stringify(body) });
      toast('⚔️', `Mission <b>${esc(body.title || text.slice(0, 40))}</b> deployed${context.length ? ` with ${context.length} intel item${context.length > 1 ? 's' : ''}` : ''}`, 'success');
      reset();
      close();
      onDeployed?.(body);
    } catch { /* toast shown by api() */ } finally {
      btn.disabled = false;
    }
  });

  return { open, close, isOpen: () => !el.hidden };
}
