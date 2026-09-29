from __future__ import annotations

# Compact no-logo transmission-alert email renderer.
# Alarm timing/state is supplied by the production alarm policy.
# Satellite name/band come from validated metadata.
# Frequency/polarisation remain probe/config-derived engineering metadata.

from datetime import datetime
from html import escape

from tv43_alarm_policy_final import (
    Any,
    Condition,
    EmailMessage,
    EMAIL_FROM,
    EMAIL_TO,
    SUBJECT,
    duration_text,
)


def _metadata(meta: dict[str, Any]) -> tuple[str, str, str]:
    satellite = str(meta.get("satellite_name") or "Not configured").strip()
    band = str(meta.get("frequency_band") or "Not configured").strip()

    frequency_raw = meta.get("frequency_mhz")
    polarisation = str(meta.get("polarisation") or "").strip().upper()

    if frequency_raw in (None, ""):
        frequency_text = "Frequency not configured"
    else:
        try:
            frequency = float(frequency_raw)
            if frequency.is_integer():
                frequency_text = f"{int(frequency)} MHz"
            else:
                frequency_text = f"{frequency:g} MHz"
        except (TypeError, ValueError):
            frequency_text = f"{str(frequency_raw).strip()} MHz"

    if polarisation:
        engineering = f"{frequency_text} {polarisation}"
    else:
        engineering = frequency_text

    return satellite, band, engineering


def make_whatsapp_email(
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

    satellite, band, engineering = _metadata(meta)

    pkt = ZoneInfo("Asia/Karachi")
    timestamp = occurred_at.astimezone(pkt).strftime("%Y-%m-%d %H:%M:%S")

    if is_recovery:
        duration = max(0, int((occurred_at - started_at).total_seconds()))
        down_text = duration_text(duration)
        status_text = "✅ UP"
    else:
        down_text = ""
        status_text = "❌ DOWN"

    # Plain-text body
    text_lines = [
        f"{timestamp} PKT",
    ]

    for name in names:
        text_lines.append(f"Channel: {name}: {status_text}")

    text_lines.append(f"{satellite}, {band}, {engineering}")

    if is_recovery:
        text_lines.append(f"Total downtime: {down_text}")

    text_lines.extend([
        "",
        "Transmission Monitoring Automated Notification",
    ])

    plain_body = "\n".join(text_lines) + "\n"

    # HTML body
    channel_rows = "".join(
        f'<div style="font-size:16px;line-height:1.5;">'
        f'Channel: {escape(name)}: {status_text}'
        f'</div>'
        for name in names
    )

    downtime_row = ""
    if is_recovery:
        downtime_row = (
            f'<div style="font-size:16px;line-height:1.5;">'
            f'Total downtime: {escape(down_text)}'
            f'</div>'
        )

    html_body = (
        '<!doctype html>'
        '<html><body style="margin:0;padding:0;background:#ffffff;'
        'font-family:Arial,Helvetica,sans-serif;color:#111111;">'
        '<div style="max-width:680px;margin:0 auto;padding:28px 30px;">'

        f'<div style="font-size:16px;line-height:1.5;">'
        f'{escape(timestamp)} PKT'
        f'</div>'

        f'{channel_rows}'

        f'<div style="font-size:16px;line-height:1.5;">'
        f'{escape(satellite)}, {escape(band)}, {escape(engineering)}'
        f'</div>'

        f'{downtime_row}'

        '<div style="margin-top:14px;font-size:10px;font-style:italic;'
        'color:#555555;white-space:nowrap;">'
        'Transmission Monitoring Automated Notification'
        '</div>'

        '</div></body></html>'
    )

    msg = EmailMessage()
    msg["Subject"] = SUBJECT
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    return msg