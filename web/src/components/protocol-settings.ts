/** Protocol presentation and recovery belong to the mounted settings surface. */
import { api, type FullConfig } from '../api';
import { store } from '../state';
import type { SurfaceScope } from '../ui/lifecycle';

const escape = (value: string) => value.replace(/[&<>"']/g, char =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]!));
const names: Record<string, string> = { airplay: 'AirPlay', airplay2: 'AirPlay 2', dlna: 'DLNA' };
const statuses: Record<string, string> = {
  ready: '可用', disabled: '已关闭', unsupported: '不支持', idle: '待配置',
  degraded: '部分可用', blocked: '暂不可用',
};

export function renderProtocolRow(config: FullConfig | null, key: 'airplay' | 'airplay2' | 'dlna'): string {
  const state = config?.protocol_status?.[key];
  const enabled = key === 'airplay' ? config?.airplay_enabled !== false : key === 'airplay2' ? !!config?.airplay2_enabled : !!config?.dlna_enabled;
  const supported = state?.status !== 'unsupported' && (key !== 'airplay2' || !!config?.airplay2_available);
  const title = names[key];
  const status = state ? statuses[state.status] ?? '状态未知' : '正在读取状态';
  const detail = state?.detail || (supported ? '设置是否提供播放入口' : '当前安装方式不支持此功能');
  return `<div class="cell"><div class="cell-content"><div class="cell-title protocol-title">${title}${key === 'airplay2' ? ' <span class="feature-badge">实验性</span>' : ''}${key === 'dlna' ? `<details class="protocol-help"><summary aria-label="DLNA 生效方式" title="开关立即生效；投放音量控制下次投放时生效。正在播放时，请停止后重新选择音箱并投放。">ⓘ</summary><span class="protocol-help-content">开关立即生效；投放音量控制会在下次投放媒体时生效。若正在播放，请先在播放器中停止，再重新选择音箱并投放。</span></details>` : ''}</div>
    <span class="cell-subtitle">${escape(enabled ? status : '已关闭')} · ${escape(detail)}</span></div>
    <input type="checkbox" class="switch" id="${key}-enabled" ${enabled ? 'checked' : ''} ${supported ? '' : 'disabled'} aria-label="开启 ${title}"></div>
    ${enabled && state && ['blocked','degraded'].includes(state.status) ? `<div class="cell"><div class="cell-content"><span class="cell-subtitle">${title} 启动未成功，可重新尝试；详细原因见诊断。</span></div><button class="button compact secondary" type="button" data-retry-protocol="${key}">重新启动</button></div>` : ''}`;
}

export function bindProtocolRecovery(
  container: HTMLElement, scope: SurfaceScope, onStateChange: () => void,
) {
  container.querySelectorAll<HTMLButtonElement>('[data-retry-protocol]').forEach(button => {
    scope.listen(button, 'click', async () => {
      if (button.disabled) return;
      button.disabled = true;
      button.textContent = '正在启动…';
      try {
        const result = await api.retryProtocol(button.dataset.retryProtocol!);
        if (!scope.active) return;
        const config = store.get().fullConfig;
        if (config) store.set({ fullConfig: { ...config, protocol_status: result.protocol_status } });
        onStateChange();
      } catch (error) {
        if (scope.active) store.showToast(error instanceof Error ? error.message : '检测失败，请稍后重试');
      } finally {
        if (scope.active) { button.disabled = false; button.textContent = '重新启动'; }
      }
    });
  });
}
