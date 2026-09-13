from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from email_notifier import send_message
from tv43_alarm_policy_final import Condition, make_email


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Send a presentation-only TV43 email test without touching "
            "alarm state."
        )
    )
    parser.add_argument(
        "--recovery",
        action="store_true",
        help="Send recovery-format test instead of DOWN-format test.",
    )
    parser.add_argument(
        "--channel-name",
        default="Geo News",
        help="Channel name shown in the test message.",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    started = now - timedelta(minutes=12, seconds=34)

    condition = Condition(
        kind="individual_service_down",
        event_key="FORMAT-TEST-ONLY",
        affected=((1050, args.channel_name),),
        status_line=f"❌ {args.channel_name} — DOWN",
    )
    meta = {
        "host": "TEST",
        "module": 0,
        "channel": 0,
        "carrier_name": "FORMAT TEST",
    }

    event_type = "RECOVERY" if args.recovery else "ALARM"
    message = make_email(
        meta=meta,
        condition=condition,
        event_type=event_type,
        started_at=started,
        occurred_at=now,
    )

    send_message(message)
    print(
        "TEST EMAIL SENT: "
        + ("RECOVERY / UP format" if args.recovery else "ALARM / DOWN format")
    )
    print(
        "No alarm episode was created, changed, rearmed, cleared, or "
        "commissioned by this test."
    )


if __name__ == "__main__":
    main()
