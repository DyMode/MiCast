import pytest

from micast.config import ReceiverConfig, SpeakerGroupConfig, settings
from micast.dlna import DlnaService
from micast.routes.dlna import _dispatch, _scpd


class FakeDeviceManager:
    def __init__(self):
        self.calls = []
        self._owners = {}

    async def play_stream(self, did, url, owner=None, force=False):
        self.calls.append(("play", did, url, owner, force))
        if owner is not None:
            self._owners[did] = owner
        return True

    async def stop(self, did, owner=None):
        self.calls.append(("pause", did, owner))

    async def stop_playback(self, did):
        self.calls.append(("stop", did))

    async def set_volume(self, did, volume):
        self.calls.append(("volume", did, volume))
        return volume

    def owned_targets(self, receiver_id, owner):
        return [
            did for did in settings.receiver_targets(receiver_id) if self._owners.get(did) == owner
        ]


def configure(monkeypatch):
    monkeypatch.setattr(settings, "sender_volume_mode", "linked")
    monkeypatch.setattr(settings, "default_volume_enabled", False)
    group = SpeakerGroupConfig(id="all", name="全屋", speaker_ids=["a", "b"])
    receivers = [
        ReceiverConfig(id="living", name="客厅", target_type="speaker", target_id="a"),
        ReceiverConfig(id="whole", name="全屋", target_type="group", target_id="all"),
    ]
    monkeypatch.setattr(settings, "receivers", receivers)
    monkeypatch.setattr(settings, "groups", [group])
    monkeypatch.setattr(settings, "dlna_enabled", True)
    monkeypatch.setattr(settings, "sync_groups_enabled", True)


def test_dlna_hides_groups_without_deleting_them(monkeypatch):
    configure(monkeypatch)
    service = DlnaService(FakeDeviceManager())

    assert [item.name for item in service.active_receivers()] == ["客厅", "全屋"]

    monkeypatch.setattr(settings, "sync_groups_enabled", False)
    assert [item.name for item in service.active_receivers()] == ["客厅"]
    assert settings.groups[0].name == "全屋"


def test_dlna_service_descriptions_declare_action_arguments_and_state_variables():
    from xml.etree import ElementTree as ET

    namespace = {"upnp": "urn:schemas-upnp-org:service-1-0"}
    transport = ET.fromstring(_scpd("AVTransport"))
    actions = {
        item.findtext("upnp:name", namespaces=namespace): item
        for item in transport.findall("upnp:actionList/upnp:action", namespace)
    }

    set_uri = actions["SetAVTransportURI"]
    arguments = set_uri.findall("upnp:argumentList/upnp:argument", namespace)
    assert [item.findtext("upnp:name", namespaces=namespace) for item in arguments] == [
        "InstanceID",
        "CurrentURI",
        "CurrentURIMetaData",
    ]
    assert all(item.find("upnp:relatedStateVariable", namespace) is not None for item in arguments)
    assert transport.findall("upnp:serviceStateTable/upnp:stateVariable", namespace)


async def test_dlna_routes_group_media_to_every_speaker(monkeypatch):
    configure(monkeypatch)
    manager = FakeDeviceManager()
    service = DlnaService(manager)

    await service.set_uri("whole", "http://media.local/song.mp3")
    await service.play("whole")

    assert manager.calls == [
        ("play", "a", "http://media.local/song.mp3", "dlna:whole", True),
        ("play", "b", "http://media.local/song.mp3", "dlna:whole", True),
    ]
    assert service.state_for("whole").state == "PLAYING"


async def test_dlna_does_not_report_playing_when_every_speaker_rejects(monkeypatch):
    configure(monkeypatch)
    manager = FakeDeviceManager()

    async def reject(*args, **kwargs):
        return False

    manager.play_stream = reject
    service = DlnaService(manager)
    await service.set_uri("living", "http://media.local/song.mp3")


    with pytest.raises(ValueError, match="No speaker accepted"):
        await service.play("living")
    assert service.state_for("living").state == "STOPPED"


async def test_dlna_soap_transport_and_volume(monkeypatch):
    configure(monkeypatch)
    manager = FakeDeviceManager()
    service = DlnaService(manager)
    body = b"<Envelope><CurrentURI>http://media.local/a.mp3</CurrentURI></Envelope>"

    await _dispatch(service, "living", "AVTransport", "SetAVTransportURI", body)
    await _dispatch(service, "living", "AVTransport", "Play", b"")
    info = await _dispatch(service, "living", "AVTransport", "GetTransportInfo", b"")
    await _dispatch(
        service,
        "living",
        "RenderingControl",
        "SetVolume",
        b"<Envelope><DesiredVolume>63</DesiredVolume></Envelope>",
    )

    assert info["CurrentTransportState"] == "PLAYING"
    assert ("volume", "a", 63) in manager.calls
    assert service.state_for("living").volume == 63


async def test_dlna_accepts_next_track_uri(monkeypatch):
    configure(monkeypatch)
    service = DlnaService(FakeDeviceManager())
    body = b"<Envelope><NextURI>http://media.local/next.mp3</NextURI></Envelope>"

    await _dispatch(service, "living", "AVTransport", "SetNextAVTransportURI", body)

    assert service.state_for("living").next_uri == "http://media.local/next.mp3"


async def test_dlna_explicit_compatibility_play_promotes_queued_next_track(monkeypatch):
    configure(monkeypatch)
    manager = FakeDeviceManager()
    service = DlnaService(manager)
    await service.set_uri("living", "http://media.local/first.mp3")
    await service.play("living")
    await service.set_next_uri("living", "http://media.local/second.mp3")
    await service.play("living", advance_next=True)
    state = service.state_for("living")
    assert state.uri == "http://media.local/second.mp3"
    assert state.next_uri == ""


async def test_dlna_repeated_soap_play_preserves_current_track_and_queue(monkeypatch):
    configure(monkeypatch)
    manager = FakeDeviceManager()
    service = DlnaService(manager)
    await service.set_uri("living", "http://media.local/first.mp3")
    await service.play("living")
    identity = service.state_for("living").session_id
    await service.set_next_uri("living", "http://media.local/second.mp3")
    before = list(manager.calls)
    await _dispatch(service, "living", "AVTransport", "Play", b"")
    assert service.state_for("living").session_id == identity
    assert service.state_for("living").next_uri == "http://media.local/second.mp3"
    assert manager.calls == before
    await _dispatch(service, "living", "AVTransport", "Next", b"")
    assert service.state_for("living").uri == "http://media.local/second.mp3"
    assert "<name>Next</name>" in _scpd("AVTransport")
    await service.stop()


async def test_dlna_seek_without_playing_does_not_acquire_outputs(monkeypatch):
    configure(monkeypatch)
    manager = FakeDeviceManager()
    service = DlnaService(manager)
    await service.set_uri("living", "http://media.local/first.mp3")
    await service.seek("living", 42)
    assert not manager.calls
    assert service.state_for("living").state == "STOPPED"
    await service.play("living")
    assert "ss=42" in manager.calls[-1][2]
    await service.stop()


async def test_dlna_resume_does_not_promote_queued_track(monkeypatch):
    configure(monkeypatch)
    service = DlnaService(FakeDeviceManager())
    await service.set_uri("living", "http://media.local/first.mp3")
    await service.play("living")
    await service.set_next_uri("living", "http://media.local/second.mp3")
    await service.pause("living")
    await service.play("living")
    assert service.state_for("living").uri == "http://media.local/first.mp3"
    assert service.state_for("living").next_uri == "http://media.local/second.mp3"
    await service.sessions.close_all()


async def test_dlna_natural_end_advances_only_after_tail_cleanup(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    configure(monkeypatch)
    clock = [0]
    from micast.playback_sessions import PlaybackSessions

    sessions = PlaybackSessions(lambda: 120, clock=lambda: clock[0])
    service = DlnaService(FakeDeviceManager(), sessions)
    await service.set_uri("living", "http://media.local/first.mp3")
    state = service.state_for("living")
    lease = sessions.begin("dlna:living", "dlna", state.session_id)
    released = AsyncMock()
    sessions.register(lease.token, "media", released, kind="media")
    await service.set_next_uri("living", "http://media.local/second.mp3")
    sessions.quiet(lease.token, "media_finished", grace=5)
    await sessions.tick()
    assert not service._next_tasks
    released.assert_not_awaited()
    clock[0] = 5
    await sessions.tick()
    await asyncio.gather(*list(service._next_tasks.values()))
    assert state.uri == "http://media.local/second.mp3"
    assert state.state == "PLAYING" and not state.next_uri
    released.assert_awaited_once()
    await service.stop()


async def test_dlna_replacement_cancels_automatic_next(monkeypatch):
    configure(monkeypatch)
    service = DlnaService(FakeDeviceManager())
    await service.set_uri("living", "http://media.local/first.mp3")
    state = service.state_for("living")
    lease = service.sessions.begin("dlna:living", "dlna", state.session_id)
    await service.set_next_uri("living", "http://media.local/second.mp3")
    service.sessions.quiet(lease.token, "media_finished", grace=0)
    await service.sessions.tick()
    await service.set_uri("living", "http://media.local/replacement.mp3")
    import asyncio

    await asyncio.gather(*list(service._next_tasks.values()))
    assert state.uri == "http://media.local/replacement.mp3" and state.state == "STOPPED"
    assert not state.next_uri
    await service.stop()


async def test_dlna_stop_cancels_a_queued_automatic_next(monkeypatch):
    configure(monkeypatch)
    service = DlnaService(FakeDeviceManager())
    await service.set_uri("living", "http://media.local/first.mp3")
    state = service.state_for("living")
    lease = service.sessions.begin("dlna:living", "dlna", state.session_id)
    await service.set_next_uri("living", "http://media.local/second.mp3")
    service.sessions.quiet(lease.token, "media_finished", grace=0)
    await service.sessions.tick()
    await service.stop_playback("living")
    import asyncio

    await asyncio.sleep(0)
    assert state.state == "STOPPED"
    assert service.sessions.current("dlna:living") is None
    assert not service._next_tasks
    await service.stop()


async def test_dlna_metadata_is_session_scoped_and_unsupported_fields_are_empty(monkeypatch):
    configure(monkeypatch)
    service = DlnaService(FakeDeviceManager())
    await service.set_uri("living", "http://media.local/first.mp3", '''
        <DIDL-Lite><item><title>First</title><artist>Artist</artist>
        <albumArtURI>http://media.local/art.jpg</albumArtURI>
        <res duration="0:03:02.5">http://media.local/first.mp3</res></item></DIDL-Lite>
    ''')
    lease = service.sessions.begin("dlna:living", "dlna", service.state_for("living").session_id)
    track = service.now_playing()["dlna:living"]
    assert track["title"] == "First" and track["duration"] == 182
    assert track["lyric_lines"] is None and track["audio_id"] is None
    service.state_for("living").metadata = '<item><albumArtURI>http://[invalid</albumArtURI></item>'
    assert service.now_playing()["dlna:living"]["cover"] is None
    await service.sessions.close(lease.token)
    assert not service.now_playing()


def test_dlna_uses_stable_unique_device_ids(monkeypatch):
    configure(monkeypatch)
    service = DlnaService(FakeDeviceManager())

    assert service.uuid_for("living") == service.uuid_for("living")
    assert service.uuid_for("living") != service.uuid_for("whole")



@pytest.mark.parametrize("duration,seconds,wire", [
    ("0:03:02.5", 182, "00:03:02"),
    ("12:00:01", 43201, "12:00:01"),
    ("0:00:00", 0, "00:00:00"),
    ("", None, "00:00:00"),
    ("garbage", None, "00:00:00"),
    ("-1:03:02", None, "00:00:00"),
    ("0:60:00", None, "00:00:00"),
    ("0:00:60", None, "00:00:00"),
    ("0:00:nan", None, "00:00:00"),
    ("0:00:inf", None, "00:00:00"),
])
async def test_dlna_duration_is_shared_by_metadata_queries_and_events(monkeypatch, duration, seconds, wire):
    configure(monkeypatch)
    service = DlnaService(FakeDeviceManager())
    uri = "http://media.local/song.mp3"
    await service.set_uri("living", uri, f'<item><res duration="{duration}">{uri}</res><title>Song</title></item>')
    lease = service.sessions.begin("dlna:living", "dlna", service.state_for("living").session_id)
    track = service.now_playing()["dlna:living"]
    assert track["duration"] == seconds and track["title"] == "Song"
    position = await _dispatch(service, "living", "AVTransport", "GetPositionInfo", b"")
    media = await _dispatch(service, "living", "AVTransport", "GetMediaInfo", b"")
    assert position["TrackDuration"] == media["MediaDuration"] == wire
    event = service.event_snapshot("living", "AVTransport")
    assert event["CurrentTrackDuration"] == event["CurrentMediaDuration"] == wire
    await service.sessions.close(lease.token)


async def test_dlna_duration_selects_current_resource_and_clears_on_replacement(monkeypatch):
    configure(monkeypatch)
    service = DlnaService(FakeDeviceManager())
    await service.set_uri("living", "http://media.local/current.mp3", '''
        <DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">
        <item><res duration="0:01:00">http://media.local/other.mp3</res>
        <res duration="0:03:02.5">http://media.local/current.mp3</res></item></DIDL-Lite>
    ''')
    assert service.duration_time("living") == "00:03:02"
    await service.set_next_uri("living", "http://media.local/next.mp3", '<item><res duration="0:05:00"/></item>')
    assert service.duration_time("living") == "00:03:02"
    await service.next_track("living")
    assert service.duration_time("living") == "00:05:00"
    # Replacement closes the legacy test adapter's owned speaker resource.
    from unittest.mock import AsyncMock
    service.device_manager.stop_playback = AsyncMock()
    await service.set_uri("living", "http://media.local/new.mp3")
    assert service.duration_time("living") == "00:00:00"
    await service.set_uri("living", "http://media.local/bad.mp3", '<item><res duration="0:01:00">')
    assert service.duration_time("living") == "00:00:00"
