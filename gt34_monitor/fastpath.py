from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .collector import GT34Poller
from .models import ConfiguredInput, DVBService, ElementaryStream
from .oids import (
    AUDIO_STREAM_TYPES,
    VIDEO_STREAM_TYPES,
    INPUT_NAME_BASE,
    INPUT_SOURCE_BASE,
    MODULE_PRODUCT_BASE,
    TS_CONNECTION_BASE,
    TS_DIRECTION_IN,
    DVB_SERVICE_NAME_BASE,
    DVB_SERVICE_STATUS_BASE,
    DVB_SERVICE_PROVIDER_BASE,
    MPEG_ES_TYPE_BASE,
    MPEG_ES_AUDIO_LANGUAGE_BASE,
    as_int,
)
from .webstatus import GT34WebStatusClient


class FastGT34Poller(GT34Poller):
    """Read-only per-input fast path.

    It preserves the production data model and fail-closed semantics while
    replacing chassis-wide SNMP walks with index-scoped reads for the one
    requested GT34 input/TS. No OID meaning or alarm rule is changed.
    """

    def get_gt34_input(self, module: int, channel: int) -> ConfiguredInput:
        # Exact module product + exact input catalogue cells. This avoids two
        # module-table walks and two full input-catalogue walks in production.
        product_oid = f"{MODULE_PRODUCT_BASE}.{module}"
        name_oid = f"{INPUT_NAME_BASE}.{module}.{channel}"
        source_oid_cell = f"{INPUT_SOURCE_BASE}.{module}.{channel}"
        rows = {vb.oid: vb.value for vb in self.client.get(
            [product_oid, name_oid, source_oid_cell]
        )}

        product = str(rows.get(product_oid) or "").strip().upper()
        if product != "GT34":
            raise LookupError(
                f"Slot {module} is {product or 'not installed'}, not GT34; "
                "GT34 receiver polling is refused for this slot"
            )

        name = rows.get(name_oid)
        source = rows.get(source_oid_cell)
        if name is None or not isinstance(source, str) or not source:
            raise LookupError(
                f"Configured GT34 input m{module}/c{channel} was not found"
            )

        return ConfiguredInput(module, channel, str(name), source)

    def _ts_connections(self, module: int | None = None) -> list:
        # Restrict walk to the target module's incoming TS rows when known.
        if module is None:
            return super()._ts_connections()
        return self.client.walk(f"{TS_CONNECTION_BASE}.{module}.{TS_DIRECTION_IN}")

    def _ts_state(self, input_row: ConfiguredInput):
        target_pointer = f"{INPUT_SOURCE_BASE}.{input_row.module}.{input_row.channel}"
        for vb in self._ts_connections(input_row.module):
            if vb.value != target_pointer:
                continue
            indexes = self._ts_indexes(vb.oid)
            if indexes is None:
                continue
            module, direction, ts_index = indexes
            if module != input_row.module or direction != TS_DIRECTION_IN:
                continue

            # Reuse the production implementation's exact downstream OID set
            # after finding the TS index, but without another broad walk.
            from .oids import (
                TS_BITRATE_BASE, TS_PAYLOAD_BITRATE_BASE,
                DVB_STREAM_TSID_BASE, DVB_STREAM_NID_BASE,
                DVB_STREAM_ONID_BASE, DVB_STREAM_NETWORK_NAME_BASE,
                DVB_STREAM_NUM_SERVICES_BASE,
            )
            oid_map = {
                "bitrate": f"{TS_BITRATE_BASE}.{module}.{direction}.{ts_index}",
                "payload": f"{TS_PAYLOAD_BITRATE_BASE}.{module}.{direction}.{ts_index}",
                "tsid": f"{DVB_STREAM_TSID_BASE}.{module}.{direction}.{ts_index}",
                "nid": f"{DVB_STREAM_NID_BASE}.{module}.{direction}.{ts_index}",
                "onid": f"{DVB_STREAM_ONID_BASE}.{module}.{direction}.{ts_index}",
                "network": f"{DVB_STREAM_NETWORK_NAME_BASE}.{module}.{direction}.{ts_index}",
                "num_services": f"{DVB_STREAM_NUM_SERVICES_BASE}.{module}.{direction}.{ts_index}",
            }
            values = {x.oid: x.value for x in self.client.get(list(oid_map.values()))}
            return (
                True, ts_index,
                as_int(values.get(oid_map["bitrate"])),
                as_int(values.get(oid_map["payload"])),
                as_int(values.get(oid_map["tsid"])),
                as_int(values.get(oid_map["nid"])),
                as_int(values.get(oid_map["onid"])),
                None if values.get(oid_map["network"]) is None else str(values.get(oid_map["network"])),
                as_int(values.get(oid_map["num_services"])),
            )
        return (False, None, None, None, None, None, None, None, None)

    def _discover_services(self, input_row: ConfiguredInput, ts_index: int | None):
        if ts_index is None:
            return []
        m = input_row.module
        d = TS_DIRECTION_IN
        t = ts_index

        # Scope the service-name walk to this one TS, not the complete chassis.
        name_base = f"{DVB_SERVICE_NAME_BASE}.{m}.{d}.{t}"
        service_rows: list[tuple[int, str]] = []
        for vb in self.client.walk(name_base):
            suffix = vb.oid[len(name_base):].strip(".")
            try:
                sid = int(suffix)
            except ValueError:
                continue
            name = str(vb.value or "").strip() or f"SID {sid}"
            service_rows.append((sid, name))
        service_rows.sort(key=lambda x: x[0])
        if not service_rows:
            return []

        # One GET carries status+provider for every service rather than two GETs
        # per SID.
        status_oid_by_sid = {
            sid: f"{DVB_SERVICE_STATUS_BASE}.{m}.{d}.{t}.{sid}"
            for sid, _ in service_rows
        }
        provider_oid_by_sid = {
            sid: f"{DVB_SERVICE_PROVIDER_BASE}.{m}.{d}.{t}.{sid}"
            for sid, _ in service_rows
        }
        all_meta_oids = []
        for sid, _ in service_rows:
            all_meta_oids.extend([status_oid_by_sid[sid], provider_oid_by_sid[sid]])
        # WISI SNMP agents may silently drop oversized multi-varbind GETs.
        # Verified on 192.168.3.45 M5/C5: 48 OIDs time out, while
        # smaller batches succeed. Keep a conservative 16-OID ceiling.
        meta = {}
        for start in range(0, len(all_meta_oids), 16):
            batch = all_meta_oids[start:start + 16]
            meta.update({vb.oid: vb.value for vb in self.client.get(batch)})

        # One TS-scoped ES walk, then group rows by program/SID.
        es_base = f"{MPEG_ES_TYPE_BASE}.{m}.{d}.{t}"
        streams_by_sid: dict[int, list[tuple[int, int]]] = {}
        for vb in self.client.walk(es_base):
            suffix = vb.oid[len(es_base):].strip(".").split(".")
            if len(suffix) != 2:
                continue
            try:
                sid, pid = int(suffix[0]), int(suffix[1])
            except ValueError:
                continue
            st = as_int(vb.value)
            if st is not None:
                streams_by_sid.setdefault(sid, []).append((pid, st))

        # Fetch all audio-language cells for this TS in one request.
        lang_oid: dict[tuple[int, int], str] = {}
        for sid, pairs in streams_by_sid.items():
            for pid, st in pairs:
                if st in AUDIO_STREAM_TYPES:
                    lang_oid[(sid, pid)] = f"{MPEG_ES_AUDIO_LANGUAGE_BASE}.{m}.{d}.{t}.{sid}.{pid}"
        lang_values = {}
        if lang_oid:
            lang_values = {vb.oid: vb.value for vb in self.client.get(list(lang_oid.values()))}

        services: list[DVBService] = []
        for sid, service_name in service_rows:
            raw = sorted(streams_by_sid.get(sid, []), key=lambda x: x[0])
            elementary_streams = []
            for pid, st in raw:
                lv = lang_values.get(lang_oid.get((sid, pid), ""))
                language = None if lv in (None, b"", "") else str(lv)
                elementary_streams.append(ElementaryStream(pid=pid, stream_type=st, language=language))

            video_streams = [s for s in elementary_streams if s.category == "video"]
            audio_streams = [s for s in elementary_streams if s.category == "audio"]
            pv = video_streams[0] if video_streams else None
            pa = audio_streams[0] if audio_streams else None
            provider_raw = meta.get(provider_oid_by_sid[sid])
            provider = (str(provider_raw or "").strip() or None)
            services.append(DVBService(
                sid=sid,
                name=service_name,
                provider=provider,
                status=as_int(meta.get(status_oid_by_sid[sid])),
                elementary_streams=elementary_streams,
                elementary_stream_count=len(elementary_streams),
                video_pid=None if pv is None else pv.pid,
                video_stream_type=None if pv is None else pv.stream_type,
                audio_pid=None if pa is None else pa.pid,
                audio_stream_type=None if pa is None else pa.stream_type,
                audio_language=None if pa is None else pa.language,
                audio_stream_count=len(audio_streams),
            ))
        return services


def _load_remote_cache(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_remote_cache(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def enrich_sample_from_web_fast(sample, host: str, cache_path: Path, timeout_seconds: float = 3.0) -> bool:
    """Fail-closed web fast path with positive revalidation every poll.

    A cached remote is only a routing hint. It is NEVER trusted as identity:
    every use fetches live data and must pass the unchanged production tuner
    fingerprint matcher. On any mismatch/error, full production discovery is
    performed and only an exactly-one-match result is cached.
    """
    if not sample.effective_locked or sample.ts_present is not True:
        return False

    symbolrate = getattr(sample, "symbol_rate_bd", None)
    client = GT34WebStatusClient(host, timeout_seconds=timeout_seconds)
    key = f"{host}|m{sample.module}"

    # Use one atomic cache file per host/module. This removes the shared
    # read-modify-write race between concurrent PRTG processes for different
    # modules while retaining positive live fingerprint revalidation.
    safe_host = host.replace(".", "_").replace(":", "_")
    module_cache_path = cache_path.with_name(
        f"{cache_path.stem}.{safe_host}.m{sample.module}{cache_path.suffix}"
    )

    cache = _load_remote_cache(module_cache_path)
    remote = cache.get(key)
    matched = None

    if remote:
        try:
            payload = client._fetch_remote_pair(remote)
            statuses = client.parse_status(payload)
            status = client._match_tuner(
                statuses=statuses,
                channel=sample.channel,
                rf_khz=sample.frequency_khz,
                lo_khz=sample.lo_frequency_khz,
                snr_db=sample.snr_db,
                rf_level_dbm=sample.rf_level_dbm,
                symbolrate=symbolrate,
            )
            if status is not None:
                matched = (remote, status)
        except Exception:
            matched = None

    if matched is None:
        # Exact production discovery/matching semantics as fallback.
        matched = client.read_input(
            channel=sample.channel,
            rf_khz=sample.frequency_khz,
            lo_khz=sample.lo_frequency_khz,
            snr_db=sample.snr_db,
            rf_level_dbm=sample.rf_level_dbm,
            symbolrate=symbolrate,
        )
        if matched is None:
            return False
        remote, _ = matched
        cache[key] = remote
        try:
            _save_remote_cache(module_cache_path, cache)
        except Exception:
            pass

    remote, status = matched
    if status.locked is not True:
        return False

    sample.web_remote_id = remote
    sample.detected_constellation = status.constellation
    sample.detected_code_rate = status.code_rate
    sample.detected_isi = status.isi
    sample.web_ber_text = status.ber_text
    sample.web_if_frequency_khz = status.frequency_khz
    sample.web_tuner_id = status.tuner_id
    if status.current_bitrate_bps is not None and status.current_bitrate_bps > 0:
        sample.web_ts_bitrate_bps = status.current_bitrate_bps
    return True
