from __future__ import annotations

"""WISI Tangram native Transport Stream Monitor event reader.

Observed chassis gateway protocol:
POST http://<host>/data.xmlc?size=N
body: one resource per line followed by END.

This module discovers GT34 logical remotes from status.xmlc (product=GT34,
slotaddress), reads tsdb/monitor/log.xmlc, and decodes WISI date/time values.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from urllib.request import Request, build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar
import xml.etree.ElementTree as ET

DAY_FRACTION = 2**32
DATE_EPOCH = datetime(1900, 1, 1)
REMOTE_CANDIDATES = tuple(f"203_0_113_{n}" for n in range(21, 27))


@dataclass(frozen=True)
class NativeEvent:
    event_type: int
    description: str
    input_id: int
    active: bool
    started_at_local: datetime
    ended_at_local: datetime | None

    @property
    def duration_seconds(self) -> float | None:
        if self.ended_at_local is None:
            return None
        return max(0.0, (self.ended_at_local - self.started_at_local).total_seconds())


def decode_wisi_datetime(date_value: int, time_value: int) -> datetime | None:
    if date_value <= 0 or time_value < 0:
        return None
    seconds = (time_value / DAY_FRACTION) * 86400.0
    return DATE_EPOCH + timedelta(days=date_value, seconds=seconds)


def _post_batch(host: str, resources: list[str], timeout: float = 5.0) -> str:
    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    # Establish the same lightweight session pattern used by the WISI web UI.
    try:
        opener.open(Request(f"http://{host}/", headers={"User-Agent": "Mozilla/5.0"}), timeout=timeout).read()
    except Exception:
        pass
    payload = ("\r\n".join(resources) + "\r\nEND").encode("utf-8")
    req = Request(
        f"http://{host}/data.xmlc?size={len(resources)}",
        data=payload,
        method="POST",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": f"http://{host}/",
            "Origin": f"http://{host}",
            "Content-Type": "text/plain; charset=UTF-8",
            "Connection": "close",
        },
    )
    with opener.open(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


@lru_cache(maxsize=16)
def discover_gt34_remotes(host: str) -> dict[int, str]:
    """Return {physical_slot: logical_remote} using device-reported status.xmlc."""
    resources = [f"remote/{r}/status.xmlc" for r in REMOTE_CANDIDATES]
    root = ET.fromstring(_post_batch(host, resources))
    result: dict[int, str] = {}
    for reply in root.findall(".//reply[@ip]"):
        remote = reply.get("ip", "")
        uixml = reply.find("./file/uixml")
        if uixml is None:
            continue
        product = uixml.find("product")
        slot = uixml.findtext("slotaddress")
        if product is not None and product.get("name") == "GT34" and slot:
            result[int(slot)] = remote
    return result


def parse_log_reply(xml_text: str) -> list[NativeEvent]:
    root = ET.fromstring(xml_text)
    events: list[NativeEvent] = []
    for node in root.findall(".//entry"):
        start = decode_wisi_datetime(int(node.get("start_date", "0")), int(node.get("start_time", "0")))
        if start is None:
            continue
        end_date = int(node.get("end_date", "0"))
        end_time = int(node.get("end_time", "0"))
        end = decode_wisi_datetime(end_date, end_time) if end_date > 0 else None
        events.append(NativeEvent(
            event_type=int(node.get("type", "0")),
            description=(node.text or "").strip(),
            input_id=int(node.get("input_id", "-1")),
            active=node.get("active", "false").lower() == "true",
            started_at_local=start,
            ended_at_local=end,
        ))
    return events


def fetch_log(host: str, logical_remote: str, *, start: int = 0, length: int = 100, draw: int = 1, timeout: float = 5.0) -> list[NativeEvent]:
    resource = f"remote/{logical_remote}/tsdb/monitor/log.xmlc?start={start}&length={length}&draw={draw}"
    return parse_log_reply(_post_batch(host, [resource], timeout=timeout))


def fetch_slot_log(host: str, module: int, *, length: int = 100, timeout: float = 5.0) -> list[NativeEvent]:
    remote = discover_gt34_remotes(host).get(int(module))
    if not remote:
        raise RuntimeError(f"No GT34 logical remote discovered for {host} slot {module}")
    return fetch_log(host, remote, length=length, timeout=timeout)


def tuner_unlock_events(events: list[NativeEvent], channel: int) -> list[NativeEvent]:
    # WISI TS-monitor input_id is zero-based; project logical channel is one-based.
    input_id = int(channel) - 1
    return sorted(
        [e for e in events if e.input_id == input_id and e.event_type == 186 and e.description == "Tuner unlocked"],
        key=lambda e: e.started_at_local,
        reverse=True,
    )
