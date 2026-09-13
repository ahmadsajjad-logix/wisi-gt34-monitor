from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "database" / "wisi_monitor.db"
RETENTION_DAYS = 15

SCHEMA = """
CREATE TABLE IF NOT EXISTS tv43_carrier_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at TEXT NOT NULL,
    host TEXT NOT NULL,
    module INTEGER NOT NULL,
    channel INTEGER NOT NULL,
    input_name TEXT NOT NULL,

    carrier_health INTEGER NOT NULL,
    demod_lock INTEGER NOT NULL,
    ts_present INTEGER NOT NULL,
    service_integrity INTEGER NOT NULL,

    rf_level_dbm REAL,
    snr_db REAL,
    ber REAL,
    frequency_mhz REAL,
    symbol_rate_mbd REAL,
    ts_bitrate_mbps REAL,

    polarisation TEXT,
    modulation_config TEXT,
    fec_config TEXT,
    modulation_detected TEXT,
    fec_detected TEXT,
    isi_mis INTEGER,
    pls_mode TEXT,
    pls_id INTEGER,

    lnb_lo_mhz REAL,
    lnb_voltage TEXT,
    tone_22khz TEXT,
    if_frequency_mhz REAL,

    tsid INTEGER,
    nid INTEGER,
    onid INTEGER,
    network_name TEXT,
    providers TEXT,

    expected_services INTEGER NOT NULL,
    current_services INTEGER NOT NULL,
    running_services INTEGER NOT NULL,
    video_services INTEGER NOT NULL,
    total_elementary_streams INTEGER NOT NULL,
    missing_expected_services INTEGER NOT NULL,
    es_metadata_missing INTEGER NOT NULL,
    unexpected_services INTEGER NOT NULL,

    status_text TEXT NOT NULL,

    UNIQUE(host, module, channel, sampled_at)
);

CREATE INDEX IF NOT EXISTS idx_tv43_carrier_history_lookup
ON tv43_carrier_history(host, module, channel, sampled_at);

CREATE TABLE IF NOT EXISTS tv43_service_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at TEXT NOT NULL,
    host TEXT NOT NULL,
    module INTEGER NOT NULL,
    channel INTEGER NOT NULL,
    sid INTEGER NOT NULL,
    expected_name TEXT NOT NULL,
    current_name TEXT,
    present INTEGER NOT NULL,
    running INTEGER,
    has_es INTEGER NOT NULL,
    has_video INTEGER NOT NULL,
    elementary_stream_count INTEGER NOT NULL,
    provider TEXT,
    service_status TEXT,
    service_detail TEXT,

    UNIQUE(host, module, channel, sid, sampled_at)
);

CREATE INDEX IF NOT EXISTS idx_tv43_service_history_lookup
ON tv43_service_history(host, module, channel, sid, sampled_at);

CREATE TABLE IF NOT EXISTS tv43_history_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _enum_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _service_has_video(service: Any) -> bool:
    try:
        if bool(getattr(service, "has_video", False)):
            return True
    except Exception:
        pass
    try:
        return getattr(service, "video_pid", None) is not None
    except Exception:
        return False


def _service_has_es(service: Any) -> bool:
    try:
        if int(getattr(service, "elementary_stream_count", 0) or 0) > 0:
            return True
    except Exception:
        pass
    try:
        if getattr(service, "video_pid", None) is not None:
            return True
        if getattr(service, "audio_pid", None) is not None:
            return True
    except Exception:
        pass
    return False


def _service_detail(service: Any) -> str:
    sid = getattr(service, "sid", "?")
    name = str(getattr(service, "name", "") or f"SID {sid}").strip()
    status = str(getattr(service, "status_label", "") or "").strip()

    streams = list(getattr(service, "elementary_streams", []) or [])
    if not streams:
        return f"{name} SID {sid} {status} ES-N/A".strip()

    parts: list[str] = []
    for stream in streams:
        category = str(getattr(stream, "category", "other"))
        prefix = {"video": "V", "audio": "A"}.get(category, "O")
        pid = getattr(stream, "pid", "?")
        label = (
            getattr(stream, "type_label", None)
            or getattr(stream, "stream_type_name", None)
            or getattr(stream, "stream_type", "")
        )
        language = str(getattr(stream, "language", "") or "").strip()
        item = f"{prefix}{pid}:{label}"
        if language:
            item += f"/{language}"
        parts.append(item)
    return f"{name} SID {sid} {status} ES{len(streams)} " + ",".join(parts)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _maybe_cleanup(conn: sqlite3.Connection, now: datetime) -> None:
    row = conn.execute(
        "SELECT value FROM tv43_history_meta WHERE key='last_retention_at'"
    ).fetchone()

    if row is not None:
        try:
            last = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last.astimezone(timezone.utc)).total_seconds() < 86400:
                return
        except Exception:
            pass

    cutoff = _iso(now - timedelta(days=RETENTION_DAYS))

    conn.execute(
        "DELETE FROM tv43_service_history WHERE sampled_at < ?",
        (cutoff,),
    )
    conn.execute(
        "DELETE FROM tv43_carrier_history WHERE sampled_at < ?",
        (cutoff,),
    )
    conn.execute(
        """
        INSERT INTO tv43_history_meta(key,value)
        VALUES('last_retention_at',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (_iso(now),),
    )


def persist_tv43_history(
    *,
    host: str,
    module: int,
    channel_id: int,
    input_name: str,
    sample: Any,
    expected: dict[int, str],
    current_by_sid: dict[int, Any],
    missing_sids: list[int],
    es_missing_expected: list[Any],
    carrier_health: bool,
    ts_present: bool,
    service_integrity: bool,
    video_services: list[Any],
    status_text: str,
    database_path: Path | str = DATABASE_PATH,
) -> None:
    """
    Append one authoritative TV43 carrier observation plus one row for every
    expected service. The schema is deliberately separate from legacy tuner
    history because logical WISI channels can exceed the 0-7 tuner index model.
    """
    now = _utcnow()
    sampled_at = _iso(now)

    db = Path(database_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_schema(conn)

        services = list(current_by_sid.values())
        running = sum(
            1 for svc in services
            if bool(getattr(svc, "running", False))
        )
        total_es = sum(
            int(getattr(svc, "elementary_stream_count", 0) or 0)
            for svc in services
        )

        missing_set = {int(x) for x in missing_sids}
        es_missing_set = {
            int(getattr(svc, "sid"))
            for svc in es_missing_expected
            if getattr(svc, "sid", None) is not None
        }
        unexpected = sorted(set(current_by_sid) - set(expected))

        providers: list[str] = []
        for svc in services:
            p = str(getattr(svc, "provider", "") or "").strip()
            if p and p not in providers:
                providers.append(p)

        demod_lock = 1 if bool(getattr(sample, "effective_locked", False)) else 0
        bitrate_bps = getattr(sample, "ts_bitrate_bps", None)

        conn.execute(
            """
            INSERT OR IGNORE INTO tv43_carrier_history(
                sampled_at,host,module,channel,input_name,
                carrier_health,demod_lock,ts_present,service_integrity,
                rf_level_dbm,snr_db,ber,frequency_mhz,symbol_rate_mbd,
                ts_bitrate_mbps,polarisation,modulation_config,fec_config,
                modulation_detected,fec_detected,isi_mis,pls_mode,pls_id,
                lnb_lo_mhz,lnb_voltage,tone_22khz,if_frequency_mhz,
                tsid,nid,onid,network_name,providers,
                expected_services,current_services,running_services,
                video_services,total_elementary_streams,
                missing_expected_services,es_metadata_missing,
                unexpected_services,status_text
            )
            VALUES(
                ?,?,?,?,?, ?,?,?,?,
                ?,?,?,?,?, ?,
                ?,?,?,?,?, ?,?,?,
                ?,?,?,?,
                ?,?,?,?,?,
                ?,?,?,?, ?,?,?,?,?
            )
            """,
            (
                sampled_at, host, int(module), int(channel_id), input_name,
                1 if carrier_health else 0,
                demod_lock,
                1 if ts_present else 0,
                1 if service_integrity else 0,
                _as_float(getattr(sample, "rf_level_dbm", None)),
                _as_float(getattr(sample, "snr_db", None)),
                _as_float(getattr(sample, "ber", None)),
                (
                    None if getattr(sample, "frequency_khz", None) is None
                    else _as_float(getattr(sample, "frequency_khz")) / 1000.0
                ),
                (
                    None if getattr(sample, "symbol_rate_bd", None) is None
                    else _as_float(getattr(sample, "symbol_rate_bd")) / 1_000_000.0
                ),
                (
                    None if bitrate_bps is None
                    else _as_float(bitrate_bps) / 1_000_000.0
                ),
                _enum_text(getattr(sample, "polarisation", None)),
                _enum_text(getattr(sample, "modulation", None)),
                _enum_text(getattr(sample, "code_rate", None)),
                _enum_text(getattr(sample, "detected_constellation", None)),
                _enum_text(getattr(sample, "detected_code_rate", None)),
                _as_int(
                    getattr(sample, "detected_isi", None)
                    if getattr(sample, "detected_isi", None) is not None
                    else getattr(sample, "mis", None)
                ),
                _enum_text(getattr(sample, "pls_mode", None)),
                _as_int(getattr(sample, "pls_id", None)),
                (
                    None if getattr(sample, "lo_frequency_khz", None) is None
                    else _as_float(getattr(sample, "lo_frequency_khz")) / 1000.0
                ),
                _enum_text(getattr(sample, "lnb_voltage", None)),
                _enum_text(getattr(sample, "tone_22khz", None)),
                (
                    None if getattr(sample, "web_if_frequency_khz", None) is None
                    else _as_float(getattr(sample, "web_if_frequency_khz")) / 1000.0
                ),
                _as_int(getattr(sample, "tsid", None)),
                _as_int(getattr(sample, "nid", None)),
                _as_int(getattr(sample, "onid", None)),
                _enum_text(getattr(sample, "network_name", None)),
                ", ".join(providers),
                len(expected),
                len(services),
                running,
                len(video_services),
                total_es,
                len(missing_set),
                len(es_missing_set),
                len(unexpected),
                status_text,
            ),
        )

        for sid, expected_name in sorted(expected.items()):
            svc = current_by_sid.get(int(sid))
            present = svc is not None

            conn.execute(
                """
                INSERT OR IGNORE INTO tv43_service_history(
                    sampled_at,host,module,channel,sid,expected_name,
                    current_name,present,running,has_es,has_video,
                    elementary_stream_count,provider,service_status,service_detail
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sampled_at,
                    host,
                    int(module),
                    int(channel_id),
                    int(sid),
                    str(expected_name),
                    (
                        None if svc is None
                        else str(getattr(svc, "name", "") or "").strip()
                    ),
                    1 if present else 0,
                    (
                        None if svc is None
                        else 1 if bool(getattr(svc, "running", False)) else 0
                    ),
                    1 if (svc is not None and _service_has_es(svc)) else 0,
                    1 if (svc is not None and _service_has_video(svc)) else 0,
                    (
                        0 if svc is None
                        else int(getattr(svc, "elementary_stream_count", 0) or 0)
                    ),
                    (
                        None if svc is None
                        else str(getattr(svc, "provider", "") or "").strip()
                    ),
                    (
                        None if svc is None
                        else str(getattr(svc, "status_label", "") or "").strip()
                    ),
                    (
                        f"{expected_name} SID {sid} MISSING"
                        if svc is None
                        else _service_detail(svc)
                    ),
                ),
            )

        _maybe_cleanup(conn, now)
        conn.commit()
    finally:
        conn.close()
