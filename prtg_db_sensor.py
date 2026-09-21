from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from config import DATABASE_PATH


# ---------------------------------------------------------------------------
# PRTG helpers
# ---------------------------------------------------------------------------

def prtg_error(text: str) -> str:
    return json.dumps({"prtg": {"error": 1, "text": text}}, ensure_ascii=False)


def add_channel(
    result: list[dict],
    name: str,
    value,
    customunit: str,
    *,
    float_value: bool = False,
    limit_min_error: float | None = None,
    limit_max_error: float | None = None,
    limit_max_warning: float | None = None,
) -> None:
    if value is None:
        return

    channel = {
        "channel": name,
        "value": value,
        "unit": "Custom",
        "customunit": customunit,
    }

    if float_value:
        channel["float"] = 1
        channel["decimalmode"] = "Auto"

    limits = {}
    if limit_min_error is not None:
        limits["limitminerror"] = limit_min_error
    if limit_max_error is not None:
        limits["limitmaxerror"] = limit_max_error
    if limit_max_warning is not None:
        limits["limitmaxwarning"] = limit_max_warning

    if limits:
        channel["limitmode"] = 1
        channel.update(limits)

    result.append(channel)


def parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compact(value) -> str:
    return " ".join(str(value or "").split()).strip()


def service_display_names(services: list[sqlite3.Row]) -> dict[int, str]:
    counts: dict[str, int] = {}
    for svc in services:
        base = compact(svc["service_name"]) or f"Unnamed service {svc['sid']}"
        counts[base] = counts.get(base, 0) + 1

    names: dict[int, str] = {}
    for svc in services:
        base = compact(svc["service_name"]) or f"Unnamed service {svc['sid']}"
        if counts[base] > 1:
            names[int(svc["service_db_id"])] = f"{base} [SID {svc['sid']}]"
        else:
            names[int(svc["service_db_id"])] = base
    return names


# ---------------------------------------------------------------------------
# SQLite reads - read only
# ---------------------------------------------------------------------------

def latest_snapshot(
    conn: sqlite3.Connection,
    host: str,
    module_number: int,
    input_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            c.host AS chassis_host,
            m.module_number,
            t.id AS tuner_id,
            t.input_id,
            t.display_number,
            ts.id AS tuner_sample_id,
            ts.sampled_at,
            ts.lock_state,
            ts.rf_level_dbm,
            ts.snr_db,
            ts.ber_value,
            ts.ber_text,
            ts.frequency_raw,
            ts.frequency_offset_raw,
            ts.symbol_rate,
            ts.modulation,
            ts.fec,
            ts.isi,
            xs.current_bitrate_bps,
            xs.tei_delta,
            xs.sync_error_delta,
            xs.input_cc_delta,
            ps.pcr_accuracy_errors_delta,
            ps.pcr_repetition_errors_delta,
            ps.pcr_discontinuity_errors_delta,
            ps.ref_discontinuities_delta,
            ps.playout_fifo_reset_delta,
            ps.into_freerunning_delta,
            ps.pcr_pid_changed_delta,
            ps.freerunning
        FROM modules m
        JOIN chassis c ON c.id = m.chassis_id
        JOIN tuners t ON t.module_id = m.id
        JOIN tuner_samples ts ON ts.id = (
            SELECT ts2.id
            FROM tuner_samples ts2
            WHERE ts2.tuner_id = t.id
            ORDER BY ts2.id DESC
            LIMIT 1
        )
        LEFT JOIN ts_samples xs
          ON xs.tuner_id = t.id
         AND xs.sampled_at = ts.sampled_at
        LEFT JOIN pcr_input_samples ps
          ON ps.tuner_id = t.id
         AND ps.sampled_at = ts.sampled_at
        WHERE c.host = ?
          AND m.module_number = ?
          AND t.input_id = ?
        LIMIT 1
        """,
        (host, module_number, input_id),
    ).fetchone()


def current_transport_stream(
    conn: sqlite3.Connection,
    tuner_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT tsid, onid, network_id, last_seen_at
        FROM transport_streams
        WHERE tuner_id = ?
        ORDER BY
            CASE WHEN last_seen_at IS NULL THEN 1 ELSE 0 END,
            last_seen_at DESC,
            id DESC
        LIMIT 1
        """,
        (tuner_id,),
    ).fetchone()


def current_services(
    conn: sqlite3.Connection,
    tuner_id: int,
) -> list[sqlite3.Row]:
    """
    Return the current collector-owned service inventory for this tuner.

    `services` is the authoritative discovery table populated by collector.py.
    `channel_inventory` is a derived monitoring baseline and can lag newly
    discovered chassis/tuners, so it must not gate PRTG service discovery.

    When channel_state exists, use it.  Otherwise, a service present in the
    tuner's latest collector service snapshot is presented as UP.  Python's
    channel monitor remains authoritative for transition alarms/email.
    """
    return conn.execute(
        """
        SELECT
            s.id AS service_db_id,
            s.service_id AS sid,
            s.service_name,
            s.provider_name,
            s.pmt_pid,
            s.pcr_pid,
            s.last_seen_at,
            COALESCE(
                cs.state,
                CASE
                    WHEN s.last_seen_at = (
                        SELECT MAX(s2.last_seen_at)
                        FROM services s2
                        WHERE s2.tuner_id = s.tuner_id
                    )
                    THEN 'UP'
                    ELSE 'UNKNOWN'
                END
            ) AS state,
            COALESCE(cs.reason, 'collector_inventory') AS reason
        FROM services s
        LEFT JOIN channel_state cs ON cs.service_db_id = s.id
        WHERE s.tuner_id = ?
          AND s.last_seen_at = (
              SELECT MAX(s2.last_seen_at)
              FROM services s2
              WHERE s2.tuner_id = s.tuner_id
          )
        ORDER BY s.service_id, s.service_name
        """,
        (tuner_id,),
    ).fetchall()


def service_streams(
    conn: sqlite3.Connection,
    service_db_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            ss.pid,
            ss.stream_type,
            ss.stream_type_name,
            ss.language,
            ss.last_seen_at,
            CASE
                WHEN cai.id IS NULL THEN NULL
                ELSE COALESCE(cas.state, 'UNKNOWN')
            END AS audio_state
        FROM service_streams ss
        LEFT JOIN channel_audio_inventory cai
          ON cai.service_db_id = ss.service_id
         AND cai.pid = ss.pid
        LEFT JOIN channel_audio_state cas
          ON cas.audio_inventory_id = cai.id
        WHERE ss.service_id = ?
        ORDER BY ss.pid
        """,
        (service_db_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def render(
    host: str,
    module_number: int,
    input_id: int,
    database_path: Path | str = DATABASE_PATH,
) -> str:
    conn = sqlite3.connect(str(database_path), timeout=10.0)
    conn.row_factory = sqlite3.Row

    try:
        row = latest_snapshot(conn, host, module_number, input_id)
        if row is None:
            return prtg_error(
                f"No sample found for {host} module {module_number}, input {input_id}"
            )

        tuner_id = int(row["tuner_id"])
        sampled_at = str(row["sampled_at"])
        age_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - parse_timestamp(sampled_at)).total_seconds(),
        )

        services = current_services(conn, tuner_id)
        names = service_display_names(services)
        transport = current_transport_stream(conn, tuner_id)

        lock = 1 if int(row["lock_state"] or 0) == 1 else 0
        bitrate = row["current_bitrate_bps"]
        ts_present = 1 if lock == 1 and bitrate is not None and float(bitrate) > 0 else 0
        carrier_ts_health = 1 if lock == 1 and ts_present == 1 else 0

        up_count = sum(1 for s in services if s["state"] == "UP")
        down_count = sum(1 for s in services if s["state"] == "DOWN")

        # ------------------------------------------------------------------
        # Channels arranged to resemble the useful legacy PRTG presentation.
        # PRTG limits here are visual status only. Python remains authoritative
        # for operational alarms and email notifications.
        # ------------------------------------------------------------------
        result: list[dict] = []

        add_channel(result, "Demod Lock", lock, "locked", limit_min_error=1)
        add_channel(
            result,
            "Transport Stream Present",
            ts_present,
            "present",
            limit_min_error=1,
        )
        add_channel(
            result,
            "Carrier / TS Health",
            carrier_ts_health,
            "operational",
            limit_min_error=1,
        )

        add_channel(result, "RF Level", row["rf_level_dbm"], "dBm", float_value=True)
        add_channel(result, "SNR", row["snr_db"], "dB", float_value=True)
        add_channel(result, "BER", row["ber_value"], "ratio", float_value=True)

        # frequency_raw is deliberately labelled raw: its unit has not yet been
        # independently established in the new collector/parser baseline.
        add_channel(
            result,
            "Frequency Raw",
            row["frequency_raw"],
            "raw",
            float_value=True,
        )

        # Only emit Symbol Rate when the current DB actually contains it.
        add_channel(
            result,
            "Symbol Rate",
            row["symbol_rate"],
            "raw",
            float_value=True,
        )

        if bitrate is not None:
            add_channel(
                result,
                "TS Bitrate",
                round(float(bitrate) / 1_000_000.0, 6),
                "Mbit/s",
                float_value=True,
            )

        if transport is not None:
            add_channel(result, "TSID", transport["tsid"], "ID")
            add_channel(result, "ONID", transport["onid"], "ID")
            add_channel(result, "NID", transport["network_id"], "ID")

        add_channel(result, "Discovered Services", len(services), "services")
        add_channel(result, "Running Services", up_count, "services")
        add_channel(
            result,
            "Missing / Down Services",
            down_count,
            "services",
            limit_max_error=0,
        )

        # Per-service operational status.
        total_es = 0
        audio_present = 0
        audio_missing = 0
        service_details: list[str] = []

        for svc in services:
            service_db_id = int(svc["service_db_id"])
            name = names[service_db_id]
            state = str(svc["state"])

            add_channel(
                result,
                f"Service {name}",
                1 if state == "UP" else 0,
                "ok",
                limit_min_error=1,
            )

            streams = service_streams(conn, service_db_id)
            total_es += len(streams)

            video_pids: list[str] = []
            audio_desc: list[str] = []
            other_pids: list[str] = []

            for st in streams:
                pid = int(st["pid"])
                type_name = compact(st["stream_type_name"]).lower()
                audio_state = st["audio_state"]

                if "video" in type_name:
                    video_pids.append(str(pid))
                elif audio_state is not None:
                    lang = compact(st["language"])[:12]
                    audio_label = f"Audio {name} PID {pid}"
                    if lang:
                        audio_label += f" {lang}"

                    # Keep per-audio PID state in sensor text and aggregate
                    # Audio Tracks Present/Missing channels only.  Creating one
                    # PRTG channel per audio PID can exceed the 50-channel limit
                    # on large multiplexes.

                    if audio_state == "PRESENT":
                        audio_present += 1
                    elif audio_state == "MISSING":
                        audio_missing += 1

                    audio_desc.append(
                        f"{pid}"
                        + (f"/{lang}" if lang else "")
                        + f":{audio_state}"
                    )
                else:
                    other_pids.append(str(pid))

            detail = f"{name} SID {svc['sid']} {state}"
            if svc["provider_name"]:
                detail += f" PROVIDER {compact(svc['provider_name'])}"
            if svc["pmt_pid"] is not None:
                detail += f" PMT {svc['pmt_pid']}"
            if svc["pcr_pid"] is not None:
                detail += f" PCR {svc['pcr_pid']}"
            if video_pids:
                detail += f" V {','.join(video_pids)}"
            if audio_desc:
                detail += f" A {','.join(audio_desc)}"
            if other_pids:
                detail += f" OTHER {','.join(other_pids)}"

            service_details.append(detail)

        add_channel(result, "Total Elementary Streams", total_es, "streams")
        add_channel(result, "Audio Tracks Present", audio_present, "tracks")
        add_channel(
            result,
            "Audio Tracks Missing",
            audio_missing,
            "tracks",
            limit_max_error=0,
        )

        # Objective technical counters already collected by Python.
        add_channel(result, "TEI Delta", row["tei_delta"], "events")
        add_channel(result, "Sync Error Delta", row["sync_error_delta"], "events")
        add_channel(result, "Input CC Error Delta", row["input_cc_delta"], "events")
        add_channel(
            result,
            "PCR Accuracy Error Delta",
            row["pcr_accuracy_errors_delta"],
            "events",
        )
        add_channel(
            result,
            "PCR Repetition Error Delta",
            row["pcr_repetition_errors_delta"],
            "events",
        )
        add_channel(
            result,
            "PCR Discontinuity Delta",
            row["pcr_discontinuity_errors_delta"],
            "events",
        )
        add_channel(
            result,
            "PCR Ref Discontinuity Delta",
            row["ref_discontinuities_delta"],
            "events",
        )
        add_channel(
            result,
            "Playout FIFO Reset Delta",
            row["playout_fifo_reset_delta"],
            "events",
        )
        add_channel(
            result,
            "Freerunning Entry Delta",
            row["into_freerunning_delta"],
            "events",
        )
        add_channel(
            result,
            "PCR PID Changed Delta",
            row["pcr_pid_changed_delta"],
            "events",
        )
        add_channel(result, "PCR Freerunning", row["freerunning"], "1=yes")

        add_channel(result, "Data Age", round(age_seconds, 1), "s", float_value=True)

        if len(result) > 50:
            return prtg_error(
                f"M{module_number}/input {input_id} requires {len(result)} PRTG "
                "channels; PRTG EXE/Script Advanced maximum is 50."
            )

        # ------------------------------------------------------------------
        # Compact legacy-style status line for the device/sensor overview.
        # No unsupported RF metadata is fabricated.
        # ------------------------------------------------------------------
        primary_names = ", ".join(names.values()) if names else "NO SERVICES"

        headline = [
            primary_names,
            "LOCKED" if lock else "UNLOCKED",
            f"{host} M{module_number}/input {input_id}",
        ]

        if row["modulation"]:
            headline.append(compact(row["modulation"]))
        if row["fec"]:
            headline.append(f"FEC {compact(row['fec'])}")
        if row["isi"] is not None:
            headline.append(f"ISI {row['isi']}")
        if row["rf_level_dbm"] is not None:
            headline.append(f"RF {row['rf_level_dbm']} dBm")
        if row["snr_db"] is not None:
            headline.append(f"SNR {row['snr_db']} dB")
        if row["ber_text"]:
            headline.append(f"BER {compact(row['ber_text'])}")

        headline.append(f"SERVICES {len(services)}")
        headline.append(f"RUNNING {up_count}")
        headline.append(f"DOWN {down_count}")

        providers = sorted(
            {
                compact(s["provider_name"])
                for s in services
                if compact(s["provider_name"])
            }
        )
        if providers:
            headline.append("PROVIDERS " + ", ".join(providers))

        if transport is not None:
            ids = []
            if transport["tsid"] is not None:
                ids.append(f"TSID {transport['tsid']}")
            if transport["network_id"] is not None:
                ids.append(f"NID {transport['network_id']}")
            if transport["onid"] is not None:
                ids.append(f"ONID {transport['onid']}")
            if ids:
                headline.append(" ".join(ids))

        if service_details:
            headline.append(" | ".join(service_details))

        text = " | ".join(headline)

        return json.dumps(
            {"prtg": {"result": result, "text": text}},
            ensure_ascii=False,
        )

    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only legacy-style PRTG presentation backed by the "
            "WISI monitor SQLite database."
        )
    )
    parser.add_argument("--host", default="192.168.3.27")
    parser.add_argument("--module", type=int, required=True)
    parser.add_argument(
        "--input",
        type=int,
        required=True,
        help="Canonical WISI input_id (0-7)",
    )
    parser.add_argument("--database", default=str(DATABASE_PATH))
    args = parser.parse_args()

    try:
        print(render(args.host, args.module, args.input, args.database))
        return 0
    except Exception as exc:
        print(prtg_error(f"WISI DB sensor failed: {exc}"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
