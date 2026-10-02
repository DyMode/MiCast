"""Playback registry with a controllable clock and isolated device manager."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from micast.playback_sessions import PlaybackSessions
from micast.xiaomi.device_manager import DeviceManager


def registry(timeout=60):
    clock = [1000.0]
    return PlaybackSessions(lambda: timeout, lambda: clock[0]), clock


def device_manager(sessions):
    manager = DeviceManager(SimpleNamespace(subscribe_expiry=lambda callback: None))
    manager.sessions = sessions
    manager.refresh_service = AsyncMock(return_value=True)
    manager.cloud_degraded = lambda: False
    manager._start_watchdog = Mock()
    manager._save_codec_capabilities = Mock()
    api = SimpleNamespace(play_music_url=AsyncMock(), play_url=AsyncMock(),
                          pause=AsyncMock(), stop=AsyncMock(), get_status=AsyncMock())
    manager.cloud_api = lambda did: api
    manager._service = object()
    return manager, api
