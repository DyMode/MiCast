import { api } from "./api";
import { icon } from "./icons";
import { store } from "./state";
import { setMute, setVolume } from "./volume-service";

// One delegated handler survives page rerenders without duplicate listeners.
document.addEventListener("click", async (event) => {
  const target = event.target as HTMLElement;

  const muteButton = target.closest<HTMLButtonElement>("[data-mute]");
  if (muteButton && !muteButton.disabled) {
    const dids = (muteButton.dataset.mute ?? "").split(",").filter(Boolean);
    if (dids.length) await toggleMute(muteButton, dids);
    return;
  }

  const button = target.closest<HTMLButtonElement>("[data-volume-step], [data-volume-unify]");
  if (!button || button.disabled) return;
  const dids = (button.dataset.volumeTargets ?? "").split(",").filter(Boolean);
  if (!dids.length) return;
  const relative = button.dataset.volumeStep !== undefined;
  if (!relative && !button.parentElement?.querySelector<HTMLInputElement>("input")?.value) {
    store.showToast("请填写要统一到的音量");
    return;
  }
  const value = relative ? Number(button.dataset.volumeStep) : Number(button.parentElement?.querySelector<HTMLInputElement>("input")?.value);
  if (!Number.isInteger(value) || (!relative && (value < 0 || value > 100))) return;
  button.disabled = true;
  try {
    await setVolume(value, dids, relative);
    store.showToast(relative ? "已同步增减音箱音量" : "音箱音量已统一");
  } catch (error) {
    store.showToast(error instanceof Error ? error.message : "音量调整失败");
  } finally { button.disabled = false; }
});

async function toggleMute(button: HTMLButtonElement, dids: string[]) {
  const state = store.get();
  const muted = new Map<string, boolean>();
  for (const d of state.playback?.devices ?? []) muted.set(d.did, d.muted);
  for (const d of state.devices) if (!muted.has(d.did)) muted.set(d.did, !!d.muted);
  const allMuted = dids.length > 0 && dids.every((did) => muted.get(did) === true);
  const nextMuted = !allMuted;
  const previousMarkup = button.innerHTML;
  const previousLabel = button.getAttribute("aria-label") || "静音";
  button.setAttribute("aria-pressed", String(nextMuted));
  button.setAttribute("aria-label", nextMuted ? "取消静音" : "静音");
  button.title = nextMuted ? "取消静音" : "静音";
  button.innerHTML = icon(nextMuted ? "mute" : "speaker");
  button.disabled = true;
  try {
    await setMute(nextMuted, dids);
    store.showToast(allMuted ? "已取消静音" : "已静音");
  } catch (error) {
    button.setAttribute("aria-pressed", String(allMuted));
    button.setAttribute("aria-label", previousLabel);
    button.title = previousLabel;
    button.innerHTML = previousMarkup;
    store.showToast(error instanceof Error ? error.message : "静音操作失败");
  } finally {
    button.disabled = false;
  }
}
