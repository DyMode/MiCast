"""DAAP/DMAP metadata parsing (phone → receiver track info via SET_PARAMETER).

Tag semantics follow shairport-sync-metadata-reader: minm=title, asar=artist,
asal=album. Nested container tags are flattened recursively.
"""

import re

_DMAP_CONTAINER_TAGS = ("mdcl", "mlit", "msrv", "mlcl")


def parse_dmap(data: bytes) -> dict[str, list[bytes]]:
    """Parse DAAP TLV into {4-char tag: [payload, ...]}; containers flattened."""
    result: dict[str, list[bytes]] = {}
    _walk(data, result)
    return result


def _walk(data: bytes, out: dict[str, list[bytes]]) -> None:
    pos, n = 0, len(data)
    while pos + 8 <= n:
        tag = data[pos : pos + 4].decode("ascii", errors="replace")
        length = int.from_bytes(data[pos + 4 : pos + 8], "big")
        pos += 8
        if pos + length > n:
            break  # truncated; drop the rest
        payload = data[pos : pos + length]
        pos += length
        if tag in _DMAP_CONTAINER_TAGS:
            _walk(payload, out)
        else:
            out.setdefault(tag, []).append(payload)


def _strip_artist_suffix(title: str, artist: str) -> str:
    """QQ音乐 puts the artist at the end of minm ("青花瓷 - 周杰伦"),
    Apple Music too ("连名带姓 · 季末"); strip it when the suffix and the
    artist field corroborate each other."""
    for sep in (" - ", " · ", "-", "·"):
        idx = title.find(sep)
        if idx <= 0:
            continue
        prefix, suffix = title[:idx].strip(), title[idx + len(sep) :].strip()
        if not prefix or not suffix:
            continue
        if artist:
            parts = re.split(r"--+| — | · |—|·", artist)
            names = {artist, *(p.strip() for p in parts if p.strip())}
            derived = _derive_title_from_artist(artist)
            if derived:
                names.add(derived)
            if any(n and (n in suffix or suffix in n) for n in names):
                return prefix
        else:
            return prefix
    return title


def _strip_artist_prefix(title: str, artist: str) -> str:
    """QQ音乐 prefix form: "周杰伦--告白气球" — strip when the prefix
    corroborates the artist field (which may carry an album tail)."""
    artist = artist.strip()
    if not artist:
        return title
    for sep in ("--", " - ", "—"):
        idx = title.find(sep)
        if idx <= 0:
            continue
        prefix, rest = title[:idx].strip(), title[idx + len(sep) :].strip()
        if not prefix or not rest:
            continue
        if prefix == artist or artist.startswith(prefix):
            return rest
    return title


def _derive_title_from_artist(artist: str) -> str:
    """Some senders never send the real title in minm (only scrolling lyrics
    and credit lines); it hides inside asar: "挚友 · Eric周兴哲",
    "搁浅 — 周杰伦" (title first), QQ音乐 "周杰伦--告白气球" (title last)."""
    for sep in (" · ", " — ", "·", "—"):
        if sep in artist:
            parts = [p.strip() for p in artist.split(sep)]
            if len(parts) == 2 and all(parts):
                return parts[0]
    if "--" in artist:
        prefix, _, suffix = artist.partition("--")
        if prefix.strip() and suffix.strip():
            return suffix.strip()
    return ""


def _looks_like_lyric(text: str) -> bool:
    """Scrolling-lyrics senders (NetEase) put the current lyric LINE into asar.
    Real artist names are short and never carry sentence punctuation."""
    if len(text) > 12:
        return True
    return any(mark in text for mark in "，。！？…、,.!?")


def _split_title_artist(title: str) -> tuple[str, str]:
    """Recover (title, artist) from a "title - artist" sender title."""
    for sep in (" - ", " · ", " — "):
        idx = title.rfind(sep)
        if idx > 0:
            prefix, suffix = title[:idx].strip(), title[idx + len(sep) :].strip()
            if prefix and suffix and len(suffix) <= 12:
                return prefix, suffix
    return title, ""


def track_meta(body: bytes) -> dict[str, str]:
    """Extract {title, artist, album, derived} from a SET_PARAMETER dmap body."""
    tags = parse_dmap(body)

    def first(tag: str) -> str:
        values = tags.get(tag) or []
        return values[0].decode("utf-8", errors="replace").strip() if values else ""

    raw_title = first("minm")
    if not raw_title:
        return {}
    artist = first("asar")
    stripped_title, recovered = _split_title_artist(raw_title)
    # A corroborated title suffix identifies the artist even for short lyric
    # lines without punctuation; sentence-length heuristics alone lose these.
    suffix_lyric = bool(recovered and artist and artist != recovered and not _derive_title_from_artist(artist))
    if artist and not _derive_title_from_artist(artist) and (_looks_like_lyric(artist) or suffix_lyric):
        # Lyric line parked in asar: not an artist. The real one is usually the
        # title's suffix ("共您别离 - 张国荣"), which the strip helpers refuse to
        # cut without a corroborating artist — recover it directly.
        stripped_title, recovered = _split_title_artist(raw_title)
        return {
            "title": stripped_title,
            "artist": recovered,
            "album": first("asal"),
            "derived": "",
            # The raw line itself feeds the player's lyric display.
            "lyric_line": artist,
        }
    title = _strip_artist_prefix(_strip_artist_suffix(raw_title, artist), artist)
    derived = _derive_title_from_artist(artist)
    if derived and derived != title:
        separator = "--" if "--" in artist else ("·" if "·" in artist else "—")
        parts = [part.strip() for part in artist.split(separator)]
        singer = parts[0] if separator == "--" else parts[-1]
        return {"title": derived, "artist": singer, "album": first("asal"), "derived": derived, "lyric_line": raw_title}
    return {
        "title": title,
        "artist": artist,
        "album": first("asal"),
        "derived": _derive_title_from_artist(artist),
    }
