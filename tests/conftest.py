"""Selectable suites; the default pytest command still runs every test."""

from pathlib import Path

# These suites exercise real codec output or timed audio delivery. Keep cheap
# planning, ownership and protocol regressions in the default development suite.
_AUDIO_FILES = {
    "test_audio_encoder.py",
    "test_encoder_coalescing.py",
    "test_flac_delay_line_loss.py",
    "test_flac_keepalive.py",
    "test_room_measure.py",
    "test_source_stall.py",
}
_AUDIO_TESTS = {
    "test_stop_from_own_aux_task_completes_without_recursion",
    "test_restart_source_recovers_pipeline",
}
_PACKAGING_FILES = {"test_packaging.py", "test_fnos_packaging.py"}


def pytest_collection_modifyitems(items):
    import pytest

    for item in items:
        filename = Path(str(item.path)).name
        if filename in _AUDIO_FILES or item.originalname in _AUDIO_TESTS:
            item.add_marker(pytest.mark.audio)
        if filename in _PACKAGING_FILES:
            item.add_marker(pytest.mark.packaging)
