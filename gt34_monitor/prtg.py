from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable

from .config import ThresholdConfig
from .models import DVBService, ReceiverSample
from .oids import STREAM_TYPE_LABELS
from .transponder import derive_rf_band, transponder_status_parts


def _prtg_safe_text(value: str) -> str:
    result = str(value)

    replacements = {
        "Â°": " deg",
        "Ã‚Â°": " deg",
        "°": " deg",
        "â€“": "-",
        "â€”": "-",
        "–": "-",
        "—": "-",
        "â€™": "'",
        "’": "'",
        "â€œ": '"',
        "â€": '"',
        "“": '"',
        "”": '"',
    }

    for old, new in replacements.items():
        result = result.replace(old, new)

    result = " ".join(result.split())

    return (
        result
        .encode("ascii", errors="replace")
        .decode("ascii")
    )


def _channel(
    name: str,
    value: int | float,
    unit: str = "Custom",
    custom_unit: str | None = None,
    float_value: bool = False,
    limit_min_warning: float | None = None,
    limit_max_warning: float | None = None,
    limit_min_error: float | None = None,
    limit_max_error: float | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "channel": _prtg_safe_text(name),
        "value": value,
        "unit": unit,
    }

    if custom_unit:
        item["customunit"] = custom_unit

    if float_value:
        item["float"] = 1
        item["decimalmode"] = "Auto"

    if (
        limit_min_warning is not None
        or limit_max_warning is not None
        or limit_min_error is not None
        or limit_max_error is not None
    ):
        item["limitmode"] = 1

        if limit_min_warning is not None:
            item["limitminwarning"] = limit_min_warning

        if limit_max_warning is not None:
            item["limitmaxwarning"] = limit_max_warning

        if limit_min_error is not None:
            item["limitminerror"] = limit_min_error

        if limit_max_error is not None:
            item["limitmaxerror"] = limit_max_error

    return item


def _service_channel_name(
    name: str,
    sid: int,
    duplicate_names: set[str],
    prefix: str = "",
) -> str:
    base_name = name.strip() or f"SID {sid}"

    if base_name.casefold() in duplicate_names:
        base_name = f"{base_name} [SID {sid}]"

    p = f"{prefix} " if prefix else ""
    return f"{p}Service {base_name}"


def _transport_operational(sample: ReceiverSample) -> bool:
    """True only when this input has usable carrier/TS evidence."""
    return sample.effective_locked and sample.ts_present is True


def _expected_service_health(
    sample: ReceiverSample,
    sid: int,
) -> int:
    """Root-cause-aware expected-service health for PRTG.

    Carrier/TS failure suppresses child service alarms.  When transport is
    healthy, an expected television service must exist, must not report an
    explicit DVB non-running state, and must expose at least one elementary
    stream/PID.  This makes a WISI "No PIDs available" service fail closed
    without changing the raw DVB running-status semantics stored in history.
    """
    if not _transport_operational(sample):
        return 1

    service = sample.live_services_by_sid.get(sid)

    if service is None:
        return 0

    if not service.operational_ok:
        return 0

    return 1 if int(service.elementary_stream_count or 0) > 0 else 0


def _service_duplicate_names(sample: ReceiverSample) -> set[str]:
    if sample.expected_services:
        names = list(sample.expected_services.values())
    else:
        names = [service.name for service in sample.services]

    counts = Counter(name.casefold().strip() for name in names)
    return {name for name, count in counts.items() if count > 1}


def _operational_channels(
    sample: ReceiverSample,
    thresholds: ThresholdConfig,
    prefix: str = "",
    include_config: bool = True,
) -> list[dict[str, Any]]:
    p = f"{prefix} " if prefix else ""
    results: list[dict[str, Any]] = []

    results.append(
        _channel(
            f"{p}Demod Lock",
            1 if sample.effective_locked else 0,
            "Custom",
            "locked",
        )
    )

    if sample.ts_present is not None:
        results.append(
            _channel(
                f"{p}Transport Stream Present",
                1 if sample.ts_present else 0,
                "Custom",
                "present",
            )
        )

    # Single root-cause alarm for loss of carrier and/or transport. Demod Lock
    # and Transport Stream Present remain visible diagnostics, while this one
    # combined channel is the sole transport-level PRTG error source.
    results.append(
        _channel(
            f"{p}Carrier / TS Health",
            1 if _transport_operational(sample) else 0,
            "Custom",
            "operational",
            limit_min_error=1,
        )
    )

    if sample.ts_bitrate_bps is not None:
        results.append(
            _channel(
                f"{p}TS Bitrate",
                sample.ts_bitrate_bps / 1_000_000.0,
                "Custom",
                "Mbit/s",
                True,
            )
        )

    if sample.rf_level_dbm is not None:
        results.append(
            _channel(
                f"{p}RF Level",
                sample.rf_level_dbm,
                "Custom",
                "dBm",
                True,
                limit_min_warning=thresholds.rf_warning_below_dbm,
                limit_max_warning=thresholds.rf_warning_above_dbm,
                limit_min_error=thresholds.rf_error_below_dbm,
                limit_max_error=thresholds.rf_error_above_dbm,
            )
        )

    if sample.snr_db is not None:
        results.append(
            _channel(
                f"{p}SNR",
                sample.snr_db,
                "Custom",
                "dB",
                True,
                limit_min_warning=thresholds.snr_warning_below_db,
                limit_min_error=thresholds.snr_error_below_db,
            )
        )

    if sample.ber is not None:
        results.append(
            _channel(
                f"{p}BER",
                sample.ber,
                "Custom",
                "BER",
                True,
            )
        )

    if sample.frequency_khz is not None:
        results.append(
            _channel(
                f"{p}Frequency",
                sample.frequency_khz / 1000.0,
                "Custom",
                "MHz",
                True,
            )
        )

    if sample.symbol_rate_bd is not None:
        results.append(
            _channel(
                f"{p}Symbol Rate",
                sample.symbol_rate_bd / 1_000_000.0,
                "Custom",
                "MBd",
                True,
            )
        )

    if include_config:
        if sample.tsid is not None:
            results.append(
                _channel(
                    f"{p}TSID",
                    sample.tsid,
                    "Custom",
                    "ID",
                )
            )

        if sample.nid is not None:
            results.append(
                _channel(
                    f"{p}NID",
                    sample.nid,
                    "Custom",
                    "ID",
                )
            )

        if sample.onid is not None:
            results.append(
                _channel(
                    f"{p}ONID",
                    sample.onid,
                    "Custom",
                    "ID",
                )
            )

        results.append(
            _channel(
                f"{p}Discovered Services",
                sample.discovered_service_count,
                "Custom",
                "services",
            )
        )

        results.append(
            _channel(
                f"{p}Running Services",
                sample.running_service_count,
                "Custom",
                "services",
            )
        )

        if sample.expected_services:
            results.append(
                _channel(
                    f"{p}Missing Services",
                    sample.missing_expected_service_count,
                    "Custom",
                    "services",
                )
            )

            results.append(
                _channel(
                    f"{p}Unexpected Services",
                    sample.unexpected_service_count,
                    "Custom",
                    "services",
                    limit_max_warning=0,
                )
            )

            results.append(
                _channel(
                    f"{p}Service Name Mismatches",
                    sample.service_name_mismatch_count,
                    "Custom",
                    "services",
                    limit_max_warning=0,
                )
            )

        duplicate_names = _service_duplicate_names(sample)

        if sample.expected_services:
            for sid, expected_name in sorted(sample.expected_services.items()):
                results.append(
                    _channel(
                        _service_channel_name(
                            expected_name,
                            sid,
                            duplicate_names,
                            prefix=prefix,
                        ),
                        _expected_service_health(sample, sid),
                        "Custom",
                        "ok",
                        limit_min_error=1,
                    )
                )
        else:
            for service in sorted(sample.services, key=lambda item: item.sid):
                results.append(
                    _channel(
                        _service_channel_name(
                            service.name,
                            service.sid,
                            duplicate_names,
                            prefix=prefix,
                        ),
                        1 if service.operational_ok else 0,
                        "Custom",
                        "ok",
                        limit_min_error=1,
                    )
                )

        if sample.modulation is not None:
            results.append(
                _channel(
                    f"{p}Modulation Enum",
                    sample.modulation,
                    "Custom",
                    "enum",
                )
            )

        if sample.code_rate is not None:
            results.append(
                _channel(
                    f"{p}FEC Enum",
                    sample.code_rate,
                    "Custom",
                    "enum",
                )
            )

        if sample.mis is not None:
            results.append(
                _channel(
                    f"{p}MIS",
                    sample.mis,
                    "Custom",
                    "value",
                )
            )

    return results



def _elementary_stream_channels(
    sample: ReceiverSample,
    prefix: str = "",
) -> list[dict[str, Any]]:
    """
    Build a lean PRTG ES summary while preserving the full per-service
    elementary-stream/PID map in the collector and sensor status text.

    Exported ES summary channels:
    - ES Metadata Unavailable
    - Total Elementary Streams
    - ES Compliance Health
    """
    p = f"{prefix} " if prefix else ""
    services = list(sample.services)

    services_without_es = 0
    total_es = 0
    es_compliance_errors = 0

    for service in services:
        es_count = int(service.elementary_stream_count or 0)
        total_es += es_count

        if es_count <= 0:
            services_without_es += 1
            continue

        video_count = len(
            getattr(service, "video_streams", []) or []
        )
        audio_count = len(
            getattr(service, "audio_streams", []) or []
        )
        other_count = len(
            getattr(service, "other_streams", []) or []
        )

        if video_count == 0 and service.video_pid is not None:
            video_count = 1

        if audio_count == 0 and service.audio_pid is not None:
            audio_count = 1

        known_streams = video_count + audio_count + other_count
        if known_streams > es_count:
            es_compliance_errors += 1

    return [
        _channel(
            f"{p}ES Metadata Unavailable",
            services_without_es,
            "Custom",
            "services",
        ),
        _channel(
            f"{p}Total Elementary Streams",
            total_es,
            "Custom",
            "streams",
        ),
        _channel(
            f"{p}ES Compliance Health",
            1 if es_compliance_errors == 0 else 0,
            "Custom",
            "ok",
            limit_min_error=1,
        ),
    ]


def _service_description(service: DVBService) -> str:
    """
    Compact human-readable per-service DVB/ES summary for the PRTG status text.

    Every ES row exposed by WISI is shown, so multiplexed and non-multiplexed
    services can display all currently available A/V/other PID mappings without
    consuming one PRTG channel per raw PID.
    """
    parts = [
        f"{service.name}",
        f"SID {service.sid}",
        service.status_label.upper(),
    ]

    streams = list(
        getattr(service, "elementary_streams", []) or []
    )

    if streams:
        parts.append(f"ES{len(streams)}")

        stream_parts: list[str] = []

        for stream in streams:
            category = getattr(stream, "category", "other")
            prefix = {
                "video": "V",
                "audio": "A",
                "other": "O",
            }.get(category, "O")

            type_label = getattr(stream, "type_label", None)
            if not type_label:
                type_label = STREAM_TYPE_LABELS.get(
                    getattr(stream, "stream_type", -1),
                    str(getattr(stream, "stream_type", "")),
                )

            item = f"{prefix}{stream.pid}:{type_label}".strip()

            language = getattr(stream, "language", None)
            if language:
                item += f":{language}"

            stream_parts.append(item)

        parts.append(",".join(stream_parts))

        audio_count = sum(
            1
            for stream in streams
            if getattr(stream, "category", "") == "audio"
        )
        parts.append(f"A#{audio_count}")
    else:
        parts.append("ES-N/A")

    descrambling = getattr(
        service,
        "descrambling_status_label",
        "unknown",
    )
    if descrambling != "unknown":
        parts.append(f"CAS {descrambling.upper()}")

    return " ".join(parts)


def _baseline_status_fragments(sample: ReceiverSample) -> list[str]:
    if not sample.expected_services:
        return []

    if not _transport_operational(sample):
        return [
            f"EXPECTED SERVICES {sample.expected_service_count}",
            "SERVICE CHECK SUPPRESSED: CARRIER/TS UNAVAILABLE",
        ]

    fragments = [
        (
            f"EXPECTED {sample.present_expected_service_count}/"
            f"{sample.expected_service_count}"
        )
    ]

    if sample.missing_expected_service_sids:
        missing = ", ".join(
            f"{sample.expected_services[sid]} SID {sid}"
            for sid in sample.missing_expected_service_sids
        )
        fragments.append(f"MISSING: {missing}")

    es_unavailable: list[str] = []
    live = sample.live_services_by_sid
    for sid, expected_name in sorted(sample.expected_services.items()):
        service = live.get(sid)
        if service is None or not service.operational_ok:
            continue
        if int(service.elementary_stream_count or 0) <= 0:
            es_unavailable.append(f"{expected_name} SID {sid}")
    if es_unavailable:
        fragments.append(f"ES/PID UNAVAILABLE: {', '.join(es_unavailable)}")

    if sample.unexpected_services:
        unexpected = ", ".join(
            f"{service.name} SID {service.sid}"
            for service in sample.unexpected_services
        )
        fragments.append(f"UNEXPECTED: {unexpected}")

    if sample.name_mismatch_sids:
        mismatches = ", ".join(
            (
                f"SID {sid} expected '{sample.expected_services[sid]}' "
                f"got '{live[sid].name}'"
            )
            for sid in sample.name_mismatch_sids
        )
        fragments.append(f"NAME CHANGE: {mismatches}")

    return fragments


def _status_text(sample: ReceiverSample) -> str:
    lock = "LOCKED" if sample.effective_locked else "UNLOCKED"
    ts = "TS PRESENT" if sample.ts_present else "TS ABSENT"

    # Observation time comes from the PRTG probe/server system clock.
    # ReceiverSample stores that observation timestamp in UTC.
    # Only the PRTG presentation is converted to Pakistan Standard Time.
    try:
        from datetime import datetime, timedelta, timezone

        pkt = timezone(timedelta(hours=5))
        timestamp = (
            datetime.fromisoformat(
                sample.timestamp_utc.replace("Z", "+00:00")
            )
            .astimezone(pkt)
            .strftime("%d-%b-%Y %H:%M:%S PKT")
        )
    except (AttributeError, TypeError, ValueError):
        timestamp = sample.timestamp_utc or "TIME N/A"

    satellite = (
        sample.satellite_name.strip()
        if sample.satellite_name and sample.satellite_name.strip()
        else "SATELLITE N/A"
    )

    if sample.frequency_khz is not None:
        frequency = f"{sample.frequency_khz / 1000:g} MHz"
    else:
        frequency = "FREQUENCY N/A"

    band, _band_provenance = derive_rf_band(sample.frequency_khz)

    frequency_band = (
        f"{frequency} / {band}"
        if band
        else frequency
    )

    parts = [
        timestamp,
        sample.display_name,
        lock,
        satellite,
        frequency_band,
        ts,
    ]

    # Complete tuner/transponder presentation. Band is explicitly marked as
    # derived from RF frequency; raw WISI values remain preserved in history.
    parts.extend(transponder_status_parts(sample))

    if sample.web_ts_bitrate_bps is not None:
        parts.append(f"BITRATE {sample.web_ts_bitrate_bps / 1_000_000.0:.3f} Mbit/s")

    if sample.snr_db is not None:
        parts.append(f"SNR {sample.snr_db:g} dB")
    else:
        parts.append("SNR N/A")

    if sample.ber is not None:
        parts.append(f"BER {sample.ber:g}")

    if sample.tsid is not None:
        parts.append(f"TSID {sample.tsid}")
    if sample.nid is not None:
        parts.append(f"NID {sample.nid}")
    if sample.onid is not None:
        parts.append(f"ONID {sample.onid}")
    if sample.network_name and sample.network_name.strip():
        parts.append(f'NETWORK "{sample.network_name.strip()}"')

    providers = sorted(
        {
            service.provider.strip()
            for service in sample.services
            if service.provider and service.provider.strip()
        },
        key=str.casefold,
    )
    if providers:
        parts.append(f"PROVIDERS: {', '.join(providers)}")

    parts.append(f"SERVICES {len(sample.services)}")
    parts.extend(_baseline_status_fragments(sample))

    service_summary = "; ".join(
        _service_description(service)
        for service in sample.services
    )
    parts.append(service_summary if service_summary else "NO SERVICES")

    return _prtg_safe_text(" | ".join(parts))


def render_prtg(
    sample: ReceiverSample,
    thresholds: ThresholdConfig,
) -> str:
    results = _operational_channels(
        sample,
        thresholds,
        include_config=True,
    )

    results.extend(
        _elementary_stream_channels(sample)
    )

    return json.dumps(
        {
            "prtg": {
                "result": results,
                "text": _status_text(sample),
            }
        },
        ensure_ascii=True,
    )


def render_prtg_all(
    samples: Iterable[ReceiverSample],
    thresholds: ThresholdConfig,
) -> str:
    sample_list = list(samples)
    results: list[dict[str, Any]] = []

    for sample in sample_list:
        safe_name = _prtg_safe_text(sample.display_name)

        results.extend(
            _operational_channels(
                sample,
                thresholds,
                prefix=safe_name,
                include_config=False,
            )
        )

    locked = sum(
        1
        for sample in sample_list
        if sample.effective_locked
    )

    names = ", ".join(
        _prtg_safe_text(sample.display_name)
        for sample in sample_list
        if sample.effective_locked
    )

    text = (
        f"{locked}/{len(sample_list)} "
        f"inputs operationally locked"
    )

    if names:
        text += f" | {names}"

    return json.dumps(
        {
            "prtg": {
                "result": results,
                "text": _prtg_safe_text(text),
            }
        },
        ensure_ascii=True,
    )


def render_prtg_error(message: str) -> str:
    return json.dumps(
        {
            "prtg": {
                "error": 1,
                "text": _prtg_safe_text(message),
            }
        },
        ensure_ascii=True,
    )
