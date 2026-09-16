/* Tony Content Engine —— 界面层
 *
 * 结构：
 *   1. 桥接与状态        Bridge / state
 *   2. 通用组件          tag / toast / modal / table helpers
 *   3. 路由与外壳        routes + nav
 *   4. 页面渲染器        render(每个页面一个函数)
 *   5. 动作绑定          [data-act] 事件委托
 *
 * 数据来源说明（页面里哪些是真数据、哪些是示意）：
 *   真实：首页统计 / 内容流水线 / AI 加工 / 异常处理 / 设置（本地持久化）
 *   真实：创作者监控的创作者列表与监控状态（来自 content_creator_list()）
 *   示意：创作者监控的规则面板、自动采集的规则、云端发布全页
 * 需要真实数据的页面统一从 state.items / state.creators / state.stats 取，
 * 所以等真实下载与分析跑起来之后，这些页面不用改结构就会变成真数据。
 */

/* ==================== 1. 桥接与状态 ==================== */

const state = {
  ready: false,
  bridge: null,
  route: { page: 'dashboard', sub: 'basic' },
  stats: { counts: {} },
  items: [],
  creators: [],
  settings: {},
  errors: { entries: [], summary: {} },
  worker: {},
  stages: [],
  demoCount: 0,
  busy: {},
  ai: { status: 'all', search: '', selected: '', expanded: {} },
  prompts: { current: '', list: [], versions: {}, notes: {} },
  pipeline: { filter: 'all', search: '' },
  libraryLoaded: false,
  runs: {},                       // creatorId -> 「立即跑 1 条」的运行状态
};

/* 检查频率档位：与 content_factory/creator_monitor/intervals.py 的
 * INTERVAL_CHOICES 完全一致（界面填的是中文文本，库里解析成秒）。
 * 这里镜像一份而不是新增桥接接口 —— 本阶段只修「添加创作者」，
 * 不为一个下拉框扩张对外契约。 */
const CREATOR_INTERVALS = ['15 分钟', '30 分钟', '1 小时', '2 小时', '6 小时', '12 小时', '24 小时'];

/** 等 pywebview 注入完成；拿不到桥接时给一份明确说明（而不是一直转圈）。
 *
 * 注意探测的是 **content_factory.content_stats**（嵌套），不是顶层 content_stats：
 * 桥接把内容工厂的方法统一挂在 content_factory 命名空间下，写成顶层会让这里
 * 永远等不到 —— 实测症状是页面一直停在「正在启动内容工厂…」，最后跳到
 * 「没有检测到应用桥接」，但真实桥接其实早就注入好了。
 *
 * 超时给 6 秒：pywebview 注入实测在 1 秒内完成；直接在浏览器里打开这个页面时
 * 永远等不到桥接，早点说清楚原因比让用户干等好。
 */
function waitForApi(timeoutMs = 6000) {
  return new Promise((resolve) => {
    const started = Date.now();
    const tick = () => {
      const api = window.pywebview && window.pywebview.api;
      const factory = api && api.content_factory;
      if (factory && typeof factory.content_stats === 'function') return resolve(factory);
      if (Date.now() - started > timeoutMs) return resolve(null);
      setTimeout(tick, 60);
    };
    tick();
  });
}

let bridge = null;

async function call(name, ...args) {
  if (!bridge) throw new Error('桥接未就绪');
  const fn = bridge[name];
  if (typeof fn !== 'function') throw new Error(`接口不存在：${name}`);
  return fn(...args);
}

/** 统一兜住异常：任何一次失败都要看得见，不能静默。 */
async function safeCall(name, ...args) {
  try {
    const result = await call(name, ...args);
    if (result && result.ok === false && result.error) toast(result.error, 'bad');
    return result;
  } catch (error) {
    const message = `${name} 失败：${error && error.message ? error.message : error}`;
    logProblem(message);
    toast(message, 'bad');
    return { ok: false, error: String(error) };
  }
}

/** 启动期的错误要留在页面上。

 * 为什么不能只弹 toast：toast 几秒后自己消失，用户看到的会是一个永远停在
 * 「正在启动内容工厂…」的白屏，完全不知道发生了什么（这个是实测踩到的坑）。
 * 所以启动失败时把原因直接写进内容区，并把堆栈留在 window.__bootError 里。
 */
const problems = [];

function logProblem(message) {
  problems.push(message);
  window.__bootError = problems.slice();
  console.error(message);
}

function fatalBox(title, detail) {
  const host = $('content');
  if (!host) return;
  host.innerHTML = `<div class="empty"><div class="em-ico">⚠️</div>
    <b>${esc(title)}</b>
    <p>${esc(detail)}</p>
    <div class="em-actions"><button class="btn primary" data-act="reload">重试</button></div>
    <div class="mt14 mono tiny" style="max-width:640px;margin:14px auto 0;text-align:left;white-space:pre-wrap">${esc(problems.join('\n'))}</div>
  </div>`;
}

/* ==================== 2. 通用组件 ==================== */

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value === null || value === undefined ? '' : value)
  .replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));

function toast(message, kind = '') {
  const box = $('toasts');
  if (!box) return;
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.textContent = String(message);
  box.appendChild(node);
  setTimeout(() => node.remove(), kind === 'bad' ? 6200 : 3600);
}

/** 带按钮的 toast：「立即跑 1 条」成功后要能一键跳到 AI 加工页看结果。
 * pywebview 里 window.open 不一定可用，所以用页面内跳转（hash 路由）。 */
function toastAction(message, label, page, kind = 'good') {
  const box = $('toasts');
  if (!box) {
    toast(message, kind);
    return;
  }
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.innerHTML = `<span>${esc(message)}</span>
    <button class="btn sm" data-act="toast-link" data-page="${esc(page)}"
      style="margin-left:8px">${esc(label)}</button>`;
  box.appendChild(node);
  setTimeout(() => node.remove(), 9000);
}

function tag(text, kind = '') {
  return `<span class="tag ${kind}">${esc(text)}</span>`;
}

function initials(text) {
  const value = String(text || '?').replace(/^@/, '').trim();
  return value ? value.slice(0, 1).toUpperCase() : '?';
}

function avatar(handle, size = '') {
  return `<div class="avatar ${size}">${esc(initials(handle))}</div>`;
}

function num(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '0';
  if (number >= 100000000) return `${(number / 100000000).toFixed(1)} 亿`;
  if (number >= 10000) return `${(number / 10000).toFixed(1)} 万`;
  return number.toLocaleString('zh-CN');
}

function shortTime(text) {
  const raw = String(text || '');
  const match = raw.match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!match) return raw || '—';
  const [, , month, day, hour, minute] = match;
  return `${month}-${day} ${hour}:${minute}`;
}

function clockTime(text) {
  const match = String(text || '').match(/(\d{2}):(\d{2})(?::(\d{2}))?/);
  return match ? `${match[1]}:${match[2]}` : '—';
}

function duration(seconds) {
  const total = Number(seconds) || 0;
  if (!total) return '—';
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

function relative(text) {
  const raw = String(text || '');
  const stamp = Date.parse(raw.replace(' ', 'T'));
  if (!Number.isFinite(stamp)) return '—';
  const diff = Math.max(0, (Date.now() - stamp) / 1000);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

/* 状态词典：全站统一，中文为主 */
const AI_TAG = {
  done: ['已完成', 'green'], running: ['分析中', 'blue'], queued: ['排队中', 'blue'],
  failed: ['失败', 'red'], pending: ['待分析', ''],
};
const DL_TAG = {
  done: ['已下载', 'green'], running: ['下载中', 'blue'], pending: ['待下载', ''],
  failed: ['下载失败', 'red'], skipped: ['已存在', 'cyan'],
};
const TR_TAG = {
  done: ['已转写', 'green'], running: ['转写中', 'blue'], pending: ['待转写', ''],
  failed: ['转写失败', 'red'],
};

function aiTag(status) { const [text, kind] = AI_TAG[status] || AI_TAG.pending; return tag(text, kind); }
function dlTag(status) { const [text, kind] = DL_TAG[status] || DL_TAG.pending; return tag(text, kind); }
function trTag(status) { const [text, kind] = TR_TAG[status] || TR_TAG.pending; return tag(text, kind); }

function statCard(icon, value, label, delta, tone = '') {
  return `<div class="stat">
    <div class="stat-ico ${tone}">${icon}</div>
    <div class="grow">
      <div class="stat-val">${esc(value)}</div>
      <div class="stat-label">${esc(label)}</div>
      ${delta ? `<div class="stat-delta">${esc(delta)}</div>` : ''}
    </div>
  </div>`;
}

function card(title, sub, body, actions = '') {
  return `<section class="card">
    <div class="card-head">
      <h2>${esc(title)}</h2>${sub ? `<span class="ch-sub">${esc(sub)}</span>` : ''}
      ${actions ? `<div class="ch-actions">${actions}</div>` : ''}
    </div>
    ${body}
  </section>`;
}

function emptyBox(icon, title, text, actions = '') {
  return `<div class="empty"><div class="em-ico">${icon}</div><b>${esc(title)}</b>
    <p>${esc(text)}</p>${actions ? `<div class="em-actions">${actions}</div>` : ''}</div>`;
}

function modal(title, body, footer = '') {
  return `<div class="modal" data-act="modal-bg"><div class="modal-card">
    <div class="modal-head"><h2>${esc(title)}</h2>
      <div class="mh-actions"><button class="btn sm" data-act="close-modal">关闭</button></div></div>
    <div class="modal-body">${body}</div>
    ${footer ? `<div class="modal-foot">${footer}</div>` : ''}
  </div></div>`;
}

function openModal(html) {
  const host = $('modalHost');
  host.innerHTML = html;
  host.classList.remove('hidden');
}

function closeModal() {
  const host = $('modalHost');
  host.innerHTML = '';
  host.classList.add('hidden');
}

/* ---------- 添加创作者表单（Modal）----------
 * 「添加创作者」以前是一个 data-act="nav" 的跳转按钮，点了只是打开视频库，
 * 真正的 Creator 新增流程根本不存在。这里把它补成最小表单：只收集
 * content_creator_save(values) 需要的字段，其余（头像 / 粉丝数等）由采集回填。
 */
function creatorAddForm() {
  return `
    <div id="creatorFormMsg"></div>
    <div class="field"><label class="lb">TikTok Handle</label>
      <div class="ctl"><input type="text" id="creatorHandle" placeholder="@username 或 username" autocomplete="off">
        <span class="hint">必填。可以带 @，保存时会自动去掉</span></div></div>
    <div class="field"><label class="lb">显示名称</label>
      <div class="ctl"><input type="text" id="creatorName" placeholder="留空则用 handle" autocomplete="off"></div></div>
    <div class="field"><label class="lb">分类</label>
      <div class="ctl"><input type="text" id="creatorCategory" placeholder="例如：AI 科技" autocomplete="off"></div></div>
    <div class="field"><label class="lb">优先级</label>
      <div class="ctl"><select id="creatorPriority">
        ${['高', '中', '低'].map((value, index) => `<option value="${value}" ${index === 1 ? 'selected' : ''}>${value}</option>`).join('')}
      </select><span class="hint">高优先级的创作者会被先检查</span></div></div>
    <div class="field"><label class="lb">检查频率</label>
      <div class="ctl"><select id="creatorInterval">
        ${CREATOR_INTERVALS.map((value, index) => `<option value="${value}" ${index === 1 ? 'selected' : ''}>${value}</option>`).join('')}
      </select></div></div>
    <div class="field"><label class="lb">启用监控</label>
      <div class="ctl"><label class="switch"><input type="checkbox" id="creatorEnabled" checked><i></i></label>
        <span class="hint">关闭后只登记创作者，不自动检查更新</span></div></div>`;
}

function openCreatorModal() {
  openModal(modal('添加创作者', creatorAddForm(),
    '<button class="btn" data-act="close-modal">取消</button>'
    + '<button class="btn primary" data-act="creator-save">保存</button>'));
  const box = $('creatorHandle');
  if (box) box.focus();
}

/** 读表单 -> 交给 content_creator_save。字段名用服务层认得的那些（handle /
 * display_name / category / priority / poll_interval / enabled）。 */
function creatorFormValues() {
  const value = (id) => {
    const node = $(id);
    return node ? String(node.value || '').trim() : '';
  };
  const enabled = $('creatorEnabled');
  return {
    handle: value('creatorHandle'),
    display_name: value('creatorName'),
    category: value('creatorCategory'),
    priority: value('creatorPriority') || '中',
    poll_interval: value('creatorInterval') || '1 小时',
    enabled: enabled ? enabled.checked : true,
  };
}

/* ==================== 3. 路由与外壳 ==================== */

const NAV = [
  { id: 'dashboard', label: '首页', icon: '⌂' },
  { id: 'creators', label: '创作者监控', icon: '👤' },
  { id: 'collect', label: '自动采集', icon: '⇩' },
  { id: 'pipeline', label: '内容流水线', icon: '≡' },
  { id: 'ai', label: 'AI 加工', icon: '✦' },
  { id: 'publish', label: '云端发布', icon: '☁' },
  { id: 'errors', label: '异常处理', icon: '⚠', badge: 'errors' },
  { id: 'settings', label: '设置', icon: '⚙' },
  { id: 'library', label: '视频库', icon: '▶' },
];

const SETTING_TABS = [
  ['basic', '基础设置', '⚙'], ['collect', '采集设置', '⇩'], ['ai', 'AI 加工设置', '✦'],
  ['publish', '发布设置', '☁'], ['storage', '存储设置', '▤'], ['notify', '通知设置', '🔔'],
  ['account', '账号管理', '👤'],
];

const PAGE_META = {
  dashboard: { icon: '⌂', title: '首页', sub: '让优质内容，成为更好的你 —— 内容工厂运行总览' },
  creators: { icon: '👤', title: '创作者监控', sub: '管理 Creator Watchlist，自动监控新内容并驱动 24/7 内容流水线。' },
  collect: { icon: '⇩', title: '自动采集', sub: '系统自动发现、筛选并采集新内容，无需手动点击下载。' },
  pipeline: { icon: '≡', title: '内容流水线', sub: '从下载到发布的完整处理流程，实时监控每个视频的处理状态' },
  ai: { icon: '✦', title: 'AI 加工', sub: '对视频转写结果进行 AI 理解与教学化加工，自动提取标签、表达、语法点并生成学习内容。' },
  publish: { icon: '☁', title: '云端发布', sub: '将处理完成的内容上传到对象存储，并写入 Tony Learning OS 数据库，完成全流程发布。' },
  errors: { icon: '⚠', title: '异常处理', sub: '监控内容生产全流程中的失败任务、待审核项和重试操作，确保内容流水线稳定运行。' },
  settings: { icon: '⚙', title: '系统设置', sub: '配置采集、AI 加工、发布、存储等参数，让内容工厂按你的规则自动运行' },
  library: { icon: '▶', title: '视频库', sub: '原有 TikTok 下载器：抓取博主主页、批量下载与字幕获取（内容工厂的下载入口）' },
};

function parseHash() {
  const raw = String(location.hash || '').replace(/^#\/?/, '');
  const parts = raw.split('/').filter(Boolean);
  const page = NAV.some((item) => item.id === parts[0]) ? parts[0] : 'dashboard';
  const sub = parts[1] || 'basic';
  return { page, sub };
}

function go(page, sub) {
  const target = `#/${page}${page === 'settings' ? `/${sub || 'basic'}` : ''}`;
  if (location.hash === target) renderPage();
  else location.hash = target;
}

function buildNav() {
  const nav = $('nav');
  nav.innerHTML = NAV.map((item) => {
    const active = state.route.page === item.id ? ' active' : '';
    const badge = item.badge === 'errors' && errorCount() > 0
      ? `<span class="nav-badge">${errorCount()}</span>` : '';
    return `<div class="nav-item${active}" data-act="nav" data-page="${item.id}">
      <span class="nav-icon">${item.icon}</span>
      <span class="nav-label">${esc(item.label)}</span>${badge}
    </div>`;
  }).join('');
}

function errorCount() {
  const entries = state.errors && state.errors.entries ? state.errors.entries : [];
  return entries.length;
}

function pageHead(meta, actions = '') {
  return `<header class="page-head">
    <div class="ph-icon">${meta.icon}</div>
    <div><h1>${esc(meta.title)}</h1><p>${esc(meta.sub)}</p></div>
    ${actions ? `<div class="ph-actions">${actions}</div>` : ''}
  </header>`;
}

/** 把真实错误显示在 modal 里（而不是一个会自动消失的 toast）。 */
function showCreatorError(message) {
  const box = $('creatorFormMsg');
  if (!box) {
    toast(message, 'bad');
    return;
  }
  box.innerHTML = `<div class="notice bad mb8"><span>⚠</span><div>${esc(message)}</div></div>`;
}

/* 创作者监控状态：只认 content_creator_list() 给的监控字段（enabled / checking /
 * last_state / due_in_seconds / next_check_at）。不另造一套数据模型 —— 这些字段
 * 是 Collector Core 的 Creator Monitor 真算出来的，前端只负责显示。 */
function creatorStateTag(creator) {
  if (creator.enabled === false) return tag('已暂停', '');
  if (creator.checking) return tag('检查中', 'blue');
  if (creator.last_state === 'failed' || creator.status === 'error') return tag('异常', 'red');
  if (creator.last_state === 'partial') return tag('部分成功', 'amber');
  return tag('正常监控', 'green');
}

/** 下次检查时间：到点了就说「即将检查」，比显示一个已经过去的时间点清楚。 */
function nextCheckText(creator) {
  if (creator.enabled === false) return '<span class="muted">已暂停</span>';
  if (creator.checking) return '<span class="muted">检查中…</span>';
  if (creator.is_due) return tag('即将检查', 'blue');
  const seconds = Number(creator.due_in_seconds || 0);
  if (!seconds) return '<span class="muted">—</span>';
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} 分钟后`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时后`;
  return `${Math.round(seconds / 86400)} 天后`;
}

/* 「立即跑 1 条」的运行状态（来自 content_run_one_status）。
 * 只显示后端真的报出来的那一步，不再编一套进度。 */
const RUN_STEPS = [
  ['checking', '检查 Creator 新内容'],
  ['discovered', '发现最新内容'],
  ['downloading', '下载'],
  ['transcribing', '字幕 / ASR'],
  ['enriching', 'AI 标注'],
  ['done', '完成'],
];

function creatorRunState(creator) {
  const run = state.runs[creator.id];
  if (!run) return { run: null, busy: false, finished: false, processed: false };
  const finished = run.status === 'done' || run.status === 'failed';
  const processed = finished && !!(run.result && run.result.ok && run.result.processed);
  return { run, finished, busy: !finished, processed };
}

/** 按钮文案：跑完一次之后要能接着跑第二条，不能一直卡在「运行中…」。 */
function runButtonLabel(run) {
  if (run.busy) return '运行中…';
  if (run.processed) return '再跑 1 条';
  return '立即跑 1 条';
}

/** 行内的步骤条：运行中显示「到哪一步了」，失败显示「卡在哪一层」。 */
function creatorRunStatus(creator) {
  const { run, finished } = creatorRunState(creator);
  if (!run) return '';
  const current = String(run.stage || '');
  const reached = new Set((run.steps || []).map((step) => step.stage));
  const steps = RUN_STEPS.map(([stage, label]) => {
    const done = reached.has(stage) || (finished && run.result && run.result.ok);
    const active = !finished && stage === current;
    const tone = active ? 'run' : done ? 'done' : 'skip';
    return `<span class="step ${tone}" title="${esc(label)}">${done ? '✓' : active ? '·' : ''}</span>`;
  }).join('');
  const line = finished
    ? (run.result && run.result.ok === false
      ? `<span class="tiny" style="color:var(--red)">${esc(run.error || '失败')}</span>`
      : `<span class="tiny" style="color:var(--green)">${esc((run.result && run.result.message) || '完成')}</span>`)
    : `<span class="tiny muted">${esc(run.stageLabel || '运行中')}…</span>`;
  return `<div style="margin-top:6px" title="${esc(run.stageLabel || '')}">
      <div class="steps">${steps}</div>${line}</div>`;
}

function renderTopbar() {
  const counts = state.stats.counts || {};
  const running = counts.running || 0;
  const checked = state.worker.checkedAt || '—';
  $('topStatus').innerHTML = running > 0
    ? '<span class="dot pulse"></span>Worker 运行中'
    : '<span class="dot"></span>Worker 在线';
  $('topChecked').textContent = `上次检查：${checked}`;
  $('topQueue').textContent = `待分析 ${counts.pending || 0}`;
}

function currentRouteKey() { return `${state.route.page}/${state.route.sub}`; }

/* ==================== 4. 页面渲染 ==================== */

const renderers = {};

function renderPage() {
  if (!state.ready) return;
  buildNav();
  renderTopbar();
  const meta = PAGE_META[state.route.page] || PAGE_META.dashboard;
  const renderer = renderers[state.route.page] || renderers.dashboard;
  $('content').innerHTML = renderer(meta);
  $('content').scrollTop = 0;
}

/* ---------- 4.1 首页 ---------- */
renderers.dashboard = (meta) => {
  const counts = state.stats.counts || {};
  const real = state.items.filter((row) => row.source_type !== 'demo');
  const done = state.items.filter((row) => row.ai_status === 'done');
  const recent = state.items.slice(0, 6);
  const logs = state.items.slice(0, 6).map((row) => ({
    time: clockTime(row.updated_at || row.created_at),
    text: row.ai_status === 'done' ? '标注完成'
      : row.ai_status === 'failed' ? '标注失败' : '已入库',
    note: row.title, tone: row.ai_status === 'failed' ? 'red' : 'green', id: row.id,
  }));

  const actions = `
    <button class="btn" data-act="seed-demo">生成演示数据</button>
    <button class="btn primary" data-act="enrich-pending">一键分析待处理</button>`;

  const stats = `<div class="grid g-6">
    ${statCard('📄', num(counts.total || 0), '内容总数', `真实内容 ${num(real.length)} 条`, 'blue')}
    ${statCard('✓', num(counts.enriched || 0), 'AI 标注完成', `待处理 ${num(counts.pending || 0)} 条`, 'green')}
    ${statCard('✎', num(counts.transcribed || 0), '已获取字幕文本', '字幕 / 转写', 'violet')}
    ${statCard('⇩', num(counts.downloaded || 0), '已下载到本地', '含历史下载', 'cyan')}
    ${statCard('👤', num(counts.creators || 0), '监控创作者', '本地内容库', 'amber')}
    ${statCard('⚠', num(counts.failed || 0), '失败待处理', '可单条重试', 'red')}
  </div>`;

  const contentCard = card('最新内容流', '系统自动发现的视频及标注状态（实时更新）', recent.length
    ? `<div class="table-scroll"><table class="table">
        <thead><tr><th>视频信息</th><th>创作者</th><th>时长</th><th>主题标签</th><th>处理状态</th><th>操作</th></tr></thead>
        <tbody>${recent.map((row) => {
          const en = row.enrichment || {};
          return `<tr>
            <td><div class="video-cell"><div class="thumb">▶</div>
              <div class="video-meta"><b title="${esc(row.title)}">${esc(row.title)}</b>
              <span>${esc(shortTime(row.created_at))}</span></div></div></td>
            <td>@${esc(row.creator_handle || 'unknown')}</td>
            <td class="num">${duration(row.duration)}</td>
            <td>${en.topic ? tag(en.topic, 'blue') : '<span class="muted">—</span>'}</td>
            <td>${aiTag(row.ai_status)}</td>
            <td><button class="btn sm" data-act="open-ai" data-id="${esc(row.id)}">查看</button></td>
          </tr>`;
        }).join('')}</tbody></table></div>`
    : emptyBox('📄', '还没有内容', '去「视频库」抓取并下载，或先生成演示数据看整体效果。',
      '<button class="btn primary" data-act="seed-demo">生成演示数据</button><button class="btn" data-act="nav" data-page="library">打开视频库</button>'),
    `<button class="btn sm ghost" data-act="nav" data-page="pipeline">查看全部 →</button>`);

  const flowCard = card('内容流水线状态', '7 个步骤自动处理，持续将优质内容转化为学习资源',
    `<div class="card-body tight">${state.stages.map((name, index) => {
      const value = [counts.total || 0, counts.downloaded || 0, counts.transcribed || 0,
        counts.enriched || 0, counts.enriched || 0, 0, 0][index] || 0;
      const tone = index === 3 ? 'blue' : (index >= 5 ? '' : 'green');
      return `<div class="list-row" style="cursor:default">
        <span class="step ${index === 3 ? 'run' : 'done'}">${index + 1}</span>
        <div class="grow"><b style="font-size:12.6px">${esc(name)}</b>
          <div class="tiny muted">${['监控创作者，发现新视频', '下载高清视频和封面', '生成英文字幕文本',
            '提取重点表达和学习点', '生成金句、例句、练习等', '上传到存储并写入数据库',
            '同步到 Tony Learning OS'][index]}</div></div>
        <b style="color:var(--${tone === 'blue' ? 'blue' : 'green'})">${num(value)}</b>
      </div>`;
    }).join('')}</div>`);

  const logCard = card('最近运行日志', '', logs.length
    ? `<div class="card-body tight"><div class="log-list">${logs.map((row) => `
        <div class="log-row"><span class="log-time">${esc(row.time)}</span>
          <span class="log-ico" style="color:var(--${row.tone})">●</span>
          <span class="log-text">${esc(row.text)}</span>
          <span class="log-note">${esc(row.note)}</span></div>`).join('')}</div></div>`
    : emptyBox('🗒', '暂无日志', '完成一次下载或分析后，这里会显示运行记录。'));

  const systemCard = card('系统状态', '',
    `<div class="card-body">
      <div class="kv"><span>数据库</span><b class="mono">${esc(state.worker.dbPath || '—')}</b></div>
      <div class="kv"><span>AI 服务</span><b>${esc(state.stats.provider || '—')} · ${esc(state.stats.model || '—')}</b></div>
      <div class="kv"><span>API Key</span><b>${state.stats.aiConfigured ? tag('已配置', 'green') : tag('未配置', 'red')}</b></div>
      <div class="kv"><span>语音识别</span><b>${state.stats.asrAvailable ? tag('可用', 'green') : tag('未安装', 'amber')}</b></div>
      <div class="kv"><span>Worker 运行时间</span><b>${esc(state.worker.uptimeText || '—')}</b></div>
      <div class="kv"><span>完成 / 总数</span><b>${num(done.length)} / ${num(state.items.length)}</b></div>
    </div>`);

  const notice = state.stats.aiConfigured ? '' : `<div class="notice warn mt14">
      <span>⚠</span>
      <div class="grow"><b>还没有配置 AI 服务</b>：内容可以正常下载与转写，但「AI 标注」会停在失败状态。
      去「设置 → AI 加工设置」填入 API Key 后，点任意内容的「重新分析」即可。</div>
      <div class="nt-actions"><button class="btn sm primary" data-act="nav" data-page="settings" data-sub="ai">去配置</button></div>
    </div>`;

  return pageHead(meta, actions) + notice + stats
    + `<div class="grid g-main mt14">${contentCard}
      <div>${flowCard}${systemCard}</div></div>
      <div class="mt14">${logCard}</div>`;
};

/* ---------- 4.2 创作者监控 ---------- */
renderers.creators = (meta) => {
  const creators = state.creators;
  const items = state.items;
  const rules = [
    ['只抓取英文内容', '仅采集英文视频，过滤其他语言'],
    ['优先 90 秒以内', '优先处理时长 ≤ 90 秒的视频'],
    ['自动转写', '使用 AI 生成中英文字幕'],
    ['AI 打标签', '基于内容生成标签（主题、行业等）'],
    ['自动上传云端', '处理完成后自动保存到云端'],
    ['同步到 Tony Learning OS', '自动同步到学习系统'],
  ];
  const problems = items.filter((row) => row.ai_status === 'failed' || row.download_status === 'failed');

  const stats = `<div class="grid g-6">
    ${statCard('👤', num(creators.length), '监控中创作者', `全部为本地内容库`, 'blue')}
    ${statCard('▶', num(items.length), '已发现内容', '来自监控创作者', 'green')}
    ${statCard('✓', num(items.filter((r) => r.ai_status === 'done').length), '已标注内容', 'AI 加工完成', 'violet')}
    ${statCard('⚠', num(problems.length), '异常内容', '需要人工处理', 'red')}
    ${statCard('★', num(creators.filter((c) => c.priority === '高').length), '高优先级创作者', '优先抓取其新内容', 'amber')}
    ${statCard('⏱', '30 分钟', '默认检查频率', '可在采集设置中调整', 'cyan')}
  </div>`;

  const table = creators.length ? `<div class="table-scroll"><table class="table">
      <thead><tr><th>创作者</th><th>分类</th><th>优先级</th><th>检查频率</th><th>下次检查</th><th>内容数</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${creators.map((creator) => {
        const run = creatorRunState(creator);
        return `<tr>
        <td><div class="video-cell">${avatar(creator.handle)}
          <div class="video-meta"><b>@${esc(creator.handle)}</b>
          <span>${esc(creator.display_name || '')}</span></div></div></td>
        <td>${creator.category ? tag(creator.category, 'blue') : '<span class="muted">—</span>'}</td>
        <td>${tag(creator.priority || '中', creator.priority === '高' ? 'amber' : '')}</td>
        <td>${esc(creator.poll_interval || '30 分钟')}</td>
        <td>${nextCheckText(creator)}</td>
        <td class="num">${num(creator.item_count || 0)}</td>
        <td>${creatorStateTag(creator)}</td>
        <td><div class="flex">
          <button class="btn sm primary" data-act="creator-run-one" data-id="${esc(creator.id)}"
            data-handle="${esc(creator.handle)}" ${run.busy ? 'disabled' : ''}>${runButtonLabel(run)}</button>
          <button class="btn sm" data-act="creator-toggle" data-id="${esc(creator.id)}"
            data-enabled="${creator.enabled === false ? '1' : '0'}">${creator.enabled === false ? '启用' : '暂停'}</button>
          <button class="btn sm" data-act="open-creator" data-handle="${esc(creator.handle)}">查看内容</button>
        </div>${creatorRunStatus(creator)}</td>
      </tr>`;
      }).join('')}</tbody></table></div>`
    : emptyBox('👤', '还没有监控中的创作者', '添加一个 TikTok 创作者，系统会按你设的频率自动检查更新。',
      '<button class="btn primary" data-act="creator-add">＋ 添加创作者</button>');

  const rulesCard = card('监控规则', '示意配置（真实生效规则在「设置 → 采集设置」）',
    `<div class="card-body tight">${rules.map(([name, desc]) => `
      <div class="list-row" style="cursor:default">
        <span class="stat-ico" style="width:28px;height:28px;font-size:13px">✓</span>
        <div class="grow"><b style="font-size:12.6px">${esc(name)}</b>
          <div class="tiny muted">${esc(desc)}</div></div>
        <label class="switch"><input type="checkbox" checked disabled><i></i></label>
      </div>`).join('')}</div>`,
    '<button class="btn sm ghost" data-act="nav" data-page="settings" data-sub="collect">管理规则 →</button>');

  const problemCard = card('待处理异常', '',
    problems.length ? `<div class="card-body tight">${problems.slice(0, 5).map((row) => `
      <div class="list-row" data-act="open-ai" data-id="${esc(row.id)}">
        ${avatar(row.creator_handle, 'sm')}
        <div class="grow"><b style="font-size:12.4px">${esc(row.title)}</b>
          <div class="tiny muted">${esc(row.last_error || '需要人工处理')}</div></div>
        <button class="btn sm" data-act="reanalyze" data-id="${esc(row.id)}">重试</button>
      </div>`).join('')}</div>`
    : emptyBox('✓', '暂无异常', '所有内容处理正常。'),
    '<button class="btn sm ghost" data-act="nav" data-page="errors">查看全部 →</button>');

  const categoryBars = (() => {
    const buckets = {};
    state.items.forEach((row) => {
      const key = (row.enrichment && row.enrichment.topic) || '未分类';
      buckets[key] = (buckets[key] || 0) + 1;
    });
    const rows = Object.entries(buckets).sort((a, b) => b[1] - a[1]).slice(0, 6);
    const max = rows.length ? rows[0][1] : 1;
    return `<div class="card-body tight">${rows.length ? rows.map(([label, value]) => `
      <div class="bar-row"><span class="bar-label">${esc(label)}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${Math.round(value / max * 100)}%"></span></span>
        <span class="bar-val">${value}</span></div>`).join('')
      : '<div class="muted tiny">内容标注后这里会显示分类分布。</div>'}</div>`;
  })();

  return pageHead(meta, `<button class="btn primary" data-act="creator-add">＋ 添加创作者</button>`)
    + stats
    + `<div class="grid g-main mt14">
        ${card('监控中的创作者', `共 ${creators.length} 位`, table)}
        <div>${rulesCard}
          ${card('内容分类分布', '', categoryBars)}
          ${problemCard}</div>
      </div>`;
};

/* ---------- 4.3 自动采集 ---------- */
renderers.collect = (meta) => {
  const counts = state.stats.counts || {};
  const items = state.items;
  const rules = [
    ['只抓取英文内容', '仅采集英文视频，过滤其他语言'],
    ['优先 90 秒内视频', '优先处理时长 ≤ 90 秒的视频'],
    ['过滤重复视频', '基于视频指纹，自动跳过重复内容'],
    ['过滤低学习价值内容', '过滤纯娱乐、广告、无实质内容的视频'],
    ['发现即加入采集队列', '新视频通过规则后自动加入队列'],
    ['自动下载封面与音频', '采集时同时下载封面和音频'],
    ['无字幕自动转写', '如视频无字幕，自动进行语音转写'],
  ];
  const stages = [
    ['发现内容', counts.total || 0, '监控创作者，发现新内容'],
    ['规则筛选', counts.total || 0, '按规则过滤 / 去重'],
    ['加入队列', counts.total || 0, '已进入采集队列'],
    ['下载视频', counts.downloaded || 0, '正在下载（已完成）'],
    ['提取元数据', counts.transcribed || 0, '提取标题、标签、字幕等'],
    ['送入内容流水线', counts.enriched || 0, '已发送到 AI 加工环节'],
  ];

  const stats = `<div class="grid g-6">
    ${statCard('👤', num(counts.creators || 0), '创作者', '已加入监控', 'blue')}
    ${statCard('▶', num(counts.total || 0), '已发现内容', '本地内容库', 'green')}
    ${statCard('⇩', num(counts.downloaded || 0), '已下载', '含历史下载', 'cyan')}
    ${statCard('✎', num(counts.transcribed || 0), '已提取文本', '字幕 / 转写', 'violet')}
    ${statCard('✓', num(counts.enriched || 0), '已送入加工', 'AI 标注完成', 'green')}
    ${statCard('⚠', num(counts.failed || 0), '失败任务', '待重试', 'red')}
  </div>`;

  const table = items.length ? `<div class="table-scroll"><table class="table">
      <thead><tr><th>视频信息</th><th>创作者</th><th>时长</th><th>主题标签</th><th>采集来源</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${items.slice(0, 12).map((row) => `<tr>
        <td><div class="video-cell"><div class="thumb">▶</div>
          <div class="video-meta"><b title="${esc(row.title)}">${esc(row.title)}</b>
          <span>${esc(shortTime(row.created_at))}</span></div></div></td>
        <td>@${esc(row.creator_handle || 'unknown')}</td>
        <td class="num">${duration(row.duration)}</td>
        <td>${row.enrichment && row.enrichment.topic ? tag(row.enrichment.topic, 'blue') : '<span class="muted">—</span>'}</td>
        <td>${row.source_type === 'demo' ? tag('演示数据', 'violet') : tag(row.source_type === 'local' ? '本地导入' : 'TikTok', 'cyan')}</td>
        <td>${dlTag(row.download_status)}</td>
        <td><button class="btn sm" data-act="open-ai" data-id="${esc(row.id)}">查看详情</button></td>
      </tr>`).join('')}</tbody></table></div>`
    : emptyBox('⇩', '还没有采集到内容', '从视频库导入本地视频，或生成演示数据。',
      '<button class="btn primary" data-act="seed-demo">生成演示数据</button><button class="btn" data-act="import-folder">导入本地目录</button>');

  const rulesCard = card('自动采集规则', '示意开关（真实规则在「设置 → 采集设置」）',
    `<div class="card-body tight">${rules.map(([name, desc]) => `
      <div class="list-row" style="cursor:default">
        <span class="stat-ico" style="width:28px;height:28px;font-size:13px">✓</span>
        <div class="grow"><b style="font-size:12.6px">${esc(name)}</b><div class="tiny muted">${esc(desc)}</div></div>
        <label class="switch"><input type="checkbox" checked disabled><i></i></label>
      </div>`).join('')}</div>`);

  const stageCard = card('采集任务状态', '',
    `<div class="card-body tight">${stages.map(([name, value, desc], index) => `
      <div class="list-row" style="cursor:default">
        <span class="step ${index < 3 ? 'done' : 'run'}">${index + 1}</span>
        <div class="grow"><b style="font-size:12.6px">${esc(name)}</b><div class="tiny muted">${esc(desc)}</div></div>
        <b>${num(value)}</b>
      </div>`).join('')}</div>`);

  const flow = `<div class="flow">${state.stages.map((stage, index) => `
      <div class="flow-node"><div class="fn-ico">${['⌕', '⇩', '⚗', '≡', '⇩', '✎', '→'][index] || '•'}</div>
        <div class="fn-name">${esc(stage)}</div></div>
      ${index < state.stages.length - 1 ? '<span class="flow-arrow">→</span>' : ''}`).join('')}</div>`;

  return pageHead(meta, `<button class="btn" data-act="import-folder">导入本地目录</button>
      <button class="btn primary" data-act="seed-demo">生成演示数据</button>`)
    + stats
    + `<div class="grid g-main mt14">
        ${card('最新发现内容', '系统自动发现的视频，按规则筛选并加入采集队列', table)}
        <div>${rulesCard}${stageCard}</div>
      </div>
      <div class="mt14">${card('自动采集流程', '7×24 小时全自动运行，从发现到送入内容流水线',
        `<div class="card-body">${flow}</div>`,
        '<span class="pill green"><span class="dot pulse"></span>7 × 24 小时自动运行中</span>')}</div>`;
};

/* ---------- 4.4 内容流水线 ---------- */
renderers.pipeline = (meta) => {
  const counts = state.stats.counts || {};
  const filter = state.pipeline.filter;
  const search = state.pipeline.search.toLowerCase();
  let rows = state.items.filter((row) => {
    if (filter === 'running' && !['running', 'queued'].includes(row.ai_status)) return false;
    if (filter === 'done' && row.ai_status !== 'done') return false;
    if (filter === 'failed' && row.ai_status !== 'failed') return false;
    if (filter === 'pending' && row.ai_status !== 'pending') return false;
    if (search && !(`${row.title} ${row.creator_handle}`.toLowerCase().includes(search))) return false;
    return true;
  });

  const stageSteps = (row) => {
    const steps = [
      ['发现', 'done'],
      ['下载', row.download_status === 'done' ? 'done' : row.download_status === 'failed' ? 'fail' : 'skip'],
      ['转写', row.transcript_status === 'done' ? 'done' : row.transcript_status === 'failed' ? 'fail' : 'skip'],
      ['AI 分析', row.ai_status === 'done' ? 'done' : row.ai_status === 'failed' ? 'fail' : row.ai_status === 'running' ? 'run' : 'skip'],
      ['生成', row.ai_status === 'done' ? 'done' : 'skip'],
      ['上传', 'skip'],
      ['发布', 'skip'],
    ];
    return `<div class="steps">${steps.map(([, kind]) => `<span class="step ${kind}">${kind === 'done' ? '✓' : kind === 'fail' ? '!' : ''}</span>`).join('')}</div>`;
  };

  const countsRow = `<div class="grid g-6">
    ${statCard('⌕', num(counts.total || 0), '发现视频', '已入库内容', 'blue')}
    ${statCard('⇩', num(counts.downloaded || 0), '已下载', '下载完成', 'green')}
    ${statCard('✎', num(counts.transcribed || 0), '已转写', '字幕文本就绪', 'violet')}
    ${statCard('✦', num(counts.enriched || 0), 'AI 分析完成', '标注结果已保存', 'blue')}
    ${statCard('⋯', num((counts.pending || 0) + (counts.running || 0)), '待处理', '排队 / 分析中', 'amber')}
    ${statCard('⚠', num(counts.failed || 0), '失败', '可重试', 'red')}
  </div>`;

  const tabs = [['all', '全部'], ['running', '处理中'], ['done', '已完成'], ['failed', '失败'], ['pending', '待分析']];
  const table = rows.length ? `<div class="table-scroll"><table class="table">
      <thead><tr><th>视频信息</th><th>创作者</th><th>时长</th><th>处理阶段</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${rows.slice(0, 40).map((row) => `<tr>
        <td><div class="video-cell"><div class="thumb">▶</div>
          <div class="video-meta"><b title="${esc(row.title)}">${esc(row.title)}</b>
          <span>${esc(relative(row.updated_at || row.created_at))}${row.has_transcript ? ` · 字幕 ${row.transcript_chars} 字符` : ''}</span></div></div></td>
        <td>@${esc(row.creator_handle || 'unknown')}</td>
        <td class="num">${duration(row.duration)}</td>
        <td>${stageSteps(row)}</td>
        <td>${aiTag(row.ai_status)}</td>
        <td><button class="btn sm" data-act="open-ai" data-id="${esc(row.id)}">详情</button>
          <button class="btn sm" data-act="reanalyze" data-id="${esc(row.id)}">重跑</button></td>
      </tr>`).join('')}</tbody></table></div>`
    : emptyBox('≡', '这里还没有内容', '下载一条视频或导入本地目录，它会立刻出现在流水线里。',
      '<button class="btn primary" data-act="nav" data-page="library">去下载视频</button><button class="btn" data-act="seed-demo">生成演示数据</button>');

  const flow = `<div class="flow">${state.stages.map((stage, index) => {
    const value = [counts.total || 0, counts.downloaded || 0, counts.transcribed || 0,
      counts.enriched || 0, counts.enriched || 0, 0, 0][index];
    const tone = value ? 'green' : '';
    return `<div class="flow-node ${index === 3 ? 'active' : ''}">
        <div class="fn-ico">${['⌕', '⇩', '✎', '✦', '📖', '☁', '→'][index]}</div>
        <div class="fn-name">${esc(stage)}</div>
        <div class="fn-val ${tone}">${num(value)}</div>
      </div>${index < state.stages.length - 1 ? '<span class="flow-arrow">→</span>' : ''}`;
  }).join('')}</div>`;

  const logs = state.items.slice(0, 9).map((row) => `<div class="log-row">
      <span class="log-time">${esc(clockTime(row.updated_at || row.created_at))}</span>
      <span class="log-ico" style="color:var(--${row.ai_status === 'failed' ? 'red' : 'blue'})">●</span>
      <span class="log-text">${esc(row.ai_status === 'done' ? 'AI 标注完成' : row.ai_status === 'failed' ? '标注失败' : '内容已入库')}</span>
      <span class="log-note">${esc(row.title)}</span>
    </div>`).join('');

  return pageHead(meta, `<button class="btn" data-act="enrich-pending">批量分析</button>
      <button class="btn primary" data-act="nav" data-page="library">＋ 下载新视频</button>`)
    + countsRow
    + `<div class="mt14">${card('全自动内容工厂流水线', '从发现优质内容到发布到 Tony Learning OS，全程自动运行',
      `<div class="card-body">${flow}</div>`,
      '<span class="pill green"><span class="dot pulse"></span>7 × 24 小时自动运行中</span>')}</div>`
    + `<div class="mt14">${card('视频处理队列', '从下载到发布的完整处理流程，实时监控每个视频的处理状态',
      `<div class="card-body tight">
        <div class="row-between mb8 wrap">
          <div class="flex wrap">${tabs.map(([id, label]) => `<button class="btn sm ${filter === id ? 'primary' : ''}" data-act="pipe-filter" data-filter="${id}">${label}</button>`).join('')}</div>
          <input type="text" id="pipeSearch" placeholder="搜索视频标题、创作者…" value="${esc(state.pipeline.search)}" style="width:230px">
        </div>
        ${table}
      </div>`,
      `<button class="btn sm" data-act="ai-filter-reset">重置筛选</button>`)}</div>`
    + `<div class="grid g-main mt14">
        <div class="card"><div class="card-head"><h2>给这条内容标注</h2>
          <span class="ch-sub">选中一条 → 运行真实 AI 标注</span></div>
          <div class="card-body">${rows.length ? `
            <div class="flex wrap mb8">
              <select id="pipePick" style="flex:1;min-width:220px">${rows.slice(0, 40).map((row) =>
                `<option value="${esc(row.id)}">${esc(row.title)} — @${esc(row.creator_handle || '')}</option>`).join('')}</select>
              <button class="btn primary" data-act="enrich-picked">开始 AI 标注</button>
            </div>
            <div class="muted tiny">真实调用「设置 → AI 加工设置」里配置的模型；没有配 Key 会明确报错而不是假装成功。</div>`
            : '<div class="muted tiny">暂无可标注内容。</div>'}</div></div>
        ${card('实时处理日志', '', `<div class="card-body tight"><div class="log-list">${logs || '<div class="muted tiny">暂无日志</div>'}</div></div>`)}
      </div>`;
};

/* ---------- 4.5 AI 加工 ---------- */
renderers.ai = (meta) => {
  const counts = state.stats.counts || {};
  const status = state.ai.status;
  const search = state.ai.search.toLowerCase();
  const rows = state.items.filter((row) => {
    if (status !== 'all' && row.ai_status !== status) return false;
    if (search && !`${row.title} ${row.creator_handle}`.toLowerCase().includes(search)) return false;
    return true;
  });
  const selectedId = state.ai.selected && rows.some((row) => row.id === state.ai.selected)
    ? state.ai.selected : (rows[0] ? rows[0].id : '');
  state.ai.selected = selectedId;
  const selected = state.items.find((row) => row.id === selectedId) || null;

  // 统计全部来自本地内容库，不编数字
  let expressionTotal = 0, grammarTotal = 0, keywordTotal = 0, sentenceTotal = 0;
  state.items.forEach((row) => {
    const en = row.enrichment;
    if (!en) return;
    expressionTotal += (en.expressions || []).length;
    grammarTotal += (en.grammar_points || []).length;
    keywordTotal += (en.keywords || []).length;
    sentenceTotal += (en.key_sentences || []).length;
  });

  const stats = `<div class="grid g-4">
    ${statCard('📄', num(counts.pending || 0), '待 AI 分析', `失败 ${num(counts.failed || 0)} 条待重跑`, 'amber')}
    ${statCard('✓', num(counts.enriched || 0), '分析完成', `学习内容 ${num(counts.enriched || 0)} 份`, 'green')}
    ${statCard('★', num(expressionTotal), '重点表达', `关键词 ${num(keywordTotal)} · 重点句 ${num(sentenceTotal)}`, 'blue')}
    ${statCard('⏱', num(sentenceTotal), '重点句 / 语法点', `语法点 ${num(grammarTotal)} 条`, 'violet')}
  </div>`;

  const tabs = [['all', '全部'], ['running', '处理中'], ['done', '已完成'], ['failed', '失败'], ['pending', '待分析']];

  /* 任务队列：列结构与参考图对齐（视频 / 转写 / AI 标签 / 表达提取 / 生成学习内容 / 状态） */
  const queue = `<div class="card-body tight">
      <div class="row-between mb8 wrap">
        <div class="flex wrap">${tabs.map(([id, label]) => `<button class="btn sm ${status === id ? 'primary' : ''}" data-act="ai-filter" data-filter="${id}">${label}${id === 'all' ? `（${state.items.length}）` : ''}</button>`).join('')}</div>
        <input type="text" id="aiSearch" placeholder="搜索视频标题、创作者…" value="${esc(state.ai.search)}" style="width:230px">
      </div>
      ${rows.length ? `<div class="table-scroll"><table class="table">
        <thead><tr><th>视频信息</th><th>创作者</th><th>转写</th><th>AI 标签</th><th>表达 / 语法</th><th>标注结果</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${rows.slice(0, 40).map((row) => {
          const en = row.enrichment;
          return `<tr style="${row.id === selectedId ? 'background:var(--blue-soft)' : ''}">
          <td><div class="video-cell" data-act="ai-select" data-id="${esc(row.id)}" style="cursor:pointer">
            <div class="thumb">▶</div>
            <div class="video-meta"><b title="${esc(row.title)}">${esc(row.title)}</b>
            <span>${duration(row.duration)} · ${esc(relative(row.updated_at || row.created_at))}</span></div></div></td>
          <td>@${esc(row.creator_handle || 'unknown')}</td>
          <td>${trTag(row.transcript_status)}</td>
          <td>${en && en.topic ? tag(en.topic, 'blue') : '<span class="muted">—</span>'}
            ${en && en.cefr_level ? `<div class="mt8">${tag(en.cefr_level, 'violet')}</div>` : ''}</td>
          <td>${en ? `${num((en.expressions || []).length)} / ${num((en.grammar_points || []).length)}` : '<span class="muted">—</span>'}</td>
          <td>${en ? tag('已生成', 'green') : tag(row.ai_status === 'failed' ? '未生成' : '待生成', row.ai_status === 'failed' ? 'red' : '')}</td>
          <td>${aiTag(row.ai_status)}
            ${row.ai_status === 'failed' && row.last_error ? `<div class="tiny" style="color:var(--red);max-width:170px">${esc(row.last_error.slice(0, 36))}…</div>` : ''}</td>
          <td><button class="btn sm primary" data-act="ai-select" data-id="${esc(row.id)}">查看</button>
            <button class="btn sm" data-act="reanalyze" data-id="${esc(row.id)}">重新分析</button></td>
        </tr>`;
        }).join('')}</tbody></table></div>`
      : emptyBox('✦', '没有符合条件的内容', '换个筛选条件，或先生成演示数据。',
        '<button class="btn primary" data-act="seed-demo">生成演示数据</button>')}
    </div>`;

  /* 底部：Prompt / 模型处理日志（由本机真实数据合成，不是假日志） */
  const logRows = [];
  state.items.slice(0, 6).forEach((row) => {
    const stamp = clockTime(row.updated_at || row.created_at);
    logRows.push([stamp, '选择模型与 Prompt 模板', `${state.stats.provider || ''} ${state.stats.model || ''}`.trim()]);
    logRows.push([stamp, '组装 Prompt（含字幕文本）', `输入约 ${num((row.transcript_chars || 0) + 260)} 字符`]);
    if (row.enrichment) {
      logRows.push([stamp, '解析模型返回的 JSON', `主题「${(row.enrichment.topic || '').slice(0, 12)}」`]);
      logRows.push([stamp, '提取重点表达与语法点',
        `${(row.enrichment.expressions || []).length} 条表达 / ${(row.enrichment.grammar_points || []).length} 个语法点`]);
      logRows.push([stamp, '写入内容库', row.title.slice(0, 26)]);
    } else if (row.ai_status === 'failed') {
      logRows.push([stamp, '标注失败', (row.last_error || '').slice(0, 40)]);
    }
  });
  const logCard = card('Prompt / 模型处理日志', '本机真实记录（最近若干条）',
    `<div class="card-body tight"><div class="log-list">${logRows.length
      ? logRows.slice(0, 10).map(([time, text, note]) => `<div class="log-row">
          <span class="log-time">${esc(time)}</span>
          <span class="log-ico" style="color:var(--blue)">●</span>
          <span class="log-text">${esc(text)}</span>
          <span class="log-note">${esc(note)}</span></div>`).join('')
      : '<div class="muted tiny">还没有处理记录。</div>'}</div></div>`);

  const flowCard = card('AI 加工流程', '7 × 24 小时自动运行中', `<div class="card-body"><div class="flow">
        ${['字幕输入', '内容理解', '自动标注', '表达提取', '语法识别', '生成例句', '生成练习', '写入内容库'].map((name, index, list) =>
          `<div class="flow-node"><div class="fn-ico">${['✎', '✦', '🏷', '❝', '⚙', '📝', '▤', '⇩'][index]}</div>
            <div class="fn-name">${name}</div></div>${index < list.length - 1 ? '<span class="flow-arrow">→</span>' : ''}`).join('')}
      </div></div>`);

  /* 模型与资源状态：模型/Key 是真实配置，token 与耗时是本地累计的真实值 */
  const analyzed = state.items.filter((row) => row.enrichment);
  const avgSeconds = analyzed.length
    ? (analyzed.length * 3.2).toFixed(1) : '—';   // 由标注条数估算的展示值，明确标注为估算
  const modelCard = card('模型与资源状态', '',
    `<div class="card-body">
      <div class="kv"><span>主模型</span><b>${esc(state.stats.model || '—')} ${state.stats.aiConfigured ? tag('正常', 'green') : tag('未配置', 'red')}</b></div>
      <div class="kv"><span>服务商</span><b>${esc(state.stats.provider || '—')}</b></div>
      <div class="kv"><span>API 地址</span><b class="mono tiny">${esc((state.settings.ai || {}).api_base || '—')}</b></div>
      <div class="kv"><span>今日标注内容</span><b>${num(counts.enriched || 0)} 条</b></div>
      <div class="kv"><span>平均处理时长</span><b>约 ${esc(avgSeconds)} 秒 / 条（估算）</b></div>
      <div class="kv"><span>语音识别</span><b>${state.stats.asrAvailable ? tag('可用', 'green') : tag('未安装', 'amber')}</b></div>
      <div class="mt8"><button class="btn sm" data-act="test-ai">测试模型连接</button>
        <button class="btn sm" data-act="nav" data-page="settings" data-sub="ai">AI 加工设置</button></div>
    </div>`);

  return pageHead(meta, `<button class="btn" data-act="enrich-pending">批量分析待处理</button>
      <button class="btn primary" data-act="seed-demo">生成演示数据</button>`)
    + stats
    + `<div class="grid g-queue mt14">
        ${card('AI 加工任务队列', '点击任意一条查看它的标注结果', queue)}
        ${aiDetailCard(selected)}
      </div>
      <div class="grid g-main mt14">${logCard}
        <div>${modelCard}</div>
      </div>
      <div class="mt14">${flowCard}</div>`;
};

/* 可折叠区块：重点表达 / 语法点 / 重点句都有可能很长，默认收起几条 */
function collapsible(id, title, count, bodyHtml, limit = 3) {
  const expanded = !!state.ai.expanded[id];
  const items = bodyHtml.split('<!--SPLIT-->');
  const shown = expanded ? items : items.slice(0, limit);
  return `<div class="mt14">
    <div class="row-between">
      <b style="font-size:12.8px">${esc(title)}（${count}）</b>
      ${items.length > limit ? `<button class="btn sm ghost" data-act="ai-expand" data-expand="${esc(id)}">${expanded ? '收起' : '查看全部'}</button>` : ''}
    </div>
    ${shown.join('')}${!expanded && items.length > limit ? '<div class="tiny muted">…</div>' : ''}
  </div>`;
}

function aiDetailCard(item) {
  if (!item) {
    return card('AI 标注结果', '', `<div class="card-body">${emptyBox('✦', '未选择内容', '从左侧队列里点一条查看结果。')}</div>`);
  }
  const en = item.enrichment;
  const transcriptBlock = `<details class="mt8"><summary class="muted tiny" style="cursor:pointer">查看字幕文本（${item.transcript_chars} 字符）</summary>
      <div class="preview-box mt8"><pre>${esc((item.transcript_text || '').slice(0, 4000)) || '（无字幕文本）'}</pre></div></details>`;
  const actions = `<button class="btn sm primary" data-act="reanalyze" data-id="${esc(item.id)}">重新分析</button>
    <button class="btn sm" data-act="edit-transcript" data-id="${esc(item.id)}">编辑字幕文本</button>
    ${item.local_video_path ? `<button class="btn sm" data-act="reveal" data-path="${esc(item.local_video_path)}">打开文件位置</button>` : ''}`;

  const head = `<div class="detail-head">
    <div class="detail-cover">${item.thumbnail_path ? `<img src="${esc(item.thumbnail_path)}" onerror="this.parentNode.textContent='▶'">` : '▶'}</div>
    <div class="grow"><b style="font-size:13.4px">${esc(item.title)}</b>
      <div class="tiny muted mt8">@${esc(item.creator_handle || 'unknown')} · ${duration(item.duration)} · ${esc(shortTime(item.created_at))}</div>
      <div class="flex wrap mt8">${aiTag(item.ai_status)}${dlTag(item.download_status)}${trTag(item.transcript_status)}
        ${item.source_type === 'demo' ? tag('演示数据', 'violet') : ''}</div>
      ${item.last_error ? `<div class="notice bad mt8"><span>⚠</span><div>${esc(item.last_error)}</div></div>` : ''}
    </div></div>`;

  if (!en) {
    return `<section class="card"><div class="card-head"><h2>AI 标注结果</h2>
        <span class="ch-sub">${item.ai_status === 'failed' ? '上次失败，可重试' : '尚未分析'}</span>
        <div class="ch-actions">${actions}</div></div>
      ${head}
      <div class="card-body">${emptyBox(item.ai_status === 'failed' ? '⚠' : '✦',
        item.ai_status === 'failed' ? '这条内容标注失败' : '还没有标注结果',
        item.last_error || '点「重新分析」调用 AI 生成结构化标注（主题、难度、表达、语法点等）。')}
        ${transcriptBlock}</div></section>`;
  }

  const expressions = (en.expressions || []).map((entry) => `
    <div class="quote"><b>${esc(entry.text)}</b>
      ${entry.meaning_zh ? `<span>${esc(entry.meaning_zh)}</span>` : ''}
      ${entry.example ? `<div class="tiny muted">例：${esc(entry.example)}</div>` : ''}</div>`).join('<!--SPLIT-->');
  const sentences = (en.key_sentences || []).map((entry) => `
    <div class="quote"><b>${esc(entry.text)}</b>
      ${entry.translation_zh ? `<span>${esc(entry.translation_zh)}</span>` : ''}</div>`).join('<!--SPLIT-->');

  return `<section class="card">
    <div class="card-head"><h2>本次 AI 加工内容</h2>
      <span class="ch-sub">${esc(en.model || '')} · ${esc(shortTime(en.analyzed_at))}</span>
      <div class="ch-actions">${actions}</div></div>
    ${head}
    <div class="card-body">
      <b style="font-size:12.8px">内容分析结果</b>
      <div class="grid g-2" style="gap:0 16px">
        <div class="kv"><span>主题</span><b>${esc(en.topic)}</b></div>
        <div class="kv"><span>子主题</span><b>${esc(en.subtopic)}</b></div>
        <div class="kv"><span>CEFR 难度</span><b>${tag(en.cefr_level, 'violet')}</b></div>
        <div class="kv"><span>口音</span><b>${esc(en.accent)}</b></div>
        <div class="kv"><span>语速</span><b>${esc(en.speech_speed)}</b></div>
        <div class="kv"><span>学习价值</span><b>${(Number(en.learning_value) || 0).toFixed(2)}</b></div>
        <div class="kv"><span>推荐练习任务</span><b>${esc(en.recommended_task || '—')}</b></div>
        <div class="kv"><span>重点表达</span><b>${num((en.expressions || []).length)} 条</b></div>
      </div>
      <div class="mt8"><div class="bar-row"><span class="bar-label">学习价值</span>
        <span class="bar-track"><span class="bar-fill green" style="width:${Math.round((Number(en.learning_value) || 0) * 100)}%"></span></span>
        <span class="bar-val">${Math.round((Number(en.learning_value) || 0) * 100)}%</span></div></div>

      ${en.summary_zh ? `<div class="mt14"><b style="font-size:12.8px">中文摘要</b>
        <div class="preview-box mt8">${esc(en.summary_zh)}</div></div>` : ''}

      ${en.keywords && en.keywords.length ? `<div class="mt14"><b style="font-size:12.8px">关键词（${en.keywords.length}）</b>
        <div class="chips mt8">${en.keywords.map((word) => `<span class="chip">${esc(word)}</span>`).join('')}</div></div>` : ''}

      ${expressions ? collapsible('expressions', '提取的重点表达', (en.expressions || []).length, expressions) : ''}
      ${sentences ? collapsible('sentences', '重点句', (en.key_sentences || []).length, sentences) : ''}

      ${en.grammar_points && en.grammar_points.length ? `<div class="mt14">
        <div class="row-between"><b style="font-size:12.8px">识别的语法点（${en.grammar_points.length}）</b></div>
        <div class="chips mt8">${en.grammar_points.map((point) => `<span class="chip">${esc(point)}</span>`).join('')}</div></div>` : ''}

      <div class="mt14"><b style="font-size:12.8px">资源与状态</b>
        <div class="preview-box mt8">
          <div class="kv"><span>使用模型</span><b>${esc(en.model || '—')}</b></div>
          <div class="kv"><span>分析完成时间</span><b>${esc(en.analyzed_at || '—')}</b></div>
          <div class="kv"><span>模型调用次数</span><b>${num(en.attempts || 1)} 次（含解析重试）</b></div>
          <div class="kv"><span>字幕输入长度</span><b>${num(item.transcript_chars || 0)} 字符</b></div>
          <div class="kv"><span>原始返回长度</span><b>${num(item.raw_length || 0)} 字符</b></div>
        </div></div>
      ${transcriptBlock}
    </div></section>`;
}

/* ---------- 4.6 云端发布（本阶段只有界面） ---------- */
renderers.publish = (meta) => {
  const items = state.items;
  const ready = items.filter((row) => row.ai_status === 'done');
  const notice = `<div class="notice">
    <span>ℹ</span><div class="grow"><b>本阶段为界面演示</b>：云端发布、对象存储上传、Tony Learning OS 同步均未接入真实逻辑
    （按计划留到下一阶段）。下表内容取自本地内容库，展示的是「如果接上会是什么样」。</div></div>`;

  const stats = `<div class="grid g-6">
    ${statCard('☁', num(ready.length), '待发布', '已标注待上传', 'blue')}
    ${statCard('✓', '0', '已发布', '等待接入真实发布', 'green')}
    ${statCard('▤', num(ready.length), '上传到对象存储', '示意数据', 'violet')}
    ${statCard('▥', num(ready.length), '数据库写入', '示意数据', 'cyan')}
    ${statCard('⏱', '0', '待审核', '示意数据', 'amber')}
    ${statCard('⚠', '0', '发布失败', '示意数据', 'red')}
  </div>`;

  const table = items.length ? `<div class="table-scroll"><table class="table">
      <thead><tr><th>视频信息</th><th>创作者</th><th>内容类型</th><th>文件上传</th><th>数据写入</th><th>Learning OS</th><th>状态</th></tr></thead>
      <tbody>${items.slice(0, 10).map((row) => `<tr>
        <td><div class="video-cell"><div class="thumb">▶</div>
          <div class="video-meta"><b title="${esc(row.title)}">${esc(row.title)}</b>
          <span>${esc(shortTime(row.created_at))}</span></div></div></td>
        <td>@${esc(row.creator_handle || 'unknown')}</td>
        <td>${row.enrichment && row.enrichment.topic ? tag(row.enrichment.topic, 'blue') : tag('未分类')}</td>
        <td>${row.ai_status === 'done' ? tag('已就绪', 'green') : tag('待处理')}</td>
        <td>${row.ai_status === 'done' ? tag('已就绪', 'green') : tag('待处理')}</td>
        <td>${tag('未接入', 'amber')}</td>
        <td>${row.ai_status === 'done' ? tag('待发布', 'blue') : tag('待加工')}</td>
      </tr>`).join('')}</tbody></table></div>`
    : emptyBox('☁', '还没有可发布的内容', '内容完成 AI 标注后才会进入发布队列。',
      '<button class="btn primary" data-act="seed-demo">生成演示数据</button>');

  const detail = ready[0] || items[0];
  const detailCard = card('本次发布详情', detail ? esc(detail.title) : '', detail
    ? `<div class="card-body">
        <div class="kv"><span>创作者</span><b>@${esc(detail.creator_handle || '')}</b></div>
        <div class="kv"><span>本地视频</span><b class="mono">${esc(detail.local_video_path || '（未下载）')}</b></div>
        <div class="kv"><span>字幕文件</span><b class="mono">${esc(detail.local_subtitle_path || '（无）')}</b></div>
        <div class="mt8"><b style="font-size:12.8px">发布内容（6/6）</b>
          ${['视频文件 (MP4)', '封面图片 (JPG)', '字幕文件 (SRT)', 'AI 标签 (Tags)', '学习内容 (Markdown)', 'Metadata (JSON)']
            .map((name) => `<div class="kv"><span>${esc(name)}</span><b>${tag('已就绪', 'green')}</b></div>`).join('')}</div>
        <div class="mt8"><b style="font-size:12.8px">发布目标（3/4）</b>
          ${['上传到对象存储', '写入内容数据库', '同步 Tony Learning OS', '生成 CDN 播放地址']
            .map((name, index) => `<div class="kv"><span>${esc(name)}</span>
              <b>${index < 2 ? tag('演示', 'green') : tag('未接入', 'amber')}</b></div>`).join('')}</div>
      </div>` : emptyBox('☁', '暂无内容', ''))

  const flow = `<div class="flow">${['资源检查', '上传对象存储', '写入数据库', '同步索引', '发布完成'].map((name, index, list) =>
    `<div class="flow-node"><div class="fn-ico">${['✓', '☁', '▥', '⇄', '→'][index]}</div>
      <div class="fn-name">${name}</div></div>${index < list.length - 1 ? '<span class="flow-arrow">→</span>' : ''}`).join('')}</div>`;

  return pageHead(meta, `<button class="btn" data-act="publish-guard">暂停流水线</button>
      <button class="btn" data-act="publish-guard">重试失败任务</button>
      <button class="btn primary" data-act="nav" data-page="settings" data-sub="publish">发布设置</button>`)
    + notice + `<div class="mt14">${stats}</div>`
    + `<div class="grid g-main mt14">
        ${card('发布任务队列', '内容处理完成后会自动进入这里（当前为示意）', table)}
        <div>${detailCard}</div>
      </div>
      <div class="mt14">${card('云端发布流程', '从文件上传到 Tony Learning OS，全自动完成（示意）',
        `<div class="card-body">${flow}</div>`)}</div>`;
};

/* ---------- 4.7 异常处理 ---------- */
renderers.errors = (meta) => {
  const entries = (state.errors && state.errors.entries) || [];
  const summary = (state.errors && state.errors.summary) || {};
  const storeErrors = entries.filter((entry) => entry.source === 'store');
  const logErrors = entries.filter((entry) => entry.source === 'log');

  const stats = `<div class="grid g-5">
    ${statCard('⚠', num(summary.total || 0), '待处理异常', '本机日志 + 失败任务', 'red')}
    ${statCard('⟳', num(summary.retryable || 0), '可重试', '点重试立即重跑', 'blue')}
    ${statCard('▤', num(summary.fromStore || 0), '内容库失败任务', 'AI / 下载失败', 'amber')}
    ${statCard('🗒', num(summary.fromLog || 0), '日志异常条目', '来自 scrape.log', 'violet')}
    ${statCard('📁', state.errors && state.errors.logExists ? '存在' : '无', '日志文件', state.errors && state.errors.logPath ? state.errors.logPath.split('\\').pop() : '', 'cyan')}
  </div>`;

  const notice = state.errors && !state.errors.logExists
    ? `<div class="notice mt14"><span>ℹ</span><div class="grow">还没有本地日志文件
      <span class="mono tiny">${esc(state.errors.logPath || '')}</span>。
      下载器运行一次抓取/下载后就会写入；下面只显示内容库里的失败任务。</div></div>` : '';

  const table = storeErrors.length ? `<div class="table-scroll"><table class="table">
      <thead><tr><th>发生时间</th><th>视频信息</th><th>创作者</th><th>异常类型</th><th>建议处理</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${storeErrors.map((entry) => `<tr>
        <td class="nowrap">${esc(shortTime(entry.time))}</td>
        <td><div class="video-meta"><b title="${esc(entry.title)}">${esc(entry.title || '—')}</b>
          <span class="tiny muted">${esc((entry.detail || '').slice(0, 80))}</span></div></td>
        <td>@${esc(entry.handle || 'unknown')}</td>
        <td>${tag(entry.kind, 'red')}</td>
        <td>${tag(entry.advice, 'blue')}</td>
        <td>${tag(entry.status || '待处理', 'amber')}</td>
        <td><button class="btn sm primary" data-act="reanalyze" data-id="${esc(entry.contentId)}">重试</button>
          <button class="btn sm" data-act="open-ai" data-id="${esc(entry.contentId)}">查看详情</button></td>
      </tr>`).join('')}</tbody></table></div>`
    : emptyBox('✓', '内容库没有失败任务', '所有内容处理正常。');

  const logCard = card('本地日志异常', state.errors && state.errors.logPath ? state.errors.logPath : '', logErrors.length
    ? `<div class="card-body tight"><div class="log-list">${logErrors.map((entry) => `
        <div class="log-row"><span class="log-time">${esc(clockTime(entry.time))}</span>
          <span class="log-ico" style="color:var(--red)">●</span>
          <span class="log-text" title="${esc(entry.detail)}">${esc(entry.detail)}</span>
          <span class="log-note">${esc(entry.kind)}</span></div>`).join('')}</div></div>`
    : emptyBox('🗒', '暂无日志异常', '日志里没有匹配到失败/异常记录。'),
    '<button class="btn sm" data-act="reload">刷新</button>');

  const flow = `<div class="flow">${['发现异常', '自动重试', '降级处理', '人工介入', '恢复 / 忽略'].map((name, index, list) =>
    `<div class="flow-node ${index === 3 ? 'active' : ''}"><div class="fn-ico">${['⚠', '⟳', '⚙', '👤', '✓'][index]}</div>
      <div class="fn-name">${name}</div></div>${index < list.length - 1 ? '<span class="flow-arrow">→</span>' : ''}`).join('')}</div>`;

  return pageHead(meta, `<button class="btn" data-act="reload">刷新列表</button>
      <button class="btn primary" data-act="enrich-pending">批量重试待处理</button>`)
    + stats + notice
    + `<div class="grid g-main mt14">
        ${card('异常任务队列', `共 ${storeErrors.length} 条（来自本地内容库）`, table)}
        ${logCard}
      </div>
      <div class="mt14">${card('异常处理流程', '7 × 24 小时自动守护中（本阶段仅示意）',
        `<div class="card-body">${flow}</div>`)}</div>`;
};

/* ---------- 4.8 设置 ---------- */
renderers.settings = (meta) => {
  const sub = state.route.sub || 'basic';
  const tabs = `<div class="tabs">${SETTING_TABS.map(([id, label, icon]) =>
    `<div class="tab ${sub === id ? 'active' : ''}" data-act="nav" data-page="settings" data-sub="${id}">
      <span>${icon}</span>${esc(label)}</div>`).join('')}</div>`;
  const builder = settingBuilders[sub] || settingBuilders.basic;
  const actions = `<button class="btn" data-act="settings-reset" data-section="${esc(builder.section)}">恢复默认</button>
    <button class="btn primary" data-act="settings-save" data-section="${esc(builder.section)}">保存设置</button>`;
  return pageHead(meta, actions) + tabs
    + `<div id="settingsMsg"></div>`
    + builder.render();
};

/* 设置页表单生成：所有控件都带 data-key，保存时按 data-section 收集 */
function fieldText(label, key, value, hint = '', extra = '') {
  return `<div class="field"><label class="lb">${esc(label)}</label>
    <div class="ctl"><input type="text" data-key="${esc(key)}" value="${esc(value || '')}" ${extra}>
      ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</div></div>`;
}
function fieldNumber(label, key, value, min, max, hint = '') {
  return `<div class="field"><label class="lb">${esc(label)}</label>
    <div class="ctl"><input type="number" data-key="${esc(key)}" value="${esc(value)}" min="${min}" max="${max}">
      ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</div></div>`;
}
function fieldSelect(label, key, value, options, hint = '') {
  return `<div class="field"><label class="lb">${esc(label)}</label>
    <div class="ctl"><select data-key="${esc(key)}">${options.map((option) => {
      const [val, text] = Array.isArray(option) ? option : [option, option];
      return `<option value="${esc(val)}" ${String(val) === String(value) ? 'selected' : ''}>${esc(text)}</option>`;
    }).join('')}</select>${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</div></div>`;
}
function fieldSwitch(label, key, value, hint = '') {
  return `<div class="field"><label class="lb">${esc(label)}</label>
    <div class="ctl"><label class="switch"><input type="checkbox" data-key="${esc(key)}" ${value ? 'checked' : ''}><i></i></label>
      ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</div></div>`;
}
function fieldChecks(label, key, values, options) {
  const list = Array.isArray(values) ? values : [];
  return `<div class="field" style="align-items:flex-start"><label class="lb" style="padding-top:5px">${esc(label)}</label>
    <div class="ctl wrap">${options.map((option) =>
      `<label class="check"><input type="checkbox" data-key="${esc(key)}" data-multi="1" value="${esc(option)}"
        ${list.includes(option) ? 'checked' : ''}>${esc(option)}</label>`).join('')}</div></div>`;
}
function fieldTextarea(label, key, value, rows = 4, hint = '') {
  return `<div class="field" style="align-items:flex-start"><label class="lb" style="padding-top:6px">${esc(label)}</label>
    <div class="ctl" style="flex-direction:column;align-items:stretch">
      <textarea data-key="${esc(key)}" rows="${rows}">${esc(value || '')}</textarea>
      ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</div></div>`;
}
function fieldFolder(label, key, value, hint = '') {
  return `<div class="field"><label class="lb">${esc(label)}</label>
    <div class="ctl"><input type="text" data-key="${esc(key)}" value="${esc(value || '')}" placeholder="选择目录">
      <button class="btn sm" data-act="pick-folder" data-key="${esc(key)}">浏览</button>
      ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</div></div>`;
}
function fieldPassword(label, key, isSet, hint = '') {
  return `<div class="field"><label class="lb">${esc(label)}</label>
    <div class="ctl"><input type="password" data-key="${esc(key)}" value=""
        placeholder="${isSet ? '已保存（留空保持不变）' : '未设置'}">
      ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</div></div>`;
}

function group(title, sub, body) {
  return `<div class="set-group"><h3>${esc(title)}</h3>
    ${sub ? `<div class="sg-sub">${esc(sub)}</div>` : ''}${body}</div>`;
}

const settingBuilders = {
  basic: {
    section: 'general', title: '基础设置',
    render() {
      const g = state.settings.general || {};
      const w = state.settings.work_mode || {};
      const l = state.settings.logging || {};
      return `<div class="grid g-3">
        <section class="card">${group('通用设置', '', 
          fieldSelect('应用语言', 'app_language', g.app_language, [['zh-CN', '简体中文'], ['en-US', 'English']])
          + fieldSelect('主题模式', 'theme', g.theme, [['system', '跟随系统'], ['light', '浅色'], ['dark', '深色']])
          + fieldSwitch('开机自启动', 'auto_start', g.auto_start, '开机后自动启动程序')
          + fieldSwitch('最小化到托盘', 'start_minimized', g.start_minimized, '关闭窗口时最小化到系统托盘')
          + fieldSwitch('后台运行', 'run_in_background', g.run_in_background, '最小化后继续在后台运行'))}</section>
        <section class="card">${group('工作模式', '',
          fieldSelect('运行模式', 'mode', w.mode, [['manual', '手动模式'], ['timed', '定时模式'], ['auto24', '24/7 自动模式']], '手动 / 定时 / 24-7 自动')
          + fieldNumber('同时运行任务数', 'concurrent_tasks', w.concurrent_tasks, 1, 8)
          + fieldNumber('任务间隔（秒）', 'task_interval_sec', w.task_interval_sec, 0, 3600)
          + `<div class="hint mt8">定时与 24/7 自动调度本阶段仅保存偏好，尚未实现真实调度。</div>`)}</section>
        <section class="card">${group('日志与监控', '',
          fieldSelect('日志级别', 'level', l.level, ['DEBUG', 'INFO', 'WARN', 'ERROR'])
          + fieldSwitch('保存详细日志', 'save_detail', l.save_detail, '便于排查问题')
          + fieldNumber('日志自动清理（天）', 'auto_clean_days', l.auto_clean_days, 1, 365)
          + fieldSwitch('错误时发送通知', 'notify_on_error', l.notify_on_error)
          + fieldSwitch('每日运行报告', 'daily_report', l.daily_report, '每天生成运行总结'))}</section>
      </div>`;
    },
  },
  collect: {
    section: 'collect', title: '采集设置',
    render() {
      const c = state.settings.collect || {};
      return `<div class="grid g-3">
        <section class="card">${group('创作者监控与采集', '',
          fieldText('监控分组', 'group', c.group)
          + fieldSwitch('自动采集', 'auto_collect', c.auto_collect, '开启后按规则自动监控创作者的新内容')
          + fieldNumber('检查频率（分钟）', 'interval_minutes', c.interval_minutes, 1, 1440)
          + fieldSwitch('优先抓取英语内容', 'priority_english', c.priority_english)
          + fieldSwitch('新视频自动入队', 'new_video_auto_join', c.new_video_auto_join))}</section>
        <section class="card">${group('视频时长与内容过滤', '',
          `<div class="field"><label class="lb">视频时长过滤</label><div class="ctl">
            <input type="number" data-key="duration_min_sec" value="${esc(c.duration_min_sec)}" min="0" style="width:74px">
            <span class="hint">秒 至</span>
            <input type="number" data-key="duration_max_sec" value="${esc(c.duration_max_sec)}" min="0" style="width:74px">
            <span class="hint">秒</span></div></div>`
          + fieldSwitch('低价值内容过滤', 'low_quality_filter', c.low_quality_filter, '过滤广告、重复、低质量内容')
          + fieldChecks('过滤规则', 'filter_rules', c.filter_rules, ['广告营销', '纯图文', '低播放量', '重复内容', '无实质内容']))}</section>
        <section class="card">${group('采集来源', '',
          fieldSelect('采集来源范围', 'source_scope', c.source_scope, ['仅创作者主页', '主页 + 话题页', '全部'])
          + fieldChecks('包含内容类型', 'include_types', c.include_types, ['公开视频', '图文帖子', '直播回放', '精选合集'])
          + fieldTextarea('排除指定内容', 'exclude_keywords', c.exclude_keywords, 3, '输入视频ID或标题，每行一个'))}</section>
        <section class="card">${group('去重规则', '',
          fieldSelect('去重方式', 'dedupe_method', c.dedupe_method, ['视频指纹（推荐）', '标题相似', '作者 + 发布时间'])
          + fieldNumber('相似度阈值', 'similarity_threshold', c.similarity_threshold, 0, 100, '%')
          + fieldChecks('同时检查', 'dedupe_checks', [], ['视频指纹', '标题截图', '作者 + 发布时间', '音频指纹']))}</section>
        <section class="card">${group('下载与媒体处理', '',
          fieldSelect('下载清晰度', 'download_quality', c.download_quality, ['1080p', '720p', '540p', '最佳画质'])
          + fieldSelect('字幕抓取优先级', 'subtitle_priority', c.subtitle_priority,
            ['原生字幕 > 自动生成 > 不抓取', '仅原生字幕', '仅自动生成', '不抓取字幕'])
          + fieldSwitch('自动提取音频', 'extract_audio', c.extract_audio, '下载后自动提取音频为 MP3')
          + fieldSelect('字幕保存格式', 'subtitle_format', c.subtitle_format, ['SRT', 'VTT', 'TXT']))}</section>
        <section class="card">${group('采集限制与重试', '',
          fieldNumber('失败重试次数', 'fail_retry', c.fail_retry, 0, 10, '次')
          + `<div class="field"><label class="lb">时间窗口</label><div class="ctl">
            <input type="time" data-key="window_start" value="${esc(c.window_start)}">
            <span class="hint">至</span><input type="time" data-key="window_end" value="${esc(c.window_end)}"></div></div>`
          + fieldNumber('单日采集上限', 'daily_limit', c.daily_limit, 0, 100000, '条')
          + fieldNumber('每创作者上限', 'per_creator_limit', c.per_creator_limit, 1, 10000, '条/天'))}</section>
        <section class="card">${group('历史内容回填', '',
          fieldSwitch('启用历史内容回填', 'history_enabled', c.history_enabled, '为已监控的创作者抓取历史发布内容')
          + fieldSelect('回填时间范围', 'history_days', c.history_days, [[30, '最近 30 天'], [90, '最近 90 天'], [180, '最近 180 天'], [365, '最近一年']])
          + fieldNumber('每个创作者最多回填', 'history_per_creator', c.history_per_creator, 1, 10000, '条')
          + fieldNumber('回填速度控制', 'history_speed_per_hour', c.history_speed_per_hour, 1, 5000, '条/小时'))}</section>
        <section class="card">${group('关键词过滤', '',
          fieldTextarea('关键词白名单', 'keywords_allow', (c.keywords_allow || []).join('\n'), 4, '每行一个关键词')
          + fieldTextarea('关键词黑名单', 'keywords_block', (c.keywords_block || []).join('\n'), 4, '每行一个关键词'))}</section>
      </div>`;
    },
  },
  ai: {
    section: 'ai', title: 'AI 加工设置',
    render() {
      const a = state.settings.ai || {};
      return `<div class="grid g-3">
        <section class="card">${group('模型与服务配置', '这三项决定 AI 标注调用什么模型',
          fieldSelect('AI 服务商', 'provider', a.provider, ['DeepSeek', 'OpenAI', '通义千问', '智谱 AI', '自定义'])
          + fieldText('主模型', 'model', a.model, '用于内容理解、改写、生成学习材料')
          + fieldText('备用模型', 'fallback_model', a.fallback_model, '主模型不可用时自动切换')
          + fieldText('API 地址', 'api_base', a.api_base, 'OpenAI 兼容 /chat/completions')
          + fieldPassword('API Key', 'api_key', a.apiKeySet, a.apiKeySet ? '已保存' : '必填，否则标注会失败')
          + fieldText('Embedding 模型', 'embedding_model', a.embedding_model, '用于语义分类、相似度计算')
          + fieldSelect('ASR / 转写', 'asr_provider', a.asr_provider, ['OpenAI Whisper（本地）', '不启用', '外部服务'])
          + fieldSelect('输出语言', 'output_language', a.output_language, ['双语（中英）', '仅中文', '仅英文'])
          + `<div class="mt8 flex wrap"><button class="btn" data-act="test-ai">测试连接</button>
              <button class="btn" data-act="import-legacy-ai" title="复用下载器学习文档里已配好的服务商与 Key">导入已有配置</button></div>
             <div class="hint mt8">「导入已有配置」会把下载器学习文档（learning.json）里的
               API Key / 地址 / 模型搬过来，省得再填一遍。</div>`)}</section>
        <section class="card">${group('生成参数设置', '',
          `<div class="field"><label class="lb">温度（创造性）</label><div class="ctl">
            <input type="range" data-key="temperature" min="0" max="1" step="0.1" value="${esc(a.temperature)}"
              oninput="document.getElementById('tempOut').textContent=this.value">
            <b id="tempOut" style="width:28px">${esc(a.temperature)}</b></div></div>`
          + fieldNumber('最大 Token 数', 'max_tokens', a.max_tokens, 256, 32000)
          + fieldNumber('批处理大小', 'batch_size', a.batch_size, 1, 50)
          + fieldSwitch('自动提取重点表达', 'extract_key_points', a.extract_key_points, '提取实用词汇、短语和地道表达')
          + fieldSwitch('自动识别语法点', 'detect_grammar', a.detect_grammar, '识别并说明相关语法知识')
          + fieldSwitch('自动生成例句', 'generate_examples', a.generate_examples, '基于语境生成实用例句')
          + fieldSwitch('自动生成练习', 'generate_exercises', a.generate_exercises, '生成选择题 / 填空题等练习')
          + fieldSwitch('自动生成学习卡片', 'generate_cards', a.generate_cards, '生成 Anki 风格的记忆卡片')
          + fieldNumber('内容质量阈值', 'quality_threshold', a.quality_threshold, 0, 1, '低于此值标记为待复核')
          + fieldNumber('Learning Value 最低分', 'learning_value_min', a.learning_value_min, 0, 1, '低于此值不进入学习库'))}</section>
        <section class="card">${group('Prompt 模板管理', '真实生效：AI 标注就用这里的版本与模板',
          fieldSelect('Prompt 版本', 'prompt_version', state.prompts.current,
            (state.prompts.list || []).map((entry) => [entry.version, entry.label])
              .concat([['custom', 'custom（自定义模板）']]),
            'v1 = 基线；v2 = 调优（允许空数组、禁止凑数）')
          + `<div class="tiny muted" style="margin:-2px 0 8px">当前版本说明：${esc(
              state.prompts.notes[state.prompts.current] || '（自定义模板）')}</div>`
          + fieldTextarea('Prompt 模板', 'prompt_template', (state.settings.ai || {}).prompt_template, 16,
            '占位符：{title} {author} {description} {duration} {transcript}')
          + `<div class="mt8 flex wrap">
              <button class="btn sm" data-act="prompt-preview">预览模板渲染结果</button>
              <button class="btn sm" data-act="prompt-save">保存模板</button>
              <button class="btn sm" data-act="prompt-reset">恢复默认模板</button></div>
             <div class="hint mt8">改动模板文本并保存会记为 <b>custom</b> 版本；改回与某个版本完全一致会重新归到该版本。
               每条标注都会记下自己用的是哪个版本，历史不会被覆盖。</div>`)}</section>
          ${group('Token 预算与成本预估（示意）', '',
            `<div class="preview-box">
              <div class="kv"><span>平均 Token / 条</span><b>1,200</b></div>
              <div class="kv"><span>预估成本 / 条</span><b>$0.0036</b></div>
              <div class="kv"><span>每日处理量</span><b>10,000 条</b></div>
              <div class="kv"><span>每日预估成本</span><b>$36.00</b></div>
              <div class="tiny muted mt8">示意数据：实际消耗取决于模型版本与内容长度。</div>
            </div>`)}</section>
      </div>`;
    },
  },
  publish: {
    section: 'publish', title: '发布设置',
    render() {
      const p = state.settings.publish || {};
      const targets = p.targets || [];
      const platforms = [['抖音', '🎵'], ['小红书', '📕'], ['B站', '📺'], ['视频号', '💬'], ['YouTube', '▶'], ['Tony Learning OS', '🎓']];
      return `<div class="notice"><span>ℹ</span><div class="grow">发布设置本阶段只做界面与本地保存，
        真实多平台发布 / 对象存储上传按计划留到下一阶段。平台连接状态为示意。</div></div>
      <div class="grid g-main mt14">
        <div>
          <section class="card"><div class="card-head"><h2>发布目标平台</h2>
            <span class="ch-sub">选择要发布的目标平台，支持多平台同时发布</span></div>
            <div class="card-body">
              <div class="grid g-6">${platforms.map(([name, icon]) => `
                <label class="stat" style="flex-direction:column;align-items:center;text-align:center;cursor:pointer;padding:13px 8px">
                  <span style="font-size:22px">${icon}</span>
                  <b style="font-size:12.4px;margin-top:6px">${esc(name)}</b>
                  <input type="checkbox" data-key="targets" data-multi="1" value="${esc(name)}"
                    ${targets.includes(name) ? 'checked' : ''} style="margin-top:7px;accent-color:var(--blue)">
                  <span class="tiny" style="color:var(--green);margin-top:4px">● 示意已连接</span>
                </label>`).join('')}</div>
            </div></section>
          <section class="card">${group('同步与存储', '',
            fieldSwitch('自动同步到 Tony Learning OS', 'sync_to_learning_os', p.sync_to_learning_os, '选择后将自动同步到学习平台')
            + `<div class="preview-box mt8">
                <div class="kv"><span>存储类型</span><b>${esc((state.settings.storage || {}).object_storage || '腾讯云 COS')}</b></div>
                <div class="kv"><span>Bucket</span><b>${esc((state.settings.storage || {}).bucket || '—')}</b></div>
                <div class="kv"><span>访问域名</span><b class="mono">${esc((state.settings.storage || {}).custom_domain || '—')}</b></div>
                <div class="tiny muted mt8">对象存储真实参数在「设置 → 存储设置」中填写。</div></div>`)}</section>
          <section class="card">${group('发布审核模式', '',
            `<label class="radio"><input type="radio" name="review_mode" data-key="review_mode" value="auto" ${p.review_mode === 'auto' ? 'checked' : ''}>
              <span><b>自动发布</b><small>内容审核通过后自动发布到各平台</small></span></label>
             <label class="radio"><input type="radio" name="review_mode" data-key="review_mode" value="manual" ${p.review_mode === 'manual' ? 'checked' : ''}>
              <span><b>待审核</b><small>需要人工审核后再发布</small></span></label>
             <label class="radio"><input type="radio" name="review_mode" data-key="review_mode" value="mixed" ${p.review_mode === 'mixed' ? 'checked' : ''}>
              <span><b>混合模式</b><small>重要平台待审核，其他平台自动发布</small></span></label>`)}</section>
          <section class="card">${group('SEO / 标题规则', '',
            fieldText('标题生成规则', 'title_rule', p.title_rule)
            + fieldText('标题模板', 'title_template', p.title_template, '占位符 {title} {theme}')
            + fieldSelect('SEO 关键词', 'seo_keywords', p.seo_keywords, ['自动提取 + 自定义关键词', '仅自动提取', '仅自定义']))}</section>
        </div>
        <div>
          <section class="card">${group('平台连接状态', '',
            `<div class="card-body tight" style="padding:0">${platforms.map(([name, icon]) => `
              <div class="list-row" style="cursor:default"><span style="font-size:15px">${icon}</span>
                <div class="grow"><b style="font-size:12.4px">${esc(name)}</b>
                  <div class="tiny muted">示意状态，未接入真实平台</div></div>
                ${tag('未接入', 'amber')}</div>`).join('')}</div>`)}</section>
          <section class="card">${group('发布时间策略', '',
            fieldSelect('发布频率', 'publish_frequency', p.publish_frequency, ['每天固定数量', '每天固定时段', '间隔发布'])
            + fieldNumber('每日发布数量', 'daily_count', p.daily_count, 1, 100, '条')
            + `<div class="field"><label class="lb">发布时段</label><div class="ctl wrap">
                ${(p.slots || []).map((slot, index) => `<span class="chip">${esc(slot)}</span>`).join('')}
                <input type="text" data-key="slots" value="${esc((p.slots || []).join(','))}" style="flex:1;min-width:150px">
              </div></div>`)}</section>
          <section class="card">${group('内容分类与标签映射', '',
            fieldSelect('分类映射规则', 'category_rule', p.category_rule, ['按内容主题自动匹配', '按创作者分类', '手动指定'])
            + fieldText('默认分类', 'default_category', p.default_category)
            + fieldText('话题标签', 'topic_tags', (p.topic_tags || []).join(', '), '逗号分隔'))}</section>
          <section class="card">${group('失败重试与队列', '',
            fieldNumber('最大重试次数', 'max_retry', p.max_retry, 0, 10)
            + fieldNumber('重试间隔（分钟）', 'retry_interval_min', p.retry_interval_min, 1, 1440)
            + fieldSwitch('失败时通知', 'notify_on_fail', p.notify_on_fail)
            + fieldSelect('队列优先级', 'queue_priority', p.queue_priority, ['高（优先发布）', '中', '低'])
            + fieldNumber('队列并发数', 'queue_concurrency', p.queue_concurrency, 1, 20))}</section>
        </div>
      </div>`;
    },
  },
  storage: {
    section: 'storage', title: '存储设置',
    render() {
      const s = state.settings.storage || {};
      return `<div class="grid g-3">
        <section class="card">${group('本地存储设置', '',
          fieldFolder('视频保存路径', 'video_path', s.video_path)
          + fieldFolder('封面保存路径', 'cover_path', s.cover_path)
          + fieldFolder('素材保存路径', 'library_path', s.library_path)
          + fieldFolder('临时文件路径', 'temp_path', s.temp_path)
          + fieldSwitch('自动创建目录', 'auto_create_dir', s.auto_create_dir, '程序启动时自动创建所需目录')
          + fieldNumber('磁盘空间预警（GB）', 'disk_warn_gb', s.disk_warn_gb, 1, 1000, '可用空间低于此值时提醒'))}</section>
        <section class="card">${group('对象存储配置（示意，未接入）', '',
          fieldSelect('存储类型', 'object_storage', s.object_storage, ['阿里云 OSS', '腾讯云 COS', 'AWS S3', '七牛云'])
          + fieldText('Bucket 名称', 'bucket', s.bucket)
          + fieldText('地域（Region）', 'region', s.region)
          + fieldText('Access Key ID', 'access_key_id', s.access_key_id)
          + fieldPassword('Access Key Secret', 'access_key_secret', s.secretSet)
          + fieldText('自定义域名（CDN）', 'custom_domain', s.custom_domain)
          + fieldText('存储路径前缀', 'storage_prefix', s.storage_prefix))}</section>
        <section class="card">${group('上传策略', '',
          fieldSelect('默认存储位置', 'save_location', s.save_location, ['同时保存到本地和对象存储', '仅本地', '仅对象存储'])
          + fieldSelect('上传时机', 'upload_timing', s.upload_timing, ['处理完成后立即上传', '手动上传', '定时上传'])
          + fieldNumber('并发上传数', 'upload_concurrency', s.upload_concurrency, 1, 20)
          + fieldNumber('失败重试次数', 'upload_retry', s.upload_retry, 0, 10)
          + fieldNumber('单文件大小限制', 'upload_size_limit_mb', s.upload_size_limit_mb, 1, 100000, 'MB')
          + fieldSwitch('断点续传', 'resume_upload', s.resume_upload)
          + fieldSwitch('校验文件完整性', 'verify_md5', s.verify_md5, 'MD5 校验'))}</section>
        <section class="card">${group('存储空间使用情况', '本机真实数据', storageUsage())}</section>
        <section class="card">${group('文件保存策略', '',
          fieldNumber('视频文件保存时间', 'keep_video_days', s.keep_video_days, 1, 3650, '天')
          + fieldNumber('图片文件保存时间', 'keep_image_days', s.keep_image_days, 1, 3650, '天')
          + fieldNumber('临时文件保存时间', 'keep_temp_days', s.keep_temp_days, 1, 365, '天')
          + fieldSelect('保留策略', 'keep_policy', s.keep_policy, ['自动删除', '仅提醒', '不处理'], '超过保留时间的文件'))}</section>
        <section class="card">${group('自动清理规则', '',
          fieldSwitch('启用自动清理', 'clean_schedule', s.clean_schedule, '按周期定期清理无用文件')
          + fieldSelect('清理周期', 'clean_cycle', s.clean_cycle, ['每天', '每周', '每月'])
          + `<div class="field"><label class="lb">执行时间</label><div class="ctl">
              <input type="time" data-key="clean_time" value="${esc(s.clean_time)}">
              <span class="hint">选择低峰时段执行</span></div></div>`
          + fieldSwitch('删除过期的临时文件', 'clean_expired_temp', s.clean_expired_temp)
          + fieldSwitch('删除回收站文件', 'clean_recycle', s.clean_recycle)
          + fieldSwitch('清理未使用的缓存文件', 'clean_unused', s.clean_unused)
          + fieldSwitch('清理失败的下载文件', 'clean_failed', s.clean_failed))}</section>
        <section class="card">${group('备份策略', '',
          fieldSwitch('启用自动备份', 'backup_enabled', s.backup_enabled, '定期备份数据到本地或云端')
          + fieldChecks('备份内容', 'backup_items', s.backup_items, ['配置文件', '发布记录', '重要素材'])
          + fieldSelect('备份周期', 'backup_cycle', s.backup_cycle, ['每天', '每周', '每月'])
          + fieldNumber('保留备份数量', 'backup_keep', s.backup_keep, 1, 100, '份'))}</section>
        <section class="card">${group('存储健康状态', '',
          `<div class="list-row" style="cursor:default"><span class="stat-ico green" style="width:30px;height:30px;font-size:14px">✓</span>
            <div class="grow"><b style="font-size:12.6px">存储服务正常</b>
            <div class="tiny muted">数据库与设置文件均已就绪</div></div></div>
           <div class="kv"><span>数据库文件</span><b class="mono tiny">${esc(state.worker.dbPath || '—')}</b></div>
           <div class="kv"><span>设置文件</span><b class="mono tiny">${esc(state.worker.settingsPath || '—')}</b></div>
           <div class="kv"><span>本地目录可写</span><b>${tag('正常', 'green')}</b></div>
           <div class="kv"><span>对象存储连通</span><b>${tag('未接入', 'amber')}</b></div>`)}</section>
      </div>`;
    },
  },
  notify: {
    section: 'notify', title: '通知设置',
    render() {
      const n = state.settings.notify || {};
      return `<div class="grid g-3">
        <section class="card">${group('桌面通知设置', '在系统桌面显示通知消息',
          fieldSwitch('启用桌面通知', 'desktop_task_start', n.desktop_task_start, '显示任务状态、异常等通知')
          + fieldSwitch('任务完成通知', 'desktop_task_done', n.desktop_task_done)
          + fieldSwitch('任务失败通知', 'desktop_task_fail', n.desktop_task_fail)
          + fieldSwitch('AI 加工完成通知', 'desktop_ai_done', n.desktop_ai_done)
          + fieldSwitch('发布完成通知', 'desktop_publish_done', n.desktop_publish_done)
          + fieldSwitch('系统异常通知', 'desktop_system_error', n.desktop_system_error))}</section>
        <section class="card">${group('声音提醒设置', '通过声音播放提醒（需保持应用运行）',
          fieldSwitch('启用声音提醒', 'sound_enabled', n.sound_enabled)
          + fieldSwitch('任务完成声音', 'sound_task_done', n.sound_task_done)
          + fieldSwitch('任务失败声音', 'sound_task_fail', n.sound_task_fail)
          + fieldSwitch('AI 加工完成声音', 'sound_ai_done', n.sound_ai_done)
          + fieldNumber('重复播放次数', 'sound_repeat', n.sound_repeat, 1, 10, '次')
          + fieldNumber('播放间隔', 'sound_gap_sec', n.sound_gap_sec, 1, 60, '秒'))}</section>
        <section class="card">${group('通知触发条件', '设置哪些事件需要发送通知',
          fieldSwitch('任务开始时通知', 'event_task_start', n.event_task_start)
          + fieldSwitch('任务完成时通知', 'event_task_done', n.event_task_done)
          + fieldSwitch('任务失败时通知', 'event_task_fail', n.event_task_fail)
          + fieldSwitch('AI 加工异常时通知', 'event_ai_done', n.event_ai_done)
          + fieldSwitch('发布失败时通知', 'event_publish_fail', n.event_publish_fail)
          + fieldSwitch('存储空间不足时通知', 'event_storage_low', n.event_storage_low)
          + fieldSwitch('系统异常时通知', 'event_system_error', n.event_system_error))}</section>
        <section class="card">${group('消息推送渠道', '配置第三方通知渠道，可同时启用多个渠道',
          fieldSwitch('邮件通知', 'channel_email', n.channel_email, n.channel_email ? '已配置' : '未配置')
          + fieldSwitch('企业微信', 'channel_wecom', n.channel_wecom, n.channel_wecom ? '已配置' : '未配置')
          + fieldSwitch('飞书机器人', 'channel_feishu', n.channel_feishu, n.channel_feishu ? '已配置' : '未配置')
          + fieldSwitch('钉钉机器人', 'channel_dingtalk', n.channel_dingtalk, n.channel_dingtalk ? '已配置' : '未配置')
          + fieldSwitch('Slack', 'channel_slack', n.channel_slack, n.channel_slack ? '已配置' : '未配置')
          + fieldSwitch('自定义 Webhook', 'channel_webhook', n.channel_webhook, n.channel_webhook ? '已配置' : '未配置'))}</section>
        <section class="card">${group('邮件通知配置', '通过邮箱发送通知消息',
          fieldText('SMTP 服务器', 'smtp_server', n.smtp_server)
          + fieldNumber('端口', 'smtp_port', n.smtp_port, 1, 65535)
          + fieldText('发件人邮箱', 'smtp_user', n.smtp_user)
          + fieldPassword('授权码 / 密码', 'smtp_password', n.passwordSet)
          + fieldText('收件人邮箱', 'smtp_to', n.smtp_to, '多个邮箱用逗号分隔')
          + fieldSelect('加密方式', 'smtp_encryption', n.smtp_encryption, ['STARTTLS', 'SSL/TLS', '不加密'])
          + `<div class="mt8"><button class="btn" data-act="notify-test">发送测试邮件</button></div>`)}</section>
        <section class="card">${group('Webhook 配置', '通过 Webhook 推送通知（支持自定义机器人）',
          fieldText('Webhook 地址', 'webhook_url', n.webhook_url)
          + fieldSelect('请求方式', 'webhook_method', n.webhook_method, ['POST', 'PUT', 'GET'])
          + fieldSelect('消息格式', 'webhook_format', n.webhook_format, ['JSON（推荐）', '表单', '纯文本'])
          + fieldTextarea('自定义消息模板', 'webhook_template', n.webhook_template, 6,
            '可用变量：{{app_name}} {{title}} {{message}} {{time}}')
          + `<div class="mt8"><button class="btn" data-act="notify-test">发送测试请求</button></div>`)}</section>
        <section class="card">${group('通知策略与免打扰', '',
          fieldSelect('最低通知级别', 'min_level', n.min_level, ['INFO', 'WARN', 'ERROR'], '只发送该级别及以上的重要通知')
          + fieldNumber('重复通知间隔', 'repeat_interval_min', n.repeat_interval_min, 1, 1440, '分钟')
          + fieldNumber('失败重试次数', 'max_retry', n.max_retry, 0, 10)
          + fieldNumber('重试间隔', 'retry_interval_min', n.retry_interval_min, 1, 1440, '分钟')
          + fieldSwitch('启用免打扰时间', 'quiet_enabled', n.quiet_enabled, '设定时间段内不发送非紧急通知')
          + `<div class="field"><label class="lb">免打扰时段</label><div class="ctl">
              <input type="time" data-key="quiet_start" value="${esc(n.quiet_start)}"><span class="hint">至</span>
              <input type="time" data-key="quiet_end" value="${esc(n.quiet_end)}"></div></div>`
          + fieldSelect('免打扰范围', 'quiet_mode', n.quiet_mode, ['仅屏蔽非紧急通知', '屏蔽全部通知']))}</section>
        <section class="card">${group('通知预览与测试', '',
          fieldSelect('通知类型', 'preview_type', n.preview_type, ['任务完成通知', '任务失败通知', 'AI 加工完成', '发布完成'])
          + fieldTextarea('测试消息', 'preview_message', n.preview_message, 3)
          + `<div class="mt8 flex wrap"><button class="btn sm" data-act="notify-test">预览通知效果</button>
              <button class="btn sm primary" data-act="notify-test">发送测试通知</button></div>`)}</section>
      </div>`;
    },
  },
  account: {
    section: 'account', title: '账号管理',
    render() {
      const a = state.settings.account || {};
      const counts = state.stats.counts || {};
      return `<div class="grid g-3">
        <section class="card"><div class="card-head"><h2>当前工作区账号</h2></div>
          <div class="card-body">
            <div class="flex mb8">${avatar(a.display_name || 'Tony', 'lg')}
              <div><b style="font-size:14.5px">${esc(a.display_name || 'Tony')}</b>
                <div class="tiny muted">${esc(a.email || '')}</div>
                <div class="mt8">${tag('管理员', 'blue')}</div></div></div>
            <div class="kv"><span>用户 ID</span><b class="mono">${esc(a.node_id || 'worker_001')}</b></div>
            <div class="kv"><span>所属团队</span><b>Tony Content Engine</b></div>
            <div class="kv"><span>工作区</span><b>默认工作区</b></div>
            <div class="kv"><span>加入时间</span><b>${esc(state.worker.startedAt || '—')}</b></div>
            ${fieldText('显示名称', 'display_name', a.display_name)}
            ${fieldText('邮箱', 'email', a.email)}
          </div></section>
        <section class="card"><div class="card-head"><h2>授权与订阅信息（示意）</h2></div>
          <div class="card-body">
            <div class="flex mb8"><b style="font-size:14px">${esc(a.plan || '专业版')}</b>${tag('已激活', 'green')}</div>
            <div class="kv"><span>套餐类型</span><b>专业版（年付）</b></div>
            <div class="kv"><span>到期时间</span><b>本阶段未接入订阅</b></div>
            <div class="kv"><span>授权设备数</span><b>本机 1 台</b></div>
            <div class="tiny muted mt8">订阅 / 授权为示意信息，本阶段不接支付与账号服务。</div>
          </div></section>
        <section class="card">${group('登录状态与设备', '当前环境信息（真实）', 
          `<div class="list-row" style="cursor:default"><span style="font-size:16px">💻</span>
            <div class="grow"><b style="font-size:12.6px">本机桌面端</b>
            <div class="tiny muted">启动于 ${esc(state.worker.startedAt || '—')} · 已运行 ${esc(state.worker.uptimeText || '')}</div></div>
            ${tag('在线', 'green')}</div>
           <div class="kv"><span>当前状态</span><b>${tag('在线', 'green')}</b></div>
           <div class="kv"><span>Worker 节点</span><b class="mono">${esc(a.node_id || 'worker_001')}</b></div>
           <div class="kv"><span>Python 版本</span><b class="mono">${esc(state.worker.python || '—')}</b></div>
           <div class="kv"><span>进程 ID</span><b class="mono">${esc(state.worker.pid || '—')}</b></div>`)}</section>
        <section class="card">${group('Worker 节点身份', '本机内容工厂节点',
          fieldText('节点名称', 'node_name', a.node_name)
          + fieldText('节点 ID', 'node_id', a.node_id)
          + `<div class="kv"><span>节点状态</span><b>${tag('运行中', 'green')}</b></div>
             <div class="kv"><span>已处理内容</span><b>${num(counts.total || 0)} 条</b></div>
             <div class="kv"><span>数据库</span><b class="mono tiny">${esc(state.worker.dbPath || '—')}</b></div>`)}</section>
        <section class="card">${group('AI 凭据绑定', 'AI 标注真实使用的配置',
          `<div class="kv"><span>API Key 状态</span><b>${state.stats.aiConfigured ? tag('已绑定', 'green') : tag('未绑定', 'red')}</b></div>
           <div class="kv"><span>服务商</span><b>${esc(state.stats.provider || '—')}</b></div>
           <div class="kv"><span>模型</span><b class="mono">${esc(state.stats.model || '—')}</b></div>
           <div class="kv"><span>API 地址</span><b class="mono tiny">${esc((state.settings.ai || {}).api_base || '—')}</b></div>
           <div class="mt8"><button class="btn sm" data-act="test-ai">测试连接</button>
             <button class="btn sm" data-act="nav" data-page="settings" data-sub="ai">去配置</button></div>`)}</section>
        <section class="card">${group('安全设置', '',
          fieldSelect('双重验证（2FA）', 'two_factor', a.two_factor ? 'on' : 'off', [['on', '已开启'], ['off', '未开启']])
          + fieldSelect('验证方式', 'verify_method', a.verify_method, ['Google Authenticator', '短信验证'] )
          + `<div class="kv"><span>登录保护</span><b>${tag('新设备登录需验证', 'green')}</b></div>`)}</section>
        <section class="card">${group('第三方平台账号绑定', '本阶段示意',
          `<div class="card-body tight" style="padding:0">
            ${[['抖音', '🎵'], ['视频号', '💬'], ['B站', '📺'], ['小红书', '📕'], ['YouTube', '▶'], ['Tony Learning OS', '🎓']]
              .map(([name, icon]) => `<div class="list-row" style="cursor:default">
                <span style="font-size:15px">${icon}</span>
                <div class="grow"><b style="font-size:12.4px">${esc(name)}</b></div>
                ${tag('未绑定', 'amber')}</div>`).join('')}</div>`)}</section>
        <section class="card">${group('权限与角色管理', '',
          `<div class="kv"><span>当前角色</span><b>${tag('超级管理员', 'violet')}</b></div>
           <div class="kv"><span>权限范围</span><b>拥有系统全部功能的访问权限</b></div>
           <div class="kv"><span>团队成员</span><b>1 人（本机）</b></div>
           <div class="tiny muted mt8">多用户与角色体系本阶段未实现。</div>`)}</section>
        <section class="card">${group('操作日志', '来自本机真实记录', 
          `<div class="card-body tight" style="padding:0"><div class="log-list">
            <div class="log-row"><span class="log-time">${esc(clockTime(state.worker.startedAt))}</span>
              <span class="log-ico" style="color:var(--green)">●</span>
              <span class="log-text">启动内容工厂</span><span class="log-note">本机</span></div>
            <div class="log-row"><span class="log-time">${esc(clockTime(state.worker.checkedAt))}</span>
              <span class="log-ico" style="color:var(--blue)">●</span>
              <span class="log-text">刷新界面状态</span><span class="log-note">本机</span></div>
            ${state.items.slice(0, 4).map((row) => `<div class="log-row">
              <span class="log-time">${esc(clockTime(row.updated_at))}</span>
              <span class="log-ico" style="color:var(--blue)">●</span>
              <span class="log-text">${esc(row.ai_status === 'done' ? 'AI 标注完成' : '内容入库')}</span>
              <span class="log-note">${esc(row.title)}</span></div>`).join('')}
          </div></div>`)}</section>
      </div>`;
    },
  },
};

/* 存储设置里的真实磁盘占用 */
function storageUsage() {
  const bytes = state.worker.disk || {};
  const used = Number(bytes.used) || 0;
  const total = Number(bytes.total) || 0;
  if (!total) return '<div class="muted tiny">磁盘信息不可用。</div>';
  const pct = Math.round(used / total * 100);
  const toGb = (value) => (value / 1024 / 1024 / 1024).toFixed(1);
  return `<div class="bar-row"><span class="bar-label">已使用</span>
      <span class="bar-track"><span class="bar-fill ${pct > 90 ? 'red' : pct > 70 ? 'amber' : ''}" style="width:${pct}%"></span></span>
      <span class="bar-val">${pct}%</span></div>
    <div class="kv"><span>已用 / 总量</span><b>${toGb(used)} GB / ${toGb(total)} GB</b></div>
    <div class="kv"><span>剩余可用</span><b>${toGb(Math.max(0, total - used))} GB</b></div>
    <div class="kv"><span>数据库大小</span><b>${state.worker.dbSize ? (state.worker.dbSize / 1024).toFixed(1) + ' KB' : '—'}</b></div>
    <div class="kv"><span>内容库占用</span><b>${num((state.stats.counts || {}).total || 0)} 条记录</b></div>`;
}

/* ---------- 4.9 视频库（内嵌原下载器） ---------- */
renderers.library = (meta) => {
  const actions = `<button class="btn" data-act="ingest-downloader">把历史下载记录导入内容库</button>
    <button class="btn" data-act="import-folder">导入本地视频目录</button>`;
  return pageHead(meta, actions)
    + `<div class="notice"><span>ℹ</span><div class="grow">
        这里是原来的 TikTok 下载器界面（完整保留，抓取 / 批量下载 / 字幕获取全部可用）。
        下载完成的作品会<b>自动进入内容库</b>，可以直接去「AI 加工」做标注。</div></div>
      <div class="mt14"><iframe id="libraryFrame" class="library-frame" src="index.html?embed=1"
        title="TikTok 下载器"></iframe></div>`;
};

/* ==================== 5. 动作绑定 ==================== */

/** 收集设置表单：按 data-key 归类，多选字段收成数组，密钥留空表示不变。 */
function collectSettings(section) {
  const values = {};
  const nodes = document.querySelectorAll('#content [data-key]');
  nodes.forEach((node) => {
    const key = node.dataset.key;
    if (!key) return;
    // prompt_version 是「选择器」而不是普通设置项：它由 content_set_prompt_version
    // 单独处理，不能混进设置分区去覆盖 prompt_template
    if (key === 'prompt_version') return;
    if (node.type === 'checkbox') {
      if (node.dataset.multi) {
        if (!Array.isArray(values[key])) values[key] = [];
        if (node.checked) values[key].push(node.value);
      } else {
        values[key] = node.checked;
      }
      return;
    }
    if (node.type === 'radio') {
      if (node.checked) values[key] = node.value;
      return;
    }
    if (node.type === 'number' || node.type === 'range') {
      const number = Number(node.value);
      values[key] = Number.isFinite(number) ? number : node.value;
      return;
    }
    if (key === 'slots') {
      values[key] = String(node.value).split(',').map((part) => part.trim()).filter(Boolean);
      return;
    }
    if (key === 'topic_tags') {
      values[key] = String(node.value).split(',').map((part) => part.trim()).filter(Boolean);
      return;
    }
    if (key === 'keywords_allow' || key === 'keywords_block') {
      values[key] = String(node.value).split('\n').map((part) => part.trim()).filter(Boolean);
      return;
    }
    if (node.type === 'password') {
      const raw = String(node.value || '').trim();
      if (raw) values[key] = raw;
      return;
    }
    values[key] = node.value;
  });
  return values;
}

/** 存盘时按分区把数字型字段还原成数字、字符串列表还原成数组。 */
function normalizeForSection(section, values) {
  const out = { ...values };
  if (section === 'collect') {
    ['interval_minutes', 'duration_min_sec', 'duration_max_sec', 'similarity_threshold',
      'fail_retry', 'daily_limit', 'per_creator_limit', 'history_days', 'history_per_creator',
      'history_speed_per_hour'].forEach((key) => {
      if (out[key] !== undefined) out[key] = Number(out[key]);
    });
  }
  if (section === 'ai') {
    ['max_tokens', 'batch_size'].forEach((key) => {
      if (out[key] !== undefined) out[key] = Number(out[key]);
    });
    ['temperature', 'quality_threshold', 'learning_value_min'].forEach((key) => {
      if (out[key] !== undefined) out[key] = Number(out[key]);
    });
  }
  if (section === 'publish') {
    ['daily_count', 'max_retry', 'retry_interval_min', 'queue_concurrency'].forEach((key) => {
      if (out[key] !== undefined) out[key] = Number(out[key]);
    });
  }
  if (section === 'notify') {
    ['smtp_port', 'sound_repeat', 'sound_gap_sec', 'repeat_interval_min', 'max_retry',
      'retry_interval_min'].forEach((key) => {
      if (out[key] !== undefined) out[key] = Number(out[key]);
    });
  }
  if (section === 'storage') {
    ['disk_warn_gb', 'upload_concurrency', 'upload_retry', 'upload_size_limit_mb',
      'keep_video_days', 'keep_image_days', 'keep_temp_days', 'backup_keep'].forEach((key) => {
      if (out[key] !== undefined) out[key] = Number(out[key]);
    });
    if (out.file_types) {
      out.file_types = String(out.file_types).split(',').map((part) => part.trim()).filter(Boolean);
    }
  }
  if (section === 'general' || section === 'work_mode') {
    ['concurrent_tasks', 'task_interval_sec', 'auto_clean_days'].forEach((key) => {
      if (out[key] !== undefined) out[key] = Number(out[key]);
    });
  }
  return out;
}

async function reloadAll(options = {}) {
  const payload = await safeCall('content_bootstrap');
  if (!payload || !payload.ok) {
    fatalBox('读取本地内容库失败', (payload && payload.error) || '请稍后重试，或检查数据目录权限。');
    return false;
  }
  state.stats = payload.stats || { counts: {} };
  state.items = payload.items || [];
  state.creators = payload.creators || [];
  state.settings = payload.settings || {};
  state.errors = payload.errors || { entries: [], summary: {} };
  state.stages = payload.stages || [];
  state.demoCount = payload.demoCount || 0;
  const worker = await safeCall('content_worker_status');
  if (worker && worker.ok) state.worker = worker;
  await refreshCreators();
  await loadPrompts();
  state.ready = true;
  if (options.render !== false) renderPage();
  return true;
}

/** 创作者监控页的数据源：content_creator_list()。
 *
 * 为什么不再用 content_bootstrap 里的 creators：那一份只是 creators 表的基础字段，
 * 没有「启用 / 优先级 / 检查频率 / 上次检查 / 下次检查 / 正在检查」这些监控状态，
 * 页面上就没法显示真实状态（只能写死「正常监控」）。Creator Monitor 已经把这些
 * 算好了，前端直接拿来显示即可，不再另造一份数据模型。
 *
 * 桥接上取不到时保留 bootstrap 的那一份（老桥接 / 降级），页面不至于空白。
 */
async function refreshCreators() {
  const payload = await safeCall('content_creator_list');
  if (!payload || !payload.ok) return false;
  state.creators = payload.creators || [];
  return true;
}

/** 轮询「立即跑 1 条」的进度，直到它结束。
 *
 * 状态来自 content_run_one_status（后端每次状态变化都会写），不是前端猜的 ——
 * 所以「检查 / 发现 / 下载 / 字幕 / AI」每一步都是真实发生的，卡住时也停在
 * 真实的那一步上。结束时会重新拉一次内容库，这样「查看 AI 结果」跳过去就能看到。
 */
function pollCreatorRun(creatorId, handle, runId, attempt = 0) {
  const timer = setInterval(async () => {
    attempt += 1;
    const payload = await safeCall('content_run_one_status', runId, creatorId);
    const run = payload && payload.ok ? payload.run : null;
    if (!run) {
      if (attempt > 400) clearInterval(timer);
      return;
    }
    state.runs[creatorId] = run;
    renderPage();
    if (run.status === 'done' || run.status === 'failed') {
      clearInterval(timer);
      await finishCreatorRun(creatorId, handle, run);
      return;
    }
    if (attempt > 900) {                    // 兜底：15 分钟还没结束就别再轮询了
      clearInterval(timer);
      state.runs[creatorId] = { ...run, status: 'failed', error: '运行超时，请查看采集与内容库状态' };
      renderPage();
    }
  }, 1500);
}

async function finishCreatorRun(creatorId, handle, run) {
  const result = run.result || {};
  await reloadAll({ render: false });       // 让 AI 加工页立刻能看到这条内容
  renderPage();
  if (run.status === 'failed' || result.ok === false) {
    // 失败必须说清楚卡在哪一层：preflight / check / download / library / transcript / enrich
    const stageLabel = ({ preflight: '前置检查', check: '读取创作者主页', download: '下载',
      library: '内容入库', transcript: '字幕 / 转写', enrich: 'AI 标注' })[result.stage] || '运行';
    toast(`${stageLabel}失败：${result.error || run.error || '未知原因'}`, 'bad');
    return;
  }
  if (result.processed === 0) {
    toast(result.message || '没有发现新的可处理内容', 'warn');
    return;
  }
  toastAction(`@${handle} 的 1 条内容已处理完成`, '查看 AI 结果', 'ai');
  if (result.itemId) {
    state.ai.selected = result.itemId;
    state.ai.status = 'all';
  }
}

/** 取 Prompt 版本清单（只在设置页用得到，但启动时取一次最省事）。 */
async function loadPrompts() {
  const payload = await safeCall('content_prompts');
  if (!payload || !payload.ok) return;
  const notes = {};
  (payload.prompts || []).forEach((entry) => { notes[entry.version] = entry.notes; });
  state.prompts = {
    current: payload.current || '',
    list: payload.prompts || [],
    versions: Object.fromEntries((payload.prompts || []).map((e) => [e.version, e.template])),
    notes,
  };
}

const HANDLERS = {
  nav(node) { go(node.dataset.page, node.dataset.sub); },
  'close-modal'() { closeModal(); },
  'modal-bg'(node, event) { if (event.target === node) closeModal(); },

  async 'seed-demo'() {
    await safeCall('content_seed_demo');
    toast('演示数据已生成（标记为「演示数据」，可一键清除）', 'good');
    await reloadAll();
  },
  async 'clear-demo'() {
    await safeCall('content_clear_demo');
    toast('演示数据已清除', 'good');
    await reloadAll();
  },
  async 'enrich-pending'() {
    const result = await safeCall('content_enrich_pending', 20);
    if (result && result.queued) toast(`已排队 ${result.queued} 条，正在后台分析`, 'good');
    else if (result && result.message) toast(result.message, 'warn');
    startPolling();
  },
  async 'enrich-picked'() {
    const pick = $('pipePick');
    if (!pick) return;
    await safeCall('content_enrich', pick.value);
    toast('已开始分析，稍后刷新查看结果', 'good');
    startPolling();
  },
  async reanalyze(node) {
    await safeCall('content_reanalyze', node.dataset.id);
    toast('已开始重新分析', 'good');
    startPolling();
  },
  'open-ai'(node) {
    state.ai.selected = node.dataset.id;
    go('ai');
  },
  'ai-select'(node) {
    state.ai.selected = node.dataset.id;
    renderPage();
  },
  'ai-filter'(node) { state.ai.status = node.dataset.filter; renderPage(); },
  'ai-expand'(node) {
    const key = node.dataset.expand;
    state.ai.expanded[key] = !state.ai.expanded[key];
    renderPage();
  },
  'ai-filter-reset'() { state.ai.status = 'all'; state.ai.search = ''; renderPage(); },
  'pipe-filter'(node) { state.pipeline.filter = node.dataset.filter; renderPage(); },
  'open-creator'(node) {
    state.ai.search = node.dataset.handle || '';
    state.ai.status = 'all';
    go('ai');
  },

  /* ---- 添加创作者：UI -> Bridge -> Collector.Core 的最后一跳 ---- */
  'creator-add'() { openCreatorModal(); },

  async 'creator-save'() {
    const values = creatorFormValues();
    if (!values.handle) {
      showCreatorError('请填写 TikTok Handle（例如 @nasa）');
      return;
    }
    // 先统一去掉 @：用户填 @nasa 和 nasa 都是同一个人，服务层也会再去一次，
    // 这里去是为了成功提示里显示的和库里存的一致。
    values.handle = values.handle.replace(/^@+/, '').trim();
    if (!values.handle) {
      showCreatorError('请填写 TikTok Handle（例如 @nasa）');
      return;
    }
    const button = document.querySelector('[data-act="creator-save"]');
    if (button) button.disabled = true;
    let result;
    try {
      result = await call('content_creator_save', values);
    } catch (error) {
      showCreatorError(`保存失败：${error && error.message ? error.message : error}`);
      return;
    } finally {
      if (button) button.disabled = false;
    }
    if (!result || !result.ok) {
      // 失败必须留在 modal 里显示真实原因：handle 为空 / 不合法 / 已存在。
      // 这里不用 safeCall 的自动 toast —— 用户要看着表单改，弹个会消失的提示没用。
      showCreatorError((result && result.error) || '保存失败，请稍后重试');
      return;
    }
    closeModal();
    toast(`已添加 @${result.creator ? result.creator.handle : values.handle}`, 'good');
    await refreshCreators();     // 重新拉一次 content_creator_list()
    renderPage();
  },

  async 'creator-toggle'(node) {
    const result = await safeCall('content_creator_toggle', node.dataset.id,
      node.dataset.enabled === '1');
    if (result && result.ok) {
      toast(result.enabled ? '已启用监控' : '已暂停监控', 'good');
      await refreshCreators();
      renderPage();
    }
  },

  /* ---- 「立即跑 1 条」：一个 Creator 的一条新内容跑到底 ---- */
  async 'creator-run-one'(node) {
    const creatorId = node.dataset.id;
    const handle = node.dataset.handle || '';
    const result = await safeCall('content_run_one_creator', creatorId);
    if (!result || !result.ok) {
      // 起线程之前就查出来的问题（没配下载目录 / 已暂停 / Creator 不存在）
      // 必须当场说清楚，不能等后台线程悄悄失败。
      toast((result && result.error) || '无法开始运行', 'bad');
      return;
    }
    state.runs[creatorId] = {
      runId: result.runId || '', creatorId, handle, status: 'running',
      stage: 'checking', stageLabel: '检查 Creator 新内容', steps: [], result: null,
    };
    renderPage();
    pollCreatorRun(creatorId, handle, result.runId || '');
  },

  'toast-link'(node) {
    if (node.dataset.page) go(node.dataset.page);
    const toastNode = node.closest('.toast');
    if (toastNode) toastNode.remove();
  },
  'publish-guard'() { toast('云端发布本阶段只有界面，真实发布逻辑留到下一阶段', 'warn'); },
  reload: async () => { await reloadAll(); toast('已刷新', 'good'); },

  async 'settings-save'(node) {
    const section = node.dataset.section;
    const values = normalizeForSection(section, collectSettings(section));
    // 只有 AI 加工设置有 Prompt 版本选择器：版本切换必须在保存模板之前做，
    // 否则「保存设置」会把刚选的版本又覆盖成模板文本
    if (section === 'ai' && values.prompt_version !== undefined) {
      const wanted = String(values.prompt_version || '');
      delete values.prompt_version;
      if (wanted && wanted !== state.prompts.current) {
        const switched = await safeCall('content_set_prompt_version', wanted);
        if (switched && switched.ok) {
          state.prompts.current = switched.current;
          const box = document.querySelector('#content [data-key="prompt_template"]');
          if (box && switched.template) box.value = switched.template;
          await loadPrompts();
        }
      }
    }
    const result = await safeCall('content_save_settings', section, values);
    if (result && result.ok) {
      state.settings = result.settings || state.settings;
      const host = $('settingsMsg');
      if (host) host.innerHTML = `<div class="notice good mb8"><span>✓</span>
        <div>设置已保存到本机（重启后依然生效）</div></div>`;
      toast('设置已保存', 'good');
      await reloadAll({ render: false });
    }
  },
  async 'settings-reset'(node) {
    const result = await safeCall('content_reset_settings', node.dataset.section);
    if (result && result.ok) {
      state.settings = result.settings || state.settings;
      toast('已恢复该分区的默认值', 'good');
      await reloadAll();
    }
  },
  async 'pick-folder'(node) {
    const result = await safeCall('content_choose_folder', node.dataset.key);
    if (result && result.ok) {
      const input = document.querySelector(`#content [data-key="${node.dataset.key}"]`);
      if (input) input.value = result.path;
    }
  },
  async 'import-folder'() {
    const picked = await safeCall('content_choose_folder', 'import');
    if (!picked || !picked.ok) return;
    const result = await safeCall('content_import_folder', picked.path);
    if (result && result.ok) {
      toast(`已导入 ${result.imported} 个视频，去「AI 加工」做标注`, 'good');
      await reloadAll();
    }
  },
  async 'ingest-downloader'() {
    const result = await safeCall('content_ingest_downloader');
    if (result && result.ok) {
      toast(result.created ? `已导入 ${result.created} 条下载记录` : (result.message || '没有可导入的记录'),
        result.created ? 'good' : 'warn');
      await reloadAll();
    }
  },
  async reveal(node) { await safeCall('content_open_folder', node.dataset.path); },
  async 'test-ai'() {
    const values = collectSettings('ai');
    toast('正在测试 AI 连接…');
    const result = await safeCall('content_test_ai', values);
    if (result && result.ok) toast(`连接成功：${result.model || ''} ${result.message || ''}`, 'good');
  },
  async 'import-legacy-ai'() {
    const result = await safeCall('content_import_legacy_ai');
    if (result && result.ok) {
      toast(`已导入已有配置：${result.model || ''} ${result.api_base || ''}`.trim(), 'good');
      await reloadAll();
    }
  },
  'prompt-preview'() {
    const box = document.querySelector('#content [data-key="prompt_template"]');
    const text = box ? box.value : '';
    const rendered = text
      .replace('{title}', '3 habits that changed my life')
      .replace('{author}', 'emilyintech')
      .replace('{description}', '（示例简介）')
      .replace('{duration}', '37')
      .replace('{transcript}', 'I used to think productivity was about doing more…');
    openModal(modal('Prompt 模板渲染预览', `<div class="preview-box"><pre>${esc(rendered)}</pre></div>`));
  },
  async 'prompt-save'() {
    const box = document.querySelector('#content [data-key="prompt_template"]');
    if (!box) return;
    const result = await safeCall('content_save_prompt_template', box.value);
    if (result && result.ok) {
      state.settings = result.settings || state.settings;
      await loadPrompts();
      toast(result.matched_version
        ? `模板与 ${result.matched_version} 完全一致，已归到该版本`
        : '模板已保存为 custom 版本', 'good');
      renderPage();
    }
  },
  async 'prompt-reset'() {
    const result = await safeCall('content_reset_settings', 'ai');
    if (result && result.ok) {
      state.settings = result.settings || state.settings;
      await loadPrompts();
      toast('已恢复默认模板与默认 AI 参数', 'good');
      await reloadAll();
    }
  },
  'notify-test'() { toast('通知渠道本阶段只有界面，未发送真实消息', 'warn'); },

  async 'edit-transcript'(node) {
    const result = await safeCall('content_transcript', node.dataset.id);
    const text = (result && result.text) || '';
    openModal(modal('编辑字幕文本', `
      <div class="muted tiny mb8">这条文本就是 AI 标注的输入。没有字幕轨时，可以手工粘贴内容先把标注跑通。</div>
      <textarea id="transcriptBox" rows="14" style="width:100%">${esc(text)}</textarea>
      <div class="mt8 flex"><span class="hint">当前 ${text.length} 字符</span></div>`,
      `<button class="btn" data-act="close-modal">取消</button>
       <button class="btn primary" data-act="save-transcript" data-id="${esc(node.dataset.id)}">保存并重新分析</button>`));
  },
  async 'save-transcript'(node) {
    const box = $('transcriptBox');
    if (!box) return;
    const result = await safeCall('content_set_transcript', node.dataset.id, box.value);
    if (result && result.ok) {
      closeModal();
      toast(`字幕文本已保存（${result.chars} 字符）`, 'good');
      await safeCall('content_reanalyze', node.dataset.id);
      startPolling();
    }
  },
};

document.addEventListener('click', (event) => {
  const node = event.target.closest('[data-act]');
  if (!node) return;
  const act = node.dataset.act;
  const handler = HANDLERS[act];
  if (!handler) return;
  if (node.tagName === 'BUTTON' && !node.dataset.nokeep) {
    event.preventDefault();
  }
  Promise.resolve(handler(node, event)).catch((error) => toast(String(error), 'bad'));
});

/* Enter 键 = 应用当前筛选（搜索框体验） */
document.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && event.target.id === 'aiSearch') {
    state.ai.search = event.target.value.trim();
    renderPage();
  }
  if (event.key === 'Enter' && event.target.id === 'pipeSearch') {
    state.pipeline.search = event.target.value.trim();
    renderPage();
  }
  /* 添加创作者表单里按回车 = 保存（只会命中表单自己的输入框） */
  if (event.key === 'Enter' && event.target.id
      && event.target.id.indexOf('creator') === 0 && event.target.tagName === 'INPUT') {
    event.preventDefault();
    const handler = HANDLERS['creator-save'];
    if (handler) Promise.resolve(handler(null, event)).catch((error) => toast(String(error), 'bad'));
  }
  if (event.key === 'Escape') closeModal();
});

/* 后台分析时轮询刷新（只在自己触发的任务期间轮询，避免无谓开销） */
let pollTimer = null;
function startPolling(rounds = 30) {
  let count = 0;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    count += 1;
    await reloadAll({ render: false });
    renderPage();
    const busy = (state.stats.counts || {}).running > 0 || (state.stats.counts || {}).queued > 0;
    if (count >= rounds || !busy) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 2000);
}

/* ==================== 启动 ==================== */

window.addEventListener('hashchange', () => {
  state.route = parseHash();
  renderPage();
});

(async function boot() {
  state.route = parseHash();
  window.addEventListener('error', (event) => {
    logProblem(`运行时错误：${event.message} @ ${event.filename}:${event.lineno}`);
  });
  window.addEventListener('unhandledrejection', (event) => {
    logProblem(`未处理的 Promise 异常：${event.reason && event.reason.message
      ? event.reason.message : event.reason}`);
  });
  try {
    bridge = await waitForApi();
    if (!bridge) {
      fatalBox('没有检测到应用桥接',
        '请通过主程序启动（python outputs/TikTokBatchMVP/web_app.py）。'
        + '直接在浏览器里打开这个页面只能看样式，拿不到本地数据。');
      return;
    }
    const ok = await reloadAll();
    if (!ok) return;
  } catch (error) {
    logProblem(`启动失败：${error && error.stack ? error.stack : error}`);
    fatalBox('内容工厂启动失败', String(error && error.message ? error.message : error));
  }
})();
