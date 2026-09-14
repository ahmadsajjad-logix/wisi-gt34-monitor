from __future__ import annotations

import random
import socket
from dataclasses import dataclass
from typing import Any, Iterable


class SnmpError(RuntimeError):
    pass


class SnmpTimeout(SnmpError):
    pass


@dataclass(slots=True)
class VarBind:
    oid: str
    value: Any
    tag: int


def _len_bytes(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + _len_bytes(len(payload)) + payload


def _enc_int(value: int) -> bytes:
    if value == 0:
        raw = b"\x00"
    elif value > 0:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if raw[0] & 0x80:
            raw = b"\x00" + raw
    else:
        size = max(1, (value.bit_length() + 8) // 8)
        raw = value.to_bytes(size, "big", signed=True)
        while len(raw) > 1 and raw[0] == 0xFF and raw[1] & 0x80:
            raw = raw[1:]
    return _tlv(0x02, raw)


def _enc_octets(value: bytes) -> bytes:
    return _tlv(0x04, value)


def _base128(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    parts = []
    while n:
        parts.append(n & 0x7F)
        n >>= 7
    parts.reverse()
    return bytes((part | 0x80) if i < len(parts) - 1 else part for i, part in enumerate(parts))


def _enc_oid(oid: str) -> bytes:
    nums = [int(x) for x in oid.strip(".").split(".")]
    if len(nums) < 2 or nums[0] > 2 or nums[1] > 39:
        raise ValueError(f"Invalid OID: {oid}")
    payload = _base128(40 * nums[0] + nums[1]) + b"".join(_base128(n) for n in nums[2:])
    return _tlv(0x06, payload)


def _enc_null() -> bytes:
    return b"\x05\x00"


def _read_len(data: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(data):
        raise SnmpError("Truncated BER length")
    first = data[pos]
    pos += 1
    if not (first & 0x80):
        return first, pos
    count = first & 0x7F
    if count == 0 or count > 4 or pos + count > len(data):
        raise SnmpError("Invalid BER length")
    return int.from_bytes(data[pos:pos + count], "big"), pos + count


def _read_tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    if pos >= len(data):
        raise SnmpError("Truncated BER")
    tag = data[pos]
    length, p = _read_len(data, pos + 1)
    end = p + length
    if end > len(data):
        raise SnmpError("Truncated BER payload")
    return tag, data[p:end], end


def _dec_oid(payload: bytes) -> str:
    values: list[int] = []
    n = 0
    for b in payload:
        n = (n << 7) | (b & 0x7F)
        if not (b & 0x80):
            values.append(n)
            n = 0
    if n:
        raise SnmpError("Malformed OID")
    if not values:
        return ""
    first = values.pop(0)
    if first < 40:
        head = [0, first]
    elif first < 80:
        head = [1, first - 40]
    else:
        head = [2, first - 80]
    return ".".join(str(x) for x in head + values)


def _decode_value(tag: int, payload: bytes) -> Any:
    if tag == 0x02:  # INTEGER, signed
        return int.from_bytes(payload, "big", signed=True) if payload else 0
    if tag == 0x04:  # OCTET STRING
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return payload
    if tag == 0x05:
        return None
    if tag == 0x06:
        return _dec_oid(payload)
    if tag == 0x40 and len(payload) == 4:
        return ".".join(str(b) for b in payload)
    if tag in (0x41, 0x42, 0x43, 0x46):  # Counter32, Gauge32, TimeTicks, Counter64
        return int.from_bytes(payload, "big", signed=False)
    if tag == 0x44:  # Opaque
        return payload
    if tag in (0x80, 0x81, 0x82):
        return None
    return payload


def _decode_response(data: bytes) -> tuple[int, int, int, list[VarBind]]:
    tag, outer, end = _read_tlv(data, 0)
    if tag != 0x30 or end != len(data):
        raise SnmpError("Invalid SNMP message")
    pos = 0
    tag, version_raw, pos = _read_tlv(outer, pos)
    if tag != 0x02:
        raise SnmpError("Missing SNMP version")
    _version = int.from_bytes(version_raw, "big", signed=True)
    tag, _community, pos = _read_tlv(outer, pos)
    if tag != 0x04:
        raise SnmpError("Missing community")
    pdu_tag, pdu, pos = _read_tlv(outer, pos)
    if pdu_tag != 0xA2:
        raise SnmpError(f"Unexpected PDU tag 0x{pdu_tag:02x}")
    p = 0
    tag, req_raw, p = _read_tlv(pdu, p)
    request_id = int.from_bytes(req_raw, "big", signed=True)
    tag, err_raw, p = _read_tlv(pdu, p)
    error_status = int.from_bytes(err_raw, "big", signed=True)
    tag, idx_raw, p = _read_tlv(pdu, p)
    error_index = int.from_bytes(idx_raw, "big", signed=True)
    tag, vb_list, p = _read_tlv(pdu, p)
    if tag != 0x30:
        raise SnmpError("Missing varbind list")
    vbs: list[VarBind] = []
    q = 0
    while q < len(vb_list):
        tag, vb, q = _read_tlv(vb_list, q)
        if tag != 0x30:
            raise SnmpError("Invalid varbind")
        r = 0
        tag, oid_raw, r = _read_tlv(vb, r)
        if tag != 0x06:
            raise SnmpError("Varbind OID missing")
        value_tag, value_raw, r = _read_tlv(vb, r)
        vbs.append(VarBind(_dec_oid(oid_raw), _decode_value(value_tag, value_raw), value_tag))
    return request_id, error_status, error_index, vbs


class SnmpV2cClient:
    """Dependency-free SNMPv2c GET/GETNEXT client for read-only monitoring."""

    def __init__(self, host: str, community: str = "public", port: int = 161,
                 timeout: float = 2.0, retries: int = 1) -> None:
        self.host = host
        self.community = community.encode("utf-8")
        self.port = port
        self.timeout = timeout
        self.retries = retries

    def _message(self, pdu_tag: int, oids: Iterable[str], request_id: int) -> bytes:
        varbinds = b"".join(_tlv(0x30, _enc_oid(oid) + _enc_null()) for oid in oids)
        pdu = _enc_int(request_id) + _enc_int(0) + _enc_int(0) + _tlv(0x30, varbinds)
        # version=1 means SNMPv2c
        return _tlv(0x30, _enc_int(1) + _enc_octets(self.community) + _tlv(pdu_tag, pdu))

    def _request(self, pdu_tag: int, oids: list[str]) -> list[VarBind]:
        request_id = random.randint(1, 0x7FFFFFFF)
        packet = self._message(pdu_tag, oids, request_id)
        last_error: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(self.timeout)
                    sock.sendto(packet, (self.host, self.port))
                    response, _addr = sock.recvfrom(65535)
                resp_id, error_status, error_index, vbs = _decode_response(response)
                if resp_id != request_id:
                    raise SnmpError("Mismatched SNMP request-id")
                if error_status:
                    raise SnmpError(f"SNMP error-status={error_status}, index={error_index}")
                return vbs
            except socket.timeout as exc:
                last_error = exc
            except OSError as exc:
                last_error = exc
        raise SnmpTimeout(f"No SNMP response from {self.host}:{self.port}") from last_error

    def get(self, oids: list[str]) -> list[VarBind]:
        return self._request(0xA0, oids)

    def get_next(self, oid: str) -> VarBind:
        values = self._request(0xA1, [oid])
        if not values:
            raise SnmpError("Empty SNMP GETNEXT response")
        return values[0]

    def walk(self, base_oid: str, max_rows: int = 10000) -> list[VarBind]:
        base = base_oid.strip(".")
        current = base
        result: list[VarBind] = []
        for _ in range(max_rows):
            vb = self.get_next(current)
            if vb.tag == 0x82 or not (vb.oid == base or vb.oid.startswith(base + ".")):
                break
            if vb.oid == current:
                raise SnmpError(f"Agent returned non-increasing OID {vb.oid}")
            result.append(vb)
            current = vb.oid
        return result
