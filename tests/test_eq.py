"""Per-speaker EQ: config signatures, stream variants, filter chain, topology."""

import pytest
from test_topology import FakeBridge, FakeDeviceManager, _latency, _raop_diag

from micast import topology
from micast.config import (
    EQ_BANDS_HZ,
    EqPoint,
    ReceiverConfig,
    Settings,
    SpeakerConfig,
    SpeakerGroupConfig,
)
from micast.curve_fit import (
    CURVE_FREQ_RANGE,
    NIGHT_ATTENUATION,
    add_curve,
    fit_points,
    format_graphic_eq,
    gain_table,
    is_flat,
    loudness_band,
    loudness_curve,
    normalize_points,
    parse_graphic_eq,
    pchip_eval,
)
from micast.speaker_pipeline import SpeakerPipeline


@pytest.fixture()
def cfg(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    return Settings()


def test_eq_curve_none_when_disabled_or_flat(cfg):
    cfg.speakers = [SpeakerConfig(did="didA")]
    assert cfg.speaker_eq_curve("didA") is None

    cfg.set_speaker_eq_curve("didA", enabled=True, points=[])
    assert cfg.speaker_eq_curve("didA") is None  # flat EQ keeps the base stream

    cfg.set_speaker_eq_curve("didA", enabled=True, points=[(31, 3)])
    assert cfg.speaker_eq_curve("didA") == ((31.0, 3.0),)

    cfg.set_speaker_eq_curve("didA", enabled=False, points=[(31, 3)])
    assert cfg.speaker_eq_curve("didA") is None


def test_eq_points_are_clamped(cfg):
    speaker = cfg.set_speaker_eq_curve("didA", enabled=True, points=[(100, 99), (1000, -99)])
    assert [(p.freq, p.gain_db) for p in speaker.eq_points] == [(100.0, 12.0), (1000.0, -12.0)]


def test_harman_preset_identity_persists(cfg):
    speaker = cfg.set_speaker_eq_curve("didA", enabled=True, points=[(100, 2)], preset="harman")
    assert speaker.eq_preset == "harman"


def test_tuning_revision_and_one_step_undo(cfg):
    speaker = cfg.set_speaker_eq_curve("didA", enabled=True, points=[(100, 3)], preset="bass")
    first_revision = speaker.eq_revision
    cfg.set_speaker_eq_curve("didA", enabled=True, points=[(100, -4)], preset="")
    speaker = cfg.get_speaker("didA")
    assert speaker.eq_revision == first_revision + 1
    assert speaker.eq_undo is not None

    restored = cfg.undo_speaker_tuning("didA")
    assert [(p.freq, p.gain_db) for p in restored.eq_points] == [(100.0, 3.0)]
    assert restored.eq_preset == "bass"
    assert restored.eq_undo is None
    assert restored.eq_revision == first_revision + 2
    with pytest.raises(ValueError, match="没有可撤销"):
        cfg.undo_speaker_tuning("didA")


def test_legacy_ten_band_config_migrates_to_points():
    bands = [3, 0, -2] + [0] * 7
    speaker = SpeakerConfig(did="didA", eq_enabled=True, eq_bands=bands)
    # Zero-gain bands survive as 0 dB anchor points (they shape the curve).
    assert [(p.freq, p.gain_db) for p in speaker.eq_points] == [
        (float(hz), float(g)) for hz, g in zip(EQ_BANDS_HZ, bands, strict=True)
    ]


def test_legacy_five_band_config_migrates_to_nearest_iso_band(cfg):
    """Old 60/250/1k/4k/12k gains land on 62/250/1k/4k/16k, not the first five."""
    speaker = SpeakerConfig(did="didA", eq_enabled=True, eq_bands=[3, 1, -2, 2, -4])
    assert [(p.freq, p.gain_db) for p in speaker.eq_points] == [
        (31.0, 0.0),
        (62.0, 3.0),
        (125.0, 0.0),
        (250.0, 1.0),
        (500.0, 0.0),
        (1000.0, -2.0),
        (2000.0, 0.0),
        (4000.0, 2.0),
        (8000.0, 0.0),
        (16000.0, -4.0),
    ]
    # The deprecated API path migrates the same way for a stale client.
    updated = cfg.set_speaker_eq("didB", enabled=True, bands=[3, 1, -2, 2, -4])
    assert [(p.freq, p.gain_db) for p in updated.eq_points] == [
        (31.0, 0.0),
        (62.0, 3.0),
        (125.0, 0.0),
        (250.0, 1.0),
        (500.0, 0.0),
        (1000.0, -2.0),
        (2000.0, 0.0),
        (4000.0, 2.0),
        (8000.0, 0.0),
        (16000.0, -4.0),
    ]


def test_curve_signature_rounding_shares_streams(cfg):
    """Curves differing only below rounding precision share one split stream."""
    cfg.speakers = [SpeakerConfig(did="didA"), SpeakerConfig(did="didB")]
    cfg.receivers = [ReceiverConfig(id="r1", name="全屋", target_type="group", target_id="g1")]
    cfg.groups = [SpeakerGroupConfig(id="g1", name="全屋", speaker_ids=["didA", "didB"])]
    cfg.set_speaker_eq_curve("didA", enabled=True, points=[(100, 3.001)])
    cfg.set_speaker_eq_curve("didB", enabled=True, points=[(100, 3.004)])
    # Every sink wants the same split, so that is the only stream published:
    # the plain base has no consumer (see needs_plain_base).
    variants = cfg.receiver_stream_variants("r1")
    assert [v["suffix"] for v in variants] == ["-q1"]
    assert cfg.stream_suffix("r1", "didA") == "-q1"
    assert cfg.stream_suffix("r1", "didB") == "-q1"


def test_mirror_variants_split_by_eq_signature(cfg):
    cfg.speakers = [SpeakerConfig(did="didA"), SpeakerConfig(did="didB")]
    cfg.receivers = [ReceiverConfig(id="r1", name="全屋", target_type="group", target_id="g1")]
    cfg.groups = [SpeakerGroupConfig(id="g1", name="全屋", speaker_ids=["didA", "didB"])]

    # No EQ anywhere: a single base stream.
    variants = cfg.receiver_stream_variants("r1")
    assert [v["suffix"] for v in variants] == [""]

    # One speaker with EQ: base stream + one split stream.
    cfg.set_speaker_eq("didB", enabled=True, bands=[4, 2] + [0] * 8, preset="bass")
    variants = cfg.receiver_stream_variants("r1")
    assert [v["suffix"] for v in variants] == ["", "-q1"]
    assert cfg.stream_suffix("r1", "didA") == ""
    assert cfg.stream_suffix("r1", "didB") == "-q1"

    # Same EQ on both: one shared split stream and no base — nothing left
    # resolves to the mix.
    cfg.set_speaker_eq("didA", enabled=True, bands=[4, 2] + [0] * 8, preset="bass")
    variants = cfg.receiver_stream_variants("r1")
    assert [v["suffix"] for v in variants] == ["-q1"]
    assert cfg.stream_suffix("r1", "didA") == "-q1"

    # Different EQs: two split streams in order of first appearance.
    cfg.set_speaker_eq("didA", enabled=True, bands=[0, 0, 3] + [0] * 7, preset="vocal")
    variants = cfg.receiver_stream_variants("r1")
    assert [v["suffix"] for v in variants] == ["-q1", "-q2"]
    assert cfg.stream_suffix("r1", "didA") == "-q1"  # didA appears first
    assert cfg.stream_suffix("r1", "didB") == "-q2"


def test_single_eq_speaker_publishes_only_its_own_variant(cfg):
    """A lone EQ'd speaker must not run a second encoder for nobody.

    Field data (0.3.3): such a receiver's plain base stream broadcast 4 MB
    over 100 s with zero clients — the speaker pulls its -q1 — on a box that
    was already CPU-starved, and its speakers stuttered for the headroom.
    """
    cfg.speakers = [SpeakerConfig(did="didA")]
    cfg.receivers = [ReceiverConfig(id="r1", name="客厅", target_type="speaker", target_id="didA")]
    cfg.set_speaker_eq_curve("didA", enabled=True, points=[(100, 3.0)])

    assert cfg.group_for_receiver("r1") is None
    assert not cfg.needs_plain_base("r1")
    assert [v["suffix"] for v in cfg.receiver_stream_variants("r1")] == ["-q1"]
    assert cfg.stream_suffix("r1", "didA") == "-q1"


def test_resolved_suffix_always_names_a_published_variant(cfg):
    """Every sink resolves to a variant the receiver actually publishes.

    This is the invariant that makes a tightened plain base safe: a URL into a
    stream nobody registered answers 404, and a speaker (plus the codec
    capability prober behind no_stream_pull) reads 404 as "this format is
    unsupported" — permanently.
    """

    def assert_resolved_suffixes_are_published(receiver_id: str) -> set[str]:
        published = {v["suffix"] for v in cfg.receiver_stream_variants(receiver_id)}
        for did in cfg.receiver_targets(receiver_id):
            assert cfg.stream_suffix(receiver_id, did) in published
        return published

    members = ["didA", "didB", "didC", "didD"]
    cfg.speakers = [SpeakerConfig(did=did) for did in members]
    cfg.receivers = [
        ReceiverConfig(id="r1", name="组播", target_type="group", target_id="g1"),
        ReceiverConfig(id="r2", name="全屋", target_type="group", target_id="g2"),
    ]
    cfg.groups = [
        SpeakerGroupConfig(
            id="g1",
            name="组播",
            speaker_ids=members,
            mode="stereo",
            channels={"didA": "left", "didB": "left", "didC": "right", "didD": "right"},
        ),
        SpeakerGroupConfig(id="g2", name="全屋", speaker_ids=members),
    ]
    cfg.set_speaker_eq_curve("didA", enabled=True, points=[(100, 3.0)])
    cfg.set_speaker_loudness("didB", True)
    cfg.set_speaker_eq_curve("didD", enabled=True, points=[(100, 3.0)])

    assert assert_resolved_suffixes_are_published("r1") == {"-L-q1", "-L-q2", "-R", "-R-q1"}
    # The mirror receiver has a channel-less plain member (didC), so its mix
    # stays published.
    assert "" in assert_resolved_suffixes_are_published("r2")

    # Tune the last plain member: the mix loses its only consumer and goes
    # away — and every sink still names a published stream.
    cfg.set_speaker_eq_curve("didC", enabled=True, points=[(50, -2.0)])
    assert "" not in assert_resolved_suffixes_are_published("r2")
    assert not cfg.needs_plain_base("r2")


def test_stream_suffix_falls_back_to_a_published_variant(cfg):
    """A sink whose tuning is not in the plan still gets a stream that exists.

    The left channel here has two curves and no plain variant, so a third
    left-hand curve must land on a published -L stream instead of the bare
    "-L" nobody registered.
    """
    cfg.speakers = [SpeakerConfig(did=did) for did in ("didA", "didB", "didC", "didD")]
    cfg.receivers = [ReceiverConfig(id="r1", name="组播", target_type="group", target_id="g1")]
    cfg.groups = [
        SpeakerGroupConfig(
            id="g1",
            name="组播",
            speaker_ids=["didA", "didB"],
            mode="stereo",
            channels={"didA": "left", "didB": "left", "didC": "right", "didD": "left"},
        )
    ]
    cfg.set_speaker_eq_curve("didA", enabled=True, points=[(100, 3.0)])
    cfg.set_speaker_eq_curve("didB", enabled=True, points=[(100, -3.0)])
    cfg.set_speaker_eq_curve("didC", enabled=True, points=[(500, 2.0)])
    cfg.set_speaker_eq_curve("didD", enabled=True, points=[(200, 5.0)])

    assert [v["suffix"] for v in cfg.receiver_stream_variants("r1")] == ["-L-q1", "-L-q2"]
    assert cfg.stream_suffix("r1", "didD") == "-L-q1"
    assert cfg.stream_id_for("r1", "didD") == "r1-L-q1"
    # The right channel publishes nothing at all, so the bare suffix is all
    # that is left to ask for.
    assert cfg.stream_suffix("r1", "didC") == "-R"


def test_stereo_variants_combine_channel_and_eq(cfg):
    cfg.speakers = [SpeakerConfig(did="didA"), SpeakerConfig(did="didB")]
    cfg.receivers = [ReceiverConfig(id="r1", name="立体声", target_type="group", target_id="g1")]
    cfg.groups = [
        SpeakerGroupConfig(
            id="g1",
            name="立体声",
            speaker_ids=["didA", "didB"],
            mode="stereo",
            channels={"didA": "left", "didB": "right"},
        )
    ]
    variants = cfg.receiver_stream_variants("r1")
    assert [v["suffix"] for v in variants] == ["-L", "-R"]

    cfg.set_speaker_eq("didA", enabled=True, bands=[5] + [0] * 9)
    variants = cfg.receiver_stream_variants("r1")
    # EQ splits the left channel off the plain -L stream; the base mix still
    # has no consumer in a fully channel-split group.
    assert [v["suffix"] for v in variants] == ["-L-q1", "-R"]
    assert cfg.stream_suffix("r1", "didA") == "-L-q1"
    assert cfg.stream_suffix("r1", "didB") == "-R"


def test_pipeline_filter_includes_curve(cfg, monkeypatch):
    monkeypatch.setattr("micast.speaker_pipeline.settings", cfg)
    pipeline = SpeakerPipeline(
        device_id="r1",
        alias="t",
        pcm_source=None,
        stream_server=None,
        eq_curve=[(31.0, 3.0), (125.0, -2.0)],
    )
    filt = pipeline._build_audio_filter()
    assert filt is not None
    names = [name for name, _ in filt]
    if names == ["firequalizer"]:
        args = filt[0][1]
        assert args.startswith("gain_entry='")
        assert args.count("entry(") >= 100  # dense 120-point gain table
    else:
        # equalizer-chain fallback: a few dozen peaking bands subsampled from
        # the dense table, so the raw control-point frequencies are not kept.
        assert set(names) == {"equalizer"}
        assert 0 < len(filt) <= 24
        joined = ",".join(args for _, args in filt)
        assert "g=3.00" in joined  # +3 dB bass end of the curve
        assert "g=-2.00" in joined  # −2 dB treble end of the curve

    flat = SpeakerPipeline(
        device_id="r1",
        alias="t",
        pcm_source=None,
        stream_server=None,
        eq_curve=[],
    )
    assert flat._build_audio_filter() is None


def test_pchip_is_monotone_and_bounded():
    pts = normalize_points([(100, 5), (1000, -5), (10000, 5)])
    table = gain_table(pts, size=200)
    # No overshoot beyond the control-point range (PCHIP property).
    assert all(-5.01 <= g <= 5.01 for _, g in table)
    assert abs(pchip_eval(pts, 1000) + 5) < 1e-6
    # Flat extension beyond the outermost points.
    assert abs(pchip_eval(pts, 20) - 5) < 1e-6
    assert abs(pchip_eval(pts, 20000) - 5) < 1e-6


def test_fit_points_converges_and_stays_in_range():
    import math

    freqs = [20 * 1.122**i for i in range(100)]  # extends past 20kHz on purpose
    gains = [math.sin(i / 10) * 5 for i in range(100)]
    pts = fit_points(freqs, gains, max_points=8)
    assert 2 <= len(pts) <= 8
    assert all(CURVE_FREQ_RANGE[0] <= f <= CURVE_FREQ_RANGE[1] for f, _ in pts)
    # Recovered curve tracks the measured one closely at the knots.
    for f, g in pts:
        assert abs(pchip_eval(pts, f) - g) < 1e-6


def test_graphic_eq_round_trip():
    pts = normalize_points([(31, 4), (250, -1.5), (1000, 0), (8000, 2)])
    text = format_graphic_eq(gain_table(pts))
    back = parse_graphic_eq(text)
    assert not is_flat(back)
    # Dense export thins to the 24-point control-point cap on import, so the
    # recovered curve tracks rather than exactly reproduces the original.
    for f in (31, 250, 1000, 8000):
        assert abs(pchip_eval(back, f) - pchip_eval(pts, f)) < 1.5


def test_topology_shows_eq_stream_variant(cfg, monkeypatch):
    monkeypatch.setattr(topology, "settings", cfg)
    cfg.speakers = [SpeakerConfig(did="didA"), SpeakerConfig(did="didB")]
    cfg.receivers = [ReceiverConfig(id="r1", name="全屋", target_type="group", target_id="g1")]
    cfg.groups = [SpeakerGroupConfig(id="g1", name="全屋", speaker_ids=["didA", "didB"])]
    cfg.set_speaker_eq("didB", enabled=True, bands=[4, 2] + [0] * 8, preset="bass")

    bridge = FakeBridge(
        {
            "raop": _raop_diag(),
            "streams": {
                "r1": {
                    "clients": 1,
                    "flowing": True,
                    "bytes_sent": 0,
                    "dropped_chunks": 0,
                    "latency": _latency(),
                },
                "r1-q1": {
                    "clients": 1,
                    "flowing": True,
                    "bytes_sent": 0,
                    "dropped_chunks": 0,
                    "latency": _latency(),
                },
            },
        }
    )
    snap = topology.build_topology(bridge, FakeDeviceManager(playing=["didA", "didB"]))

    stream_ids = {n["id"] for n in snap["nodes"] if n["kind"] == "stream"}
    assert stream_ids == {"stream:r1", "stream:r1-q1"}
    eq_node = next(n for n in snap["nodes"] if n["id"] == "stream:r1-q1")
    assert eq_node["eq"] is True

    pull = [e for e in snap["edges"] if e.get("direction") == "pull"]
    routes = {(e["from"], e["to"]) for e in pull}
    assert routes == {("stream:r1", "spk:didA"), ("stream:r1-q1", "spk:didB")}


def test_split_stream_id_with_eq_suffix():
    assert topology._split_stream_id("r1") == ("r1", None)
    assert topology._split_stream_id("r1-L") == ("r1", "left")
    assert topology._split_stream_id("r1-q1") == ("r1", None)
    assert topology._split_stream_id("r1-R-q2") == ("r1", "right")


def test_night_mode_layers_bass_shelf_without_eq(cfg):
    cfg.speakers = [SpeakerConfig(did="didA")]
    # Night mode alone (EQ disabled) still produces a non-flat curve so the
    # speaker gets its own split stream and a bass-attenuating filter.
    cfg.set_speaker_night_mode("didA", True)
    curve = cfg.speaker_eq_curve("didA")
    assert curve is not None
    assert any(g < 0 for _, g in curve)

    # Turning it off returns to None (flat + disabled).
    cfg.set_speaker_night_mode("didA", False)
    assert cfg.speaker_eq_curve("didA") is None


def test_night_mode_stacks_on_active_curve(cfg):
    cfg.speakers = [SpeakerConfig(did="didA")]
    cfg.set_speaker_eq_curve("didA", enabled=True, points=[(1000, 0.0)])
    cfg.set_speaker_night_mode("didA", True)
    curve = cfg.speaker_eq_curve("didA")
    assert curve is not None
    # Night shelf is deepest at 20 Hz and eases to 0 dB by ~640 Hz; at 1 kHz the
    # active flat curve is essentially unchanged.
    assert pchip_eval(list(curve), 20) < pchip_eval(list(curve), 1000)


def test_add_curve_sums_pointwise():
    base = normalize_points([(1000, 2.0)])
    merged = add_curve(base, NIGHT_ATTENUATION)
    assert abs(pchip_eval(merged, 1000) - 2.0) < 0.5
    assert pchip_eval(merged, 20) < 1.0


def test_content_profiles_save_switch_and_clear(cfg):
    cfg.speakers = [SpeakerConfig(did="didA")]
    cfg.set_speaker_eq_curve("didA", enabled=True, points=[(100, 3.0), (1000, 0.0)])
    cfg.save_speaker_profile("didA", "movie")

    # A manual edit clears the active-scene label.
    cfg.set_speaker_eq_curve("didA", enabled=True, points=[(100, -2.0)])
    assert cfg.get_speaker("didA").content_profile == ""

    # Switching back restores the saved scene curve and labels it active.
    speaker = cfg.set_speaker_content_profile("didA", "movie")
    assert speaker.content_profile == "movie"
    assert speaker.eq_enabled is True
    assert [(p.freq, p.gain_db) for p in speaker.eq_points] == [(100.0, 3.0), (1000.0, 0.0)]


def test_loudness_curve_fades_with_volume():
    assert loudness_curve(100) == []
    quiet = loudness_curve(20)
    # A low/high shelf: deep bass lift, far less at the mid band.
    assert any(g > 5 for _, g in quiet)
    assert pchip_eval(quiet, 20) > pchip_eval(quiet, 1000)


def test_loudness_band_quantizes_into_11_levels():
    assert loudness_band(0) == 0
    assert loudness_band(100) == 10
    # A nudge inside one band never triggers a filter rebuild.
    assert loudness_band(40) == loudness_band(44)


def test_loudness_splits_stream_with_flat_eq(cfg):
    cfg.speakers = [SpeakerConfig(did="didA"), SpeakerConfig(did="didB")]
    cfg.receivers = [ReceiverConfig(id="r1", name="全屋", target_type="group", target_id="g1")]
    cfg.groups = [SpeakerGroupConfig(id="g1", name="全屋", speaker_ids=["didA", "didB"])]
    # Flat EQ on both, but didB has loudness on: it still gets its own stream.
    cfg.set_speaker_loudness("didB", True)
    variants = cfg.receiver_stream_variants("r1")
    assert [v["suffix"] for v in variants] == ["", "-q1"]
    assert cfg.stream_suffix("r1", "didA") == ""
    assert cfg.stream_suffix("r1", "didB") == "-q1"
    # Turning it off shares the base stream again.
    cfg.set_speaker_loudness("didB", False)
    assert cfg.stream_suffix("r1", "didB") == ""


def test_pipeline_loudness_layers_filter_on_flat_curve(cfg, monkeypatch):
    monkeypatch.setattr("micast.speaker_pipeline.settings", cfg)
    pipeline = SpeakerPipeline(
        device_id="r1",
        alias="t",
        pcm_source=None,
        stream_server=None,
        eq_curve=[],
        loudness=True,
    )
    assert pipeline._build_audio_filter() is None  # 100% → flat, no encoder
    pipeline.set_loudness_level(20)
    assert pipeline._build_audio_filter() is not None


def test_content_profile_requires_save_and_valid_name(cfg):
    cfg.speakers = [SpeakerConfig(did="didA")]
    with pytest.raises(ValueError):
        cfg.set_speaker_content_profile("didA", "movie")  # not saved yet
    with pytest.raises(ValueError):
        cfg.save_speaker_profile("didA", "bogus")

    cfg.save_speaker_profile("didA", "music")
    cfg.delete_speaker_profile("didA", "music")
    assert "music" not in cfg.get_speaker("didA").eq_profiles


def test_curve_library_save_rename_delete(cfg):
    cfg.save_curve("客厅低音", [(100.0, 4.0), (1000.0, -1.0)])
    assert cfg.list_saved_curves() == {"客厅低音": [(100.0, 4.0), (1000.0, -1.0)]}
    with pytest.raises(ValueError):
        cfg.save_curve("客厅低音", [(500.0, 1.0)])  # duplicate name rejected
    cfg.rename_saved_curve("客厅低音", "客厅")
    assert list(cfg.list_saved_curves()) == ["客厅"]
    cfg.delete_saved_curve("客厅")
    assert cfg.list_saved_curves() == {}


def test_curve_library_survives_save_and_load(tmp_path, monkeypatch):
    """The library must round-trip through micast.json — a save_to_file that
    drops saved_curves silently wipes user curves on restart/uninstall."""
    monkeypatch.setenv("MICAST_DATA_DIR", str(tmp_path))
    first = Settings()
    first.save_curve("客厅低音", [(100.0, 4.0), (1000.0, -1.0)])

    second = Settings()
    second.load_from_file()
    assert second.list_saved_curves() == {"客厅低音": [(100.0, 4.0), (1000.0, -1.0)]}
    assert all(isinstance(p, EqPoint) for p in second.saved_curves["客厅低音"])
