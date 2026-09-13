from __future__ import annotations

import argparse
import csv
import json
import logging
from html import escape
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from email_notifier import EMAIL_FROM, EMAIL_TO, send_message

POLICY_DB = ROOT / "database" / "tv43_alarm_policy.sqlite3"
SNAPSHOT_DIR = ROOT / "state" / "tv43_alarm_snapshots"
MANIFEST = ROOT / "prtg_tv43_deployment" / "tv43_created_sensors.csv"
LOG_PATH = ROOT / "logs" / "tv43_alarm_policy.log"

CHECK_INTERVAL_SECONDS = 5
SNAPSHOT_STALE_SECONDS = 120
SUBJECT = "Transmission Alert"


@dataclass(frozen=True)
class Condition:
    kind: str
    event_key: str
    affected: tuple[tuple[int, str], ...]
    status_line: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_time(dt: datetime) -> str:
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo("Asia/Karachi")).strftime(
        "%Y-%m-%d %H:%M:%S PKT"
    )


def duration_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def configure_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("tv43_alarm_policy")
    log.setLevel(logging.INFO)
    log.propagate = False
    if log.handlers:
        return log

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    fh = RotatingFileHandler(
        LOG_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


def open_db() -> sqlite3.Connection:
    POLICY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(POLICY_DB), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alert_episode (
            event_key TEXT PRIMARY KEY,
            host TEXT NOT NULL,
            module INTEGER NOT NULL,
            channel INTEGER NOT NULL,
            condition_kind TEXT NOT NULL,
            active INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            cleared_at TEXT,
            affected_json TEXT NOT NULL,
            status_line TEXT NOT NULL,
            alarm_email_sent_at TEXT,
            recovery_email_sent_at TEXT
        );

        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            cleared_at TEXT,
            duration_seconds INTEGER,
            condition_kind TEXT NOT NULL,
            affected_json TEXT NOT NULL,
            status_line TEXT NOT NULL
        );
        """
    )
    conn.commit()


def load_manifest() -> dict[str, dict[str, Any]]:
    if not MANIFEST.exists():
        raise FileNotFoundError(f"TV43 deployment state missing: {MANIFEST}")
    rows: dict[str, dict[str, Any]] = {}
    with MANIFEST.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            host = str(row["host"])
            module = int(row["module"])
            channel = int(row["channel"])
            key = f"{host}|M{module}C{channel}"
            rows[key] = {
                "host": host,
                "module": module,
                "channel": channel,
                "carrier_name": str(row["sensor_name"]),
            }
    if len(rows) != 43:
        raise RuntimeError(f"Expected 43 TV43 carriers, found {len(rows)}")
    return rows


def snapshot_path(meta: dict[str, Any]) -> Path:
    return SNAPSHOT_DIR / (
        f"{meta['host'].replace('.', '_')}_M{meta['module']}C{meta['channel']}.json"
    )


def read_snapshot(meta: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return json.loads(snapshot_path(meta).read_text(encoding="utf-8"))
    except Exception:
        return None


def services(snapshot: dict[str, Any], field: str) -> tuple[tuple[int, str], ...]:
    result = []
    for row in snapshot.get(field, []) or []:
        result.append((int(row["sid"]), str(row["name"])))
    return tuple(result)


def derive_conditions(snapshot: dict[str, Any]) -> list[Condition]:
    ch = dict(snapshot.get("channels") or {})
    host = str(snapshot["host"])
    module = int(snapshot["module"])
    channel = int(snapshot["channel"])
    prefix = f"{host}|M{module}C{channel}"

    expected = services(snapshot, "expected_services")
    missing = services(snapshot, "missing_services")
    es_missing = services(snapshot, "es_missing_services")

    if snapshot.get("execution_error"):
        return [
            Condition(
                kind="execution_failure",
                event_key=f"{prefix}|execution_failure",
                affected=expected,
                status_line="❌ Monitoring execution failure",
            )
        ]

    locked = int(ch.get("Demod Lock", 0) or 0) == 1
    ts_up = int(ch.get("Transport Stream Present", 0) or 0) == 1

    # CONDITION 1: carrier lost -> all channels on carrier DOWN.
    if not locked:
        return [
            Condition(
                kind="carrier_unlocked",
                event_key=f"{prefix}|carrier_unlocked",
                affected=expected,
                status_line="❌ Carrier UNLOCKED",
            )
        ]

    # CONDITION 2: carrier locked but TS unavailable -> all channels DOWN.
    if not ts_up:
        return [
            Condition(
                kind="transport_stream_down",
                event_key=f"{prefix}|transport_stream_down",
                affected=expected,
                status_line="❌ Carrier LOCKED + Transport Stream DOWN",
            )
        ]

    # CONDITION 3: carrier/TS work; only the failed individual services DOWN.
    down_by_sid: dict[int, str] = {}
    for sid, name in missing:
        down_by_sid[sid] = name
    for sid, name in es_missing:
        down_by_sid[sid] = name

    result: list[Condition] = []
    for sid, name in sorted(down_by_sid.items()):
        result.append(
            Condition(
                kind="individual_service_down",
                event_key=f"{prefix}|service_down|SID{sid}",
                affected=((sid, name),),
                status_line=f"❌ {name} — DOWN",
            )
        )
    return result


def make_email(
    *,
    meta: dict[str, Any],
    condition: Condition,
    event_type: str,
    started_at: datetime,
    occurred_at: datetime,
) -> EmailMessage:
    from zoneinfo import ZoneInfo

    is_recovery = event_type != "ALARM"
    names = [name for _, name in condition.affected] or ["Unknown"]
    pkt = ZoneInfo("Asia/Karachi")

    local_occurred = occurred_at.astimezone(pkt)
    local_started = started_at.astimezone(pkt)
    timestamp = local_occurred.strftime("%Y-%m-%d %H:%M:%S")

    if is_recovery:
        duration = max(0, int((occurred_at - started_at).total_seconds()))
        down_text = duration_text(duration)
        status_text = "✅ UP"
    else:
        down_text = ""
        status_text = "❌ DOWN"

    text_lines = [
        "Transmission Alert",
        "",
        f"Date/time stamp: {timestamp}",
        "PKT",
        "",
    ]

    for idx, name in enumerate(names):
        if idx:
            text_lines.append("")
        text_lines.extend(
            [
                f"Channel name: {name}",
                "Satellite name: Paksat MM1",
                "Frequency band: C-band",
                f"Channel status: {status_text}",
            ]
        )
        if is_recovery:
            text_lines.append(f"Total downtime: {down_text}")

    text_lines.extend(["", "Automated transmission monitoring notification."])
    plain_body = "\n".join(text_lines) + "\n"

    blocks: list[str] = []
    for name in names:
        recovery_row = ""
        if is_recovery:
            recovery_row = (
                '<tr><td style="padding:7px 0;font-weight:600;">Total downtime:</td>'
                f'<td style="padding:7px 0;">{escape(down_text)}</td></tr>'
            )

        blocks.append(
            '<table role="presentation" '
            'style="border-collapse:collapse;width:100%;font-size:16px;'
            'line-height:1.5;margin-top:8px;">'
            f'<tr><td style="padding:7px 0;width:180px;font-weight:600;">Channel name:</td>'
            f'<td style="padding:7px 0;">{escape(name)}</td></tr>'
            '<tr><td style="padding:7px 0;font-weight:600;">Satellite name:</td>'
            '<td style="padding:7px 0;">Paksat MM1</td></tr>'
            '<tr><td style="padding:7px 0;font-weight:600;">Frequency band:</td>'
            '<td style="padding:7px 0;">C-band</td></tr>'
            '<tr><td style="padding:7px 0;font-weight:600;">Channel status:</td>'
            f'<td style="padding:7px 0;font-weight:700;">{status_text}</td></tr>'
            f'{recovery_row}'
            '</table>'
        )

    recovery_times = ""

    logo_path = ROOT / "PEMRA_Logo.png"
    logo_html = ""
    if logo_path.exists():
        logo_html = (
            '<img src="cid:pemra-logo" alt="PEMRA" '
            'style="display:inline-block;width:24px;height:24px;'
            'object-fit:contain;vertical-align:middle;margin:0 8px 0 0;">'
        )

    html_body = (
        '<!doctype html><html><body '
        'style="margin:0;padding:0;background:#ffffff;'
        'font-family:Arial,Helvetica,sans-serif;color:#111111;">'
        '<div style="max-width:680px;margin:0 auto;padding:28px 30px;">'
        '<div style="font-size:28px;font-weight:700;margin-bottom:22px;">'
        'Transmission Alert</div>'
        '<div style="font-size:16px;line-height:1.5;margin-bottom:18px;">'
        f'<div><strong>Date/time stamp:</strong> {timestamp}</div>'
        '<div>PKT</div>'
        '</div>'
        + "".join(blocks)
        + recovery_times
        + '<div style="margin-top:34px;font-size:13px;'
          'font-style:italic;color:#555555;white-space:nowrap;">'
        + logo_html
        + '<span style="vertical-align:middle;">'
          'Automated transmission monitoring notification.</span>'
          '</div></div></body></html>'
    )

    msg = EmailMessage()
    msg["Subject"] = SUBJECT
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    if logo_path.exists():
        html_part = msg.get_payload()[-1]
        html_part.add_related(
            logo_path.read_bytes(),
            maintype="image",
            subtype="png",
            cid="<pemra-logo>",
            filename="PEMRA_Logo.png",
            disposition="inline",
        )

    return msg


def insert_history(
    conn: sqlite3.Connection,
    *,
    condition: Condition,
    event_type: str,
    occurred_at: datetime,
    started_at: datetime,
    cleared_at: datetime | None = None,
) -> None:
    duration = (
        None
        if cleared_at is None
        else max(0, int((cleared_at - started_at).total_seconds()))
    )
    conn.execute(
        """
        INSERT INTO alert_history(
            event_key,event_type,occurred_at,started_at,cleared_at,
            duration_seconds,condition_kind,affected_json,status_line
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            condition.event_key,
            event_type,
            occurred_at.isoformat(),
            started_at.isoformat(),
            None if cleared_at is None else cleared_at.isoformat(),
            duration,
            condition.kind,
            json.dumps(condition.affected, ensure_ascii=False),
            condition.status_line,
        ),
    )


def process_snapshot(
    conn: sqlite3.Connection,
    log: logging.Logger,
    key: str,
    meta: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    observed_at = parse_dt(str(snapshot["observed_at"]))
    age = int((utcnow() - observed_at).total_seconds())
    if age > SNAPSHOT_STALE_SECONDS:
        log.warning("Stale snapshot %s age=%ss; no state transition inferred", key, age)
        return

    current = {c.event_key: c for c in derive_conditions(snapshot)}

    active_rows = conn.execute(
        """
        SELECT * FROM alert_episode
        WHERE host=? AND module=? AND channel=? AND active=1
        """,
        (meta["host"], meta["module"], meta["channel"]),
    ).fetchall()
    active = {str(row["event_key"]): row for row in active_rows}

    # New alarm condition: email immediately on first detection.
    for event_key, condition in current.items():
        row = active.get(event_key)
        if row is None:
            started_at = observed_at
            msg = make_email(
                meta=meta,
                condition=condition,
                event_type="ALARM",
                started_at=started_at,
                occurred_at=observed_at,
            )
            send_message(msg)

            conn.execute(
                """
                INSERT INTO alert_episode(
                    event_key,host,module,channel,condition_kind,active,
                    started_at,last_seen_at,cleared_at,affected_json,status_line,
                    alarm_email_sent_at,recovery_email_sent_at
                )
                VALUES(?,?,?,?,?,1,?,?,NULL,?,?,?,NULL)
                ON CONFLICT(event_key) DO UPDATE SET
                    active=1,
                    condition_kind=excluded.condition_kind,
                    started_at=excluded.started_at,
                    last_seen_at=excluded.last_seen_at,
                    cleared_at=NULL,
                    affected_json=excluded.affected_json,
                    status_line=excluded.status_line,
                    alarm_email_sent_at=excluded.alarm_email_sent_at,
                    recovery_email_sent_at=NULL
                """,
                (
                    event_key,
                    meta["host"],
                    meta["module"],
                    meta["channel"],
                    condition.kind,
                    started_at.isoformat(),
                    observed_at.isoformat(),
                    json.dumps(condition.affected, ensure_ascii=False),
                    condition.status_line,
                    utcnow().isoformat(),
                ),
            )
            insert_history(
                conn,
                condition=condition,
                event_type="ALARM",
                occurred_at=observed_at,
                started_at=started_at,
            )
            log.error("ALARM EMAIL SENT IMMEDIATELY | %s | %s", event_key, condition.status_line)
        else:
            # Same alarm still active: refresh only; never repeat email.
            conn.execute(
                """
                UPDATE alert_episode
                SET last_seen_at=?,affected_json=?,status_line=?
                WHERE event_key=?
                """,
                (
                    observed_at.isoformat(),
                    json.dumps(condition.affected, ensure_ascii=False),
                    condition.status_line,
                    event_key,
                ),
            )

    # Previously active condition no longer exists -> immediate recovery email.
    for event_key, row in active.items():
        if event_key in current:
            continue

        started_at = parse_dt(str(row["started_at"]))
        affected = tuple(
            (int(item[0]), str(item[1]))
            for item in json.loads(str(row["affected_json"]))
        )
        condition = Condition(
            kind=str(row["condition_kind"]),
            event_key=event_key,
            affected=affected,
            status_line=str(row["status_line"]),
        )
        msg = make_email(
            meta=meta,
            condition=condition,
            event_type="RECOVERY",
            started_at=started_at,
            occurred_at=observed_at,
        )
        send_message(msg)

        conn.execute(
            """
            UPDATE alert_episode
            SET active=0,cleared_at=?,recovery_email_sent_at=?,last_seen_at=?
            WHERE event_key=?
            """,
            (
                observed_at.isoformat(),
                utcnow().isoformat(),
                observed_at.isoformat(),
                event_key,
            ),
        )
        insert_history(
            conn,
            condition=condition,
            event_type="RECOVERY",
            occurred_at=observed_at,
            started_at=started_at,
            cleared_at=observed_at,
        )
        log.info("RECOVERY EMAIL SENT IMMEDIATELY | %s", event_key)


def run_once(log: logging.Logger, *, strict: bool = False) -> int:
    manifest = load_manifest()
    failures = 0
    with closing(open_db()) as conn:
        ensure_schema(conn)
        for key, meta in manifest.items():
            snapshot = read_snapshot(meta)
            if snapshot is None:
                continue
            try:
                process_snapshot(conn, log, key, meta, snapshot)
                conn.commit()
            except Exception:
                failures += 1
                conn.rollback()
                log.exception("Policy processing failed | %s", key)
    if strict and failures:
        raise RuntimeError(f"{failures} alarm-policy processing failure(s)")
    return failures



def rearm_current_active_episodes(log: logging.Logger) -> int:
    """
    One-time commissioning action.

    Marks only CURRENTLY ACTIVE episodes inactive so the next strict pass sends
    one fresh alarm email for each currently detected fault. This is used once
    during final installation to prove end-to-end email delivery. Normal
    runtime behavior remains unchanged: no repeated email while an episode
    stays active.
    """
    with closing(open_db()) as conn:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT event_key FROM alert_episode WHERE active=1"
        ).fetchall()
        count = len(rows)
        if count:
            conn.execute(
                """
                UPDATE alert_episode
                SET active=0,
                    cleared_at=COALESCE(cleared_at, ?),
                    recovery_email_sent_at=COALESCE(recovery_email_sent_at, ?)
                WHERE active=1
                """,
                (utcnow().isoformat(), utcnow().isoformat()),
            )
            conn.commit()
        log.warning(
            "COMMISSIONING REARM | prior active episodes reset=%d | "
            "next pass will send one fresh alarm for each current fault",
            count,
        )
        return count

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--rearm-current", action="store_true")
    ap.add_argument("--interval", type=float, default=CHECK_INTERVAL_SECONDS)
    args = ap.parse_args()

    log = configure_logging()
    log.info("=" * 100)
    log.info("WISI GT34 TV43 ALERT POLICY V8 STARTED")
    log.info("alarm_email=IMMEDIATE_ON_DETECTION | duplicate_email=SUPPRESSED | recovery_email=IMMEDIATE")
    log.info("logic=carrier_unlocked/all_down ; locked_ts_down/all_down ; locked_ts_up/individual_down")
    log.info("=" * 100)

    if args.rearm_current:
        rearm_current_active_episodes(log)

    while True:
        run_once(log, strict=args.strict)
        if args.once:
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
