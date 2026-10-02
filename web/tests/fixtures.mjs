export const audio = { format: 'mp3', bitrate: '320k', sample_rate: 48000, auto_transcode: true };
export const eq = { enabled: true, points: [[20,0],[100,3],[1000,0],[10000,-2],[20000,0]], preset: '', target: '', revision: 1 };
export const devices = [
  { did: 'd1', name: '小米智能音箱', alias: '客厅', model: 'lx06', presence: 'online', enabled: true, selected: true, volume: 40, eq },
  { did: 'd2', name: '小米音箱 Pro', alias: '卧室', model: 'l17a', presence: 'online', enabled: true, selected: false, volume: 30, eq },
];
export const config = {
  deployment: 'development', audio, app: { name: 'MiCast' }, receiver_mode: 'multi', airplay_protocol: 'classic', airplay_engine: 'local', dlna_enabled: true,
  sync_groups_enabled: true, large_delay_enabled: false, touchscreen_lyrics: true, default_volume: 40, default_volume_enabled: true, stale_session_timeout: 300,
  sender_volume_mode: 'independent', notify_webhook_url: '', airplay2_enabled: false, network_discovery_enabled: false, airplay2_available: false,
  airplay2_mode: 'disabled', airplay2_can_add_instances: false, storage: { mode: 'development', data_dir: 'C:/MiCast/data', log_dir: 'C:/MiCast/logs' },
  dlna_status: { status: 'running', detail: '正常' }, selected_device_id: 'd1', ports: [],
  receivers: [{ id: 'r1', name: '客厅', target_type: 'speaker', target_id: 'd1', enabled: true }],
  groups: [{ id: 'g1', name: '全屋播放', speaker_ids: ['d1','d2'], delays_ms: {}, anchor_did: 'd1', mode: 'mirror', channels: {}, gains_db: {} }], speaker_names: {},
};
export const status = { status: 'running', pcm_source: 'AirPlay', audio, stream_url: '', error_count: 0,
  receivers: [{ did: 'r1', name: '客厅', status: 'running', stream_url: '' }], airplay_protocol: 'classic', airplay_engine: 'local',
  now_playing: { r1: { title: '测试歌曲', artist: '测试歌手', album: '', lyric_lines: ['音乐正在播放'], cover: null } },
  orchestration: { configured: false, status: 'disabled', detail: '' }, diagnostics: { raop: {}, streams: {}, sinks: {} },
};
export const playback = { playing: true, paused: false, volume: 35, mixed_volume: true, muted: false,
  devices: devices.map(d => ({ did: d.did, name: d.alias, volume: d.volume, playing: true, paused: false, muted: false, state: 'playing' })),
};
export const topology = { ts: Date.now()/1000, status: 'running',
  nodes: [{ id: 'src', kind: 'source', label: 'iPhone', status: 'playing' }, { id: 'r1', kind: 'engine', label: '客厅', status: 'running' }, { id: 'd1', kind: 'speaker', label: '客厅音箱', status: 'playing' }],
  edges: [{ from: 'src', to: 'r1', active: true, protocol: 'RAOP', direction: 'push' }, { from: 'r1', to: 'd1', active: true, protocol: 'MP3', direction: 'pull' }],
};

export async function installFixtures(page, overrides = {}) {
  const requests = [];
  await page.addInitScript(() => {
    class TestSocket {
      static OPEN = 1; static CONNECTING = 0; readyState = 1;
      constructor(url) { this.url = url; if (url.endsWith('api/ws')) window.__stateSocket = this; setTimeout(() => this.onopen?.(), 10); }
      close() { this.readyState = 3; }
    }
    window.WebSocket = TestSocket;
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace('/app/micast', '');
    requests.push({ path, method: request.method(), body: request.postData() });
    const routes = {
      '/api/access/status': { access_configured: true, setup_complete: true, auth_enabled: false, authenticated: true, username: 'admin' },
      '/api/config': config, '/api/config/audio': audio, '/api/status': status, '/api/devices': devices, '/api/playback/state': playback,
      '/api/xiaomi/status': { logged_in: true, user_id: 'test', status: 'connected' },
      '/api/airplay2/state': { enabled: false, instances: [], targets: [], orchestration: {}, summary: {} },
      '/api/tuning/d1': { did: 'd1', ...eq }, '/api/tuning/d2': { did: 'd2', ...eq },
      '/api/devices/eq/presets': { presets: { flat: [[20,0],[20000,0]] }, targets: {}, saved: {}, freq_range: [20,20000], gain_range: [-12,12] },
      '/api/update/check': { update_available: false, current_version: '0.5.6' }, '/api/topology': topology,
    };
    if (overrides[path]) {
      const replacement = await overrides[path](route, requests);
      if (replacement === undefined) return;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(replacement) });
      return;
    }
    if (path === '/api/topology/stream') {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify(topology)}\n\n` });
      return;
    }
    let data = routes[path] ?? {};
    if (path === '/api/tuning/eq') { const payload = JSON.parse(request.postData()); data = { ...payload, revision: 2 }; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) });
  });
  return requests;
}
