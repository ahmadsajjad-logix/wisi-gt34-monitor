from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .models import (
    ConfiguredInput,
    DVBService,
    ElementaryStream,
    InstalledModule,
    ReceiverIndex,
    ReceiverSample,
)
from .oids import (
    AUDIO_STREAM_TYPES,
    DVB_SERVICE_NAME_BASE,
    DVB_SERVICE_PROVIDER_BASE,
    DVB_SERVICE_STATUS_BASE,
    DVB_STREAM_NETWORK_NAME_BASE,
    DVB_STREAM_NID_BASE,
    DVB_STREAM_NUM_SERVICES_BASE,
    DVB_STREAM_ONID_BASE,
    DVB_STREAM_TSID_BASE,
    INPUT_NAME_BASE,
    INPUT_SOURCE_BASE,
    METRICS,
    MODULE_HARDWARE_REVISION_BASE,
    MODULE_IDENTIFIER_BASE,
    MODULE_PRODUCT_BASE,
    MODULE_REPORTED_SLOT_BASE,
    MODULE_SOFTWARE_VERSION_BASE,
    MODULE_STATUS_TEXT_BASE,
    PHYSICAL_MODULE_SLOTS,
    MPEG_ES_AUDIO_LANGUAGE_BASE,
    MPEG_ES_TYPE_BASE,
    TS_BITRATE_BASE,
    TS_CONNECTION_BASE,
    TS_DIRECTION_IN,
    TS_PAYLOAD_BITRATE_BASE,
    VIDEO_STREAM_TYPES,
    as_int,
    input_catalogue_source_pointer_oid,
    metric_oid_for_source,
)
from .snmp import SnmpV2cClient, VarBind


log = logging.getLogger(__name__)


class GT34Poller:
    def __init__(
        self,
        client: SnmpV2cClient,
        expected_services: dict[str, str] | None = None,
        expected_mux: dict[str, dict[int, str]] | None = None,
    ) -> None:
        self.client = client

        # Legacy single-service aliases retained for compatibility.
        self.expected_services = dict(expected_services or {})

        # Stable multiplex baselines used for missing/unexpected-service checks.
        self.expected_mux: dict[str, dict[int, str]] = {
            str(input_key): {
                int(sid): str(name)
                for sid, name in sid_map.items()
            }
            for input_key, sid_map in (expected_mux or {}).items()
        }

        self._ts_cache_at = 0.0
        self._ts_cache: list[VarBind] = []

        self._service_cache_at = 0.0
        self._service_cache: list[VarBind] = []

        self._es_cache_at = 0.0
        self._es_cache: list[VarBind] = []

    @staticmethod
    def _catalogue_index(
        base: str,
        oid: str,
    ) -> tuple[int, int] | None:
        suffix = oid[len(base):].strip(".").split(".")

        if len(suffix) != 2:
            return None

        try:
            return int(suffix[0]), int(suffix[1])
        except ValueError:
            return None

    def discover_modules(
        self,
        physical_slots_only: bool = True,
    ) -> list[InstalledModule]:
        """
        Discover WISI chassis/module inventory.

        The supplied GT01W SNMP walk exposes product name in column 2 of the
        chassis module table and related hardware/software/identifier/status
        fields in columns 3, 4, 5, 10 and 11.

        By default only physical GT01W slots 1..6 are returned. Set
        physical_slots_only=False to include controller/system component rows.
        """
        products: dict[int, str] = {}

        for vb in self.client.walk(MODULE_PRODUCT_BASE):
            suffix = vb.oid[len(MODULE_PRODUCT_BASE):].strip(".")

            try:
                slot = int(suffix)
            except (TypeError, ValueError):
                continue

            if physical_slots_only and slot not in PHYSICAL_MODULE_SLOTS:
                continue

            if vb.value is None:
                continue

            product = str(vb.value).strip()
            if product:
                products[slot] = product

        modules: list[InstalledModule] = []

        for slot in sorted(products):
            oid_map = {
                "hardware_revision":
                    f"{MODULE_HARDWARE_REVISION_BASE}.{slot}",
                "software_version":
                    f"{MODULE_SOFTWARE_VERSION_BASE}.{slot}",
                "identifier":
                    f"{MODULE_IDENTIFIER_BASE}.{slot}",
                "status_text":
                    f"{MODULE_STATUS_TEXT_BASE}.{slot}",
                "reported_slot":
                    f"{MODULE_REPORTED_SLOT_BASE}.{slot}",
            }

            values = {
                item.oid: item.value
                for item in self.client.get(list(oid_map.values()))
            }

            def as_optional_text(key: str) -> str | None:
                value = values.get(oid_map[key])
                if value is None:
                    return None
                text = str(value).strip()
                return text or None

            reported_slot = as_int(
                values.get(oid_map["reported_slot"])
            )

            modules.append(
                InstalledModule(
                    slot=slot,
                    product=products[slot],
                    hardware_revision=as_optional_text(
                        "hardware_revision"
                    ),
                    software_version=as_optional_text(
                        "software_version"
                    ),
                    identifier=as_optional_text("identifier"),
                    status_text=as_optional_text("status_text"),
                    reported_slot=reported_slot,
                )
            )

        return modules

    def discover_inputs(self) -> list[ConfiguredInput]:
        names: dict[tuple[int, int], str] = {}
        sources: dict[tuple[int, int], str] = {}

        for vb in self.client.walk(INPUT_NAME_BASE):
            idx = self._catalogue_index(INPUT_NAME_BASE, vb.oid)

            if idx is not None and vb.value is not None:
                names[idx] = str(vb.value)

        for vb in self.client.walk(INPUT_SOURCE_BASE):
            idx = self._catalogue_index(INPUT_SOURCE_BASE, vb.oid)

            if (
                idx is not None
                and isinstance(vb.value, str)
                and vb.value
            ):
                sources[idx] = vb.value

        return [
            ConfiguredInput(
                idx[0],
                idx[1],
                names[idx],
                sources[idx],
            )
            for idx in sorted(set(names) & set(sources))
        ]

    def gt34_slots(self) -> set[int]:
        """
        Return the physical chassis slots that currently contain GT34 modules.

        This is discovered dynamically from the WISI chassis/module table.
        No slot numbers are hard-coded into the receiver polling path.
        """
        return {
            module.slot
            for module in self.discover_modules(physical_slots_only=True)
            if module.is_gt34
        }

    def discover_gt34_inputs(self) -> list[ConfiguredInput]:
        """
        Return only logical inputs whose parent physical slot is a GT34.

        The chassis may also contain GT42 modules whose logical inputs appear in
        the common WISI input catalogue. Those rows must not be interpreted with
        GT34 DVB-S/S2/S2X receiver OIDs.
        """
        gt34_slots = self.gt34_slots()

        if not gt34_slots:
            return []

        return [
            row
            for row in self.discover_inputs()
            if row.module in gt34_slots
        ]

    def discover_receivers(self) -> list[ReceiverIndex]:
        return [
            ReceiverIndex(row.module, row.channel)
            for row in self.discover_gt34_inputs()
        ]

    def get_gt34_input(
        self,
        module: int,
        channel: int,
    ) -> ConfiguredInput:
        gt34_slots = self.gt34_slots()

        if module not in gt34_slots:
            installed = {
                item.slot: item.product
                for item in self.discover_modules(physical_slots_only=True)
            }
            product = installed.get(module, "not installed")
            raise LookupError(
                f"Slot {module} is {product}, not GT34; "
                "GT34 receiver polling is refused for this slot"
            )

        for row in self.discover_gt34_inputs():
            if row.module == module and row.channel == channel:
                return row

        raise LookupError(
            f"Configured GT34 input m{module}/c{channel} was not found"
        )

    def get_input(
        self,
        module: int,
        channel: int,
    ) -> ConfiguredInput:
        for row in self.discover_inputs():
            if row.module == module and row.channel == channel:
                return row

        raise LookupError(
            f"Configured WISI input m{module}/c{channel} was not found"
        )

    def _ts_connections(self) -> list[VarBind]:
        now = time.monotonic()

        if self._ts_cache and now - self._ts_cache_at < 2.0:
            return self._ts_cache

        self._ts_cache = self.client.walk(TS_CONNECTION_BASE)
        self._ts_cache_at = now

        return self._ts_cache

    @staticmethod
    def _ts_indexes(
        connection_oid: str,
    ) -> tuple[int, int, int] | None:
        suffix = (
            connection_oid[len(TS_CONNECTION_BASE):]
            .strip(".")
            .split(".")
        )

        if len(suffix) != 3:
            return None

        try:
            return (
                int(suffix[0]),
                int(suffix[1]),
                int(suffix[2]),
            )
        except ValueError:
            return None

    def _service_names(self) -> list[VarBind]:
        now = time.monotonic()

        if self._service_cache and now - self._service_cache_at < 2.0:
            return self._service_cache

        self._service_cache = self.client.walk(DVB_SERVICE_NAME_BASE)
        self._service_cache_at = now

        return self._service_cache

    @staticmethod
    def _service_indexes(
        oid: str,
    ) -> tuple[int, int, int, int] | None:
        suffix = (
            oid[len(DVB_SERVICE_NAME_BASE):]
            .strip(".")
            .split(".")
        )

        if len(suffix) != 4:
            return None

        try:
            return (
                int(suffix[0]),
                int(suffix[1]),
                int(suffix[2]),
                int(suffix[3]),
            )
        except ValueError:
            return None

    def _elementary_stream_types(self) -> list[VarBind]:
        now = time.monotonic()

        if self._es_cache and now - self._es_cache_at < 2.0:
            return self._es_cache

        self._es_cache = self.client.walk(MPEG_ES_TYPE_BASE)
        self._es_cache_at = now

        return self._es_cache

    @staticmethod
    def _es_indexes(
        oid: str,
    ) -> tuple[int, int, int, int, int] | None:
        suffix = (
            oid[len(MPEG_ES_TYPE_BASE):]
            .strip(".")
            .split(".")
        )

        if len(suffix) != 5:
            return None

        try:
            return (
                int(suffix[0]),
                int(suffix[1]),
                int(suffix[2]),
                int(suffix[3]),
                int(suffix[4]),
            )
        except ValueError:
            return None

    def _service_elementary_streams(
        self,
        module: int,
        ts_index: int,
        sid: int,
    ) -> list[ElementaryStream]:
        """Return the complete ES map WISI exposes for one DVB service.

        Index of the MPEG ES table is
        ``module.direction.ts_index.program_number.pid``.  Audio language is
        queried for every audio PID, not only the first one.
        """
        raw_streams: list[tuple[int, int]] = []

        for vb in self._elementary_stream_types():
            indexes = self._es_indexes(vb.oid)

            if indexes is None:
                continue

            (
                row_module,
                direction,
                row_ts_index,
                program_number,
                pid,
            ) = indexes

            if (
                row_module != module
                or direction != TS_DIRECTION_IN
                or row_ts_index != ts_index
                or program_number != sid
            ):
                continue

            stream_type = as_int(vb.value)
            if stream_type is not None:
                raw_streams.append((pid, stream_type))

        raw_streams.sort(key=lambda item: item[0])

        language_oids: dict[int, str] = {
            pid: (
                f"{MPEG_ES_AUDIO_LANGUAGE_BASE}."
                f"{module}.{TS_DIRECTION_IN}.{ts_index}.{sid}.{pid}"
            )
            for pid, stream_type in raw_streams
            if stream_type in AUDIO_STREAM_TYPES
        }

        language_by_pid: dict[int, str | None] = {}
        if language_oids:
            language_values = {
                item.oid: item.value
                for item in self.client.get(list(language_oids.values()))
            }
            for pid, oid in language_oids.items():
                value = language_values.get(oid)
                language_by_pid[pid] = (
                    None
                    if value in (None, b"", "")
                    else str(value)
                )

        return [
            ElementaryStream(
                pid=pid,
                stream_type=stream_type,
                language=language_by_pid.get(pid),
            )
            for pid, stream_type in raw_streams
        ]

    def _discover_services(
        self,
        input_row: ConfiguredInput,
        ts_index: int | None,
    ) -> list[DVBService]:
        """
        Discover every DVB service belonging to this incoming TS.

        Service table index:
            module.direction.ts_index.SID
        """
        if ts_index is None:
            return []

        service_rows: list[tuple[int, str]] = []

        for vb in self._service_names():
            indexes = self._service_indexes(vb.oid)

            if indexes is None:
                continue

            (
                module,
                direction,
                row_ts_index,
                sid,
            ) = indexes

            if (
                module != input_row.module
                or direction != TS_DIRECTION_IN
                or row_ts_index != ts_index
            ):
                continue

            service_name = str(vb.value or "").strip()
            if not service_name:
                service_name = f"SID {sid}"

            service_rows.append((sid, service_name))

        service_rows.sort(key=lambda row: row[0])

        services: list[DVBService] = []

        for sid, service_name in service_rows:
            status_oid = (
                f"{DVB_SERVICE_STATUS_BASE}."
                f"{input_row.module}."
                f"{TS_DIRECTION_IN}."
                f"{ts_index}."
                f"{sid}"
            )

            provider_oid = (
                f"{DVB_SERVICE_PROVIDER_BASE}."
                f"{input_row.module}."
                f"{TS_DIRECTION_IN}."
                f"{ts_index}."
                f"{sid}"
            )

            status_vbs = self.client.get([status_oid])
            status = as_int(status_vbs[0].value) if status_vbs else None

            provider_vbs = self.client.get([provider_oid])
            provider = (
                str(provider_vbs[0].value or "").strip()
                if provider_vbs
                else ""
            ) or None

            elementary_streams = self._service_elementary_streams(
                input_row.module,
                ts_index,
                sid,
            )

            video_streams = [
                stream
                for stream in elementary_streams
                if stream.category == "video"
            ]
            audio_streams = [
                stream
                for stream in elementary_streams
                if stream.category == "audio"
            ]

            primary_video = video_streams[0] if video_streams else None
            primary_audio = audio_streams[0] if audio_streams else None

            services.append(
                DVBService(
                    sid=sid,
                    name=service_name,
                    provider=provider,
                    status=status,
                    elementary_streams=elementary_streams,
                    elementary_stream_count=len(elementary_streams),
                    video_pid=(
                        None if primary_video is None else primary_video.pid
                    ),
                    video_stream_type=(
                        None
                        if primary_video is None
                        else primary_video.stream_type
                    ),
                    audio_pid=(
                        None if primary_audio is None else primary_audio.pid
                    ),
                    audio_stream_type=(
                        None
                        if primary_audio is None
                        else primary_audio.stream_type
                    ),
                    audio_language=(
                        None
                        if primary_audio is None
                        else primary_audio.language
                    ),
                    audio_stream_count=len(audio_streams),
                )
            )

        return services

    def _ts_state(
        self,
        input_row: ConfiguredInput,
    ) -> tuple[
        bool,
        int | None,
        int | None,
        int | None,
        int | None,
        int | None,
        int | None,
        str | None,
        int | None,
    ]:
        target_pointer = input_catalogue_source_pointer_oid(
            input_row.module,
            input_row.channel,
        )

        for vb in self._ts_connections():
            if vb.value != target_pointer:
                continue

            indexes = self._ts_indexes(vb.oid)

            if indexes is None:
                continue

            module, direction, ts_index = indexes

            if (
                module != input_row.module
                or direction != TS_DIRECTION_IN
            ):
                continue

            oid_map = {
                "bitrate":
                    f"{TS_BITRATE_BASE}.{module}.{direction}.{ts_index}",
                "payload":
                    f"{TS_PAYLOAD_BITRATE_BASE}.{module}.{direction}.{ts_index}",
                "tsid":
                    f"{DVB_STREAM_TSID_BASE}.{module}.{direction}.{ts_index}",
                "nid":
                    f"{DVB_STREAM_NID_BASE}.{module}.{direction}.{ts_index}",
                "onid":
                    f"{DVB_STREAM_ONID_BASE}.{module}.{direction}.{ts_index}",
                "network":
                    f"{DVB_STREAM_NETWORK_NAME_BASE}.{module}.{direction}.{ts_index}",
                "num_services":
                    f"{DVB_STREAM_NUM_SERVICES_BASE}.{module}.{direction}.{ts_index}",
            }

            values = {
                item.oid: item.value
                for item in self.client.get(list(oid_map.values()))
            }

            return (
                True,
                ts_index,
                as_int(values.get(oid_map["bitrate"])),
                as_int(values.get(oid_map["payload"])),
                as_int(values.get(oid_map["tsid"])),
                as_int(values.get(oid_map["nid"])),
                as_int(values.get(oid_map["onid"])),
                (
                    None
                    if values.get(oid_map["network"]) is None
                    else str(values.get(oid_map["network"]))
                ),
                as_int(values.get(oid_map["num_services"])),
            )

        return (
            False,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )

    def poll_input(
        self,
        input_row: ConfiguredInput,
    ) -> ReceiverSample:
        names = list(METRICS)

        oid_by_name = {
            name: metric_oid_for_source(
                input_row.source_oid,
                METRICS[name],
            )
            for name in names
        }

        varbinds = self.client.get(list(oid_by_name.values()))

        raw_by_oid = {
            vb.oid: vb.value
            for vb in varbinds
        }

        values = {
            name: METRICS[name].parser(
                raw_by_oid.get(oid_by_name[name])
            )
            for name in names
        }

        (
            raw_ts_present,
            ts_index,
            ts_bitrate_bps,
            ts_payload_bitrate_bps,
            tsid,
            nid,
            onid,
            network_name,
            service_count,
        ) = self._ts_state(input_row)

        # WISI can retain the TS connection/service-table state after an input
        # has lost its usable RF signal.  Treat that retained connection as
        # raw topology only; operational TS presence requires the same signal
        # conditions used by ReceiverSample.effective_locked.
        lock_value = values.get("lock")
        snr_value = values.get("snr_db")
        ts_present = bool(
            raw_ts_present
            and lock_value == 2
            and snr_value is not None
            and snr_value > 0
        )

        if ts_present:
            services = self._discover_services(
                input_row,
                ts_index,
            )
        else:
            # The connection row may be cached/stale after signal loss.
            # Preserve the TS index for diagnostics, but do not expose stale
            # transport metadata or stale DVB services as live reception.
            ts_bitrate_bps = None
            ts_payload_bitrate_bps = None
            tsid = None
            nid = None
            onid = None
            network_name = None
            service_count = 0
            services = []

        input_key = f"{input_row.module}.{input_row.channel}"
        expected_mux = dict(self.expected_mux.get(input_key, {}))

        # Backward-compatible single-service fields.
        expected_name = self.expected_services.get(
            input_key,
            input_row.name,
        )

        expected_service = next(
            (
                service
                for service in services
                if service.name.casefold().strip()
                == expected_name.casefold().strip()
            ),
            None,
        )

        return ReceiverSample(
            module=input_row.module,
            channel=input_row.channel,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),

            input_name=input_row.name,
            source_family=input_row.family,
            source_oid=input_row.source_oid,

            ts_present=ts_present,
            ts_index=ts_index,
            ts_bitrate_bps=ts_bitrate_bps,
            ts_payload_bitrate_bps=ts_payload_bitrate_bps,

            tsid=tsid,
            nid=nid,
            onid=onid,
            network_name=network_name,
            service_count=service_count,

            expected_service_name=expected_name,
            expected_service_present=expected_service is not None,
            expected_service_sid=(
                None if expected_service is None else expected_service.sid
            ),
            expected_service_status=(
                None if expected_service is None else expected_service.status
            ),

            elementary_streams_present=(
                None
                if expected_service is None
                else expected_service.elementary_stream_count > 0
            ),
            elementary_stream_count=(
                None
                if expected_service is None
                else expected_service.elementary_stream_count
            ),
            primary_video_pid=(
                None if expected_service is None else expected_service.video_pid
            ),
            primary_video_stream_type=(
                None
                if expected_service is None
                else expected_service.video_stream_type
            ),
            primary_audio_pid=(
                None if expected_service is None else expected_service.audio_pid
            ),
            primary_audio_stream_type=(
                None
                if expected_service is None
                else expected_service.audio_stream_type
            ),
            primary_audio_language=(
                None
                if expected_service is None
                else expected_service.audio_language
            ),
            audio_stream_count=(
                None
                if expected_service is None
                else expected_service.audio_stream_count
            ),

            services=services,
            expected_services=expected_mux,

            **values,
        )

    def poll_receiver(
        self,
        receiver: ReceiverIndex,
    ) -> ReceiverSample:
        return self.poll_input(
            self.get_input(
                receiver.module,
                receiver.channel,
            )
        )
