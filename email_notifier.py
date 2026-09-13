from __future__ import annotations

import os
import smtplib
import sqlite3
import ssl
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DATABASE_PATH


SMTP_HOST = os.getenv("WISI_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("WISI_SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("WISI_SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("WISI_SMTP_PASSWORD", "")

EMAIL_FROM = os.getenv("WISI_EMAIL_FROM", SMTP_USERNAME)
EMAIL_TO = os.getenv("WISI_EMAIL_TO", "")
EMAIL_SUBJECT = "Transmission Alert"

SATELLITE_NAME = "Paksat MM1"
FREQUENCY_BAND = "C-band"

LOCAL_TIMEZONE = ZoneInfo("Asia/Karachi")


@dataclass(frozen=True)
class EmailNotificationResult:
    candidates: int
    sent: int
    skipped_already_sent: int


def open_db(path: Path | str = DATABASE_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def ensure_email_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_notifications (
            monitoring_event_id INTEGER PRIMARY KEY,
            sent_at TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            FOREIGN KEY(monitoring_event_id)
                REFERENCES monitoring_events(id)
        )
        """
    )


def latest_service_name(
    conn: sqlite3.Connection,
    tuner_id: int,
    occurred_at: str,
) -> str:
    rows = conn.execute(
        """
        SELECT
            service_name,
            service_id,
            last_seen_at
        FROM services
        WHERE tuner_id = ?
          AND service_name IS NOT NULL
          AND TRIM(service_name) <> ''
          AND last_seen_at <= ?
        ORDER BY
            last_seen_at DESC,
            service_id ASC,
            id ASC
        """,
        (tuner_id, occurred_at),
    ).fetchall()

    if not rows:
        # Fall back to newest known service for the tuner. This is useful when
        # a lock-loss event occurs just after service metadata stopped updating.
        rows = conn.execute(
            """
            SELECT
                service_name,
                service_id,
                last_seen_at
            FROM services
            WHERE tuner_id = ?
              AND service_name IS NOT NULL
              AND TRIM(service_name) <> ''
            ORDER BY
                last_seen_at DESC,
                service_id ASC,
                id ASC
            """,
            (tuner_id,),
        ).fetchall()

    if not rows:
        return "Unknown"

    newest_seen = str(rows[0]["last_seen_at"])
    newest_rows = [
        row
        for row in rows
        if str(row["last_seen_at"]) == newest_seen
    ]

    names: list[str] = []

    for row in newest_rows:
        name = str(row["service_name"]).strip()
        if name and name not in names:
            names.append(name)

    return ", ".join(names) if names else "Unknown"


def event_status(row: sqlite3.Row) -> str:
    metric = str(row["metric"])
    category = str(row["category"])
    message = str(row["message"])

    is_recovery = (
        category == "recovery"
        or message.startswith("ALARM RECOVERY:")
    )

    if metric == "demod_lock":
        return "LOCKED" if is_recovery else "UNLOCKED"

    if metric == "ts_bitrate":
        return "LOCKED" if is_recovery else "LOCKED"

    return "UNKNOWN"


def local_timestamp(occurred_at: str) -> str:
    dt = datetime.fromisoformat(occurred_at)

    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=ZoneInfo("UTC")
        )

    return dt.astimezone(
        LOCAL_TIMEZONE
    ).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


def build_message(
    *,
    occurred_at: str,
    channel_name: str,
    channel_status: str,
) -> EmailMessage:
    body = (
        f"Date/time stamp: {local_timestamp(occurred_at)}\n"
        f"Channel name: {channel_name}\n"
        f"Satellite name: {SATELLITE_NAME}\n"
        f"Frequency band: {FREQUENCY_BAND}\n"
        f"Channel status: {channel_status}\n"
    )

    message = EmailMessage()
    message["Subject"] = EMAIL_SUBJECT
    message["From"] = EMAIL_FROM
    message["To"] = EMAIL_TO
    message.set_content(body)

    return message


def send_message(message: EmailMessage) -> None:
    if not SMTP_USERNAME or not SMTP_PASSWORD or not EMAIL_TO:
        raise RuntimeError(
            "SMTP configuration is incomplete. Set WISI_SMTP_USERNAME, "
            "WISI_SMTP_PASSWORD and WISI_EMAIL_TO in the environment."
        )

    context = ssl.create_default_context()

    with smtplib.SMTP(
        SMTP_HOST,
        SMTP_PORT,
        timeout=30,
    ) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(
            SMTP_USERNAME,
            SMTP_PASSWORD,
        )
        smtp.send_message(message)


def transition_events_for_sample_times(
    conn: sqlite3.Connection,
    sample_times: set[str],
) -> list[sqlite3.Row]:
    if not sample_times:
        return []

    placeholders = ",".join(
        "?"
        for _ in sample_times
    )

    return conn.execute(
        f"""
        SELECT
            e.id,
            e.occurred_at,
            e.tuner_id,
            e.category,
            e.metric,
            e.severity,
            e.message,
            m.module_number,
            t.input_id,
            t.display_number
        FROM monitoring_events e
        JOIN tuners t
          ON t.id = e.tuner_id
        JOIN modules m
          ON m.id = e.module_id
        WHERE e.occurred_at IN ({placeholders})
          AND e.metric IN ('demod_lock', 'ts_bitrate')
          AND (
                e.message LIKE 'ALARM OPEN:%'
                OR e.message LIKE 'ALARM RECOVERY:%'
              )
        ORDER BY e.id
        """,
        tuple(sorted(sample_times)),
    ).fetchall()


def send_transition_notifications(
    *,
    sample_times: set[str],
    database_path: Path | str = DATABASE_PATH,
) -> EmailNotificationResult:
    sent = 0
    skipped = 0

    with closing(
        open_db(database_path)
    ) as conn:
        ensure_email_schema(conn)
        conn.commit()

        events = transition_events_for_sample_times(
            conn,
            sample_times,
        )

        for row in events:
            event_id = int(row["id"])

            already_sent = conn.execute(
                """
                SELECT 1
                FROM email_notifications
                WHERE monitoring_event_id = ?
                """,
                (event_id,),
            ).fetchone()

            if already_sent is not None:
                skipped += 1
                continue

            channel_name = latest_service_name(
                conn,
                int(row["tuner_id"]),
                str(row["occurred_at"]),
            )

            message = build_message(
                occurred_at=str(row["occurred_at"]),
                channel_name=channel_name,
                channel_status=event_status(row),
            )

            send_message(message)

            conn.execute(
                """
                INSERT INTO email_notifications(
                    monitoring_event_id,
                    sent_at,
                    recipient,
                    subject
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    event_id,
                    datetime.now(
                        LOCAL_TIMEZONE
                    ).isoformat(
                        timespec="seconds"
                    ),
                    EMAIL_TO,
                    EMAIL_SUBJECT,
                ),
            )
            conn.commit()
            sent += 1

    return EmailNotificationResult(
        candidates=len(events),
        sent=sent,
        skipped_already_sent=skipped,
    )


# ---------------------------------------------------------------------------
# Channel/service transition notification path
# ---------------------------------------------------------------------------

def pending_channel_transition_groups(
    conn: sqlite3.Connection,
) -> list[list[sqlite3.Row]]:
    """Return unsent transitions grouped into one operational notification.

    Service-availability transitions sharing tuner/time/new state/reason are
    aggregated, so a carrier or whole-TS outage creates one email listing all
    newly affected channels rather than one email per service.
    Audio-track transitions remain service/track specific.
    """
    rows = conn.execute(
        """
        SELECT *
        FROM channel_transition_events
        WHERE emailed = 0
        ORDER BY occurred_at, tuner_id, trigger_type, id
        """
    ).fetchall()

    groups: list[list[sqlite3.Row]] = []
    group_map: dict[tuple, list[sqlite3.Row]] = {}
    order: list[tuple] = []

    for row in rows:
        if str(row["trigger_type"]) == "service_availability":
            key = (
                str(row["occurred_at"]),
                int(row["tuner_id"]),
                str(row["trigger_type"]),
                str(row["new_state"]),
                str(row["reason"]),
            )
        else:
            key = (
                str(row["occurred_at"]),
                int(row["tuner_id"]),
                str(row["trigger_type"]),
                int(row["service_db_id"]),
                row["audio_pid"],
                str(row["new_state"]),
            )
        if key not in group_map:
            group_map[key] = []
            order.append(key)
        group_map[key].append(row)

    for key in order:
        groups.append(group_map[key])
    return groups


def build_channel_transition_message(rows: list[sqlite3.Row]) -> EmailMessage:
    first = rows[0]
    trigger = str(first["trigger_type"])
    occurred_at = str(first["occurred_at"])

    if trigger == "service_availability":
        new_state = str(first["new_state"])
        reason = str(first["reason"])
        # Preserve event rows for delivery bookkeeping, but render each channel
        # name only once. This prevents duplicate names if separate historical
        # transition batches happen to share the same grouping key.
        names = list(
            dict.fromkeys(
                str(row["service_name"]).strip()
                for row in rows
                if str(row["service_name"]).strip()
            )
        )
        if len(names) == 1:
            channel_name = names[0]
        else:
            symbol = "❌" if new_state == "DOWN" else "✅"
            channel_name = "\n" + "\n".join(
                f"{symbol} {name} — {new_state}" for name in names
            )

        if new_state == "DOWN":
            if reason == "carrier_unlocked":
                status = "❌ DOWN (Carrier UNLOCKED)"
            elif reason == "transport_stream_down":
                status = "❌ DOWN (Carrier LOCKED, Transport Stream DOWN)"
            else:
                status = "❌ DOWN"
        else:
            status = "✅ UP"
    else:
        row = first
        channel_name = str(row["service_name"])
        new_state = str(row["new_state"])
        pid = row["audio_pid"]
        language = str(row["audio_language"] or "").strip()
        detail = f"PID {pid}" if pid is not None else "audio track"
        if language:
            detail += f", {language}"
        if new_state == "MISSING":
            status = f"⚠️ AUDIO TRACK MISSING ({detail})"
        else:
            status = f"✅ AUDIO TRACK RESTORED ({detail})"

    body = (
        f"Date/time stamp: {local_timestamp(occurred_at)}\n"
        f"Channel name: {channel_name}\n"
        f"Satellite name: {SATELLITE_NAME}\n"
        f"Frequency band: {FREQUENCY_BAND}\n"
        f"Channel status: {status}\n\n"
        "Automated transmission monitoring notification.\n"
    )
    message = EmailMessage()
    message["Subject"] = EMAIL_SUBJECT
    message["From"] = EMAIL_FROM
    message["To"] = EMAIL_TO
    message.set_content(body)
    return message


def send_channel_transition_notifications(
    *,
    database_path: Path | str = DATABASE_PATH,
) -> EmailNotificationResult:
    sent = 0
    skipped = 0
    candidates = 0

    with closing(open_db(database_path)) as conn:
        # channel_monitor creates this table; fail closed if that stage has not
        # yet initialized its schema.
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_transition_events'"
        ).fetchone()
        if table is None:
            return EmailNotificationResult(0, 0, 0)

        groups = pending_channel_transition_groups(conn)
        candidates = len(groups)
        for rows in groups:
            message = build_channel_transition_message(rows)
            send_message(message)
            sent_at = datetime.now(LOCAL_TIMEZONE).isoformat(timespec="seconds")
            ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE channel_transition_events SET emailed = 1, emailed_at = ? WHERE id IN ({placeholders})",
                (sent_at, *ids),
            )
            conn.commit()
            sent += 1

    return EmailNotificationResult(
        candidates=candidates,
        sent=sent,
        skipped_already_sent=skipped,
    )
