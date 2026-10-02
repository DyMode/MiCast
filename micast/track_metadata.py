"""Track metadata subsystem: session-scoped enrichment for the now-playing UI.

The transport layer (``micast.raop.server``) captures whatever the sender
pushes — DAAP tags and artwork bytes — and lives/dies with its receiver
session. This module owns the *enrichment* that outlives a single capture:
the Xiaomi music-library match (audioID, cover URL, duration) that the
lyrics/cover chain produces. ``AudioBridge._now_playing`` composes both
sides into the public per-receiver dict, and the cover endpoint serves
sender artwork bytes first, then redirects to the library cover.

Everything here is keyed by receiver id and dropped with the receiver's
session, so a stale cover from yesterday's cast can never resurface.
"""

from dataclasses import dataclass, field


@dataclass
class TrackEnrichment:
    """Library-match fields attached to one receiver's now-playing entry."""

    audio_id: str = ""
    cover_url: str = ""
    duration: int | None = None
    updated_at: float = 0.0


@dataclass
class TrackMetadataRegistry:
    """Per-receiver enrichment store; the single write/read surface.

    Replaces the bare ``bridge.lyrics_matched`` dict, which carried only the
    audioID. New sources (DLNA albumArtURI, AirPlay 2 metadata) attach here
    as additional collectors, not as new ad-hoc attributes.
    """

    entries: dict[str, TrackEnrichment] = field(default_factory=dict)

    def set_library_match(
        self,
        receiver_id: str,
        *,
        audio_id: str,
        cover_url: str = "",
        duration: int | None = None,
        updated_at: float = 0.0,
    ) -> None:
        entry = self.entries.get(receiver_id)
        if entry is None:
            entry = TrackEnrichment()
            self.entries[receiver_id] = entry
        entry.audio_id = audio_id
        entry.cover_url = cover_url or entry.cover_url
        entry.duration = duration if duration is not None else entry.duration
        entry.updated_at = updated_at

    def enrichment_for(self, receiver_id: str) -> TrackEnrichment:
        return self.entries.get(receiver_id) or TrackEnrichment()

    def drop(self, receiver_id: str) -> None:
        self.entries.pop(receiver_id, None)

    def clear(self) -> None:
        self.entries.clear()
