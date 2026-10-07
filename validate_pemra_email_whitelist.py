from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "pemra_email_whitelist.json"
EXPECTED = ROOT / "tv43_expected_services.json"

def normalize(value: str) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())

def main() -> None:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    entries = list(cfg["entries"])
    aliases = {
        normalize(row["monitored_name"]): str(row["whitelist_name"])
        for row in cfg["approved_monitored_aliases"]
    }

    assert len(entries) == 176, len(entries)

    reference = [row for row in entries if row["source"] != "Manual PEMRA-licensed override"]
    manual = [row for row in entries if row["source"] == "Manual PEMRA-licensed override"]

    assert len(reference) == 166, len(reference)
    assert len(manual) == 10, len(manual)

    whitelist_names = {normalize(row["name"]) for row in entries}
    assert len(whitelist_names) == 173, len(whitelist_names)

    for monitored_norm, whitelist_name in aliases.items():
        assert monitored_norm
        assert normalize(whitelist_name) in whitelist_names, whitelist_name

    raw = json.loads(EXPECTED.read_text(encoding="utf-8"))
    carriers = raw["carriers"]

    monitored = []
    for key, entry in carriers.items():
        for svc in entry.get("services", []):
            sid = int(svc["sid"])
            name = str(svc["name"])

            # Production V11.6 resolves this WISI duplicate-name pair before
            # condition/email presentation.
            if key == "192.168.3.27|M1C1":
                if sid == 1:
                    name = "Aaj News"
                elif sid == 257:
                    name = "Aaj Entertainment"

            monitored.append((key, sid, name))

    def qualified(name: str) -> bool:
        n = normalize(name)
        return n in whitelist_names or n in aliases

    qualified_rows = [row for row in monitored if qualified(row[2])]
    black_rows = [row for row in monitored if not qualified(row[2])]

    qualified_labels = sorted({row[2] for row in qualified_rows}, key=str.casefold)
    black_labels = sorted({row[2] for row in black_rows}, key=str.casefold)

    source_targets = defaultdict(set)
    for name in qualified_labels:
        n = normalize(name)
        if n in whitelist_names:
            source_targets[n].add(name)
        if n in aliases:
            source_targets[normalize(aliases[n])].add(name)

    matched_reference_rows = sum(
        1 for row in reference if source_targets.get(normalize(row["name"]))
    )
    matched_manual_rows = sum(
        1 for row in manual if source_targets.get(normalize(row["name"]))
    )

    assert matched_reference_rows == 106, matched_reference_rows
    assert matched_manual_rows == 10, matched_manual_rows
    assert len(qualified_rows) == 129, len(qualified_rows)
    assert len(black_rows) == 39, len(black_rows)
    assert len(qualified_labels) == 120, len(qualified_labels)
    assert len(black_labels) == 36, len(black_labels)

    m5c5 = [
        name
        for key, sid, name in monitored
        if key == "192.168.3.45|M5C5" and qualified(name)
    ]

    expected_11552 = [
        "A1 TV",
        "MINIMAX 2",
        "SUN TV",
        "APNA HD",
        "8XM HD",
        "ABBTAKK HD",
        "JALWA HD",
        "K21 NEWS HD",
        "Ocean News TV",
        "NTN NEWS",
        "FASTSPORTS HD",
    ]
    assert m5c5 == expected_11552, m5c5

    print("=" * 92)
    print("PEMRA EMAIL WHITELIST VALIDATION")
    print("=" * 92)
    print(f"Reference PDF rows                 : {len(reference)}")
    print(f"Manual licensed overrides          : {len(manual)}")
    print(f"Total audit rows                   : {len(entries)}")
    print(f"Unique normalized whitelist names  : {len(whitelist_names)}")
    print(f"Reference rows currently matched   : {matched_reference_rows}")
    print(f"Manual overrides currently matched : {matched_manual_rows}")
    print(f"Monitored service occurrences      : {len(monitored)}")
    print(f"Email-qualified occurrences        : {len(qualified_rows)}")
    print(f"Non-qualified occurrences          : {len(black_rows)}")
    print(f"Qualified distinct monitored names : {len(qualified_labels)}")
    print(f"Blacklist distinct monitored names : {len(black_labels)}")
    print()
    print("11552 MHz H qualified email names:")
    for name in m5c5:
        print(f"  - {name}")
    print()
    print("Blacklist monitored names:")
    for name in black_labels:
        print(f"  - {name}")
    print("=" * 92)
    print("PEMRA EMAIL WHITELIST VALIDATION: PASS")
    print("=" * 92)

if __name__ == "__main__":
    main()
