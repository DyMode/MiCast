import { api } from "./api";
import { store } from "./state";
import { targetOwner } from './selectors';

let sequence = 0;
const confirmations = new Map<string, number>();
function requestScope(kind: string, ids?: string[]) {
  const token = ++sequence;
  const state = store.get();
  const epoch = store.runtimeGeneration;
  const targets = ids ?? state.playback?.devices.map(d => d.did) ?? state.devices.map(d => d.did);
  targets.forEach(did => confirmations.set(`${kind}:${did}`, token));
  const owners = new Map(targets.map(did => [did, targetOwner(state, did)]));
  const identity = (owner: ReturnType<typeof targetOwner>) => owner ? `${owner.owner}:${owner.generation}` : '';
  return (did: string) => confirmations.get(`${kind}:${did}`) === token &&
    store.runtimeGeneration === epoch &&
    identity(targetOwner(store.get(), did)) === identity(owners.get(did));
}

export async function setVolume(volume: number, deviceIds?: string[], relative = false): Promise<void> {
  const current = requestScope('volume', deviceIds);
  const result = await api.setVolume(volume, deviceIds, relative);
  const confirmed = new Map(result.devices.filter((d) => current(d.did) && d.ok && d.volume !== undefined).map((d) => [d.did, d.volume!]));
  const state = store.get();
  const playback = state.playback;
  const devices = playback?.devices.map((d) => confirmed.has(d.did) ? { ...d, volume: confirmed.get(d.did)! } : d);
  const known = devices?.map((d) => d.volume).filter((v): v is number => v != null) ?? [];
  store.set({
    devices: state.devices.map((d) => confirmed.has(d.did) ? { ...d, volume: confirmed.get(d.did)! } : d),
    ...(playback && devices ? { playback: { ...playback, devices, volume: known.length ? Math.round(known.reduce((a, b) => a + b, 0) / known.length) : null, mixed_volume: new Set(known).size > 1 } } : {}),
  });
  document.dispatchEvent(new CustomEvent("micast:render-devices"));
  document.dispatchEvent(new CustomEvent("micast:render-playback"));
  const failed = result.devices.filter((d) => !d.ok);
  if (failed.length) throw new Error(`${failed.length} 台音箱未能调整${confirmed.size ? "，其他音箱已更新" : ""}：${failed[0].error ?? "请检查连接"}`);
}

export async function setMute(muted: boolean, deviceIds?: string[]): Promise<void> {
  const current = requestScope('mute', deviceIds);
  const result = await api.setMute(muted, deviceIds);
  const confirmed = new Map(result.devices.filter((d) => current(d.did) && d.ok).map((d) => [d.did, d.muted!]));
  const state = store.get();
  const playback = state.playback;
  const devices = playback?.devices.map((d) => confirmed.has(d.did) ? { ...d, muted: confirmed.get(d.did)! } : d);
  store.set({
    devices: state.devices.map((d) => confirmed.has(d.did) ? { ...d, muted: confirmed.get(d.did)! } : d),
    ...(playback && devices ? { playback: { ...playback, devices, muted: !!devices.length && devices.every((d) => d.muted) } } : {}),
  });
  document.dispatchEvent(new CustomEvent("micast:render-devices"));
  document.dispatchEvent(new CustomEvent("micast:render-playback"));
  const failed = result.devices.filter((d) => !d.ok);
  if (failed.length) throw new Error(`${failed.length} 台音箱未能${muted ? "静音" : "恢复音量"}：${failed[0].error ?? "请检查连接"}`);
}
