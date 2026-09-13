from __future__ import annotations

from contextlib import closing

from alarm_persistence import ensure_alarm_state_schema
from channel_monitor import ensure_channel_schema
from database import initialize_database
from email_notifier import ensure_email_schema


def main() -> int:
    conn = initialize_database()
    try:
        ensure_alarm_state_schema(conn)
        ensure_channel_schema(conn)
        ensure_email_schema(conn)
        conn.commit()
    finally:
        conn.close()

    print("WISI GT34 runtime schema initialized successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
