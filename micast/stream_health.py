"""Stream activity checks independent of application globals."""

import re


def stream_active_for(bridge, manager, device_id):
    url = manager.stream_url_of(device_id) or ""
    match = re.search(r"/stream/([^/?]+)", url)
    if not match:
        return True
    stream_id = match.group(1)
    server = bridge.stream_server
    if server.client_count(stream_id) == 0:
        return False
    return server.is_flowing(stream_id, window=10.0) or not bridge.stream_starved(stream_id)
