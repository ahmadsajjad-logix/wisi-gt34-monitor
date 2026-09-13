from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "database" / "wisi_monitor.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", default=str(DB))
    args = ap.parse_args()

    conn = sqlite3.connect(args.database)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {"tv43_carrier_history", "tv43_service_history", "tv43_history_meta"}
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError("Missing TV43 history tables: " + ", ".join(missing))

        carrier_rows = conn.execute(
            "SELECT COUNT(*) FROM tv43_carrier_history"
        ).fetchone()[0]
        service_rows = conn.execute(
            "SELECT COUNT(*) FROM tv43_service_history"
        ).fetchone()[0]

        latest = conn.execute(
            """
            SELECT sampled_at,host,module,channel,input_name,
                   carrier_health,service_integrity,
                   rf_level_dbm,snr_db,ber,frequency_mhz,symbol_rate_mbd,
                   ts_bitrate_mbps,tsid,nid,onid,
                   expected_services,current_services,video_services,
                   total_elementary_streams,missing_expected_services,
                   es_metadata_missing,status_text
            FROM tv43_carrier_history
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        geo = conn.execute(
            """
            SELECT sampled_at,expected_name,present,has_es,has_video,service_detail
            FROM tv43_service_history
            WHERE host='192.168.3.8' AND module=1 AND channel=10 AND sid=1100
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        print("=" * 100)
        print("TV43 15-DAY HISTORY VALIDATION")
        print("=" * 100)
        print(f"Database             : {args.database}")
        print(f"Carrier history rows : {carrier_rows}")
        print(f"Service history rows : {service_rows}")

        if latest:
            print("-" * 100)
            print(
                f"Latest carrier       : {latest['host']} "
                f"M{latest['module']}C{latest['channel']} "
                f"{latest['input_name']}"
            )
            print(f"Sampled              : {latest['sampled_at']}")
            print(f"RF / SNR / BER       : {latest['rf_level_dbm']} / {latest['snr_db']} / {latest['ber']}")
            print(f"Frequency / SR       : {latest['frequency_mhz']} MHz / {latest['symbol_rate_mbd']} MBd")
            print(f"TS bitrate           : {latest['ts_bitrate_mbps']} Mbit/s")
            print(f"TSID/NID/ONID        : {latest['tsid']}/{latest['nid']}/{latest['onid']}")
            print(
                f"Services             : current={latest['current_services']} "
                f"expected={latest['expected_services']} "
                f"video={latest['video_services']} "
                f"ES={latest['total_elementary_streams']}"
            )

        if geo:
            print("-" * 100)
            print("Geo ME latest service-history row:")
            print(
                f"  sampled={geo['sampled_at']} | "
                f"present={geo['present']} | has_es={geo['has_es']} | "
                f"has_video={geo['has_video']}"
            )
            print(f"  {geo['service_detail']}")

        print("=" * 100)
        print("TV43 HISTORY VALIDATION PASS")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
