import { api } from "./api";
import { primaryTrack } from "./components/playback-bar";
import { store } from "./state";

// Phone lock-screen / system-media integration. Only meaningful while a cast
// is live AND track metadata exists — with no track there is nothing to show,
// matching the player's hide-instead-of-placeholder rule.
let handlersInstalled = false;

function installHandlers(): void {
  if (handlersInstalled) return;
  handlersInstalled = true;
  const mediaSession = navigator.mediaSession;
  mediaSession.setActionHandler("play", () => {
    void api.play().catch(() => undefined);
  });
  mediaSession.setActionHandler("pause", () => {
    void api.pause().catch(() => undefined);
  });
  mediaSession.setActionHandler("stop", () => {
    void api.stopPlayback().catch(() => undefined);
  });
}

export function updateMediaSession(): void {
  if (!("mediaSession" in navigator)) return;
  const playback = store.get().playback;
  const active =
    Boolean(playback?.devices.length) && Boolean(playback?.playing || playback?.paused);
  const track = primaryTrack(store.get().status);
  if (!active || !track) {
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
    return;
  }
  const artwork = track.cover
    ? [{ src: new URL(track.cover.url, document.baseURI).href, sizes: "512x512" }]
    : [];
  navigator.mediaSession.metadata = new MediaMetadata({
    title: track.title ?? "",
    artist: track.artist ?? "",
    album: track.album ?? "",
    artwork,
  });
  navigator.mediaSession.playbackState = playback!.playing ? "playing" : "paused";
  installHandlers();
}
