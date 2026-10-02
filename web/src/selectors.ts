/** Shared projections: components read facts without inventing another state. */
import type { State } from './state';
import { store } from './state';
import type { PlaybackState } from './api';

export function activePlayback(state: State): PlaybackState | null {
  const playback = state.playback;
  return playback?.devices.length && (playback.playing || playback.paused) ? playback : null;
}

export function targetOwner(state: State, did: string) {
  const key = did.startsWith('airplay:') ? did : did.startsWith('dlna:') ? `dlna-target:${did.slice(5)}` : `speaker:${did}`;
  const epoch = store.runtimeGeneration;
  const status = !epoch || state.status?.runtime?.epoch === epoch ? state.status?.runtime : undefined;
  const playback = !epoch || state.playback?.runtime?.epoch === epoch ? state.playback?.runtime : undefined;
  const runtime = playback && (!status || playback.revision > status.revision ||
    (playback.revision === status.revision && (playback.sequence ?? 0) > (status.sequence ?? 0))) ? playback : status;
  return runtime?.targets.find(target => target.target === key);
}

export function activeSessions(state: State) {
  return state.status?.runtime?.sessions.filter(session => ['active', 'quiet', 'paused'].includes(session.state)) ?? [];
}

export function deviceTuning(state: State, did: string) {
  return state.devices.find(device => device.did === did)?.eq;
}

export function volumeTargets(state: State, ids: string[]) {
  return ids.filter(did => targetOwner(state, did)?.capabilities?.volume_control !== false);
}
