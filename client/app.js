const $ = (id) => document.getElementById(id);
const state = {
  status: null, health: null, settings: null,
  errors: {}, loading: false, saving: false, refreshing: false, fixture: null,
};
const pending = new Map();
let sequence = 0;
const priorResolve = window.__nativeResolve;
window.__nativeResolve = (callbackId, result) => {
  const entry = pending.get(String(callbackId));
  if (!entry) {
    if (typeof priorResolve === 'function') priorResolve(callbackId, result);
    return;
  }
  pending.delete(String(callbackId));
  clearTimeout(entry.timer);
  if (result && result.ok === true) entry.resolve(result.data);
  else entry.reject(new Error(errorText(result?.error, '安卓请求失败')));
};

function errorText(value, fallback = '请求失败，请稍后重试') {
  if (typeof value === 'string' && value.trim()) return value.slice(0, 1000);
  if (value && typeof value.message === 'string') return value.message.slice(0, 1000);
  return fallback;
}

async function request(method, path, body) {
  if (state.fixture) return state.fixture.request(method, path, body);
  if (typeof window.Native?.request === 'function') {
    return new Promise((resolve, reject) => {
      const callbackId = `live-client-${Date.now()}-${++sequence}`;
      const timer = setTimeout(() => {
        pending.delete(callbackId);
        reject(new Error('安卓请求超时，请重试'));
      }, 120000);
      pending.set(callbackId, { resolve, reject, timer });
      try { window.Native.request(method, path, JSON.stringify(body || {}), callbackId); }
      catch (error) {
        clearTimeout(timer);
        pending.delete(callbackId);
        reject(error);
      }
    });
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120000);
  try {
    const response = await fetch(path, {
      method, signal: controller.signal, cache: 'no-store', credentials: 'same-origin',
      headers: body === undefined ? { Accept: 'application/json' } : {
        Accept: 'application/json', 'Content-Type': 'application/json',
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    let data;
    try { data = await response.json(); }
    catch { throw new Error(response.ok ? '后端返回了无效 JSON' : `请求失败（HTTP ${response.status}）`); }
    if (!response.ok) throw new Error(errorText(data?.error, `请求失败（HTTP ${response.status}）`));
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超时，请重试');
    if (error instanceof TypeError) throw new Error('无法连接本地后端，请检查服务和网络');
    throw error;
  } finally { clearTimeout(timer); }
}

function dateValue(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value)) return null;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}
function time(value, fallback = '暂无记录') {
  const date = dateValue(value);
  return date ? date.toLocaleString('zh-CN', { hour12: false }) : fallback;
}
function safeUrl(value, allowRelative = false) {
  if (typeof value !== 'string' || (!/^https?:\/\//i.test(value) && !(allowRelative && /^(?:\.\/|\/(?!\/))/.test(value)))) return null;
  try {
    const url = new URL(value, location.href);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = String(content);
  return node;
}
function notice(id, text) {
  $(id).textContent = text || '';
  $(id).hidden = !text;
}
function feedback(text) { notice('feedback', text); }
function addDetail(parent, label, value) {
  const row = el('div');
  row.append(el('dt', '', label), el('dd', '', value));
  parent.append(row);
}
const names = {
  live: '直播中', offline: '未开播', unknown: '状态未知',
  ok: '运行正常', healthy: '运行正常', success: '成功', failure: '失败',
  error: '异常', degraded: '部分异常', stale: '数据已过期',
  pending: '等待中', queued: '排队中', in_progress: '运行中', completed: '已结束',
  cancelled: '已取消', skipped: '已跳过', timed_out: '已超时',
  accepted: '服务器已接受', disabled: '未启用', not_configured: '未配置',
};
function label(value, fallback = '未知') {
  return typeof value === 'string' && value ? (names[value] || value) : fallback;
}

function renderControls() {
  const busy = state.loading || state.saving || state.refreshing;
  $('refresh').disabled = busy;
  $('refresh').textContent = state.refreshing ? '正在实时检测…' : '立即检测 ↗';
  $('reload').disabled = busy;
  for (const id of ['room-id', 'room-name', 'repository', 'add-room', 'save-repo']) {
    $(id).disabled = !state.settings || busy;
  }
  $('add-room').textContent = state.saving ? '正在保存…' : '＋ 添加房间';
  $('save-repo').textContent = state.saving ? '正在保存…' : '保存仓库';
  document.querySelectorAll('.remove').forEach((button) => { button.disabled = !state.settings || busy; });
  const errorCount = Object.keys(state.errors).length;
  $('connection').dataset.state = errorCount ? 'error' : 'ok';
  $('connection').textContent = state.fixture ? '截图预览' :
    state.refreshing ? '正在检测' : state.loading ? '正在同步' : errorCount ?
      (state.status || state.health || state.settings ? '部分数据不可用' : '连接失败') :
      typeof window.Native?.request === 'function' ? '安卓本地连接' : '本地连接';
  // WebView may report no network even while its local native bridge remains available.
  $('network-note').hidden = navigator.onLine !== false || !!state.fixture;
}

function roomCard(room) {
  const card = el('article', 'room-card');
  const cover = el('div', 'cover');
  const placeholder = el('span', '', 'NO COVER / 暂无封面');
  cover.append(placeholder);
  const source = safeUrl(room.cover, true);
  if (source) {
    const image = el('img');
    image.alt = '';
    image.loading = 'lazy';
    image.decoding = 'async';
    image.referrerPolicy = 'no-referrer';
    image.addEventListener('error', () => image.remove(), { once: true });
    image.src = source;
    cover.append(image);
  }
  const status = ['live', 'offline'].includes(room.status) ? room.status : 'unknown';
  cover.append(el('span', `badge ${status}`, `${label(status)}${room.stale ? ' · 已过期' : ''}`));
  const body = el('div', 'room-body');
  const top = el('div', 'room-top');
  top.append(el('h3', 'room-name', room.name || `房间 ${room.id}`), el('span', 'room-id', `# ${room.id}`));
  body.append(top, el('p', 'room-title', room.title || (status === 'unknown' ? '等待有效的检测结果' : '暂无直播标题')));
  const dates = el('dl', 'room-times');
  if (status === 'live') addDetail(dates, '本次开播', time(room.live_started_at));
  addDetail(dates, room.last_live_estimated ? '上次开播（估算）' : '上次开播', time(room.last_live_at));
  addDetail(dates, '最近检测', time(room.checked_at));
  body.append(dates);
  if (room.error) body.append(el('p', 'room-error', errorText(room.error)));
  const actions = el('div', 'card-actions');
  const url = safeUrl(room.url);
  if (url) {
    const link = el('a', 'text-link', '打开直播间 ↗');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    actions.append(link);
  } else actions.append(el('span', 'muted text-link', '暂无直播间链接'));
  const remove = el('button', 'text-link remove', '移除');
  remove.type = 'button';
  remove.setAttribute('aria-label', `移除 ${room.name || room.id}`);
  remove.addEventListener('click', () => removeRoom(String(room.id)));
  actions.append(remove);
  body.append(actions);
  card.append(cover, body);
  return card;
}

function renderRooms() {
  const target = $('rooms');
  target.replaceChildren();
  target.setAttribute('aria-busy', String(state.loading || state.refreshing));
  const observed = state.status?.rooms || [];
  const rooms = state.settings ? state.settings.rooms.map((configured) => {
    const found = observed.find((room) => String(room.id) === String(configured.id));
    return { ...(found || { status: 'unknown' }), id: configured.id, name: configured.name || found?.name || '' };
  }) : observed;
  const statusError = state.errors.status;
  const partial = rooms.filter((room) => room.error || room.stale || !['live', 'offline'].includes(room.status)).length;
  notice('rooms-message', statusError ? `房间查询失败：${statusError}${state.status ? '。保留上次获取的数据。' : ''}` :
    partial ? `${partial} 个房间的数据未知、过期或检测失败，请查看各房间说明。` : '');
  $('total').textContent = state.settings || state.status ? String(rooms.length) : '—';
  const liveCount = rooms.filter((room) => room.status === 'live').length;
  $('live-count').textContent = state.status ? `${liveCount}${partial || statusError ? ' *' : ''}` : '—';
  $('live-count').title = partial || statusError ? '包含未知或过期状态，直播数量可能不完整' : '最近一次获取到的直播数量';
  $('checked-at').textContent = time(state.status?.checked_at, '尚无有效检测时间');
  $('room-count').textContent = state.settings || state.status ? String(rooms.length).padStart(2, '0') : '';
  if (!rooms.length) {
    const empty = el('div', 'empty');
    empty.append(el('strong', '', state.loading ? '正在获取房间…' : statusError ? '暂时无法查询' : '还没有关注的房间'),
      el('span', '', state.loading ? '等待本地服务返回数据' : statusError ? '请检查本地服务，然后重新连接。' : '在关注设置中添加房间 ID，开始关注动态。'));
    target.append(empty);
  } else rooms.forEach((room) => target.append(roomCard(room)));
  renderControls();
}

function renderHealth() {
  const health = state.health;
  const checked = dateValue(health?.last_checked_at);
  const now = state.fixture?.now ?? Date.now();
  const stale = health?.stale === true || (checked && now - checked.getTime() > 15 * 60 * 1000);
  const mailUnavailable = health?.mail_status === 'unavailable';
  $('health-badge').textContent = !health ? (state.errors.health ? '不可用' : '加载中') :
    state.errors.health ? '读取失败' : mailUnavailable ? '邮箱异常' : stale ? '已过期' :
      health.status === 'ok' && health.mail_status !== 'ok' ? '邮箱待确认' : label(health.status);
  $('health-status').textContent = health ? (health.status === 'ok' ? '检测正常' : label(health.status)) : '尚未获取';
  $('mail-status').textContent = mailUnavailable ? '邮箱认证失败，请更新授权码' :
    health?.mail_status === 'ok' ? '认证正常' : '未知 · 尚未确认';
  $('health-checked').textContent = time(health?.last_checked_at);
  $('health-mail').textContent = time(health?.last_mail_at);
  const countValue = (value) => Array.isArray(value) ? String(value.length) :
    typeof value === 'number' && Number.isFinite(value) && value >= 0 ? String(value) :
      typeof value === 'boolean' ? (value ? '有待处理' : '无') : '—';
  $('health-pending').textContent = countValue(health?.pending);
  $('failures').textContent = countValue(health?.failures);
  const run = health?.latest_run;
  $('run-status').textContent = run ? `${label(run.status)}${run.conclusion ? ` · ${label(run.conclusion)}` : ''}` : '暂无运行记录';
  $('run-created').textContent = time(run?.created_at);
  const url = safeUrl(run?.html_url);
  $('run-link').hidden = !url;
  if (url) $('run-link').href = url;
  else $('run-link').removeAttribute('href');
  const messages = [];
  if (mailUnavailable) messages.push('邮箱认证失败，请更新授权码。B 站检测结果不能代表邮件通知可用。');
  if (state.errors.health) messages.push(`云端健康查询失败：${state.errors.health}${health ? '。保留上次获取的数据。' : ''}`);
  if (stale) messages.push('云端心跳已过期，当前任务状态可能已变化。');
  if (health?.error) messages.push(errorText(health.error));
  notice('health-message', messages.join(' '));
}

function validateResponse(key, data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('后端数据格式不正确');
  if (key === 'settings' || key === 'status') {
    if (!Array.isArray(data.rooms) || data.rooms.some((room) => !room || typeof room !== 'object' ||
      !['string', 'number'].includes(typeof room.id))) throw new Error('后端房间数据格式不正确');
  }
  if (key === 'settings' && data.repository != null && typeof data.repository !== 'string') throw new Error('仓库设置格式不正确');
  return data;
}

async function load(includeSettings = false) {
  if (state.loading || state.saving) return;
  state.loading = true;
  renderControls();
  $('rooms').setAttribute('aria-busy', 'true');
  const keys = includeSettings ? ['status', 'health', 'settings'] : ['status', 'health'];
  await Promise.allSettled(keys.map(async (key) => {
    try {
      state[key] = validateResponse(key, await request('GET', `/api/${key}`));
      delete state.errors[key];
      if (key === 'settings') $('repository').value = state.settings.repository || '';
    } catch (error) { state.errors[key] = errorText(error); }
    notice('settings-message', state.errors.settings ? `设置读取失败：${state.errors.settings}。重新连接后可编辑。` : '');
    renderRooms();
    renderHealth();
  }));
  state.loading = false;
  renderRooms();
  renderControls();
}

async function saveSettings(repository, rooms) {
  if (!state.settings || state.saving || state.loading || state.refreshing) return false;
  state.saving = true;
  renderControls();
  try {
    const payload = { repository, rooms: rooms.map(({ id, name }) => ({ id: String(id), name: name || '' })) };
    const result = await request('PUT', '/api/settings', payload);
    state.settings = result && Array.isArray(result.rooms) ? validateResponse('settings', result) : payload;
    delete state.errors.settings;
    $('repository').value = state.settings.repository || '';
    feedback('本地设置已保存。云端 ENV_FILE 未修改。');
    renderRooms();
    return true;
  } catch (error) {
    feedback(`保存失败：${errorText(error)}。当前改动尚未保存。`);
    return false;
  } finally {
    state.saving = false;
    renderControls();
  }
}

async function removeRoom(id) {
  if (!state.settings) return;
  const saved = await saveSettings(state.settings.repository || '', state.settings.rooms.filter((room) => String(room.id) !== id));
  if (saved) await load();
}

$('room-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!state.settings) return;
  if (state.settings.rooms.length >= 30) { feedback('最多关注 30 个房间，请先移除其他房间。'); return; }
  const raw = $('room-id').value.trim();
  if (!/^\d{1,20}$/.test(raw) || /^0+$/.test(raw)) { feedback('请输入有效的正整数房间 ID。'); return; }
  const id = raw.replace(/^0+/, '');
  if (state.settings.rooms.some((room) => String(room.id).replace(/^0+/, '') === id)) {
    feedback('这个房间已在关注列表中。'); return;
  }
  const name = $('room-name').value.trim();
  const saved = await saveSettings(state.settings.repository || '', [...state.settings.rooms, { id, name }]);
  if (saved) { $('room-form').reset(); await load(); }
});
$('repo-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!state.settings) return;
  const repository = $('repository').value.trim();
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) {
    feedback('仓库格式应为 owner/repository，例如 octocat/Hello-World。'); return;
  }
  if (await saveSettings(repository, state.settings.rooms)) await load();
});
async function refresh() {
  if (state.refreshing || state.loading || state.saving) return;
  state.refreshing = true;
  renderControls();
  feedback('正在请求实时检测…');
  try {
    await request('POST', '/api/refresh', {});
    await load();
    feedback(state.errors.status ? '实时检测请求已完成，但结果读取失败，请重新连接。' : '实时检测请求已完成，已重新读取房间状态。');
  } catch (error) { feedback(`实时检测失败：${errorText(error)}。已保留当前数据。`); }
  finally { state.refreshing = false; renderControls(); }
}
$('refresh').addEventListener('click', () => { void refresh(); });
$('reload').addEventListener('click', () => load(true));
window.addEventListener('offline', renderControls);
window.addEventListener('online', () => { renderControls(); if (!state.refreshing) void load(!state.settings); });
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && !state.refreshing) void load(!state.settings);
});

async function start() {
  // Explicitly opt in. Production does not import or select any fixture by default.
  const fixtureName = new URLSearchParams(location.search).get('screenshot');
  if (fixtureName || window.__LIVE_NOTIFY_FIXTURE__) {
    try {
      const { createFixture } = await import('./screenshot-data.js');
      state.fixture = createFixture(window.__LIVE_NOTIFY_FIXTURE__ || fixtureName);
      $('fixture-note').hidden = false;
    } catch (error) {
      feedback(`截图数据加载失败：${errorText(error)}。请移除 screenshot 参数后连接真实后端。`);
      $('connection').textContent = '预览加载失败';
      $('refresh').disabled = true;
      $('reload').disabled = true;
      return;
    }
  }
  await load(true);
  if (!document.hidden) await refresh();
  setInterval(() => {
    if (!document.hidden) void refresh();
  }, 60000);
}
void start();
