"""HTTP description and SOAP control endpoints for local DLNA renderers."""

from __future__ import annotations

import logging
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from starlette.background import BackgroundTask

from micast.dlna import (
    AV_TRANSPORT,
    CONNECTION_MANAGER,
    DLNA_SINK_PROTOCOLS,
    MEDIA_RENDERER,
    RENDERING_CONTROL,
    DlnaService,
    xml_value,
)
from micast.dlna_events import SubscriptionError

router = APIRouter(prefix="/dlna", tags=["dlna"])
logger = logging.getLogger(__name__)


def install(service: DlnaService) -> APIRouter:
    @router.get("/{receiver_id}/description.xml")
    async def description(receiver_id: str):
        receiver = _receiver(service, receiver_id)
        device_uuid = service.uuid_for(receiver_id)
        base = f"/dlna/{receiver_id}"
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>http://{escape(service.location_for(receiver_id).split("/")[2])}/</URLBase>
  <device>
    <deviceType>{MEDIA_RENDERER}</deviceType>
    <friendlyName>{escape(receiver.name)}</friendlyName>
    <manufacturer>MiCast</manufacturer><manufacturerURL>https://github.com/</manufacturerURL>
    <modelDescription>MiCast local audio bridge</modelDescription>
    <modelName>MiCast DLNA Renderer</modelName><modelNumber>0.1</modelNumber>
    <dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>
    <UDN>uuid:{device_uuid}</UDN>
    <serviceList>
      {_service_xml(AV_TRANSPORT, "AVTransport", base)}
      {_service_xml(RENDERING_CONTROL, "RenderingControl", base)}
      {_service_xml(CONNECTION_MANAGER, "ConnectionManager", base)}
    </serviceList>
  </device>
</root>"""
        return _xml(xml)

    @router.get("/{receiver_id}/{service_name}.xml")
    async def service_description(receiver_id: str, service_name: str):
        _receiver(service, receiver_id)
        if service_name not in {"AVTransport", "RenderingControl", "ConnectionManager"}:
            raise HTTPException(status_code=404, detail="Unknown DLNA service")
        return _xml(_scpd(service_name))

    @router.post("/{receiver_id}/{service_name}/control")
    async def control(receiver_id: str, service_name: str, request: Request):
        _receiver(service, receiver_id)
        # Keep an unauthenticated protocol request from allocating unlimited
        # metadata before XML parsing or session creation.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 1024 * 1024:
                raise HTTPException(status_code=413, detail="DLNA control body too large")
            body.extend(chunk)
        body = bytes(body)
        soap_action = request.headers.get("soapaction", "").strip('"').rsplit("#", 1)[-1]
        logger.info("DLNA %s: %s#%s", receiver_id, service_name, soap_action)
        try:
            values = await _dispatch(
                service, receiver_id, service_name, soap_action, body,
                play_advances_next=request.headers.get("x-micast-play-next") == "1",
            )
        except ValueError as exc:
            logger.warning("DLNA %s action failed: %s", receiver_id, exc)
            return _soap_fault(701, str(exc))
        except Exception:
            logger.exception(
                "DLNA %s action crashed: %s#%s",
                receiver_id,
                service_name,
                soap_action,
            )
            return _soap_fault(501, "Playback command failed")
        return _soap_response(_service_type(service_name), soap_action, values)

    @router.api_route(
        "/{receiver_id}/{service_name}/event",
        methods=["SUBSCRIBE"],
        operation_id="dlna_event_subscription",
    )
    @router.api_route(
        "/{receiver_id}/{service_name}/event", methods=["UNSUBSCRIBE"],
        operation_id="dlna_event_unsubscription",
    )
    async def event_subscription(receiver_id: str, service_name: str, request: Request):
        _receiver(service, receiver_id)
        try:
            if request.method == "UNSUBSCRIBE":
                await service.events.unsubscribe(
                    receiver_id, service_name, request.headers,
                    peer=request.client.host if request.client else None,
                )
                return Response(status_code=200)
            subscription, seconds = await service.events.subscribe_from_peer(
                receiver_id, service_name, request.headers,
                request.client.host if request.client else None,
            )
        except SubscriptionError as exc:
            return Response(status_code=exc.status)
        return Response(
            status_code=200, headers={"SID": subscription.sid, "TIMEOUT": f"Second-{seconds}"},
            background=BackgroundTask(service.events.start, subscription),
        )

    return router


def _receiver(service: DlnaService, receiver_id: str):
    receiver = service.receiver(receiver_id)
    if receiver is None:
        raise HTTPException(status_code=404, detail="DLNA renderer not found")
    return receiver


async def _dispatch(
    service: DlnaService, receiver_id: str, service_name: str, action: str, body: bytes,
    *, play_advances_next: bool = False,
) -> dict[str, str | int]:
    state = service.state_for(receiver_id)
    if service_name == "AVTransport":
        if action == "SetAVTransportURI":
            await service.set_uri(
                receiver_id,
                xml_value(body, "CurrentURI"),
                xml_value(body, "CurrentURIMetaData"),
            )
            return {}
        if action == "SetNextAVTransportURI":
            await service.set_next_uri(
                receiver_id,
                xml_value(body, "NextURI"),
                xml_value(body, "NextURIMetaData"),
            )
            return {}
        if action == "Play":
            await service.play(receiver_id, advance_next=play_advances_next)
            return {}
        if action == "Next":
            await service.next_track(receiver_id)
            return {}
        if action == "Pause":
            await service.pause(receiver_id)
            return {}
        if action == "Stop":
            await service.stop_playback(receiver_id)
            return {}
        if action == "Seek":
            unit = xml_value(body, "Unit")
            if unit != "REL_TIME":
                raise ValueError("Only REL_TIME seek is supported")
            await service.seek(receiver_id, _parse_rel_time(xml_value(body, "Target")))
            return {}
        if action == "GetTransportInfo":
            return {
                "CurrentTransportState": state.state,
                "CurrentTransportStatus": "ERROR_OCCURRED" if state.error else "OK",
                "CurrentSpeed": "1",
            }
        if action == "GetPositionInfo":
            seconds = (
                int(service.media.position(receiver_id) or 0) if service.media is not None else 0
            )
            position = f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
            return {
                "Track": 1,
                "TrackDuration": service.duration_time(receiver_id),
                "TrackMetaData": state.metadata,
                "TrackURI": state.uri,
                "RelTime": position,
                "AbsTime": position,
                "RelCount": 0,
                "AbsCount": 0,
            }
        if action == "GetMediaInfo":
            return {
                "NrTracks": 1,
                "MediaDuration": service.duration_time(receiver_id),
                "CurrentURI": state.uri,
                "CurrentURIMetaData": state.metadata,
                "NextURI": state.next_uri,
                "NextURIMetaData": state.next_metadata,
                "PlayMedium": "NETWORK",
                "RecordMedium": "NOT_IMPLEMENTED",
                "WriteStatus": "NOT_IMPLEMENTED",
            }
    elif service_name == "RenderingControl":
        if action == "SetVolume":
            volume = max(0, min(100, int(xml_value(body, "DesiredVolume", "50"))))
            await service.set_volume(receiver_id, volume)
            return {}
        if action == "GetVolume":
            return {"CurrentVolume": await service.get_volume(receiver_id)}
        if action == "SetMute":
            muted = xml_value(body, "DesiredMute", "0") in {"1", "true"}
            await service.set_mute(receiver_id, muted)
            return {}
        if action == "GetMute":
            return {"CurrentMute": int(state.muted)}
    elif service_name == "ConnectionManager":
        if action == "GetProtocolInfo":
            return {
                "Source": "",
                "Sink": DLNA_SINK_PROTOCOLS,
            }
        if action == "GetCurrentConnectionIDs":
            return {"ConnectionIDs": "0"}
        if action == "GetCurrentConnectionInfo":
            return {
                "RcsID": 0,
                "AVTransportID": 0,
                "ProtocolInfo": "",
                "PeerConnectionManager": "",
                "PeerConnectionID": -1,
                "Direction": "Input",
                "Status": "OK",
            }
    raise ValueError(f"Unsupported action: {service_name}#{action}")


def _service_type(service_name: str) -> str:
    return {
        "AVTransport": AV_TRANSPORT,
        "RenderingControl": RENDERING_CONTROL,
        "ConnectionManager": CONNECTION_MANAGER,
    }[service_name]


def _parse_rel_time(target: str) -> float:
    """DLNA REL_TIME target ("HH:MM:SS" or "HH:MM:SS.mmm") → seconds."""
    parts = target.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"Bad REL_TIME target: {target!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    seconds = float(parts[2])
    return hours * 3600 + minutes * 60 + seconds


def _service_xml(service_type: str, service_name: str, base: str) -> str:
    return f"""<service><serviceType>{service_type}</serviceType>
<serviceId>urn:upnp-org:serviceId:{service_name}</serviceId>
<SCPDURL>{base}/{service_name}.xml</SCPDURL>
<controlURL>{base}/{service_name}/control</controlURL>
<eventSubURL>{base}/{service_name}/event</eventSubURL></service>"""


def _scpd(service_name: str) -> str:
    action_specs, state_variables = _scpd_spec(service_name)
    if service_name in {"AVTransport", "RenderingControl"}:
        state_variables = [*state_variables, _state_variable("LastChange", "string", evented=True)]
    action_xml = "".join(_action_xml(name, arguments) for name, arguments in action_specs)
    state_xml = "".join(state_variables)
    return f"""<?xml version="1.0"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<actionList>{action_xml}</actionList><serviceStateTable>{state_xml}</serviceStateTable></scpd>"""


def _action_xml(name: str, arguments: list[tuple[str, str, str]]) -> str:
    argument_xml = "".join(
        f"<argument><name>{arg_name}</name><direction>{direction}</direction>"
        f"<relatedStateVariable>{variable}</relatedStateVariable></argument>"
        for arg_name, direction, variable in arguments
    )
    return f"<action><name>{name}</name><argumentList>{argument_xml}</argumentList></action>"


def _state_variable(
    name: str, data_type: str, allowed: tuple[str, ...] = (), *, evented=False
) -> str:
    allowed_xml = ""
    if allowed:
        allowed_xml = (
            "<allowedValueList>"
            + "".join(f"<allowedValue>{value}</allowedValue>" for value in allowed)
            + "</allowedValueList>"
        )
    return (
        f'<stateVariable sendEvents="{"yes" if evented else "no"}"><name>{name}</name>'
        f"<dataType>{data_type}</dataType>{allowed_xml}</stateVariable>"
    )


def _scpd_spec(
    service_name: str,
) -> tuple[list[tuple[str, list[tuple[str, str, str]]]], list[str]]:
    instance = ("InstanceID", "in", "A_ARG_TYPE_InstanceID")
    channel = ("Channel", "in", "A_ARG_TYPE_Channel")
    if service_name == "AVTransport":
        actions = [
            (
                "SetAVTransportURI",
                [
                    instance,
                    ("CurrentURI", "in", "AVTransportURI"),
                    ("CurrentURIMetaData", "in", "AVTransportURIMetaData"),
                ],
            ),
            (
                "SetNextAVTransportURI",
                [
                    instance,
                    ("NextURI", "in", "NextAVTransportURI"),
                    ("NextURIMetaData", "in", "NextAVTransportURIMetaData"),
                ],
            ),
            ("Play", [instance, ("Speed", "in", "TransportPlaySpeed")]),
            ("Next", [instance]),
            ("Pause", [instance]),
            ("Stop", [instance]),
            (
                "Seek",
                [
                    instance,
                    ("Unit", "in", "A_ARG_TYPE_SeekMode"),
                    ("Target", "in", "A_ARG_TYPE_SeekTarget"),
                ],
            ),
            (
                "GetTransportInfo",
                [
                    instance,
                    ("CurrentTransportState", "out", "TransportState"),
                    ("CurrentTransportStatus", "out", "TransportStatus"),
                    ("CurrentSpeed", "out", "TransportPlaySpeed"),
                ],
            ),
            (
                "GetPositionInfo",
                [
                    instance,
                    ("Track", "out", "CurrentTrack"),
                    ("TrackDuration", "out", "CurrentTrackDuration"),
                    ("TrackMetaData", "out", "CurrentTrackMetaData"),
                    ("TrackURI", "out", "AVTransportURI"),
                    ("RelTime", "out", "RelativeTimePosition"),
                    ("AbsTime", "out", "AbsoluteTimePosition"),
                    ("RelCount", "out", "RelativeCounterPosition"),
                    ("AbsCount", "out", "AbsoluteCounterPosition"),
                ],
            ),
            (
                "GetMediaInfo",
                [
                    instance,
                    ("NrTracks", "out", "NumberOfTracks"),
                    ("MediaDuration", "out", "CurrentMediaDuration"),
                    ("CurrentURI", "out", "AVTransportURI"),
                    ("CurrentURIMetaData", "out", "AVTransportURIMetaData"),
                    ("NextURI", "out", "NextAVTransportURI"),
                    ("NextURIMetaData", "out", "NextAVTransportURIMetaData"),
                    ("PlayMedium", "out", "PlaybackStorageMedium"),
                    ("RecordMedium", "out", "RecordStorageMedium"),
                    ("WriteStatus", "out", "RecordMediumWriteStatus"),
                ],
            ),
        ]
        variables = [
            _state_variable("A_ARG_TYPE_InstanceID", "ui4"),
            _state_variable("AVTransportURI", "uri"),
            _state_variable("AVTransportURIMetaData", "string"),
            _state_variable("NextAVTransportURI", "uri"),
            _state_variable("NextAVTransportURIMetaData", "string"),
            _state_variable("TransportPlaySpeed", "string", ("1",)),
            _state_variable(
                "TransportState",
                "string",
                ("STOPPED", "PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING", "NO_MEDIA_PRESENT"),
            ),
            _state_variable("TransportStatus", "string", ("OK", "ERROR_OCCURRED")),
            _state_variable("A_ARG_TYPE_SeekMode", "string", ("REL_TIME",)),
            _state_variable("A_ARG_TYPE_SeekTarget", "string"),
            _state_variable("CurrentTrack", "ui4"),
            _state_variable("CurrentTrackDuration", "string"),
            _state_variable("CurrentTrackMetaData", "string"),
            _state_variable("RelativeTimePosition", "string"),
            _state_variable("AbsoluteTimePosition", "string"),
            _state_variable("RelativeCounterPosition", "i4"),
            _state_variable("AbsoluteCounterPosition", "i4"),
            _state_variable("NumberOfTracks", "ui4"),
            _state_variable("CurrentMediaDuration", "string"),
            _state_variable("PlaybackStorageMedium", "string", ("NETWORK", "NOT_IMPLEMENTED")),
            _state_variable("RecordStorageMedium", "string", ("NOT_IMPLEMENTED",)),
            _state_variable("RecordMediumWriteStatus", "string", ("NOT_IMPLEMENTED",)),
        ]
        return actions, variables
    if service_name == "RenderingControl":
        actions = [
            ("SetVolume", [instance, channel, ("DesiredVolume", "in", "Volume")]),
            ("GetVolume", [instance, channel, ("CurrentVolume", "out", "Volume")]),
            ("SetMute", [instance, channel, ("DesiredMute", "in", "Mute")]),
            ("GetMute", [instance, channel, ("CurrentMute", "out", "Mute")]),
        ]
        return actions, [
            _state_variable("A_ARG_TYPE_InstanceID", "ui4"),
            _state_variable("A_ARG_TYPE_Channel", "string", ("Master",)),
            _state_variable("Volume", "ui2"),
            _state_variable("Mute", "boolean"),
        ]
    if service_name == "ConnectionManager":
        actions = [
            (
                "GetProtocolInfo",
                [("Source", "out", "SourceProtocolInfo"), ("Sink", "out", "SinkProtocolInfo")],
            ),
            ("GetCurrentConnectionIDs", [("ConnectionIDs", "out", "CurrentConnectionIDs")]),
            (
                "GetCurrentConnectionInfo",
                [
                    ("ConnectionID", "in", "A_ARG_TYPE_ConnectionID"),
                    ("RcsID", "out", "A_ARG_TYPE_RcsID"),
                    ("AVTransportID", "out", "A_ARG_TYPE_AVTransportID"),
                    ("ProtocolInfo", "out", "A_ARG_TYPE_ProtocolInfo"),
                    ("PeerConnectionManager", "out", "A_ARG_TYPE_ConnectionManager"),
                    ("PeerConnectionID", "out", "A_ARG_TYPE_ConnectionID"),
                    ("Direction", "out", "A_ARG_TYPE_Direction"),
                    ("Status", "out", "A_ARG_TYPE_ConnectionStatus"),
                ],
            ),
        ]
        return actions, [
            _state_variable("SourceProtocolInfo", "string", evented=True),
            _state_variable("SinkProtocolInfo", "string", evented=True),
            _state_variable("CurrentConnectionIDs", "string", evented=True),
            _state_variable("A_ARG_TYPE_ConnectionID", "i4"),
            _state_variable("A_ARG_TYPE_RcsID", "i4"),
            _state_variable("A_ARG_TYPE_AVTransportID", "i4"),
            _state_variable("A_ARG_TYPE_ProtocolInfo", "string"),
            _state_variable("A_ARG_TYPE_ConnectionManager", "string"),
            _state_variable("A_ARG_TYPE_Direction", "string", ("Input", "Output")),
            _state_variable(
                "A_ARG_TYPE_ConnectionStatus",
                "string",
                (
                    "OK",
                    "ContentFormatMismatch",
                    "InsufficientBandwidth",
                    "UnreliableChannel",
                    "Unknown",
                ),
            ),
        ]
    raise ValueError(f"Unknown DLNA service: {service_name}")


def _soap_response(service_type: str, action: str, values: dict[str, str | int]) -> Response:
    fields = "".join(f"<{key}>{escape(str(value))}</{key}>" for key, value in values.items())
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action}Response xmlns:u="{service_type}">'
        f"{fields}</u:{action}Response></s:Body></s:Envelope>"
    )
    return _xml(body)


def _soap_fault(code: int, description: str) -> Response:
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body><s:Fault><faultcode>s:Client</faultcode>"
        "<faultstring>UPnPError</faultstring><detail>"
        '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{code}</errorCode>"
        f"<errorDescription>{escape(description)}</errorDescription>"
        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
    )
    return Response(body, status_code=500, media_type='text/xml; charset="utf-8"')


def _xml(body: str) -> Response:
    return Response(body, media_type='text/xml; charset="utf-8"')
