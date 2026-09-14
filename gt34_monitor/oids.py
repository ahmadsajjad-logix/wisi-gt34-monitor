from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

WISI_ENTERPRISE = "1.3.6.1.4.1.7465"

# WISI chassis/module inventory table observed in the supplied GT01W walk.
# Table row index is the WISI component/module number. Physical module slots
# are 1..6 on the user's GT01W chassis. Other rows (for example 7, 10, 11)
# represent controller/system components and are not physical module slots.
MODULE_TABLE_ENTRY_BASE = "1.3.6.1.4.1.7465.20.2.9.1.2.1.3.1"
MODULE_PRODUCT_BASE = f"{MODULE_TABLE_ENTRY_BASE}.2"
MODULE_HARDWARE_REVISION_BASE = f"{MODULE_TABLE_ENTRY_BASE}.3"
MODULE_SOFTWARE_VERSION_BASE = f"{MODULE_TABLE_ENTRY_BASE}.4"
MODULE_IDENTIFIER_BASE = f"{MODULE_TABLE_ENTRY_BASE}.5"
MODULE_STATUS_TEXT_BASE = f"{MODULE_TABLE_ENTRY_BASE}.10"
MODULE_REPORTED_SLOT_BASE = f"{MODULE_TABLE_ENTRY_BASE}.11"

PHYSICAL_MODULE_SLOTS = frozenset(range(1, 7))


# WISI chassis input catalogue. Each row maps a logical input name to the
# source-object OID that implements that input.
INPUT_SOURCE_BASE = "1.3.6.1.4.1.7465.20.2.9.1.2.1.7.1.2"
INPUT_NAME_BASE = "1.3.6.1.4.1.7465.20.2.9.1.2.1.7.1.3"

# WISI-GTTS-MIB::gtTSConnection. Index = {gtModule, gtTSDirection, gtTSIndex}.
# The value is a RowPointer back to the logical input/output row through which
# the TS is flowing. In the supplied GT34 walks, disabling Geo News removes its
# gtTSConnection row completely while the configured input remains catalogued.
TS_CONNECTION_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.1.1.3"
TS_BITRATE_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.1.1.4"
TS_PAYLOAD_BITRATE_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.1.1.5"
TS_DIRECTION_IN = 1

# WISI-GTTS-MIB DVB-SI stream metadata. Index = {gtModule, gtTSDirection, gtTSIndex}.
DVB_STREAM_TSID_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.5.1.1"
DVB_STREAM_NID_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.5.1.2"
DVB_STREAM_ONID_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.5.1.3"
DVB_STREAM_NETWORK_NAME_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.5.1.4"
DVB_STREAM_NUM_SERVICES_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.5.1.5"

# WISI-GTTS-MIB DVB service table. Index =
# {gtModule, gtTSDirection, gtTSIndex, gtDVBServiceID}.
DVB_SERVICE_PROVIDER_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.6.1.3"
DVB_SERVICE_NAME_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.6.1.4"
DVB_SERVICE_STATUS_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.6.1.5"

# WISI-GTTS-MIB MPEG elementary-stream table. Index =
# {gtModule, gtTSDirection, gtTSIndex, gtMPEGProgramNumber, gtPID}.
# The table value is the PMT stream_type for that PID; the optional companion
# table carries ISO-639 language codes for audio elementary streams.
MPEG_ES_TYPE_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.4.1.2"
MPEG_ES_AUDIO_LANGUAGE_BASE = "1.3.6.1.4.1.7465.20.2.9.5.3.1.4.1.3"

# ISO/IEC 13818-1 PMT stream_type values explicitly described by the supplied
# WISI-GTTS-MIB and useful for classifying the GT34's live elementary streams.
VIDEO_STREAM_TYPES = {0x01, 0x02, 0x10, 0x1B}
AUDIO_STREAM_TYPES = {0x03, 0x04, 0x0F, 0x11}
STREAM_TYPE_LABELS = {
    0x01: "MPEG-1 Video",
    0x02: "MPEG-2 Video",
    0x03: "MPEG-1 Audio",
    0x04: "MPEG-2 Audio",
    0x0F: "AAC ADTS",
    0x10: "MPEG-4 Visual",
    0x11: "AAC LATM",
    0x1B: "H.264/AVC",
}

# Known receiver source table entry bases observed in supplied WISI MIB/walks.
DVB_S_ENTRY = "1.3.6.1.4.1.7465.20.2.9.4.4.3.1.2.1"
DVB_S2X_ENTRY = "1.3.6.1.4.1.7465.20.2.9.4.4.8.1.2.1"


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, bytes):
            value = value.decode("ascii", errors="strict")
        return float(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None


def as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, bytes):
            value = value.decode("ascii", errors="strict")
        return int(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    column: int
    unit: str
    parser: Callable[[Any], Any]
    writable_config: bool = False

    @property
    def base_oid(self) -> str:
        return f"{DVB_S2X_ENTRY}.{self.column}"

    def oid(self, module: int, channel: int) -> str:
        return f"{self.base_oid}.{module}.{channel}"


METRICS: dict[str, Metric] = {
    "lock": Metric("lock", 3, "state", as_int),
    "rf_level_dbm": Metric("rf_level_dbm", 4, "dBm", as_float),
    "snr_db": Metric("snr_db", 5, "dB", as_float),
    "ber": Metric("ber", 7, "", as_float),
    "frequency_khz": Metric("frequency_khz", 11, "kHz", as_int, True),
    "polarisation": Metric("polarisation", 12, "enum", as_int, True),
    "symbol_rate_bd": Metric("symbol_rate_bd", 13, "Bd", as_int, True),
    "modulation": Metric("modulation", 14, "enum", as_int, True),
    "code_rate": Metric("code_rate", 15, "enum", as_int, True),
    "pls_mode": Metric("pls_mode", 16, "enum", as_int, True),
    "pls_id": Metric("pls_id", 17, "", as_int, True),
    "mis": Metric("mis", 18, "", as_int, True),
    "lnb_type": Metric("lnb_type", 20, "enum", as_int, True),
    "lo_frequency_khz": Metric("lo_frequency_khz", 21, "kHz", as_int, True),
    "lnb_voltage": Metric("lnb_voltage", 22, "V/enum", as_int, True),
    "tone_22khz": Metric("tone_22khz", 23, "enum", as_int, True),
}


def source_entry_base(source_row_oid: str) -> str:
    parts = source_row_oid.strip(".").split(".")
    if len(parts) < 4:
        raise ValueError(f"Invalid WISI source row OID: {source_row_oid}")
    return ".".join(parts[:-3])


def source_indexes(source_row_oid: str) -> tuple[int, int]:
    parts = source_row_oid.strip(".").split(".")
    if len(parts) < 3:
        raise ValueError(f"Invalid WISI source row OID: {source_row_oid}")
    return int(parts[-2]), int(parts[-1])


def metric_oid_for_source(source_row_oid: str, metric: Metric) -> str:
    module, channel = source_indexes(source_row_oid)
    return f"{source_entry_base(source_row_oid)}.{metric.column}.{module}.{channel}"


def input_catalogue_source_pointer_oid(module: int, channel: int) -> str:
    """OID of the gtInputs-table source-pointer cell for a logical input."""
    return f"{INPUT_SOURCE_BASE}.{module}.{channel}"


def source_family(source_row_oid: str) -> str:
    base = source_entry_base(source_row_oid)
    if base == DVB_S2X_ENTRY:
        return "DVB-S2X/S2"
    if base == DVB_S_ENTRY:
        return "DVB-S"
    return "unknown"


LOCK_LABELS = {1: "unlocked", 2: "locked"}
POLARISATION_LABELS = {1: "horizontal", 2: "vertical", 3: "circular-left", 4: "circular-right"}
MODULATION_LABELS = {1: "auto", 4: "QPSK", 8: "8PSK", 16: "16QAM"}
CODE_RATE_LABELS = {
    1: "auto", 500: "1/2", 600: "3/5", 666: "2/3", 750: "3/4",
    800: "4/5", 833: "5/6", 875: "7/8", 888: "8/9", 900: "9/10",
}
PLS_MODE_LABELS = {0: "auto", 1: "manual"}
LNB_TYPE_LABELS = {1: "none", 2: "universal", 3: "fixed"}
LNB_VOLTAGE_LABELS = {1: "auto", 2: "off", 13: "13V", 18: "18V"}
TONE_LABELS = {1: "auto", 2: "off", 3: "on"}
