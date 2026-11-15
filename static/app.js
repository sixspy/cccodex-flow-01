/* 编排流台：组件化 SPA（Svelte 式状态驱动渲染，无构建步骤便于容器验收） */
const state = {
  user: '',
  lab: '',
  items: [],
  selected: null,
  policy: null,
  notice: null,   // {kind:'ok'|'warn'|'error', text, pathMismatch}
  missingOpen: false,
};

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(j.detail || j.error || '请求失败');
    err.payload = j;
    err.status = r.status;
    throw err;
  }
  return j;
}

function postJSON(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

function apply(payload) {
  if (!payload) return;
  if (payload.item) {
    state.selected = payload.item;
    const i = state.items.findIndex((it) => it.id === payload.item.id);
    if (i >= 0) state.items[i] = payload.item;
  }
  if (payload.policy) state.policy = payload.policy;
  if (payload.items) state.items = payload.items;
}

function fail(e) {
  const p = e.payload || {};
  state.notice = {
    kind: e.status === 401 ? 'error' : 'warn',
    text: p.detail || e.message,
    pathMismatch: !!p.path_mismatch,
    lost: !!p.lost,
    reason: p.reason || '',
  };
  return p;
}

async function refresh() {
  const out = await api('/api/flows');
  apply(out);
}

async function boot() {
  try {
    const me = await api('/api/me');
    state.user = me.user;
    state.lab = me.lab;
    await refresh();
    render();
  } catch (e) {
    location.href = '/login';
  }
}

async function select(id) {
  const out = await api('/api/flows/' + id);
  state.selected = out.item;
  state.policy = out.policy;
  state.missingOpen = false;
  state.notice = null;
  render();
}

async function doExtract() {
  try {
    const out = await postJSON('/api/flows/' + state.selected.id + '/extract');
    apply({ item: out });
    await refresh();
    state.notice = { kind: 'ok', text: '抽取完成，等待节点齐套核验' };
  } catch (e) {
    fail(e);
  }
  render();
}

async function doVerify() {
  try {
    const out = await postJSON('/api/flows/' + state.selected.id + '/verify');
    if (out.verifying) {
      state.selected = out.item;
      state.notice = { kind: 'warn', text: '核验进行中，期间齐套策略不可变更' };
      render();
      await pollVerify(state.selected.id);
    } else {
      apply(out);
      const item = out.item || state.selected;
      if (item && item.missing_nodes && item.missing_nodes.length) {
        state.notice = {
          kind: 'warn',
          text: '核验未齐套：缺 ' + item.missing_nodes.join('、'),
        };
      } else {
        state.notice = { kind: 'ok', text: '节点齐套已过闸' };
      }
    }
    await refresh();
    const fresh = await api('/api/flows/' + state.selected.id);
    state.selected = fresh.item;
    state.policy = fresh.policy;
  } catch (e) {
    fail(e);
    await refresh().catch(() => {});
    const fresh = await api('/api/flows/' + state.selected.id).catch(() => null);
    if (fresh) {
      state.selected = fresh.item;
      state.policy = fresh.policy;
    }
  }
  render();
}

async function pollVerify(id) {
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 500));
    const fresh = await api('/api/flows/' + id).catch(() => null);
    if (!fresh) continue;
    state.selected = fresh.item;
    state.policy = fresh.policy;
    render();
    if (fresh.item.gate_state !== '核验中') {
      const missing = fresh.item.missing_nodes || [];
      state.notice = missing.length
        ? { kind: 'warn', text: '核验未齐套：缺 ' + missing.join('、') }
        : { kind: 'ok', text: '节点齐套已过闸' };
      render();
      return;
    }
  }
}

async function doLand() {
  try {
    const out = await postJSON('/api/flows/' + state.selected.id + '/land', {
      path: state.policy ? state.policy.key : null,
    });
    apply(out);
    await refresh();
    const item = out.item || state.selected;
    if (item && item.gate_state === '待复核') {
      state.notice = { kind: 'warn', text: '缺项按策略落库为待复核，列表只读', pathMismatch: false };
    } else {
      state.notice = { kind: 'ok', text: '落库完成' };
    }
  } catch (e) {
    fail(e);
    await refresh().catch(() => {});
    const fresh = await api('/api/flows/' + state.selected.id).catch(() => null);
    if (fresh) {
      state.selected = fresh.item;
      state.policy = fresh.policy;
    }
  }
  render();
}

async function saveNode(field, value) {
  try {
    const out = await api('/api/flows/' + state.selected.id + '/nodes', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [field]: value }),
    });
    apply(out);
    await refresh();
    const fresh = await api('/api/flows/' + state.selected.id);
    state.selected = fresh.item;
    state.notice = { kind: 'ok', text: '节点字段已更新，齐套状态回到待检' };
  } catch (e) {
    fail(e);
  }
  render();
}

async function pickPolicy(key) {
  try {
    const out = await postJSON('/api/policy', { key });
    state.policy = out.policy;
    state.notice = { kind: 'ok', text: '齐套策略已钉死：' + out.policy.name };
  } catch (e) {
    fail(e);
  }
  render();
}

async function logout() {
  await api('/api/logout', { method: 'POST' });
  location.href = '/login';
}

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

const GATE_CLASS = { '待检': 'wait', '核验中': 'running', '已过': 'ok', '待复核': 'review', '已退回': 'bad' };

function gateChip(it) {
  const cls = GATE_CLASS[it.gate_state] || 'wait';
  const text = it.gate_state === '待复核'
    ? `待复核（缺 ${(it.missing_nodes || []).length} 项）`
    : it.gate_state;
  return `<span class="chip gate ${cls}" data-gate="${esc(it.gate_state)}"><b>${esc(text)}</b></span>`;
}

function overviewBar() {
  const counts = { 待检: 0, 已过: 0, 待复核: 0 };
  state.items.forEach((it) => {
    if (counts[it.gate_state] != null) counts[it.gate_state] += 1;
  });
  const policyName = state.policy ? state.policy.name : '—';
  return `
    <div class="overview" data-overview="1">
      <span class="ov-item"><b>${counts['待检']}</b> 待检</span>
      <span class="ov-item ok"><b>${counts['已过']}</b> 已过</span>
      <span class="ov-item review"><b>${counts['待复核']}</b> 待复核</span>
      <span class="ov-policy">策略名 <b>${esc(policyName)}</b></span>
    </div>`;
}

function listRows() {
  return overviewBar() + state.items.map((it) => {
    const picked = state.selected && state.selected.id === it.id ? 'on' : '';
    const ro = it.read_only ? ' readonly' : '';
    return `
    <button type="button" class="row ${picked}${ro}" data-id="${it.id}" data-gate="${esc(it.gate_state)}">
      <strong>${esc(it.title)}</strong>
      <span class="mono">${esc(it.code || '（编码节点空缺）')}</span>
      ${gateChip(it)}
      <span class="meta">${esc(it.status)} · 策略 ${esc(state.policy ? state.policy.name : '—')}${it.verify_lost ? ' · 抢检落败' : ''}</span>
    </button>`;
  }).join('');
}

function missingCard(it) {
  const missing = it.missing || [];
  if (!missing.length) return '';
  const open = state.missingOpen ? ' open' : '';
  const lines = missing.map((m) => `
      <li><span class="mono">${esc(m.field)}</span> <b>${esc(m.label)}</b></li>`).join('');
  const lostNote = it.verify_lost
    ? `<p class="lost">抢检落败：先手已过闸，本次核验不计。</p>`
    : `<p class="hint">争抢核验仅先手那次过；落败一方标「抢检落败」。</p>`;
  return `
    <section class="missing${open}" data-card="missing">
      <button type="button" class="missing-head" id="btn-missing">
        <span class="chip bad">缺项卡片 · ${missing.length} 项</span>
        <span class="missing-sum">${state.missingOpen ? '收起' : '展开'}缺项名</span>
      </button>
      <div class="missing-body">
        <p>${it.gate_state === '待检' ? '尚未核验，以下按当前节点预检：' : '缺哪个节点字段：'}</p>
        <ul>${lines}</ul>
        ${lostNote}
      </div>
    </section>`;
}

function detailView() {
  const it = state.selected;
  if (!it) return '<p class="hint">从左侧选择一条编排流</p>';
  const gate = it.gate_state;
  const policy = state.policy || { key: 'review', name: '缺项待复核', pinned: true, editable: false, options: [] };
  const canExtract = !it.read_only && gate !== '核验中' && (!it.extracted || ['待抽取', '抽取失败'].includes(it.status));
  const verifyLabel = gate === '待检' ? '节点齐套核验' : gate === '核验中' ? '核验中…' : gate === '已过' ? '已过闸' : '待复核';
  // 只有待检可发起核验：先手之后的重试一律落败，不给无效按钮。
  const canVerify = !it.read_only && gate === '待检';
  const extractedReady = it.status === '已抽取待落库';
  const canLand = !it.read_only && gate !== '待检' && gate !== '核验中' && gate !== '已退回' && extractedReady;
  const isBlocked = gate === '已退回';
  const landHint = gate === '待检'
    ? '未核验不能落库'
    : isBlocked
      ? '缺项拦截：整批不许落库，已退回重抽'
      : gate === '待复核'
        ? '待复核按策略落库一次后列表只读'
        : '';
  const nodesLocked = it.read_only || it.status === '已落库' || gate === '核验中';
  const nodeInputs = `
    <div class="nodes">
      <label>标题节点<input id="node-title" value="${esc(it.title)}" ${nodesLocked ? 'disabled' : ''} /></label>
      <label>编码节点<input id="node-code" value="${esc(it.code)}" ${nodesLocked ? 'disabled' : ''} placeholder="编码节点空缺也算缺项" /></label>
      <label>来源节点<input id="node-source" value="${esc(it.source_uri)}" ${nodesLocked ? 'disabled' : ''} /></label>
    </div>`;
  const policyPanel = `
    <section class="policy ${policy.editable ? '' : 'pinned'}" data-policy="${esc(policy.key)}">
      <span class="chip">齐套策略 · ${esc(policy.name)}${policy.pinned ? '（本日钉死）' : ''}</span>
      <span class="mono">${esc(policy.key)}</span>
      <span class="hint">${policy.pinned ? '本日钉死一种，写完当天不许换弦' : ''}</span>
      <div class="policy-opts">
        ${(policy.options || []).map((o) => `<button type="button" class="ghost policy-btn ${o.key === policy.key ? 'on' : ''}" data-key="${esc(o.key)}" ${policy.pinned || policy.editable === false ? 'disabled' : ''}>${esc(o.name)}</button>`).join('')}
      </div>
    </section>`;
  const notice = state.notice
    ? `<p class="notice ${state.notice.kind}${state.notice.pathMismatch ? ' path-mismatch' : ''}" data-path-mismatch="${state.notice.pathMismatch ? '1' : '0'}">${state.notice.pathMismatch ? '⚠ 节点齐套路径偏了 · ' : ''}${esc(state.notice.text)}</p>`
    : '';
  return `
    <section class="detail" data-gate="${esc(gate)}">
      <h2>${esc(it.title)}</h2>
      <p class="mono">${esc(it.source_uri)}</p>
      <p>状态 <span class="chip">${esc(it.status)}</span> · 齐套 ${gateChip(it)} · 已抽取 ${it.extracted} · 已落库 ${it.landed}${it.read_only ? ' · <span class="chip bad">列表只读</span>' : ''}</p>
      <p class="hint">本日策略 <b data-policy-name>${esc(policy.name)}</b>（${esc(policy.key)}）· 齐不齐由服务端裁定</p>
      ${notice}
      ${policyPanel}
      ${nodeInputs}
      ${missingCard(it)}
      <div class="actions">
        <button type="button" id="btn-ex" ${canExtract ? '' : 'disabled'}>执行抽取</button>
        <button type="button" id="btn-verify" class="ghost" ${canVerify ? '' : 'disabled'}>${esc(verifyLabel)}</button>
        <button type="button" id="btn-land" class="ghost" ${canLand ? '' : 'disabled'}>执行落库</button>
      </div>
      ${landHint ? `<p class="hint">${esc(landHint)}</p>` : ''}
      <p class="hint">齐套由服务端裁定；双方争抢核验仅先手那次过。</p>
    </section>`;
}

function render() {
  const el = document.getElementById('app');
  const policyName = state.policy ? state.policy.name : '—';
  el.innerHTML = `
    <header class="bar"><div class="brand">${esc(state.lab)}</div><div>${esc(state.user)} · 齐套策略 ${esc(policyName)} · <button type="button" id="out" class="link">退出</button></div></header>
    <div class="layout"><aside>${listRows()}</aside><main>${detailView()}</main></div>`;
  el.querySelectorAll('.row').forEach((b) => { b.onclick = () => select(+b.dataset.id); });
  const out = el.querySelector('#out'); if (out) out.onclick = logout;
  const be = el.querySelector('#btn-ex'); if (be) be.onclick = doExtract;
  const bv = el.querySelector('#btn-verify'); if (bv) bv.onclick = doVerify;
  const bl = el.querySelector('#btn-land'); if (bl) bl.onclick = doLand;
  const bm = el.querySelector('#btn-missing');
  if (bm) bm.onclick = () => { state.missingOpen = !state.missingOpen; render(); };
  el.querySelectorAll('.policy-btn').forEach((b) => {
    b.onclick = () => pickPolicy(b.dataset.key);
  });
  const bind = (id, field) => {
    const input = el.querySelector(id);
    if (input && !input.disabled) input.onchange = () => saveNode(field, input.value);
  };
  bind('#node-title', 'title');
  bind('#node-code', 'code');
  bind('#node-source', 'source_uri');
}

boot();
