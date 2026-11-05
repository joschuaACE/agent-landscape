// Context picker – one dialog for everything the agents of a mission may use:
// repositories, knowledge bases, MCP tools, files & folders and web pages.
// It works on a draft copy of the selection: Cancel changes nothing, Apply returns the new list.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
export const human = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const MAX_FILE = 1024 * 1024;
const MAX_TOTAL = 5 * 1024 * 1024;

export const KIND_INFO = {
  repo: { icon: '📦', label: 'Repository', cat: 'repos' },
  store: { icon: '📚', label: 'Knowledge', cat: 'knowledge' },
  tool: { icon: '🔌', label: 'Tools', cat: 'tools' },
  file: { icon: '📎', label: 'File', cat: 'files' },
  path: { icon: '📁', label: 'Folder', cat: 'files' },
  url: { icon: '🔗', label: 'Web page', cat: 'web' },
};
export const keyOf = (i) => `${i.kind}|${i.path || i.url || i.tool || i.store || i.repo || i.name}`;

const CATS = [
  { key: 'suggested', icon: '✨', label: 'Suggested' },
  { key: 'repos', icon: '📦', label: 'Repositories' },
  { key: 'knowledge', icon: '📚', label: 'Knowledge' },
  { key: 'tools', icon: '🔌', label: 'Tools' },
  { key: 'files', icon: '📁', label: 'Files & folders' },
  { key: 'web', icon: '🔗', label: 'Web pages' },
];
const STOP = new Set(('with that this from into have will should would about what which their there your them then than also when '
  + 'where agent agents mission create make using based need want some more each every after before other only just like over such '
  + 'very please could into the and for are our new add update change').split(' '));
const words = (t) => new Set((String(t).toLowerCase().match(/[a-z0-9äöüß][\w.äöüß-]{3,}/g) || []).filter((w) => !STOP.has(w)));
const matches = (text, q) => q.split(/\s+/).filter(Boolean).every((w) => text.toLowerCase().includes(w));
const base = (p) => p.split('/').filter(Boolean).pop() || p;

// PDF / Office documents are converted to markdown on the server (markitdown, local + offline)
export const CONVERTIBLE = { pdf: 'PDF', docx: 'Word', pptx: 'PowerPoint', xlsx: 'Excel', xls: 'Excel', epub: 'EPUB', msg: 'Outlook mail' };
const MAX_DOC = 25 * 1024 * 1024;
const ext = (name) => (name.match(/\.([a-z0-9]+)$/i)?.[1] || '').toLowerCase();
const k = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));

async function convertDoc(file) {
  const res = await fetch(`/api/context/convert?name=${encodeURIComponent(file.name)}`, {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.detail || res.statusText);
  return json;
}

/** Read dropped / picked files → context items. Text files are read as-is; PDF / Office documents are
 * converted to markdown on the server. Problems are reported in ONE toast instead of one per file. */
export async function readTextFiles(fileList, usedChars, toast) {
  const files = [...fileList];
  const out = [];
  const problems = [];
  let total = usedChars;
  const docs = files.filter((f) => CONVERTIBLE[ext(f.name)]);
  if (docs.length) toast('⏳', `Converting ${docs.length} document${docs.length > 1 ? 's' : ''} to text…`);
  const conversions = new Map(docs.filter((f) => f.size <= MAX_DOC).map((f) => [f, convertDoc(f).then((r) => ({ r }), (e) => ({ e }))]));
  for (const file of files) {
    const kind = CONVERTIBLE[ext(file.name)];
    let content, mime, detail;
    if (kind) {
      if (file.size > MAX_DOC) { problems.push(`${file.name}: larger than 25 MB`); continue; }
      const { r, e } = await conversions.get(file);
      if (e) { problems.push(e.message); continue; }
      content = r.text;
      mime = 'text/markdown';
      detail = `${kind} · ${human(file.size)} → ${k(r.chars)} chars`;
    } else {
      if (file.size > MAX_FILE) { problems.push(`${file.name}: text files are limited to 1 MB – add its folder instead`); continue; }
      const buf = new Uint8Array(await file.arrayBuffer());
      if (buf.subarray(0, 8192).includes(0)) {
        problems.push(`${file.name}: unsupported binary format (PDF, Word, PowerPoint, Excel and text files work)`);
        continue;
      }
      content = new TextDecoder('utf-8').decode(buf);
      mime = file.type || 'text/plain';
      detail = human(file.size);
    }
    if (total + content.length > MAX_TOTAL) { problems.push(`${file.name}: attachments would exceed 5 MB of text in total`); continue; }
    total += content.length;
    out.push({ kind: 'file', name: file.name, content, mime, label: file.name, detail });
  }
  if (problems.length) {
    toast('⚠️', problems.length === 1 ? esc(problems[0])
      : `${problems.length} files were not added:<br>${problems.slice(0, 4).map((p) => `• ${esc(p)}`).join('<br>')}${problems.length > 4 ? '<br>…' : ''}`, 'error');
  }
  return out;
}
export const fileChars = (items) => items.filter((i) => i.kind === 'file').reduce((s, i) => s + i.content.length, 0);

// ------------------------------------------------------------- item factories
const repoDetail = (write, ref, r) => `${write ? '✍️ Edit on mission branch' : '👁 Read-only'} · ${ref || r?.branch || r?.base || 'default branch'}`;
const repoItem = (r, write = false, ref = '') => ({
  kind: 'repo', repo: r.name, write, ref: ref || undefined, label: r.name,
  title: `${r.description || r.name}\n${r.url || r.path || ''}`, detail: repoDetail(write, ref, r),
});
const storeItem = (s) => ({ kind: 'store', store: s.name, label: s.name, detail: `${s.files} files${s.owned ? '' : ' · read-only'}`, title: s.purpose || s.name });
const serverItem = (sv) => {
  const ro = sv.tools.filter((t) => !t.write).length;
  return { kind: 'tool', tool: sv.name, label: sv.name, title: `${sv.name}: all ${ro} read-only tools`, detail: `all ${ro} read-only tools`, count: ro, state: 'ok' };
};
const toolItem = (sv, t) => ({
  kind: 'tool', tool: t.ref, label: t.name, title: `${t.ref} – ${t.description}`, detail: t.write ? 'changes data' : sv.name,
  count: 1, state: t.write ? 'warn' : 'ok',
});

export function createContextPicker({ el, store, api, toast }) {
  const cat = { repos: null, kb: null, tools: null };
  const st = { draft: [], cat: 'repos', q: '', view: 'list', text: '', resolve: null, auto: false, expanded: new Set(),
    lastView: '', pathCheck: null };
  let options = new Map(); // row key → () => item (rebuilt on every body render)

  el.innerHTML = `<div class="pk-dialog" role="dialog" aria-modal="true" aria-label="Add context">
    <header class="pk-head">
      <div class="pk-title"><b>Add context</b><small>What may the agents of this mission use?</small></div>
      <input class="pk-search" type="search" placeholder="🔎 Search repositories, knowledge and tools…" spellcheck="false" autocomplete="off">
      <button type="button" class="btn small" data-pk="cancel" title="Close without changes (Esc)">✕</button>
    </header>
    <div class="pk-main"><nav class="pk-nav" aria-label="Context categories"></nav><section class="pk-body"></section></div>
    <footer class="pk-foot"><span class="pk-sum"></span><span class="sep"></span>
      <button type="button" class="btn" data-pk="cancel">Cancel</button>
      <button type="button" class="btn primary" data-pk="apply" title="⌘↵">Apply</button></footer>
    <input type="file" class="pk-file" multiple hidden></div>`;
  const $ = (s) => el.querySelector(s);
  const nav = $('.pk-nav');
  const body = $('.pk-body');
  const search = $('.pk-search');
  const sum = $('.pk-sum');
  const fileInput = $('.pk-file');

  // -------------------------------------------------------------- catalogs
  const get = async (url) => { try { const r = await fetch(url); return r.ok ? await r.json() : null; } catch { return null; } };
  async function load(force = false) {
    const jobs = [];
    if (force || !cat.repos) jobs.push(get('/api/repos').then((d) => { cat.repos = d || { repos: [], errors: [] }; refresh(); }));
    if (force || !cat.kb) jobs.push(get('/api/knowledge').then((d) => { cat.kb = d || { enabled: false, stores: [] }; refresh(); }));
    if (force || !cat.tools) jobs.push(get('/api/tools').then((d) => { cat.tools = d || { available: false, servers: [] }; refresh(); }));
    await Promise.all(jobs);
  }
  const isOpen = () => !el.hidden;
  function refresh() { if (isOpen()) render(); }

  // ----------------------------------------------------------- suggestions
  function suggestions(text = st.text) {
    const low = text.toLowerCase();
    const tw = words(text);
    const hit = (w) => w && w.length > 2 && new RegExp(`(^|[^\\w])${reEsc(w.toLowerCase())}($|[^\\w])`).test(low);
    const out = [];
    for (const r of cat.repos?.repos || []) {
      if ([r.name, r.name.replace(/[-_.]/g, ' '), ...(r.keywords || [])].some(hit)) {
        out.push({ key: `repo|${r.name}`, type: 'repo', r, label: r.name, why: 'name or keyword mentioned' });
      }
    }
    for (const s of cat.kb?.stores || []) {
      const sw = words(`${s.name.replace(/[-_]/g, ' ')} ${s.purpose || ''} ${s.region || ''}`);
      const n = [...sw].filter((w) => tw.has(w)).length;
      if (n >= 2) out.push({ key: `store|${s.name}`, type: 'store', s, label: s.name, why: `${n} matching terms` });
    }
    for (const sv of cat.tools?.servers || []) {
      const first = sv.name.split(/[_-]/)[0];
      if ([sv.name, sv.name.replace(/[_-]/g, ' ').replace(/ mcp$/, ''), first.length >= 4 ? first : ''].some(hit)) {
        out.push({ key: `tool|${sv.name}`, type: 'server', sv, label: sv.name, why: 'server mentioned' });
      }
    }
    return out;
  }

  // ------------------------------------------------------------- selection
  const find = (key) => st.draft.find((i) => keyOf(i) === key);
  const has = (key) => !!find(key);
  function upsert(item) {
    const k = keyOf(item);
    const idx = st.draft.findIndex((x) => keyOf(x) === k);
    if (idx >= 0) st.draft[idx] = { ...st.draft[idx], ...item };
    else st.draft.push(item);
  }
  const drop = (key) => { st.draft = st.draft.filter((i) => keyOf(i) !== key); };
  const repoOf = (name) => cat.repos?.repos.find((r) => r.name === name);

  // ------------------------------------------------------------- templates
  const badge = (text, cls = '', title = '') => `<span class="pk-badge ${cls}" title="${esc(title)}">${esc(text)}</span>`;
  const loading = (what) => `<p class="pk-loading">Loading ${what}…</p>`;
  const empty = (title, text = '', action = '') => `<div class="pk-empty"><b>${title}</b>${text ? `<span>${text}</span>` : ''}${action}</div>`;
  const intro = (title, text, action = '') => `<div class="pk-intro"><div><b>${title}</b><p>${text}</p></div>${action}</div>`;
  const section = (title, html) => (html ? `<div class="pk-sec"><h5>${title}</h5>${html}</div>` : '');
  function row({ key, checked, icon, title, sub = '', meta = '', badges = '', aside = '', extra = '', disabled = false }) {
    return `<div class="pk-row ${checked ? 'on' : ''}" data-key="${esc(key)}">
      <label class="pk-pick"><input type="checkbox" data-toggle="${esc(key)}" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
        <span class="pk-ico">${icon}</span>
        <span class="pk-txt"><b>${esc(title)} ${badges}</b>${sub ? `<small>${esc(sub)}</small>` : ''}${meta ? `<small class="muted">${esc(meta)}</small>` : ''}</span></label>
      ${aside ? `<div class="pk-aside">${aside}</div>` : ''}${extra}</div>`;
  }

  function repoRow(r, why = '') {
    const key = `repo|${r.name}`;
    options.set(key, () => repoItem(r));
    const sel = find(key);
    const status = r.error ? `⚠️ ${r.error}` : r.cloned ? [r.base, r.last_commit].filter(Boolean).join(' · ')
      : r.managed ? 'not cloned yet – cloned on first use' : '⚠️ local checkout missing';
    const extra = sel ? `<div class="pk-settings">
        <div class="seg pk-mode" role="radiogroup" aria-label="Access">
          <label><input type="radio" name="pkm-${esc(r.name)}" value="read" data-repo-mode="${esc(r.name)}" ${sel.write ? '' : 'checked'}><span>👁 Read-only</span></label>
          <label><input type="radio" name="pkm-${esc(r.name)}" value="write" data-repo-mode="${esc(r.name)}" ${sel.write ? 'checked' : ''}><span>✍️ Edit on a mission branch</span></label>
        </div>
        <label class="pk-ref">Base <input data-repo-ref="${esc(r.name)}" value="${esc(sel.ref || '')}" placeholder="${esc(r.branch || r.base || 'default branch')}" spellcheck="false"></label>
        <p class="pk-note">${sel.write
          ? '✍️ Specialists with repo write access commit to a new branch <code>agents/&lt;mission&gt;</code>. You review the diff in the report and push – agents never push.'
          : 'Agents research an isolated snapshot (git worktree) – nothing is changed.'}</p>
      </div>` : '';
    return row({ key, checked: !!sel, icon: '📦', title: r.name, sub: r.description, meta: status,
      badges: why ? badge('suggested', 'sug', why) : '', extra });
  }

  function storeRow(s, why = '') {
    const key = `store|${s.name}`;
    options.set(key, () => storeItem(s));
    return row({ key, checked: has(key), icon: '📚', title: s.name, sub: s.purpose,
      meta: `${s.files} files${s.region ? ` · ${s.region}` : ''}${s.created_by ? ` · by ${s.created_by}` : ''}`,
      badges: (why ? badge('suggested', 'sug', why) : '') + (s.owned ? '' : badge('read-only')) });
  }

  function serverBlock(sv, why = '', q = '') {
    const ro = sv.tools.filter((t) => !t.write);
    const key = `tool|${sv.name}`;
    options.set(key, () => serverItem(sv));
    const tools = sv.tools.filter((t) => !q || matches(`${sv.name} ${t.name} ${t.description}`, q));
    const picked = sv.tools.filter((t) => has(`tool|${t.ref}`)).length;
    const open = st.expanded.has(sv.name) || (q && tools.length) || picked > 0;
    const header = row({
      key, checked: has(key), disabled: !ro.length, icon: '🔌', title: sv.name,
      sub: ro.length ? `All ${ro.length} read-only tools` : 'No read-only tools – pick tools individually',
      meta: `${sv.tools.length} tools${picked ? ` · ${picked} picked individually` : ''}`,
      badges: (why ? badge('suggested', 'sug', why) : '') + (sv.status !== 'ok' ? badge(sv.status, 'warn') : ''),
      aside: sv.tools.length ? `<button type="button" class="pk-expand" data-expand="${esc(sv.name)}">${open ? 'Hide tools ▴' : 'Choose tools ▾'}</button>` : '',
    });
    const list = open ? `<div class="pk-sub">${tools.map((t) => {
      const k = `tool|${t.ref}`;
      options.set(k, () => toolItem(sv, t));
      return row({ key: k, checked: has(k), icon: t.write ? '⚠️' : '🔧', title: t.name, sub: t.description,
        badges: t.write ? badge('changes data', 'warn', 'This tool can create, change or delete data') : '' });
    }).join('') || '<p class="pk-note">No matching tools.</p>'}</div>` : '';
    return `<div class="pk-group">${header}${list}</div>`;
  }

  // ---------------------------------------------------------------- bodies
  function suggestedBody() {
    if (!cat.repos || !cat.kb) return loading('suggestions');
    const sug = suggestions();
    if (!sug.length) {
      return empty('No suggestions for this briefing yet',
        'Mention a repository, system or topic in the objective – or browse the categories on the left.');
    }
    return intro('Suggested for this briefing', 'Matched against the title and objective you wrote. Tick what the agents should use.')
      + section('Repositories', sug.filter((s) => s.type === 'repo').map((s) => repoRow(s.r, s.why)).join(''))
      + section('Knowledge', sug.filter((s) => s.type === 'store').map((s) => storeRow(s.s, s.why)).join(''))
      + section('Tools', sug.filter((s) => s.type === 'server').map((s) => serverBlock(s.sv, s.why)).join(''));
  }

  function reposBody() {
    if (!cat.repos) return loading('repositories');
    const reg = cat.repos.editable ? '<button type="button" class="btn small" data-pk="register">➕ Register repository</button>' : '';
    const sugKeys = new Set(suggestions().map((s) => s.key));
    return intro('Repositories', 'Agents research the code in an isolated git worktree – read-only, or editable on a new mission branch that you review and push.', reg)
      + (cat.repos.repos.map((r) => repoRow(r, sugKeys.has(`repo|${r.name}`) ? 'mentioned in the briefing' : '')).join('')
        || empty('No repositories registered yet', 'Register a remote (GitHub, Azure DevOps …) or a local checkout once – then pick it in any mission.',
          cat.repos.editable ? '<button type="button" class="btn primary" data-pk="register">➕ Register your first repository</button>' : ''))
      + (cat.repos.errors || []).map((e) => `<p class="pk-note err">⚠️ ${esc(e)}</p>`).join('');
  }

  function knowledgeBody() {
    const kb = cat.kb;
    if (!kb) return loading('knowledge bases');
    if (!kb.enabled) return empty('No vector store configured', 'Set VECTOR_STORE_URL and VECTOR_STORE_KEY in .env to use knowledge bases.');
    const own = kb.stores.filter((s) => s.owned);
    const ext = kb.stores.filter((s) => !s.owned);
    return intro('Knowledge', `Linked knowledge bases are searchable by every agent of the mission. Orchestrators can create new ones on their own,
        and finished missions are archived into their region's knowledge.${kb.kind === 'memory' ? ' <b>Mock mode:</b> in-memory stores.' : ''}`)
      + section('Created by agents', own.map((s) => storeRow(s)).join(''))
      + section('Other stores', ext.map((s) => storeRow(s)).join(''))
      + (kb.stores.length ? '' : empty('No knowledge bases yet', 'Orchestrators create them during missions.'));
  }

  function toolsBody() {
    const t = cat.tools;
    if (!t) return loading('tools from the LiteLLM gateway');
    if (!t.available) return empty('No LiteLLM proxy configured', 'Set LITELLM_PROXY_URL to use MCP tools.');
    return intro('Tools', `MCP tools from your LiteLLM gateway. Ticking a server gives the agents all its <b>read-only</b> tools;
        tools that change data must be chosen one by one.${t.mock ? ' <b>Mock mode:</b> tools are simulated, not called.' : ''}`,
      '<button type="button" class="btn small" data-pk="refresh-tools" title="Reload from LiteLLM">⟳ Reload</button>')
      + (t.servers.map((sv) => serverBlock(sv)).join('') || empty('No MCP servers on the gateway'))
      + (t.error ? `<p class="pk-note err">⚠️ ${esc(t.error)}</p>` : '');
  }

  function filesBody() {
    const recent = JSON.parse(localStorage.getItem('al.recentPaths') || '[]');
    const chosen = st.draft.filter((i) => i.kind === 'file' || i.kind === 'path');
    const chosenRows = chosen.map((i) => {
      const k = keyOf(i);
      options.set(k, () => i);
      return row({ key: k, checked: true, icon: i.kind === 'file' ? '📎' : '📁', title: i.label,
        sub: i.kind === 'path' ? i.path : 'uploaded document', meta: i.detail });
    }).join('');
    const recentRows = recent.filter((p) => !has(`path|${p}`)).map((p) => {
      const k = `path|${p}`;
      options.set(k, () => ({ kind: 'path', path: p, label: base(p), title: p, detail: 'folder' }));
      return row({ key: k, checked: false, icon: '🕘', title: base(p), sub: p });
    }).join('');
    const roots = (store.config.context_roots || []).join(', ');
    return intro('Files & folders', 'Upload documents (PDFs and Office files are converted to text locally), or give agents read-only access to a folder on this machine. Secrets like .env stay hidden.')
      + `<div class="pk-drop" data-pk="upload" role="button" tabindex="0">📎 <b>Upload documents</b><small>click or drop here · PDF, Word, PowerPoint, Excel (≤ 25 MB, converted to text) · text / code / markdown (≤ 1 MB)</small></div>
        <div class="pk-inputrow"><input class="pk-path" placeholder="Folder or file path, e.g. ~/git/project" spellcheck="false" autocomplete="off">
          <button type="button" class="btn" data-pk="add-path" disabled>📁 Add</button></div>
        <p class="pk-note pk-path-status">Agents can list, read and search it.${roots ? ` Allowed roots: <code>${esc(roots)}</code>` : ''}</p>`
      + section('Selected', chosenRows) + section('Recent folders', recentRows);
  }

  function webBody() {
    const urls = st.draft.filter((i) => i.kind === 'url').map((i) => {
      const k = keyOf(i);
      options.set(k, () => i);
      return row({ key: k, checked: true, icon: '🔗', title: i.label, sub: i.url });
    }).join('');
    return intro('Web pages', 'Pages are fetched when the mission deploys and handed to the agents as documents.')
      + `<div class="pk-inputrow"><input class="pk-url" type="url" placeholder="https://learn.microsoft.com/…" spellcheck="false" autocomplete="off">
          <button type="button" class="btn" data-pk="add-url">🔗 Add</button></div>`
      + section('Selected', urls);
  }

  function searchBody(q) {
    const repos = (cat.repos?.repos || []).filter((r) => matches(`${r.name} ${r.description} ${(r.keywords || []).join(' ')} ${r.url || r.path || ''}`, q));
    const stores = (cat.kb?.stores || []).filter((s) => matches(`${s.name} ${s.purpose || ''} ${s.region || ''}`, q));
    const servers = (cat.tools?.servers || []).filter((sv) => matches(sv.name, q) || sv.tools.some((t) => matches(`${t.name} ${t.description}`, q)));
    const html = section(`Repositories (${repos.length})`, repos.map((r) => repoRow(r)).join(''))
      + section(`Knowledge (${stores.length})`, stores.map((s) => storeRow(s)).join(''))
      + section(`Tools (${servers.length})`, servers.map((sv) => serverBlock(sv, '', matches(sv.name, q) ? '' : q)).join(''));
    return html || empty(`Nothing matches “${esc(st.q)}”`, 'Files, folders and web pages are added in their own categories.');
  }

  function registerBody() {
    return `<div class="pk-register">
      <button type="button" class="btn small" data-pk="back">← Back to repositories</button>
      <h4>Register a repository</h4>
      <p class="pk-note">Saved to <code>${esc(cat.repos?.file || 'repos.yaml')}</code> and available in every mission. Remote repositories are
        cloned with your own git credentials (credential helper / SSH agent) – nothing is stored here.</p>
      <div class="pk-form">
        <label>Name <small>lowercase letters, digits, . _ -</small><input data-f="name" placeholder="bbraun-copilot-studio" spellcheck="false"></label>
        <div class="pk-field">Source
          <div class="seg pk-src"><label><input type="radio" name="pk-src" value="url" checked><span>🌐 Remote URL</span></label>
            <label><input type="radio" name="pk-src" value="path"><span>💻 Local checkout</span></label></div></div>
        <label class="pk-src-url">Remote URL<input data-f="url" placeholder="https://dev.azure.com/org/project/_git/repo  or  git@github.com:org/repo.git" spellcheck="false"></label>
        <label class="pk-src-path" hidden>Local path<input data-f="path" placeholder="~/git/my-repo" spellcheck="false"></label>
        <label>Base branch <small>optional – default branch of the remote</small><input data-f="branch" placeholder="main" spellcheck="false"></label>
        <label>Description <small>agents read this</small><input data-f="description" placeholder="Copilot Studio solution – topics, flows, connectors"></label>
        <label>Keywords <small>comma-separated – a briefing that mentions one suggests this repository</small><input data-f="keywords" placeholder="copilot studio, b.braun"></label>
      </div>
      <div class="pk-form-actions"><button type="button" class="btn" data-pk="back">Cancel</button>
        <button type="button" class="btn primary" data-pk="save-repo">📦 Register &amp; clone</button></div>
    </div>`;
  }

  // ---------------------------------------------------------------- render
  function counts() {
    const n = (...kinds) => st.draft.filter((i) => kinds.includes(i.kind)).length;
    return { suggested: suggestions().filter((s) => !has(s.key)).length, repos: n('repo'), knowledge: n('store'),
      tools: n('tool'), files: n('file', 'path'), web: n('url') };
  }
  function renderNav() {
    const c = counts();
    nav.innerHTML = CATS.map((x) => `<button type="button" class="pk-cat ${!st.q && st.view === 'list' && st.cat === x.key ? 'active' : ''}" data-cat="${x.key}">
      <span>${x.icon}</span><b>${x.label}</b>${c[x.key] ? `<em class="${x.key === 'suggested' ? 'sug' : ''}" title="${x.key === 'suggested' ? 'suggestions' : 'selected'}">${c[x.key]}</em>` : ''}</button>`).join('');
  }
  function renderFoot() {
    const n = st.draft.length;
    const edits = st.draft.filter((i) => i.kind === 'repo' && i.write).length;
    const writes = st.draft.filter((i) => i.kind === 'tool' && i.state === 'warn').length;
    sum.innerHTML = n
      ? `<b>${n}</b> selected${edits ? ` · ✍️ ${edits} editable repo${edits > 1 ? 's' : ''}` : ''}${writes ? ` · ⚠️ ${writes} tool${writes > 1 ? 's' : ''} that change data` : ''}`
      : 'Nothing selected – agents work from the briefing only';
  }
  function render() {
    options = new Map();
    const q = st.q.trim().toLowerCase();
    const view = `${st.view}|${q ? 'search' : st.cat}`;
    const top = view === st.lastView ? body.scrollTop : 0;
    body.innerHTML = st.view === 'register' ? registerBody() : q ? searchBody(q)
      : { suggested: suggestedBody, repos: reposBody, knowledge: knowledgeBody, tools: toolsBody, files: filesBody, web: webBody }[st.cat]();
    body.scrollTop = top;
    st.lastView = view;
    renderNav();
    renderFoot();
  }

  // ------------------------------------------------------------ open/close
  function open({ items = [], category = null, text = '' } = {}) {
    st.draft = items.map((i) => ({ ...i }));
    st.text = text;
    st.q = '';
    search.value = '';
    st.view = 'list';
    st.auto = !category;
    st.cat = category || (suggestions().some((s) => !has(s.key)) ? 'suggested' : 'repos');
    st.lastView = '';
    el.hidden = false;
    render();
    load(true).then(() => {
      if (st.auto && isOpen() && st.cat !== 'suggested' && suggestions().some((s) => !has(s.key))) { st.cat = 'suggested'; render(); }
      st.auto = false;
    });
    setTimeout(() => (st.cat === 'files' ? body.querySelector('.pk-path') : st.cat === 'web' ? body.querySelector('.pk-url') : search)?.focus(), 40);
    return new Promise((resolve) => { st.resolve = resolve; });
  }
  function finish(result) {
    if (!isOpen()) return;
    el.hidden = true;
    const r = st.resolve;
    st.resolve = null;
    r?.(result);
  }

  // ---------------------------------------------------------------- events
  search.addEventListener('input', () => { st.q = search.value; st.view = 'list'; render(); });
  nav.addEventListener('click', (e) => {
    const c = e.target.closest('[data-cat]')?.dataset.cat;
    if (!c) return;
    st.cat = c;
    st.auto = false;
    st.q = '';
    search.value = '';
    st.view = 'list';
    render();
    if (c === 'files') body.querySelector('.pk-path')?.focus();
    if (c === 'web') body.querySelector('.pk-url')?.focus();
  });

  body.addEventListener('change', (e) => {
    const t = e.target;
    if (t.dataset.toggle) {
      const key = t.dataset.toggle;
      if (t.checked) upsert(options.get(key)());
      else drop(key);
      if (t.checked && key.startsWith('tool|') && !key.includes('/')) st.expanded.delete(key.slice(5));
      render();
    } else if (t.dataset.repoMode) {
      const item = find(`repo|${t.dataset.repoMode}`);
      if (item) {
        item.write = t.value === 'write';
        item.detail = repoDetail(item.write, item.ref, repoOf(item.repo));
      }
      render();
    } else if (t.name === 'pk-src') {
      body.querySelector('.pk-src-url').hidden = t.value !== 'url';
      body.querySelector('.pk-src-path').hidden = t.value !== 'path';
      body.querySelector(t.value === 'url' ? '[data-f=url]' : '[data-f=path]').focus();
    }
  });

  let pathTimer = null;
  body.addEventListener('input', (e) => {
    const t = e.target;
    if (t.dataset.repoRef) {
      const item = find(`repo|${t.dataset.repoRef}`);
      if (item) {
        item.ref = t.value.trim() || undefined;
        item.detail = repoDetail(item.write, item.ref, repoOf(item.repo));
      }
    } else if (t.classList.contains('pk-path')) {
      clearTimeout(pathTimer);
      pathTimer = setTimeout(checkPath, 250);
    }
  });

  async function checkPath() {
    const input = body.querySelector('.pk-path');
    const btn = body.querySelector('[data-pk=add-path]');
    const status = body.querySelector('.pk-path-status');
    if (!input) return;
    const path = input.value.trim();
    st.pathCheck = null;
    if (!path) { btn.disabled = true; return; }
    const res = await get(`/api/context/inspect?path=${encodeURIComponent(path)}`);
    if (!res || input.value.trim() !== path) return;
    st.pathCheck = res.ok ? res : null;
    btn.disabled = !res.ok || (res.kind === 'file' && (!res.text || res.too_big));
    status.className = `pk-note pk-path-status ${res.ok && !btn.disabled ? 'ok' : 'err'}`;
    status.textContent = !res.ok ? `✗ ${res.error}` : res.kind === 'dir'
      ? `✓ Folder · ${res.files}${res.files_capped ? '+' : ''} files · ${res.size_h}${res.types?.length ? ` · ${res.types.join(', ')}` : ''}`
      : !res.text ? '✗ Binary file – not supported' : res.too_big ? `✗ File too large (${res.size_h}) – add its folder`
        : `✓ File · ${res.size_h}`;
  }
  function addPath() {
    const r = st.pathCheck;
    if (!r) return;
    upsert({ kind: 'path', path: r.path, label: r.name, title: r.path,
      detail: r.kind === 'dir' ? `${r.files}${r.files_capped ? '+' : ''} files · ${r.size_h}` : r.size_h });
    const recent = JSON.parse(localStorage.getItem('al.recentPaths') || '[]');
    localStorage.setItem('al.recentPaths', JSON.stringify([r.path, ...recent.filter((p) => p !== r.path)].slice(0, 8)));
    st.pathCheck = null;
    render();
    body.querySelector('.pk-path')?.focus();
  }
  function addUrl() {
    const input = body.querySelector('.pk-url');
    const url = input.value.trim();
    if (!/^https?:\/\/\S+\.\S+/.test(url)) { toast('⚠️', 'Enter a valid http(s) URL', 'error'); return; }
    upsert({ kind: 'url', url, label: url.replace(/^https?:\/\//, '').slice(0, 70), title: url, detail: 'fetched at deploy' });
    render();
    body.querySelector('.pk-url')?.focus();
  }
  async function addFiles(list) {
    const added = await readTextFiles(list, fileChars(st.draft), toast);
    added.forEach(upsert);
    if (added.length) render();
  }
  fileInput.addEventListener('change', async () => { await addFiles(fileInput.files); fileInput.value = ''; });
  body.addEventListener('dragover', (e) => {
    const drop = e.target.closest('.pk-drop');
    if (drop && e.dataTransfer?.types.includes('Files')) { e.preventDefault(); drop.classList.add('drag'); }
  });
  body.addEventListener('dragleave', (e) => e.target.closest('.pk-drop')?.classList.remove('drag'));
  body.addEventListener('drop', async (e) => {
    const drop = e.target.closest('.pk-drop');
    if (!drop || !e.dataTransfer?.files?.length) return;
    e.preventDefault();
    drop.classList.remove('drag');
    await addFiles(e.dataTransfer.files);
  });

  async function saveRepo() {
    const fld = (k) => body.querySelector(`[data-f=${k}]`).value.trim();
    const src = body.querySelector('input[name=pk-src]:checked').value;
    const bodyJson = { name: fld('name').toLowerCase(), url: src === 'url' ? fld('url') || null : null,
      path: src === 'path' ? fld('path') || null : null, branch: fld('branch') || null, description: fld('description'),
      keywords: fld('keywords').split(',').map((k) => k.trim()).filter(Boolean) };
    if (!/^[a-z0-9][a-z0-9._-]{0,63}$/.test(bodyJson.name)) { toast('⚠️', 'Name: lowercase letters, digits, . _ -', 'error'); return; }
    if (!bodyJson.url && !bodyJson.path) { toast('⚠️', src === 'url' ? 'Enter the remote URL' : 'Enter the local path', 'error'); return; }
    const btn = body.querySelector('[data-pk=save-repo]');
    btn.disabled = true;
    btn.textContent = '⏳ Cloning…';
    try {
      const r = await api(`/api/repos/${encodeURIComponent(bodyJson.name)}`, { method: 'PUT', body: JSON.stringify(bodyJson) });
      toast('📦', `Repository <b>${esc(r.name)}</b> registered${r.warning ? ` – ⚠️ ${esc(r.warning)}` : r.base ? ` · ${esc(r.base)}` : ''}`,
        r.warning ? 'error' : 'success');
      cat.repos = (await get('/api/repos')) || cat.repos;
      upsert(repoItem(repoOf(r.name) || r));
      st.view = 'list';
      st.cat = 'repos';
      render();
    } catch {
      btn.disabled = false;
      btn.textContent = '📦 Register & clone';
    }
  }

  el.addEventListener('click', async (e) => {
    const exp = e.target.closest('[data-expand]')?.dataset.expand;
    if (exp) {
      if (st.expanded.has(exp)) st.expanded.delete(exp); else st.expanded.add(exp);
      render();
      return;
    }
    const act = e.target.closest('[data-pk]')?.dataset.pk;
    switch (act) {
      case 'cancel': finish(null); break;
      case 'apply': finish(st.draft); break;
      case 'register': st.view = 'register'; render(); body.querySelector('[data-f=name]')?.focus(); break;
      case 'back': st.view = 'list'; render(); break;
      case 'save-repo': saveRepo(); break;
      case 'upload': fileInput.click(); break;
      case 'add-path': addPath(); break;
      case 'add-url': addUrl(); break;
      case 'refresh-tools':
        cat.tools = null;
        render();
        cat.tools = (await get('/api/tools?refresh=true')) || { available: false, servers: [] };
        render();
        break;
      default:
    }
  });

  // keyboard: Esc steps back / cancels, ⌘↵ applies, Enter in inputs never submits the briefing form
  window.addEventListener('keydown', (e) => {
    if (!isOpen()) return;
    const t = e.target;
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopImmediatePropagation();
      if (st.view === 'register') { st.view = 'list'; render(); } else if (st.q) { st.q = ''; search.value = ''; render(); } else finish(null);
    } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      finish(st.draft);
    } else if (e.key === 'Enter' && el.contains(t)) {
      if (t.classList?.contains('pk-path')) { e.preventDefault(); if (!body.querySelector('[data-pk=add-path]').disabled) addPath(); }
      else if (t.classList?.contains('pk-url')) { e.preventDefault(); addUrl(); }
      else if (t.dataset?.f) { e.preventDefault(); saveRepo(); }
      else if (t.matches?.('.pk-drop')) { e.preventDefault(); fileInput.click(); }
      else if (t.matches?.('input[type=text], input:not([type]), input[type=search]')) e.preventDefault();
    }
  }, true);

  return {
    open,
    isOpen,
    cancel: () => finish(null),
    preload: () => load(false),
    /** Suggestions for a briefing text that are not yet in ``items`` (for the hint under the context list). */
    suggest: (text, items) => {
      const keys = new Set(items.map(keyOf));
      return suggestions(text).filter((s) => !keys.has(s.key));
    },
  };
}
