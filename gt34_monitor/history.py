from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ReceiverSample
from .transponder import derive_rf_band


HISTORY_SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS prtg_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_timestamp_utc TEXT NOT NULL,
    persisted_at_utc TEXT NOT NULL,
    host TEXT NOT NULL,
    module INTEGER NOT NULL,
    channel INTEGER NOT NULL,
    input_name TEXT,
    source_family TEXT,
    source_oid TEXT,
    raw_lock INTEGER,
    effective_lock INTEGER NOT NULL,
    ts_present INTEGER,
    ts_index INTEGER,
    rf_level_dbm REAL,
    snr_db REAL,
    ber REAL,
    frequency_khz INTEGER,
    polarisation_raw INTEGER,
    symbol_rate_bd INTEGER,
    modulation_raw INTEGER,
    code_rate_raw INTEGER,
    pls_mode_raw INTEGER,
    pls_id_raw INTEGER,
    mis_raw INTEGER,
    lnb_type_raw INTEGER,
    lo_frequency_khz INTEGER,
    lnb_voltage_raw INTEGER,
    tone_22khz_raw INTEGER,
    rf_band TEXT,
    rf_band_provenance TEXT,
    ts_bitrate_bps INTEGER,
    ts_payload_bitrate_bps INTEGER,
    web_enriched INTEGER NOT NULL,
    detected_constellation TEXT,
    detected_code_rate TEXT,
    detected_isi INTEGER,
    web_ts_bitrate_bps INTEGER,
    web_ber_text TEXT,
    web_if_frequency_khz INTEGER,
    web_tuner_id INTEGER,
    web_remote_id TEXT,
    tsid INTEGER,
    nid INTEGER,
    onid INTEGER,
    network_name TEXT,
    service_count_reported INTEGER,
    service_count_discovered INTEGER NOT NULL,
    running_service_count INTEGER NOT NULL,
    expected_service_count INTEGER NOT NULL,
    present_expected_service_count INTEGER NOT NULL,
    missing_expected_service_count INTEGER NOT NULL,
    unexpected_service_count INTEGER NOT NULL,
    service_name_mismatch_count INTEGER NOT NULL,
    prtg_channel_count INTEGER NOT NULL,
    prtg_status_text TEXT NOT NULL,
    prtg_payload_sha256 TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prtg_obs_host_input_time
ON prtg_observations(host, module, channel, sample_timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_prtg_obs_time ON prtg_observations(sample_timestamp_utc);

CREATE TABLE IF NOT EXISTS prtg_channels (
    observation_id INTEGER NOT NULL REFERENCES prtg_observations(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    channel_name TEXT NOT NULL,
    value REAL,
    unit TEXT,
    custom_unit TEXT,
    float_flag INTEGER,
    limit_mode INTEGER,
    channel_json TEXT NOT NULL,
    PRIMARY KEY (observation_id, ordinal)
);

CREATE TABLE IF NOT EXISTS service_history (
    observation_id INTEGER NOT NULL REFERENCES prtg_observations(id) ON DELETE CASCADE,
    sid INTEGER NOT NULL,
    service_name TEXT NOT NULL,
    provider TEXT,
    running_status_raw INTEGER,
    running_status_label TEXT,
    operational_ok INTEGER NOT NULL,
    expected_name TEXT,
    is_expected INTEGER NOT NULL,
    is_present INTEGER NOT NULL,
    name_matches_expected INTEGER,
    descrambling_status_raw INTEGER,
    descrambling_status_label TEXT,
    elementary_stream_count INTEGER NOT NULL,
    PRIMARY KEY (observation_id, sid)
);

CREATE TABLE IF NOT EXISTS elementary_stream_history (
    observation_id INTEGER NOT NULL REFERENCES prtg_observations(id) ON DELETE CASCADE,
    sid INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    stream_type_raw INTEGER NOT NULL,
    stream_type_label TEXT NOT NULL,
    category TEXT NOT NULL,
    language TEXT,
    PRIMARY KEY (observation_id, sid, pid)
);
"""


def default_history_path(config_path: str | Path) -> Path:
    config_file = Path(config_path).resolve()
    return config_file.parent / "database" / "gt34_prtg_history.sqlite3"


class PrtgHistoryDatabase:
    """Concurrent-safe append-only history for exact PRTG observations.

    One PRTG sensor execution opens a short-lived connection and commits its
    observation, PRTG channels, services and ES rows in one transaction.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=15.0)
        self.connection.execute("PRAGMA busy_timeout=15000")
        for attempt in range(5):
            try:
                self.connection.executescript(SCHEMA)
                self.connection.commit()
                break
            except sqlite3.OperationalError as exc:
                try:
                    self.connection.rollback()
                except Exception:
                    pass
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))

    def close(self) -> None:
        self.connection.close()

    def persist(
        self,
        *,
        host: str,
        sample: ReceiverSample,
        web_enriched: bool,
        prtg_payload_json: str,
        retries: int = 5,
    ) -> int:
        payload: dict[str, Any] = json.loads(prtg_payload_json)
        prtg = payload.get("prtg", {})
        channels = list(prtg.get("result", []) or [])
        status_text = str(prtg.get("text", ""))
        band, band_provenance = derive_rf_band(sample.frequency_khz)
        payload_sha256 = hashlib.sha256(prtg_payload_json.encode("utf-8")).hexdigest()

        for attempt in range(retries):
            try:
                return self._persist_once(
                    host=host,
                    sample=sample,
                    web_enriched=web_enriched,
                    prtg_payload_json=prtg_payload_json,
                    status_text=status_text,
                    channels=channels,
                    band=band,
                    band_provenance=band_provenance,
                    payload_sha256=payload_sha256,
                )
            except sqlite3.OperationalError as exc:
                try:
                    self.connection.rollback()
                except Exception:
                    pass
                if "locked" not in str(exc).lower() or attempt + 1 >= retries:
                    raise
                time.sleep(0.15 * (attempt + 1))
        raise RuntimeError("history persistence retry loop exhausted")

    def _persist_once(
        self,
        *,
        host: str,
        sample: ReceiverSample,
        web_enriched: bool,
        prtg_payload_json: str,
        status_text: str,
        channels: list[dict[str, Any]],
        band: str | None,
        band_provenance: str | None,
        payload_sha256: str,
    ) -> int:
        self.connection.execute("BEGIN IMMEDIATE")
        observation_columns = [
            "sample_timestamp_utc","persisted_at_utc","host","module","channel","input_name","source_family","source_oid",
            "raw_lock","effective_lock","ts_present","ts_index","rf_level_dbm","snr_db","ber","frequency_khz","polarisation_raw",
            "symbol_rate_bd","modulation_raw","code_rate_raw","pls_mode_raw","pls_id_raw","mis_raw","lnb_type_raw",
            "lo_frequency_khz","lnb_voltage_raw","tone_22khz_raw","rf_band","rf_band_provenance","ts_bitrate_bps",
            "ts_payload_bitrate_bps","web_enriched","detected_constellation","detected_code_rate","detected_isi",
            "web_ts_bitrate_bps","web_ber_text","web_if_frequency_khz","web_tuner_id","web_remote_id","tsid","nid","onid",
            "network_name","service_count_reported","service_count_discovered","running_service_count",
            "expected_service_count","present_expected_service_count","missing_expected_service_count",
            "unexpected_service_count","service_name_mismatch_count","prtg_channel_count","prtg_status_text",
            "prtg_payload_sha256","schema_version",
        ]
        observation_values = (
            sample.timestamp_utc, datetime.now(timezone.utc).isoformat(), host, sample.module, sample.channel,
            sample.input_name, sample.source_family, sample.source_oid, sample.lock, int(sample.effective_locked),
            None if sample.ts_present is None else int(sample.ts_present), sample.ts_index, sample.rf_level_dbm,
            sample.snr_db, sample.ber, sample.frequency_khz, sample.polarisation, sample.symbol_rate_bd,
            sample.modulation, sample.code_rate, sample.pls_mode, sample.pls_id, sample.mis, sample.lnb_type,
            sample.lo_frequency_khz, sample.lnb_voltage, sample.tone_22khz, band, band_provenance,
            sample.ts_bitrate_bps, sample.ts_payload_bitrate_bps, int(bool(web_enriched)),
            sample.detected_constellation, sample.detected_code_rate, sample.detected_isi,
            sample.web_ts_bitrate_bps, sample.web_ber_text, sample.web_if_frequency_khz, sample.web_tuner_id,
            sample.web_remote_id, sample.tsid, sample.nid, sample.onid, sample.network_name, sample.service_count,
            sample.discovered_service_count, sample.running_service_count, sample.expected_service_count,
            sample.present_expected_service_count, sample.missing_expected_service_count,
            sample.unexpected_service_count, sample.service_name_mismatch_count, len(channels), status_text,
            payload_sha256, HISTORY_SCHEMA_VERSION,
        )
        cur = self.connection.execute(
            f"INSERT INTO prtg_observations ({','.join(observation_columns)}) "
            f"VALUES ({','.join('?' for _ in observation_columns)})",
            observation_values,
        )
        observation_id = int(cur.lastrowid)

        for ordinal, channel in enumerate(channels, start=1):
            self.connection.execute(
                """INSERT INTO prtg_channels(
                    observation_id,ordinal,channel_name,value,unit,custom_unit,float_flag,limit_mode,channel_json
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    observation_id, ordinal, str(channel.get("channel", "")), channel.get("value"),
                    channel.get("unit"), channel.get("customunit"), channel.get("float"), channel.get("limitmode"),
                    json.dumps(channel, ensure_ascii=True, separators=(",", ":")),
                ),
            )

        live = sample.live_services_by_sid
        all_sids = sorted(set(live) | set(sample.expected_services))
        for sid in all_sids:
            service = live.get(sid)
            expected_name = sample.expected_services.get(sid)
            if service is None:
                self.connection.execute(
                    """INSERT INTO service_history(
                        observation_id,sid,service_name,provider,running_status_raw,running_status_label,operational_ok,
                        expected_name,is_expected,is_present,name_matches_expected,descrambling_status_raw,
                        descrambling_status_label,elementary_stream_count
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (observation_id,sid,expected_name or "",None,None,"absent",0,expected_name,1,0,None,None,"unknown",0),
                )
                continue

            name_match = None if expected_name is None else int(
                service.name.casefold().strip() == expected_name.casefold().strip()
            )
            self.connection.execute(
                """INSERT INTO service_history(
                    observation_id,sid,service_name,provider,running_status_raw,running_status_label,operational_ok,
                    expected_name,is_expected,is_present,name_matches_expected,descrambling_status_raw,
                    descrambling_status_label,elementary_stream_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation_id, sid, service.name, service.provider, service.status, service.status_label,
                    int(service.operational_ok), expected_name, int(expected_name is not None), 1, name_match,
                    service.descrambling_status, service.descrambling_status_label,
                    int(service.elementary_stream_count or len(service.elementary_streams)),
                ),
            )
            for stream in service.elementary_streams:
                self.connection.execute(
                    """INSERT INTO elementary_stream_history(
                        observation_id,sid,pid,stream_type_raw,stream_type_label,category,language
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        observation_id, sid, stream.pid, stream.stream_type, stream.type_label,
                        stream.category, stream.language,
                    ),
                )

        self.connection.commit()
        return observation_id
