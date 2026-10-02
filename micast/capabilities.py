"""Protocol capabilities describe controls, not separate playback policies."""

INPUT_CAPABILITIES = {
    "airplay": {"finite": False, "seek": False, "source_pause": False, "metadata": "sender"},
    "airplay2": {"finite": False, "seek": False, "source_pause": False, "metadata": "unavailable"},
    "dlna": {"finite": True, "seek": True, "source_pause": True, "metadata": "didl"},
}
OUTPUT_CAPABILITIES = {
    "xiaomi": {"transport": "http", "volume_readback": True},
    "airplay": {"transport": "rtp", "volume_readback": False},
    "dlna": {"transport": "http", "volume_readback": True},
}


def output_kind(target):
    if target.startswith("airplay:"):
        return "airplay"
    if target.startswith("dlna-target:"):
        return "dlna"
    return "xiaomi"


def playback_capabilities(protocol, output=None):
    return {
        **INPUT_CAPABILITIES.get(protocol, INPUT_CAPABILITIES["airplay"]),
        **OUTPUT_CAPABILITIES.get(output, {}),
        "pause_output": True,
        "eq": True,
        "channels": True,
        "delay": True,
    }
