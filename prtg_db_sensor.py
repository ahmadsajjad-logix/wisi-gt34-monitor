from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import DATABASE_PATH


def prtg_error(text: str) -> str:
    return json.dumps({"prtg": {"error": 1, "text": text}}, ensure_ascii=False)


def add_channel(result: list[dict], name: str, value, customunit: str, *, float_value: bool = False) -> None:
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
    result.append(channel)


def parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def latest_snapshot(conn: sqlite3.Connection, module_number: int, input_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            m.module_number,
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
        WHERE m.module_number = ?
          AND t.input_id = ?
        LIMIT 1
        """,
        (module_number, input_id),
    ).fetchone()


def render(module_number: int, input_id: int, database_path: Path | str = DATABASE_PATH) -> str:
    conn = sqlite3.connect(str(database_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        row = latest_snapshot(conn, module_number, input_id)
        if row is None:
            return prtg_error(f"No sample found for module {module_number}, input {input_id}")

        result: list[dict] = []
        sampled_at = str(row["sampled_at"])
        age_seconds = max(0.0, (datetime.now(timezone.utc) - parse_timestamp(sampled_at)).total_seconds())

        add_channel(result, "Data Age", round(age_seconds, 1), "s", float_value=True)
        add_channel(result, "Demod Lock", row["lock_state"], "1=locked")
        add_channel(result, "RF Level", row["rf_level_dbm"], "dBm", float_value=True)
        add_channel(result, "SNR", row["snr_db"], "dB", float_value=True)
        add_channel(result, "BER", row["ber_value"], "ratio", float_value=True)
        if row["current_bitrate_bps"] is not None:
            add_channel(result, "TS Bitrate", round(float(row["current_bitrate_bps"]) / 1_000_000.0, 6), "Mbit/s", float_value=True)

        add_channel(result, "TEI Delta", row["tei_delta"], "events")
        add_channel(result, "Sync Error Delta", row["sync_error_delta"], "events")
        add_channel(result, "Input CC Error Delta", row["input_cc_delta"], "events")
        add_channel(result, "PCR Accuracy Error Delta", row["pcr_accuracy_errors_delta"], "events")
        add_channel(result, "PCR Repetition Error Delta", row["pcr_repetition_errors_delta"], "events")
        add_channel(result, "PCR Discontinuity Delta", row["pcr_discontinuity_errors_delta"], "events")
        add_channel(result, "PCR Ref Discontinuity Delta", row["ref_discontinuities_delta"], "events")
        add_channel(result, "Playout FIFO Reset Delta", row["playout_fifo_reset_delta"], "events")
        add_channel(result, "Freerunning Entry Delta", row["into_freerunning_delta"], "events")
        add_channel(result, "PCR PID Changed Delta", row["pcr_pid_changed_delta"], "events")
        add_channel(result, "PCR Freerunning", row["freerunning"], "1=yes")

        text_parts = [
            f"M{module_number}/input {input_id}",
            f"sample {sampled_at}",
        ]
        if row["ber_text"]:
            text_parts.append(f"BER {row['ber_text']}")
        if row["modulation"]:
            text_parts.append(str(row["modulation"]))
        if row["fec"]:
            text_parts.append(f"FEC {row['fec']}")

        return json.dumps(
            {"prtg": {"result": result, "text": " | ".join(text_parts)}},
            ensure_ascii=False,
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only PRTG sensor backed by the WISI monitor SQLite database.")
    parser.add_argument("--module", type=int, required=True)
    parser.add_argument("--input", type=int, required=True, help="Canonical WISI input_id (0-7)")
    parser.add_argument("--database", default=str(DATABASE_PATH))
    args = parser.parse_args()

    try:
        print(render(args.module, args.input, args.database))
        return 0
    except Exception as exc:
        print(prtg_error(f"WISI DB sensor failed: {exc}"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
