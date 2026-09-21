import sqlite3
from datetime import datetime, timezone

from config import (
    DATABASE_DIR,
    LOG_DIR,
    DATABASE_PATH,
    WISI_HOST,
    WISI_MODULES,
)


SCHEMA_VERSION = 1


def utc_now():
    return datetime.now(
        timezone.utc
    ).isoformat(timespec="milliseconds")


def get_connection():
    DATABASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    LOG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA journal_mode=WAL;"
    )

    conn.execute(
        "PRAGMA synchronous=NORMAL;"
    )

    conn.execute(
        "PRAGMA foreign_keys=ON;"
    )

    conn.execute(
        "PRAGMA busy_timeout=30000;"
    )

    return conn


def create_schema(conn):
    conn.executescript(
        """
        ------------------------------------------------------------
        -- SCHEMA METADATA
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );


        ------------------------------------------------------------
        -- CHASSIS
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS chassis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            host TEXT NOT NULL UNIQUE,

            name TEXT,

            created_at TEXT NOT NULL,
            last_seen_at TEXT
        );


        ------------------------------------------------------------
        -- MODULES
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS modules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            chassis_id INTEGER NOT NULL,

            module_number INTEGER NOT NULL,

            module_name TEXT,

            remote_identifier TEXT NOT NULL,

            remote_ip TEXT NOT NULL,

            created_at TEXT NOT NULL,
            last_seen_at TEXT,

            UNIQUE (
                chassis_id,
                module_number
            ),

            UNIQUE (
                chassis_id,
                remote_identifier
            ),

            FOREIGN KEY (
                chassis_id
            )
            REFERENCES chassis(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- TUNERS / TS INPUTS
        --
        -- WISI input_id is canonical.
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS tuners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            module_id INTEGER NOT NULL,

            input_id INTEGER NOT NULL,

            hwid INTEGER,

            display_number INTEGER NOT NULL,

            configured_name TEXT,

            first_seen_at TEXT NOT NULL,

            last_seen_at TEXT,

            UNIQUE (
                module_id,
                input_id
            ),

            FOREIGN KEY (
                module_id
            )
            REFERENCES modules(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- TUNER RF / DEMODULATOR SAMPLES
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS tuner_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            tuner_id INTEGER NOT NULL,

            sampled_at TEXT NOT NULL,

            lock_state INTEGER,

            enabled INTEGER,

            state INTEGER,

            disabled INTEGER,

            rf_level_dbm REAL,

            snr_db REAL,

            ber_text TEXT,

            ber_value REAL,

            frequency_raw REAL,

            frequency_offset_raw REAL,

            symbol_rate REAL,

            modulation TEXT,

            fec TEXT,

            isi INTEGER,

            raw_source TEXT,

            FOREIGN KEY (
                tuner_id
            )
            REFERENCES tuners(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- TRANSPORT STREAM SAMPLES
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS ts_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            tuner_id INTEGER NOT NULL,

            sampled_at TEXT NOT NULL,

            current_bitrate_bps REAL,

            min_bitrate_bps REAL,

            max_bitrate_bps REAL,

            tei_total INTEGER,

            tei_delta INTEGER,

            sync_error_total INTEGER,

            sync_error_delta INTEGER,

            input_cc_total INTEGER,

            input_cc_delta INTEGER,

            raw_source TEXT,

            FOREIGN KEY (
                tuner_id
            )
            REFERENCES tuners(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- PID CATALOG
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS pids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            tuner_id INTEGER NOT NULL,

            pid INTEGER NOT NULL,

            first_seen_at TEXT NOT NULL,

            last_seen_at TEXT,

            UNIQUE (
                tuner_id,
                pid
            ),

            FOREIGN KEY (
                tuner_id
            )
            REFERENCES tuners(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- PID TIME-SERIES
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS pid_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            pid_id INTEGER NOT NULL,

            sampled_at TEXT NOT NULL,

            bitrate_bps REAL,

            packet_count INTEGER,

            packet_delta INTEGER,

            cc_error_total INTEGER,

            cc_error_delta INTEGER,

            pcr_present INTEGER,

            scrambled INTEGER,

            pes_header INTEGER,

            pes_scrambled INTEGER,

            flags TEXT,

            FOREIGN KEY (
                pid_id
            )
            REFERENCES pids(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- PCR / INPUT REGULATOR SAMPLES
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS pcr_input_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            tuner_id INTEGER NOT NULL,

            sampled_at TEXT NOT NULL,

            monitor_enabled INTEGER,

            periodic_rate REAL,

            buffer_conf_jitter REAL,

            buffer_jitter REAL,

            buffer_size INTEGER,

            buffer_level INTEGER,

            fill_wait INTEGER,

            freerunning INTEGER,

            jitter_min REAL,

            jitter_max REAL,

            pcr_diff_max REAL,

            ref_discontinuities_total INTEGER,

            ref_discontinuities_delta INTEGER,

            pcr_accuracy_errors_total INTEGER,

            pcr_accuracy_errors_delta INTEGER,

            pcr_repetition_errors_total INTEGER,

            pcr_repetition_errors_delta INTEGER,

            pcr_discontinuity_errors_total INTEGER,

            pcr_discontinuity_errors_delta INTEGER,

            to_wait_state_total INTEGER,

            to_wait_state_delta INTEGER,

            playout_fifo_reset_total INTEGER,

            playout_fifo_reset_delta INTEGER,

            unref_discontinuity_total INTEGER,

            unref_discontinuity_delta INTEGER,

            sample_ignored_total INTEGER,

            sample_ignored_delta INTEGER,

            into_freerunning_total INTEGER,

            into_freerunning_delta INTEGER,

            pcr_pid_changed_total INTEGER,

            pcr_pid_changed_delta INTEGER,

            raw_source TEXT,

            FOREIGN KEY (
                tuner_id
            )
            REFERENCES tuners(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- INDIVIDUAL PCR PID SAMPLES
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS pcr_pid_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            pid_id INTEGER NOT NULL,

            sampled_at TEXT NOT NULL,

            active INTEGER,

            last_seen_raw INTEGER,

            pcr_bitrate REAL,

            stc_bitrate REAL,

            stc_factor REAL,

            jitter_min REAL,

            jitter_max REAL,

            pcr_diff_max REAL,

            ref_discontinuities_total INTEGER,

            ref_discontinuities_delta INTEGER,

            pcr_accuracy_errors_total INTEGER,

            pcr_accuracy_errors_delta INTEGER,

            pcr_repetition_errors_total INTEGER,

            pcr_repetition_errors_delta INTEGER,

            pcr_discontinuity_errors_total INTEGER,

            pcr_discontinuity_errors_delta INTEGER,

            FOREIGN KEY (
                pid_id
            )
            REFERENCES pids(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- DVB TRANSPORT STREAM IDENTIFICATION
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS transport_streams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            tuner_id INTEGER NOT NULL,

            tsid INTEGER,

            onid INTEGER,

            network_id INTEGER,

            first_seen_at TEXT NOT NULL,

            last_seen_at TEXT,

            UNIQUE (
                tuner_id,
                tsid,
                onid
            ),

            FOREIGN KEY (
                tuner_id
            )
            REFERENCES tuners(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- SERVICES
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            tuner_id INTEGER NOT NULL,

            service_id INTEGER NOT NULL,

            service_name TEXT,

            provider_name TEXT,

            pmt_pid INTEGER,

            pcr_pid INTEGER,

            first_seen_at TEXT NOT NULL,

            last_seen_at TEXT,

            UNIQUE (
                tuner_id,
                service_id
            ),

            FOREIGN KEY (
                tuner_id
            )
            REFERENCES tuners(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- IMMUTABLE SERVICE OBSERVATION SAMPLES
        --
        -- One row per service observed in a specific tuner sample.
        -- Used by the alert policy to evaluate service presence against
        -- the same acquisition generation rather than mutable catalog state.
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS service_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            tuner_id INTEGER NOT NULL,

            sampled_at TEXT NOT NULL,

            service_id INTEGER NOT NULL,

            service_name TEXT,

            provider_name TEXT,

            pmt_pid INTEGER,

            pcr_pid INTEGER,

            running_status INTEGER,

            elementary_stream_count INTEGER,

            UNIQUE (
                tuner_id,
                sampled_at,
                service_id
            ),

            FOREIGN KEY (
                tuner_id
            )
            REFERENCES tuners(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- SERVICE ELEMENTARY STREAMS
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS service_streams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            service_id INTEGER NOT NULL,

            pid INTEGER NOT NULL,

            stream_type INTEGER,

            stream_type_name TEXT,

            language TEXT,

            first_seen_at TEXT NOT NULL,

            last_seen_at TEXT,

            UNIQUE (
                service_id,
                pid,
                stream_type
            ),

            FOREIGN KEY (
                service_id
            )
            REFERENCES services(id)
            ON DELETE CASCADE
        );


        ------------------------------------------------------------
        -- CURRENT COUNTER STATE
        --
        -- Used for reliable delta calculation across application
        -- restarts.
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS counter_state (
            counter_key TEXT PRIMARY KEY,

            counter_value INTEGER,

            sampled_at TEXT NOT NULL
        );


        ------------------------------------------------------------
        -- GENERATED MONITORING EVENTS
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS monitoring_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            occurred_at TEXT NOT NULL,

            module_id INTEGER,

            tuner_id INTEGER,

            pid_id INTEGER,

            category TEXT NOT NULL,

            severity TEXT NOT NULL,

            metric TEXT NOT NULL,

            value REAL,

            delta REAL,

            message TEXT NOT NULL,

            acknowledged INTEGER NOT NULL DEFAULT 0,

            FOREIGN KEY (
                module_id
            )
            REFERENCES modules(id),

            FOREIGN KEY (
                tuner_id
            )
            REFERENCES tuners(id),

            FOREIGN KEY (
                pid_id
            )
            REFERENCES pids(id)
        );


        ------------------------------------------------------------
        -- POLL EXECUTION HISTORY
        ------------------------------------------------------------

        CREATE TABLE IF NOT EXISTS poll_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            started_at TEXT NOT NULL,

            completed_at TEXT,

            success INTEGER,

            duration_seconds REAL,

            modules_attempted INTEGER,

            modules_succeeded INTEGER,

            error_message TEXT
        );


        ------------------------------------------------------------
        -- INDEXES
        ------------------------------------------------------------

        CREATE INDEX IF NOT EXISTS
        idx_tuner_samples_time
        ON tuner_samples(
            tuner_id,
            sampled_at
        );

        CREATE INDEX IF NOT EXISTS
        idx_ts_samples_time
        ON ts_samples(
            tuner_id,
            sampled_at
        );

        CREATE INDEX IF NOT EXISTS
        idx_service_samples_tuner_time
        ON service_samples(
            tuner_id,
            sampled_at
        );

        CREATE INDEX IF NOT EXISTS
        idx_pid_samples_time
        ON pid_samples(
            pid_id,
            sampled_at
        );

        CREATE INDEX IF NOT EXISTS
        idx_pcr_input_samples_time
        ON pcr_input_samples(
            tuner_id,
            sampled_at
        );

        CREATE INDEX IF NOT EXISTS
        idx_pcr_pid_samples_time
        ON pcr_pid_samples(
            pid_id,
            sampled_at
        );

        CREATE INDEX IF NOT EXISTS
        idx_monitoring_events_time
        ON monitoring_events(
            occurred_at
        );

        CREATE INDEX IF NOT EXISTS
        idx_monitoring_events_severity
        ON monitoring_events(
            severity,
            occurred_at
        );
        """
    )

    conn.execute(
        """
        INSERT INTO schema_metadata (
            key,
            value
        )
        VALUES (
            'schema_version',
            ?
        )
        ON CONFLICT(key)
        DO UPDATE SET
            value = excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )

    conn.commit()


def register_equipment(conn):
    now = utc_now()

    conn.execute(
        """
        INSERT INTO chassis (
            host,
            name,
            created_at,
            last_seen_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(host)
        DO UPDATE SET
            last_seen_at = excluded.last_seen_at
        """,
        (
            WISI_HOST,
            "WISI TANGRAM Chassis",
            now,
            now,
        ),
    )

    chassis_row = conn.execute(
        """
        SELECT id
        FROM chassis
        WHERE host = ?
        """,
        (WISI_HOST,),
    ).fetchone()

    chassis_id = chassis_row["id"]

    for (
        module_number,
        module_config,
    ) in WISI_MODULES.items():

        conn.execute(
            """
            INSERT INTO modules (
                chassis_id,
                module_number,
                module_name,
                remote_identifier,
                remote_ip,
                created_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(
                chassis_id,
                module_number
            )
            DO UPDATE SET
                module_name =
                    excluded.module_name,

                remote_identifier =
                    excluded.remote_identifier,

                remote_ip =
                    excluded.remote_ip,

                last_seen_at =
                    excluded.last_seen_at
            """,
            (
                chassis_id,
                module_number,
                module_config["name"],
                module_config["remote"],
                module_config["remote_ip"],
                now,
                now,
            ),
        )

    conn.commit()


def register_tuners(conn):
    now = utc_now()

    module_rows = conn.execute(
        """
        SELECT
            id,
            module_number
        FROM modules
        """
    ).fetchall()

    for module_row in module_rows:
        module_id = module_row["id"]

        for input_id in range(8):

            conn.execute(
                """
                INSERT INTO tuners (
                    module_id,
                    input_id,
                    display_number,
                    first_seen_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?)

                ON CONFLICT(
                    module_id,
                    input_id
                )
                DO UPDATE SET
                    display_number =
                        excluded.display_number
                """,
                (
                    module_id,
                    input_id,
                    input_id + 1,
                    now,
                    now,
                ),
            )

    conn.commit()


def initialize_database():
    conn = get_connection()

    try:
        create_schema(conn)

        register_equipment(conn)

        register_tuners(conn)

        return conn

    except Exception:
        conn.close()
        raise


def show_database_summary(conn):
    print()
    print("=" * 72)
    print("WISI MONITOR DATABASE")
    print("=" * 72)

    print(
        f"Database : "
        f"{DATABASE_PATH.resolve()}"
    )

    version = conn.execute(
        """
        SELECT value
        FROM schema_metadata
        WHERE key = 'schema_version'
        """
    ).fetchone()

    print(
        f"Schema   : "
        f"{version['value'] if version else 'UNKNOWN'}"
    )

    tables = [
        "chassis",
        "modules",
        "tuners",
        "tuner_samples",
        "ts_samples",
        "pids",
        "pid_samples",
        "pcr_input_samples",
        "pcr_pid_samples",
        "transport_streams",
        "services",
        "service_samples",
        "service_streams",
        "counter_state",
        "monitoring_events",
        "poll_runs",
    ]

    print()
    print("TABLE COUNTS")
    print("-" * 72)

    for table in tables:

        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM {table}
            """
        ).fetchone()

        print(
            f"{table:<30}"
            f"{row['count']:>10}"
        )

    print()
    print("REGISTERED MODULES")
    print("-" * 72)

    rows = conn.execute(
        """
        SELECT
            m.module_number,
            m.module_name,
            m.remote_ip,
            COUNT(t.id) AS tuner_count
        FROM modules m

        LEFT JOIN tuners t
            ON t.module_id = m.id

        GROUP BY
            m.id

        ORDER BY
            m.module_number
        """
    ).fetchall()

    for row in rows:

        print(
            f"Module {row['module_number']} | "
            f"{row['remote_ip']} | "
            f"Tuners: {row['tuner_count']}"
        )

    print("=" * 72)


def main():
    conn = initialize_database()

    try:
        show_database_summary(conn)

    finally:
        conn.close()


if __name__ == "__main__":
    main()