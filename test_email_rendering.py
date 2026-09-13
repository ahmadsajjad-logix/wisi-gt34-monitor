from __future__ import annotations

import sqlite3

from email_notifier import build_channel_transition_message


def make_row(conn: sqlite3.Connection, **values) -> sqlite3.Row:
    cols = [
        "id", "occurred_at", "tuner_id", "service_db_id", "service_id",
        "service_name", "trigger_type", "old_state", "new_state", "reason",
        "audio_pid", "audio_language", "emailed", "emailed_at",
    ]
    select = "SELECT " + ", ".join(f'? AS "{col}"' for col in cols)
    return conn.execute(select, tuple(values.get(col) for col in cols)).fetchone()


def main() -> int:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        common = dict(
            occurred_at="2026-09-13T10:08:22+00:00",
            tuner_id=14,
            trigger_type="service_availability",
            old_state="DOWN",
            new_state="UP",
            reason="service_present",
            audio_pid=None,
            audio_language=None,
            emailed=0,
            emailed_at=None,
        )
        names = ["AAN TV - HD", "M-News", "M-World", "M-Sports", "ME", "PLANET-6"]
        rows = []
        event_id = 1
        for repeat in range(2):
            for sid, name in enumerate(names, start=1):
                rows.append(make_row(
                    conn,
                    id=event_id,
                    service_db_id=100 + sid,
                    service_id=sid,
                    service_name=name,
                    **common,
                ))
                event_id += 1

        body = build_channel_transition_message(rows).get_content()
        for name in names:
            marker = f"✅ {name} — UP"
            count = body.count(marker)
            assert count == 1, f"Expected one rendered row for {name}, got {count}"

        assert "Channel status: ✅ UP" in body
        print("EMAIL RENDER DEDUP TEST: PASS")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
