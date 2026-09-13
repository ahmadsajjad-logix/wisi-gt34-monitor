from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

HOST_RE = re.compile(
    r"WISI GT34 DVB SERVICE INVENTORY \| host=(?P<host>\d+\.\d+\.\d+\.\d+)"
)

HEADER_RE = re.compile(
    r"^MODULE\s+(?P<module>\d+)\s*/\s*CHANNEL\s+(?P<channel>\d+)\s*\|\s*"
    r"(?P<frequency>[0-9.]+)\s+MHz\s+(?P<polarisation>[A-Za-z]+)\s*\|\s*"
    r"(?P<symbolrate>[0-9.]+)\s+MBd\s*$"
)

INPUT_RE = re.compile(
    r"^Input name:\s*(?P<name>.*?)\s*\|\s*Lock:\s*(?P<lock>[^|]+)\s*\|\s*"
    r"TS:\s*(?P<ts>[^|]+)\s*\|\s*TSID:\s*(?P<tsid>[^|]+)\s*\|\s*Services:\s*(?P<count>\d+)\s*$"
)

SERVICE_RE = re.compile(
    r"^\s*(?P<sid>\d+)\s+(?P<status>[A-Z0-9_-]+)\s+(?P<name>.+?)\s*$",
    re.I,
)

SEPARATOR_RE = re.compile(r"^-{5,}$")

def normalize_line(line: str) -> str:
    # Discovery output may contain ANSI-free console text but with leading spaces.
    return line.rstrip("\r\n")

def read_discovery_text(path: Path) -> str:
    data = path.read_bytes()

    # Windows PowerShell 5.1 Tee-Object / redirection commonly writes UTF-16LE,
    # sometimes without a BOM. Detect BOMs first, then alternating NUL bytes.
    if data.startswith(b"\\xff\\xfe"):
        return data.decode("utf-16-le", errors="replace")
    if data.startswith(b"\\xfe\\xff"):
        return data.decode("utf-16-be", errors="replace")
    if data.startswith(b"\\xef\\xbb\\xbf"):
        return data.decode("utf-8-sig", errors="replace")

    sample = data[:8192]
    if sample:
        half = max(1, len(sample) // 2)
        odd_nuls = sample[1::2].count(0)
        even_nuls = sample[0::2].count(0)
        if odd_nuls / half > 0.20:
            return data.decode("utf-16-le", errors="replace")
        if even_nuls / half > 0.20:
            return data.decode("utf-16-be", errors="replace")

    return data.decode("utf-8", errors="replace")


def host_from_filename(path: Path) -> str:
    m = re.fullmatch(
        r"services_(\\d+)_(\\d+)_(\\d+)_(\\d+)\\.txt",
        path.name,
        re.I,
    )
    return ".".join(m.groups()) if m else ""


def parse_one(path: Path):
    text = read_discovery_text(path)
    lines = text.splitlines()

    # Controlled filename fallback; an in-file authoritative header overrides it.
    host = host_from_filename(path)
    current = None
    carriers = []
    services = []

    for raw in lines:
        line = normalize_line(raw)
        stripped = line.strip()

        mh = HOST_RE.search(stripped)
        if mh:
            host = mh.group("host")
            continue

        mhdr = HEADER_RE.match(stripped)
        if mhdr:
            current = {
                "host": host or "",
                "module": int(mhdr.group("module")),
                "channel": int(mhdr.group("channel")),
                "frequency_mhz": float(mhdr.group("frequency")),
                "polarisation": mhdr.group("polarisation"),
                "symbol_rate_mbd": float(mhdr.group("symbolrate")),
                "input_name": "",
                "lock": "",
                "ts": "",
                "tsid": "",
                "service_count": 0,
            }
            carriers.append(current)
            continue

        if current is None:
            continue

        mi = INPUT_RE.match(stripped)
        if mi:
            current["input_name"] = mi.group("name").strip()
            current["lock"] = mi.group("lock").strip()
            current["ts"] = mi.group("ts").strip()
            current["tsid"] = mi.group("tsid").strip()
            current["service_count"] = int(mi.group("count"))
            continue

        if (
            current["service_count"] > 0
            and stripped
            and "SID" not in stripped.upper()
            and not SEPARATOR_RE.match(stripped)
        ):
            ms = SERVICE_RE.match(line)
            if ms:
                services.append({
                    "host": current["host"],
                    "module": current["module"],
                    "channel": current["channel"],
                    "frequency_mhz": current["frequency_mhz"],
                    "polarisation": current["polarisation"],
                    "symbol_rate_mbd": current["symbol_rate_mbd"],
                    "input_name": current["input_name"],
                    "sid": int(ms.group("sid")),
                    "status": ms.group("status").strip(),
                    "service_name": ms.group("name").strip(),
                })

    return carriers, services

def write_csv(path: Path, rows: list[dict], fields: list[str]):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--expect-hosts", nargs="*", default=[])
    args = ap.parse_args()

    inp = Path(args.input_dir)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    all_carriers = []
    all_services = []
    parsed_hosts = set()

    files = sorted(inp.glob("services_*.txt"))
    if not files:
        raise SystemExit("No services_*.txt files found in input directory.")

    for p in files:
        carriers, services = parse_one(p)
        all_carriers.extend(carriers)
        all_services.extend(services)
        parsed_hosts.update(c["host"] for c in carriers if c["host"])

    if args.expect_hosts:
        missing = sorted(set(args.expect_hosts) - parsed_hosts)
        if missing:
            raise SystemExit(f"Expected hosts missing from parsed inventory: {', '.join(missing)}")

    if not all_carriers:
        raise SystemExit("Parser produced zero logical inputs. Refusing to continue.")

    active = [
        c for c in all_carriers
        if c["lock"].upper() == "LOCKED"
        and c["ts"].upper() == "PRESENT"
        and int(c["service_count"]) > 0
    ]

    active_keys = {(c["host"], c["module"], c["channel"]) for c in active}
    active_services = [
        s for s in all_services
        if (s["host"], s["module"], s["channel"]) in active_keys
    ]

    # Sanity: service counts from the carrier summaries must be explainable.
    parsed_count_by_key = {}
    for s in active_services:
        key = (s["host"], s["module"], s["channel"])
        parsed_count_by_key[key] = parsed_count_by_key.get(key, 0) + 1

    mismatches = []
    for c in active:
        key = (c["host"], c["module"], c["channel"])
        parsed = parsed_count_by_key.get(key, 0)
        expected = int(c["service_count"])
        if parsed != expected:
            mismatches.append((key, expected, parsed, c["input_name"]))

    carrier_fields = [
        "host", "module", "channel", "frequency_mhz", "polarisation",
        "symbol_rate_mbd", "input_name", "lock", "ts", "tsid", "service_count",
    ]
    service_fields = [
        "host", "module", "channel", "frequency_mhz", "polarisation",
        "symbol_rate_mbd", "input_name", "sid", "status", "service_name",
    ]

    write_csv(out / "all_logical_inputs.csv", all_carriers, carrier_fields)
    write_csv(out / "active_service_carriers.csv", active, carrier_fields)
    write_csv(out / "active_services.csv", active_services, service_fields)

    geo = [
        s for s in active_services
        if "geo" in s["service_name"].lower()
        or "geo" in s["input_name"].lower()
    ]
    write_csv(out / "geo_services.csv", geo, service_fields)

    manifest = []
    for c in active:
        same = [
            s for s in active_services
            if s["host"] == c["host"]
            and s["module"] == c["module"]
            and s["channel"] == c["channel"]
        ]
        primary = same[0]["service_name"] if same else c["input_name"]
        manifest.append({
            **c,
            "primary_service": primary,
            "sensor_name": (
                f"WISI TV - {c['host']} - M{c['module']}C{c['channel']} - {primary}"
            ),
            "prtg_parameters": (
                f'--host "{c["host"]}" --module {c["module"]} --channel {c["channel"]}'
            ),
        })

    manifest_fields = carrier_fields + [
        "primary_service", "sensor_name", "prtg_parameters"
    ]
    write_csv(out / "prtg_authoritative_manifest.csv", manifest, manifest_fields)

    lines = [
        "=" * 118,
        "WISI GT34 AUTHORITATIVE LOGICAL-INPUT INVENTORY",
        "=" * 118,
        f"Logical inputs discovered : {len(all_carriers)}",
        f"Active service carriers   : {len(active)}",
        f"Active DVB services       : {len(active_services)}",
        f"Geo-related services      : {len(geo)}",
        f"Service-count mismatches  : {len(mismatches)}",
        "",
        "PER-HOST SUMMARY",
        "-" * 118,
    ]

    for host in sorted(parsed_hosts, key=lambda x: tuple(map(int, x.split(".")))):
        hc = [c for c in all_carriers if c["host"] == host]
        ha = [c for c in active if c["host"] == host]
        hs = [s for s in active_services if s["host"] == host]
        lines.append(
            f"{host:15} logical_inputs={len(hc):2d} "
            f"active_carriers={len(ha):2d} active_services={len(hs):3d}"
        )

    lines += ["", "ACTIVE SERVICE CARRIERS", "-" * 118]

    for c in active:
        names = [
            s["service_name"] for s in active_services
            if s["host"] == c["host"]
            and s["module"] == c["module"]
            and s["channel"] == c["channel"]
        ]
        lines.append(
            f"{c['host']:15} M{c['module']}C{c['channel']:<2} "
            f"{c['frequency_mhz']:>8g} {c['polarisation']:<2} "
            f"SR={c['symbol_rate_mbd']:>7g} MBd | {c['input_name']} | "
            f"services={c['service_count']} | " + "; ".join(names)
        )

    lines += ["", "GEO CROSS-CHECK", "-" * 118]
    if geo:
        for s in geo:
            lines.append(
                f"{s['host']} M{s['module']}C{s['channel']} "
                f"{s['frequency_mhz']:g} MHz {s['polarisation']} "
                f"SID {s['sid']} {s['service_name']} [{s['status']}]"
            )
    else:
        lines.append("NO GEO SERVICES FOUND")

    if mismatches:
        lines += ["", "SERVICE COUNT MISMATCHES", "-" * 118]
        for key, expected, parsed, name in mismatches:
            lines.append(
                f"{key[0]} M{key[1]}C{key[2]} {name}: expected {expected}, parsed {parsed}"
            )

    report = "\n".join(lines)
    (out / "authoritative_inventory_report.txt").write_text(
        report + "\n", encoding="utf-8"
    )
    print(report)

    if mismatches:
        raise SystemExit(
            f"Structured inventory parsed, but {len(mismatches)} service-count mismatch(es) remain."
        )

if __name__ == "__main__":
    main()
