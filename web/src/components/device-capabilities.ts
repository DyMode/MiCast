import { api } from '../api';
import { store } from '../state';
import { icon } from '../icons';
import { escapeHtml as esc, message } from './receivers-shared';

type Proof = { action: string; format: string; level: string; stale: boolean; can_confirm: boolean; pulled_at: number; verified_at: number };
type Device = { id: string; name: string; model: string; availability: string; revision: number; records: Proof[] };
type Entry = { id: string; kind: string; name: string; target_type: string; policy: string; local_target_id: string | null; route: { channel: string; reason: string } };
export type CapabilityData = { devices: Device[]; entries: Entry[]; runtime: Record<string, Record<string, { detail: string; failure_stage: string; retries: number; timings_ms: Record<string, number> }>> };

// One shared read: the three render units below (settings 控制通道, account
// 指令验证, debug 控制通道状态) slice the same payload for their hosts.
let data: CapabilityData | null = null;
let loading = false;
let refreshed = 0;
let feedback = '';
let loadVersion = 0;
const busy = new Set<string>();
const drafts = new Map<string, { policy: string; target: string }>();
const expandedDevices = new Set<string>();
store.subscribe((state, prev) => {
  if (state.xiaomi.user_id !== prev.xiaomi.user_id || state.xiaomi.logged_in !== prev.xiaomi.logged_in) {
    loadVersion++; data = null; refreshed = 0; loading = false; feedback = ''; drafts.clear(); busy.clear(); expandedDevices.clear();
  }
});
const levels: Record<string, string> = { unknown: '未知', declared: '协议声明', accepted: '命令成功', pulled: '已传输音频', confirmed: '已确认出声', unsupported: '明确不支持' };
const actions: Record<string, string> = { play_file: '文件播放', play_stream: '持续流播放', pause: '暂停', resume: '继续', stop: '停止', get_volume: '读取音量', set_volume: '设置音量', status: '读取状态' };
const failureLabels: Record<string, string> = { control_timeout: '控制请求超时', control_failed: '控制请求失败', no_stream_pull: '设备未取流', stream_interrupted: '设备取流中断', source_idle: '音源暂无数据', unreachable: '设备离线', manual_retry_failed: '手动重试失败', stop_failed: '旧播放停止失败' };
const proofKey = (device: Device, proof: Proof) => `confirm:${device.id}:${proof.action}:${proof.format}:${proof.pulled_at}`;
const policies: Record<string, string> = { legacy: '沿用原配置', auto: '已验证时优先本地', local: '仅本地', cloud: '仅云端' };
const timingLabels: Record<string, string> = { control: '控制响应', first_http_pull: '首次取流' };

function requestRender(): void {
  window.dispatchEvent(new Event('micast:request-render'));
}

async function load(force = false): Promise<void> {
  if (loading && !force) return;
  const version = ++loadVersion;
  const current = store.beginRead('capabilities', true);
  loading = true;
  try {
    const result = await api.getCapabilities();
    if (version !== loadVersion || !current()) return;
    // Tolerate a bare {} from a stubbed/misbehaving endpoint.
    data = Array.isArray(result?.devices) && Array.isArray(result?.entries)
      ? result
      : { devices: [], entries: [], runtime: {} };
    refreshed = Date.now(); feedback = '';
  } catch (error) {
    if (version !== loadVersion || !current()) return;
    feedback = message(error); refreshed = Date.now();
  } finally {
    if (version === loadVersion) { loading = false; requestRender(); }
  }
}

/** Hosts call this when their page opens so the units fill in. */
export function refreshCapabilities(force = false): void { void load(force); }

const loadingGroup = `<div class="group"><div class="cell"><div class="cell-content"><span class="cell-title">正在读取能力数据…</span></div></div></div>`;
const feedbackRow = () => feedback ? `<div class="cell"><div class="cell-content"><span class="cell-subtitle">${esc(feedback)}</span></div></div>` : '';

function relativeTime(unixSeconds: number): string {
  const age = Math.max(0, Date.now() / 1000 - unixSeconds);
  if (age < 90) return '刚刚';
  if (age < 3600) return `${Math.round(age / 60)} 分钟前`;
  if (age < 86400) return `${Math.round(age / 3600)} 小时前`;
  return `${Math.round(age / 86400)} 天前`;
}

function ensureLoaded(): void {
  if (!data || Date.now() - refreshed > 10000) void load();
}

// ---------- Unit 1: control channel policies (settings → 高级设置) ----------

export function renderControlChannel(): string {
  if (!data) return loadingGroup;
  const speakerEntries = data.entries.filter(entry => entry.target_type === 'speaker');
  if (!speakerEntries.length) {
    return `<div class="group">${feedbackRow()}<div class="cell"><div class="cell-content"><span class="cell-title">没有使用云端控制的播放入口</span><span class="cell-subtitle">添加云端音箱的播放入口后，可在此设置本地优先或仅本地的控制通道</span></div></div></div>`;
  }
  // One card per entry: with several speakers a flat cell list makes the
  // 关联本地设备 row read as belonging to whichever name sits above it.
  return `${feedbackRow() ? `<div class="group">${feedbackRow()}</div>` : ''}
  ${speakerEntries.map(entry => {
    const key = `${entry.kind}:${entry.id}`;
    const draft = drafts.get(key) || { policy: entry.policy, target: entry.local_target_id || '' };
    const pending = busy.has(key);
    return `<div class="group">
      <div class="cell">
        <div class="cell-content"><span class="cell-title">${esc(entry.name)}</span><span class="cell-subtitle">${esc(entry.route.reason)}</span></div>
        <select class="input" data-cap-policy="${esc(key)}" ${pending ? 'disabled' : ''} aria-label="${esc(entry.name)} 控制策略">
          ${Object.entries(policies).map(([value, label]) => `<option value="${value}" ${draft.policy === value ? 'selected' : ''}>${label}</option>`).join('')}
        </select>
      </div>
      <div class="cell">
        <div class="cell-content"><span class="cell-title">关联本地设备</span></div>
        <select class="input" data-cap-target="${esc(key)}" ${pending ? 'disabled' : ''} aria-label="${esc(entry.name)} 关联本地设备">
          <option value="">未关联</option>
          ${data!.devices.filter(device => device.id.startsWith('dlna:')).map(device => `<option value="${esc(device.id.replace(/^dlna:/, ''))}" ${draft.target === device.id.replace(/^dlna:/, '') ? 'selected' : ''}>${esc(device.name || device.id)} · ${device.availability === 'online' ? '在线' : '离线'}</option>`).join('')}
        </select>
      </div>
    </div>`;
  }).join('')}`;
}

export function bindControlChannel(container: HTMLElement): void {
  container.querySelectorAll<HTMLSelectElement>('[data-cap-policy],[data-cap-target]').forEach(select => {
    select.addEventListener('change', async () => {
      const key = select.dataset.capPolicy ?? select.dataset.capTarget ?? '';
      if (busy.has(key)) return;
      const kind = key.split(':', 1)[0];
      const id = key.slice(kind.length + 1);
      const policy = container.querySelector<HTMLSelectElement>(`[data-cap-policy="${CSS.escape(key)}"]`)?.value ?? 'auto';
      const target = container.querySelector<HTMLSelectElement>(`[data-cap-target="${CSS.escape(key)}"]`)?.value ?? '';
      const current = store.beginRead(`capabilities-save:${key}`, true);
      drafts.set(key, { policy, target }); busy.add(key); requestRender();
      try {
        await api.setControlPolicy(kind, id, policy, target || null);
        if (!current()) return;
        drafts.delete(key); await load(true);
      } catch (error) { if (current()) feedback = message(error); }
      finally { if (current()) { busy.delete(key); requestRender(); } }
    });
  });
  ensureLoaded();
}

// ---------- Unit 2: device proofs (服务 → 指令验证) ----------

export function renderDeviceProofs(): string {
  if (!data) return loadingGroup;
  const devices = data.devices.filter(device => device.id.startsWith('xiaomi:'));
  if (!devices.length) {
    return `<div class="group">${feedbackRow()}<div class="cell"><div class="cell-content"><span class="cell-title">尚无验证记录</span><span class="cell-subtitle">用手机实际播放后，这里会记录每台音箱真实响应过的指令</span></div></div></div>`;
  }
  return `<div class="group">${feedbackRow()}
    <div class="cell">
      <div class="cell-content"><span class="cell-title">协议声明不代表实际出声</span><span class="cell-subtitle">验证级别：命令成功 &lt; 已传输音频 &lt; 已确认出声；听到待确认的设备出声后，点「已听到声音」完成确认</span></div>
      <button class="button plain" data-cap-refresh ${loading ? 'disabled' : ''}>${loading ? '正在读取…' : '刷新'}</button>
    </div>
  </div>
  ${devices.map(device => {
    const expanded = expandedDevices.has(device.id);
    const counts = { confirmed: 0, pending: 0, unsupported: 0, unverified: 0 };
    device.records.forEach(proof => {
      if (proof.level === 'confirmed') counts.confirmed++;
      else if (proof.level === 'unsupported') counts.unsupported++;
      else if (proof.can_confirm) counts.pending++;
      else counts.unverified++;
    });
    const chips = [
      counts.confirmed ? `<span class="meta-chip ok">已确认 ${counts.confirmed}</span>` : '',
      counts.pending ? `<span class="meta-chip warn">待确认 ${counts.pending}</span>` : '',
      counts.unsupported ? `<span class="meta-chip">不支持 ${counts.unsupported}</span>` : '',
      counts.unverified ? `<span class="meta-chip">未验证 ${counts.unverified}</span>` : '',
    ].filter(Boolean).join('');
    const confirmable = device.records.findIndex(proof => proof.can_confirm);
    return `<div class="sync-group">
      <div class="cell sync-group-header">
        <button class="group-toggle group-identity ${expanded ? 'expanded' : ''}" type="button" data-cap-device="${esc(device.id)}" aria-expanded="${expanded}" aria-label="展开或收起 ${esc(device.name || device.id)} 的指令验证">
          <span class="group-identity-copy"><strong>${esc(device.name || device.id)}</strong><small>${esc(device.model)} · ${device.availability === 'online' ? '在线' : device.availability === 'unknown' ? '在线状态未知' : '离线'}</small>${chips ? `<span class="meta-chip-row">${chips}</span>` : ''}</span>${icon('chevron')}
        </button>
        ${confirmable >= 0 ? `<div class="group-header-actions"><button class="button secondary" data-cap-confirm="${esc(device.id)}" data-index="${confirmable}" ${busy.has(proofKey(device, device.records[confirmable])) ? 'disabled' : ''}>已听到声音</button></div>` : ''}
      </div>
      <div class="sync-group-body" ${expanded ? '' : 'hidden'}>
        <div class="group">${device.records.map((proof, i) => `<div class="cell">
          <div class="cell-content"><span class="cell-title">${esc(actions[proof.action] || proof.action)}${proof.format ? ` · ${esc(proof.format)}` : ''}</span><span class="cell-subtitle">${esc(levels[proof.level] || proof.level)}${proof.stale ? ' · 记录已过期' : ''}${proof.verified_at ? ` · ${relativeTime(proof.verified_at)}` : ' · 尚无验证记录'}</span></div>
          ${proof.can_confirm ? `<button class="button secondary" data-cap-confirm="${esc(device.id)}" data-index="${i}" ${busy.has(proofKey(device, proof)) ? 'disabled' : ''}>已听到声音</button>` : ''}
        </div>`).join('') || '<div class="cell"><div class="cell-content"><span class="cell-subtitle">尚无验证记录</span></div></div>'}</div>
      </div>
    </div>`;
  }).join('')}`;
}

export function bindDeviceProofs(container: HTMLElement): void {
  container.querySelectorAll<HTMLButtonElement>('[data-cap-device]').forEach(button => button.addEventListener('click', () => {
    const id = button.dataset.capDevice!;
    if (expandedDevices.has(id)) expandedDevices.delete(id); else expandedDevices.add(id);
    requestRender();
  }));
  container.querySelector('[data-cap-refresh]')?.addEventListener('click', () => void load(true));
  bindConfirmButtons(container);
  ensureLoaded();
}

function bindConfirmButtons(container: HTMLElement): void {
  container.querySelectorAll<HTMLButtonElement>('[data-cap-confirm]').forEach(button => {
    // Bind the exact displayed proof; a later read cannot confirm an unseen test.
    const device = data?.devices.find(item => item.id === button.dataset.capConfirm);
    const proof = device?.records[Number(button.dataset.index)];
    button.addEventListener('click', async () => {
      if (!device || !proof) return;
      const key = proofKey(device, proof);
      if (busy.has(key)) return;
      const current = store.beginRead(key, true);
      busy.add(key); requestRender();
      try {
        await api.confirmCapability({ device_id: device.id, revision: device.revision, action: proof.action, format: proof.format, pulled_at: proof.pulled_at });
        if (current()) await load(true);
      } catch (error) { if (current()) feedback = message(error); }
      finally { if (current()) { busy.delete(key); requestRender(); } }
    });
  });
}

// ---------- Unit 3: control runtime (诊断 → 控制通道) ----------

export function renderControlRuntime(open: boolean): string {
  if (!data) return '';
  const failures: Array<{ owner: string; detail: string; failure_stage: string; retries: number; timings_ms: Record<string, number> }> = [];
  for (const [owner, targets] of Object.entries(data.runtime)) {
    for (const runtime of Object.values(targets)) {
      if (runtime.failure_stage) failures.push({ owner, ...runtime });
    }
  }
  if (!failures.length) return '';
  return `<details class="diagnostic-details" data-debug-section="control" ${open ? 'open' : ''}>
    <summary><span><strong>控制通道</strong><small>${failures.length} 个入口播放异常</small></span></summary>
    <div class="group">${failures.map(runtime => `<div class="cell">
      <div class="cell-content"><span class="cell-title">${esc(data!.entries.find(entry => entry.id === runtime.owner)?.name || runtime.owner)}</span><span class="cell-subtitle">${esc(runtime.detail)} · ${esc(failureLabels[runtime.failure_stage] || '播放异常')} · 已重试 ${runtime.retries} 次</span><span class="cell-subtitle">${Object.entries(runtime.timings_ms).map(([key, value]) => `${timingLabels[key] ?? '首次音频字节'} ${value} ms`).join(' · ')}（时序为服务端估算，不代表实际出声延迟）</span></div>
      <button class="button secondary" data-cap-retry="${esc(runtime.owner)}" ${busy.has(`retry:${runtime.owner}`) ? 'disabled' : ''}>重试本地播放</button>
    </div>`).join('')}</div>
  </details>`;
}

export function bindControlRuntime(container: HTMLElement, onUpdate?: () => void): void {
  container.querySelectorAll<HTMLButtonElement>('[data-cap-retry]').forEach(button => button.addEventListener('click', async () => {
    const owner = button.dataset.capRetry!;
    const key = `retry:${owner}`;
    if (busy.has(key)) return;
    const current = store.beginRead(key, true);
    busy.add(key); requestRender();
    try { await api.retryLocalPlayback(owner); if (current()) await load(true); }
    catch (error) { if (current()) feedback = message(error); }
    finally {
      if (current()) { busy.delete(key); requestRender(); }
      // The debug page preserves its markup across polls; push the new data in.
      if (container.isConnected) onUpdate?.();
    }
  }));
  // First open (or stale data): pull once, then let the host repaint.
  if (!data || Date.now() - refreshed > 10000) {
    void load().then(() => { if (container.isConnected) onUpdate?.(); });
  }
}

// ---------- Local bridge summary (播放页 局域网设备 rows) ----------

/** Short verified-state label for a discovered DLNA device, or null when unknown. */
export function dlnaProofLabel(deviceId: string): string | null {
  const device = data?.devices.find(item => item.id === `dlna:${deviceId}`);
  if (!device || !device.records.length) return null;
  if (device.records.some(proof => proof.level === 'confirmed')) return '已确认实际播放';
  if (device.records.some(proof => proof.can_confirm)) return '待确认出声';
  if (device.records.some(proof => proof.level === 'pulled' || proof.level === 'accepted')) return '已传输音频 · 待确认';
  return '仅协议声明';
}
