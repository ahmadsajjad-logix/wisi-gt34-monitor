from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alarm_policy import EXPECTED_ACTIVE_INPUTS
from config import DATABASE_PATH


AUDIO_STREAM_TYPES = {0x03, 0x04, 0x0F, 0x11}
VIDEO_STREAM_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24, 0x42}
AUDIO_NAME_MARKERS = (
    "audio", "aac", "ac-3", "ac3", "e-ac-3", "eac3", "dolby", "mp2", "mpeg-1 layer", "mpeg-2 layer"
)
VIDEO_NAME_MARKERS = (
    "video", "h.264", "avc", "h.265", "hevc", "mpeg-1 video", "mpeg-2 video"
)


@dataclass(frozen=True)
class ChannelMonitorResult:
    services_evaluated: int
    availability_transitions: int
    audio_tracks_evaluated: int
    audio_transitions: int
    inventory_added: int
    audio_inventory_added: int


def open_db(path: Path | str = DATABASE_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def ensure_channel_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS channel_inventory (
            service_db_id INTEGER PRIMARY KEY,
            tuner_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            service_name TEXT NOT NULL,
            baselined_at TEXT NOT NULL,
            last_inventory_seen_at TEXT NOT NULL,
            FOREIGN KEY(service_db_id) REFERENCES services(id),
            FOREIGN KEY(tuner_id) REFERENCES tuners(id)
        );

        CREATE INDEX IF NOT EXISTS idx_channel_inventory_tuner
        ON channel_inventory(tuner_id, service_id);

        CREATE TABLE IF NOT EXISTS channel_state (
            service_db_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL CHECK(state IN ('UP', 'DOWN')),
            reason TEXT NOT NULL,
            initialized_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL,
            FOREIGN KEY(service_db_id) REFERENCES services(id)
        );

        CREATE TABLE IF NOT EXISTS channel_audio_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_db_id INTEGER NOT NULL,
            pid INTEGER NOT NULL,
            stream_type INTEGER,
            stream_type_name TEXT,
            language TEXT,
            baselined_at TEXT NOT NULL,
            last_inventory_seen_at TEXT NOT NULL,
            UNIQUE(service_db_id, pid),
            FOREIGN KEY(service_db_id) REFERENCES services(id)
        );

        CREATE TABLE IF NOT EXISTS channel_audio_state (
            audio_inventory_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL CHECK(state IN ('PRESENT', 'MISSING')),
            initialized_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL,
            FOREIGN KEY(audio_inventory_id) REFERENCES channel_audio_inventory(id)
        );

        CREATE TABLE IF NOT EXISTS channel_transition_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            tuner_id INTEGER NOT NULL,
            service_db_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            service_name TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            old_state TEXT NOT NULL,
            new_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            audio_pid INTEGER,
            audio_language TEXT,
            emailed INTEGER NOT NULL DEFAULT 0 CHECK(emailed IN (0, 1)),
            emailed_at TEXT,
            FOREIGN KEY(tuner_id) REFERENCES tuners(id),
            FOREIGN KEY(service_db_id) REFERENCES services(id)
        );

        CREATE INDEX IF NOT EXISTS idx_channel_transition_pending
        ON channel_transition_events(emailed, occurred_at, tuner_id, trigger_type);
        """
    )


def is_audio_stream(row: sqlite3.Row) -> bool:
    stream_type = row["stream_type"]
    if stream_type is not None and int(stream_type) in AUDIO_STREAM_TYPES:
        return True
    label = str(row["stream_type_name"] or "").strip().lower()
    return any(marker in label for marker in AUDIO_NAME_MARKERS)


def is_video_stream(row: sqlite3.Row) -> bool:
    stream_type = row["stream_type"]
    if stream_type is not None and int(stream_type) in VIDEO_STREAM_TYPES:
        return True
    label = str(row["stream_type_name"] or "").strip().lower()
    return any(marker in label for marker in VIDEO_NAME_MARKERS)


def monitorable_service_name(
    conn: sqlite3.Connection,
    *,
    service_db_id: int,
    service_id: int,
    service_name: str | None,
) -> str | None:
    """Return a monitorable display name, or None for non-A/V services.

    Named services remain monitorable exactly as signalled.  If SDT/name
    metadata is absent, a service is admitted only when its collected
    elementary streams contain at least one recognizable audio or video
    stream.  This prevents unnamed signalling/data-only services from being
    promoted to TV-channel inventory merely because a SID exists.
    """
    name = str(service_name or "").strip()
    if name:
        return name

    streams = conn.execute(
        """
        SELECT stream_type, stream_type_name
        FROM service_streams
        WHERE service_id = ?
        """,
        (service_db_id,),
    ).fetchall()

    if any(is_audio_stream(row) or is_video_stream(row) for row in streams):
        return f"Unnamed service (SID {service_id})"

    return None


def transition_service_name(
    conn: sqlite3.Connection,
    *,
    tuner_id: int,
    service_id: int,
    service_name: str,
) -> str:
    """Make transition/email labels unambiguous without changing identity.

    When more than one service on the same tuner has the same displayed name,
    append the MPEG service ID.  Canonical identity remains service_db_id/SID.
    """
    name = str(service_name).strip()
    duplicates = conn.execute(
        """
        SELECT COUNT(*)
        FROM channel_inventory
        WHERE tuner_id = ?
          AND LOWER(TRIM(service_name)) = LOWER(TRIM(?))
        """,
        (tuner_id, name),
    ).fetchone()[0]

    if int(duplicates) > 1:
        return f"{name} (SID {service_id})"
    return name


def latest_tuners(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest AS (
            SELECT tuner_id, MAX(id) AS sample_id
            FROM tuner_samples
            GROUP BY tuner_id
        )
        SELECT
            m.module_number,
            t.id AS tuner_id,
            t.input_id,
            s.sampled_at,
            s.lock_state,
            x.current_bitrate_bps
        FROM latest l
        JOIN tuner_samples s ON s.id = l.sample_id
        JOIN tuners t ON t.id = s.tuner_id
        JOIN modules m ON m.id = t.module_id
        LEFT JOIN ts_samples x
          ON x.tuner_id = t.id AND x.sampled_at = s.sampled_at
        ORDER BY m.module_number, t.input_id
        """
    ).fetchall()


def ensure_service_inventory(
    conn: sqlite3.Connection,
    *,
    tuner_id: int,
    sampled_at: str,
) -> tuple[int, int]:
    """Persist service/audio baselines without generating transition events."""
    services = conn.execute(
        """
        SELECT id, service_id, service_name, last_seen_at
        FROM services
        WHERE tuner_id = ?
        ORDER BY service_id, id
        """,
        (tuner_id,),
    ).fetchall()

    added = 0
    audio_added = 0

    for svc in services:
        service_db_id = int(svc["id"])
        service_id = int(svc["service_id"])
        name = monitorable_service_name(
            conn,
            service_db_id=service_db_id,
            service_id=service_id,
            service_name=svc["service_name"],
        )
        if name is None:
            continue

        cur = conn.execute(
            "SELECT 1 FROM channel_inventory WHERE service_db_id = ?",
            (service_db_id,),
        ).fetchone()
        if cur is None:
            conn.execute(
                """
                INSERT INTO channel_inventory(
                    service_db_id, tuner_id, service_id, service_name,
                    baselined_at, last_inventory_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    service_db_id, tuner_id, service_id, name,
                    sampled_at, str(svc["last_seen_at"]),
                ),
            )
            added += 1
        else:
            conn.execute(
                """
                UPDATE channel_inventory
                SET service_name = ?, last_inventory_seen_at = ?
                WHERE service_db_id = ?
                """,
                (name, str(svc["last_seen_at"]), service_db_id),
            )

        streams = conn.execute(
            """
            SELECT id, pid, stream_type, stream_type_name, language, last_seen_at
            FROM service_streams
            WHERE service_id = ?
            ORDER BY pid, id
            """,
            (service_db_id,),
        ).fetchall()
        for stream in streams:
            if not is_audio_stream(stream):
                continue
            existing = conn.execute(
                """
                SELECT id FROM channel_audio_inventory
                WHERE service_db_id = ? AND pid = ?
                """,
                (service_db_id, int(stream["pid"])),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO channel_audio_inventory(
                        service_db_id, pid, stream_type, stream_type_name,
                        language, baselined_at, last_inventory_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        service_db_id,
                        int(stream["pid"]),
                        stream["stream_type"],
                        stream["stream_type_name"],
                        stream["language"],
                        sampled_at,
                        str(stream["last_seen_at"]),
                    ),
                )
                audio_added += 1
            else:
                conn.execute(
                    """
                    UPDATE channel_audio_inventory
                    SET stream_type = ?, stream_type_name = ?, language = ?,
                        last_inventory_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        stream["stream_type"], stream["stream_type_name"],
                        stream["language"], str(stream["last_seen_at"]),
                        int(existing["id"]),
                    ),
                )

    return added, audio_added


def insert_transition(
    conn: sqlite3.Connection,
    *,
    occurred_at: str,
    tuner_id: int,
    service_db_id: int,
    service_id: int,
    service_name: str,
    trigger_type: str,
    old_state: str,
    new_state: str,
    reason: str,
    audio_pid: int | None = None,
    audio_language: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO channel_transition_events(
            occurred_at, tuner_id, service_db_id, service_id, service_name,
            trigger_type, old_state, new_state, reason,
            audio_pid, audio_language, emailed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            occurred_at, tuner_id, service_db_id, service_id, service_name,
            trigger_type, old_state, new_state, reason,
            audio_pid, audio_language,
        ),
    )


def evaluate_service_availability(
    conn: sqlite3.Connection,
    *,
    tuner: sqlite3.Row,
) -> tuple[int, int]:
    tuner_id = int(tuner["tuner_id"])
    sampled_at = str(tuner["sampled_at"])
    lock_state = tuner["lock_state"]
    bitrate = tuner["current_bitrate_bps"]

    inventory = conn.execute(
        """
        SELECT i.*, s.last_seen_at
        FROM channel_inventory i
        JOIN services s ON s.id = i.service_db_id
        WHERE i.tuner_id = ?
        ORDER BY i.service_id, i.service_db_id
        """,
        (tuner_id,),
    ).fetchall()

    evaluated = 0
    transitions = 0

    for svc in inventory:
        evaluated += 1
        present = str(svc["last_seen_at"]) == sampled_at

        if lock_state != 1:
            new_state = "DOWN"
            reason = "carrier_unlocked"
        elif bitrate is None or float(bitrate) <= 0:
            new_state = "DOWN"
            reason = "transport_stream_down"
        elif not present:
            new_state = "DOWN"
            reason = "service_missing"
        else:
            new_state = "UP"
            reason = "service_present"

        state = conn.execute(
            "SELECT * FROM channel_state WHERE service_db_id = ?",
            (int(svc["service_db_id"]),),
        ).fetchone()

        if state is None:
            # First observation establishes the baseline and deliberately does
            # not generate email/event traffic.
            conn.execute(
                """
                INSERT INTO channel_state(
                    service_db_id, state, reason, initialized_at,
                    last_observed_at, last_changed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(svc["service_db_id"]), new_state, reason,
                    sampled_at, sampled_at, sampled_at,
                ),
            )
            continue

        old_state = str(state["state"])
        if old_state != new_state:
            insert_transition(
                conn,
                occurred_at=sampled_at,
                tuner_id=tuner_id,
                service_db_id=int(svc["service_db_id"]),
                service_id=int(svc["service_id"]),
                service_name=transition_service_name(
                    conn,
                    tuner_id=tuner_id,
                    service_id=int(svc["service_id"]),
                    service_name=str(svc["service_name"]),
                ),
                trigger_type="service_availability",
                old_state=old_state,
                new_state=new_state,
                reason=reason,
            )
            transitions += 1
            conn.execute(
                """
                UPDATE channel_state
                SET state = ?, reason = ?, last_observed_at = ?, last_changed_at = ?
                WHERE service_db_id = ?
                """,
                (new_state, reason, sampled_at, sampled_at, int(svc["service_db_id"])),
            )
        else:
            conn.execute(
                """
                UPDATE channel_state
                SET reason = ?, last_observed_at = ?
                WHERE service_db_id = ?
                """,
                (reason, sampled_at, int(svc["service_db_id"])),
            )

    return evaluated, transitions


def evaluate_audio_tracks(
    conn: sqlite3.Connection,
    *,
    tuner: sqlite3.Row,
) -> tuple[int, int]:
    tuner_id = int(tuner["tuner_id"])
    sampled_at = str(tuner["sampled_at"])
    lock_state = tuner["lock_state"]
    bitrate = tuner["current_bitrate_bps"]

    # Audio-track absence is evaluated only while the carrier, TS and service
    # are available. Carrier/service outages are handled by availability state.
    if lock_state != 1 or bitrate is None or float(bitrate) <= 0:
        return 0, 0

    rows = conn.execute(
        """
        SELECT
            a.id AS audio_inventory_id,
            a.service_db_id,
            a.pid,
            a.language,
            i.service_id,
            i.service_name,
            s.last_seen_at AS service_last_seen,
            ss.last_seen_at AS stream_last_seen
        FROM channel_audio_inventory a
        JOIN channel_inventory i ON i.service_db_id = a.service_db_id
        JOIN services s ON s.id = a.service_db_id
        LEFT JOIN service_streams ss
          ON ss.service_id = a.service_db_id AND ss.pid = a.pid
        WHERE i.tuner_id = ?
        ORDER BY i.service_id, a.pid
        """,
        (tuner_id,),
    ).fetchall()

    evaluated = 0
    transitions = 0

    for row in rows:
        # Do not independently alert on audio if the service itself is absent.
        if str(row["service_last_seen"]) != sampled_at:
            continue

        evaluated += 1
        new_state = (
            "PRESENT"
            if row["stream_last_seen"] is not None
            and str(row["stream_last_seen"]) == sampled_at
            else "MISSING"
        )

        state = conn.execute(
            "SELECT * FROM channel_audio_state WHERE audio_inventory_id = ?",
            (int(row["audio_inventory_id"]),),
        ).fetchone()

        if state is None:
            conn.execute(
                """
                INSERT INTO channel_audio_state(
                    audio_inventory_id, state, initialized_at,
                    last_observed_at, last_changed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(row["audio_inventory_id"]), new_state,
                    sampled_at, sampled_at, sampled_at,
                ),
            )
            continue

        old_state = str(state["state"])
        if old_state != new_state:
            insert_transition(
                conn,
                occurred_at=sampled_at,
                tuner_id=tuner_id,
                service_db_id=int(row["service_db_id"]),
                service_id=int(row["service_id"]),
                service_name=transition_service_name(
                    conn,
                    tuner_id=tuner_id,
                    service_id=int(row["service_id"]),
                    service_name=str(row["service_name"]),
                ),
                trigger_type="audio_track",
                old_state=old_state,
                new_state=new_state,
                reason=("audio_track_missing" if new_state == "MISSING" else "audio_track_restored"),
                audio_pid=int(row["pid"]),
                audio_language=(None if row["language"] is None else str(row["language"])),
            )
            transitions += 1
            conn.execute(
                """
                UPDATE channel_audio_state
                SET state = ?, last_observed_at = ?, last_changed_at = ?
                WHERE audio_inventory_id = ?
                """,
                (new_state, sampled_at, sampled_at, int(row["audio_inventory_id"])),
            )
        else:
            conn.execute(
                """
                UPDATE channel_audio_state
                SET last_observed_at = ?
                WHERE audio_inventory_id = ?
                """,
                (sampled_at, int(row["audio_inventory_id"])),
            )

    return evaluated, transitions


def evaluate_channel_states(
    *,
    database_path: Path | str = DATABASE_PATH,
) -> ChannelMonitorResult:
    with closing(open_db(database_path)) as conn:
        ensure_channel_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            services_evaluated = 0
            availability_transitions = 0
            audio_tracks_evaluated = 0
            audio_transitions = 0
            inventory_added = 0
            audio_inventory_added = 0

            for tuner in latest_tuners(conn):
                key = (int(tuner["module_number"]), int(tuner["input_id"]))
                if key not in EXPECTED_ACTIVE_INPUTS:
                    continue

                added, a_added = ensure_service_inventory(
                    conn,
                    tuner_id=int(tuner["tuner_id"]),
                    sampled_at=str(tuner["sampled_at"]),
                )
                inventory_added += added
                audio_inventory_added += a_added

                se, st = evaluate_service_availability(conn, tuner=tuner)
                ae, at = evaluate_audio_tracks(conn, tuner=tuner)
                services_evaluated += se
                availability_transitions += st
                audio_tracks_evaluated += ae
                audio_transitions += at

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return ChannelMonitorResult(
        services_evaluated=services_evaluated,
        availability_transitions=availability_transitions,
        audio_tracks_evaluated=audio_tracks_evaluated,
        audio_transitions=audio_transitions,
        inventory_added=inventory_added,
        audio_inventory_added=audio_inventory_added,
    )
