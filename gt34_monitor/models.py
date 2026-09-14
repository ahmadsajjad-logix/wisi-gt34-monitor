from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .oids import (
    CODE_RATE_LABELS,
    LNB_TYPE_LABELS,
    LNB_VOLTAGE_LABELS,
    LOCK_LABELS,
    MODULATION_LABELS,
    PLS_MODE_LABELS,
    POLARISATION_LABELS,
    TONE_LABELS,
    source_family,
    source_indexes,
)


@dataclass(frozen=True, slots=True)
class ReceiverIndex:
    module: int
    channel: int

    @property
    def key(self) -> str:
        return f"m{self.module}-c{self.channel}"


@dataclass(frozen=True, slots=True)
class InstalledModule:
    """
    One row from the WISI chassis/module inventory.

    slot is the table/component index. For the physical GT01W module cage,
    slots 1..6 are the six plug-in module positions.
    """

    slot: int
    product: str
    hardware_revision: str | None = None
    software_version: str | None = None
    identifier: str | None = None
    status_text: str | None = None
    reported_slot: int | None = None

    @property
    def is_gt34(self) -> bool:
        return self.product.strip().upper() == "GT34"

    @property
    def is_gt42(self) -> bool:
        return self.product.strip().upper() == "GT42"

    @property
    def is_receiver_module(self) -> bool:
        return self.is_gt34



@dataclass(frozen=True, slots=True)
class ConfiguredInput:
    module: int
    channel: int
    name: str
    source_oid: str

    @property
    def key(self) -> str:
        return f"m{self.module}-c{self.channel}"

    @property
    def family(self) -> str:
        return source_family(self.source_oid)

    @property
    def source_module(self) -> int:
        return source_indexes(self.source_oid)[0]

    @property
    def source_channel(self) -> int:
        return source_indexes(self.source_oid)[1]


@dataclass(frozen=True, slots=True)
class ElementaryStream:
    """One PMT elementary-stream row exposed by WISI-GTTS-MIB.

    ``language`` is populated only when WISI exposes an ISO-639 value for the
    PID.  No assumption is made about encryption/descrambling from ES metadata.
    """

    pid: int
    stream_type: int
    language: str | None = None

    @property
    def category(self) -> str:
        from .oids import AUDIO_STREAM_TYPES, VIDEO_STREAM_TYPES

        if self.stream_type in VIDEO_STREAM_TYPES:
            return "video"
        if self.stream_type in AUDIO_STREAM_TYPES:
            return "audio"
        return "other"

    @property
    def type_label(self) -> str:
        from .oids import STREAM_TYPE_LABELS

        return STREAM_TYPE_LABELS.get(
            self.stream_type,
            f"0x{self.stream_type:02X} ({self.stream_type})",
        )


@dataclass(slots=True)
class DVBService:
    sid: int
    name: str
    provider: str | None = None
    status: int | None = None

    # Complete PMT elementary-stream map currently exposed by the GTTS MIB.
    elementary_streams: list[ElementaryStream] = field(default_factory=list)
    elementary_stream_count: int = 0

    # Optional descrambler state.  The GTTS service/ES tables used by GT34
    # polling do not themselves prove whether a service is encrypted or
    # successfully descrambled, so this remains None unless a verified
    # descrambler source is added later.
    descrambling_status: int | None = None

    video_pid: int | None = None
    video_stream_type: int | None = None

    audio_pid: int | None = None
    audio_stream_type: int | None = None
    audio_language: str | None = None

    audio_stream_count: int = 0

    @property
    def video_streams(self) -> list[ElementaryStream]:
        return [stream for stream in self.elementary_streams if stream.category == "video"]

    @property
    def audio_streams(self) -> list[ElementaryStream]:
        return [stream for stream in self.elementary_streams if stream.category == "audio"]

    @property
    def other_streams(self) -> list[ElementaryStream]:
        return [stream for stream in self.elementary_streams if stream.category == "other"]

    @property
    def es_metadata_state(self) -> str:
        # Zero ES rows is not automatically a service failure.  This matters
        # for encrypted/special services and for device/table limitations.
        return "available" if self.elementary_streams else "unavailable-or-empty"

    @property
    def descrambling_status_label(self) -> str:
        return {1: "scrambled", 2: "descrambled"}.get(
            self.descrambling_status,
            "unknown",
        )

    @property
    def running(self) -> bool:
        return self.status == 4

    @property
    def explicit_failure(self) -> bool:
        """
        WISI/DVB running-status values with clear non-operational semantics.

        Unknown/vendor-specific values are deliberately NOT treated as failures.
        This is required for services such as the observed VTV1/VTV2 entries,
        which are present but do not report status 4.
        """
        return self.status in {1, 2, 3, 5}

    @property
    def operational_ok(self) -> bool:
        # A DVBService object exists only when the service is present in the
        # live GT34 service table. Unknown/vendor-specific status is therefore
        # accepted as present unless it is an explicitly non-running state.
        return not self.explicit_failure

    @property
    def status_label(self) -> str:
        labels = {
            0: "unknown",
            1: "not-running",
            2: "starting-soon",
            3: "pausing",
            4: "running",
            5: "off-air",
        }
        if self.status in labels:
            return labels[self.status]
        if self.status is None:
            return "unknown"
        return f"status-{self.status}"


@dataclass(slots=True)
class ReceiverSample:
    module: int
    channel: int
    timestamp_utc: str

    input_name: str | None = None

    # Static carrier identity metadata supplied by the validated baseline.
    # This is presentation metadata; it does not participate in RF/TS alarms.
    satellite_name: str | None = None

    source_family: str | None = None
    source_oid: str | None = None

    lock: int | None = None
    rf_level_dbm: float | None = None
    snr_db: float | None = None
    ber: float | None = None

    frequency_khz: int | None = None
    polarisation: int | None = None
    symbol_rate_bd: int | None = None
    modulation: int | None = None
    code_rate: int | None = None

    pls_mode: int | None = None
    pls_id: int | None = None
    mis: int | None = None

    lnb_type: int | None = None
    lo_frequency_khz: int | None = None
    lnb_voltage: int | None = None
    tone_22khz: int | None = None

    ts_present: bool | None = None
    ts_index: int | None = None
    ts_bitrate_bps: int | None = None
    ts_payload_bitrate_bps: int | None = None

    # Optional read-only GT34 web-status enrichment. These fields are not used
    # by the existing SNMP lock/TS/compliance alarm logic.
    detected_constellation: str | None = None
    detected_code_rate: str | None = None
    detected_isi: int | None = None
    web_ts_bitrate_bps: int | None = None
    web_ber_text: str | None = None
    web_if_frequency_khz: int | None = None
    web_tuner_id: int | None = None
    web_remote_id: str | None = None

    tsid: int | None = None
    nid: int | None = None
    onid: int | None = None
    network_name: str | None = None
    service_count: int | None = None

    # Legacy single-service fields retained for compatibility.
    expected_service_name: str | None = None
    expected_service_present: bool | None = None
    expected_service_sid: int | None = None
    expected_service_status: int | None = None

    elementary_streams_present: bool | None = None
    elementary_stream_count: int | None = None
    primary_video_pid: int | None = None
    primary_video_stream_type: int | None = None
    primary_audio_pid: int | None = None
    primary_audio_stream_type: int | None = None
    primary_audio_language: str | None = None
    audio_stream_count: int | None = None

    # Every service currently present in the incoming multiplex.
    services: list[DVBService] = field(default_factory=list)

    # Stable regulatory baseline for this logical carrier/input:
    # SID -> expected service name.
    expected_services: dict[int, str] = field(default_factory=dict)

    @classmethod
    def now(
        cls,
        module: int,
        channel: int,
        **values: Any,
    ) -> "ReceiverSample":
        return cls(
            module,
            channel,
            datetime.now(timezone.utc).isoformat(),
            **values,
        )

    @property
    def display_name(self) -> str:
        return self.input_name or f"m{self.module}/c{self.channel}"

    @property
    def effective_locked(self) -> bool:
        """
        Operational lock.

        The GT34 can retain the raw lock object after a logical input has
        stopped carrying a usable signal. Require raw demod lock, positive SNR
        and a live TS connection.
        """
        return (
            self.lock == 2
            and self.snr_db is not None
            and self.snr_db > 0
            and self.ts_present is True
        )

    @property
    def rf_level_dbuv_75ohm(self) -> float | None:
        if self.rf_level_dbm is None:
            return None
        return self.rf_level_dbm + 108.75

    @property
    def live_services_by_sid(self) -> dict[int, DVBService]:
        return {service.sid: service for service in self.services}

    @property
    def discovered_service_count(self) -> int:
        return len(self.services)

    @property
    def running_service_count(self) -> int:
        return sum(1 for service in self.services if service.running)

    @property
    def expected_service_count(self) -> int:
        return len(self.expected_services)

    @property
    def present_expected_service_count(self) -> int:
        live = self.live_services_by_sid
        return sum(1 for sid in self.expected_services if sid in live)

    @property
    def missing_expected_service_sids(self) -> list[int]:
        live = self.live_services_by_sid
        return sorted(sid for sid in self.expected_services if sid not in live)

    @property
    def missing_expected_service_count(self) -> int:
        return len(self.missing_expected_service_sids)

    @property
    def unexpected_services(self) -> list[DVBService]:
        if not self.expected_services:
            return []
        return sorted(
            (
                service
                for service in self.services
                if service.sid not in self.expected_services
            ),
            key=lambda service: service.sid,
        )

    @property
    def unexpected_service_count(self) -> int:
        return len(self.unexpected_services)

    @property
    def name_mismatch_sids(self) -> list[int]:
        live = self.live_services_by_sid
        mismatches: list[int] = []

        for sid, expected_name in self.expected_services.items():
            service = live.get(sid)
            if service is None:
                continue
            if service.name.casefold().strip() != expected_name.casefold().strip():
                mismatches.append(sid)

        return sorted(mismatches)

    @property
    def service_name_mismatch_count(self) -> int:
        return len(self.name_mismatch_sids)

    def labels(self) -> dict[str, str | None]:
        return {
            "lock": LOCK_LABELS.get(self.lock),
            "effective_lock":
                "locked" if self.effective_locked else "unlocked",
            "polarisation":
                POLARISATION_LABELS.get(self.polarisation),
            "modulation":
                MODULATION_LABELS.get(self.modulation),
            "code_rate":
                CODE_RATE_LABELS.get(self.code_rate),
            "pls_mode":
                PLS_MODE_LABELS.get(self.pls_mode),
            "lnb_type":
                LNB_TYPE_LABELS.get(self.lnb_type),
            "lnb_voltage":
                LNB_VOLTAGE_LABELS.get(self.lnb_voltage),
            "tone_22khz":
                TONE_LABELS.get(self.tone_22khz),
            "ts_present":
                None
                if self.ts_present is None
                else ("present" if self.ts_present else "absent"),
            "expected_service_present":
                None
                if self.expected_service_present is None
                else (
                    "present"
                    if self.expected_service_present
                    else "absent"
                ),
            "expected_service_status": {
                0: "unknown",
                1: "not-running",
                2: "starting-soon",
                3: "pausing",
                4: "running",
                5: "off-air",
            }.get(self.expected_service_status),
            "elementary_streams_present":
                None
                if self.elementary_streams_present is None
                else (
                    "present"
                    if self.elementary_streams_present
                    else "absent"
                ),
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["effective_locked"] = self.effective_locked
        data["rf_level_dbuv_75ohm"] = self.rf_level_dbuv_75ohm
        data["discovered_service_count"] = self.discovered_service_count
        data["running_service_count"] = self.running_service_count
        data["expected_service_count"] = self.expected_service_count
        data["present_expected_service_count"] = self.present_expected_service_count
        data["missing_expected_service_count"] = self.missing_expected_service_count
        data["unexpected_service_count"] = self.unexpected_service_count
        data["service_name_mismatch_count"] = self.service_name_mismatch_count
        data["labels"] = self.labels()
        return data
