"""Touch-screen cover/lyrics: match phone-pushed DAAP track metadata against
Xiaomi's music library and re-issue the play command with the song's audioID,
so screen-equipped speakers show real cover art and scrolling lyrics.

Ported strategy (thanks miair-next): metadata arrives late for some senders
(NetEase), scrolling-lyrics senders push a line every few seconds through the
same channel, and credit lines ("作词…") look exactly like titles. So matching
is event-driven and conservative: strict matching for minm titles, fuzzy
fallback only for titles derived from the artist field, credit lines skipped,
hits cached so flipping back to a previous song costs no search.
"""

import asyncio
import contextlib
import logging
import time

logger = logging.getLogger(__name__)

# Credit lines every sender pushes; never a song title, never search these.
_CREDIT_PREFIXES = ("作词", "作詞", "作曲", "编曲", "編曲", "演唱", "词：", "詞：", "曲：")

_QUICK_PATH_WAIT_SECONDS = 1.0
_WATCH_INTERVAL_SECONDS = 1.0


class LyricsSession:
    """One receiver's cover/lyrics matching task, living as long as its session."""

    def __init__(self, receiver_id: str, server, device_manager, resend):
        """server: RaopServer (daap_meta/daap_events source).
        resend: async callback(hit) re-issuing play with the library match;
        hit is the dict from search_track {audio_id, cover_url, duration}."""
        self.receiver_id = receiver_id
        self.server = server
        self.device_manager = device_manager
        self._resend = resend
        self._task: asyncio.Task | None = None
        self._session_audio_id = ""

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        try:
            # Quick path: metadata often lands within a second of RECORD.
            deadline = time.monotonic() + _QUICK_PATH_WAIT_SECONDS
            while time.monotonic() < deadline:
                meta = self.server.daap_meta
                if meta.get("title"):
                    hit = None
                    for title, fuzzy in self._candidates(meta):
                        hit = await self._search(title, meta, fuzzy=fuzzy)
                        if hit:
                            break
                    if hit:
                        self._session_audio_id = hit["audio_id"]
                        await self._resend(hit)
                        break
                    break  # one strict attempt; the watcher keeps trying
                await asyncio.sleep(0.05)
            await self._watch()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Lyrics session failed for %s", self.receiver_id)

    async def _watch(self) -> None:
        tried: set[tuple[str, str]] = set()  # searched, no hit — don't spam the API
        matched: dict[tuple[str, str], dict] = {}  # title -> hit, for "back to previous"
        # Replay the current event too: it may have arrived during the quick
        # search (or carry a derived title the initial strict lookup missed).
        last_seq = self.server.daap_events[-1][0] - 1 if self.server.daap_events else 0
        while True:
            await asyncio.sleep(_WATCH_INTERVAL_SECONDS)
            events = [e for e in self.server.daap_events if e[0] > last_seq]
            if not events:
                continue
            last_seq = events[-1][0]
            for _, meta in events:
                title = meta.get("title", "")
                if not title:
                    continue
                hit = None
                for cand, fuzzy in self._candidates(meta):
                    key = (cand, meta.get("artist", ""))
                    cached = matched.get(key)
                    if cached:
                        hit = cached
                        break
                    if key in tried:
                        continue
                    tried.add(key)
                    hit = await self._search(cand, meta, fuzzy=fuzzy)
                    if hit:
                        matched[key] = hit
                        break
                if hit and hit["audio_id"] != self._session_audio_id:
                    self._session_audio_id = hit["audio_id"]
                    logger.info("歌词/封面切换: %s -> audioID=%s", title, hit["audio_id"])
                    await self._resend(hit)

    @staticmethod
    def _candidates(meta: dict) -> list[tuple[str, bool]]:
        title = meta.get("title") or ""
        derived = meta.get("derived") or ""
        result = [(title, False)] if title and not title.startswith(_CREDIT_PREFIXES) else []
        if derived and derived != title and not derived.startswith(_CREDIT_PREFIXES):
            result.append((derived, True))
        return result

    async def _search(self, title: str, meta: dict, fuzzy: bool) -> dict | None:
        if not title:
            return None
        try:
            return await asyncio.wait_for(
                self.device_manager.search_track(
                    title, meta.get("artist", ""), fuzzy_fallback=fuzzy
                ),
                timeout=5.0,
            )
        except Exception as exc:
            logger.warning(
                "Track display lookup failed for %s (%s): %s", self.receiver_id, title, exc
            )
            return None
