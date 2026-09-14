from __future__ import annotations

import http.cookiejar
import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebTunerStatus:
    tuner_id: int

    # Live tuner values from tuner_flux.xmlc
    locked: bool | None = None
    frequency_khz: int | None = None
    level_dbm: float | None = None
    snr_db: float | None = None
    ber_text: str | None = None
    constellation: str | None = None
    code_rate: str | None = None
    isi: int | None = None

    # Live TS bitrate from tsio/inputs_conf_flux.xmlc
    current_bitrate_bps: int | None = None

    # v1.4 deterministic identity from tuner.xmlc
    configured_frequency_khz: int | None = None
    configured_symbolrate: int | None = None


class GT34WebStatusClient:
    """
    Read-only WISI GT34 web-status XML client.

    SNMP remains authoritative for receiver/input identity and normal
    monitoring.

    The web XML interface is used only as optional enrichment for values
    which are available internally in the GT34 web interface but are not
    exposed as useful detected values by the current SNMP agent, including:

      - detected constellation
      - detected FEC/code rate
      - operational ISI
      - textual BER
      - current input TS bitrate

    v1.4 also reads tuner.xmlc and uses configured RF frequency plus
    configured symbol rate as deterministic evidence that a web tuner
    corresponds to the SNMP receiver input.

    Matching is deliberately fail-closed.
    """

    _REMOTE_RE = re.compile(
        r"remote/([0-9]+(?:_[0-9]+){3})/",
        re.I,
    )

    _IP_RE = re.compile(
        r"\b(\d{1,3}(?:[._]\d{1,3}){3})\b"
    )

    def __init__(
        self,
        host: str,
        timeout_seconds: float = 1.5,
    ) -> None:
        self.host = host
        self.base = f"http://{host}"
        self.timeout_seconds = timeout_seconds

        jar = http.cookiejar.CookieJar()

        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )

        self._candidate_cache: list[str] | None = None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request(
        self,
        path: str,
        data: bytes | None = None,
    ) -> bytes:
        url = self.base + path

        headers = {
            "Accept": "*/*",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": self.base + "/",
        }

        if data is not None:
            headers["Content-Type"] = "text/plain; charset=UTF-8"

        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
        )

        with self._opener.open(
            request,
            timeout=self.timeout_seconds,
        ) as response:
            return response.read()

    def _initialise(self) -> bytes:
        return self._request("/")

    # ------------------------------------------------------------------
    # Internal GT34 remote discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_remote(
        value: str,
    ) -> str | None:
        value = value.replace(".", "_")

        parts = value.split("_")

        if len(parts) != 4:
            return None

        try:
            octets = [
                int(part)
                for part in parts
            ]
        except ValueError:
            return None

        if any(
            octet < 0 or octet > 255
            for octet in octets
        ):
            return None

        return "_".join(
            str(octet)
            for octet in octets
        )

    def discover_remote_candidates(
        self,
    ) -> list[str]:
        if self._candidate_cache is not None:
            return list(
                self._candidate_cache
            )

        blobs: list[bytes] = []

        discovery_paths = (
            "/",
            "/modules.xmlc",
            "/am/childunitlist.xmlc",
            "/am/groupunitlist.xmlc",
            "/am/sysunitlist.xmlc",
        )

        for path in discovery_paths:
            try:
                blobs.append(
                    self._request(path)
                )
            except Exception:
                continue

        text = "\n".join(
            blob.decode(
                "utf-8",
                errors="ignore",
            )
            for blob in blobs
        )

        candidates: list[str] = []

        # Preferred discovery form:
        #
        # remote/169_254_x_x/...
        for match in self._REMOTE_RE.finditer(text):
            candidate = self._normalise_remote(
                match.group(1)
            )

            if (
                candidate is not None
                and candidate not in candidates
            ):
                candidates.append(candidate)

        # Some controller resources expose the internal IP without a
        # literal "remote/" prefix.
        #
        # These are only candidates. They must subsequently answer the
        # GT34 resource request and pass all receiver identity checks.
        for match in self._IP_RE.finditer(text):
            candidate = self._normalise_remote(
                match.group(1)
            )

            if (
                candidate is not None
                and candidate not in candidates
            ):
                candidates.append(candidate)

        self._candidate_cache = candidates

        return list(candidates)

    # ------------------------------------------------------------------
    # GT34 XML resource retrieval
    # ------------------------------------------------------------------

    def _fetch_remote_pair(
        self,
        remote: str,
    ) -> bytes:
        """
        Fetch all three resources required for v1.4.

        The historical method name _fetch_remote_pair is intentionally
        retained so existing diagnostics/callers remain compatible.

        Resource 1:
            tsio/inputs_conf_flux.xmlc
            -> live input TS bitrate

        Resource 2:
            tuner_flux.xmlc
            -> live tuner status

        Resource 3:
            tuner.xmlc
            -> configured tuner RF frequency and symbol rate
        """

        body = (
            f"remote/{remote}/tsio/inputs_conf_flux.xmlc\r\n"
            f"remote/{remote}/tuner_flux.xmlc\r\n"
            f"remote/{remote}/tuner.xmlc\r\n"
            "END"
        ).encode("utf-8")

        return self._request(
            "/data.xmlc?size=3",
            data=body,
        )

    # ------------------------------------------------------------------
    # XML helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _text(
        parent: ET.Element,
        tag: str,
    ) -> str | None:
        node = parent.find(tag)

        if (
            node is None
            or node.text is None
        ):
            return None

        value = node.text.strip()

        return value or None

    @staticmethod
    def _as_int(
        value: str | None,
    ) -> int | None:
        if value is None:
            return None

        try:
            return int(value)
        except ValueError:
            return None

    @staticmethod
    def _as_float(
        value: str | None,
    ) -> float | None:
        if value is None:
            return None

        try:
            return float(value)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # XML parser
    # ------------------------------------------------------------------

    @classmethod
    def parse_status(
        cls,
        payload: bytes,
    ) -> dict[int, WebTunerStatus]:
        root = ET.fromstring(payload)

        # ==============================================================
        # 1. INPUT TS BITRATE
        # ==============================================================

        bitrates: dict[int, int] = {}

        for item in root.findall(
            ".//inputs_flux/input"
        ):
            input_id = cls._as_int(
                item.get("id")
            )

            typespec = item.find(
                "typespec"
            )

            bitrate_parent = (
                typespec
                if typespec is not None
                else item
            )

            bitrate = cls._as_int(
                cls._text(
                    bitrate_parent,
                    "current_bitrate",
                )
            )

            if (
                input_id is not None
                and bitrate is not None
            ):
                bitrates[input_id] = bitrate

        # ==============================================================
        # 2. CONFIGURED TUNER IDENTITY
        #
        # Confirmed response structure:
        #
        # <file name="tuner.xmlc?" ...>
        #     ...
        #     <tuners>
        #         <tuner id="0">
        #             <params>
        #                 <frequency>...</frequency>
        #                 <symbolrate>...</symbolrate>
        #
        # The trailing '?' is removed before comparison.
        # ==============================================================

        configured: dict[
            int,
            tuple[int | None, int | None],
        ] = {}

        for file_node in root.findall(
            ".//file"
        ):
            filename = (
                file_node.get("name")
                or ""
            ).rstrip("?")

            if filename != "tuner.xmlc":
                continue

            for tuner in file_node.findall(
                ".//tuners/tuner"
            ):
                tuner_id = cls._as_int(
                    tuner.get("id")
                )

                if tuner_id is None:
                    continue

                params = tuner.find(
                    "params"
                )

                if params is None:
                    continue

                configured_frequency = (
                    cls._as_int(
                        cls._text(
                            params,
                            "frequency",
                        )
                    )
                )

                configured_symbolrate = (
                    cls._as_int(
                        cls._text(
                            params,
                            "symbolrate",
                        )
                    )
                )

                configured[tuner_id] = (
                    configured_frequency,
                    configured_symbolrate,
                )

        # ==============================================================
        # 3. LIVE TUNER STATUS
        #
        # Only tuner_flux.xmlc is used for live status.
        #
        # This prevents the similarly structured tuner.xmlc configuration
        # nodes from being mistaken for live tuner-status nodes.
        # ==============================================================

        result: dict[
            int,
            WebTunerStatus,
        ] = {}

        for file_node in root.findall(
            ".//file"
        ):
            filename = (
                file_node.get("name")
                or ""
            ).rstrip("?")

            if filename != "tuner_flux.xmlc":
                continue

            for tuner in file_node.findall(
                ".//tuners/tuner"
            ):
                tuner_id = cls._as_int(
                    tuner.get("id")
                )

                status = tuner.find(
                    "status"
                )

                if (
                    tuner_id is None
                    or status is None
                ):
                    continue

                locked_raw = cls._as_int(
                    cls._text(
                        status,
                        "locked",
                    )
                )

                constellation = cls._text(
                    status,
                    "constellation",
                )

                code_rate = cls._text(
                    status,
                    "code_rate",
                )

                isi = cls._as_int(
                    cls._text(
                        status,
                        "isi",
                    )
                )

                (
                    configured_frequency,
                    configured_symbolrate,
                ) = configured.get(
                    tuner_id,
                    (
                        None,
                        None,
                    ),
                )

                result[tuner_id] = (
                    WebTunerStatus(
                        tuner_id=tuner_id,

                        locked=(
                            None
                            if locked_raw is None
                            else locked_raw == 1
                        ),

                        # Live tuner_flux frequency is the receiver IF,
                        # not necessarily the original satellite RF.
                        frequency_khz=cls._as_int(
                            cls._text(
                                status,
                                "frequency",
                            )
                        ),

                        level_dbm=cls._as_float(
                            cls._text(
                                status,
                                "level",
                            )
                        ),

                        snr_db=cls._as_float(
                            cls._text(
                                status,
                                "snr",
                            )
                        ),

                        ber_text=cls._text(
                            status,
                            "ber",
                        ),

                        constellation=(
                            None
                            if constellation
                            in (
                                None,
                                "Unknown",
                                "N/A",
                            )
                            else constellation
                        ),

                        code_rate=(
                            None
                            if code_rate
                            in (
                                None,
                                "Unknown",
                                "N/A",
                            )
                            else code_rate
                        ),

                        isi=isi,

                        current_bitrate_bps=(
                            bitrates.get(
                                tuner_id
                            )
                        ),

                        configured_frequency_khz=(
                            configured_frequency
                        ),

                        configured_symbolrate=(
                            configured_symbolrate
                        ),
                    )
                )

        return result

    # ------------------------------------------------------------------
    # Expected IF calculation
    # ------------------------------------------------------------------

    @staticmethod
    def _expected_if_khz(
        rf_khz: int | None,
        lo_khz: int | None,
    ) -> int | None:
        """
        Calculate expected tuner IF only when SNMP exposes a usable LO.

        Some operational GT34 inputs expose LO=0 via SNMP. In that case
        zero is treated as unavailable for IF matching.

        No physical LO frequency is inferred or fabricated.
        """

        if (
            rf_khz is None
            or lo_khz is None
            or lo_khz <= 0
        ):
            return None

        return abs(
            lo_khz - rf_khz
        )

    # ------------------------------------------------------------------
    # Fail-closed tuner matching
    # ------------------------------------------------------------------

    @classmethod
    def _match_tuner(
        cls,
        statuses: dict[
            int,
            WebTunerStatus,
        ],
        channel: int,
        rf_khz: int | None,
        lo_khz: int | None,
        snr_db: float | None,
        rf_level_dbm: float | None = None,
        symbolrate: int | None = None,
    ) -> WebTunerStatus | None:
        """
        Match one SNMP logical receiver input to a web tuner.

        Matching rules:

        1. Only nominal tuner (channel - 1) is eligible.
        2. If tuner.xmlc configuration identity is available, configured
           RF and configured symbol rate must both exactly match SNMP.
        3. Healthy SNMP input requires positive web tuner lock.
        4. If SNMP provides a usable LO, live IF must agree within 2 MHz.
        5. Web/SNMP SNR must agree within 3 dB.
        6. Web/SNMP RF level must agree within 8 dB.

        There is deliberately no arbitrary tuner fallback.
        """

        expected_if = cls._expected_if_khz(
            rf_khz,
            lo_khz,
        )

        # GT34 logical channel 1 maps to web tuner 0,
        # channel 5 maps to tuner 4, etc.
        nominal = statuses.get(
            channel - 1
        )

        if nominal is None:
            return None

        # ==============================================================
        # v1.4 deterministic configuration fingerprint
        # ==============================================================

        config_identity_available = (
            nominal.configured_frequency_khz
            is not None
            or nominal.configured_symbolrate
            is not None
        )

        if config_identity_available:

            # A partial configuration fingerprint is not sufficient.
            if (
                nominal.configured_frequency_khz
                is None
                or nominal.configured_symbolrate
                is None
                or rf_khz is None
                or symbolrate is None
            ):
                return None

            # Both are GT34 configured frequency values in kHz.
            if (
                nominal.configured_frequency_khz
                != rf_khz
            ):
                return None

            # Both are configured symbol rates in Bd.
            if (
                nominal.configured_symbolrate
                != symbolrate
            ):
                return None

        # ==============================================================
        # Positive live-lock safeguard
        # ==============================================================

        if (
            snr_db is not None
            and snr_db > 0
            and nominal.locked is not True
        ):
            return None

        # ==============================================================
        # IF consistency
        # ==============================================================

        if expected_if is not None:

            if nominal.frequency_khz is None:
                return None

            if (
                abs(
                    nominal.frequency_khz
                    - expected_if
                )
                > 2_000
            ):
                return None

        # ==============================================================
        # SNR consistency
        # ==============================================================

        if (
            snr_db is not None
            and snr_db > 0
            and nominal.snr_db is not None
            and abs(
                nominal.snr_db
                - snr_db
            )
            > 3.0
        ):
            return None

        # ==============================================================
        # RF-level consistency
        # ==============================================================

        if (
            rf_level_dbm is not None
            and rf_level_dbm != 0
            and nominal.level_dbm is not None
            and abs(
                nominal.level_dbm
                - rf_level_dbm
            )
            > 8.0
        ):
            return None

        return nominal

    # ------------------------------------------------------------------
    # Internal remote selection
    # ------------------------------------------------------------------

    def read_input(
        self,
        channel: int,
        rf_khz: int | None,
        lo_khz: int | None,
        snr_db: float | None,
        rf_level_dbm: float | None = None,
        symbolrate: int | None = None,
    ) -> tuple[
        str,
        WebTunerStatus,
    ] | None:
        """
        Search the discovered internal GT34 resources.

        Exactly one internal remote must satisfy the complete fingerprint.

        Zero matches -> no enrichment.
        Multiple matches -> ambiguous -> no enrichment.
        """

        try:
            self._initialise()
        except Exception:
            # Discovery endpoints may still work even if the initial
            # root request is not useful.
            pass

        matches: list[
            tuple[
                str,
                WebTunerStatus,
            ]
        ] = []

        for remote in (
            self.discover_remote_candidates()
        ):

            # Never consider the non-address placeholder.
            if remote == "0_0_0_0":
                continue

            try:
                payload = (
                    self._fetch_remote_pair(
                        remote
                    )
                )

                statuses = (
                    self.parse_status(
                        payload
                    )
                )

            except (
                ET.ParseError,
                urllib.error.URLError,
                TimeoutError,
                OSError,
            ):
                continue

            except Exception:
                # Optional enrichment must never break the SNMP sensor.
                continue

            status = self._match_tuner(
                statuses=statuses,
                channel=channel,
                rf_khz=rf_khz,
                lo_khz=lo_khz,
                snr_db=snr_db,
                rf_level_dbm=rf_level_dbm,
                symbolrate=symbolrate,
            )

            if status is not None:
                matches.append(
                    (
                        remote,
                        status,
                    )
                )

        # Exactly one verified match is required.
        if len(matches) == 1:
            return matches[0]

        if len(matches) > 1:
            log.debug(
                "Ambiguous GT34 web match "
                "host=%s channel=%s remotes=%s",
                self.host,
                channel,
                [
                    remote
                    for remote, _status
                    in matches
                ],
            )

        return None


def enrich_sample_from_web(
    sample,
    host: str,
    timeout_seconds: float = 3.0,
) -> bool:
    """
    Best-effort, fail-closed GT34 web enrichment.

    SNMP remains authoritative.

    The web interface is never allowed to convert an SNMP-unhealthy input
    into an apparently healthy one.
    """

    try:
        # ==============================================================
        # CRITICAL SAFETY GATE
        #
        # Never enrich an input which is not operational according to
        # authoritative SNMP + TS state.
        # ==============================================================

        if (
            not sample.effective_locked
            or sample.ts_present is not True
        ):
            return False

        # ==============================================================
        # IMPORTANT v1.4 FIX
        #
        # ReceiverSample's actual production field is symbol_rate_bd.
        #
        # Do not use symbol_rate, symbol_rate_baud or symbol_rate_bps.
        # ==============================================================

        symbolrate = getattr(
            sample,
            "symbol_rate_bd",
            None,
        )

        client = GT34WebStatusClient(
            host,
            timeout_seconds=timeout_seconds,
        )

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

        remote, status = matched

        # Belt-and-braces positive-lock check.
        if status.locked is not True:
            return False

        # ==============================================================
        # Apply web-derived enrichment
        # ==============================================================

        sample.web_remote_id = remote

        sample.detected_constellation = (
            status.constellation
        )

        sample.detected_code_rate = (
            status.code_rate
        )

        sample.detected_isi = (
            status.isi
        )

        sample.web_ber_text = (
            status.ber_text
        )

        sample.web_if_frequency_khz = (
            status.frequency_khz
        )

        sample.web_tuner_id = (
            status.tuner_id
        )

        if (
            status.current_bitrate_bps
            is not None
            and status.current_bitrate_bps > 0
        ):
            sample.web_ts_bitrate_bps = (
                status.current_bitrate_bps
            )

        return True

    except Exception as exc:
        # Web enrichment is optional. Any web/API/parsing problem must
        # leave the existing SNMP sensor operational rather than causing
        # the PRTG sensor itself to fail.
        log.debug(
            "Optional GT34 web enrichment failed "
            "host=%s module=%s channel=%s error=%s",
            host,
            getattr(sample, "module", None),
            getattr(sample, "channel", None),
            exc,
        )

        return False