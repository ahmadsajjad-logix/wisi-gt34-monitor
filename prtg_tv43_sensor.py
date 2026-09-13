from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import time
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

RETURN_CHANNEL_MANIFEST = (
    ROOT / "prtg_tv43_deployment" / "prtg_return_channels_v113d.json"
)


def load_return_channel_names(
    host: str,
    module: int,
    channel_id: int,
) -> list[str] | None:
    """
    Return the frozen channel names that this existing PRTG sensor must emit.

    This is the key stability rule: the V11.3J installer freezes one <=50-channel
    return schema per existing sensor. Registered legacy channels outside that
    schema have their PRTG limits disabled so they cannot create false alarms.
    """
    if not RETURN_CHANNEL_MANIFEST.exists():
        return None

    data = json.loads(
        RETURN_CHANNEL_MANIFEST.read_text(encoding="utf-8-sig")
    )
    key = f"{host}|M{module}C{channel_id}"
    row = data.get(key)
    if not row:
        return None

    names = [
        str(name)
        for name in row.get("channel_names", [])
        if str(name).strip()
    ]
    return names or None
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gt34_monitor.config import load_config
from gt34_monitor.snmp import SnmpV2cClient
from gt34_monitor.fastpath import FastGT34Poller, enrich_sample_from_web_fast
from tv43_history import persist_tv43_history
from gt34_monitor.oids import (
    POLARISATION_LABELS,
    MODULATION_LABELS,
    CODE_RATE_LABELS,
    PLS_MODE_LABELS,
    LNB_VOLTAGE_LABELS,
    TONE_LABELS,
)


AUTHORITATIVE_SERVICES = (
    ROOT
    / "authoritative_inventory_v6"
    / "active_services.csv"
)


def safe_text(value: Any) -> str:
    return (
        " ".join(str(value).split())
        .encode("ascii", errors="replace")
        .decode("ascii")
    )


def channel(
    name: str,
    value: int | float,
    customunit: str,
    *,
    is_float: bool = False,
    limit_min_error: float | None = None,
    limit_max_error: float | None = None,
    limit_max_warning: float | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "channel": safe_text(name),
        "value": value,
        "unit": "Custom",
        "customunit": customunit,
    }

    if is_float:
        item["float"] = 1
        item["decimalmode"] = "Auto"

    if (
        limit_min_error is not None
        or limit_max_error is not None
        or limit_max_warning is not None
    ):
        item["limitmode"] = 1

    if limit_min_error is not None:
        item["limitminerror"] = limit_min_error

    if limit_max_error is not None:
        item["limitmaxerror"] = limit_max_error

    if limit_max_warning is not None:
        item["limitmaxwarning"] = limit_max_warning

    return item


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def service_has_video(service: Any) -> bool:
    return getattr(service, "video_pid", None) is not None


def service_has_es_metadata(service: Any) -> bool:
    es_count = getattr(service, "elementary_stream_count", None)
    if es_count not in (None, 0):
        return True
    if getattr(service, "video_pid", None) is not None:
        return True
    if getattr(service, "audio_pid", None) is not None:
        return True
    return False




def enum_label(mapping: dict[int, str], value: Any, default: str = "N/A") -> str:
    try:
        if value is None:
            return default
        return mapping.get(int(value), str(value))
    except Exception:
        return default


def int_or_unknown(value: Any) -> int:
    try:
        if value is None:
            return -1
        return int(value)
    except Exception:
        return -1


def service_health(service: Any | None) -> int:
    """
    PRTG service-health semantics for the final authoritative TV43 set.

    A service is healthy only when:
    - the authoritative SID is currently present; and
    - the GT34/web enrichment exposes elementary-stream metadata for it.

    This keeps Geo ME/Khyber ME HD visibly down without claiming black/frozen
    video. It is specifically an ES/PID-integrity finding.
    """
    if service is None:
        return 0
    return 1 if service_has_es_metadata(service) else 0


def total_elementary_streams(services: list[Any]) -> int:
    total = 0
    for service in services:
        try:
            total += int(getattr(service, "elementary_stream_count", 0) or 0)
        except Exception:
            pass
    return total


def running_service_count(services: list[Any]) -> int:
    total = 0
    for service in services:
        try:
            if bool(getattr(service, "running", False)):
                total += 1
        except Exception:
            pass
    return total


def service_detail(service: Any) -> str:
    sid = getattr(service, "sid", "?")
    name = safe_text(getattr(service, "name", "") or f"SID {sid}")
    status = safe_text(getattr(service, "status_label", "UNKNOWN")).upper()

    streams = list(getattr(service, "elementary_streams", []) or [])
    if not streams:
        return f"{name} SID {sid} {status} ES-N/A"

    parts: list[str] = []
    for stream in streams:
        category = str(getattr(stream, "category", "other"))
        prefix = {"video": "V", "audio": "A", "other": "O"}.get(category, "O")
        pid = getattr(stream, "pid", "?")
        label = safe_text(
            getattr(stream, "type_label", None)
            or getattr(stream, "stream_type_name", None)
            or getattr(stream, "stream_type", "")
        )
        language = safe_text(getattr(stream, "language", "") or "")
        item = f"{prefix}{pid}:{label}"
        if language:
            item += f"/{language}"
        parts.append(item)

    return f"{name} SID {sid} {status} ES{len(streams)} " + ",".join(parts)


def load_expected_services(
    host: str,
    module: int,
    channel_id: int,
) -> dict[int, str]:
    """
    Frozen authoritative baseline produced by Inventory V6.

    It is keyed by host + logical module/channel + DVB SID. This is deliberately
    independent of satellite presentation metadata, which was incomplete for
    several otherwise-valid TV carriers.
    """
    if not AUTHORITATIVE_SERVICES.exists():
        raise RuntimeError(
            f"Authoritative service baseline missing: {AUTHORITATIVE_SERVICES}"
        )

    expected: dict[int, str] = {}

    with AUTHORITATIVE_SERVICES.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        for row in csv.DictReader(handle):
            try:
                if (
                    str(row.get("host", "")).strip() == host
                    and int(row.get("module", -1)) == module
                    and int(row.get("channel", -1)) == channel_id
                ):
                    sid = int(row["sid"])
                    name = str(row.get("service_name", "")).strip()
                    expected[sid] = name or f"SID {sid}"
            except Exception:
                continue

    if not expected:
        raise RuntimeError(
            f"No authoritative V6 service baseline for "
            f"{host} M{module}C{channel_id}"
        )

    return expected


def render(
    host: str,
    module: int,
    channel_id: int,
    config_path: str,
) -> str:
    cfg = load_config(config_path)

    client = SnmpV2cClient(
        host,
        cfg.snmp.community,
        cfg.snmp.port,
        cfg.snmp.timeout_seconds,
        cfg.snmp.retries,
    )

    expected = load_expected_services(
        host,
        module,
        channel_id,
    )

    # Current WISI data remains authoritative for current reception.
    # The V6 service inventory supplies only the frozen expected SID/name set.
    poller = FastGT34Poller(
        client,
        cfg.services.expected,
        {},
    )

    input_row = poller.get_gt34_input(
        module,
        channel_id,
    )

    sample = poller.poll_input(input_row)

    cache = ROOT / "state" / "prtg_tv43_remote_cache.json"

    try:
        web_enriched = bool(
            enrich_sample_from_web_fast(
                sample,
                host,
                cache,
            )
        )
    except Exception:
        web_enriched = False

    services = list(
        getattr(sample, "services", [])
        or []
    )

    current_by_sid = {
        int(getattr(s, "sid")): s
        for s in services
        if getattr(s, "sid", None) is not None
    }

    expected_sids = set(expected)
    current_sids = set(current_by_sid)

    missing_sids = sorted(
        expected_sids - current_sids
    )

    unexpected_sids = sorted(
        current_sids - expected_sids
    )

    expected_present = [
        current_by_sid[sid]
        for sid in sorted(
            expected_sids & current_sids
        )
    ]

    es_missing_expected = [
        s
        for s in expected_present
        if not service_has_es_metadata(s)
    ]

    video_services = [
        s
        for s in expected_present
        if service_has_video(s)
    ]

    locked = bool(
        getattr(sample, "effective_locked", False)
    )

    ts_present = bool(
        getattr(sample, "ts_present", False)
    )

    carrier_health = (
        locked
        and ts_present
    )

    # Carrier is in the TV43 scope because V1/V4 proved video on this logical
    # carrier. Current "Video Present" remains a live condition.
    video_present = len(video_services) > 0

    # Strict service integrity for regulatory monitoring:
    #   - every authoritative expected SID must still be present;
    #   - every expected SID currently present must expose at least one ES row
    #     (video, audio or other ES metadata);
    #   - carrier/TS and current video must be present.
    #
    # This intentionally makes Geo M1C10 alarm while Geo ME SID 1100 remains
    # present but has ES/PID metadata unavailable.
    service_integrity = (
        carrier_health
        and video_present
        and len(missing_sids) == 0
        and len(es_missing_expected) == 0
    )

    bitrate_bps = getattr(
        sample,
        "web_ts_bitrate_bps",
        None,
    )
    if bitrate_bps in (None, 0):
        bitrate_bps = getattr(
            sample,
            "ts_bitrate_bps",
            None,
        )

    total_es = total_elementary_streams(services)
    running_services = running_service_count(services)

    detected_isi = getattr(sample, "detected_isi", None)
    raw_mis = getattr(sample, "mis", None)
    effective_isi_mis = (
        detected_isi
        if detected_isi is not None
        else raw_mis
    )

    # ------------------------------------------------------------------
    # FINAL STABLE PRTG PRESENTATION POLICY V11.3J
    # ------------------------------------------------------------------
    # PRTG persists channel definitions and their limits. Removing or renaming
    # an already-registered limited channel can leave it at "No data"/0 and
    # falsely force the sensor Down. Therefore:
    #
    #   1. Build one canonical value for every channel name used by any of the
    #      V5/V9/V11 production schemas.
    #   2. Read the frozen <=50 return-channel inventory produced by V11.3J.
    #   3. Return exactly those names, in that order, on every scan.
    #
    # Registered stale channels outside that frozen schema have their PRTG
    # limits disabled by the installer. No further dynamic channel-set changes
    # are permitted on these 43 objects.

    audio_services = []
    services_without_video = []
    services_without_audio = []
    total_video_streams = 0
    total_audio_streams = 0

    for svc in services:
        video_count = len(getattr(svc, "video_streams", []) or [])
        audio_count = len(getattr(svc, "audio_streams", []) or [])
        if video_count == 0 and getattr(svc, "video_pid", None) is not None:
            video_count = 1
        if audio_count == 0 and getattr(svc, "audio_pid", None) is not None:
            audio_count = 1

        total_video_streams += video_count
        total_audio_streams += audio_count

        if video_count == 0:
            services_without_video.append(svc)
        if audio_count > 0:
            audio_services.append(svc)
        else:
            services_without_audio.append(svc)

    expected_present_count = len(expected) - len(missing_sids)

    service_name_mismatches = 0
    for sid, expected_name in expected.items():
        svc = current_by_sid.get(sid)
        if svc is not None:
            current_name = safe_text(getattr(svc, "name", "") or "")
            if current_name and current_name != safe_text(expected_name):
                service_name_mismatches += 1

    es_services_available = len(services) - sum(
        1 for svc in services
        if not service_has_es_metadata(svc)
    )

    canonical: dict[str, dict[str, Any]] = {}

    def put(item: dict[str, Any]) -> None:
        canonical[str(item["channel"])] = item

    # Core current channels.
    put(channel("Carrier / TS Health", 1 if carrier_health else 0, "ok", limit_min_error=1))
    put(channel("Demod Lock", 1 if locked else 0, "locked", limit_min_error=1))
    put(channel("Transport Stream Present", 1 if ts_present else 0, "present", limit_min_error=1))
    put(channel("Video Present", 1 if video_present else 0, "present", limit_min_error=1))
    put(channel("Service Integrity Health", 1 if service_integrity else 0, "ok", limit_min_error=1))

    put(channel("RF Level", num(getattr(sample, "rf_level_dbm", None)), "dBm", is_float=True))
    put(channel("SNR", num(getattr(sample, "snr_db", None)), "dB", is_float=True))
    put(channel("BER", num(getattr(sample, "ber", None)), "BER", is_float=True))
    put(channel("Frequency", num(getattr(sample, "frequency_khz", None)) / 1000.0, "MHz", is_float=True))
    put(channel("Symbol Rate", num(getattr(sample, "symbol_rate_bd", None)) / 1_000_000.0, "MBd", is_float=True))
    put(channel("TS Bitrate", num(bitrate_bps) / 1_000_000.0, "Mbit/s", is_float=True))

    put(channel("TSID", int_or_unknown(getattr(sample, "tsid", None)), "ID"))
    put(channel("NID", int_or_unknown(getattr(sample, "nid", None)), "ID"))
    put(channel("ONID", int_or_unknown(getattr(sample, "onid", None)), "ID"))

    put(channel("Modulation Enum", int_or_unknown(getattr(sample, "modulation", None)), "enum"))
    put(channel("FEC Enum", int_or_unknown(getattr(sample, "code_rate", None)), "enum"))
    put(channel("MIS", int_or_unknown(effective_isi_mis), "value"))
    put(channel("ISI / MIS", int_or_unknown(effective_isi_mis), "value"))

    put(channel("DVB Services", len(services), "services"))
    put(channel("Discovered Services", len(services), "services"))
    put(channel("Running Services", running_services, "services"))
    put(channel("Expected Services", len(expected), "services"))
    put(channel("Present Expected Services", expected_present_count, "services"))

    put(channel("Video Services", len(video_services), "services"))
    put(channel("Services With Video", len(video_services), "services"))
    put(channel("Services Without Video", len(services_without_video), "services"))
    put(channel("Services With Audio", len(audio_services), "services"))
    put(channel("Services Without Audio", len(services_without_audio), "services"))

    put(channel("Total Elementary Streams", total_es, "streams"))
    put(channel("Total Video Streams", total_video_streams, "streams"))
    put(channel("Total Audio Streams", total_audio_streams, "streams"))

    put(channel("ES Services Available", es_services_available, "services"))
    put(channel("ES Metadata Missing", len(es_missing_expected), "services"))
    put(channel("ES Metadata Unavailable", len(es_missing_expected), "services"))

    # Preserve old limited aliases exactly.
    put(channel("Missing Services", len(missing_sids), "services", limit_max_error=0))
    put(channel("Missing Expected Services", len(missing_sids), "services", limit_max_error=0))
    put(channel("Unexpected Services", len(unexpected_sids), "services", limit_max_warning=0))
    put(channel("Service Name Mismatches", service_name_mismatches, "services", limit_max_warning=0))

    # Historical ES Compliance Health was a structural/instrumentation check,
    # not the strict per-service availability alarm. Keep it healthy when the
    # carrier/TS collection itself is operational; strict ES-N/A findings are
    # carried by Service Integrity Health + the individual Service channel.
    put(channel(
        "ES Compliance Health",
        1 if carrier_health else 0,
        "ok",
        limit_min_error=1,
    ))

    put(channel("Web Enrichment", 1 if web_enriched else 0, "available"))

    # ------------------------------------------------------------------
    # Per-service channels: generate BOTH naming conventions that have already
    # existed in production.
    # ------------------------------------------------------------------
    # Example duplicate-name mux:
    #   Service Digital 1
    #   Service Digital 1 [SID 1]
    #   Service Digital 1 [SID 257]
    #
    # The old unsuffixed alias is grouped: it is healthy only if every expected
    # SID sharing that name is healthy.
    sids_by_name: dict[str, list[int]] = {}
    for sid, expected_name in sorted(expected.items()):
        clean_name = safe_text(expected_name)
        sids_by_name.setdefault(clean_name, []).append(int(sid))

        svc = current_by_sid.get(int(sid))
        put(channel(
            f"Service {clean_name} [SID {sid}]",
            service_health(svc),
            "ok",
            limit_min_error=1,
        ))

    for clean_name, sids in sorted(sids_by_name.items()):
        grouped_ok = all(
            service_health(current_by_sid.get(sid)) == 1
            for sid in sids
        )
        put(channel(
            f"Service {clean_name}",
            1 if grouped_ok else 0,
            "ok",
            limit_min_error=1,
        ))

    registered_names = load_return_channel_names(
        host,
        module,
        channel_id,
    )

    if not registered_names:
        raise RuntimeError(
            "Stable PRTG return-channel manifest missing for "
            f"{host} M{module}C{channel_id}; run V11.1 installer first"
        )

    missing_canonical = [
        name for name in registered_names
        if name not in canonical
    ]
    if missing_canonical:
        raise RuntimeError(
            "Registered PRTG channel(s) have no canonical live value: "
            + ", ".join(missing_canonical)
        )

    results = [
        canonical[name]
        for name in registered_names
    ]

    if len(results) > 50:
        raise RuntimeError(
            f"Registered PRTG channel inventory exceeds 50: {len(results)} "
            f"for {host} M{module}C{channel_id}"
        )

    missing_text = [
        f"{expected[sid]} SID {sid} MISSING"
        for sid in missing_sids
    ]

    es_fault_text = [
        (
            f"{expected.get(int(getattr(s, 'sid')), getattr(s, 'name', ''))} "
            f"SID {getattr(s, 'sid')} ES-N/A"
        )
        for s in es_missing_expected
    ]

    unexpected_text = [
        (
            f"{getattr(current_by_sid[sid], 'name', '')} "
            f"SID {sid} UNEXPECTED"
        )
        for sid in unexpected_sids
    ]

    faults = missing_text + es_fault_text

    freq_mhz = (
        f"{num(getattr(sample, 'frequency_khz', None))/1000.0:g} MHz"
        if getattr(sample, "frequency_khz", None) is not None
        else "freq N/A"
    )
    sr_kbaud = (
        f"{num(getattr(sample, 'symbol_rate_bd', None))/1000.0:g} kBaud"
        if getattr(sample, "symbol_rate_bd", None) is not None
        else "SR N/A"
    )

    pol = enum_label(
        POLARISATION_LABELS,
        getattr(sample, "polarisation", None),
    ).upper()

    cfg_mod = enum_label(
        MODULATION_LABELS,
        getattr(sample, "modulation", None),
    )
    cfg_fec = enum_label(
        CODE_RATE_LABELS,
        getattr(sample, "code_rate", None),
    )
    pls_mode = enum_label(
        PLS_MODE_LABELS,
        getattr(sample, "pls_mode", None),
    ).upper()

    detected_mod = safe_text(
        getattr(sample, "detected_constellation", None)
        or cfg_mod
    )
    detected_fec = safe_text(
        getattr(sample, "detected_code_rate", None)
        or cfg_fec
    )

    lnb_v = enum_label(
        LNB_VOLTAGE_LABELS,
        getattr(sample, "lnb_voltage", None),
    )
    tone = enum_label(
        TONE_LABELS,
        getattr(sample, "tone_22khz", None),
    )

    lo_khz = getattr(sample, "lo_frequency_khz", None)
    lo_mhz = (
        f"{num(lo_khz)/1000.0:g} MHz"
        if lo_khz is not None
        else "N/A"
    )

    if_khz = getattr(sample, "web_if_frequency_khz", None)
    if_mhz = (
        f"{num(if_khz)/1000.0:g} MHz"
        if if_khz is not None
        else "N/A"
    )

    network_name = safe_text(
        getattr(sample, "network_name", "") or ""
    )

    providers: list[str] = []
    for svc in services:
        provider = safe_text(getattr(svc, "provider", "") or "")
        if provider and provider not in providers:
            providers.append(provider)

    service_text = "; ".join(
        service_detail(svc)
        for svc in sorted(
            services,
            key=lambda item: int(getattr(item, "sid", 0)),
        )
    )

    text = (
        f"{safe_text(getattr(sample,'display_name',''))} | "
        f"{'LOCKED' if locked else 'UNLOCKED'} | "
        f"{'TS PRESENT' if ts_present else 'TS ABSENT'} | "
        f"TP {freq_mhz} / {pol} / {sr_kbaud} | "
        f"CFG MOD {cfg_mod} FEC {cfg_fec} "
        f"PLS {pls_mode} PLS-ID {int_or_unknown(getattr(sample,'pls_id',None))} | "
        f"DET {detected_mod} / {detected_fec} / "
        f"ISI {int_or_unknown(effective_isi_mis)} | "
        f"LNB LO {lo_mhz} V {lnb_v} 22K {tone} | "
        f"IF {if_mhz} | "
        f"BITRATE {num(bitrate_bps)/1_000_000.0:.3f} Mbit/s | "
        f"RF {num(getattr(sample,'rf_level_dbm',None)):.3f} dBm | "
        f"SNR {num(getattr(sample,'snr_db',None)):.3f} dB | "
        f"BER {num(getattr(sample,'ber',None)):g} | "
        f"TSID {int_or_unknown(getattr(sample,'tsid',None))} | "
        f"NID {int_or_unknown(getattr(sample,'nid',None))} | "
        f"ONID {int_or_unknown(getattr(sample,'onid',None))} | "
        f"NETWORK \"{network_name}\" | "
        f"PROVIDERS: {', '.join(providers) if providers else 'N/A'} | "
        f"SERVICES {len(services)} | "
        f"EXPECTED {len(expected)-len(missing_sids)}/{len(expected)} | "
        f"VIDEO {len(video_services)} | "
        f"ES {total_es} | "
        f"SERVICE INTEGRITY {'OK' if service_integrity else 'FAULT'}"
    )

    if faults:
        text += " | FAULTS: " + "; ".join(
            safe_text(x) for x in faults[:20]
        )

    if unexpected_text:
        text += " | CHANGES: " + "; ".join(
            safe_text(x) for x in unexpected_text[:20]
        )

    if service_text:
        text += " | " + safe_text(service_text)

    payload = {
        "prtg": {
            "result": results,
            "text": safe_text(text),
        }
    }

    persist_alarm_snapshot(
        host=host,
        module=module,
        channel_id=channel_id,
        input_name=safe_text(getattr(sample, "display_name", "")),
        prtg_payload=payload,
        expected=expected,
        current_by_sid=current_by_sid,
        missing_sids=missing_sids,
        es_missing_expected=es_missing_expected,
        execution_error=None,
    )

    # Persist the same successful authoritative observation into SQLite.
    # History persistence is important, but database writer contention must
    # never turn a healthy live transmission into a PRTG sensor failure.
    #
    # Multiple PRTG sensors can execute concurrently and SQLite permits only
    # one writer at a time. Retry transient lock contention briefly. If the
    # database remains locked, return the valid live PRTG payload and record a
    # local diagnostic rather than reporting a false transmission alarm.
    history_error = None
    for attempt in range(5):
        try:
            persist_tv43_history(
                host=host,
                module=module,
                channel_id=channel_id,
                input_name=safe_text(getattr(sample, "display_name", "")),
                sample=sample,
                expected=expected,
                current_by_sid=current_by_sid,
                missing_sids=missing_sids,
                es_missing_expected=es_missing_expected,
                carrier_health=carrier_health,
                ts_present=ts_present,
                service_integrity=service_integrity,
                video_services=video_services,
                status_text=safe_text(text),
            )
            history_error = None
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            history_error = exc
            time.sleep(0.20 * (attempt + 1))

    if history_error is not None:
        try:
            log_dir = ROOT / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "tv43_history_write_failures.log").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                    f"{host} M{module}C{channel_id} "
                    f"history skipped after lock retries: {history_error}\n"
                )
        except Exception:
            pass

    return json.dumps(
        payload,
        ensure_ascii=True,
    )



SNAPSHOT_DIR = ROOT / "state" / "tv43_alarm_snapshots"


def _snapshot_path(host: str, module: int, channel_id: int) -> Path:
    safe_host = host.replace(".", "_")
    return SNAPSHOT_DIR / f"{safe_host}_M{module}C{channel_id}.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def persist_alarm_snapshot(
    *,
    host: str,
    module: int,
    channel_id: int,
    input_name: str,
    prtg_payload: dict[str, Any],
    expected: dict[int, str] | None = None,
    current_by_sid: dict[int, Any] | None = None,
    missing_sids: list[int] | None = None,
    es_missing_expected: list[Any] | None = None,
    execution_error: str | None = None,
) -> None:
    results = list(prtg_payload.get("prtg", {}).get("result", []) or [])
    by_name = {
        str(item.get("channel")): item.get("value")
        for item in results
        if item.get("channel") is not None
    }

    expected = expected or {}
    current_by_sid = current_by_sid or {}
    missing_sids = missing_sids or []
    es_missing_expected = es_missing_expected or []

    current_services = []
    for sid, svc in sorted(current_by_sid.items()):
        current_services.append(
            {
                "sid": int(sid),
                "name": safe_text(getattr(svc, "name", "") or expected.get(int(sid), f"SID {sid}")),
                "has_video": bool(service_has_video(svc)),
                "has_es": bool(service_has_es_metadata(svc)),
            }
        )

    es_missing_sids = {
        int(getattr(s, "sid"))
        for s in es_missing_expected
        if getattr(s, "sid", None) is not None
    }

    snapshot = {
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": host,
        "module": module,
        "channel": channel_id,
        "input_name": input_name,
        "execution_error": execution_error,
        "status_text": str(prtg_payload.get("prtg", {}).get("text", "")),
        "channels": by_name,
        "expected_services": [
            {"sid": int(sid), "name": safe_text(name)}
            for sid, name in sorted(expected.items())
        ],
        "current_services": current_services,
        "missing_services": [
            {"sid": int(sid), "name": safe_text(expected.get(int(sid), f"SID {sid}"))}
            for sid in sorted(missing_sids)
        ],
        "es_missing_services": [
            {"sid": int(sid), "name": safe_text(expected.get(int(sid), f"SID {sid}"))}
            for sid in sorted(es_missing_sids)
        ],
    }
    _atomic_write_json(
        _snapshot_path(host, module, channel_id),
        snapshot,
    )


def render_error(message: str) -> str:
    return json.dumps(
        {
            "prtg": {
                "error": 1,
                "text": safe_text(message),
            }
        },
        ensure_ascii=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Rich bounded PRTG sensor for one authoritative "
            "WISI GT34 TV-bearing logical carrier"
        )
    )

    ap.add_argument(
        "--config",
        default=str(ROOT / "config.toml"),
    )
    ap.add_argument(
        "--host",
        required=True,
    )
    ap.add_argument(
        "--module",
        type=int,
        required=True,
    )
    ap.add_argument(
        "--channel",
        type=int,
        required=True,
    )

    args = ap.parse_args()

    try:
        print(
            render(
                args.host,
                args.module,
                args.channel,
                args.config,
            )
        )
        return 0
    except Exception as exc:
        message = (
            f"WISI TV43 service-integrity sensor failed: "
            f"{type(exc).__name__}: {exc}"
        )
        payload = {
            "prtg": {
                "error": 1,
                "text": safe_text(message),
            }
        }
        try:
            persist_alarm_snapshot(
                host=args.host,
                module=args.module,
                channel_id=args.channel,
                input_name=f"M{args.module}C{args.channel}",
                prtg_payload=payload,
                execution_error=message,
            )
        except Exception:
            pass
        print(json.dumps(payload, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
