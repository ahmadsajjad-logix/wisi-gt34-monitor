from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import ConfiguredInput, ReceiverIndex, ReceiverSample
from .oids import (
    INPUT_NAME_BASE,
    INPUT_SOURCE_BASE,
    METRICS,
    TS_BITRATE_BASE,
    TS_CONNECTION_BASE,
    TS_DIRECTION_IN,
    TS_PAYLOAD_BITRATE_BASE,
    DVB_STREAM_TSID_BASE,
    DVB_STREAM_NID_BASE,
    DVB_STREAM_ONID_BASE,
    DVB_STREAM_NETWORK_NAME_BASE,
    DVB_STREAM_NUM_SERVICES_BASE,
    DVB_SERVICE_NAME_BASE,
    DVB_SERVICE_STATUS_BASE,
    MPEG_ES_TYPE_BASE,
    MPEG_ES_AUDIO_LANGUAGE_BASE,
    VIDEO_STREAM_TYPES,
    AUDIO_STREAM_TYPES,
    as_int,
    input_catalogue_source_pointer_oid,
    metric_oid_for_source,
)

LINE_RE = re.compile(r"enterprises\.(?P<oid>[0-9.]+)\s+=\s+(?:(?P<type>[^:]+):\s*)?(?P<value>.*)$")
OID_VALUE_RE = re.compile(r"(?:SNMPv2-SMI::)?enterprises\.(?P<oid>[0-9.]+)$")


def _value(text: str) -> Any:
    value = text.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    oid_match = OID_VALUE_RE.fullmatch(value)
    if oid_match:
        return "1.3.6.1.4.1." + oid_match.group("oid")
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def parse_walk(path: str | Path) -> dict[str, Any]:
    oid_values: dict[str, Any] = {}
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    for line in text.splitlines():
        match = LINE_RE.search(line)
        if not match:
            continue
        oid = "1.3.6.1.4.1." + match.group("oid")
        oid_values[oid] = _value(match.group("value"))
    return oid_values


def _catalogue_values(oid_values: dict[str, Any], base: str) -> dict[tuple[int, int], Any]:
    result: dict[tuple[int, int], Any] = {}
    for oid, value in oid_values.items():
        if not oid.startswith(base + "."):
            continue
        suffix = oid[len(base) + 1:].split(".")
        if len(suffix) != 2:
            continue
        try:
            result[(int(suffix[0]), int(suffix[1]))] = value
        except ValueError:
            continue
    return result


def discover_inputs_from_walk(oid_values: dict[str, Any]) -> list[ConfiguredInput]:
    names = _catalogue_values(oid_values, INPUT_NAME_BASE)
    sources = _catalogue_values(oid_values, INPUT_SOURCE_BASE)
    rows: list[ConfiguredInput] = []
    for idx in sorted(set(names) & set(sources)):
        source = sources[idx]
        if not isinstance(source, str):
            continue
        rows.append(ConfiguredInput(idx[0], idx[1], str(names[idx]), source))
    return rows


def discover_from_walk(oid_values: dict[str, Any]) -> list[ReceiverIndex]:
    return [ReceiverIndex(row.module, row.channel) for row in discover_inputs_from_walk(oid_values)]


def _ts_state_from_walk(oid_values: dict[str, Any], input_row: ConfiguredInput) -> tuple[
    bool, int | None, int | None, int | None,
    int | None, int | None, int | None, str | None, int | None,
]:
    target_pointer = input_catalogue_source_pointer_oid(input_row.module, input_row.channel)
    for oid, value in oid_values.items():
        if not oid.startswith(TS_CONNECTION_BASE + ".") or value != target_pointer:
            continue
        suffix = oid[len(TS_CONNECTION_BASE) + 1:].split(".")
        if len(suffix) != 3:
            continue
        try:
            module, direction, ts_index = map(int, suffix)
        except ValueError:
            continue
        if module != input_row.module or direction != TS_DIRECTION_IN:
            continue
        return (
            True, ts_index,
            as_int(oid_values.get(f"{TS_BITRATE_BASE}.{module}.{direction}.{ts_index}")),
            as_int(oid_values.get(f"{TS_PAYLOAD_BITRATE_BASE}.{module}.{direction}.{ts_index}")),
            as_int(oid_values.get(f"{DVB_STREAM_TSID_BASE}.{module}.{direction}.{ts_index}")),
            as_int(oid_values.get(f"{DVB_STREAM_NID_BASE}.{module}.{direction}.{ts_index}")),
            as_int(oid_values.get(f"{DVB_STREAM_ONID_BASE}.{module}.{direction}.{ts_index}")),
            None if oid_values.get(f"{DVB_STREAM_NETWORK_NAME_BASE}.{module}.{direction}.{ts_index}") is None
            else str(oid_values.get(f"{DVB_STREAM_NETWORK_NAME_BASE}.{module}.{direction}.{ts_index}")),
            as_int(oid_values.get(f"{DVB_STREAM_NUM_SERVICES_BASE}.{module}.{direction}.{ts_index}")),
        )
    return False, None, None, None, None, None, None, None, None


def _expected_service_from_walk(oid_values: dict[str, Any], input_row: ConfiguredInput, ts_index: int | None, expected: str) -> tuple[bool, int | None, int | None]:
    if ts_index is None:
        return False, None, None
    prefix = DVB_SERVICE_NAME_BASE + "."
    wanted = expected.casefold().strip()
    for oid, value in oid_values.items():
        if not oid.startswith(prefix):
            continue
        suffix = oid[len(prefix):].split(".")
        if len(suffix) != 4:
            continue
        try:
            module, direction, row_ts_index, sid = map(int, suffix)
        except ValueError:
            continue
        if module != input_row.module or direction != TS_DIRECTION_IN or row_ts_index != ts_index:
            continue
        if str(value or "").casefold().strip() != wanted:
            continue
        status = as_int(oid_values.get(f"{DVB_SERVICE_STATUS_BASE}.{module}.{direction}.{row_ts_index}.{sid}"))
        return True, sid, status
    return False, None, None



def _elementary_stream_state_from_walk(
    oid_values: dict[str, Any], input_row: ConfiguredInput, ts_index: int | None, program_number: int | None
) -> tuple[bool | None, int | None, int | None, int | None, int | None, int | None, str | None, int | None]:
    if ts_index is None or program_number is None:
        return False, 0, None, None, None, None, None, 0
    prefix = MPEG_ES_TYPE_BASE + "."
    streams: list[tuple[int, int]] = []
    for oid, value in oid_values.items():
        if not oid.startswith(prefix):
            continue
        suffix = oid[len(prefix):].split(".")
        if len(suffix) != 5:
            continue
        try:
            module, direction, row_ts_index, row_program, pid = map(int, suffix)
        except ValueError:
            continue
        if (module, direction, row_ts_index, row_program) != (
            input_row.module, TS_DIRECTION_IN, ts_index, program_number
        ):
            continue
        stream_type = as_int(value)
        if stream_type is not None:
            streams.append((pid, stream_type))
    if not streams:
        return False, 0, None, None, None, None, None, 0
    streams.sort(key=lambda item: item[0])
    video = next(((pid, typ) for pid, typ in streams if typ in VIDEO_STREAM_TYPES), None)
    audio_streams = [(pid, typ) for pid, typ in streams if typ in AUDIO_STREAM_TYPES]
    audio = audio_streams[0] if audio_streams else None
    language = None
    if audio is not None:
        lang = oid_values.get(
            f"{MPEG_ES_AUDIO_LANGUAGE_BASE}.{input_row.module}.{TS_DIRECTION_IN}.{ts_index}.{program_number}.{audio[0]}"
        )
        if lang not in (None, b"", ""):
            language = str(lang)
    return (
        True, len(streams),
        None if video is None else video[0],
        None if video is None else video[1],
        None if audio is None else audio[0],
        None if audio is None else audio[1],
        language, len(audio_streams),
    )

def input_sample_from_walk(oid_values: dict[str, Any], input_row: ConfiguredInput) -> ReceiverSample:
    values: dict[str, Any] = {}
    for name, metric in METRICS.items():
        oid = metric_oid_for_source(input_row.source_oid, metric)
        values[name] = metric.parser(oid_values.get(oid))
    (
        ts_present, ts_index, ts_bitrate_bps, ts_payload_bitrate_bps,
        tsid, nid, onid, network_name, service_count,
    ) = _ts_state_from_walk(oid_values, input_row)
    expected_service_name = input_row.name
    expected_service_present, expected_service_sid, expected_service_status = _expected_service_from_walk(
        oid_values, input_row, ts_index, expected_service_name
    )
    (
        elementary_streams_present, elementary_stream_count,
        primary_video_pid, primary_video_stream_type,
        primary_audio_pid, primary_audio_stream_type,
        primary_audio_language, audio_stream_count,
    ) = _elementary_stream_state_from_walk(oid_values, input_row, ts_index, expected_service_sid)
    return ReceiverSample.now(
        input_row.module,
        input_row.channel,
        input_name=input_row.name,
        source_family=input_row.family,
        source_oid=input_row.source_oid,
        ts_present=ts_present,
        ts_index=ts_index,
        ts_bitrate_bps=ts_bitrate_bps,
        ts_payload_bitrate_bps=ts_payload_bitrate_bps,
        tsid=tsid,
        nid=nid,
        onid=onid,
        network_name=network_name,
        service_count=service_count,
        expected_service_name=expected_service_name,
        expected_service_present=expected_service_present,
        expected_service_sid=expected_service_sid,
        expected_service_status=expected_service_status,
        elementary_streams_present=elementary_streams_present,
        elementary_stream_count=elementary_stream_count,
        primary_video_pid=primary_video_pid,
        primary_video_stream_type=primary_video_stream_type,
        primary_audio_pid=primary_audio_pid,
        primary_audio_stream_type=primary_audio_stream_type,
        primary_audio_language=primary_audio_language,
        audio_stream_count=audio_stream_count,
        **values,
    )


def sample_from_walk(oid_values: dict[str, Any], receiver: ReceiverIndex) -> ReceiverSample:
    for row in discover_inputs_from_walk(oid_values):
        if row.module == receiver.module and row.channel == receiver.channel:
            return input_sample_from_walk(oid_values, row)
    raise LookupError(f"Configured input {receiver.key} not found in walk")
