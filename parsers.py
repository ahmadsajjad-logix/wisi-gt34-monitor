import xml.etree.ElementTree as ET


# ============================================================
# GENERIC HELPERS
# ============================================================

def to_int(value):
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_float(value):
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_bool(value):
    if value is None:
        return None

    value = str(value).strip().lower()

    if value in ("true", "yes", "1", "on"):
        return True

    if value in ("false", "no", "0", "off"):
        return False

    return None


def element_text(element, child_name):
    if element is None:
        return None

    child = element.find(child_name)

    if child is None or child.text is None:
        return None

    return child.text.strip()


def parse_xml(xml_text):
    """
    Parse a WISI XMLC response and return the <uixml> element.

    Raises ValueError if the reply cannot be parsed or does not
    contain the expected WISI <uixml> structure.
    """

    try:
        root = ET.fromstring(xml_text)

    except ET.ParseError as exc:
        raise ValueError(
            f"Invalid XML response: {exc}"
        ) from exc

    uixml = root.find(".//uixml")

    if uixml is None:
        raise ValueError(
            "WISI response contains no <uixml> element"
        )

    return uixml


def get_attr_any(element, names):
    """
    Return the first matching XML attribute from a list of
    possible attribute names.
    """

    if element is None:
        return None

    for name in names:
        if name in element.attrib:
            return element.get(name)

    return None


def get_child_text_any(element, names):
    if element is None:
        return None

    for name in names:
        value = element_text(
            element,
            name,
        )

        if value is not None:
            return value

    return None


# ============================================================
# BER PARSING
# ============================================================

def parse_ber(value):
    """
    WISI may return BER values such as:

        <1.0E-08
        2.5E-06
        N/A

    Returns both the original representation and a numeric
    approximation where possible.

    For '<1.0E-08' the numeric value stored is 1.0e-8 and
    ber_is_upper_bound=True.
    """

    result = {
        "text": value,
        "value": None,
        "is_upper_bound": False,
    }

    if value is None:
        return result

    text = str(value).strip()

    if not text:
        return result

    if text.upper() in (
        "N/A",
        "NA",
        "NONE",
        "-",
    ):
        return result

    if text.startswith("<"):
        result["is_upper_bound"] = True

        text = text[1:].strip()

    try:
        result["value"] = float(text)

    except ValueError:
        pass

    return result


# ============================================================
# TUNER / DEMODULATOR
# ============================================================

def parse_tuner_flux(xml_text):
    """Parse tuner_flux.xmlc into records indexed by tuner/input id.

    WISI places live tuner telemetry inside each <tuner>/<status> child,
    not as attributes on the <tuner> element itself.
    """
    uixml = parse_xml(xml_text)
    results = {}

    for tuner_el in uixml.findall(".//tuners/tuner"):
        input_id = to_int(tuner_el.get("id"))
        if input_id is None:
            continue

        status_el = tuner_el.find("status")
        if status_el is None:
            continue

        lock_raw = element_text(status_el, "locked")
        ber_raw = element_text(status_el, "ber")
        ber = parse_ber(ber_raw)

        constellation_el = status_el.find("constellation")
        code_rate_el = status_el.find("code_rate")
        isi_el = status_el.find("isi")

        results[input_id] = {
            "input_id": input_id,
            "enabled": to_bool(tuner_el.get("enabled")),
            "state": to_int(tuner_el.get("state")),
            "disabled": to_bool(element_text(status_el, "disabled")),
            "lock_state": to_int(lock_raw),
            "rf_level_dbm": to_float(element_text(status_el, "level")),
            "snr_db": to_float(element_text(status_el, "snr")),
            "ber_text": ber["text"],
            "ber_value": ber["value"],
            "ber_is_upper_bound": ber["is_upper_bound"],
            "frequency_raw": to_float(element_text(status_el, "frequency")),
            "frequency_offset_raw": None,
            "symbol_rate": None,
            "modulation": (
                constellation_el.text.strip()
                if constellation_el is not None and constellation_el.text
                else None
            ),
            "modulation_id": (
                to_int(constellation_el.get("id"))
                if constellation_el is not None
                else None
            ),
            "fec": (
                code_rate_el.text.strip()
                if code_rate_el is not None and code_rate_el.text
                else None
            ),
            "fec_id": (
                to_int(code_rate_el.get("id"))
                if code_rate_el is not None
                else None
            ),
            "isi": (
                to_int(isi_el.text.strip())
                if isi_el is not None and isi_el.text
                else None
            ),
            "isi_conf": (
                to_int(isi_el.get("isi_conf"))
                if isi_el is not None
                else None
            ),
            "quality": to_float(element_text(status_el, "quality")),
        }

    return results


# ============================================================
# TRANSPORT STREAM BITRATE
# ============================================================

def parse_ts_flux(xml_text):
    """Parse tsio/inputs_conf_flux.xmlc.

    Bitrate values are child elements of <typespec>, not XML attributes.
    """
    uixml = parse_xml(xml_text)
    results = {}

    for input_el in uixml.findall(".//inputs_flux/input"):
        input_id = to_int(input_el.get("id"))
        if input_id is None:
            continue

        typespec_el = input_el.find("typespec")
        if typespec_el is None:
            continue

        results[input_id] = {
            "input_id": input_id,
            "enabled": to_bool(input_el.get("enabled")),
            "hidden": to_bool(input_el.get("hidden")),
            "static": to_bool(input_el.get("static")),
            "current_bitrate_bps": to_float(
                element_text(typespec_el, "current_bitrate")
            ),
            "min_bitrate_bps": to_float(
                element_text(typespec_el, "min_bitrate")
            ),
            "max_bitrate_bps": to_float(
                element_text(typespec_el, "max_bitrate")
            ),
        }

    return results


# ============================================================
# PIDMAPPER / TS ERRORS / PID TELEMETRY
# ============================================================

def parse_pidmapper(xml_text):
    """
    Parse tsio/pidmapper.xmlc.

    Provides:
      - input TEI counter
      - input sync-error counter
      - input CC counter
      - PID bitrate
      - PID packet count
      - PID CC errors
      - PCR flag
      - scrambling
      - PES information
    """

    uixml = parse_xml(xml_text)

    results = {}

    for input_el in uixml.findall(".//input"):

        input_id = to_int(
            input_el.get("id")
        )

        if input_id is None:
            continue

        entry = {
            "input_id": input_id,

            "cc_error_events":
                to_int(
                    input_el.get(
                        "cc_error_events"
                    )
                ),

            "tei_error_events":
                to_int(
                    input_el.get(
                        "tei_error_events"
                    )
                ),

            "sync_error_events":
                to_int(
                    input_el.get(
                        "sync_error_events"
                    )
                ),

            "pids": {},
        }

        for pid_el in input_el.findall("pid"):

            pid = to_int(
                pid_el.get("pid")
            )

            if pid is None:
                continue

            statistics = pid_el.find(
                "statistics"
            )

            entry["pids"][pid] = {
                "pid": pid,

                "bitrate_bps":
                    to_float(
                        pid_el.get(
                            "bitrate"
                        )
                    ),

                "packet_count":
                    to_int(
                        pid_el.get(
                            "count"
                        )
                    ),

                "pcr_present":
                    to_bool(
                        pid_el.get(
                            "pcr"
                        )
                    ),

                "scrambled":
                    to_bool(
                        pid_el.get(
                            "scrambled"
                        )
                    ),

                "pes_header":
                    to_bool(
                        pid_el.get(
                            "peshdr"
                        )
                    ),

                "pes_scrambled":
                    to_bool(
                        pid_el.get(
                            "pes_scrambled"
                        )
                    ),

                "flags":
                    pid_el.get(
                        "flags"
                    ),

                "cc_error_events":
                    (
                        to_int(
                            statistics.get(
                                "cc_error_events"
                            )
                        )
                        if statistics is not None
                        else None
                    ),
            }

        results[input_id] = entry

    return results


# ============================================================
# PCR / INPUT REGULATOR
# ============================================================

PCR_COUNTER_FIELDS = (
    "ref_discontinuities",
    "pcr_accuracy_errors",
    "pcr_repetition_errors",
    "pcr_discontinuity_indicator_errors",
)

INPUT_REGULATOR_COUNTER_FIELDS = (
    "to_wait_state",
    "playout_fifo_reset",
    "unref_discontinuity",
    "sample_ignored",
    "into_freerunning",
    "pcr_pid_changed",
)


def _parse_statistics_attributes(element, fields):
    result = {}
    for field in fields:
        result[field] = (
            to_int(element.get(field))
            if element is not None
            else None
        )
    return result


def parse_pcr_inputs(xml_text):
    """Parse tsio/input_regulator/inputs.xmlc."""
    uixml = parse_xml(xml_text)
    results = {}

    for ts_el in uixml.findall(".//ts"):
        input_id = to_int(ts_el.get("input_id"))
        if input_id is None:
            continue

        # GT34 exposes internal/non-tuner TS regulator instances using
        # high input IDs (for example 32768+). Production tuner monitoring
        # only uses the eight physical GT34 tuner/input IDs 0..7.
        if not 0 <= input_id <= 7:
            continue

        rate_el = ts_el.find("rate")
        buffer_el = ts_el.find("buffer")
        input_stats_el = ts_el.find("statistics")
        pcr_monitor_el = ts_el.find("pcr_monitor")
        monitor_stats_el = (
            pcr_monitor_el.find("statistics")
            if pcr_monitor_el is not None
            else None
        )

        entry = {
            "input_id": input_id,
            "hwid": to_int(ts_el.get("hwid")),
            "bitrate_mode": to_int(ts_el.get("bitrate_mode")),
            "pidmapper_jump": to_int(ts_el.get("pidmapper_jump")),
            "rate": {
                "changed": to_int(rate_el.get("changed")) if rate_el is not None else None,
                "changed_time": to_int(rate_el.get("changed_time")) if rate_el is not None else None,
                "raw": rate_el.text.strip() if rate_el is not None and rate_el.text else None,
            },
            "buffer": {
                "conf_jitter": to_float(buffer_el.get("conf_jitter")) if buffer_el is not None else None,
                "jitter": to_float(buffer_el.get("jitter")) if buffer_el is not None else None,
                "segment": to_int(buffer_el.get("segment")) if buffer_el is not None else None,
                "fill_wait": to_bool(buffer_el.get("fill_wait")) if buffer_el is not None else None,
                "freerunning": to_bool(buffer_el.get("freerunning")) if buffer_el is not None else None,
                "size": to_int(buffer_el.get("size")) if buffer_el is not None else None,
                "level": to_int(buffer_el.get("level")) if buffer_el is not None else None,
            },
            "statistics": _parse_statistics_attributes(
                input_stats_el,
                INPUT_REGULATOR_COUNTER_FIELDS,
            ),
            "pcr_monitor": {
                "enabled": to_bool(pcr_monitor_el.get("enabled")) if pcr_monitor_el is not None else None,
                "periodic_rate": to_float(element_text(pcr_monitor_el, "periodic_rate")),
                "statistics": _parse_statistics_attributes(
                    monitor_stats_el,
                    PCR_COUNTER_FIELDS + (
                        "jitter_min",
                        "jitter_max",
                        "pcr_diff_max",
                    ),
                ),
            },
            "pcr_pids": {},
        }

        if pcr_monitor_el is not None:
            for pcr_el in pcr_monitor_el.findall("pcr"):
                pid = to_int(pcr_el.get("pid"))
                if pid is None:
                    continue

                pcr_stats_el = pcr_el.find("statistics")
                recovery_calc_el = pcr_el.find("recovery_calc")

                entry["pcr_pids"][pid] = {
                    "pid": pid,
                    "last_seen": to_int(element_text(pcr_el, "last_seen")),
                    "pcr_bitrate": to_float(element_text(pcr_el, "pcr_bitrate")),
                    "stc_bitrate": to_float(element_text(pcr_el, "stc_bitrate")),
                    "stc_factor": to_float(element_text(pcr_el, "stc_factor")),
                    "recovery_reminder": to_int(element_text(pcr_el, "recovery_reminder")),
                    "recovery_calc": {
                        "a": to_float(recovery_calc_el.get("a")) if recovery_calc_el is not None else None,
                        "b": to_float(recovery_calc_el.get("b")) if recovery_calc_el is not None else None,
                        "siga": to_float(recovery_calc_el.get("siga")) if recovery_calc_el is not None else None,
                        "sigb": to_float(recovery_calc_el.get("sigb")) if recovery_calc_el is not None else None,
                    },
                    "statistics": _parse_statistics_attributes(
                        pcr_stats_el,
                        PCR_COUNTER_FIELDS + (
                            "jitter_min",
                            "jitter_max",
                            "pcr_diff_max",
                        ),
                    ),
                }

        results[input_id] = entry

    return results


# ============================================================
# TSDB / DVB SERVICE DISCOVERY
# ============================================================

STREAM_TYPE_NAMES = {
    1: "MPEG-1 video",
    2: "MPEG-2 video",
    3: "MPEG-1 audio",
    4: "MPEG-2 audio",
    6: "Private data",
    15: "AAC audio",
    16: "MPEG-4 video",
    17: "AAC LATM audio",
    27: "H.264/AVC video",
    36: "H.265/HEVC video",
}


def _first_pid(parent, path):
    if parent is None:
        return None
    pid_el = parent.find(path)
    if pid_el is None:
        return None
    return to_int(pid_el.get("pid"))


def _parse_language(es_el):
    lang_el = es_el.find("iso_639_language")
    if lang_el is None:
        return None, None
    return (
        lang_el.get("language"),
        to_int(lang_el.get("audio_type")),
    )


def parse_tsdb_input(xml_text):
    """Parse tsdb/input.xmlc transport-stream/service metadata."""
    uixml = parse_xml(xml_text)
    results = {}

    for ts_el in uixml.findall(".//ts"):
        tsio_el = ts_el.find("tsio")
        if tsio_el is None:
            continue

        input_id = to_int(tsio_el.get("input_id"))
        if input_id is None:
            continue

        psisi_el = ts_el.find("psisi")
        psisi = {}
        if psisi_el is not None:
            for table_el in list(psisi_el):
                table_name = table_el.tag.lower()
                pid_el = table_el.find("pid")
                pid = to_int(pid_el.get("pid")) if pid_el is not None else None
                psisi.setdefault(table_name, []).append(pid)

        services = {}
        services_el = ts_el.find("services")
        if services_el is not None:
            for service_el in services_el.findall("service"):
                service_id = to_int(service_el.get("id"))
                if service_id is None:
                    continue

                service = {
                    "service_id": service_id,
                    "running_status": to_int(service_el.get("running_status")),
                    "service_type": to_int(service_el.get("type")),
                    "provider_name": service_el.get("provider"),
                    "service_name": service_el.get("name"),
                    "pmt_pid": _first_pid(service_el, "pmt/pid"),
                    "pcr_pid": _first_pid(service_el, "pcr/pid"),
                    "streams": [],
                }

                for es_el in service_el.findall("es"):
                    pid = _first_pid(es_el, "pid")
                    stream_type = to_int(es_el.get("stream_type"))
                    language, audio_type = _parse_language(es_el)

                    ecm = []
                    for ecm_el in es_el.findall("ecm"):
                        ecm_pid = _first_pid(ecm_el, "pid")
                        ecm.append({
                            "ca_system_id": to_int(ecm_el.get("ca_system_id")),
                            "pid": ecm_pid,
                        })

                    service["streams"].append({
                        "pid": pid,
                        "stream_type": stream_type,
                        "stream_type_name": STREAM_TYPE_NAMES.get(stream_type, f"Stream type {stream_type}" if stream_type is not None else None),
                        "language": language,
                        "audio_type": audio_type,
                        "ecm": ecm,
                    })

                services[service_id] = service

        emm = []
        for emm_el in ts_el.findall("emm"):
            emm.append({
                "ca_system_id": to_int(emm_el.get("ca_system_id")),
                "pid": _first_pid(emm_el, "pid"),
            })

        results[input_id] = {
            "input_id": input_id,
            "ts_internal_id": to_int(ts_el.get("id")),
            "transport_stream_id": to_int(ts_el.get("transport_stream_id")),
            "original_network_id": to_int(ts_el.get("original_network_id")),
            "network_id": to_int(ts_el.get("network_id")),
            "psisi": psisi,
            "emm": emm,
            "services": services,
        }

    return results
