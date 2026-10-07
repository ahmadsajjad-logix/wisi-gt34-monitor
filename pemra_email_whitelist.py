from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "pemra_email_whitelist.json"


def normalize_email_name(value: str) -> str:
    """Normalize only for deterministic whitelist identity comparison."""
    return "".join(
        ch.lower()
        for ch in str(value or "")
        if ch.isalnum()
    )


def _load_config() -> tuple[frozenset[str], frozenset[str]]:
    raw = json.loads(
        CONFIG_PATH.read_text(
            encoding="utf-8"
        )
    )

    entries = raw.get("entries")
    aliases = raw.get(
        "approved_monitored_aliases"
    )

    if not isinstance(entries, list):
        raise RuntimeError(
            "Invalid PEMRA email whitelist: entries must be a list"
        )

    if not isinstance(aliases, list):
        raise RuntimeError(
            "Invalid PEMRA email whitelist: approved_monitored_aliases "
            "must be a list"
        )

    whitelist_names = {
        normalize_email_name(
            str(row.get("name") or "")
        )
        for row in entries
        if isinstance(row, dict)
        and str(row.get("name") or "").strip()
    }

    if not whitelist_names:
        raise RuntimeError(
            "PEMRA email whitelist contains no qualification names"
        )

    alias_names: set[str] = set()

    for row in aliases:
        if not isinstance(row, dict):
            raise RuntimeError(
                "Invalid PEMRA email whitelist alias row"
            )

        monitored_name = str(
            row.get("monitored_name") or ""
        ).strip()

        whitelist_name = str(
            row.get("whitelist_name") or ""
        ).strip()

        monitored_key = normalize_email_name(
            monitored_name
        )

        whitelist_key = normalize_email_name(
            whitelist_name
        )

        if not monitored_key:
            raise RuntimeError(
                "PEMRA email whitelist contains an empty monitored alias"
            )

        if whitelist_key not in whitelist_names:
            raise RuntimeError(
                "PEMRA email alias target is not present in whitelist: "
                f"{whitelist_name!r}"
            )

        alias_names.add(monitored_key)

    return (
        frozenset(whitelist_names),
        frozenset(alias_names),
    )


_WHITELIST_NAMES, _APPROVED_ALIAS_NAMES = _load_config()


def is_email_qualified(name: str) -> bool:
    """Return True only for an exact normalized name or approved alias."""
    key = normalize_email_name(name)

    return (
        key in _WHITELIST_NAMES
        or key in _APPROVED_ALIAS_NAMES
    )


def filter_email_affected(
    values: Iterable[tuple[int, str]],
) -> tuple[tuple[int, str], ...]:
    """Filter presentation rows only; preserve SID and monitored name."""
    return tuple(
        (int(sid), str(name))
        for sid, name in values
        if is_email_qualified(str(name))
    )
