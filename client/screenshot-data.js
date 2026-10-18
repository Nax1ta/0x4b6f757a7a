// Loaded only with an explicit ?screenshot=... or window.__LIVE_NOTIFY_FIXTURE__.
const timestamp = '2026-10-18T06:30:00Z';
const makeRooms = () => [
  { id: '10001', name: '山间电台', title: '一些音乐，一段安静的午后', cover: './preview-cover.svg', url: 'https://live.bilibili.com/10001', status: 'live', live_started_at: '2026-10-18T05:00:00Z', last_live_at: '2026-10-18T05:00:00Z', checked_at: timestamp, stale: false },
  { id: '10002', name: '慢速放映室', title: '今天也在认真生活', cover: null, url: 'https://live.bilibili.com/10002', status: 'offline', live_started_at: null, last_live_at: '2026-10-17T12:15:00Z', last_live_estimated: true, checked_at: timestamp, stale: false },
  { id: '10003', name: '无声工作台', title: '写代码与做东西', cover: null, url: 'https://live.bilibili.com/10003', status: 'offline', live_started_at: null, last_live_at: null, checked_at: timestamp, stale: false },
];
export function createFixture(input) {
  const modes = ['demo', 'empty', 'partial', 'offline', 'mail-error', 'error', 'loading'];
  if (typeof input === 'string' && !modes.includes(input)) throw new Error(`未知截图模式：${input}`);
  const mode = typeof input === 'string' ? input : 'custom';
  const rooms = makeRooms();
  if (mode === 'partial') Object.assign(rooms[2], { status: 'unknown', checked_at: null, error: '固定预览：房间查询超时', stale: true });
  if (mode === 'offline') rooms.forEach((room) => { room.status = 'offline'; room.live_started_at = null; });
  let snapshots = typeof input === 'object' && input !== null ? structuredCopyFallback(input) : {
    settings: { repository: 'example/live-notify', rooms: mode === 'empty' ? [] : rooms.map(({ id, name }) => ({ id, name })) },
    status: { checked_at: timestamp, rooms: mode === 'empty' ? [] : rooms },
    health: { status: mode === 'partial' ? 'stale' : 'ok', mail_status: mode === 'mail-error' ? 'unavailable' : 'ok', stale: mode === 'partial', last_checked_at: timestamp,
      last_mail_at: '2026-10-18T05:01:00Z', pending: 0, failures: 0,
      latest_run: { status: 'completed', conclusion: 'success', created_at: '2026-10-18T06:29:00Z', html_url: 'https://github.com/example/live-notify/actions' }, error: null },
  };
  return {
    now: Date.parse(snapshots.now || timestamp),
    async request(method, path, body) {
      if (mode === 'loading') return new Promise(() => {});
      if (mode === 'error') throw new Error('固定预览：本地服务无法连接');
      const key = path.split('/').pop();
      if (snapshots.errors?.[key]) throw new Error(String(snapshots.errors[key]));
      if (method === 'PUT' && key === 'settings') {
        snapshots.settings = structuredCopyFallback(body);
        snapshots.status.rooms = body.rooms.map((room) => ({
          ...(snapshots.status.rooms.find((old) => String(old.id) === String(room.id)) || { status: 'unknown' }), ...room,
        }));
        return structuredCopyFallback(snapshots.settings);
      }
      if (method === 'POST' && key === 'refresh') return { checked_at: snapshots.status?.checked_at };
      if (method !== 'GET' || !['status', 'settings', 'health'].includes(key)) throw new Error('预览不支持此请求');
      return structuredCopyFallback(snapshots[key]);
    },
  };
}
function structuredCopyFallback(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}
