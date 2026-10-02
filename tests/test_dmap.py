"""DMAP parsing and track_meta extraction."""

import struct

from micast.raop.dmap import parse_dmap, track_meta


def _tag(tag: str, payload: bytes) -> bytes:
    return tag.encode() + struct.pack(">I", len(payload)) + payload


def _dmap(items: dict[str, str]) -> bytes:
    inner = b"".join(_tag(k, v.encode()) for k, v in items.items())
    return _tag("mlit", inner)  # wrapped in a container, like real senders


def test_parse_dmap_flattens_containers():
    tags = parse_dmap(_dmap({"minm": "告白气球", "asar": "周杰伦"}))
    assert tags["minm"] == ["告白气球".encode()]
    assert tags["asar"] == ["周杰伦".encode()]


def test_track_meta_plain():
    meta = track_meta(_dmap({"minm": "告白气球", "asar": "周杰伦", "asal": "周杰伦的床边故事"}))
    assert meta["title"] == "告白气球"
    assert meta["artist"] == "周杰伦"
    assert meta["derived"] == ""
    # Only scrolling-lyrics senders expose a lyric line; plain senders must
    # not put the artist there.
    assert "lyric_line" not in meta


def test_track_meta_strips_artist_suffix():
    # QQ音乐: title carries "歌名 - 歌手"
    meta = track_meta(_dmap({"minm": "青花瓷 - 周杰伦", "asar": "周杰伦"}))
    assert meta["title"] == "青花瓷"


def test_track_meta_strips_artist_prefix():
    # QQ音乐 prefix form: "歌手--歌名"
    meta = track_meta(_dmap({"minm": "周杰伦--告白气球", "asar": "周杰伦"}))
    assert meta["title"] == "告白气球"


def test_track_meta_derives_title_from_artist():
    # Apple Music: minm is a lyrics line, real title hides in asar
    meta = track_meta(_dmap({"minm": "亲爱的 爱上你 从那天起", "asar": "告白气球 · 周杰伦"}))
    assert meta["derived"] == "告白气球"
    # QQ音乐: "歌手--歌名" in asar
    meta = track_meta(_dmap({"minm": "塞纳河畔 左岸的咖啡", "asar": "周杰伦--告白气球"}))
    assert meta["derived"] == "告白气球"


def test_track_meta_truncated_body_no_crash():
    assert track_meta(b"\x00\x01") == {}


def test_track_meta_lyric_line_in_artist_recovers_from_title():
    # NetEase scrolling lyrics park the current line in asar; the real artist
    # survives as the title's suffix ("共您别离 - 张国荣").
    meta = track_meta(_dmap({"minm": "共您别离 - 张国荣", "asar": "人在这一刻分开 再不要对对相相"}))
    assert meta["title"] == "共您别离"
    assert meta["artist"] == "张国荣"
    # The raw line feeds the player's lyric display.
    assert meta["lyric_line"] == "人在这一刻分开 再不要对对相相"


def test_track_meta_lyric_with_punctuation_in_artist():
    meta = track_meta(_dmap({"minm": "风继续吹 - 张国荣", "asar": "过去多少，快乐记忆"}))
    assert meta["title"] == "风继续吹"
    assert meta["artist"] == "张国荣"
    assert meta["lyric_line"] == "过去多少，快乐记忆"


def test_short_lyric_is_not_promoted_to_artist():
    meta = track_meta(_dmap({"minm": "Demo - Singer", "asar": "short line"}))
    assert meta["title"] == "Demo"
    assert meta["artist"] == "Singer"
    assert meta["lyric_line"] == "short line"


def test_title_channel_lyrics_keep_stable_song_identity():
    first = track_meta(_dmap({"minm": "first line", "asar": "Demo · Singer"}))
    second = track_meta(_dmap({"minm": "next line", "asar": "Demo · Singer"}))
    assert first["title"] == second["title"] == "Demo"
    assert first["artist"] == second["artist"] == "Singer"
    assert second["lyric_line"] == "next line"
